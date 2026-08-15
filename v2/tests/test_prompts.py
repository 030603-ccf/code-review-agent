"""Prompt loading tests: profile-specific variant + fallback."""

from lra.agents.reviewer import _build_system_prompt, review_chunk
from lra.llm.prompts import load_prompt

BASE_MARKER = "宁缺毋滥"          # reviewer.md
PROFILE_MARKER = "本地 vLLM 精简变体"  # reviewer.local_vllm.md


class FakeClient:
    class config:
        pass

    total_tokens_used = 0

    def __init__(self, name=""):
        self.config = type("Cfg", (), {"model": "fake",
                                       "context_length": 8192,
                                       "name": name})()
        self.last_system = ""

    def chat(self, messages, **kw):
        self.last_system = messages[0]["content"]
        return '{"findings": []}'


def _entry():
    return {"relpath": "a.py", "symbols": [], "imports": []}


def _chunk():
    return {"file": "a.py", "line_start": 1, "line_end": 2,
            "text": "1: x = 1\n2: y = 2\n"}


def test_load_prompt_base():
    text = load_prompt("reviewer")
    assert BASE_MARKER in text
    assert PROFILE_MARKER not in text


def test_load_prompt_profile_variant():
    text = load_prompt("reviewer", profile="local_vllm")
    assert PROFILE_MARKER in text


def test_load_prompt_missing_profile_falls_back():
    text = load_prompt("reviewer", profile="no_such_profile")
    assert BASE_MARKER in text
    assert PROFILE_MARKER not in text


def test_load_prompt_empty_profile_uses_base():
    assert BASE_MARKER in load_prompt("reviewer", profile="")
    assert BASE_MARKER in load_prompt("reviewer", profile=None)


def test_build_system_prompt_profile_selection():
    assert PROFILE_MARKER in _build_system_prompt("py", profile="local_vllm")
    assert BASE_MARKER in _build_system_prompt("py")
    assert BASE_MARKER in _build_system_prompt("py", profile="no_such_profile")


def test_review_chunk_uses_client_config_name():
    client = FakeClient(name="local_vllm")
    review_chunk(client, _entry(), _chunk())
    assert PROFILE_MARKER in client.last_system


def test_review_chunk_no_name_uses_base():
    client = FakeClient(name="")
    review_chunk(client, _entry(), _chunk())
    assert BASE_MARKER in client.last_system
    assert PROFILE_MARKER not in client.last_system
