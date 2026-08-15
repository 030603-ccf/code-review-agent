"""quixbugs 审查结果评估：召回率 + 精确率。

用法:
    python scripts/analyze.py <run_dir> <target_dir>

例:
    python scripts/analyze.py runs/quix-python targets/quixbugs/python_programs
    python scripts/analyze.py runs/quix-java targets/quixbugs/java_programs

quixbugs 每个 buggy 程序恰好 1 个已知 bug；辅助文件（python 的 *_test/node，
java 的 Node/WeightedEdge）本身无 bug，报了算误报。
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

# 各语言里「本身无 bug」的辅助文件 stem（小写）
_AUX_STEMS = {"_test", "node", "weightededge"}


def main() -> int:
    # UTF-8 stdout：避免 Windows GBK 控制台把 emoji/中文打成乱码或崩掉
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    if len(sys.argv) < 3:
        print("用法: python scripts/analyze.py <run_dir> <target_dir>")
        return 1

    run_dir = Path(sys.argv[1])
    target = Path(sys.argv[2])
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

    hit = [f for f in buggy if f in by_file]
    missed = [f for f in buggy if f not in by_file]
    false_pos = [(f, len(by_file[f])) for f in aux if f in by_file]

    print(f"=== quixbugs 审查评估：{target.name} ===")
    print(f"总文件 {len(all_files)} · buggy 程序 {len(buggy)} · 辅助文件 {len(aux)}")
    print(f"总 findings {len(findings)} 条 · 涉及文件 {len(by_file)} 个\n")

    recall = len(hit) / len(buggy) * 100 if buggy else 0
    print(f"召回率 Recall：{len(hit)}/{len(buggy)} = {recall:.1f}%")
    if missed:
        print(f"  漏报：{missed}")

    # 文件级精确率：报出 findings 的文件里，有多少是真 buggy 文件
    reported = set(by_file)
    precision = len([f for f in reported if f in buggy]) / len(reported) * 100 if reported else 0
    print(f"文件级精确率 Precision：{precision:.1f}%")
    if false_pos:
        print(f"  误报（辅助文件）：{false_pos}")

    print(f"\n--- 逐文件 ---")
    print(f"{'文件':<36}{'findings':>9}  状态")
    print("-" * 56)
    for f in buggy:
        n = len(by_file.get(f, []))
        print(f"  {f:<34}{n:>5}   {'✅ 命中' if n else '❌ 漏报'}")
    for f in aux:
        n = len(by_file.get(f, []))
        if n:
            print(f"  {f:<34}{n:>5}   ⚠️ 误报")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
