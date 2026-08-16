"""End-to-end graph tests with fake clients (no tokens, no network)."""

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from lra.graph import build_graph


class FakeClient:
    class config:
        model = "fake-model"
        context_length = 8192

    total_tokens_used = 0

    def __init__(self):
        self.total_requests = 0

    def chat(self, messages, **kw):
        self.total_requests += 1
        path = messages[-1]["content"].splitlines()[0].removeprefix("文件路径：")
        return json.dumps({"findings": [{
            "id": "F1", "category": "security", "severity": "high",
            "file_path": path, "line_start": 1, "line_end": 1,
            "title": "可能的除零", "description": "d",
            "evidence": "value = 100 / 0", "suggestion": "s",
            "confidence": 0.9}]}, ensure_ascii=False)


class FakeJudge:
    class config:
        model = "fake-judge"
        context_length = 8192

    total_tokens_used = 0

    def __init__(self, verdicts):
        self.reply = json.dumps({"verdicts": verdicts}, ensure_ascii=False)
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return self.reply


@pytest.fixture
def mini_project(tmp_path):
    (tmp_path / "a.py").write_text(
        "# comment\nvalue = 100 / 0\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "# comment\nvalue = 100 / 0\n", encoding="utf-8")
    return tmp_path


def _run(client, judge, root, run_dir, thread_id="t-e2e"):
    config = {"configurable": {"thread_id": thread_id}, "max_concurrency": 3}
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(client, judge).compile(checkpointer=saver)
        return graph.invoke(
            {"root": str(root), "run_dir": str(run_dir),
             "second_client_enabled": judge is not None}, config)


def test_end_to_end_with_second_review(mini_project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    judge = FakeJudge([
        {"finding_id": "F1", "verdict": "confirmed", "reason": "成立"},
        {"finding_id": "F2", "verdict": "rejected", "reason": "示例"},
    ])

    _run(client, judge, mini_project, run_dir)

    assert (run_dir / "project_map.json").exists()
    assert (run_dir / "findings.json").exists()
    assert (run_dir / "report.md").exists()
    assert (run_dir / "checkpoints.sqlite").exists()

    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert len(saved) == 2
    by_file = {d["file_path"]: d for d in saved}
    # evidence "value = 100 / 0" is on line 2; aggregator corrects the reported 1-1
    assert (by_file["a.py"]["line_start"], by_file["a.py"]["line_end"]) == (2, 2)
    assert by_file["a.py"]["second_verdict"] == "confirmed"
    assert by_file["b.py"]["second_verdict"] == "rejected"
    assert client.total_requests == 2
    assert judge.calls == 2


def test_conditional_edge_skips_second_review(mini_project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    _run(client, None, mini_project, run_dir, thread_id="t-nosecond")

    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "二级审查" not in md
    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert all(d["second_verdict"] is None for d in saved)


def test_syntax_error_file_yields_parse_finding(tmp_path):
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    _run(client, None, tmp_path, run_dir, thread_id="t-parse")

    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    parse = [d for d in saved if d["title"] == "语法解析失败"]
    assert len(parse) == 1
    f = parse[0]
    assert f["category"] == "correctness"
    assert f["severity"] == "critical"
    assert f["file_path"] == "bad.py"
    assert f["line_start"] == f["line_end"] >= 1
    assert f["description"]  # 解析错误原文非空
    # 语法错误文件不进 LLM（零请求）
    assert client.total_requests == 0


def test_resume_same_thread_id_no_extra_requests(mini_project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    judge = FakeJudge([])
    _run(client, judge, mini_project, run_dir, thread_id="t-resume")
    assert client.total_requests == 2

    # re-run same thread: already finished, no new requests
    config = {"configurable": {"thread_id": "t-resume"}}
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(client, judge).compile(checkpointer=saver)
        snap = graph.get_state(config)
        assert snap.values and not snap.next
        graph.invoke(None, config)
    assert client.total_requests == 2
