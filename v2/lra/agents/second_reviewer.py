"""Second reviewer — cloud arbitration. Confirms / rejects / defers each finding.

Findings are never dropped here: rejected ones keep their verdict and reason,
which makes them reusable learning material.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from lra.llm.structured import chat_structured
from lra.schemas.finding import Finding, Verdict

_SYSTEM = (
    "你是资深代码审查仲裁员。用户会给你一份初审发现清单，你要逐条裁决：\n"
    "- confirmed：问题真实存在、证据充分；\n"
    "- rejected：误报，证据不成立；\n"
    "- uncertain：证据不足以确认也无法排除。\n"
    "每条给出简短理由。只输出 JSON，不要任何解释。\n"
    '{"verdicts": [{"finding_id": "F1", "verdict": "confirmed|rejected|uncertain", '
    '"reason": "理由"}]}'
)


class VerdictItem(BaseModel):
    finding_id: str
    verdict: Verdict
    reason: str = ""


class VerdictList(BaseModel):
    verdicts: list[VerdictItem]


def load_mistakes_text(path: str | Path | None) -> str:
    """Read a JSONL 错题本 into one prompt-ready block; "" when absent/empty."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    lines = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        title = rec.get("title", "")
        reason = rec.get("reason", "")
        lines.append(f"- {title}（{reason}）")
    return "\n".join(lines)


def second_review(items: list[Finding], root, client,
                  save_path: str | Path | None = None,
                  mistakes_path: str | Path | None = None) -> list[Finding]:
    if not items:
        return items

    listing = "\n\n".join(
        f"[{f.id}] {f.title}（{f.file_path}:{f.line_start}-{f.line_end}，"
        f"严重度 {f.severity}）\n描述：{f.description}\n证据：\n{f.evidence[:400]}"
        for f in items
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"初审发现清单：\n\n{listing}"},
    ]

    try:
        result = chat_structured(client, messages, VerdictList, temperature=0.2)
        verdicts = {v.finding_id: v for v in result.verdicts}
    except Exception:
        verdicts = {}

    for f in items:
        v = verdicts.get(f.id)
        f.second_verdict = v.verdict if v else "uncertain"
        f.second_reason = v.reason if v else "仲裁调用失败，按存疑处理"

    if save_path is not None:
        Path(save_path).write_text(
            json.dumps([f.model_dump(mode="json") for f in items],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 错题本：被驳回的误报追加为 JSONL，供后续轮次提示模型不要再犯
    rejected = [f for f in items if f.second_verdict == "rejected"]
    if rejected:
        path = (Path(mistakes_path) if mistakes_path is not None
                else (Path(save_path).parent / "mistakes.jsonl"
                      if save_path is not None else None))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [{
                "title": f.title,
                "reason": f.second_reason or "",
                "category": f.category,
                "file": f.file_path,
                "evidence": f.evidence,
            } for f in rejected]
            with path.open("a", encoding="utf-8") as fh:
                fh.write("".join(
                    json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return items
