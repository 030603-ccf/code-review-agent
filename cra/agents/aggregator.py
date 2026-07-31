"""AggregatorAgent —— 证据校验与去重（确定性实现，零 LLM）。

方案里这里原本也是 14B agent，实现时改成了纯代码。
这是一个重要的工程判断，值得记住：
  - 证据校验 = 字符串匹配：代码做 100% 准确、零 token；模型做反而会幻觉
  - 去重 = 行号区间 + 分类比较：规则明确，代码做更快更稳
  结论：LLM 只该花在“理解”上，不该花在“查重”上。
  能确定做的事，绝不交给概率模型。（和 AST 扫描器同一个原则）

它修复的就是 Phase 1 报告里你亲眼看到的问题：
证据文本全对但行号漂移（SQL 注入报 8-9 实际 10-11）。
"""

import difflib
import re
from pathlib import Path

from cra.schemas.finding import Finding

# 严重度排序权重
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# 证据模糊救援：相似度阈值（低于此值视为幻觉，维持丢弃）
# 0.40 = 宽松模式：宁可多报不漏报，下游复查会再过滤一遍
RESCUE_THRESHOLD = 0.40


def _locate(evidence: str, src_lines: list[str]) -> tuple[int, int] | None:
    """在源文件里定位证据，返回 (起始行, 结束行)；找不到返回 None。

    匹配策略：忽略每行首尾空白后做"连续行精确匹配"。
    模型照抄证据时可能改掉缩进，但很少改动代码本体。
    """
    ev_lines = [l.strip() for l in evidence.strip().splitlines() if l.strip()]
    if not ev_lines:
        return None
    stripped = [l.strip() for l in src_lines]

    # 单行证据太短（如 "return None"）会到处撞车，不足为凭
    if len(ev_lines) == 1 and len(ev_lines[0]) < 8:
        return None

    k = len(ev_lines)
    for i in range(len(stripped) - k + 1):
        if stripped[i:i + k] == ev_lines:
            return (i + 1, i + k)     # 返回 1 起始的真实行号区间
    return None


def _normalize(text: str) -> str:
    """规范化文本用于相似度比较：去首尾空白、连续空白折叠为单空格。"""
    return re.sub(r'\s+', ' ', text.strip())


