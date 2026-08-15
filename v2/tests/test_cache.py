"""FindingCache unit tests + end-to-end cache-hit graph test.

The graph test runs build_graph twice with different thread_ids and a fake
LLM client; the second run must serve every chunk from the sha1 cache
(zero LLM requests).
"""

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from lra.cache import FindingCache
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


def _findings():
    return [{"id": "F1", "category": "security", "severity": "high",
             "file_path": "a.py", "line_start": 1, "line_end": 1,
             "title": "t", "description": "d", "evidence": "x",
             "suggestion": "s", "confidence": 0.9}]


def test_put_get_roundtrip(tmp_path):
    cache = FindingCache(tmp_path / "c.json")
    assert cache.get("a.py", "s1", 1, 10) is None
    cache.put("a.py", "s1", 1, 10, _findings())
    assert cache.get("a.py", "s1", 1, 10) == _findings()
    # 同文件不同行区间互不影响
    assert cache.get("a.py", "s1", 11, 20) is None
    # 同区间不同文件互不影响
    assert cache.get("b.py", "s1", 1, 10) is None


def test_none_path_disabled(tmp_path):
    cache = FindingCache(None)
    cache.put("a.py", "s1", 1, 10, _findings())
    assert cache.get("a.py", "s1", 1, 10) is None
    assert not (tmp_path / "c.json").exists()  # 禁用时 put 不写任何文件


def test_sha1_miss(tmp_path):
    cache = FindingCache(tmp_path / "c.json")
    cache.put("a.py", "s1", 1, 10, _findings())
    # 文件内容变了 → sha1 不同 → miss
    assert cache.get("a.py", "s2", 1, 10) is None


def test_cross_instance_persistence(tmp_path):
    path = tmp_path / "c.json"
    FindingCache(path).put("a.py", "s1", 1, 10, _findings())
    again = FindingCache(path)
    assert again.get("a.py", "s1", 1, 10) == _findings()
    # 无残留 .tmp 文件
    assert not path.with_name(path.name + ".tmp").exists()


def test_empty_findings_are_a_hit(tmp_path):
    cache = FindingCache(tmp_path / "c.json")
    cache.put("a.py", "s1", 1, 10, [])
    assert cache.get("a.py", "s1", 1, 10) == []  # [] 是命中，None 才是 miss


@pytest.fixture
def mini_project(tmp_path):
    (tmp_path / "a.py").write_text("# c\nvalue = 100 / 0\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# c\nvalue = 100 / 0\n", encoding="utf-8")
    return tmp_path


def _run(client, root, run_dir, cache, thread_id, context=""):
    config = {"configurable": {"thread_id": thread_id}, "max_concurrency": 3}
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(client, None, cache=cache,
                            context=context).compile(checkpointer=saver)
        return graph.invoke(
            {"root": str(root), "run_dir": str(run_dir),
             "second_client_enabled": False}, config)


def test_second_run_skips_llm(mini_project, tmp_path):
    cache = FindingCache(tmp_path / ".findings_cache.json")

    run1 = tmp_path / "run1"
    run1.mkdir()
    client1 = FakeClient()
    _run(client1, mini_project, run1, cache, thread_id="t-cache-1")
    assert client1.total_requests == 2  # a.py + b.py 各一块

    # 换 thread_id 重跑：文件未变 → 全部缓存命中 → 0 次 LLM
    run2 = tmp_path / "run2"
    run2.mkdir()
    client2 = FakeClient()
    _run(client2, mini_project, run2, cache, thread_id="t-cache-2")
    assert client2.total_requests == 0

    # 结果与首次一致（缓存 findings 的 evidence 被 aggregate 重新定位行号）
    saved1 = json.loads((run1 / "findings.json").read_text(encoding="utf-8"))
    saved2 = json.loads((run2 / "findings.json").read_text(encoding="utf-8"))
    assert len(saved1) == len(saved2) == 2
    assert saved1 == saved2


