"""Structured output: extract -> validate -> repair -> prose-extract -> retry.

Small models mangle JSON in predictable ways (markdown fences, preamble,
smart quotes, bare keys, trailing commas). Fixing those mechanically is free;
calling the LLM again is not. So the cascade is: parse strictly, then repair,
then pull findings out of natural-language prose, and only if all that fails
feed the validation error back and retry.
"""

import json
import re

from pydantic import BaseModel, ValidationError


class StructuredOutputError(Exception):
    """Raised when retries are exhausted and the output is still invalid."""


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_NO_ISSUE_RE = re.compile(
    r"(没有发现|未发现|不存在|无问题|没有问题|代码质量良好|代码没有|"
    r"no\s+(issues?|bugs?|problems?|findings?)|"
    r"the code (is|looks) (correct|fine|good|clean))",
    re.IGNORECASE,
)

# --- mechanical JSON repairs ---
_BARE_KEY_RE = re.compile(r'([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:')
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SMART_QUOTES = str.maketrans({
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
})
_SINGLE_KEY_RE = re.compile(r"([{,])\s*'([^']+)'(\s*:)")
_SINGLE_VAL_RE = re.compile(r"(:\s*)'([^']*)'")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")


def _extract_json(text: str) -> str:
    """Return the JSON object substring from possibly-noisy output."""
    t = _THINK_RE.sub("", text.strip()).strip()

    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()

    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    if _NO_ISSUE_RE.search(t):
        return '{"findings": []}'
    raise ValueError("no JSON object found")


def _repair(text: str) -> str:
    """Apply mechanical fixes and return valid JSON, or raise."""
    t = text.strip()
    t = t.translate(_SMART_QUOTES)
    t = _BARE_KEY_RE.sub(r'\1"\2":', t)
    t = _TRAILING_COMMA_RE.sub(r"\1", t)
    t = _SINGLE_KEY_RE.sub(r'\1"\2"\3', t)
    t = _SINGLE_VAL_RE.sub(r'\1"\2"', t)

    def _fix_control(m):
        ch = m.group(0)
        return "" if ch in "\n\r\t" else " "
    t = _CONTROL_CHAR_RE.sub(_fix_control, t)

    t = _extract_json(t)
    json.loads(t)
    return t


# --- prose extraction (last-resort fallback for non-JSON output) ---
# category/severity values, each with English regex + Chinese substrings.
# English boundaries use ASCII lookarounds, not \b: CJK chars are \w in
# Python, so \b would not fire between e.g. "度" and "critical".
_CATEGORY_ALIASES: dict[str, tuple[re.Pattern, tuple[str, ...]]] = {
    "security": (re.compile(r"(?<![A-Za-z])security(?![A-Za-z])"), ("安全",)),
    "performance": (re.compile(r"(?<![A-Za-z])performance(?![A-Za-z])"), ("性能",)),
    "readability": (re.compile(r"(?<![A-Za-z])readability(?![A-Za-z])"), ("可读性",)),
    "best_practice": (
        re.compile(r"(?<![A-Za-z])best\s*_?practice(?![A-Za-z])"), ("最佳实践",)),
}

_SEVERITY_ALIASES: dict[str, tuple[re.Pattern, tuple[re.Pattern, ...]]] = {
    "critical": (
        re.compile(r"(?<![A-Za-z])critical(?![A-Za-z])"),
        (re.compile(r"严重(?!度|性|程|级|等|的)"),),
    ),
    "high": (
        re.compile(r"(?<![A-Za-z])high(?![A-Za-z])"),
        (re.compile(r"(?:级别|优先级|严重度|严重性|等级|程度|风险)\s*[为:：]?\s*高"),
         re.compile(r"高\s*(?:危|风险|优先级|等级)")),
    ),
    "medium": (
        re.compile(r"(?<![A-Za-z])medium(?![A-Za-z])"),
        (re.compile(r"(?:级别|优先级|严重度|严重性|等级|程度|风险)\s*[为:：]?\s*中"),
         re.compile(r"中\s*(?:危|风险|优先级|等级)")),
    ),
    "low": (
        re.compile(r"(?<![A-Za-z])low(?![A-Za-z])"),
        (re.compile(r"(?:级别|优先级|严重度|严重性|等级|程度|风险)\s*[为:：]?\s*低"),
         re.compile(r"低\s*(?:危|风险|优先级|等级)")),
    ),
}