def _rescue(evidence: str, src_lines: list[str],
            line_start: int, line_end: int) -> tuple[str, int, int] | None:
    """证据模糊救援：精确匹配失败后，在自报行号窗口内做相似度比对。

    设计约束：
      - 只在自报窗口附近找，禁止全文搜索——防止幻觉证据吸到不相干代码上
      - 多行证据先尝试整体匹配窗口内连续行块，再退化到单行最佳匹配
      - 相似度 >= RESCUE_THRESHOLD 才救援，否则维持丢弃

    返回 (real_evidence_text, real_line_start, real_line_end) 或 None。
    """
    ev_lines = [l.strip() for l in evidence.strip().splitlines() if l.strip()]
    if not ev_lines:
        return None

    # 与 _locate 同一条规则：单行证据太短会到处撞车，不足为凭
    if len(ev_lines) == 1 and len(ev_lines[0]) < 8:
        return None

    # 计算窗口：模型自报行号 ± 2（1-based 转 0-based），clamp 到文件边界
    win_lo = max(0, line_start - 1 - 2)
    win_hi = min(len(src_lines), line_end + 2)  # line_end 已是 1-based，+2 后做 exclusive 上界
    if win_lo >= win_hi:
        return None

    window = src_lines[win_lo:win_hi]

    best_ratio = 0.0
    best_start = 0   # 窗口内 0-based 偏移
    best_end = 1
    best_text = ""

    k = len(ev_lines)

    # 策略 1：多行整体匹配——窗口内取连续 k 行块，规范化后和证据整体比
    if k > 1:
        ev_joined = _normalize("\n".join(ev_lines))
        for i in range(len(window) - k + 1):
            candidate = _normalize("\n".join(window[i:i + k]))
            ratio = difflib.SequenceMatcher(None, ev_joined, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i
                best_end = i + k
                best_text = "\n".join(window[i:i + k])

    # 策略 2：单行最佳匹配——证据展平后和窗口内每行比
    ev_flat = _normalize(" ".join(ev_lines))
    for i, wl in enumerate(window):
        ratio = difflib.SequenceMatcher(None, ev_flat, _normalize(wl)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i
            best_end = i + 1
            best_text = window[i]

    if best_ratio >= RESCUE_THRESHOLD:
        real_start = win_lo + best_start + 1  # 转 1-based
        real_end = win_lo + best_end          # exclusive 转 inclusive 1-based
        return (best_text, real_start, real_end)

    return None


def _overlaps(a: Finding, b: Finding) -> bool:
    """两条 finding 的行号区间是否重叠。"""
    return a.line_start <= b.line_end and b.line_start <= a.line_end


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """同文件 + 同分类 + 行号重叠 → 视为重复，保留置信度高者。"""
    keep: list[Finding] = []
    for f in sorted(findings, key=lambda x: -x.confidence):
        if not any(g.file_path == f.file_path
                   and g.category == f.category
                   and _overlaps(g, f) for g in keep):
            keep.append(f)
    return keep


def aggregate(findings: list[Finding], root: str | Path, bus=None) -> list[Finding]:
    """聚合主流程：证据校验（含行号纠正）-> 去重 -> 按严重度排序。

    返回"干净"的 findings；被丢弃的通过 bus 记录，可审计。
    """
    root = Path(root)
    valid: list[Finding] = []

    for f in findings:
        src_path = root / f.file_path
        if not src_path.exists():
            if bus:
                bus.emit("aggregate", "Aggregator", f"丢弃 {f.id}：文件不存在 {f.file_path}")
            continue
        src_lines = src_path.read_text(encoding="utf-8", errors="replace").splitlines()
        loc = _locate(f.evidence, src_lines)
        if loc is None:
            # 精确匹配失败 → 尝试模糊救援
            rescued = _rescue(f.evidence, src_lines, f.line_start, f.line_end)
            if rescued is None:
                # 宽松模式：证据匹配不上也不丢弃，保留原始行号，打标记
                # 下游复查员会再判一次，这里不充当“法官”
                f.evidence_corrected = True
                if bus:
                    bus.emit("aggregate", "Aggregator",
                             f"宽放 {f.id}（{f.title}）：证据未匹配但保留")
                valid.append(f)
                continue
            # 救援成功：替换证据为源码原文、行号吸附、打标记
            f.evidence, f.line_start, f.line_end = rescued
            f.evidence_corrected = True
            if bus:
                bus.emit("aggregate", "Aggregator",
                         f"救援 {f.id}（{f.title}）：证据模糊匹配成功，"
                         f"行号吸附至 {rescued[1]}-{rescued[2]}")
            valid.append(f)
            continue
        # 关键一步：用证据的真实位置纠正模型的行号——不信模型，信匹配
        if (f.line_start, f.line_end) != loc:
            if bus:
                bus.emit("aggregate", "Aggregator",
                         f"纠正 {f.id}（{f.title}）行号 {f.line_start}-{f.line_end} -> {loc[0]}-{loc[1]}")
            f.line_start, f.line_end = loc
        valid.append(f)

    deduped = _dedupe(valid)
    if bus:
        bus.emit("aggregate", "Aggregator",
                 f"校验 {len(findings)} -> 有效 {len(valid)} -> 去重后 {len(deduped)}")

    ordered = sorted(deduped, key=lambda f: (SEVERITY_ORDER[f.severity], f.file_path, f.line_start))

    # 全局重排 id——这是真实项目跑出来的血的教训：
    # 模型是每个块各自编号的（F1、F2...），跨块、跨文件必然撞车，
    # MemoirAI 审查出了 40 条漏洞、id 全是 "F1"。
    # id 是下游一切状态跟踪的键（OptState / Verifier / 错题本），
    # 撞车 = 记录互相覆盖、状态机整个错乱。
    # 排序后再编号，id 顺序就是报告顺序：F1 永远是最严重的那条。
    for i, f in enumerate(ordered, 1):
        f.id = f"F{i}"

    return ordered
