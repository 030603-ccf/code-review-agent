"""错题本 tests: reviewer prompt injection + second-review rejected writes."""

import json
import threading
import time

from lra.agents.reviewer import review_chunk
from lra.agents.second_reviewer import (load_mistakes_text, second_review,
                                        write_mistakes)
from lra.nodes import Nodes
from lra.schemas.finding import Finding


class FakeClient:
    class config:
        model = "fake"
        context_length = 8192

    total_tokens_used = 0

    def __init__(self):
        self.last_user = ""

    def chat(self, messages, **kw):
        self.last_user = messages[-1]["content"]
        return '{"findings": []}'


class FakeJudge:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, messages, **kw):
        return self.reply


def _entry():
    return {"relpath": "a.py", "symbols": [], "imports": []}


def _chunk():
    return {"file": "a.py", "line_start": 1, "line_end": 2,
            "text": "1: x = 1\n2: y = 2\n"}


# --- reviewer injection ------------------------------------------------------


def test_reviewer_injects_mistakes_before_code_block():
    client = FakeClient()
    review_chunk(client, _entry(), _chunk(), mistakes_text="F1 误报（示例）")
    assert "【历史误报，请勿再犯】F1 误报（示例）" in client.last_user
    assert client.last_user.index("【历史误报") < client.last_user.index("```")


def test_reviewer_omits_mistakes_when_empty():
    client = FakeClient()
    review_chunk(client, _entry(), _chunk())
    assert "【历史误报" not in client.last_user
    assert "```py" in client.last_user


# --- write_mistakes writes rejected to jsonl ---------------------------------


def _mk(fid, title, file_path="a.py"):
    return Finding(id=fid, category="security", severity="high",
                   file_path=file_path, line_start=1, line_end=1, title=title,
                   description="d", evidence="e", suggestion="s", confidence=0.9)


def _rej(fid, title, file_path="a.py", reason="误报"):
    f = _mk(fid, title, file_path=file_path)
    f.second_verdict = "rejected"
    f.second_reason = reason
    return f


def test_write_mistakes_writes_rejected_to_jsonl(tmp_path):
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
        {"finding_id": "F2", "verdict": "confirmed", "reason": "成立"},
    ]}, ensure_ascii=False))
    out = second_review([_mk("F1", "除零"), _mk("F2", "SQL")], tmp_path, judge)
    write_mistakes(out, tmp_path / "mistakes.jsonl")

    lines = (tmp_path / "mistakes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 只有 rejected 进错题本
    rec = json.loads(lines[0])
    assert rec["title"] == "除零"
    assert rec["reason"] == "误报"
    assert rec["category"] == "security"
    assert rec["file"] == "a.py"
    assert rec["evidence"] == "e"
    assert out[1].second_verdict == "confirmed"


def test_write_mistakes_no_rejected_writes_nothing(tmp_path):
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "confirmed", "reason": "成立"},
    ]}, ensure_ascii=False))
    out = second_review([_mk("F1", "除零")], tmp_path, judge)
    write_mistakes(out, tmp_path / "mistakes.jsonl")
    assert not (tmp_path / "mistakes.jsonl").exists()


def test_second_review_no_longer_writes_mistakes_internally(tmp_path):
    """写错题本已从 second_review 移出：单独调用它不再产生任何 jsonl。"""
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
    ]}, ensure_ascii=False))
    save = tmp_path / "sub" / "findings.json"
    save.parent.mkdir()
    second_review([_mk("F1", "除零")], tmp_path, judge, save_path=save)
    assert not (tmp_path / "sub" / "mistakes.jsonl").exists()  # 不再 beside 默认落盘


