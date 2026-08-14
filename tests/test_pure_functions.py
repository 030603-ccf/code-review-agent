"""纯函数单元测试：不依赖图、不依赖模型，直接测编排层的小函数。

覆盖对象（都是“输入 dict -> 输出 dict/列表”的纯逻辑，最好测的代码）：
    - fan_out             发牌员：零块/有块/缺键 三态
    - route_after_aggregate  条件边路由：配了终审/没配 两态
    - classify_error      异常分类：瞬时 vs 永久
    - _should_skip        文件过滤（阶段 5 的常量与函数）
"""

import sys
from pathlib import Path

import pytest
from langgraph.types import Send

import lra
from lra.errors import PermanentError, TransientError, classify_error


# ==================== fan_out（graph.py）====================

def test_fan_out_empty_work_returns_aggregate_string():
    """零块：返回普通跳转 ["aggregate"]，图不悬空。"""
    from lra.graph import fan_out
    assert fan_out({"work": []}) == ["aggregate"]


def test_fan_out_absent_work_key_returns_aggregate():
    """work 键缺失（极端情况）：get 兜底，同样返回 ["aggregate"]。"""
    from lra.graph import fan_out
    assert fan_out({}) == ["aggregate"]


def test_fan_out_with_work_returns_sends():
    """有块：每块一张 Send 工单，包裹里带 entry/chunk/run_dir。"""
    from lra.graph import fan_out
    work = [
        {"entry": {"relpath": "a.py"}, "chunk": {"file": "a.py",
                                                 "line_start": 1}},
        {"entry": {"relpath": "b.py"}, "chunk": {"file": "b.py",
                                                 "line_start": 5}},
    ]
    result = fan_out({"work": work, "run_dir": "runs/x"})
    assert len(result) == 2
    assert all(isinstance(s, Send) for s in result)
    # 包裹原样透传，还附带 run_dir（并行分支写日志要用）
    assert result[0].arg["entry"] == work[0]["entry"]
    assert result[0].arg["chunk"] == work[0]["chunk"]
    assert result[0].arg["run_dir"] == "runs/x"


# ==================== route_after_aggregate（graph.py）====================

def test_route_after_aggregate_with_judge():
    """配了终审模型 -> 走 second_review。"""
    from lra.graph import make_route_after_aggregate
    route = make_route_after_aggregate(second_client=object())
    assert route({"aggregated": []}) == "second_review"


def test_route_after_aggregate_without_judge():
    """没配终审模型 -> 直达 report。"""
    from lra.graph import make_route_after_aggregate
    route = make_route_after_aggregate(second_client=None)
    assert route({"aggregated": []}) == "report"


# ==================== fan_out_failed（graph.py，失败块补跑路由）====================

def _fake_failed_block():
    return [{"entry": {"relpath": "a.py"},
             "chunk": {"file": "a.py", "line_start": 1},
             "error": "APIError: 欠费"}]


def test_fan_out_failed_has_failed_blocks_sends_retry():
    """有失败块且轮次未满 -> 派发 retry_failed 补跑（Send 工单）。"""
    from lra.graph import fan_out_failed
    result = fan_out_failed({"failed_blocks": _fake_failed_block(),
                             "retry_round": 0})
    assert len(result) == 1
    assert isinstance(result[0], Send)
    assert result[0].node == "retry_failed"
    assert result[0].arg["round"] == 0


def test_fan_out_failed_round_limit_reached_goes_report():
    """轮数到上限 -> 不再补跑，直达 report（防无限循环）。"""
    from lra.graph import fan_out_failed
    result = fan_out_failed({"failed_blocks": _fake_failed_block(),
                             "retry_round": 1})
    assert result == ["report"]


def test_fan_out_failed_no_failed_blocks_with_judge():
    """无失败块 + 配了终审 -> 走 second_review。"""
    from lra.graph import fan_out_failed
    result = fan_out_failed({"failed_blocks": [], "retry_round": 0,
                             "second_client_enabled": True})
    assert result == ["second_review"]


def test_fan_out_failed_no_failed_blocks_without_judge():
    """无失败块 + 没配终审 -> 直达 report。"""
    from lra.graph import fan_out_failed
    result = fan_out_failed({"failed_blocks": [], "retry_round": 0,
                             "second_client_enabled": False})
    assert result == ["report"]


# ==================== 402 欠费可重试（errors.py）====================

def test_classify_402_insufficient_balance_is_transient():
    """402 余额不足归为瞬时错误：充值后重试能成功，不该永久放弃。"""
    from cra.llm.client import APIError
    from lra.errors import classify_error, TransientError

    class _R:
        status_code = 402

    err = APIError("HTTP 402: Insufficient Balance")
    err.response = _R()
    assert isinstance(classify_error(err), TransientError)


# ==================== classify_error（errors.py）====================

class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeHTTPError(Exception):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def test_classify_timeout_name_is_transient():
    """类型名带 Timeout（httpx.TimeoutException 等）-> 瞬时。"""
    assert isinstance(classify_error(TimeoutError("慢死了")), TransientError)


def test_classify_connection_error_is_transient():
    """内建 ConnectionError -> 瞬时。"""
    assert isinstance(classify_error(ConnectionError("连不上")), TransientError)


def test_classify_429_is_transient():
    """HTTP 429（速率限制）-> 瞬时。"""
    err = _FakeHTTPError(_FakeResponse(429))
    assert isinstance(classify_error(err), TransientError)


def test_classify_503_is_transient():
    """HTTP 503（服务不可用）-> 瞬时。"""
    err = _FakeHTTPError(_FakeResponse(503))
    assert isinstance(classify_error(err), TransientError)


def test_classify_400_is_permanent():
    """HTTP 400（请求本身有错）-> 永久。"""
    err = _FakeHTTPError(_FakeResponse(400))
    assert isinstance(classify_error(err), PermanentError)


def test_classify_value_error_is_permanent():
    """普通异常（ValueError 等）-> 永久。"""
    assert isinstance(classify_error(ValueError("坏的输出")), PermanentError)


def test_classify_own_transient_error_passes_through():
    """lra 自己的 TransientError 原样放行，不会被误判成永久。"""
    err = classify_error(TransientError("超时了"))
    assert isinstance(err, TransientError)
