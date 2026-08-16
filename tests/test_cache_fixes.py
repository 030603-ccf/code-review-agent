"""Regression tests for the four cache / second-review race bugs.

Covers:
  1. FindingCache._save_locked fixed .tmp name (cross-process race) + OSError swallow.
  2. Cache key includes the dep_context / _lsp_candidates dimensions.
  3. Permanent error must not write the degraded (tool-only/empty) result to cache.
  4. Second-review timeout thread mutates a deepcopy, not the originals.
"""

import json
import threading
import time
from pathlib import Path

import lra.nodes as nodes_mod
from lra.cache import FindingCache
from lra.nodes import Nodes, _chunk_cache_context
from lra.schemas.finding import Finding


def _findings():
    return [{"id": "F1", "category": "security", "severity": "high",
             "file_path": "a.py", "line_start": 1, "line_end": 1,
             "title": "t", "description": "d", "evidence": "x",
             "suggestion": "s", "confidence": 0.9}]


# ---------------------------------------------------------------------------
# Bug 1 — cache.py 固定 .tmp 文件名跨进程竞争
# ---------------------------------------------------------------------------

def test_save_uses_unique_tmp_name(tmp_path, monkeypatch):
    cache = FindingCache(tmp_path / "c.json")
    seen = []
    real_replace = Path.replace

    def fake_replace(self, target):
        seen.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)
    cache.put("a.py", "s1", 1, 10, _findings())

    assert len(seen) == 1
    name = seen[0]
    # 不再用固定 c.json.tmp（并发 run 会互相 replace 竞争）
    assert name != "c.json.tmp"
    assert name.endswith(".tmp")
    # 带 PID + 随机后缀，且藏在隐藏点前缀里
    assert ".c.json." in name
    # 无固定 .tmp 残留
    assert not (tmp_path / "c.json.tmp").exists()


def test_save_replace_oserror_is_silent(tmp_path, monkeypatch):
    cache = FindingCache(tmp_path / "c.json")

    def raise_replace(self, target):
        # 并发 run 已抢先 replace 掉 tmp → FileNotFoundError
        raise FileNotFoundError("tmp vanished (concurrent run)")

    monkeypatch.setattr(Path, "replace", raise_replace)
    # 首次 put 立即落盘 → 触发 _save_locked → replace 抛 OSError 应被静默吞掉
    cache.put("a.py", "s1", 1, 10, _findings())
    # 数据仍在内存里可用（replace 失败只影响落盘，不影响本次 run 的读）
    assert cache.get("a.py", "s1", 1, 10) == _findings()


# ---------------------------------------------------------------------------
# Bug 2 — 缓存键缺 dep_context / _lsp_candidates 维度
# ---------------------------------------------------------------------------

def test_chunk_cache_context_includes_dep_and_lsp():
    # 二者都为空 → 保持原 context（与旧键兼容，不整体失效）
    assert _chunk_cache_context("base", {}) == "base"
    assert _chunk_cache_context("base", {"_dep_context": "", "_lsp_candidates": ""}) == "base"

    a = _chunk_cache_context("base", {"_dep_context": "dep: a"})
    b = _chunk_cache_context("base", {"_dep_context": "dep: b"})
    assert a != "base" and b != "base" and a != b
    # 相同输入 → 相同结果（确定性）
    assert a == _chunk_cache_context("base", {"_dep_context": "dep: a"})

    # LSP 候选也进维度
    lsp = _chunk_cache_context("base", {"_lsp_candidates": "[行 1] warn"})
    assert lsp != "base"


class _ReviewClient:
    class config:
        model = "fake-model"
        context_length = 8192
        name = ""

    def __init__(self):
        self.total_requests = 0

    def chat(self, messages, **kw):
        self.total_requests += 1
        return json.dumps({"findings": [{
            "id": "F1", "category": "security", "severity": "high",
            "file_path": "a.py", "line_start": 1, "line_end": 1,
            "title": "t", "description": "d", "evidence": "x",
            "suggestion": "s", "confidence": 0.9}]}, ensure_ascii=False)


def _payload(dep="", lsp=""):
    entry = {"relpath": "a.py", "sha1": "s1", "symbols": [], "imports": []}
    if dep:
        entry["_dep_context"] = dep
    if lsp:
        entry["_lsp_candidates"] = lsp
    chunk = {"file": "a.py", "line_start": 1, "line_end": 2, "text": "x = 1\n"}
    return {"chunk": chunk, "entry": entry, "run_dir": ".",
            "issue_hint": "", "mistakes_text": ""}


