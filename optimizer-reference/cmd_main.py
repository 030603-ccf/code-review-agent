"""CLI 入口

    python -m cra review   <项目路径>      审查，产物存 runs/<时间戳>/
    python -m cra optimize runs/<时间戳>   基于审查结果优化（全程只动副本）
    python -m cra verify   runs/<时间戳>   独立复查：不重新派修复单，直接对现有副本重新验证
    python -m cra eval <项目路径> --labels <标签文件>     先审查再比对，产物存 eval/runs/<时间戳>/
    python -m cra eval --run <run目录> --labels <标签文件>  零 LLM：直接比对已有 findings.json

Phase 2 产物：project_map.json  findings.json  report.md  run_state.json  events.jsonl
Phase 3 新增：optimized_copy/  prompts/*.task.md  opt_state.json  verification.md
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from cra.eval import load_labels, match
from cra.llm.client import LLMClient
from cra.memory.run_state import RunState
from cra.optimizer.copier import create_workspace, hash_tree
from cra.optimizer.fixer import make_fixer
from cra.optimizer.loop import optimize_loop
from cra.optimizer.opt_state import OptState
from cra.optimizer.verifier import verify_fixes
from cra.orchestrator.events import EventBus
from cra.orchestrator.pipeline import Pipeline
from cra.schemas.finding import Finding

# 新模块：符号提取后端、LSP 客户端、配置解析
from cra.analysis.symbol_backend import SymbolBackend, get_backend
from cra.analysis.lsp_client import LSPClient


# 二级审查的"关闭词表"：CLI 和 Web UI 共用同一套词（大小写、首尾空格不敏感）。
# 空串也算——Web 表单的"不启用"选项、shell 里手滑传 "" 都落在这里。
_SECOND_OFF_VALUES = frozenset({"", "none", "off"})


def _resolve_second_name(cli_value: str | None, cfg_value: str | None) -> str | None:
    """决定二级审查用哪个 profile。返回 None = 不启用（单模型初审模式）。

    三态语义：
      1. CLI 没传（None）      -> 回退 config.yaml 的 review.second_profile
      2. CLI 显式传 ""/none/off -> 明确禁用（就算 config 里配了云模型也不用）
      3. 其他值                 -> 用 CLI 指定的 profile

    为什么不能写成 `cli or cfg`：空串是 falsy，用户想"关掉"会被静默
    回退成 config 里的云模型——违背意图还白烧 token。config 值也过一遍
    关闭词表：yaml 里写 `second_profile: none`（字符串）同样算禁用。
    """
    def _norm(v: str | None) -> str | None:
        if v is None:
            return None
        v = str(v).strip()
        return None if v.lower() in _SECOND_OFF_VALUES else v

    # None 才回退；空串是"显式禁用"而不是"没传"——这是本函数存在的意义
    return _norm(cli_value) if cli_value is not None else _norm(cfg_value)


def _structural_thresholds(cfg: dict) -> tuple[int, int]:
    """从 config.yaml 的 structural 节读结构检测阈值（缺省 60 行 / 4 层）。

    整节缺失、单项缺失都回退默认值：config.yaml 可能是老版本拷来的，
    不能要求用户每次升级代码都补齐新字段（向后兼容的最便宜做法）。
    """
    sc = cfg.get("structural") or {}
    return sc.get("max_function_lines", 60), sc.get("max_nesting_depth", 4)


def _resolve_symbol_backend(cfg: dict) -> SymbolBackend:
    """从 config.yaml 的 analysis.symbol_backend 字段决定符号提取后端。

    可选值：auto | heuristic | tree_sitter，缺省回退 auto。
    """
    name = (cfg.get("analysis") or {}).get("symbol_backend", "auto")
    return get_backend(name)


def _resolve_lsp_config(cfg: dict) -> "LSPClient | None":
    """从 config.yaml 的 lsp 节构造 LSPClient。

    enabled 为 False 或不存在时返回 None（不启用 LSP）。
    """
    lsp_cfg = cfg.get("lsp") or {}
    if not lsp_cfg.get("enabled", False):
        return None
    servers = lsp_cfg.get("servers") or {}
    if not servers:
        return None
    # 取第一个 server 命令（字典保持插入顺序，Python 3.7+）
    first_cmd_str = next(iter(servers.values()))
    cmd = first_cmd_str.split()
    timeout = lsp_cfg.get("timeout", 10)
    return LSPClient(cmd=cmd, timeout=float(timeout))


def _resolve_distill_config(cfg: dict) -> tuple[bool, int, bool, float]:
    """从 config.yaml 的 analysis 节读取蒸馏与老化排序配置。

    Returns:
        (distill_enabled, distill_max_chars, aging_enabled, history_weight)
    """
    analysis = cfg.get("analysis") or {}
    distill_enabled = bool(analysis.get("distill", False))
    distill_max_chars = int(analysis.get("distill_max_chars", 500))
    aging = analysis.get("aging") or {}
    aging_enabled = bool(aging.get("enabled", False))
    history_weight = float(aging.get("history_weight", 2.0))
    return distill_enabled, distill_max_chars, aging_enabled, history_weight


def _resolve_mistakes_path(cfg: dict) -> "str | None":
    """从 config.yaml 的 mistake_notebook 节解析错题本路径。

    返回 None = 不启用错题本（enabled=false 或没配置）。
    路径是相对于项目根的（和 config.yaml 同级）。
    """
    mn = cfg.get("mistake_notebook") or {}
    if not mn.get("enabled", False):
        return None
    from pathlib import Path as _P
    raw = mn.get("path", "memory/mistakes.jsonl")
    return str(_P(raw))


def cmd_review(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"错误：{root} 不是目录")
        return 1

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path("runs") / run_id

    # 续跑模式：复用旧 run 目录（初审断点续跑：已审块跳过、发现读回）。
    # 场景：初审跑了几百个块被超时/断电打断，重跑不必从零开始
    if args.resume:
        run_dir = Path(args.resume)
        if not run_dir.is_dir():
            print(f"错误：{run_dir} 不是目录")
            return 1
        run_id = run_dir.name
    else:
        run_dir.mkdir(parents=True, exist_ok=True)

    # run_state.json 在就 load（保留历史进度），不在就新建
    state_path = run_dir / "run_state.json"
    state = RunState.load(state_path) if args.resume and state_path.exists() \
        else RunState(run_id)

    client = LLMClient.from_config(args.config, profile=args.profile)

    # 二级审查（终审仲裁）：三态语义见 _resolve_second_name——
    # 没传参回退 config，显式传 none/off/"" 是禁用，不是回退
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    second_name = _resolve_second_name(
        args.second_profile, (cfg.get("review") or {}).get("second_profile"))
    second_client = (LLMClient.from_config(args.config, profile=second_name)
                     if second_name else None)

    # STRUCT 阶段（确定性结构检测）的阈值：config.yaml 的 structural 节，
    # 缺省 60 行 / 4 层。在这里（CLI 层）解析配置，Pipeline 只收数字
    max_fn_lines, max_nest = _structural_thresholds(cfg)

    # 新特性配置解析：符号提取后端、LSP 客户端、蒸馏与老化排序
    symbol_backend = _resolve_symbol_backend(cfg)
    lsp_client = _resolve_lsp_config(cfg)
    distill_enabled, distill_max_chars, aging_enabled, history_weight = _resolve_distill_config(cfg)

    bus = EventBus(run_dir)

    print(f"目标：{root}")
    print(f"产物目录：{run_dir}（并发度 {args.concurrency}）"
          + (" · 续跑模式" if args.resume else ""))
    print(f"初审模型：{client.config.model}"
          + (f" · 二级审查：{second_client.config.model}"
             if second_client else "（未启用二级审查）"))
    print(f"结构检测：函数 ≤{max_fn_lines} 行 / 嵌套 ≤{max_nest} 层（零 LLM）")
    print(f"符号后端：{cfg.get('analysis', {}).get('symbol_backend', 'auto')}"
          + (f" · LSP：已启用" if lsp_client else "")
          + (f" · 蒸馏：已启用" if distill_enabled else ""))

    t0 = time.time()
    pipeline = Pipeline(
        root=root, run_dir=run_dir, client=client,
        concurrency=args.concurrency, bus=bus, state=state,
        second_client=second_client,
        max_function_lines=max_fn_lines, max_nesting_depth=max_nest,
        backend=symbol_backend, lsp_client=lsp_client,
        distill=distill_enabled, distill_max_chars=distill_max_chars,
        aging_enabled=aging_enabled, history_weight=history_weight,
        mistakes_path=_resolve_mistakes_path(cfg),
        mistakes_max_inject=cfg.get("mistake_notebook", {}).get("max_inject", 5),
    )
    findings = pipeline.run()
    elapsed = time.time() - t0

    print(f"\n完成：{len(findings)} 个问题，{elapsed:.0f} 秒，"
          f"{client.total_requests} 次请求，{client.total_tokens_used} tokens")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    """优化闭环：副本 -> 任务书 -> 修改 -> 复查。四步都有产物落盘，可逐步人工检查。"""
    run_dir = Path(args.run_dir)
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"错误：{findings_path} 不存在，先跑 review 生成审查结果")
        return 1

    findings = [Finding(**d) for d in json.loads(
        findings_path.read_text(encoding="utf-8"))]
    if not findings:
        print("审查结果是 0 条漏洞，没有需要修复的内容")
        return 0

    # 原项目路径从审查阶段的 project_map.json 里取——不依赖用户记忆
    pm = json.loads((run_dir / "project_map.json").read_text(encoding="utf-8"))
    target_root = Path(pm["root"])

    # fixer 配置：CLI 参数优先，其次是 config.yaml，最后是默认值
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    fixer_cfg = cfg.get("fixer", {})
    backend = args.backend or fixer_cfg.get("backend", "api")
    prompt_mode = args.prompt_mode or fixer_cfg.get("prompt_mode", "template")

    # ---------- 第 1 步：建副本 + 修复前哈希快照 ----------
    copy_root = create_workspace(target_root, run_dir)
    state = OptState(str(target_root), str(copy_root))
    state.register_findings(findings)
    state.record_hashes("hash_before", hash_tree(copy_root))
    state.data["fixer"] = {"backend": backend, "prompt_mode": prompt_mode}
    print(f"副本：{copy_root}")
    print(f"待修：{len(findings)} 条漏洞（原项目 {target_root} 不会被触碰）")

    # ---------- 第 2~4 步：迭代修复（任务书 -> 修改 -> 复查，最多 N 轮）----------
    # review_client 有两个用途：llm 模式下写任务书 + 每轮复查（都是轻活，14B 够格）
    review_client = LLMClient.from_config(args.config, profile=args.profile)
    if backend == "api":
        api_profile = fixer_cfg.get("api", {}).get(
            "profile", "cloud_api_deepseek-v4-pro")
        fix_client = LLMClient.from_config(args.config, profile=api_profile)
        fixer = make_fixer("api", copy_root, state=state, client=fix_client)
        print(f"修改器：api 后端（{api_profile}）")
    else:
        oc = fixer_cfg.get("opencode", {})
        fixer = make_fixer("opencode", copy_root, state=state,
                           cmd=oc.get("cmd", "opencode run"),
                           timeout=oc.get("timeout", 600))
        print(f"修改器：opencode 后端（{oc.get('cmd', 'opencode run')}）")

    print(f"开始迭代修复（最多 {args.max_rounds} 轮，任务书 {prompt_mode} 模式）……")
    # 构建验证层配置：复查之后跑 ruff/tsc/dotnet build，零 token 确定性检查
    build_check_cfg = cfg.get("build_check")
    summary = optimize_loop(run_dir, copy_root, findings, state, fixer,
                            review_client, max_rounds=args.max_rounds,
                            prompt_mode=prompt_mode,
                            build_check_cfg=build_check_cfg)

    # 收敛过程逐轮打印：能看到"remaining 怎么一轮轮变少（或卡住）"
    for h in summary["history"]:
        print(f"  第 {h['round']} 轮：✅ {h['verified']} · "
              f"❌ {h['remaining']} · ⛔ {h['failed']} · 🆕 {h['new_findings']}")
    if summary.get("stuck"):
        print("⚠️ 检测到停滞：两轮剩余问题完全相同，提前刹车交给你处理")
    if summary["deleted"]:
        print(f"⚠️ 高危：这些文件被修改器删除了：{summary['deleted']}")
    print(f"\n验证报告：{run_dir / 'verification.md'}")
    print(f"各轮任务书：{run_dir / 'prompts'}（按 roundN 分目录）")
    print(f"修复后的代码在副本里：{copy_root}（确认无误后由你手动合并）")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """独立复查：不重新派修复单，直接对现有副本重新验证。

    典型场景：复查的提示词/材料改版后想立刻看效果——
    不必再等修改器跑一轮（opencode 修一次要几分钟，复查只要一两分钟）。
    opt_state.json 里存着副本路径和各文件哈希，load 回来就能用。
    """
    run_dir = Path(args.run_dir)
    state_path = run_dir / "opt_state.json"
    if not state_path.exists():
        print(f"错误：{state_path} 不存在，先跑 optimize 建副本")
        return 1
    state = OptState.load(state_path)
    client = LLMClient.from_config(args.config, profile=args.profile)
    summary = verify_fixes(run_dir, state.data["copy_root"], client, state)
    print(f"判定：✅ {len(summary['verified'])} · "
          f"❌ {len(summary['remaining'])} · ⛔ {len(summary['failed'])} · "
          f"🆕 {len(summary['new_findings'])} · "
          f"🚨 原项目被污染 {len(summary['polluted'])}")
    print(f"验证报告：{run_dir / 'verification.md'}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """评估：把审查结果和 labels.json 标准答案比对，输出召回率/精确率/噪声。

    两种模式（互斥，必须二选一）：
      fresh    给目标路径——先跑完整审查（产物存 eval/runs/<时间戳>/，
               与 review 子命令共用 Pipeline，代码完全同一套），再比对
      compare  --run 指向已有 run 目录——零 LLM，直接读 findings.json
               比对。改标签、调 tolerance 时用这种模式，秒出结果

    评估结果（指标 + 逐条明细）写进 run 目录的 eval_result.json——
    fresh 写进新建的 run 目录，compare 写进你指定的那个。
    """
    # ---- 模式检查：fresh 和 compare 互斥，且必须有一个 ----
    if args.run and args.path:
        print("错误：--run 与目标路径不能同时给（--run 是零 LLM 的纯比对模式）")
        return 1
    if not args.run and not args.path:
        print("错误：请给目标路径（fresh 模式）或 --run <run目录>（compare 模式）")
        return 1

    # 标签先加载：标签写错了就没必要烧 token 跑审查——fail fast
    try:
        labels = load_labels(args.labels)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误：标签文件加载失败：{e}")
        return 1

    findings: list[Finding]
    if args.run:
        # ---------- compare 模式：零 LLM，读已有产物 ----------
        run_dir = Path(args.run)
        findings_path = run_dir / "findings.json"
        if not findings_path.exists():
            print(f"错误：{findings_path} 不存在，--run 应指向一次完整审查的产物目录")
            return 1
        findings = [Finding(**d) for d in json.loads(
            findings_path.read_text(encoding="utf-8"))]
        print(f"模式：compare（零 LLM）· run：{run_dir} · "
              f"{len(findings)} 条 finding")
    else:
        # ---------- fresh 模式：先跑完整审查 ----------
        root = Path(args.path).resolve()
        if not root.is_dir():
            print(f"错误：{root} 不是目录")
            return 1

        # 评估产物和普通审查分开放（eval/runs/ 而不是 runs/）：
        # 评估 run 是"为了打分"跑的，以后批量清理/对比时不会和
        # 日常审查的产物混在一起
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path("eval") / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        client = LLMClient.from_config(args.config, profile=args.profile)

        # 二级审查：三态语义与 cmd_review 完全一致（没传回退 config，
        # 显式 none/off/"" 是禁用）——评估要测的就是你真实用的那套配置
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        second_name = _resolve_second_name(
            args.second_profile, (cfg.get("review") or {}).get("second_profile"))
        second_client = (LLMClient.from_config(args.config, profile=second_name)
                         if second_name else None)

        # STRUCT 阶段阈值：与 cmd_review 同一套读取逻辑——评估要测的
        # 就是你真实用的那套配置，阈值当然不能搞两套
        max_fn_lines, max_nest = _structural_thresholds(cfg)

        # 新特性配置解析：与 cmd_review 同一套逻辑
        symbol_backend = _resolve_symbol_backend(cfg)
        lsp_client = _resolve_lsp_config(cfg)
        distill_enabled, distill_max_chars, aging_enabled, history_weight = _resolve_distill_config(cfg)

        print(f"模式：fresh · 目标：{root}")
        print(f"产物目录：{run_dir}（并发度 {args.concurrency}）")
        print(f"初审模型：{client.config.model}"
              + (f" · 二级审查：{second_client.config.model}"
                 if second_client else "（未启用二级审查）"))
        print(f"结构检测：函数 ≤{max_fn_lines} 行 / 嵌套 ≤{max_nest} 层（零 LLM）")
        print(f"符号后端：{cfg.get('analysis', {}).get('symbol_backend', 'auto')}"
              + (f" · LSP：已启用" if lsp_client else "")
              + (f" · 蒸馏：已启用" if distill_enabled else ""))

        pipeline = Pipeline(
            root=root, run_dir=run_dir, client=client,
            concurrency=args.concurrency, bus=EventBus(run_dir),
            state=RunState(run_id), second_client=second_client,
            max_function_lines=max_fn_lines, max_nesting_depth=max_nest,
            backend=symbol_backend, lsp_client=lsp_client,
            distill=distill_enabled, distill_max_chars=distill_max_chars,
            aging_enabled=aging_enabled, history_weight=history_weight,
            mistakes_path=_resolve_mistakes_path(cfg),
            mistakes_max_inject=cfg.get("mistake_notebook", {}).get("max_inject", 5),
        )
        t0 = time.time()
        findings = pipeline.run()
        print(f"审查完成：{len(findings)} 个问题，{time.time() - t0:.0f} 秒，"
              f"{client.total_requests} 次请求，{client.total_tokens_used} tokens")

    # ---------- 比对 ----------
    result = match(findings, labels, tolerance=args.tolerance)
    m = result.metrics

    out_path = run_dir / "eval_result.json"
    out_path.write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ---- 控制台报告：先指标，后逐条明细（明细才是改 prompt 时要盯的）----
    print(f"\n========== 评估结果（tolerance={args.tolerance} 行）==========")
    print(f"召回率  recall    = {m.recall:.1%}"
          f"（{m.matched_labels}/{m.total_must_find} 个必答问题被找到）")
    print(f"精确率  precision = {m.precision:.1%}"
          f"（{m.matched_findings}/{m.total_findings} 条报告是真的）")
    print(f"噪声违反          = {m.noise_count} 条（踩中 must_not_find 诱饵）")
    if m.category_accuracy is not None:
        print(f"类别一致率（参考）= {m.category_accuracy:.1%}（仅统计命中对）")

    if result.missed:
        print(f"\n漏报（{len(result.missed)} 个该报没报的）：")
        for lb in result.missed:
            print(f"  ✗ {lb.id} {lb.file}:{lb.line_start}-{lb.line_end}"
                  f" [{lb.category}] {lb.title}")
    if result.false_positives:
        print(f"\n误报（{len(result.false_positives)} 条没对上任何必答题的）：")
        for f in result.false_positives:
            print(f"  ? {f.id} {f.file_path}:{f.line_start}-{f.line_end}"
                  f" [{f.category}] {f.title}")
    if result.noise:
        print(f"\n噪声违反（{len(result.noise)} 条踩中诱饵的）：")
        for n in result.noise:
            print(f"  ! {n.finding_id} 踩中 {n.label_id}")

    print(f"\n完整结果（含逐条匹配对）：{out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cra", description="多 agent 代码审查助手")
    sub = parser.add_subparsers(dest="command", required=True)

    p_review = sub.add_parser("review", help="审查一个项目目录")
    p_review.add_argument("path", help="目标项目路径")
    p_review.add_argument("--profile", default="local_vllm", help="用哪个模型配置")
    p_review.add_argument("--config", default="config.yaml", help="配置文件路径")
    p_review.add_argument("--concurrency", type=int, default=3,
                          help="Reviewer 并发数（本地 14B 建议 2-4）")
    p_review.add_argument("--second-profile", default=None,
                          help="二级审查（终审仲裁）用哪个模型配置，不填则不启用")
    # 断点续跑：指向一个已存在的 run 目录。
    # 初审：读 findings_raw.jsonl，跳过已审块、读回已有发现；
    # 二级审查：findings.json 里已有终局裁决（confirmed/rejected）的跳过。
    p_review.add_argument("--resume", default=None,
                          help="续跑指定的 run 目录：跳过已审块、发现读回")
    p_review.set_defaults(func=cmd_review)

    p_opt = sub.add_parser("optimize", help="基于某次审查结果优化代码（只动副本）")
    p_opt.add_argument("run_dir", help="审查产物目录，如 runs/20260719_120000")
    p_opt.add_argument("--profile", default="local_vllm",
                       help="复查/写任务书用的模型（默认本地 14B）")
    p_opt.add_argument("--config", default="config.yaml", help="配置文件路径")
    p_opt.add_argument("--backend", choices=["api", "opencode"], default=None,
                       help="修改器后端，不填则读 config.yaml 的 fixer.backend")
    p_opt.add_argument("--prompt-mode", choices=["template", "llm"], default=None,
                       help="任务书生成模式，不填则读 config.yaml 的 fixer.prompt_mode")
    p_opt.add_argument("--max-rounds", type=int, default=3,
                       help="迭代修复的轮次硬上限（默认 3 轮）")
    p_opt.set_defaults(func=cmd_optimize)

    p_ver = sub.add_parser("verify", help="对已有副本重新复查（不重新修复）")
    p_ver.add_argument("run_dir", help="优化产物目录，如 runs/batch_memory")
    p_ver.add_argument("--profile", default="local_vllm", help="复查用哪个模型配置")
    p_ver.add_argument("--config", default="config.yaml", help="配置文件路径")
    p_ver.set_defaults(func=cmd_verify)

    p_eval = sub.add_parser("eval", help="评估审查质量：和 labels.json 标准答案比对")
    # 目标路径做成可选位置参数：compare 模式（--run）下它不该出现，
    # 两者互斥的检查在 cmd_eval 里做（argparse 的互斥组管不了"位置参数 vs 可选参数"）
    p_eval.add_argument("path", nargs="?", default=None,
                        help="目标项目路径（fresh 模式：先审查再比对）")
    p_eval.add_argument("--run", default=None,
                        help="已有 run 目录（compare 模式：零 LLM 直接比对 findings.json）")
    p_eval.add_argument("--labels", required=True, help="标签文件 labels.json 的路径")
    p_eval.add_argument("--profile", default="local_vllm", help="用哪个模型配置")
    p_eval.add_argument("--config", default="config.yaml", help="配置文件路径")
    p_eval.add_argument("--concurrency", type=int, default=3,
                        help="Reviewer 并发数（本地 14B 建议 2-4）")
    p_eval.add_argument("--second-profile", default=None,
                        help="二级审查（终审仲裁）用哪个模型配置，不填则不启用")
    p_eval.add_argument("--tolerance", type=int, default=5,
                        help="行号容差：finding 区间向两边各扩几行再求交（默认 5）")
    p_eval.set_defaults(func=cmd_eval)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
