"""Unit tests for the cross-file dependency graph."""

from lra.analysis.dep_graph import build_dep_graph, format_dep_context


def test_python_dep_graph():
    pm = {"files": [
        {"relpath": "main.py", "imports": ["from utils import parse_config"]},
        {"relpath": "utils.py", "imports": []},
    ]}
    g = build_dep_graph(pm)
    assert g["main.py"]["depends_on"] == ["utils.py"]
    assert g["utils.py"]["depended_by"] == ["main.py"]


def test_java_dep_graph_by_class_name():
    pm = {"files": [
        {"relpath": "BFS.java", "imports": []},
        {"relpath": "Node.java", "imports": []},
    ]}
    contents = {
        "BFS.java": "import Node;\n",
        "Node.java": "public class Node {}\n",
    }
    g = build_dep_graph(pm, contents)
    assert g["BFS.java"]["depends_on"] == ["Node.java"]


def test_format_dep_context_empty_when_isolated():
    pm = {"files": [{"relpath": "a.py", "imports": [], "symbols": []}]}
    g = build_dep_graph(pm)
    assert format_dep_context("a.py", g, pm) == ""


def test_format_dep_context_mentions_dependency():
    pm = {"files": [
        {"relpath": "main.py", "imports": ["from utils import parse_config"],
         "symbols": [{"kind": "function", "signature": "def main()", "name": "main"}]},
        {"relpath": "utils.py", "imports": [],
         "symbols": [{"kind": "function", "signature": "def parse_config(x)", "name": "parse_config"}]},
    ]}
    g = build_dep_graph(pm)
    ctx = format_dep_context("main.py", g, pm)
    assert "跨文件依赖" in ctx
    assert "utils.py" in ctx
    assert "parse_config" in ctx
