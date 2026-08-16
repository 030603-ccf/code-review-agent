"""Thread-safe OpenAI-compatible LLM client.

Design points:
- Secrets come from environment variables (api_key_env), never from the repo.
- httpx enforces a *real* per-request timeout (it aborts the socket). There is
  deliberately no "node-level wall-clock timeout": Python cannot kill a running
  thread, so any such wrapper would be fake. The retry budget below is the only
  additional layer.
- Token/request counters are lock-guarded so parallel callers report exact
  totals.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml


class LLMError(Exception):
    """A non-retryable API error, carrying the HTTP status when available."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 response: httpx.Response | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class RateLimitError(LLMError):
    """HTTP 429. Subclassed so error classifiers can key on the type name."""


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    context_length: int = 128000
    timeout: float = 120.0
    rpm: int = 0
    extra_body: dict | None = None
    name: str = ""


class RateGate:
    """Throttle requests to at most `interval_sec` apart (thread-safe)."""

    def __init__(self, interval_sec: float):
        self._interval = interval_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._last + self._interval - now
                if wait <= 0:
                    self._last = now
                    return
            time.sleep(wait)


class LLMClient:
    """Single entry point for all model calls. Thread-safe for requests."""

    def __init__(self, config: LLMConfig, max_retries: int = 3):
        self.config = config
        self.max_retries = max_retries
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.Client(
            base_url=config.base_url, headers=headers, timeout=config.timeout)
        self._lock = threading.Lock()
        self._total_tokens = 0
        self._total_requests = 0
        self._gate = RateGate(60.0 / config.rpm) if config.rpm > 0 else None

    @classmethod
    def from_config(cls, path: str | Path, profile: str | None = None) -> "LLMClient":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        name = profile or data["default_profile"]
        raw = data["profiles"][name]

        # api_key_env names an env var; the key itself never lives in the file.
        env_name = raw.get("api_key_env")
        api_key = os.environ.get(env_name, "") if env_name else raw.get("api_key", "")

        known = set(LLMConfig.__dataclass_fields__) - {"api_key"}
        cfg = LLMConfig(name=name, api_key=api_key,
                        **{k: v for k, v in raw.items() if k in known})
        return cls(cfg)

    def chat(self, messages: list[dict], extra_body: dict | None = None,
             **overrides) -> str:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": overrides.get("temperature", self.config.temperature),
            "max_tokens": overrides.get("max_tokens", self.config.max_tokens),
        }
        if "response_format" in overrides:
            payload["response_format"] = overrides["response_format"]
        merged = extra_body if extra_body is not None else self.config.extra_body
        if merged:
            payload.update(merged)

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if self._gate is not None:
                    self._gate.acquire()
                resp = self._client.post("/chat/completions", json=payload)
                if resp.status_code == 429:
                    last_err = RateLimitError(
                        f"HTTP 429: {resp.text[:200]}", status_code=429, response=resp)
                    if attempt < self.max_retries - 1:
                        time.sleep(0.5 * 2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    last_err = LLMError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}",
                        status_code=resp.status_code, response=resp)
                    if attempt < self.max_retries - 1:
                        time.sleep(0.5 * 2 ** attempt)
                    continue
                data = resp.json()
                usage = data.get("usage") or {}
                tokens = usage.get("total_tokens") or (
                    usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                with self._lock:
                    self._total_tokens += tokens
                    self._total_requests += 1
                return data["choices"][0]["message"]["content"]
            except (httpx.HTTPError, LLMError) as e:  # httpx timeouts included
                last_err = e
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * 2 ** attempt)
                continue

        # Re-raise the *original* error type so callers can classify it
        # (e.g. httpx.TimeoutException stays transient, not a generic wrapper).
        raise last_err if last_err is not None else LLMError("chat failed")

    @property
    def total_tokens_used(self) -> int:
        return self._total_tokens

    @property
    def total_requests(self) -> int:
        return self._total_requests

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