def test_changed_sha1_forces_llm_again(mini_project, tmp_path):
    cache = FindingCache(tmp_path / ".findings_cache.json")

    run1 = tmp_path / "run1"
    run1.mkdir()
    client1 = FakeClient()
    _run(client1, mini_project, run1, cache, thread_id="t-change-1")
    assert client1.total_requests == 2

    # 改一个文件：只有 b.py 命中缓存，a.py 重新走 LLM
    (mini_project / "a.py").write_text("# changed\nvalue = 1 / 0\n",
                                       encoding="utf-8")
    run2 = tmp_path / "run2"
    run2.mkdir()
    client2 = FakeClient()
    _run(client2, mini_project, run2, cache, thread_id="t-change-2")
    assert client2.total_requests == 1


def test_context_dimension_in_key(tmp_path):
    cache = FindingCache(tmp_path / "c.json")
    cache.put("a.py", "s1", 1, 10, _findings(), context="ctxA")
    assert cache.get("a.py", "s1", 1, 10, context="ctxA") == _findings()
    # 不同 context → 不同键 → miss
    assert cache.get("a.py", "s1", 1, 10, context="ctxB") is None
    # 缺省 context 也不命中（键里拼了 context 维度）
    assert cache.get("a.py", "s1", 1, 10) is None


def test_context_change_forces_llm_again(mini_project, tmp_path):
    cache = FindingCache(tmp_path / ".findings_cache.json")

    run1 = tmp_path / "run1"
    run1.mkdir()
    client1 = FakeClient()
    _run(client1, mini_project, run1, cache, "t-ctx-1", context="hint-a")
    assert client1.total_requests == 2

    # 换 context（如换 --issue-hint）→ 缓存整体失效 → 重跑 LLM
    run2 = tmp_path / "run2"
    run2.mkdir()
    client2 = FakeClient()
    _run(client2, mini_project, run2, cache, "t-ctx-2", context="hint-b")
    assert client2.total_requests == 2

    # 回到原 context → 命中缓存，0 次 LLM
    run3 = tmp_path / "run3"
    run3.mkdir()
    client3 = FakeClient()
    _run(client3, mini_project, run3, cache, "t-ctx-3", context="hint-a")
    assert client3.total_requests == 0


def test_put_throttles_disk_writes(tmp_path, monkeypatch):
    cache = FindingCache(tmp_path / "c.json")
    saves = {"n": 0}
    original = cache._save_locked

    def counting_save():
        saves["n"] += 1
        original()

    monkeypatch.setattr(cache, "_save_locked", counting_save)
    for i in range(50):
        cache.put("a.py", "s1", i, i, _findings())
    cache.flush()
    # 50 次 put 只有寥寥几次落盘（首次立即 + flush 兜底），远小于 put 次数
    assert 1 <= saves["n"] <= 3


def test_throttled_put_flushes_dirty_data(tmp_path):
    path = tmp_path / "c.json"
    cache = FindingCache(path)
    cache.put("a.py", "s1", 1, 1, _findings())  # 首次立即落盘
    cache.put("b.py", "s1", 1, 1, _findings())  # 节流窗口内只置 dirty
    cache.flush()                                # flush 兜底写盘
    again = FindingCache(path)
    assert again.get("a.py", "s1", 1, 1) == _findings()
    assert again.get("b.py", "s1", 1, 1) == _findings()
    # 无残留 .tmp 文件
    assert not path.with_name(path.name + ".tmp").exists()


def test_context_fingerprint_dimensions(tmp_path):
    from lra.__main__ import _context_fingerprint

    run_dir = tmp_path / "runs" / "t"
    run_dir.mkdir(parents=True)
    base = _context_fingerprint("", tmp_path, run_dir)

    # issue_hint 变化 → 指纹变化
    assert _context_fingerprint("hint", tmp_path, run_dir) != base
    # rules.json 内容变化 → 指纹变化
    (tmp_path / ".codereview").mkdir()
    (tmp_path / ".codereview" / "rules.json").write_text(
        '{"rules": []}', encoding="utf-8")
    assert _context_fingerprint("", tmp_path, run_dir) != base
    # 错题本内容变化 → 指纹变化
    (run_dir.parent / "memory").mkdir()
    (run_dir.parent / "memory" / "mistakes.jsonl").write_text(
        '{"title": "t", "reason": "r"}\n', encoding="utf-8")
    assert _context_fingerprint("", tmp_path, run_dir) != base
    # 相同输入 → 相同指纹（确定性，长度 16）
    fp = _context_fingerprint("", tmp_path, run_dir)
    assert fp == _context_fingerprint("", tmp_path, run_dir)
    assert len(fp) == 16
