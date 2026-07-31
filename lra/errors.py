"""lra/errors.py —— 异常分类：不再把所有的失败一视同仁。

为什么需要它：
    原版 review_chunk 节点对任何异常都 `return []`——"单块失败不拖垮整个
    run"的韧性是对的，但把"网络抖了一下"（重试一次就好了）和"模型输出
    了不可修复的 JSON"（重试一百次也没用）混为一谈，白白浪费重试机会，
    或者反过来在真正能救的错误上直接放弃。

    这里把异常分成两类，由 classify_error() 给每个原始异常贴标签：
        TransientError   瞬时错误（可重试）——网络超时、速率限制、服务暂不可用
        PermanentError   永久错误（不可重试）——请求本身有问题、输出无法修复

    注意：这个分类器只服务**节点级**的容错决策（这一块失败要不要重试）。
    cra 的 LLMClient 自己已有内建的指数退避重试（0.5s/1s/2s，3 次），
    这里管的是它重试耗尽之后、节点层面是否再给一次机会。
"""


class LRAError(Exception):
    """所有 lra 自有异常的基类。"""


class TransientError(LRAError):
    """瞬时错误（可重试）——网络超时、速率限制、服务暂不可用。

    retry_after_sec: 建议等待秒数（None = 用默认指数退避）。
    """

    def __init__(self, message: str, retry_after_sec: float | None = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class PermanentError(LRAError):
    """永久错误（不可重试）——请求被拒绝、模型输出不可修复等。

    cause: 原始异常（想追溯根因时用）。
    """

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


# HTTP 状态码中"服务暂时不行，等会儿再试"的一类
_RETRYABLE_STATUS_CODES = {408, 429, 502, 503, 504}


def classify_error(exc: Exception) -> TransientError | PermanentError:
    """把原始异常归类为 TransientError 或 PermanentError。

    判据（从具体到一般）：
      0. 已经是 lra 自己的分类异常 → 原样返回（分类器不是"二次加工厂"）
      1. 异常类型名/消息里带瞬时特征（Timeout / ConnectError / RateLimit...）
      2. 内建网络异常（ConnectionError / TimeoutError）
      3. 异常携带 HTTP response，状态码在可重试集合里
      4. 其余一律视为永久错误
    """
    # 第 0 条：自己的分类异常原样放行。
    # 否则 classify_error(TransientError("...")) 会因为没有 response 属性、
    # 类型名也不在特征表里，被误判成永久错误——那重试就没意义了
    if isinstance(exc, LRAError):
        return exc

    name = type(exc).__name__
    msg = str(exc)

    # ---- 瞬时：类型名特征 ----
    transient_names = (
        "Timeout",          # httpx.TimeoutException / APITimeoutError / TimeoutError
        "ConnectError",     # httpx.ConnectError
        "RemoteProtocolError",
        "RateLimit",        # RateLimitError（429）
        "ReadError",        # httpx.ReadError
        "WriteError",       # httpx.WriteError
    )
    if any(token in name for token in transient_names):
        return TransientError(msg)

    # ---- 瞬时：内建网络异常 ----
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return TransientError(msg)

    # ---- 瞬时：HTTP 状态码 ----
    if hasattr(exc, "response"):
        code = getattr(exc.response, "status_code", 0)
        if code in _RETRYABLE_STATUS_CODES:
            return TransientError(msg, retry_after_sec=_parse_retry_after(exc))

    # ---- 其余：永久错误 ----
    return PermanentError(msg, cause=exc)


def _parse_retry_after(exc: Exception) -> float | None:
    """从 HTTP 响应头里读 Retry-After（429 时服务器会告诉你要等多久）。"""
    try:
        value = exc.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None
