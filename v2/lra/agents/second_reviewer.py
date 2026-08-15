"""Second reviewer — cloud arbitration. Confirms / rejects / defers each finding.

Findings are never dropped here: rejected ones keep their verdict and reason,
which makes them reusable learning material.
"""

import json
import threading
from pathlib import Path

from pydantic import BaseModel

from lra.llm.structured import chat_structured
from lra.schemas.finding import Finding, Verdict

# 错题本去重是 read-modify-write，节点层 8 个 worker 会并发写同一份
# mistakes.jsonl；用一把进程内锁让「读现有 title → 追加」原子化。
_MISTAKES_LOCK = threading.Lock()

# 错题本注入上限：只把最近 N 条误报送进 prompt，跑得越多不越贵。
# 共享常量：nodes.scan 用它截断注入，__main__._context_fingerprint 用它
# 决定指纹哈希哪几条——两边必须同值，否则缓存键与实际注入内容脱节。
MISTAKE_INJECT_LIMIT = 20

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


def _read_records(path: Path) -> list[dict]:
    """Parse a JSONL 错题本 into records, skipping blank / broken lines."""
    records: list[dict] = []
    if not path.is_file():
        return records
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return records


def load_mistakes_text(path: str | Path | None,
                       limit: int | None = None) -> str:
    """Read a JSONL 错题本 into one prompt-ready block; "" when absent/empty.

    `limit` caps how many of the most recent entries (last lines, append-only)
    are injected, so a long-lived notebook never bloats every prompt.
    """
    if not path:
        return ""
    records = _read_records(Path(path))
    if limit is not None:
        records = records[-limit:]
    return "\n".join(
        f"- {r.get('title', '')}（{r.get('reason', '')}）" for r in records)


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

    # 错题本：被驳回的误报追加为 JSONL，供后续轮次提示模型不要再犯。
    # 写前去重：相同 (file, title) 的条目（含本批内部重复）只写一次，防止无限膨胀。
    # 不同文件的同 title 误报是不同样本，各自保留。
    rejected = [f for f in items if f.second_verdict == "rejected"]
    if rejected:
        path = (Path(mistakes_path) if mistakes_path is not None
                else (Path(save_path).parent / "mistakes.jsonl"
                      if save_path is not None else None))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _MISTAKES_LOCK:
                existing = {(r.get("file"), r["title"])
                            for r in _read_records(path) if r.get("title")}
                seen: set[tuple] = set()
                records: list[dict] = []
                for f in rejected:
                    key = (f.file_path, f.title)
                    if key in existing or key in seen:
                        continue
                    seen.add(key)
                    records.append({
                        "title": f.title,
                        "reason": f.second_reason or "",
                        "category": f.category,
                        "file": f.file_path,
                        "evidence": f.evidence,
                    })
                if records:
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write("".join(
                            json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return items
