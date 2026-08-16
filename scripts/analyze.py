"""quixbugs 审查结果评估：召回率（文件级 + 行级）+ 精确率。

用法:
    python scripts/analyze.py <run_dir> <target_dir> [--correct-dir DIR]

例:
    python scripts/analyze.py runs/quix-python targets/quixbugs/python_programs \\
        --correct-dir targets/quixbugs/correct_python_programs
    python scripts/analyze.py runs/quix-java targets/quixbugs/java_programs \\
        --correct-dir targets/quixbugs/correct_java_programs

quixbugs 每个 buggy 程序恰好 1 个已知 bug；辅助文件（python 的 *_test/node，
java 的 Node/WeightedEdge）本身无 bug，报了算误报。

口径（不再用文件级冒充精度）：
    文件级召回 = 有 finding 落在 bug 文件即算命中（宽松）。
    行级召回   = 该文件任一 finding 的 line_start/line_end 覆盖 bug 行才算命中。
                 bug 行 = buggy 版本与 correct 版本 diff 出的变更行。
                 需要 --correct-dir；目录缺失/未提供时行级退化为文件级并明确标注。
"""

import argparse
import difflib
import json
import sys
from collections import defaultdict
from pathlib import Path

# 各语言里「本身无 bug」的辅助文件 stem（小写）
_AUX_STEMS = {"_test", "node", "weightededge"}


def _strip_trailing_docstring(text: str) -> str:
    """去掉 quixbugs 文件末尾的 module docstring（算法题描述，非程序本体）。

    buggy 文件习惯在代码后附一段三引号描述；correct 版本有时还会把 buggy 代码
    整段嵌进这段 docstring。直接 diff 会被这段描述把真正的 bug 行和副本代码混淆
    对齐（如 bucketsort 的 enumerate(arr)→enumerate(counts) 会被对齐到相邻行，真实
    bug 行反而漏标）。这里按行找第一个「行首就是三引号」的位置截断，只 diff 真实
    代码。对 Java（无尾部 docstring）是无操作。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('"""') or line.startswith("'''"):
            lines = lines[:i]
            break
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def bug_lines(buggy_text: str, correct_text: str) -> set[int]:
    """返回 buggy 版本里与 correct 不同的行号集合（1-based）。

    用 difflib.SequenceMatcher 找变更块：
    - replace/delete：buggy 里这些行需要改/删，直接就是 bug 行；
    - insert：correct 多出的行 = buggy 缺行，bug 位置是插入点（1-based）。
    截断尾部 docstring 不影响 buggy 代码区的行号（docstring 在末尾）。
    """
    a = _strip_trailing_docstring(buggy_text).splitlines()
    b = _strip_trailing_docstring(correct_text).splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    lines: set[int] = set()
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            lines.update(range(i1 + 1, i2 + 1))
        elif tag == "insert":
            # buggy 在 i1（0-based）行之后缺行；插入点 1-based = i1 + 1（>=1）
            lines.add(max(1, i1 + 1))
    return lines


def overlaps(line_start: int | None, line_end: int | None,
             bug_lines_set: set[int]) -> bool:
    """finding 的行区间是否覆盖任一 bug 行。"""
    lo = max(1, int(line_start or 1))
    hi = max(lo, int(line_end or lo))
    return any(lo <= bl <= hi for bl in bug_lines_set)


def any_finding_hits(findings: list[dict], buggy_text: str,
                     correct_text: str) -> bool:
    """行级命中：该文件的任一 finding 覆盖 bug 行。"""
    bl = bug_lines(buggy_text, correct_text)
    if not bl:
        return False
    return any(overlaps(f.get("line_start"), f.get("line_end"), bl)
               for f in findings)


