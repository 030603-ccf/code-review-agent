"""按符号切块 —— 修复 Phase 1"6000 字符一刀切"的正规方案。

铁律：切块永远沿函数/类边界下刀，绝不腰斩符号。
策略：
  1. 文件 ≤ 预算：整文件一块（大多数文件走这里）
  2. 文件 > 预算：把符号按行号排序后贪心打包——连续的小函数装进同一块
  3. 单个符号 > 预算（巨型函数）：在它内部按行切，块间保留重叠行
切出来的每一块都带真实行号，模型的 line_start/line_end 就有据可依。
"""

# 单块字符预算：14B 上下文有限（8192），块 + 提示词 + 签名上下文 + 输出
# 要一起塞进上下文。MemoirAI 压测实测：6000 时大块会溢出（400 错误），4500 安全
MAX_CHUNK_CHARS = 4500
OVERLAP_LINES = 10     # 巨型符号内部切分时，相邻块重叠的行数（保住上下文连续性）
# 无符号文件的兜底窗口：180 行 × 平均约 25 字符/行 ≈ 4500 预算，刚好装得下。
# 这个数就是这么拍出来的——行窗和字符预算是同一件事的两种度量。
WINDOW_FALLBACK_LINES = 180


def _numbered(lines: list[str], lo: int, hi: int) -> str:
    """把 lines[lo-1:hi] 渲染成带行号前缀的文本（行号 = 真实文件行号）。"""
    return "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))


def _range_chars(lines: list[str], lo: int, hi: int) -> int:
    """估算 lines[lo-1:hi] 的字符数（+1 是换行符）。"""
    return sum(len(l) + 1 for l in lines[lo - 1:hi])


def _make_chunk(rel: str, lo: int, hi: int, lines: list[str], symbols: list[dict]) -> dict:
    covered = [s["name"] for s in symbols
               if s["line_start"] >= lo and s["line_end"] <= hi]
    return {
        "file": rel,
        "line_start": lo,
        "line_end": hi,
        "text": _numbered(lines, lo, hi),
        "symbols": covered,   # 本块完整覆盖了哪些符号（上下文注入时要用）
    }


def _split_lines(rel: str, lines: list[str], lo: int, hi: int,
                 max_chars: int, overlap: int, symbols: list[dict]) -> list[dict]:
    """把 [lo, hi] 区间内部按行切成多段，段间保留 overlap 行重叠。

    只用于一种绝境：单个符号比整块预算还大（巨型函数）。
    （整文件无符号的退化路径已挪给 _window_fallback 固定行窗。）
    """
    chunks = []
    cur = lo
    while cur <= hi:
        end = cur
        # 从 cur 开始尽量往后扩，直到字符预算耗尽
        while end < hi and _range_chars(lines, cur, end + 1) <= max_chars:
            end += 1
        chunks.append(_make_chunk(rel, cur, end, lines, symbols))
        # 下一段从 end+1-overlap 开始，让两块共享 overlap 行
        nxt = end + 1 - overlap
        cur = nxt if nxt > cur else end + 1   # 防御死循环：至少前进一行
    return chunks


def _window_fallback(rel: str, lines: list[str],
                     max_chars: int, overlap: int,
                     window: int = WINDOW_FALLBACK_LINES) -> list[dict]:
    """无符号文件的固定行窗切块 —— 次优但安全的退化路径。

    适用场景：启发式提取不到符号的文件（纯模板 Vue、配置风格的 js、
    或者干脆没注册的代码风格）。没有符号边界可依，"不腰斩函数"无从谈起，
    只能按固定行窗下刀 + 块间重叠 overlap 行保住上下文连续性。
    说它"次优"：块边界可能正好切在某个函数中间；
    说它"安全"：重叠行让模型至少能看到切口前后的代码，不会彻底断章。

    双闸设计：行窗之内再受字符预算约束——防止压缩代码/超长行
    （比如 minified js 一行几千字符）把一个窗口撑爆上下文。
    """
    chunks = []
    n = len(lines)
    cur = 1
    while cur <= n:
        end = min(cur + window - 1, n)
        # 字符闸：窗口超预算就往后收（至少保住一行，防死循环）
        while end > cur and _range_chars(lines, cur, end) > max_chars:
            end -= 1
        chunks.append(_make_chunk(rel, cur, end, lines, []))  # 无符号可登记
        if end == n:
            break   # 已切到文件尾：直接收工。否则重叠步进会再产出一个
                    # 只含 overlap 行的"尾巴块"——内容全是重复，纯属浪费
        # 下一段从 end+1-overlap 开始，让两块共享 overlap 行
        nxt = end + 1 - overlap
        cur = nxt if nxt > cur else end + 1   # 防御死循环：至少前进一行
    return chunks


def chunk_file(entry: dict, content: str,
               max_chars: int = MAX_CHUNK_CHARS,
               overlap: int = OVERLAP_LINES) -> list[dict]:
    """把单个文件切成若干块。entry 是 project_map 里的索引条目。"""
    rel = entry["relpath"]
    lines = content.splitlines()
    n = len(lines)
    symbols = sorted(entry["symbols"], key=lambda s: s["line_start"])

    # 情况 1：整文件装得下
    if len(content) <= max_chars:
        return [_make_chunk(rel, 1, n, lines, symbols)]

    # 情况 2：没有任何符号信息（非 Python 启发式提取落空、纯脚本、纯模板），
    # 走固定行窗兜底——次优但安全，绝不能静默丢弃文件
    if not symbols:
        return _window_fallback(rel, lines, max_chars, overlap)

    # 情况 3：按符号贪心打包
    chunks: list[dict] = []
    cur_lo = cur_hi = None

    def flush():
        if cur_lo is not None:
            chunks.append(_make_chunk(rel, cur_lo, cur_hi, lines, symbols))

    for s in symbols:
        lo, hi = s["line_start"], s["line_end"]
        if _range_chars(lines, lo, hi) > max_chars:
            # 巨型符号：先封存当前块，再把它内部切开
            flush()
            cur_lo = cur_hi = None
            chunks.extend(_split_lines(rel, lines, lo, hi, max_chars, overlap, symbols))
            continue
        # 尝试把这个符号并入当前块（区间取并集，中间的空隙代码自然包含）
        new_lo = lo if cur_lo is None else min(cur_lo, lo)
        new_hi = hi if cur_hi is None else max(cur_hi, hi)
        if _range_chars(lines, new_lo, new_hi) <= max_chars:
            cur_lo, cur_hi = new_lo, new_hi
        else:
            flush()           # 装不下了：封块
            cur_lo, cur_hi = lo, hi
    flush()

    # 文件头（import 区，第一个符号之前）并入第一块；
    # 文件尾（最后一个符号之后的模块级代码）并入最后一块
    if chunks:
        first, last = chunks[0], chunks[-1]
        if first["line_start"] > 1:
            first["line_start"] = 1
            first["text"] = _numbered(lines, 1, first["line_end"])
        if last["line_end"] < n:
            last["line_end"] = n
            last["text"] = _numbered(lines, last["line_start"], n)

    return chunks
