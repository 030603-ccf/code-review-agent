"""Unit tests for project-rule loading and glob-based prompt injection."""

import json

from lra.agents.rules import format_rules_injection, load_rules


def test_load_rules_missing_file_returns_empty(tmp_path):
    assert load_rules(tmp_path) == {}
    assert load_rules(tmp_path / "nope") == {}


def test_load_rules_list_shape(tmp_path):
    (tmp_path / ".codereview").mkdir()
    (tmp_path / ".codereview" / "rules.json").write_text(json.dumps({
        "rules": [
            {"glob": "**/*.py", "prompt": "检查 SQL 拼接", "category": "security"},
            {"glob": "src/**/*.ts", "prompt": "检查类型安全"},
        ]}), encoding="utf-8")
    rules = load_rules(tmp_path)
    assert rules == {"**/*.py": "检查 SQL 拼接", "src/**/*.ts": "检查类型安全"}


def test_load_rules_simplified_mapping(tmp_path):
    (tmp_path / ".codereview").mkdir()
    (tmp_path / ".codereview" / "rules.json").write_text(
        json.dumps({"**/*.py": "检查可变默认参数"}), encoding="utf-8")
    assert load_rules(tmp_path) == {"**/*.py": "检查可变默认参数"}


def test_load_rules_corrupt_file_returns_empty(tmp_path):
    (tmp_path / ".codereview").mkdir()
    (tmp_path / ".codereview" / "rules.json").write_text("{not json", encoding="utf-8")
    assert load_rules(tmp_path) == {}


def test_format_injection_glob_matching():
    rules = {"**/*.py": "py 规则", "src/**": "src 规则", "*.md": "md 规则"}
    text = format_rules_injection("a.py", rules)
    assert "py 规则" in text
    assert "src 规则" not in text
    assert "md 规则" not in text
    # 递归 glob 也应匹配顶层文件
    assert "py 规则" in format_rules_injection("top.py", rules)
    # 嵌套路径
    text = format_rules_injection("src/lib/x.ts", rules)
    assert "src 规则" in text
    assert "py 规则" not in text


def test_format_injection_no_match_returns_empty():
    assert format_rules_injection("a.py", {"*.md": "x"}) == ""
    assert format_rules_injection("a.py", {}) == ""


def test_format_injection_text_shape():
    text = format_rules_injection("a.py", {"**/*.py": "检查死循环"})
    assert text.startswith("【项目规则】")
    assert "- 检查死循环" in text
