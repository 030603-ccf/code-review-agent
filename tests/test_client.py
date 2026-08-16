"""Unit tests for LLMClient.chat retry behavior on malformed responses.

A transient proxy glitch (non-JSON body, empty choices, missing keys) must be
retried inside chat() and, once retries are exhausted, re-raised with its
original type so classify_error can tag it transient — not permanent.
"""

import json
import time

import pytest

from lra.errors import TransientError, classify_error
from lra.llm.client import LLMClient, LLMConfig


class _BadJsonResponse:
    status_code = 200

    def json(self):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)


class _EmptyChoicesResponse:
    status_code = 200

    def json(self):
        return {"choices": [], "usage": {"total_tokens": 1}}


class _MissingContentResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {}}], "usage": {"total_tokens": 1}}


def _client_with(monkeypatch, response, max_retries=3):
    client = LLMClient(LLMConfig(base_url="http://x", model="m"),
                       max_retries=max_retries)
    calls = []

    def fake_post(url, json=None):
        calls.append(url)
        return response

    monkeypatch.setattr(client._client, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # no backoff delay
    return client, calls


@pytest.mark.parametrize("response, exc_type", [
    (_BadJsonResponse(), json.JSONDecodeError),
    (_EmptyChoicesResponse(), IndexError),
    (_MissingContentResponse(), KeyError),
])
def test_malformed_success_response_retried_as_transient(monkeypatch, response,
                                                         exc_type):
    client, calls = _client_with(monkeypatch, response, max_retries=3)
    with pytest.raises(exc_type) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 3  # retried to exhaustion, not raised on first hit
    assert isinstance(classify_error(excinfo.value), TransientError)


def test_valid_response_still_returns_content(monkeypatch):
    class _GoodResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 1},
            }

    client, calls = _client_with(monkeypatch, _GoodResponse(), max_retries=3)
    assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert len(calls) == 1
