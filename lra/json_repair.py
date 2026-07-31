"""json_repair.py —— 零 LLM 的 JSON 修复器。

当 LLM 输出的 JSON 不能通过 json.loads 或 pydantic 校验时，
在烧 token 调 LLM 重试之前，先用这个工具抢救一把。

三层级联（从快到慢、从精确到兜底）：
  第 1 层：JSON 语法修复 —— 修引号/逗号/键名，毫秒级
  第 2 层：结构化关键词提取 —— 从自然语言文本中抠出 finding 字段，拼成 JSON
  第 3 层：兜底 —— "没问题"模式识别，返回空 findings

设计原则：
  - 宁可不修（抛异常让 caller 走 LLM 重试）也不修出假数据
  - 所有正则都在 _RE 前缀的模块常量里，方便读和改
"""

import json
import re


# ========== 第 3 层兜底：自然语言"没问题"模式 ==========
_NO_ISSUE_RE = re.compile(
    r"(没有发现|未发现|不存在|无问题|没有问题|代码质量良好|代码没有|"
    r"no\s+(issues?|bugs?|problems?|findings?)|"
    r"the code (is|looks) (correct|fine|good|clean))",
    re.IGNORECASE,
)


def repair_json(text: str) -> str:
    """对 LLM 返回的原始文本做尽最大努力的 JSON 修复。

    Returns:
        合法的 JSON 字符串（可直接 json.loads）

    Raises:
        ValueError: 三层都失败，无法修复。caller 应走 LLM 重试。
    """
    t = text.strip()

    # ---- 第 1 层：语法修复 ----
    try:
        return _repair_syntax(t)
    except ValueError:
        pass

    # ---- 第 2 层：结构化提取 ----
    try:
        return _extract_findings_json(t)
    except ValueError:
        pass

    # ---- 第 3 层：自然语言兜底 ----
    if _NO_ISSUE_RE.search(t):
        return '{"findings": []}'

    raise ValueError("三层修复均失败，无法从输出中提取合法 JSON")


# ==================================================================
# 第 1 层：JSON 语法修复
# ==================================================================

# 键名模式：在 JSON 上下文中，未被引号包裹的裸键（如 findings:）
_BARE_KEY_RE = re.compile(r'([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:')

# 尾随逗号（对象或数组末尾的逗号）
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# 中文/智能引号
_SMART_QUOTES = str.maketrans({
    "“": '"', "”": '"',  # "
    "‘": "'", "’": "'",  # '
    "＂": '"',                 # fullwidth "
})

# 单引号键或值（JSON 不允许）
_SINGLE_QUOTE_KEY_RE = re.compile(r"([{,])\s*'([^']+)'(\s*:)")
_SINGLE_QUOTE_VAL_RE = re.compile(r"(:\s*)'([^']*)'")

# 控制字符（JSON 字符串内不允许裸控制字符，除了转义过的）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")


def _repair_syntax(text: str) -> str:
    """修复常见 JSON 语法错误，返回合法 JSON 字符串。

    不试图理解内容——只做机械的字符级修正。
    修完后用 json.loads 校验，失败就抛 ValueError。
    """
    t = text

    # 0. 剥 markdown 围栏和前后废话（复用 _extract_json 的核心逻辑）
    t = _strip_markdown_and_noise(t)

    # 1. 替换智能引号
    t = t.translate(_SMART_QUOTES)

    # 2. 裸键加引号：findings: → "findings":
    t = _BARE_KEY_RE.sub(r'\1"\2":', t)

    # 3. 删尾随逗号：{"a": 1,} → {"a": 1}
    t = _TRAILING_COMMA_RE.sub(r"\1", t)

    # 4. 单引号键/值 → 双引号
    t = _SINGLE_QUOTE_KEY_RE.sub(r'\1"\2"\3', t)
    t = _SINGLE_QUOTE_VAL_RE.sub(r'\1"\2"', t)

    # 5. 剔除行内控制字符（保留已被转义的）
    #    只处理 ASCII 控制字符，保留 \n \t 等转义序列
    def _replace_control(m):
        ch = m.group(0)
        # 保留常见的 JSON 转义序列（\n \t \r）
        return "" if ch in "\n\r\t" else " "
    t = _CONTROL_CHAR_RE.sub(_replace_control, t)

    # 校验
    json.loads(t)
    return t


def _strip_markdown_and_noise(text: str) -> str:
    """从文本中抠出 JSON 对象：去围栏、找首尾花括号。"""
    t = text.strip()

    # 去 markdown 围栏
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()

    # <think> 标签（DeepSeek 等模型）
    import re as _re
    t = _re.sub(r"<think>.*?</think>", "", t, flags=_re.DOTALL | _re.IGNORECASE).strip()

    # 找第一个 { 和最后一个 }
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]

    raise ValueError("找不到 JSON 对象边界")


# ==================================================================
# 第 2 层：结构化关键词提取
# ==================================================================

