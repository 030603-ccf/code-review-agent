"""错题本 tests: reviewer prompt injection + second-review rejected writes."""

import json

from lra.agents.reviewer import review_chunk
from lra.agents.second_reviewer import load_mistakes_text, second_review
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


# --- second_review writes rejected to jsonl ----------------------------------


def _mk(fid, title):
    return Finding(id=fid, category="security", severity="high",
                   file_path="a.py", line_start=1, line_end=1, title=title,
                   description="d", evidence="e", suggestion="s", confidence=0.9)


def test_second_review_writes_rejected_to_jsonl(tmp_path):
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
        {"finding_id": "F2", "verdict": "confirmed", "reason": "成立"},
    ]}, ensure_ascii=False))
    out = second_review([_mk("F1", "除零"), _mk("F2", "SQL")], tmp_path, judge,
                        mistakes_path=tmp_path / "mistakes.jsonl")

    lines = (tmp_path / "mistakes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 只有 rejected 进错题本
    rec = json.loads(lines[0])
    assert rec["title"] == "除零"
    assert rec["reason"] == "误报"
    assert rec["category"] == "security"
    assert rec["file"] == "a.py"
    assert rec["evidence"] == "e"
    assert out[1].second_verdict == "confirmed"


def test_second_review_no_rejected_writes_nothing(tmp_path):
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "confirmed", "reason": "成立"},
    ]}, ensure_ascii=False))
    second_review([_mk("F1", "除零")], tmp_path, judge,
                  mistakes_path=tmp_path / "mistakes.jsonl")
    assert not (tmp_path / "mistakes.jsonl").exists()


def test_second_review_save_path_defaults_mistakes_beside(tmp_path):
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
    ]}, ensure_ascii=False))
    save = tmp_path / "sub" / "findings.json"
    save.parent.mkdir()
    second_review([_mk("F1", "除零")], tmp_path, judge, save_path=save)
    assert (tmp_path / "sub" / "mistakes.jsonl").is_file()


def test_second_review_append_accumulates(tmp_path):
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
    ]}, ensure_ascii=False))
    path = tmp_path / "mistakes.jsonl"
    path.write_text('{"title": "旧误报", "reason": "r", "category": "x", '
                    '"file": "f", "evidence": "e"}\n', encoding="utf-8")
    second_review([_mk("F1", "新误报")], tmp_path, judge, mistakes_path=path)
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

def test_second_review_dedup_existing_title(tmp_path):
    """相同 title 已在错题本里 → 不再重复写。"""
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
    ]}, ensure_ascii=False))
    path = tmp_path / "mistakes.jsonl"
    path.write_text(json.dumps({"title": "除零", "reason": "旧理由",
                                "category": "security", "file": "a.py",
                                "evidence": "e"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    second_review([_mk("F1", "除零")], tmp_path, judge, mistakes_path=path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 没追加


def test_second_review_dedup_within_batch(tmp_path):
    """同一次调用里两个相同 title 的 rejected 只写一条。"""
    judge = FakeJudge(json.dumps({"verdicts": [
        {"finding_id": "F1", "verdict": "rejected", "reason": "误报"},
        {"finding_id": "F2", "verdict": "rejected", "reason": "还是误报"},
    ]}, ensure_ascii=False))
    path = tmp_path / "mistakes.jsonl"
    second_review([_mk("F1", "除零"), _mk("F2", "除零")], tmp_path, judge,
                  mistakes_path=path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["title"] == "除零"


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
