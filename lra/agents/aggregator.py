"""Aggregator — deterministic evidence verification, dedupe, id reassignment.

Zero LLM. String matching and line-range comparison are exact and free here;
spending tokens on them would be worse, not better.
"""

import difflib
import re
from pathlib import Path

from lra.schemas.finding import Finding

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
RESCUE_THRESHOLD = 0.40


def _locate(evidence: str, src_lines: list[str]) -> tuple[int, int] | None:
    ev_lines = [l.strip() for l in evidence.strip().splitlines() if l.strip()]
    if not ev_lines:
        return None
    if len(ev_lines) == 1 and len(ev_lines[0]) < 8:
        return None
    stripped = [l.strip() for l in src_lines]
    k = len(ev_lines)
    for i in range(len(stripped) - k + 1):
        if stripped[i:i + k] == ev_lines:
            return (i + 1, i + k)
    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _rescue(evidence: str, src_lines: list[str],
            line_start: int, line_end: int) -> tuple[str, int, int] | None:
    ev_lines = [l.strip() for l in evidence.strip().splitlines() if l.strip()]
    if not ev_lines or (len(ev_lines) == 1 and len(ev_lines[0]) < 8):
        return None

    win_lo = max(0, line_start - 1 - 2)
    win_hi = min(len(src_lines), line_end + 2)
    if win_lo >= win_hi:
        return None
    window = src_lines[win_lo:win_hi]

    best_ratio, best_start, best_end, best_text = 0.0, 0, 1, ""
    k = len(ev_lines)
    if k > 1:
        ev_joined = _normalize("\n".join(ev_lines))
        for i in range(len(window) - k + 1):
            ratio = difflib.SequenceMatcher(
                None, ev_joined, _normalize("\n".join(window[i:i + k]))).ratio()
            if ratio > best_ratio:
                best_ratio, best_start, best_end, best_text = ratio, i, i + k, "\n".join(window[i:i + k])

    ev_flat = _normalize(" ".join(ev_lines))
    for i, wl in enumerate(window):
        ratio = difflib.SequenceMatcher(None, ev_flat, _normalize(wl)).ratio()
        if ratio > best_ratio:
            best_ratio, best_start, best_end, best_text = ratio, i, i + 1, window[i]

    if best_ratio >= RESCUE_THRESHOLD:
        return (best_text, win_lo + best_start + 1, win_lo + best_end)
    return None


def _overlaps(a: Finding, b: Finding) -> bool:
    return a.line_start <= b.line_end and b.line_start <= a.line_end


def _dedupe(findings: list[Finding]) -> list[Finding]:
    keep: list[Finding] = []
    for f in sorted(findings, key=lambda x: -x.confidence):
        if not any(g.file_path == f.file_path and g.category == f.category
                   and _overlaps(g, f) for g in keep):
            keep.append(f)
    return keep


def aggregate(findings: list[Finding], root: str | Path) -> list[Finding]:
    root = Path(root)
    valid: list[Finding] = []

    for f in findings:
        src_path = root / f.file_path
        if not src_path.exists():
            continue
        src_lines = src_path.read_text(encoding="utf-8", errors="replace").splitlines()
        loc = _locate(f.evidence, src_lines)
        if loc is None:
            rescued = _rescue(f.evidence, src_lines, f.line_start, f.line_end)
            if rescued is not None:
                f.evidence, f.line_start, f.line_end = rescued
                f.evidence_corrected = True
            valid.append(f)  # keep even when unmatched; downstream re-judges
            continue
        if (f.line_start, f.line_end) != loc:
            f.line_start, f.line_end = loc
        valid.append(f)

    deduped = _dedupe(valid)
    ordered = sorted(deduped, key=lambda f: (SEVERITY_ORDER[f.severity],
                                             f.file_path, f.line_start))
    for i, f in enumerate(ordered, 1):
        f.id = f"F{i}"
    return ordered