_FILE_RE = re.compile(
    r"[\w./\\-]+\.(?:py|java|js|ts|go|rs|rb|php|sh|sql|json|yml|yaml|c|"
    r"cpp|h|hpp|cs|html|css|kt|swift|scala)"
)
_LINE_RANGE_RE = re.compile(
    r"(?:第?\s*(\d+)\s*[-–—~至到]\s*(\d+)\s*(?:行|line|lines|位置)"
    r"|(?:行|line|lines|位置)\s*[为:：]?\s*(\d+)\s*[-–—~至到]\s*(\d+))",
    re.IGNORECASE,
)
_LINE_SINGLE_RE = re.compile(
    r"(?:第|行|line|lines|位置|at)\s*[为:：]?\s*(\d+)"
    r"|(?<!\d)(\d+)\s*行",
    re.IGNORECASE,
)
_MARKER_RE = re.compile(r"(?:发现|问题|漏洞|finding|issue)\s*\d+", re.IGNORECASE)
_TITLE_NOISE_RE = re.compile(
    r"^(?:发现|问题|漏洞|finding|issue)\s*\d+\s*[：:.\-、]?\s*"
    r"|^(?:文件(?:路径)?|路径|file)\s*[为:：]?\s*[\w./\\-]+\.\w+\s*"
    r"|^(?:第\s*)?\d+\s*[-–—~至到]\s*\d+\s*(?:行|line|lines|位置)\s*"
    r"|^(?:第|行|line|lines|位置)\s*\d+\s*(?:行)?\s*"
)


def _line_anchor_spans(text: str) -> list[tuple[int, int]]:
    """Line-anchor spans; ranges win, and singles inside them are dropped."""
    ranges = [(m.start(), m.end()) for m in _LINE_RANGE_RE.finditer(text)]
    kept: list[tuple[int, int]] = []
    for s, e in sorted(
            ((m.start(), m.end()) for m in _LINE_SINGLE_RE.finditer(text)),
            key=lambda x: -(x[1] - x[0])):
        if any(s < er and e > sr for sr, er in ranges):
            continue
        if any(s < er and e > sr for sr, er in kept):
            continue
        kept.append((s, e))
    return sorted(ranges + kept)


def _anchors(text: str) -> list[tuple[int, str]]:
    """(start, kind) block-boundary anchors; kind in {file, line, marker}."""
    cands: list[tuple[int, int, str]] = []  # (start, end, kind)
    cands += [(s, e, "line") for s, e in _line_anchor_spans(text)]
    cands += [(m.start(), m.end(), "file") for m in _FILE_RE.finditer(text)]
    cands += [(m.start(), m.end(), "marker") for m in _MARKER_RE.finditer(text)]
    best: dict[int, tuple[int, str]] = {}
    for s, e, k in sorted(cands, key=lambda x: (x[0], -(x[1] - x[0]))):
        best.setdefault(s, (e, k))
    return [(s, k) for s, (e, k) in sorted(best.items())]


def _split_blocks(text: str) -> list[str]:
    """Split into per-finding chunks. A new block starts when an anchor of a
    kind already seen appears again (file + its line stay together)."""
    anchors = _anchors(text)
    if not anchors:
        return [text.strip()]
    blocks: list[str] = []
    start, seen = 0, set()
    for pos, kind in anchors:
        if kind in seen:
            blocks.append(text[start:pos])
            start = pos
            seen = {kind}
        else:
            seen.add(kind)
    blocks.append(text[start:])
    return [b.strip() for b in blocks if b.strip()]


def _scan_categories(block: str) -> set[str]:
    return {cat for cat, (en, zh) in _CATEGORY_ALIASES.items()
            if en.search(block) or any(z in block for z in zh)}


def _scan_severities(block: str) -> set[str]:
    return {sev for sev, (en, zh) in _SEVERITY_ALIASES.items()
            if en.search(block) or any(p.search(block) for p in zh)}


