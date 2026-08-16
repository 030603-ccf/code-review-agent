"""Error classification — distinguish transient (retryable) from permanent.

The reviewer node retries transient failures with exponential backoff and gives
up immediately on permanent ones (bad output that retrying cannot fix).
"""


import json


class LRAError(Exception):
    """Base class for lra's own errors."""


class TransientError(LRAError):
    """Retryable: network timeout, rate limit, service unavailable.

    retry_after_sec: suggested wait; None means use the default backoff.
    """

    def __init__(self, message: str, retry_after_sec: float | None = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class PermanentError(LRAError):
    """Not retryable: bad request, unrecoverable output."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


_RETRYABLE_STATUS = {402, 408, 429, 502, 503, 504}

_TRANSIENT_NAME_TOKENS = (
    "Timeout", "ConnectError", "RemoteProtocolError",
    "RateLimit", "ReadError", "WriteError",
)


def classify_error(exc: Exception) -> TransientError | PermanentError:
    """Tag a raw exception as transient or permanent."""
    if isinstance(exc, LRAError):
        return exc

    name = type(exc).__name__
    msg = str(exc)

    if any(t in name for t in _TRANSIENT_NAME_TOKENS):
        return TransientError(msg)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return TransientError(msg)
    # Malformed success responses from the LLM proxy (non-JSON body, empty
    # choices, missing keys) are transient glitches, not permanent failures.
    if isinstance(exc, (json.JSONDecodeError, IndexError, KeyError)):
        return TransientError(msg)
    if hasattr(exc, "response"):
        code = getattr(exc.response, "status_code", 0)
        if code in _RETRYABLE_STATUS:
            return TransientError(msg, retry_after_sec=_parse_retry_after(exc))

    return PermanentError(msg, cause=exc)


def _parse_retry_after(exc: Exception) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None
