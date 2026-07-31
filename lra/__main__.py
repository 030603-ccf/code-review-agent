"""lra 的 CLI 入口。

用法：
    python -m lra review <项目路径> [--profile local_vllm]
                                    [--second-profile none]
                                    [--concurrency 3]
                                    [--thread-id xxx]
                                    [--issue-hint "检查是否有 SQL 注入"]

与原版 `python -m cra review` 的参数对照：
    --profile / --second-profile / --config / --concurrency  语义完全一致
    --thread-id   取代了原版的 --resume：
                  原版续跑要传"上次的 run 目录"；
                  这里 run 目录直接用 thread_id 命名（runs/<thread_id>），
                  checkpoint 也记在同一个户头下——
                  **续跑 = 原样再敲一遍同一条命令**。

并发度的去处（对应原版的 Semaphore）：
    config 里的 max_concurrency 是框架级的"并行节点上限"——
    同时最多放行这么多个 review_chunk。原版那把
    asyncio.Semaphore(concurrency) 的闸门，换成了框架自带的闸门。

with 语句管理 checkpointer：
    SqliteSaver.from_conn_string() 返回一个**上下文管理器**——
    `with ... as saver:` 进块时打开 SQLite 连接，出块时自动关闭。
    编译（compile）和 invoke 都必须发生在 with 块内部，
    出了块连接就关了，机器没法再读写账本。
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import yaml
from langgraph.checkpoint.sqlite import SqliteSaver

from lra import PROJECT_ROOT
from lra.graph import build_graph

# LLMClient.from_config：读 yaml 建客户端
from cra.llm.client import LLMClient
from cra.schemas.finding import Finding

# 二级审查的“关闭词表”：CLI 和 Web UI 共用同一套词（大小写、首尾空格不敏感）。
_SECOND_OFF_VALUES = frozenset({"", "none", "off"})


def _resolve_second_name(cli_value: str | None, cfg_value: str | None) -> str | None:
    """决定二级审查用哪个 profile。返回 None = 不启用。

    三态语义：
      1. CLI 没传（None）      -> 回退 config.yaml 的 review.second_profile
      2. CLI 显式传 ""/none/off -> 明确禁用
      3. 其他值                 -> 用 CLI 指定的 profile
    """
    def _norm(v: str | None) -> str | None:
        if v is None:
            return None
        v = str(v).strip()
        return None if v.lower() in _SECOND_OFF_VALUES else v

    return _norm(cli_value) if cli_value is not None else _norm(cfg_value)


def cmd_review(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"错误：{root} 不是目录")
        return 1

    config_path = Path(args.config)

    # ---- 建两个模型客户端（终审可缺席）----
    client = LLMClient.from_config(config_path, profile=args.profile)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    second_name = _resolve_second_name(
        args.second_profile, (cfg.get("review") or {}).get("second_profile"))
    second_client = (LLMClient.from_config(config_path, profile=second_name)
                     if second_name else None)

    # ---- thread_id：这次运行的"户头名" ----
    # 不传就用时间戳；run 目录与 checkpoint 户头同名，好找
    thread_id = args.thread_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path("runs") / thread_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # RunnableConfig：框架认识的配置字典。
    #   configurable.thread_id  户头名（checkpoint 记在它名下）
    #   max_concurrency         并行节点上限（原版 Semaphore 的对应物）
    config = {"configurable": {"thread_id": thread_id},
              "max_concurrency": args.concurrency}

    print(f"目标：{root}")
    print(f"产物目录：{run_dir}（并发度 {args.concurrency} · thread {thread_id}）")
    print(f"初审模型：{client.config.model}"
          + (f" · 二级审查：{second_client.config.model}"
             if second_client else "（未启用二级审查）"))
    if args.issue_hint:
        # 线索模式：把用户的问题线索注入每块的审查提示词（见 nodes.py）
        print(f"线索模式已启用：{args.issue_hint}")

    # ---- 增量模式：先算好"只审哪些文件" ----
    diff_files: list[str] | None = None
    if args.incremental:
        from lra.diff import changed_files
        diff_files = changed_files(root, base_ref=args.base_ref)
        if diff_files:
            print(f"增量模式：{len(diff_files)} 个变更文件（{args.base_ref}..HEAD）")
        else:
            print("增量模式：非 git 仓库或没有变更，退化为全量审查")

    t0 = time.monotonic()          # 总耗时的起点（不受系统时间调整影响）
    builder = build_graph(client, second_client)
    ckpt_path = run_dir / "checkpoints.sqlite"   # 框架的账本，与产物同目录

    with SqliteSaver.from_conn_string(str(ckpt_path)) as saver:
        graph = builder.compile(checkpointer=saver)

        # get_state：翻账本——这个户头下有没有记过账？
        snap = graph.get_state(config)
        if snap.values:
            # 有账：invoke(None) 表示"不给新输入，从上次 checkpoint 接着跑"。
            #   snap.next 非空 = 上次中途被杀 -> 断点续跑
            #   snap.next 为空 = 上次已跑完   -> 直接交还旧结果，零新增请求
            print(f"检测到 thread {thread_id} 的 checkpoint，"
                  + ("断点续跑……" if snap.next
                     else "上次已完整跑完，直接读结果。"))
            result = graph.invoke(None, config)
        else:
            # 没账：全新开工，把启动材料（root/run_dir）放上工作台
            initial = {"root": str(root), "run_dir": str(run_dir)}
            if diff_files:      # 增量模式：把变更清单也放上工作台
                initial["diff_files"] = diff_files
            if args.issue_hint: # 线索模式：把用户的问题线索也放上工作台
                initial["issue_hint"] = args.issue_hint
            result = graph.invoke(initial, config)

    findings = result.get("aggregated", [])
    total_elapsed = time.monotonic() - t0
    print(f"\n完成：{len(findings)} 个问题 · 总耗时 {total_elapsed:.1f}s")
    print(f"初审：{client.total_requests} 次请求 · {client.total_tokens_used} tokens"
          + (f"\n终审：{second_client.total_requests} 次请求 · "
             f"{second_client.total_tokens_used} tokens"
             if second_client else ""))
    print(f"产物：{run_dir} 下的 project_map.json / findings.json / "
          f"report.md / checkpoints.sqlite")
    print(f"续跑：python -m lra review {root} --thread-id {thread_id}")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    """优化闭环：读审查结果 -> 建副本 -> 生成任务书 -> 修复 -> 复查 -> 迭代。"""
    from cra.optimizer.copier import create_workspace, hash_tree
    from cra.optimizer.fixer import make_fixer
    from cra.optimizer.loop import optimize_loop
    from cra.optimizer.opt_state import OptState

    # ---- 定位 run_dir 和 findings.json ----
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"错误：run 目录不存在：{run_dir}")
        return 1
    findings_path = run_dir / "findings.json"
    if not findings_path.is_file():
        print(f"错误：找不到审查结果 {findings_path}，请先运行 review")
        return 1

    # ---- 装载 findings ----
    findings = [Finding(**d) for d in json.loads(
        findings_path.read_text(encoding="utf-8"))]
    if not findings:
        print("审查结果里没有发现任何问题，无需修复。")
        return 0

    # ---- 确定目标项目路径 ----
    # 优先用 CLI 参数，其次从 project_map.json 里读
    if args.path:
        target_root = Path(args.path).resolve()
    else:
        pm_path = run_dir / "project_map.json"
        if pm_path.is_file():
            pm = json.loads(pm_path.read_text(encoding="utf-8"))
            target_root = Path(pm.get("root", "")).resolve()
        else:
            print("错误：请通过 --path 指定目标项目路径")
            return 1
    if not target_root.is_dir():
        print(f"错误：目标项目路径不存在：{target_root}")
        return 1

    # ---- 读配置 ----
    config_path = Path(args.config)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    fixer_cfg = cfg.get("fixer") or {}
    backend = args.backend or fixer_cfg.get("backend", "opencode")
    prompt_mode = fixer_cfg.get("prompt_mode", "template")
    build_check_cfg = cfg.get("build_check")

    # ---- 建模型客户端（复查用）----
    review_profile = args.profile or "local_vllm"
    review_client = LLMClient.from_config(config_path, profile=review_profile)

    # ---- api 后端时额外建修改器客户端 ----
    fixer_client = None
    if backend == "api":
        api_profile = fixer_cfg.get("api", {}).get("profile", "cloud_api_deepseek-v4-pro")
        fixer_client = LLMClient.from_config(config_path, profile=api_profile)

    # ---- 建副本 ----
    print(f"目标项目：{target_root}")
    print(f"审查结果：{len(findings)} 条发现（{findings_path}）")
    print(f"修复后端：{backend} · 任务书模式：{prompt_mode}")
    print(f"复查模型：{review_client.config.model}")
    print()

    copy_root = create_workspace(target_root, run_dir)
    print(f"副本已建立：{copy_root}")

    # ---- 初始化 OptState ----
    state = OptState(str(target_root), str(copy_root))
    state.register_findings(findings)
    state.data["fixer"] = {"backend": backend}
    # 记录修复前哈希
    before_hashes = hash_tree(copy_root)
    state.record_hashes("hash_before", before_hashes)
    state.save(run_dir / "opt_state.json")

    # ---- 造修改器 ----
    opencode_cfg = fixer_cfg.get("opencode", {})
    fixer = make_fixer(
        backend=backend,
        copy_root=copy_root,
        state=state,
        client=fixer_client,
        cmd=opencode_cfg.get("cmd", "opencode run"),
        timeout=opencode_cfg.get("timeout", 600),
    )

    # ---- 跑迭代闭环 ----
    print(f"\n开始迭代修复（最多 {args.max_rounds} 轮）……\n")
    t0 = time.monotonic()
    result = optimize_loop(
        run_dir=run_dir,
        copy_root=copy_root,
        findings=findings,
        state=state,
        fixer=fixer,
        review_client=review_client,
        max_rounds=args.max_rounds,
        prompt_mode=prompt_mode,
        build_check_cfg=build_check_cfg,
    )
    elapsed = time.monotonic() - t0

    # ---- 战报 ----
    print(f"\n{'='*60}")
    print(f"优化闭环完成 · 耗时 {elapsed:.1f}s · 共 {result.get('rounds', 0)} 轮")
    print(f"  ✅ 修好：{len(result.get('verified', []))}")
    print(f"  ❌ 仍在：{len(result.get('remaining', []))}")
    print(f"  ⛔ 改砸：{len(result.get('failed', []))}")
    print(f"  🆕 新问题：{len(result.get('new_findings', []))}")
    if result.get("stuck"):
        print(f"  ⚠️ 停滞：修改器卡住了（两轮结果相同）")
    print(f"\n产物目录：{run_dir}")
    print(f"  - opt_state.json    每条漏洞的修复命运")
    print(f"  - verification.md   复查报告")
    print(f"  - prompts/          每轮任务书")
    print(f"  - optimized_copy/   修复后的代码副本")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lra", description="多 agent 代码审查助手（LangGraph 编排版）")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- review 子命令 ----
    p_review = sub.add_parser("review", help="审查一个项目目录")
    p_review.add_argument("path", help="目标项目路径")
    p_review.add_argument("--profile", default="local_vllm",
                          help="初审用哪个模型配置")
    p_review.add_argument("--config",
                          default=str(PROJECT_ROOT / "config.yaml"),
                          help="配置文件路径（默认用项目根目录的 config.yaml）")
    p_review.add_argument("--concurrency", type=int, default=16,
                          help="Reviewer 并行数上限（传给框架的 max_concurrency）")
    p_review.add_argument("--second-profile", default=None,
                          help="终审用哪个模型配置；显式传 none/off 为禁用")
    p_review.add_argument("--thread-id", default=None,
                          help="本次运行的户头名；同 thread-id 再跑 = 断点续跑")
    p_review.add_argument("--issue-hint", default=None,
                          help="问题线索：让 LLM 重点核查的内容"
                               "（如'检查是否有 SQL 注入'）")
    p_review.add_argument("--incremental", action="store_true",
                          help="增量模式：只审查 git diff 变更的文件")
    p_review.add_argument("--base-ref", default="HEAD~1",
                          help="增量模式的 diff 基线（默认 HEAD~1）")
    p_review.set_defaults(func=cmd_review)

    # ---- optimize 子命令 ----
    p_opt = sub.add_parser("optimize", help="基于审查结果运行修复闭环")
    p_opt.add_argument("run_dir", help="review 产物目录（含 findings.json）")
    p_opt.add_argument("--path", default=None,
                       help="目标项目路径（默认从 project_map.json 读取）")
    p_opt.add_argument("--profile", default=None,
                       help="复查用哪个模型配置（默认 local_vllm）")
    p_opt.add_argument("--backend", default=None,
                       help="修复后端：api / opencode（默认读 config.yaml）")
    p_opt.add_argument("--max-rounds", type=int, default=3,
                       help="最大迭代轮数（默认 3）")
    p_opt.add_argument("--config",
                       default=str(PROJECT_ROOT / "config.yaml"),
                       help="配置文件路径")
    p_opt.set_defaults(func=cmd_optimize)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
