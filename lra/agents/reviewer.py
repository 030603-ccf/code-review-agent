"""Reviewer — one LLM call per chunk, with out-of-chunk signature context."""

import re

from lra.llm.prompts import load_prompt, load_supplement
from lra.llm.structured import chat_structured
from lra.schemas.finding import Finding, FindingList

_FMT = (
    "【输出格式】你的完整回复必须是且仅是一个合法 JSON 对象"
    "（以 { 开头、以 } 结尾），不要输出任何解释或 markdown 标记：\n"
    '{"findings": [{"id": "F1",'
    '"category": "security|performance|readability|best_practice|correctness",'
    '"severity": "critical|high|medium|low",'
    '"file_path": "用户给的文件路径",'
    '"line_start": 起始行号, "line_end": 结束行号,'
    '"title": "一句话标题",'
    '"description": "问题是什么、为什么有害",'
    '"evidence": "从文件照抄的代码",'
    '"suggestion": "怎么改",'
    '"confidence": 0.0到1.0}]}'
)

# 思考模式（thinking）下 reasoning 与正文共享 max_tokens 预算，且本常量会
# 通过 _estimate_max_tokens 覆盖 config.yaml 的 max_tokens。开思考时必须给足
# 预算（16384），否则 reasoning 会吃满 2048 把正文 JSON 截断成
# "no JSON object found"。实测（quixbugs）：开思考 + 16384 行级召回 57.5%→92.5%。
MAX_OUTPUT_TOKENS = 16384
TOKEN_BUFFER = 128
CHARS_PER_TOKEN = 3.5

# 候选文本一行一个 ``[行 N] Severity: message``；只把行号落在 chunk 范围内的
# 注入 prompt，避免让模型验证它根本看不到的行。
_CANDIDATE_LINE_RE = re.compile(r"^\[行 (\d+)\]")


def _filter_candidates_by_range(text: str, line_start: int, line_end: int) -> str:
    """Keep only candidate lines whose 行号 falls within [line_start, line_end].

    A chunk only shows those lines, so anything outside is noise that the model
    cannot verify. Lines without a parseable ``[行 N]`` prefix are dropped.
    """
    if not text:
        return ""
    kept: list[str] = []
    for line in text.splitlines():
        m = _CANDIDATE_LINE_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if line_start <= n <= line_end:
            kept.append(line)
    return "\n".join(kept)


def _build_system_prompt(ext: str, rules_text: str = "", profile: str | None = None) -> str:
    """Assemble the system prompt; `profile` (client.config.name) selects a
    {name}.{profile}.md variant when present, else the base reviewer.md."""
    parts = [load_prompt("reviewer", profile=profile)]
    supplement = load_supplement(ext)
    if supplement:
        parts.append(supplement)
    if rules_text:
        parts.append(rules_text)
    parts.append(_FMT)
    return "\n\n".join(parts)


def build_signature_context(entry: dict, chunk: dict) -> str:
    """List symbols that live entirely outside the chunk so the model knows
    they exist (prevents false "undefined name" reports)."""
    others = [s for s in entry["symbols"]
              if s["line_end"] < chunk["line_start"]
              or s["line_start"] > chunk["line_end"]]
    if not others and not entry["imports"]:
        return ""
    lines = ["【参考上下文：本文件其他符号的签名，仅供理解，不要审查它们】"]
    lines += [s["signature"] for s in others[:50]]
    if entry["imports"]:
        lines.append("【导入】 " + "; ".join(entry["imports"][:10]))
    return "\n".join(lines)


def _estimate_max_tokens(messages: list[dict], context_length: int) -> int:
    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_input = int(total_chars / CHARS_PER_TOKEN)
    available = context_length - est_input - TOKEN_BUFFER
    return max(512, min(MAX_OUTPUT_TOKENS, available))


def review_chunk(client, entry: dict, chunk: dict,
                 mistakes_text: str = "") -> list[Finding]:
    """Review a single chunk. Raises StructuredOutputError on unrecoverable
    output; transient errors from the client propagate for the node to retry.

    `mistakes_text` carries previously rejected findings (错题本) so the model
    does not repeat them; it is injected before the code block.
    """
    ctx = build_signature_context(entry, chunk)
    lang = chunk["file"].rsplit(".", 1)[-1] if "." in chunk["file"] else ""

    user_parts = [
        f"文件路径：{chunk['file']}",
        f"以下代码块覆盖该文件第 {chunk['line_start']}-{chunk['line_end']} 行，"
        f"每行前面的数字是真实行号，line_start/line_end 必须用这些行号。",
    ]
    dep_ctx = entry.get("_dep_context", "")
    if dep_ctx:
        user_parts.append(dep_ctx)
    if ctx:
        user_parts.append(ctx)
    if mistakes_text:
        user_parts.append(f"【历史误报，请勿再犯】{mistakes_text}")
    lsp_candidates = entry.get("_lsp_candidates", "")
    lsp_candidates = _filter_candidates_by_range(
        lsp_candidates, chunk["line_start"], chunk["line_end"])
    if lsp_candidates:
        user_parts.append(
            "【语言服务器候选问题】语言服务器报了以下候选，请验证："
            "真问题纳入 findings 并补全 description/suggestion；"
            "误报或纯风格建议请忽略，不要报告\n" + lsp_candidates)
    user_parts.append(f"```{lang}\n{chunk['text']}\n```")
    hint = entry.get("_issue_hint")
    if hint:
        user_parts.append(f"【用户线索】用户怀疑以下问题可能存在，请重点核查：{hint}")

    profile = getattr(getattr(client, "config", None), "name", "") or None
    messages = [
        {"role": "system",
         "content": _build_system_prompt(lang, entry.get("_rules_text", ""),
                                         profile=profile)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    context_length = getattr(getattr(client, "config", None), "context_length", 8192)
    result = chat_structured(
        client, messages, FindingList, temperature=0.2,
        max_tokens=_estimate_max_tokens(messages, context_length),
    )
    # never trust the model's file_path — overwrite with the real one
    for f in result.findings:
        f.file_path = chunk["file"]
    return result.findings
