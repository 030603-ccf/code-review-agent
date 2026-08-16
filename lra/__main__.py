"""CLI entry point — `python -m lra review <project>`.

Resume semantics: the run directory and checkpoint are keyed by `thread_id`.
Re-running the same command with the same `--thread-id` resumes from the last
checkpoint instead of starting over.
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from langgraph.checkpoint.sqlite import SqliteSaver

from lra import PROJECT_ROOT
from lra.agents.second_reviewer import MISTAKE_INJECT_LIMIT, load_mistakes_text
from lra.cache import FindingCache
from lra.graph import build_graph
from lra.llm.client import LLMClient
from lra.optimizer.copier import create_workspace, hash_tree
from lra.optimizer.fixer import make_fixer
from lra.optimizer.loop import FixCache, optimize_loop
from lra.optimizer.opt_state import OptState
from lra.schemas.finding import Finding

_SECOND_OFF = frozenset({"", "none", "off"})


def _resolve_second_name(cli_value, cfg_value):
    def _norm(v):
        if v is None:
            return None
        v = str(v).strip()
        return None if v.lower() in _SECOND_OFF else v
    return _norm(cli_value) if cli_value is not None else _norm(cfg_value)


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _context_fingerprint(issue_hint: str, root: Path, run_dir: Path) -> str:
    """sha256(issue_hint + rules.json + 注入的错题本) 前 16 位。

    这些输入会影响 reviewer 输出却不体现在文件 sha1 里，拼进缓存键，保证
    换 hint / 改规则 / 改错题本后缓存整体失效，不会静默命中旧结果。

    错题本只哈希真正注入 prompt 的那最近 ``MISTAKE_INJECT_LIMIT`` 条（与
    ``nodes.scan`` 的注入逻辑一致）：更早的条目滚出注入窗口后不再让缓存整体
    失效，否则长期运行下第 21 条误报一追加就全量重烧 token。rules.json 与
    issue_hint 没有截断，保持全量哈希。
    """
    rules = _read_text_or_empty(root / ".codereview" / "rules.json")
    mistakes = load_mistakes_text(
        run_dir.parent / "memory" / "mistakes.jsonl",
        limit=MISTAKE_INJECT_LIMIT)
    raw = "\x00".join((issue_hint or "", rules, mistakes))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def cmd_review(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"错误：{root} 不是目录")
        return 1

    config_path = Path(args.config)
    client = LLMClient.from_config(config_path, profile=args.profile)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    lsp_cfg = cfg.get("lsp") or {}
    second_name = _resolve_second_name(
        args.second_profile, (cfg.get("review") or {}).get("second_profile"))
    second_client = (LLMClient.from_config(config_path, profile=second_name)
                     if second_name else None)

    # microsecond resolution — same-second runs never collide
    thread_id = args.thread_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = Path("runs") / thread_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # sha1 增量缓存：runs/ 下跨 thread 共享，未变更文件跳过 LLM
    cache = None if args.no_cache else FindingCache(run_dir.parent / ".findings_cache.json")
    config = {"configurable": {"thread_id": thread_id},
              "max_concurrency": args.concurrency}

    print("=" * 62)
    print("  lra · 代码审查")
    print("=" * 62)
    print(f"  目标目录 : {root}")
    print(f"  产物目录 : {run_dir}")
    print(f"  缓存     : {'禁用（--no-cache）' if cache is None else run_dir.parent / '.findings_cache.json'}")
    print(f"  并发度   : {args.concurrency} · thread {thread_id}")
    print(f"  初审模型 : {client.config.model}"
          + (f"\n  二级审查 : {second_client.config.model}"
             if second_client else "\n  二级审查 : （未启用）"))
    if args.issue_hint:
        print(f"  线索模式 : {args.issue_hint}")
    if lsp_cfg.get("enabled"):
        print(f"  LSP 诊断 : 已启用（servers: {', '.join((lsp_cfg.get('servers') or {}).keys()) or '无'}）")
    print("=" * 62)

    diff_files: list[str] | None = None
    if args.incremental:
        from lra.diff import changed_files
        diff_files = changed_files(root, base_ref=args.base_ref)
        if diff_files:
            print(f"增量模式：{len(diff_files)} 个变更文件（{args.base_ref}..HEAD）")
        else:
            print("增量模式：非 git 仓库或没有变更，退化为全量审查")

    t0 = time.monotonic()
    context = _context_fingerprint(args.issue_hint or "", root, run_dir)
    builder = build_graph(client, second_client, cache, context=context,
                          lsp_cfg=lsp_cfg)
    ckpt_path = run_dir / "checkpoints.sqlite"

    with SqliteSaver.from_conn_string(str(ckpt_path)) as saver:
        graph = builder.compile(checkpointer=saver)
        snap = graph.get_state(config)
        if snap.values:
            # 续跑校验：thread_id 相同但目标项目变了 = 不是同一次 run。绝不能
            # 拿 A 项目的 checkpoint 当 B 项目的续跑结果（scan 不重跑、project_map
            # 仍是 A 的，产出会张冠李戴）。检测到路径不一致直接报错，让用户换
            # 一个 --thread-id 重跑。
            prev_root = snap.values.get("root")
            if prev_root is not None and Path(prev_root).resolve() != root:
                print(f"错误：thread {thread_id} 的 checkpoint 指向 {prev_root}，"
                      f"与本次目标 {root} 不一致。请换一个 --thread-id 重新跑。")
                return 1
            if args.retry_failed and not snap.next and snap.values.get("failed_blocks"):
                print(f"检测到 {len(snap.values['failed_blocks'])} 个失败块，倒带补跑……")
                graph.update_state(config, {"retry_round": 0}, as_node="aggregate")
                result = graph.invoke(None, config)
            else:
                print(f"检测到 thread {thread_id} 的 checkpoint，"
                      + ("断点续跑……" if snap.next else "上次已跑完，直接读结果。"))
                result = graph.invoke(None, config)
        else:
            initial = {"root": str(root), "run_dir": str(run_dir),
                       "second_client_enabled": second_client is not None}
            if diff_files:
                initial["diff_files"] = diff_files
            if args.issue_hint:
                initial["issue_hint"] = args.issue_hint
            result = graph.invoke(initial, config)

    if cache is not None:
        cache.flush()  # 节流落盘兜底（report 节点已 flush，这里再保一次）

    findings = result.get("aggregated", [])
    elapsed = time.monotonic() - t0
    print("\n" + "=" * 62)
    print("  审查完成")
    print("=" * 62)
    print(f"  发现问题 : {len(findings)} 个")
    print(f"  总耗时   : {elapsed:.1f}s")
    print(f"  初审     : {client.total_requests} 次请求 · {client.total_tokens_used} tokens")
    if second_client:
        print(f"  终审     : {second_client.total_requests} 次请求 · "
              f"{second_client.total_tokens_used} tokens")
    print(f"  产物     : {run_dir}")
    print(f"  续跑     : python -m lra review {root} --thread-id {thread_id}")
    print("=" * 62)
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    """优化闭环：读审查结果 → 建副本 → 修复 → 复查 → 迭代。"""
    run_dir = Path(args.run_dir)
    findings_path = run_dir / "findings.json"
    if not findings_path.is_file():
        print(f"错误：找不到 {findings_path}，请先运行 review")
        return 1

    findings = [Finding(**d) for d in json.loads(
        findings_path.read_text(encoding="utf-8"))]
    if not findings:
        print("审查结果里没有发现任何问题，无需修复。")
        return 0

    # 目标项目路径：优先 --path，否则从 project_map.json 读
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

    config_path = Path(args.config)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    fixer_cfg = cfg.get("fixer") or {}
    backend = args.backend or fixer_cfg.get("backend", "api")
    build_check_cfg = cfg.get("build_check") or {}
    build_cmd = args.build_cmd or build_check_cfg.get("commands", {}).get("py", "ruff check")

    review_client = LLMClient.from_config(config_path, profile=args.profile)

    print(f"目标项目   : {target_root}")
    print(f"审查结果   : {len(findings)} 条（{findings_path}）")
    print(f"修复后端   : {backend} · 复查模式 {args.verify} · 最多 {args.max_rounds} 轮")
    print(f"复查模型   : {review_client.config.model}")
    if args.issue_hint:
        print(f"线索模式   : {args.issue_hint}")
    print()

    # 断点续跑：opt_state.json + optimized_copy/ 都在 = 上次已经跑过一轮，
    # 复用副本与状态，不重建、不重装 findings，只从上次没 verified 的地方续修。
    opt_state_path = run_dir / "opt_state.json"
    copy_root = run_dir / "optimized_copy"
    if opt_state_path.is_file() and copy_root.is_dir():
        state = OptState.load(opt_state_path)
        print("检测到上次修复状态，断点续跑")
    else:
        copy_root = create_workspace(target_root, run_dir)
        print(f"副本已建立 : {copy_root}")
        state = OptState(str(target_root), str(copy_root))
        state.register_findings(findings)
        state.save(opt_state_path)

    # 修复缓存落盘：断点续跑时命中上次已修好的文件，跳过 fixer 调用。
    cache = FixCache(run_dir / "fix_cache.json")

    opencode_cfg = fixer_cfg.get("opencode", {})
    fixer = make_fixer(
        backend=backend,
        copy_root=copy_root,
        state=state,
        client=review_client if backend == "api" else None,
        cmd=opencode_cfg.get("cmd", "opencode run"),
        timeout=opencode_cfg.get("timeout", 600),
        model=review_client.config.model,
    )

    print(f"\n开始迭代修复……\n")
    t0 = time.monotonic()
    result = optimize_loop(
        run_dir=run_dir, copy_root=copy_root, findings=findings, state=state,
        fixer=fixer, review_client=review_client, max_rounds=args.max_rounds,
        verify_mode=args.verify, build_cmd=build_cmd,
        issue_hint=args.issue_hint or "", cache=cache, log=print,
    )
    elapsed = time.monotonic() - t0

    print(f"\n{'=' * 62}")
    print(f"  优化闭环完成 · {elapsed:.1f}s · 共 {result.get('rounds', 0)} 轮")
    print(f"  ✅ 修好：{len(result.get('verified', []))}")
    print(f"  ❌ 仍在：{len(result.get('remaining', []))}")
    print(f"  ⛔ 改砸：{len(result.get('failed', []))}")
    if result.get("stuck"):
        print("  ⚠️ 停滞：修改器卡住了（两轮结果相同）")
    print(f"  产物：{run_dir}/opt_state.json · verification.md · optimized_copy/")
    print("=" * 62)
    return 0


def main() -> int:
    # Force UTF-8 on stdout/stderr so Chinese output isn't mangled on a
    # GBK Windows console (cosmetic, but keeps logs copy-pasteable).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="lra", description="代码审查智能体（LangGraph 编排版）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("review", help="审查一个项目目录")
    p.add_argument("path", help="目标项目路径")
    p.add_argument("--profile", default=None, help="初审模型 profile（默认用 config 的 default_profile）")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"),
                   help="配置文件路径")
    p.add_argument("--concurrency", type=int, default=16, help="并行审查块数上限")
    p.add_argument("--second-profile", default=None,
                   help="终审模型 profile；显式传 none/off 为禁用")
    p.add_argument("--thread-id", default=None, help="本次运行户头名（同名=断点续跑）")
    p.add_argument("--issue-hint", default=None, help="引导 LLM 重点核查的线索")
    p.add_argument("--incremental", action="store_true", help="只审 git diff 变更文件")
    p.add_argument("--base-ref", default="HEAD~1", help="增量模式 diff 基线")
    p.add_argument("--retry-failed", action="store_true", help="倒带补跑失败块")
    p.add_argument("--no-cache", action="store_true",
                   help="禁用 sha1 增量缓存（每次全量调 LLM）")
    p.set_defaults(func=cmd_review)

    o = sub.add_parser("optimize", help="基于审查结果运行修复闭环")
    o.add_argument("run_dir", help="review 产物目录（含 findings.json）")
    o.add_argument("--path", default=None, help="目标项目路径（默认从 project_map.json 读）")
    o.add_argument("--profile", default=None, help="复查/修复用模型 profile")
    o.add_argument("--backend", default=None, help="修复后端：api / opencode（默认读 config）")
    o.add_argument("--max-rounds", type=int, default=3, help="最大迭代轮数")
    o.add_argument("--verify", default="llm", choices=["llm", "build"],
                   help="复查模式：llm 重审 / build 确定性闸门")
    o.add_argument("--build-cmd", default=None, help="build 模式的命令（默认 ruff check）")
    o.add_argument("--issue-hint", default=None, help="引导修复环节重点围绕的 issue 线索")
    o.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"),
                   help="配置文件路径")
    o.set_defaults(func=cmd_optimize)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