# 从"模型用自然语言描述了发现但没有输出 JSON"的文本中提取字段
_FINDING_BLOCK_RE = re.compile(
    r"(?:发现|问题|漏洞|缺陷)\s*(?:\d+|F\d+)?\s*[：:]\s*(.+?)(?=(?:发现|问题|漏洞|缺陷|$))",
    re.DOTALL,
)

# Finding 中的字段：从非结构化文本中抓取
_TITLE_RE = re.compile(r"(?:标题|title)[：:]\s*(.+?)(?:\n|$)", re.IGNORECASE)
_DESC_RE = re.compile(r"(?:描述|说明|description|问题)[：:]\s*(.+?)(?:\n\n|\n(?=[A-Z一-鿿])|$)", re.DOTALL | re.IGNORECASE)
_EVIDENCE_RE = re.compile(r"(?:证据|evidence|代码)[：:]\s*(.+?)(?:\n\n|\n(?=[A-Z一-鿿])|$)", re.DOTALL | re.IGNORECASE)
_SUGGESTION_RE = re.compile(r"(?:建议|suggestion|修复)[：:]\s*(.+?)(?:\n\n|\n(?=[A-Z一-鿿])|$)", re.DOTALL | re.IGNORECASE)
_LINE_RE = re.compile(r"(?:行|line|位置)[\s：:]*(\d+)[-–—](\d+)", re.IGNORECASE)
_FILE_RE = re.compile(r"(?:文件|file)[\s：:]*(\S+\.\w{1,6})", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"\b(security|performance|readability|best_practice)\b")
_SEVERITY_RE = re.compile(r"\b(critical|high|medium|low)\b")
_CONFIDENCE_RE = re.compile(r"(?:置信度|confidence)[\s：:]*(\d+\.?\d*)", re.IGNORECASE)


def _extract_findings_json(text: str) -> str:
    """从自然语言文本中提取 finding 信息，拼成合法 JSON。

    适用场景：模型输出了类似
        "发现 1：第 3 行有硬编码密码...严重度 critical..."
    但完全没有 JSON 结构的情况。
    """
    # 先尝试从文本中找到 category 或 severity 关键词——这是"模型确实报了发现"的信号
    cats = _CATEGORY_RE.findall(text)
    sevs = _SEVERITY_RE.findall(text)

    if not cats and not sevs:
        # 没有分类/严重度关键词 → 模型可能根本没有报发现
        raise ValueError("文本中无分类或严重度关键词，不是发现报告")

    findings = []
    # 按段落或自然分隔拆分，每段可能对应一条发现
    blocks = _split_into_finding_blocks(text)

    for i, block in enumerate(blocks):
        fid = f"F{i + 1}"
        title = _match_first(_TITLE_RE, block) or f"第{i + 1}条发现"
        desc = _match_first(_DESC_RE, block) or block[:200]
        evidence = _match_first(_EVIDENCE_RE, block) or ""
        suggestion = _match_first(_SUGGESTION_RE, block) or ""
        cat = _pick_category(block, cats) or "best_practice"
        sev = _pick_severity(block, sevs) or "medium"
        conf_str = _match_first(_CONFIDENCE_RE, block)
        confidence = float(conf_str) if conf_str else 0.7

        lines = _LINE_RE.search(block)
        line_start = int(lines.group(1)) if lines else 1
        line_end = int(lines.group(2)) if lines else line_start

        file_match = _FILE_RE.search(text)  # 文件路径通常在全文开头
        file_path = file_match.group(1) if file_match else "unknown"

        findings.append({
            "id": fid,
            "category": cat,
            "severity": sev,
            "file_path": file_path,
            "line_start": line_start,
            "line_end": line_end,
            "title": title.strip()[:120],
            "description": desc.strip()[:500],
            "evidence": evidence.strip()[:500],
            "suggestion": suggestion.strip()[:500],
            "confidence": min(max(confidence, 0.0), 1.0),
        })

    if not findings:
        raise ValueError("未能从文本中提取到任何发现")

    return json.dumps({"findings": findings}, ensure_ascii=False)


def _split_into_finding_blocks(text: str) -> list[str]:
    """把自然语言文本按"发现N"或"问题N"的标记拆成独立的块。"""
    # 按数字编号拆分
    parts = re.split(r"\n\s*(?=(?:发现|问题|漏洞|缺陷)\s*\d+)", text)
    # 去掉太短的（不太可能是一条完整的发现）
    return [p for p in parts if len(p.strip()) > 30]


def _match_first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _pick_category(text: str, fallback: list[str]) -> str | None:
    """从文本中提取 category，优先精确匹配。"""
    for cat in ("security", "performance", "readability", "best_practice"):
        if cat in text.lower():
            return cat
    return fallback[0] if fallback else None


def _pick_severity(text: str, fallback: list[str]) -> str | None:
    for sev in ("critical", "high", "medium", "low"):
        if sev in text.lower():
            return sev
    return fallback[0] if fallback else None