def test_review_chunk_cache_key_includes_dep_and_lsp(tmp_path):
    cache = FindingCache(tmp_path / "c.json")
    client = _ReviewClient()
    nodes = Nodes(client, cache=cache)

    nodes.review_chunk(_payload(dep="dep: A"))
    assert client.total_requests == 1
    # 相同 dep → 缓存命中 → 不再调 LLM
    nodes.review_chunk(_payload(dep="dep: A"))
    assert client.total_requests == 1
    # dep 变了 → 缓存 miss → 重新审查
    nodes.review_chunk(_payload(dep="dep: B"))
    assert client.total_requests == 2
    # LSP 候选也进键维度 → 再 miss
    nodes.review_chunk(_payload(dep="dep: A", lsp="[行 1] warn"))
    assert client.total_requests == 3


# ---------------------------------------------------------------------------
# Bug 3 — 永久错误把降级结果写缓存
# ---------------------------------------------------------------------------

def test_permanent_error_not_cached(tmp_path):
    from lra.llm.structured import StructuredOutputError

    cache = FindingCache(tmp_path / "c.json")

    class FlakyClient:
        class config:
            model = "fake-model"
            context_length = 8192
            name = ""

        def __init__(self):
            self.total_requests = 0
            self.broken = True

        def chat(self, messages, **kw):
            self.total_requests += 1
            if self.broken:
                raise StructuredOutputError("bad json")
            return json.dumps({"findings": [{
                "id": "F1", "category": "security", "severity": "high",
                "file_path": "a.py", "line_start": 1, "line_end": 1,
                "title": "t", "description": "d", "evidence": "x",
                "suggestion": "s", "confidence": 0.9}]}, ensure_ascii=False)

    client = FlakyClient()
    nodes = Nodes(client, cache=cache)

    nodes.review_chunk(_payload())  # 永久错误
    assert client.total_requests == 1
    # 永久错误不得写缓存：该块下次仍会重新审查
    assert cache.get("a.py", "s1", 1, 2, "fake-model", context="") is None

    # 模型恢复正常后重跑 → 必须重新走 LLM（而非命中被污染的「工具发现」缓存）
    client.broken = False
    nodes.review_chunk(_payload())
    assert client.total_requests == 2
    # 成功才写缓存：再跑命中，0 新增请求
    nodes.review_chunk(_payload())
    assert client.total_requests == 2


# ---------------------------------------------------------------------------
# Bug 4 — 终审线程竞态（超时线程改写原对象）
# ---------------------------------------------------------------------------

def test_second_review_timeout_does_not_mutate_originals(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes_mod, "SECOND_REVIEW_TIMEOUT", 0.2)

    release = threading.Event()
    done = threading.Event()

    class SlowJudge:
        class config:
            model = "fake-judge"
            context_length = 8192
            name = ""

        def chat(self, messages, **kw):
            release.wait(timeout=30)  # 卡住 → 触发终审超时
            try:
                return json.dumps({"verdicts": [
                    {"finding_id": "F1", "verdict": "confirmed",
                     "reason": "late"}]}, ensure_ascii=False)
            finally:
                done.set()

    created = []

    class RecordingFinding(Finding):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    monkeypatch.setattr(nodes_mod, "Finding", RecordingFinding)

    finding = RecordingFinding(
        id="F1", category="security", severity="high",
        file_path="a.py", line_start=1, line_end=1,
        title="t", description="d", evidence="x",
        suggestion="s", confidence=0.9)
    state = {"run_dir": str(tmp_path), "root": str(tmp_path),
             "aggregated": [finding.model_dump(mode="json")]}

    nodes = Nodes(None, SlowJudge())
    try:
        out = nodes.second_review(state)
        # 超时 → 主线程把原对象标 uncertain
        assert out["aggregated"][0]["second_verdict"] == "uncertain"
    finally:
        release.set()  # 无论如何都放行慢线程，避免测试结束时线程悬挂

    assert done.wait(timeout=5), "慢终审线程未在预期时间内结束"
    # 给 chat_structured 解析 + do_second_review 改写副本留一点余量
    time.sleep(0.2)

    # second_review 内部构建的原对象（created[-1]）不得被超时线程改写：
    # worker 拿到的是深拷贝，改的是副本，原对象仍是主线程标的 uncertain。
    internal = created[-1]
    assert internal.second_verdict == "uncertain"
    assert internal.second_reason == "终审超时"
