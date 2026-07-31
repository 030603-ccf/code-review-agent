"""verifier.py —— 复查员 v2：定向复查。

拿着每条旧漏洞的原始记录，逐条问模型"这个问题在新代码里还存在吗"。
材料三级优先：diff 对质 > 全文 > 窗口。
"""

import difflib
import json
from pathlib import Path

from cra.llm.prompts import load_prompt, profile_of
from cra.llm.structured import chat_structured
from cra.optimizer.copier import diff_hashes, hash_tree
from cra.schemas.finding import Finding
from cra.schemas.verdict import FileCheckResult

VERIFY_FULL_FILE_LINES = 400
DIFF_MAX_LINES = 250
WINDOW_LINES = 20


def _diff_text(old: str, new: str, relpath: str) -> str:
    """生成 unified diff 文本；没变化或 diff 太大时返回空串。"""
    if old == new:
        return ""
    text = "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"修复前 {relpath}",
        tofile=f"修复后 {relpath}",
        n=3,
    ))
    return text if len(text.splitlines()) <= DIFF_MAX_LINES else ""


def _windowed(lines: list[str], originals: list[Finding]) -> str:
    """给每条旧漏洞截取前后 WINDOW_LINES 行的上下文片段。"""
    n = len(lines)
    spans = sorted(
        (max(1, f.line_start - WINDOW_LINES), min(n, f.line_end + WINDOW_LINES))
        for f in originals
    )
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    windows = []
    for lo, hi in merged:
        ids = [f.id for f in originals
               if f.line_start <= hi and f.line_end >= lo]
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))
        windows.append(f"### {', '.join(ids)} 的上下文（第 {lo}-{hi} 行）\n{body}")
    return "\n\n".join(windows)


def _check_file(client, relpath: str, new_content: str,
                originals: list[Finding], diff_text: str = "") -> FileCheckResult:
    """对质一个文件：把它的全部旧漏洞和改动材料一起发给模型。"""
    system = load_prompt("verifier", profile_of(client))
    brief = [
        {
            "finding_id": f.id,
            "severity": f.severity,
            "category": f.category,
            "lines": f"{f.line_start}-{f.line_end}",
            "title": f.title,
            "description": f.description,
            "evidence": f.evidence,
        }
        for f in originals
    ]
    lines = new_content.splitlines()
    lang = relpath.rsplit(".", 1)[-1] if "." in relpath else ""
    if diff_text:
        code_part = (
            f"【修复前后的改动（unified diff，- 开头是修复前，+ 开头是修复后）】\n"
            f"```diff\n{diff_text}```\n\n"
            f"注意：diff 之外的代码没有变化。判定依据是问题模式的旧形态（- 行）\n"
            f"是否已被新形态（+ 行）消除，而不是相关代码是否仍然存在。"
        )
    elif len(lines) <= VERIFY_FULL_FILE_LINES:
        code_part = f"【修复后的文件全文】\n```{lang}\n{new_content}\n```"
    else:
        code_part = (
            f"【修复后的相关代码片段】（文件共 {len(lines)} 行，"
            f"只取每条漏洞前后 {WINDOW_LINES} 行）\n"
            f"{_windowed(lines, originals)}\n\n"
            f"注意：如果你认为判定所需的关键代码在片段之外，"
            f"按 still_exists = true 处理（保守），并在 reason 里说明。"
        )
    user = (
        f"文件：{relpath}\n\n"
        f"【原始漏洞清单】\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        f"{code_part}"
    )
    return chat_structured(
        client,
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        FileCheckResult,
        temperature=0.1,
        max_tokens=1024,
    )


