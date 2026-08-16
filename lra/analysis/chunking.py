"""Chunk a file along symbol boundaries — never cut a function in half.

Strategy:
  1. whole file fits the budget  -> one chunk
  2. no symbols                  -> fixed line-window fallback with overlap
  3. otherwise                   -> greedily pack consecutive symbols, then
     split any single oversized symbol by lines (with overlap)
"""

MAX_CHUNK_CHARS = 4500
OVERLAP_LINES = 10
WINDOW_FALLBACK_LINES = 180


def _numbered(lines: list[str], lo: int, hi: int) -> str:
    return "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))


def _range_chars(lines: list[str], lo: int, hi: int) -> int:
    return sum(len(l) + 1 for l in lines[lo - 1:hi])


def _make_chunk(rel: str, lo: int, hi: int, lines: list[str],
                symbols: list[dict]) -> dict:
    names = [s["name"] for s in symbols
             if s["line_start"] >= lo and s["line_end"] <= hi]
    return {
        "file": rel, "line_start": lo, "line_end": hi,
        "text": _numbered(lines, lo, hi), "symbols": names,
    }


def _split_lines(rel: str, lines: list[str], lo: int, hi: int,
                 max_chars: int, overlap: int, symbols: list[dict]) -> list[dict]:
    chunks: list[dict] = []
    cur = lo
    while cur <= hi:
        end = cur
        while end < hi and _range_chars(lines, cur, end + 1) <= max_chars:
            end += 1
        chunks.append(_make_chunk(rel, cur, end, lines, symbols))
        nxt = end + 1 - overlap
        cur = nxt if nxt > cur else end + 1
    return chunks


def _window_fallback(rel: str, lines: list[str], max_chars: int,
                     overlap: int) -> list[dict]:
    chunks: list[dict] = []
    n = len(lines)
    cur = 1
    while cur <= n:
        end = min(cur + WINDOW_FALLBACK_LINES - 1, n)
        while end > cur and _range_chars(lines, cur, end) > max_chars:
            end -= 1
        chunks.append(_make_chunk(rel, cur, end, lines, []))
        if end == n:
            break
        nxt = end + 1 - overlap
        cur = nxt if nxt > cur else end + 1
    return chunks


def chunk_file(entry: dict, content: str,
               max_chars: int = MAX_CHUNK_CHARS,
               overlap: int = OVERLAP_LINES) -> list[dict]:
    rel = entry["relpath"]
    lines = content.splitlines()
    n = len(lines)
    symbols = sorted(entry["symbols"], key=lambda s: s["line_start"])

    if len(content) <= max_chars:
        return [_make_chunk(rel, 1, n, lines, symbols)]
    if not symbols:
        return _window_fallback(rel, lines, max_chars, overlap)

    chunks: list[dict] = []
    cur_lo = cur_hi = None

    def flush():
        nonlocal cur_lo, cur_hi
        if cur_lo is not None:
            chunks.append(_make_chunk(rel, cur_lo, cur_hi, lines, symbols))
        cur_lo = cur_hi = None

    for s in symbols:
        lo, hi = s["line_start"], s["line_end"]
        if _range_chars(lines, lo, hi) > max_chars:
            flush()
            chunks.extend(_split_lines(rel, lines, lo, hi, max_chars, overlap, symbols))
            continue
        new_lo = lo if cur_lo is None else min(cur_lo, lo)
        new_hi = hi if cur_hi is None else max(cur_hi, hi)
        if _range_chars(lines, new_lo, new_hi) <= max_chars:
            cur_lo, cur_hi = new_lo, new_hi
        else:
            flush()
            cur_lo, cur_hi = lo, hi
    flush()

    if chunks:
        first, last = chunks[0], chunks[-1]
        if first["line_start"] > 1:
            first["line_start"] = 1
            first["text"] = _numbered(lines, 1, first["line_end"])
        if last["line_end"] < n:
            last["line_end"] = n
            last["text"] = _numbered(lines, last["line_start"], n)

    return chunks
