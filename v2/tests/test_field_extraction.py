"""Unit tests for prose -> findings extraction, the last-resort fallback
when the model answers in natural language instead of JSON."""

import pytest

from lra.llm.structured import (
    StructuredOutputError,
    chat_structured,
    extract_findings_from_text,
)
from lra.schemas.finding import FindingList


def _findings(text):
    out = extract_findings_from_text(text)
    assert out is not None
    return out["findings"]


# --- pure extraction ---------------------------------------------------------


def test_chinese_critical_with_line():
    f = _findings("发现1：第3行有SQL注入，严重度critical")[0]
    assert f["severity"] == "critical"
    assert f["line_start"] == 3 and f["line_end"] == 3
    assert f["file_path"] == ""
    assert f["category"] == "best_practice"  # no category keyword -> default
    assert f["confidence"] == 0.7
    assert f["evidence"] == ""  # never hard-extracted


def test_chinese_high_with_file_and_line_range():
    f = _findings("文件 db.py 第 10-11 行存在硬编码密码，级别高")[0]
    assert f["file_path"] == "db.py"
    assert f["line_start"] == 10 and f["line_end"] == 11
    assert f["severity"] == "high"


def test_english_extraction():
    f = _findings(
        "Finding 1: file src/auth.py line 42 has a security issue, "
        "severity high")[0]
    assert f["category"] == "security"
    assert f["severity"] == "high"
    assert f["file_path"] == "src/auth.py"
    assert f["line_start"] == 42 and f["line_end"] == 42


def test_english_line_range():
    f = _findings(
        "file auth.py lines 10-15, SQL injection, severity critical")[0]
    assert f["file_path"] == "auth.py"
    assert f["line_start"] == 10 and f["line_end"] == 15
    assert f["severity"] == "critical"


def test_block_unique_category_and_severity():
    fs = _findings(
        "发现1：文件 a.py 第 3 行存在安全问题，严重度critical。"
        "发现2：文件 b.py 第 5 行可读性差，级别低。")
    assert len(fs) == 2
    assert (fs[0]["file_path"], fs[0]["category"], fs[0]["severity"],
            fs[0]["line_start"]) == ("a.py", "security", "critical", 3)
    assert (fs[1]["file_path"], fs[1]["category"], fs[1]["severity"],
            fs[1]["line_start"]) == ("b.py", "readability", "low", 5)


def test_multiple_severities_in_block_default_to_medium():
    f = _findings("文件 db.py 第 10-11 行硬编码密码，严重度critical，级别高")[0]
    assert f["severity"] == "medium"


def test_multiple_categories_default_to_best_practice():
    f = _findings("文件 a.py 第 3 行问题影响性能和可读性，严重程度：中")[0]
    assert f["category"] == "best_practice"
    assert f["severity"] == "medium"


def test_file_mentioned_once_shared_by_findings():
    fs = _findings(
        "审查文件 db.py。发现1：第3行SQL注入，严重度critical。"
        "发现2：第5行超时，级别高。")
    assert len(fs) == 2
    assert fs[0]["file_path"] == "db.py" and fs[0]["line_start"] == 3
    assert fs[1]["file_path"] == "db.py" and fs[1]["line_start"] == 5


def test_marker_only_chunking_without_lines():
    fs = _findings(
        "发现1：严重度critical 的 SQL 注入风险。发现2：级别低 的可读性问题。")
    assert len(fs) == 2
    assert fs[0]["severity"] == "critical"
    assert fs[1]["severity"] == "low"
    assert fs[1]["category"] == "readability"


def test_no_keywords_returns_none():
    assert extract_findings_from_text("代码整体质量不错，可以合入。") is None
    assert extract_findings_from_text("请审查以下文件：a.py") is None
    assert extract_findings_from_text("") is None
    assert extract_findings_from_text("   ") is None


# --- chat_structured integration ---------------------------------------------


class _ProseClient:
    """Fake client that always answers in prose (the failure mode we fix)."""

    def __init__(self, text):
        self.text = text
        self.calls = 0
        self.last_overrides = None

    def chat(self, messages, **overrides):
        self.calls += 1
        self.last_overrides = overrides
        return self.text


def test_chat_structured_defaults_to_json_mode():
    client = _ProseClient('{"findings": []}')
    result = chat_structured(
        client, [{"role": "user", "content": "review"}], FindingList)
    assert client.last_overrides["response_format"] == {"type": "json_object"}
    assert result.findings == []


def test_chat_structured_falls_back_to_extraction():
    client = _ProseClient("发现1：第3行有SQL注入，严重度critical")
    result = chat_structured(
        client, [{"role": "user", "content": "review"}], FindingList,
        max_retries=1)
    assert client.calls == 1  # resolved without any LLM retry
    assert result.findings[0].severity == "critical"
    assert result.findings[0].line_start == 3
    assert result.findings[0].confidence == 0.7


def test_chat_structured_repairs_broken_json():
    client = _ProseClient('{"findings": [],}')  # trailing comma
    result = chat_structured(
        client, [{"role": "user", "content": "review"}], FindingList)
    assert result.findings == []


def test_chat_structured_retries_when_extraction_inapplicable():
    client = _ProseClient("代码整体质量不错，可以合入。")
    with pytest.raises(StructuredOutputError):
        chat_structured(
            client, [{"role": "user", "content": "review"}], FindingList,
            max_retries=1)
    assert client.calls == 2  # initial call + one feedback retry
