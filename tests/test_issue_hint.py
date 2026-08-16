"""端到端验证 issue 线索：审查 --issue-hint 注入 prompt，优化 render_fix_prompt
与修复缓存键纳入 issue_hint。全程 FakeClient，零网络零 token。
"""

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from lra.graph import build_graph
from lra.optimizer.loop import fix_cache_key, render_fix_prompt
from lra.schemas.finding import Finding


class CaptureClient:
    """审查用假 LLM client：chat 返回固定 JSON，并捕获每次调用的 messages。"""

    class config:
        model = "fake-model"
        context_length = 8192

    total_tokens_used = 0

    def __init__(self):
        self.total_requests = 0
        self.captured = []  # list[list[dict]]，每条是完整 messages

    def chat(self, messages, **kw):
        self.total_requests += 1
        self.captured.append(messages)
        path = messages[-1]["content"].splitlines()[0].removeprefix("文件路径：")
        return json.dumps({"findings": [{
            "id": "F1", "category": "security", "severity": "high",
            "file_path": path, "line_start": 1, "line_end": 1,
            "title": "可能的除零", "description": "d",
            "evidence": "value = 100 / 0", "suggestion": "s",
            "confidence": 0.9}]}, ensure_ascii=False)

    def all_content(self) -> str:
        return "\n".join(m["content"] for msgs in self.captured for m in msgs)


def _finding(fid="F1", file_path="src/a.py"):
    return Finding(
        id=fid, category="best_practice", severity="high",
        file_path=file_path, line_start=1, line_end=3,
        title="unused import", description="import os is unused",
        evidence="import os", suggestion="remove it", confidence=0.9,
    )


@pytest.fixture
def mini_project(tmp_path):
    (tmp_path / "a.py").write_text(
        "# comment\nvalue = 100 / 0\n", encoding="utf-8")
    return tmp_path


def _run_review(client, root, run_dir, issue_hint=None, thread_id="t-issue"):
    config = {"configurable": {"thread_id": thread_id}, "max_concurrency": 3}
    initial = {"root": str(root), "run_dir": str(run_dir),
               "second_client_enabled": False}
    if issue_hint:
        initial["issue_hint"] = issue_hint
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(client, None).compile(checkpointer=saver)
        return graph.invoke(initial, config)


# ---------- 审查：--issue-hint 注入 prompt ----------

def test_review_issue_hint_injected_into_prompt(mini_project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = CaptureClient()
    hint = "CVE-2024-1234：登录接口疑似存在 SQL 注入"

    _run_review(client, mini_project, run_dir,
                issue_hint=hint, thread_id="t-hint")

    content = client.all_content()
    assert "【用户线索】" in content
    assert hint in content


def test_review_without_issue_hint_prompt_clean(mini_project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = CaptureClient()

    _run_review(client, mini_project, run_dir,
                issue_hint=None, thread_id="t-nohint")

    content = client.all_content()
    assert "【用户线索】" not in content


# ---------- 优化：render_fix_prompt 注入用户线索 ----------

def test_render_fix_prompt_with_issue_hint_has_clue_section():
    hint = "CVE-2024-5678：拼 SQL 可被注入"
    prompt = render_fix_prompt("src/a.py", [_finding()], "import os\n",
                               issue_hint=hint)
    assert "【用户线索】" in prompt
    assert hint in prompt
    # 用户线索段落位于问题清单之前
    assert prompt.index("【用户线索】") < prompt.index("## 问题清单")


def test_render_fix_prompt_without_issue_hint_no_clue_section():
    prompt = render_fix_prompt("src/a.py", [_finding()], "import os\n")
    assert "【用户线索】" not in prompt


# ---------- 优化：fix_cache_key 纳入 issue_hint ----------

def test_fix_cache_key_differs_by_issue_hint():
    k1 = fix_cache_key(["F1"], "sha1abc", "api", "model-a", issue_hint="hint-a")
    k2 = fix_cache_key(["F1"], "sha1abc", "api", "model-a", issue_hint="hint-b")
    k3 = fix_cache_key(["F1"], "sha1abc", "api", "model-a", issue_hint="")
    assert k1 != k2
    assert k1 != k3
    assert k2 != k3
    # 相同线索 → 相同键（确定性，缓存可复用）
    assert fix_cache_key(["F1"], "sha1abc", "api", "model-a",
                         issue_hint="hint-a") == k1
