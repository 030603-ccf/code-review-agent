"""分析 quixbugs buggy 版本的审查结果：召回率 + 准确率。"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import json
from pathlib import Path

run_dir = sys.argv[1] if len(sys.argv) > 1 else "runs/quixbugs_buggy2"
findings = json.loads(Path(f"{run_dir}/findings.json").read_text(encoding="utf-8"))

# quixbugs 每个程序恰好 1 个 bug（test 文件和 node.py 没有 bug）
# 已知有 bug 的文件（排除 _test.py 和 node.py）
all_files = sorted(set(
    p.stem for p in Path(r"E:\code-review-agent\targets\quixbugs\python_programs").glob("*.py")
))
buggy_files = sorted(f for f in all_files if not f.endswith("_test") and f != "node")
test_files = sorted(f for f in all_files if f.endswith("_test") or f == "node")

print(f"=== quixbugs buggy 审查结果分析 ===\n")
print(f"总文件数：{len(all_files)}")
print(f"有 bug 的程序：{len(buggy_files)} 个")
print(f"测试/辅助文件：{len(test_files)} 个")
print(f"审查失败（超时）：lis.py, minimum_spanning_tree.py")
print(f"\n总 findings：{len(findings)} 条")

# 按文件分组
by_file: dict[str, list] = {}
for f in findings:
    by_file.setdefault(f["file_path"], []).append(f)

print(f"涉及文件：{len(by_file)} 个\n")

# 召回率：有 bug 的文件里，多少被报了至少 1 条 finding
hit = 0
miss = 0
missed_files = []
for bf in buggy_files:
    fname = bf + ".py"
    if fname in by_file:
        hit += 1
    else:
        miss += 1
        missed_files.append(fname)

# 去掉超时失败的（不算漏报，算系统故障）
timeout_files = {"lis.py", "minimum_spanning_tree.py"}
effective_buggy = len(buggy_files) - len(timeout_files)
effective_hit = hit  # 超时的两个本来就没 findings

print(f"--- 召回率（Recall）---")
print(f"有效目标（排除超时）：{effective_buggy} 个")
print(f"命中（至少 1 条 finding）：{effective_hit} 个")
print(f"漏报：{effective_buggy - effective_hit} 个")
print(f"召回率：{effective_hit}/{effective_buggy} = {effective_hit/effective_buggy*100:.1f}%")
if missed_files:
    real_miss = [f for f in missed_files if f not in timeout_files]
    if real_miss:
        print(f"漏报文件：{real_miss}")
    print(f"超时未审：{sorted(timeout_files)}")

# 准确率：报出的 findings 里，多少确实指向了真正的 bug
# 这里简单统计：每个有 bug 文件只算 1 条为"命中真 bug"，多出的算"额外发现"
print(f"\n--- 精确率（Precision）---")
true_positive = 0  # 在有 bug 文件里报出的（至少算命中）
extra_in_buggy = 0  # 在有 bug 文件里多报的
false_positive = 0  # 在无 bug 文件里报出的

for fname, flist in by_file.items():
    stem = fname.replace(".py", "")
    if stem in buggy_files or fname in buggy_files:
        true_positive += 1
        extra_in_buggy += len(flist) - 1
    else:
        false_positive += len(flist)

total_findings = len(findings)
print(f"真阳性（命中有 bug 文件）：{true_positive} 条（每文件算 1 条）")
print(f"额外发现（有 bug 文件里多报的）：{extra_in_buggy} 条")
print(f"误报（在无 bug 文件里报的）：{false_positive} 条")
print(f"文件级精确率：{true_positive}/{len(by_file)} = {true_positive/len(by_file)*100:.1f}%")

# 详细列表
print(f"\n--- 逐文件详情 ---")
print(f"{'文件':<42} {'findings':>8}  {'状态'}")
print("-" * 70)
for bf in sorted(buggy_files):
    fname = bf + ".py"
    n = len(by_file.get(fname, []))
    if fname in timeout_files:
        status = "⏱️ 超时"
    elif n > 0:
        status = "✅ 命中"
    else:
        status = "❌ 漏报"
    print(f"  {fname:<40} {n:>5}    {status}")

print(f"\n--- 测试/辅助文件（本无 bug）---")
for tf in sorted(test_files):
    fname = tf + ".py"
    n = len(by_file.get(fname, []))
    if n > 0:
        print(f"  {fname:<40} {n:>5}    ⚠️ 误报")