def main() -> int:
    # UTF-8 stdout：避免 Windows GBK 控制台把 emoji/中文打成乱码或崩掉
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="quixbugs 审查结果评估（文件级 + 行级召回）")
    parser.add_argument("run_dir", help="review 产物目录（含 findings.json）")
    parser.add_argument("target_dir", help="buggy 程序目录")
    parser.add_argument("--correct-dir", default=None,
                        help="正确版本目录（diff 出行级召回；缺省退化为文件级口径）")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    target = Path(args.target_dir)
    findings_path = run_dir / "findings.json"
    if not findings_path.is_file():
        print(f"找不到 {findings_path}")
        return 1

    findings = json.loads(findings_path.read_text(encoding="utf-8"))
    by_file: dict[str, list] = defaultdict(list)
    for f in findings:
        by_file[f["file_path"]].append(f)

    ext = ".java" if target.name == "java_programs" else ".py"
    all_files = sorted(p.name for p in target.glob(f"*{ext}"))
    buggy = sorted(f for f in all_files
                   if Path(f).stem.lower() not in _AUX_STEMS
                   and not Path(f).stem.endswith("_test"))
    aux = sorted(f for f in all_files if f not in buggy)

    # 行级口径：correct 目录存在且是目录才启用，否则退化为文件级并标注
    correct_dir = Path(args.correct_dir) if args.correct_dir else None
    line_level = correct_dir is not None and correct_dir.is_dir()
    if args.correct_dir and not line_level:
        print(f"⚠️ correct 目录不存在：{correct_dir}，行级召回退化为文件级口径\n")

    # 文件级命中
    hit_file = [f for f in buggy if f in by_file]
    missed_file = [f for f in buggy if f not in by_file]

    # 行级命中
    hit_line: list[str] = []
    missed_line: list[str] = []
    degraded: list[str] = []  # correct 缺该文件 → 行级无法判定
    if line_level:
        for f in buggy:
            if f not in by_file:
                missed_line.append(f)
                continue
            correct_path = correct_dir / f
            if not correct_path.is_file():
                degraded.append(f)
                continue
            buggy_text = (target / f).read_text(encoding="utf-8", errors="replace")
            correct_text = correct_path.read_text(encoding="utf-8", errors="replace")
            if any_finding_hits(by_file[f], buggy_text, correct_text):
                hit_line.append(f)
            else:
                missed_line.append(f)

    print(f"=== quixbugs 审查评估：{target.name} ===")
    print(f"总文件 {len(all_files)} · buggy 程序 {len(buggy)} · 辅助文件 {len(aux)}")
    print(f"总 findings {len(findings)} 条 · 涉及文件 {len(by_file)} 个\n")

    file_recall = len(hit_file) / len(buggy) * 100 if buggy else 0
    print(f"文件级召回率 Recall(file)：{len(hit_file)}/{len(buggy)} = {file_recall:.1f}%")
    if missed_file:
        print(f"  文件级漏报：{missed_file}")

    if line_level:
        line_recall = len(hit_line) / len(buggy) * 100 if buggy else 0
        print(f"行级召回率 Recall(line)：{len(hit_line)}/{len(buggy)} = {line_recall:.1f}%")
        if missed_line:
            print(f"  行级漏报：{missed_line}")
        if degraded:
            print(f"  ⚠️ correct 目录缺这些文件、行级无法判定（未计入行级召回）：{degraded}")
    elif args.correct_dir:
        print("行级召回率 Recall(line)：N/A（correct 目录不存在，退化为文件级口径）")
    else:
        print("行级召回率 Recall(line)：N/A（未提供 --correct-dir，退化为文件级口径）")

    # 文件级精确率：报出 findings 的文件里，有多少是真 buggy 文件
    reported = set(by_file)
    precision = len([f for f in reported if f in buggy]) / len(reported) * 100 if reported else 0
    false_pos = [(f, len(by_file[f])) for f in aux if f in by_file]
    print(f"文件级精确率 Precision：{precision:.1f}%")
    if false_pos:
        print(f"  误报（辅助文件）：{false_pos}")

    print(f"\n--- 逐文件 ---")
    for f in buggy:
        n = len(by_file.get(f, []))
        file_mark = "命中" if n else "漏报"
        if line_level:
            line_mark = ("命中" if f in hit_line
                         else ("漏报" if f in missed_line else "未判定"))
            print(f"  {f}: findings={n} 文件级={file_mark} 行级={line_mark}")
        else:
            print(f"  {f}: findings={n} 文件级={file_mark}")
    for f in aux:
        n = len(by_file.get(f, []))
        if n:
            print(f"  {f}: findings={n} 误报（辅助文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