def test_write_mistakes_append_accumulates(tmp_path):
    path = tmp_path / "mistakes.jsonl"
    path.write_text('{"title": "旧误报", "reason": "r", "category": "x", '
                    '"file": "f", "evidence": "e"}\n', encoding="utf-8")
    write_mistakes([_rej("F1", "新误报")], path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["title"] == "新误报"


# --- load_mistakes_text -------------------------------------------------------


def test_load_mistakes_text(tmp_path):
    path = tmp_path / "mistakes.jsonl"
    path.write_text(json.dumps({"title": "除零", "reason": "误报"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    text = load_mistakes_text(path)
    assert "除零" in text and "误报" in text
    assert load_mistakes_text(tmp_path / "nope.jsonl") == ""
    assert load_mistakes_text(None) == ""


# --- 去重 + 注入上限 ----------------------------------------------------------

def test_write_mistakes_dedup_existing_title(tmp_path):
    """相同 title 已在错题本里 → 不再重复写。"""
    path = tmp_path / "mistakes.jsonl"
    path.write_text(json.dumps({"title": "除零", "reason": "旧理由",
                                "category": "security", "file": "a.py",
                                "evidence": "e"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    write_mistakes([_rej("F1", "除零")], path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 没追加


def test_write_mistakes_dedup_within_batch(tmp_path):
    """同一次调用里两个相同 title 的 rejected 只写一条。"""
    path = tmp_path / "mistakes.jsonl"
    write_mistakes([_rej("F1", "除零"), _rej("F2", "除零")], path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["title"] == "除零"


def test_write_mistakes_keeps_same_title_in_different_files(tmp_path):
    """不同文件同 title 的 rejected 误报是不同样本，各自保留。"""
    path = tmp_path / "mistakes.jsonl"
    write_mistakes([_rej("F1", "硬编码密钥", file_path="a.py"),
                    _rej("F2", "硬编码密钥", file_path="b.py")], path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    recs = [json.loads(line) for line in lines]
    assert {r["file"] for r in recs} == {"a.py", "b.py"}
    assert all(r["title"] == "硬编码密钥" for r in recs)


def test_write_mistakes_dedup_same_file_title_across_runs(tmp_path):
    """同一文件同 title 跨 run 仍去重（旧记录 file 字段参与键）。"""
    path = tmp_path / "mistakes.jsonl"
    path.write_text(json.dumps({"title": "硬编码密钥", "reason": "旧理由",
                                "category": "security", "file": "b.py",
                                "evidence": "e"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    write_mistakes([_rej("F1", "硬编码密钥", file_path="b.py")], path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 同文件同 title，仍去重


def test_load_mistakes_text_limit_takes_last_n(tmp_path):
    """注入只取最近 N 条，跑得越多不越贵。"""
    path = tmp_path / "mistakes.jsonl"
    path.write_text("".join(
        json.dumps({"title": f"t{i}", "reason": f"r{i}"}, ensure_ascii=False) + "\n"
        for i in range(5)
    ), encoding="utf-8")

    text = load_mistakes_text(path, limit=2)
    assert "t3" in text and "t4" in text
    assert "t0" not in text and "t2" not in text

    # 不传 limit 仍全文注入（兼容旧行为）
    assert "t1" in load_mistakes_text(path)


# --- 超时线程不写错题本 --------------------------------------------------------


def test_second_review_timeout_thread_does_not_write_mistakes(tmp_path, monkeypatch):
    """超时线程继续后台跑完，但不再写错题本（写已移到主线程）。"""
    import lra.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "SECOND_REVIEW_TIMEOUT", 0.3)
    monkeypatch.setattr(nodes_mod, "SECOND_REVIEW_WORKERS", 2)
    # 动态超时项置 0，让 monkeypatch 的 0.3s 超时生效（否则会被 per-call 预算覆盖）
    monkeypatch.setattr(nodes_mod, "SECOND_REVIEW_PER_CALL_SECONDS", 0.0)

    finished = threading.Event()

    class SlowJudge:
        def chat(self, messages, **kw):
            time.sleep(0.8)  # 超过 0.3s 超时，模拟超时线程继续后台跑完
            finished.set()
            return json.dumps({"verdicts": [
                {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
            ]}, ensure_ascii=False)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mistakes_path = tmp_path / "memory" / "mistakes.jsonl"

    nodes = Nodes(client=None, second_client=SlowJudge())
    state = {"run_dir": str(run_dir), "root": str(tmp_path),
             "aggregated": [_mk("F1", "除零").model_dump(mode="json")]}

    nodes.second_review(state)

    # 主线程返回时超时线程仍在后台跑，错题本不应被它写
    assert not mistakes_path.exists()

    # 等超时线程真正跑完（其 second_review 已不再内部写错题本）
    assert finished.wait(timeout=5)
    assert not mistakes_path.exists()
