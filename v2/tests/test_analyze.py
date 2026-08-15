"""analyze.py 行级召回判定逻辑测试。

analyze.py 是独立脚本（只 import 标准库，不 import lra）；这里只测纯函数：
bug_lines / overlaps / any_finding_hits。行级口径 = finding 的 line_start/line_end
覆盖 bug 行才算命中，不再用「文件有 finding」冒充精度。
"""

from scripts.analyze import any_finding_hits, bug_lines, overlaps


def test_bug_lines_replace_single_line():
    # buggy 第 1 行是 bug，correct 修掉了
    assert bug_lines("x = 1\n", "x = 2\n") == {1}


def test_bug_lines_replace_multi_line():
    assert bug_lines("a = 1\nb = 2\n", "a = 1\nb = 20\n") == {2}


def test_bug_lines_insert_missing_line():
    # buggy 缺了 b = 2 这一行（correct 多出一行）→ bug 位置是插入点（1-based）
    assert bug_lines("a = 1\nc = 3\n", "a = 1\nb = 2\nc = 3\n") == {2}


def test_bug_lines_delete_extra_line():
    assert bug_lines("a = 1\nb = 2\n", "a = 1\n") == {2}


def test_overlaps_boundary():
    bl = {5}
    assert overlaps(1, 5, bl) is True      # 区间末端覆盖 bug 行
    assert overlaps(5, 5, bl) is True      # 精确命中
    assert overlaps(6, 10, bl) is False    # 完全在 bug 行之后
    assert overlaps(1, 4, bl) is False     # 完全在 bug 行之前
    assert overlaps(None, None, bl) is False  # 缺行号退化到第 1 行，不覆盖 5


def test_bug_lines_ignores_trailing_docstring():
    # quixbugs 惯例：代码后附一段 module docstring（correct 版本还可能把 buggy
    # 代码嵌进去）。diff 应只比较真实代码，不被描述文字混淆。
    buggy = 'def f(x):\n    return x ^ 1\n\n\n"""\nInput: int\n"""\n'
    correct = 'def f(x):\n    return x & 1\n'
    assert bug_lines(buggy, correct) == {2}


def test_any_finding_hits_line_level():
    # 造两个文件：buggy 与 correct 只差第 2 行
    buggy = "a = 1\nb = 2\nc = 3\n"
    correct = "a = 1\nb = 20\nc = 3\n"

    # finding 覆盖 bug 行（第 2 行）→ 行级命中
    hit = {"line_start": 2, "line_end": 2}
    assert any_finding_hits([hit], buggy, correct) is True

    # finding 不覆盖 bug 行（第 3 行）→ 行级漏
    miss = {"line_start": 3, "line_end": 3}
    assert any_finding_hits([miss], buggy, correct) is False

    # 文件级有 finding 但行级不覆盖：区间覆盖范围跨过 bug 行也算命中
    span = {"line_start": 1, "line_end": 3}
    assert any_finding_hits([span], buggy, correct) is True

    # buggy 与 correct 完全相同 → 无 bug 行 → 不可能行级命中
    assert any_finding_hits([{"line_start": 1, "line_end": 3}], buggy, buggy) is False
