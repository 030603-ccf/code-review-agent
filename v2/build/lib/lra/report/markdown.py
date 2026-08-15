"""Markdown report rendering.

Functional baseline: heading, summary counts, severity-grouped findings, and a
verdict split when second review ran. The visual layer may restyle this later
without changing the signature.
"""

from lra.schemas.finding import Finding

_SEV_ORDER = ["critical", "high", "medium", "low"]
_SEV_LABEL = {"critical": "🔴 严重", "high": "🟠 高", "medium": "🟡 中", "low": "🟢 低"}
_CAT_LABEL = {"security": "安全", "performance": "性能",
              "readability": "可读性", "best_practice": "最佳实践"}


def _counts(findings: list[Finding]) -> dict:
    sev = {s: 0 for s in _SEV_ORDER}
    cat: dict[str, int] = {}
    verdict = {"confirmed": 0, "rejected": 0, "uncertain": 0}
    for f in findings:
        sev[f.severity] = sev.get(f.severity, 0) + 1
        cat[f.category] = cat.get(f.category, 0) + 1
        if f.second_verdict in verdict:
            verdict[f.second_verdict] += 1
    return {"sev": sev, "cat": cat, "verdict": verdict}


def render_report(findings: list[Finding], meta: dict) -> str:
    n = len(findings)
    c = _counts(findings)
    lines: list[str] = []

    lines.append("# 代码审查报告\n")
    lines.append("| 项目 | 文件数 | 初审模型 | 初审 tokens |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(f"| `{meta.get('project', '')}` | {meta.get('file_count', '?')} | "
                 f"`{meta.get('model', '?')}` | {meta.get('tokens', 0)} |")
    if meta.get("second_model"):
        lines.append(f"\n> 终审模型：`{meta['second_model']}` · "
                     f"消耗 {meta.get('second_tokens', 0)} tokens\n")
    lines.append("---\n")
    lines.append("## 📊 统计摘要\n")
    lines.append(f"- **发现总数**：{n}")
    sev_line = " · ".join(f"{_SEV_LABEL[s]} {c['sev'].get(s, 0)}" for s in _SEV_ORDER)
    cat_line = " · ".join(f"{_CAT_LABEL.get(k, k)} {v}" for k, v in c["cat"].items())
    lines.append(f"- **严重度**：{sev_line}")
    if cat_line:
        lines.append(f"- **分类**：{cat_line}")
    if any(c["verdict"].values()):
        lines.append(f"- **终审**：✅ 确认 {c['verdict']['confirmed']} · ⚠️ 存疑 "
                     f"{c['verdict']['uncertain']} · ❌ 驳回 {c['verdict']['rejected']}")
    lines.append("")

    if not findings:
        lines.append("✅ 未发现问题。")
        return "\n".join(lines) + "\n"

    has_verdict = any(f.second_verdict is not None for f in findings)

    if has_verdict:
        rejected = [f for f in findings if f.second_verdict == "rejected"]
        kept = [f for f in findings if f.second_verdict != "rejected"]
    else:
        rejected, kept = [], findings

    def _render_group(title: str, group: list[Finding]) -> None:
        if not group:
            return
        lines.append(f"## {title}\n")
        for f in group:
            verdict_note = ""
            if has_verdict and f.second_verdict:
                verdict_note = f" · 终审 {f.second_verdict}"
            lines.append(f"### [{f.id}] {_SEV_LABEL.get(f.severity, f.severity)} "
                         f"· {_CAT_LABEL.get(f.category, f.category)}{verdict_note}")
            lines.append(f"- **{f.title}**")
            lines.append(f"- 位置：`{f.file_path}:{f.line_start}-{f.line_end}` · 置信度 {f.confidence:.2f}")
            lines.append(f"- 描述：{f.description}")
            if f.evidence:
                lines.append(f"- 证据：\n```\n{f.evidence}\n```")
            if f.suggestion:
                lines.append(f"- 建议：{f.suggestion}")
            if f.second_reason:
                lines.append(f"- 终审理由：{f.second_reason}")
            lines.append("")

    _render_group("问题清单", kept)
    if has_verdict:
        _render_group("二级审查驳回（学习材料）", rejected)

    return "\n".join(lines) + "\n"
