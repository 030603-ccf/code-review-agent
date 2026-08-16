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


def _run(client, judge, root, run_dir, thread_id="t-e2e", extra_state=None):
    config = {"configurable": {"thread_id": thread_id}, "max_concurrency": 3}
    state = {"root": str(root), "run_dir": str(run_dir),
             "second_client_enabled": judge is not None}
    if extra_state:
        state.update(extra_state)
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(client, judge).compile(checkpointer=saver)
        return graph.invoke(state, config)


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


def test_retry_failed_keeps_block_when_still_failing(tmp_path):
    """补跑仍失败时，失败块必须留在账本（不标 resolved），否则 --retry-failed
    在下次 run 看不到任何失败块、永远无法倒带补跑。"""
    from lra.nodes import Nodes

    class BoomClient:
        class config:
            model = "boom-model"
            name = ""
            context_length = 8192

        total_tokens_used = 0

        def chat(self, messages, **kw):
            raise RuntimeError("boom")

    nodes = Nodes(BoomClient())
    out = nodes.retry_failed({
        "chunk": {"file": "a.py", "line_start": 1, "line_end": 2,
                  "text": "1: x = 1\n2: y = 2\n"},
        "entry": {"relpath": "a.py", "symbols": [], "imports": [],
                  "sha1": "abc123"},
        "run_dir": str(tmp_path),
        "mistakes_text": "",
        "round": 0,
    })

    assert out["retry_round"] == 1
    assert out["findings"] == []
    blocks = out["failed_blocks"]
    assert len(blocks) == 1
    # 关键：补跑失败不标 resolved，块仍保留，供下次 --retry-failed 再试
    assert blocks[0].get("resolved") is not True
    assert blocks[0]["chunk"]["line_start"] == 1
    assert blocks[0]["error"]
    # 永久错误同时进入 llm_errors，summary.json 借此区分瞬时/永久失败
    assert len(out["llm_errors"]) == 1
    assert out["llm_errors"][0]["chunk"]["line_start"] == 1


def test_retry_failed_rewind_reruns_failed_blocks(mini_project, tmp_path):
    """--retry-failed 倒带补跑：已完成 run 遗留的失败块能被重新补跑。

    覆盖两个根因：(1) retry_round 用 overwrite reducer，倒带写回 0 不会被
    旧 max reducer 卡在 1；(2) 遗留失败块（不标 resolved）在 rewind 后仍可被
    fan_out_failed 路由到 retry_failed。
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    config = {"configurable": {"thread_id": "t-retry"}, "max_concurrency": 1}

    failed_block = {
        "entry": {"relpath": "a.py", "sha1": "x", "symbols": [], "imports": []},
        "chunk": {"file": "a.py", "line_start": 1, "line_end": 2,
                  "text": "1: x = 1\n2: y = 2\n"},
        "error": "TimeoutError: flaky",
    }

    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(client, None).compile(checkpointer=saver)
        # 模拟一次已跑完但遗留失败块的 run：retry_round=1 + failed_blocks 非空
        graph.invoke({
            "root": str(mini_project), "run_dir": str(run_dir),
            "second_client_enabled": False,
            "retry_round": 1,
            "failed_blocks": [failed_block],
        }, config)
        snap = graph.get_state(config)
        assert snap.values.get("failed_blocks")
        after_first = client.total_requests
        assert after_first >= 1

        # 倒带补跑（等价于 cmd_review 里的 --retry-failed 分支）
        graph.update_state(config, {"retry_round": 0}, as_node="aggregate")
        graph.invoke(None, config)

        snap = graph.get_state(config)
        assert not snap.values.get("failed_blocks")
        # 只补跑了一个失败块（多出一次 LLM 调用）
        assert client.total_requests == after_first + 1


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


def test_strict_incremental_zero_changes_reviews_nothing(mini_project, tmp_path):
    """strict 零变更（incremental=True + diff_files=[]）必须零 LLM 请求、零发现。

    空 diff_files 不能再走"空=全量"的旧语义，否则 --incremental-strict 会
    静默退化成一次全量审查。
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()

    result = _run(client, None, mini_project, run_dir, thread_id="t-strict-empty",
                  extra_state={"incremental": True, "diff_files": [],
                               "review_mode": "incremental"})

    assert client.total_requests == 0
    assert result.get("aggregated") == []
    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert saved == []
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "未发现问题" in md
