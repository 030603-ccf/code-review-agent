"""loop.py —— 迭代修复闭环：没修好就再修一轮，直到干净或认输。

两个刹车（缺一不可）：
    1. max_rounds   轮次硬上限，防止无限烧 token
    2. 停滞检测      两轮下来 remaining 集合一模一样 = 修改器卡住了，停下来
"""

from pathlib import Path

from cra.optimizer.build_check import BuildCheckResult, run_build_check
from cra.optimizer.prompt_builder import build_tasks
from cra.optimizer.verifier import verify_fixes


def optimize_loop(run_dir, copy_root, findings, state, fixer, review_client,
                  max_rounds: int = 3, prompt_mode: str = "template",
                  bus=None, build_check_cfg: dict | None = None) -> dict:
    """迭代主循环。返回最后一轮的复查汇总 + 轮次历史。

    参数分工：
        fixer          修改器（重活，api / opencode 后端）
        review_client  复查 + （llm 模式下）写任务书的模型（轻活）
    """
    run_dir = Path(run_dir)
    history: list[dict] = []
    prev_remaining: set | None = None
    final: dict = {}

    to_fix = list(findings)
    feedback = None

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
            if bus:
                bus.emit("fix", "Fixer",
                         f"[第{round_no}轮 {i}/{len(tasks)}] {task.file_path}"
                         f"{'写回副本' if ok else '失败'}")
            else:
                status = "✅ 写回副本" if ok else "❌ 失败"
                print(f"  [第{round_no}轮 {i}/{len(tasks)}] {task.file_path} {status}")

        # ---------- 本轮：复查 ----------
        # 收集本轮修改器实际动过的文件（哈希哨兵的分流依据）
        round_files = {task.file_path for task in tasks}
        print(f"  [第{round_no}轮] 开始复查（本轮 {len(round_files)} 个文件）……")
        summary = verify_fixes(run_dir, copy_root, review_client, state, bus,
                               round_files=round_files if round_no > 1 else None)

        # ---------- 本轮：构建验证 ----------
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
            if not build_result.passed:
                print(f"  [第{round_no}轮] ⚠️ 构建验证未通过：{build_result.command}")

        history.append({"round": round_no,
                        **{k: len(v) for k, v in summary.items()
                           if isinstance(v, list)}})
        final = summary

        # ---------- 判停 ----------
        remaining = set(state.findings_by_status("remaining"))
        print(f"  [第{round_no}轮] 结果：✅ {len(summary['verified'])} 修好 · "
              f"❌ {len(remaining)} 仍在 · ⛔ {len(summary['failed'])} 改砸")
        if not remaining:
            print("  全部修好，收工！")
            break
        if prev_remaining is not None and remaining == prev_remaining:
            final["stuck"] = True
            print("  停滞检测触发：两轮 remaining 相同，修改器卡住了，停下。")
            break
        prev_remaining = remaining

        # ---------- 准备下一轮 ----------
        to_fix = [f for f in findings if f.id in remaining]
        feedback = {fid: state.data["findings"][fid].get("note", "")
                    for fid in remaining}

    final["rounds"] = len(history)
    final["history"] = history
    return final