def _block_file(text: str, block: str) -> str:
    m = _FILE_RE.search(block)
    if m:
        return m.group(0)
    bpos = text.find(block)
    if bpos >= 0:
        best, best_pos = "", -1
        for fm in _FILE_RE.finditer(text):
            if fm.start() <= bpos and fm.start() > best_pos:
                best, best_pos = fm.group(0), fm.start()
        if best:
            return best
    m = _FILE_RE.search(text)
    return m.group(0) if m else ""


def _block_lines(block: str) -> tuple[int, int]:
    m = _LINE_RANGE_RE.search(block)
    if m:
        a, b = m.group(1) or m.group(3), m.group(2) or m.group(4)
        if a and b:
            return int(a), int(b)
    m = _LINE_SINGLE_RE.search(block)
    if m:
        n = int(m.group(1) or m.group(2))
        return n, n
    return 0, 0


def _clean_block(block: str) -> str:
    """Strip leading marker/file/line prefixes (for titles)."""
    s = block.strip()
    while True:
        new = _TITLE_NOISE_RE.sub("", s, count=1)
        if new == s:
            break
        s = new.strip()
    return s.strip(" ：:，,。.；;、\n")


def extract_findings_from_text(text: str) -> dict | None:
    """Pull {"findings": [...]} out of natural-language review prose.

    Returns None when the text carries no category/severity vocabulary at all
    (i.e. it is not a findings report). Evidence/suggestion are not extracted
    (model-rewritten code is untrustworthy; the aggregator re-reads source),
    so they stay empty. Extracted fields are discounted: confidence 0.7.
    """
    if not text or not text.strip():
        return None
    if not (_scan_categories(text) or _scan_severities(text)):
        return None
    findings = []
    for i, block in enumerate(_split_blocks(text), start=1):
        cats = _scan_categories(block)
        sevs = _scan_severities(block)
        category = cats.pop() if len(cats) == 1 else "best_practice"
        severity = sevs.pop() if len(sevs) == 1 else "medium"
        file_path = _block_file(text, block)
        line_start, line_end = _block_lines(block)
        description = block.strip(" ：:，,。.；;、\n")
        findings.append({
            "id": f"extract-{i}",
            "category": category,
            "severity": severity,
            "file_path": file_path,
            "line_start": line_start,
            "line_end": line_end,
            "title": _clean_block(block)[:60] or f"{category} issue",
            "description": description,
            "evidence": "",
            "suggestion": "",
            "confidence": 0.7,
        })
    return {"findings": findings}


def chat_structured(client, messages: list[dict], schema: type[BaseModel],
                    max_retries: int = 3, **chat_overrides):
    """Call the model and return a validated `schema` instance.

    On validation failure, first try mechanical JSON repair, then extract
    findings from prose, then retry with the error fed back into the
    conversation.
    """
    chat_overrides.setdefault("response_format", {"type": "json_object"})
    convo = list(messages)
    last_err: Exception | None = None

    for attempt in range(max_retries + 1):
        text = client.chat(convo, **chat_overrides)
        try:
            return schema.model_validate(json.loads(_extract_json(text)))
        except (ValueError, ValidationError) as e:
            last_err = e
            try:
                repaired = _repair(text)
                return schema.model_validate(json.loads(repaired))
            except Exception:
                pass  # repair failed too — fall through
            try:
                extracted = extract_findings_from_text(text)
                if extracted is not None:
                    return schema.model_validate(extracted)
            except Exception:
                pass  # prose extraction failed too — fall through to retry

        if attempt >= max_retries:
            break
        hint = (
            f"你的输出未通过校验：{last_err}\n"
            "【严格格式要求】你的回复必须以 { 开头、以 } 结尾，是一个合法 JSON 对象，"
            "不要输出任何解释、思考过程或 markdown 标记。"
            "如果代码没有问题，输出 {\"findings\": []}。"
        )
        convo = convo + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": hint},
        ]

    raise StructuredOutputError(
        f"结构化输出重试 {max_retries} 次后仍失败：{last_err}")
