"""loop.py —— 迭代修复闭环：没修好就再修一轮，直到干净或认输。

为什么需要迭代：
LLM 修复是不确定的——同一个模型同一个任务，这轮可能用教科书修法
（ast.literal_eval），下轮用偷懒的伪修复（受限 eval，你亲眼见过）。
一轮定生死不可靠；把复查理由回喂给修改器，它就知道上一轮错在哪。

这就是 agent 开发里的"反馈回路"（feedback loop）：
chat_structured 的错误回喂是句子级的（JSON 不合法就重问），
这里的回喂是任务级的（修复不合格就带理由返工）。

两个刹车（缺一不可）：
    1. max_rounds   轮次硬上限，防止无限烧 token
    2. 停滞检测      两轮下来 remaining 集合一模一样 = 修改器卡住了，
                     再转也是原地踏步，停下来交给人

为什么不重试 failed：failed 意味着删文件/语法错误这种结构性事故，
盲目重试多半再撞一次——保守起见只迭代 remaining，failed 留给人。
"""

from pathlib import Path

from cra.analysis.context_distill import DistillCache
from cra.optimizer.build_check import BuildCheckResult, run_build_check
from cra.optimizer.prompt_builder import build_tasks
from cra.optimizer.verifier import verify_fixes


def optimize_loop(run_dir, copy_root, findings, state, fixer, review_client,
                  max_rounds: int = 3, prompt_mode: str = "template",
                  bus=None, build_check_cfg: dict | None = None) -> dict:
    """迭代主循环。返回最后一轮的复查汇总 + 轮次历史。

    参数分工：
        fixer          修改器（重活，api / opencode 后端）
        review_client  复查 + （llm 模式下）写任务书的模型（轻活，14B 够格）
    """
    run_dir = Path(run_dir)
    history: list[dict] = []      # 每轮的计数汇总，给人看“收敛过程”
    prev_remaining: set | None = None
    final: dict = {}
    
    # 蒸馏缓存：多轮修复中复用，文件内容变化后自动失效（哈希不同）。
    # 当前仅创建实例，后续集成蒸馏时可直接使用。
    distill_cache = DistillCache()
    
    to_fix = list(findings)       # 第一轮全修；之后只修 remaining
    feedback = None               # 第一轮没有“上一轮”

    for round_no in range(1, max_rounds + 1):
        # 防回归清单：同文件里已 verified 的问题，提醒修改器别破坏
        keep: dict[str, list[str]] = {}
        for f in findings:
            rec = state.data["findings"].get(f.id, {})
            if rec.get("status") == "verified":
                keep.setdefault(f.file_path, []).append(f"{f.id} {f.title}")

        # ---------- 本轮：任务书 -> 修改 ----------
        tasks = build_tasks(
            to_fix, copy_root, run_dir, state=state,
            client=review_client if prompt_mode == "llm" else None,
            mode=prompt_mode, feedback=feedback,
            keep=keep or None, subdir=f"round{round_no}",
        )
        for i, task in enumerate(tasks, 1):
            ok = fixer.apply(task)
            if ok:
                # 文件被修改后蒸馏缓存失效（哈希已变，下次蒸馏会重新计算）
                distill_cache.invalidate(task.file_path)
            if bus:
                bus.emit("fix", "Fixer",
                         f"[第{round_no}轮 {i}/{len(tasks)}] {task.file_path}"
                         f"{'写回副本' if ok else '失败'}")

        # ---------- 本轮：复查 ----------
        summary = verify_fixes(run_dir, copy_root, review_client, state, bus)

        # ---------- 本轮：构建验证（零 token，确定性） ----------
        # 复查员判“问题还在不在”，构建验证判“能不能编译”。
        # ruff check / tsc / dotnet build 报的错是 100% 确定的事实——
        # 修改器改出了 import 错误 / 类型不匹配 / lint 违反，这里一拿一个准。
        build_result: BuildCheckResult | None = None
        if build_check_cfg and build_check_cfg.get("enabled", False):
            build_result = run_build_check(
                copy_root,
                commands=build_check_cfg.get("commands"),
                timeout=build_check_cfg.get("timeout", 30),
            )
            summary["build_check"] = {
                "passed": build_result.passed,
                "command": build_result.command,
                "output": build_result.output,
                "skipped": build_result.skipped,
            }
            if not build_result.passed and bus:
                bus.emit("build_check", "BuildCheck",
                         f"[第{round_no}轮] 构建验证未通过：{build_result.command}")

        history.append({"round": round_no,
                        **{k: len(v) for k, v in summary.items()
                           if isinstance(v, list)}})
        final = summary

        # ---------- 判停 ----------
        remaining = set(state.findings_by_status("remaining"))
        if not remaining:
            break                                   # 全修好了，收工
        if prev_remaining is not None and remaining == prev_remaining:
            final["stuck"] = True                   # 原地踏步，刹车
            break
        prev_remaining = remaining

        # ---------- 准备下一轮：错题 + 错题本 ----------
        to_fix = [f for f in findings if f.id in remaining]
        feedback = {fid: state.data["findings"][fid].get("note", "")
                    for fid in remaining}

    final["rounds"] = len(history)
    final["history"] = history
    return final
