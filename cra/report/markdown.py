"""Markdown 报告渲染 —— 把结构化 findings 变成人读的报告。

为什么报告也要代码生成而不是让模型写：
报告是"交付物"，格式必须稳定；模型写报告既慢又可能瞎编统计数字。
确定性的事情交给代码，这是和 AST 扫描一样的原则。
"""

from cra.schemas.finding import Finding

# 严重度排序权重：报告里 critical 永远排最前
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABEL = {"critical": "🔴 严重", "high": "🟠 高", "medium": "🟡 中", "low": "🔵 低"}
CATEGORY_LABEL = {
    "security": "安全",
    "performance": "性能",
    "readability": "可读性",
    "best_practice": "最佳实践",
}


def render_report(findings: list[Finding], meta: dict) -> str:
    """渲染完整报告。meta 里放项目路径、文件数、token 消耗等运行信息。"""
    lines: list[str] = []
    lines.append("# 代码审查报告\n")
    lines.append(f"- 项目：`{meta.get('project', '?')}`")
    lines.append(f"- 文件数：{meta.get('file_count', '?')}")
    lines.append(f"- 模型：{meta.get('model', '?')}")
    lines.append(f"- 消耗 tokens：{meta.get('tokens', '?')}")
    # 漏斗下半段（二级审查）的模型和消耗；没启用就不显示这两行
    if meta.get("second_model"):
        lines.append(f"- 二级审查模型：{meta['second_model']}"
                     f"（{meta.get('second_tokens', '?')} tokens）")
    lines.append(f"- 发现问题：**{len(findings)}** 个\n")

    if not findings:
        lines.append("✅ 未发现问题。\n")
        return "\n".join(lines)

    # 按严重度排序，严重的在前
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])

    # 二级审查跑过（任何一条带裁决标记）就按裁决分组：
    # 确认 -> 主清单；存疑 -> 单独列出等人判；驳回 -> 学习材料区
    has_second = any(f.second_verdict for f in findings)
    if has_second:
        # verdict 为 None 的是混跑时没复核到的条目，随确认件进主清单
        confirmed = [f for f in ordered
                     if f.second_verdict in ("confirmed", None)]
        uncertain = [f for f in ordered if f.second_verdict == "uncertain"]
        rejected = [f for f in ordered if f.second_verdict == "rejected"]
    else:
        confirmed, uncertain, rejected = ordered, [], []

    if has_second:
        lines.append(f"> 二级审查：✅ 确认 {len([f for f in ordered if f.second_verdict == 'confirmed'])} · "
                     f"❌ 驳回 {len(rejected)} · ❓ 存疑 {len(uncertain)}\n")

    def _render_one(i: int, f: Finding) -> None:
        verdict_tag = {"confirmed": " ✅", "uncertain": " ❓"}.get(
            f.second_verdict or "", "")
        lines.append(f"### {i}. [{SEVERITY_LABEL[f.severity]}] {f.title}{verdict_tag}\n")
        lines.append(f"- 位置：`{f.file_path}:{f.line_start}-{f.line_end}`")
        lines.append(f"- 分类：{CATEGORY_LABEL[f.category]}　置信度：{f.confidence}")
        lines.append(f"- 问题：{f.description}")
        if f.second_reason:
            # 裁决理由（含严重度修正说明）：终审为什么这么判
            lines.append(f"- 二级审查：{f.second_reason}")
        corrected_tag = "（已修正——原始证据有微小抄写误差，已自动吸附到源码原文）" if f.evidence_corrected else ""
        lines.append(f"- 证据：{corrected_tag}")
        lines.append("```python")
        lines.append(f.evidence.strip())
        lines.append("```")
        lines.append(f"- 建议：{f.suggestion}\n")

    if confirmed:
        lines.append("## 问题清单\n")
        for i, f in enumerate(confirmed, 1):
            _render_one(i, f)

    if uncertain:
        lines.append("## ❓ 存疑（二级审查拿不准，请人工判定）\n")
        for i, f in enumerate(uncertain, 1):
            _render_one(i, f)

    if rejected:
        # 驳回区是学习材料：初审模型在什么上栽了跟头，全在这里
        lines.append("## ❌ 二级审查驳回（学习材料）\n")
        for f in rejected:
            lines.append(f"- **[{SEVERITY_LABEL[f.severity]}] {f.title}**"
                         f"（`{f.file_path}:{f.line_start}-{f.line_end}`）")
            lines.append(f"  驳回理由：{f.second_reason}")
        lines.append("")

    return "\n".join(lines)