def verify_fixes(run_dir, copy_root, client, state, bus=None,
                round_files: set[str] | None = None) -> dict:
    """复查主入口。返回汇总 dict，写 verification.md，更新 opt_state。

    round_files：本轮修改器实际动过的文件集合。
        - None（第1轮或不传）= 全量复查所有变化文件
        - 有值 = 只对本轮文件做 LLM 对质；其余用哈希哨兵看守
    """
    run_dir = Path(run_dir)
    copy_root = Path(copy_root)

    # 第 1 步：哈希对比
    # 先读上一轮留下的 hash_after（哈希哨兵的基线）
    prev_hash_after: dict[str, str] = {}
    for rel, rec in state.data.get("files", {}).items():
        if "hash_after" in rec:
            prev_hash_after[rel] = rec["hash_after"]

    after = hash_tree(copy_root)
    state.record_hashes("hash_after", after)
    before = {rel: rec["hash_before"]
              for rel, rec in state.data["files"].items()
              if "hash_before" in rec}
    diff = diff_hashes(before, after)

    # 第 2 步：装载原始漏洞清单
    original = [Finding(**d) for d in json.loads(
        (run_dir / "findings.json").read_text(encoding="utf-8"))]
    orig_by_file: dict[str, list[Finding]] = {}
    for f in original:
        orig_by_file.setdefault(f.file_path, []).append(f)

    summary: dict = {
        "changed": diff["changed"],
        "added": diff["added"],
        "deleted": diff["deleted"],
        "verified": [], "remaining": [], "failed": [],
        "new_findings": [],
        "polluted": [],
    }
    report: list[str] = [
        "# 修复验证报告（定向复查）",
        "",
        f"- 增量复查：只重读了 {len(diff['changed'])} 个内容变化的文件，"
        f"未动的 {len(before) - len(diff['changed'])} 个文件零消耗",
        f"- 高危信号：被删除文件 {len(diff['deleted'])} 个，"
        f"新增文件 {len(diff['added'])} 个",
        "",
    ]

    # 原项目完整性看门狗
    target_root = state.data.get("target_root")
    if target_root and Path(target_root).exists():
        target_now = hash_tree(target_root)
        for rel, h_before in before.items():
            h_now = target_now.get(rel)
            if h_now is None:
                summary["polluted"].append(f"{rel}（原文件被删除）")
            elif h_now != h_before:
                summary["polluted"].append(rel)
        if summary["polluted"]:
            report.append("## 🚨 原项目完整性警报：修复期间以下文件被改动！")
            report.append("")
            report += [f"- {rel}" for rel in summary["polluted"]]
            report.append("")

    # 第 3 步：文件被删
    for rel in diff["deleted"]:
        report.append(f"## ⛔ {rel}：文件被修改器删除！")
        for f in orig_by_file.get(rel, []):
            state.set_finding_status(f.id, "failed", "文件被修改器删除")
            summary["failed"].append(f.id)
            report.append(f"- {f.id} [{f.severity}] {f.title}：改砸了")
        report.append("")

    if diff["added"]:
        report.append("## ⚠️ 修改器新建了这些文件（请人工确认是否合理）")
        report += [f"- {rel}" for rel in diff["added"]]
        report.append("")

    # 第 4 步：分流——本轮文件 LLM 对质 vs 非本轮文件哈希哨兵
    llm_check_files: list[str] = []   # 需要花 token 对质的
    sentinel_skipped: list[str] = []  # 哈希哨兵放行（零 token）的
    sentinel_alert: list[str] = []    # 哈希哨兵报警、升级为 LLM 的

    for rel in diff["changed"]:
        if round_files is None or rel in round_files:
            # 本轮修改器动过的 → 必须 LLM 对质
            llm_check_files.append(rel)
        else:
            # 非本轮文件 → 哈希哨兵：和上一轮 hash_after 比
            prev_h = prev_hash_after.get(rel)
            cur_h = after.get(rel)
            if prev_h is not None and cur_h != prev_h:
                # 没人动它但哈希变了 → 异常！升级为 LLM 复查
                sentinel_alert.append(rel)
                llm_check_files.append(rel)
            else:
                # 哈希没变 → 信任上一轮判定，零消耗跳过
                sentinel_skipped.append(rel)

    if sentinel_skipped:
        report.append(f"## 🔒 哈希哨兵放行（{len(sentinel_skipped)} 个文件，零 token）")
        report.append("")
        report += [f"- {rel}（哈希未变，信任上轮判定）" for rel in sentinel_skipped]
        report.append("")
    if sentinel_alert:
        report.append(f"## 🚨 哈希哨兵报警（{len(sentinel_alert)} 个文件被意外修改！）")
        report.append("")
        report += [f"- ⚠️ {rel}：非本轮修改目标但哈希变化，升级为 LLM 复查"
                   for rel in sentinel_alert]
        report.append("")

    for rel in llm_check_files:
        originals = orig_by_file.get(rel, [])
        new_content = (copy_root / rel).read_text(encoding="utf-8", errors="replace")

        old_content = None
        if target_root:
            old_file = Path(target_root) / rel
            if old_file.is_file():
                old_content = old_file.read_text(encoding="utf-8", errors="replace")
        diff_text = _diff_text(old_content, new_content, rel) \
            if old_content is not None else ""

        # Python 语法检查
        if rel.endswith(".py"):
            try:
                compile(new_content, rel, "exec")
            except SyntaxError as e:
                for f in originals:
                    state.set_finding_status(f.id, "failed", f"修复后语法错误：{e}")
                    summary["failed"].append(f.id)
                report.append(f"## ⛔ {rel}：修复后文件无法解析（{e}），改砸了")
                continue

        if not originals:
            report.append(f"## ❓ {rel}：内容有变化但没有原始漏洞记录，请人工 diff")
            continue

        try:
            result = _check_file(client, rel, new_content, originals,
                                 diff_text=diff_text)
        except Exception as e:
            for f in originals:
                state.set_finding_status(f.id, "remaining",
                                         f"复查异常，按未修好处理（保守）：{type(e).__name__}: {e}")
                summary["remaining"].append(f.id)
            report.append(f"## ❓ {rel}：复查异常（{type(e).__name__}），保守按没修好处理")
            continue

        verdict_by_id = {v.finding_id: v for v in result.verdicts}
        if rel in sentinel_alert:
            mode_tag = "（🚨 哨兵报警，升级复查）"
        elif diff_text:
            mode_tag = "（diff 对质）"
        elif len(new_content.splitlines()) > VERIFY_FULL_FILE_LINES:
            mode_tag = "（大文件，窗口对质）"
        else:
            mode_tag = ""
        report.append(f"## {rel}{mode_tag}")
        for old in originals:
            v = verdict_by_id.get(old.id)
            if v is None:
                state.set_finding_status(old.id, "remaining",
                                         "复查输出缺少该条判定，按未修好处理（保守）")
                summary["remaining"].append(old.id)
                report.append(f"- {old.id} [{old.severity}] {old.title}："
                              f"❓ 复查未判定，按没修好处理")
            elif v.still_exists:
                state.set_finding_status(old.id, "remaining", v.reason)
                summary["remaining"].append(old.id)
                report.append(f"- {old.id} [{old.severity}] {old.title}："
                              f"❌ 仍存在（{v.reason}）")
            else:
                state.set_finding_status(old.id, "verified", v.reason)
                summary["verified"].append(old.id)
                report.append(f"- {old.id} [{old.severity}] {old.title}："
                              f"✅ 已修好（{v.reason}）")

        for issue in result.new_issues:
            summary["new_findings"].append(issue.model_dump())
            report.append(f"- 🆕 修复引入的新问题 [{issue.severity}] {issue.title}"
                          f"（第 {issue.line_start}-{issue.line_end} 行）："
                          f"{issue.description}")
        report.append("")

    # 第 5 步：声称修了但文件没动的
    untouched = set(orig_by_file) - set(diff["changed"]) - set(diff["deleted"])
    for rel in sorted(untouched):
        for f in orig_by_file[rel]:
            rec = state.data["findings"].get(f.id, {})
            if rec.get("status") == "fixed":
                state.set_finding_status(f.id, "remaining",
                                         "修改器声称修复，但文件哈希未变化")
                summary["remaining"].append(f.id)
                report.append(f"## 🤥 {rel}")
                report.append(f"- {f.id} [{f.severity}] {f.title}："
                              f"❌ 声称已修但文件根本没动")

    # 第 6 步：汇总落盘
    report.insert(4, f"- 判定：✅ 修好 {len(summary['verified'])} · "
                     f"❌ 没修好 {len(summary['remaining'])} · "
                     f"⛔ 改砸 {len(summary['failed'])} · "
                     f"🆕 新发现 {len(summary['new_findings'])}")

    (run_dir / "verification.md").write_text("\n".join(report), encoding="utf-8")
    state.save(run_dir / "opt_state.json")
    return summary
