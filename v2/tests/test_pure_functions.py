"""Unit tests for pure functions: error classification, failed-block reducer,
language normalization, and chunking."""

import pytest
from pydantic import ValidationError

from lra.errors import PermanentError, TransientError, classify_error
from lra.nodes import _parse_error_findings, _parse_error_line
from lra.schemas.finding import Finding
from lra.state import _merge_failed
from lra.tools import normalize_lang
from lra.analysis.chunking import chunk_file


def test_classify_error_passthrough():
    t = TransientError("x")
    assert classify_error(t) is t
    p = PermanentError("x")
    assert classify_error(p) is p


def test_classify_error_builtin_timeout():
    assert isinstance(classify_error(TimeoutError("t")), TransientError)
    assert isinstance(classify_error(ConnectionError("c")), TransientError)


def test_classify_error_generic_is_permanent():
    assert isinstance(classify_error(ValueError("v")), PermanentError)


def test_merge_failed_dedupes_and_resolves():
    a = [{"entry": {"relpath": "x.py"}, "chunk": {"line_start": 1, "line_end": 3},
          "error": "boom"}]
    b = [{"entry": {"relpath": "x.py"}, "chunk": {"line_start": 1, "line_end": 3},
          "error": "boom"}]
    assert len(_merge_failed(a, b)) == 1  # dedupe

    resolved = [{"entry": {"relpath": "x.py"}, "chunk": {"line_start": 1, "line_end": 3},
                 "resolved": True}]
    assert _merge_failed(a, resolved) == []  # resolved removes the identity


def test_normalize_lang():
    assert normalize_lang("a.py", "") == "python"
    assert normalize_lang("A.java", "") == "java"
    assert normalize_lang("a.js", "") == "javascript"
    assert normalize_lang("a.ts", "") == "javascript"
    assert normalize_lang("a.txt", "") == ""


def test_chunk_small_file_one_chunk():
    entry = {"relpath": "x.py", "symbols": []}
    content = "a = 1\nb = 2\n"
    chunks = chunk_file(entry, content)
    assert len(chunks) == 1
    assert chunks[0]["line_start"] == 1 and chunks[0]["line_end"] == 2
    assert "1: a = 1" in chunks[0]["text"]


def test_chunk_large_file_multiple_chunks():
    entry = {"relpath": "x.py", "symbols": []}
    content = "\n".join(f"line_{i} = {i}" for i in range(1000))
    chunks = chunk_file(entry, content, max_chars=2000)
    assert len(chunks) > 1
    # chunks are contiguous and non-overlapping at the line level in window mode
    assert chunks[0]["line_start"] == 1


def _finding(category):
    return Finding(id="F1", category=category, severity="high",
                   file_path="a.py", line_start=1, line_end=1,
                   title="t", description="d", evidence="e",
                   suggestion="s", confidence=0.9)


def test_finding_accepts_correctness_category():
    assert _finding("correctness").category == "correctness"
    # 原有四类不受影响
    for c in ("security", "performance", "readability", "best_practice"):
        assert _finding(c).category == c


def test_finding_rejects_unknown_category():
    with pytest.raises(ValidationError):
        _finding("nonsense")


def test_parse_error_line_extracts_number():
    assert _parse_error_line("invalid syntax (line 3)") == 3
    assert _parse_error_line("expected ':' (line 12)") == 12


def test_parse_error_line_falls_back_to_one():
    assert _parse_error_line("unexpected EOF while parsing") == 1
    assert _parse_error_line("") == 1


def test_parse_error_findings_builds_correctness():
    files = [
        {"relpath": "bad.py", "parse_error": "invalid syntax (line 3)"},
        {"relpath": "ok.py", "parse_error": None},
    ]
    out = _parse_error_findings(files)
    assert len(out) == 1
    f = out[0]
    assert f.category == "correctness"
    assert f.severity == "critical"
    assert f.file_path == "bad.py"
    assert (f.line_start, f.line_end) == (3, 3)
    assert f.title == "语法解析失败"
    assert f.description == "invalid syntax (line 3)"
    assert f.evidence == ""
    assert f.confidence == 1.0
