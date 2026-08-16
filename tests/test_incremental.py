"""增量模式语义测试：parse_error / LSP 确定性诊断只注入变更文件。

aggregate 节点过去对 project_map 的全部文件注入 parse_error 与 LSP 诊断，
无视 diff_files，打破 --incremental 只审变更文件的承诺。这里验证：
- _incremental_filter 在增量模式下只保留变更文件、全量模式原样返回；
- Nodes.aggregate 在增量模式下 parse_error / LSP 只注入变更文件；
- 全量模式（diff_set 空）行为不变，全部文件照常注入。
"""

from lra.nodes import Nodes, _incremental_filter, _mode_filter


def _files():
    return [
        {"relpath": "changed.py", "parse_error": "bad (line 1)"},
        {"relpath": "unchanged.py", "parse_error": "also bad (line 1)"},
    ]


def _state(tmp_path, diff_files):
    (tmp_path / "changed.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "unchanged.py").write_text("y = 2\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / "t"
    run_dir.mkdir(parents=True)
    return {
        "root": str(tmp_path),
        "run_dir": str(run_dir),
        "findings": [],
        "diff_files": diff_files,
        "project_map": {"files": _files()},
    }


# --- _incremental_filter -------------------------------------------------------


def test_incremental_filter_keeps_only_diff_files():
    files = _files()
    out = _incremental_filter(files, {"changed.py"})
    assert [f["relpath"] for f in out] == ["changed.py"]


def test_incremental_filter_empty_diff_set_returns_all():
    files = _files()
    assert _incremental_filter(files, set()) is files  # 原样返回，行为不变


# --- aggregate: parse_error 注入 ----------------------------------------------


def test_aggregate_incremental_parse_error_only_diff_files(tmp_path):
    state = _state(tmp_path, diff_files=["changed.py"])
    out = Nodes(None).aggregate(state)
    paths = {f["file_path"] for f in out["aggregated"]}
    assert paths == {"changed.py"}


def test_aggregate_full_mode_parse_error_injects_all(tmp_path):
    """diff_files 为空（全量模式）→ 两个文件的 parse_error 都注入。"""
    state = _state(tmp_path, diff_files=[])
    out = Nodes(None).aggregate(state)
    paths = {f["file_path"] for f in out["aggregated"]}
    assert paths == {"changed.py", "unchanged.py"}


# --- aggregate: LSP 注入 --------------------------------------------------------


def test_aggregate_incremental_lsp_only_diff_files(tmp_path, monkeypatch):
    state = _state(tmp_path, diff_files=["changed.py"])
    # 清空 parse_error，避免与 LSP 测试交叉；只验证 LSP 收到的文件列表。
    state["project_map"]["files"] = [
        {"relpath": "changed.py", "parse_error": None},
        {"relpath": "unchanged.py", "parse_error": None},
    ]
    received = {}

    def fake_lsp_findings(root, files, lsp_cfg):
        received["files"] = [f["relpath"] for f in files]
        return []

    monkeypatch.setattr("lra.nodes.lsp_findings", fake_lsp_findings)
    Nodes(None, lsp_cfg={"enabled": True}).aggregate(state)
    assert received["files"] == ["changed.py"]


def test_aggregate_full_mode_lsp_injects_all(tmp_path, monkeypatch):
    state = _state(tmp_path, diff_files=[])
    state["project_map"]["files"] = [
        {"relpath": "changed.py", "parse_error": None},
        {"relpath": "unchanged.py", "parse_error": None},
    ]
    received = {}

    def fake_lsp_findings(root, files, lsp_cfg):
        received["files"] = [f["relpath"] for f in files]
        return []

    monkeypatch.setattr("lra.nodes.lsp_findings", fake_lsp_findings)
    Nodes(None, lsp_cfg={"enabled": True}).aggregate(state)
    assert received["files"] == ["changed.py", "unchanged.py"]


# --- strict 零变更：空 diff_files 不能退化回全量 ---------------------------------


def test_mode_filter_strict_empty_diff_returns_empty():
    files = _files()
    assert _mode_filter(files, [], incremental=True) == []
    assert _mode_filter(files, None, incremental=True) == []


def test_mode_filter_full_and_partial_semantics_unchanged():
    files = _files()
    # 旧语义：非增量 / 有变更文件时行为与 _incremental_filter 一致
    assert _mode_filter(files, [], incremental=False) is files
    assert [f["relpath"] for f in _mode_filter(
        files, ["changed.py"], incremental=True)] == ["changed.py"]
