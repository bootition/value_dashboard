"""自适应请求间隔契约：长尾升档与恢复均需窗口证据，排除排队计时污染。"""

from __future__ import annotations

import pytest

from app.core.adapters.base import BaseAdapter


class _DummyAdapter(BaseAdapter):
    def __init__(self, rate_limit: float = 0.2) -> None:
        super().__init__("tencent", {"price_daily"}, rate_limit)


def _assert_interval(adapter: _DummyAdapter, expected: float) -> None:
    assert adapter.rate_limit_interval == pytest.approx(expected)


def test_single_long_tail_does_not_escalate() -> None:
    adapter = _DummyAdapter()

    adapter.record_response_duration(31.0)

    assert adapter.rate_limit_interval == 0.2


def test_two_long_tails_within_window_escalate() -> None:
    adapter = _DummyAdapter()
    adapter.record_response_duration(0.5)
    adapter.record_response_duration(31.0)
    adapter.record_response_duration(35.0)

    _assert_interval(adapter, 0.5)


def test_second_escalation_goes_to_one_second() -> None:
    adapter = _DummyAdapter()
    adapter.record_response_duration(31.0)
    adapter.record_response_duration(35.0)
    adapter.record_response_duration(40.0)
    adapter.record_response_duration(45.0)

    _assert_interval(adapter, 1.0)


def test_full_fast_window_recovers_towards_baseline() -> None:
    adapter = _DummyAdapter()
    adapter.record_response_duration(31.0)
    adapter.record_response_duration(35.0)
    _assert_interval(adapter, 0.5)

    # 第 10 条快响应把最后一条长尾挤出窗口并触发首次降档
    for _ in range(10):
        adapter.record_response_duration(0.2)
    _assert_interval(adapter, 0.4)

    # 满窗快响应时每条都会继续降档，2 条即回到配置基线
    for _ in range(3):
        adapter.record_response_duration(0.2)
    _assert_interval(adapter, 0.2)

    for _ in range(10):
        adapter.record_response_duration(0.2)
    _assert_interval(adapter, 0.2)


def test_mixed_slow_window_blocks_recovery() -> None:
    adapter = _DummyAdapter()
    adapter.record_response_duration(31.0)
    adapter.record_response_duration(35.0)
    _assert_interval(adapter, 0.5)

    for _ in range(9):
        adapter.record_response_duration(0.2)
    adapter.record_response_duration(11.0)

    _assert_interval(adapter, 0.5)


def test_fast_response_after_escalation_does_not_re_escalate() -> None:
    adapter = _DummyAdapter()
    adapter.record_response_duration(31.0)
    adapter.record_response_duration(35.0)
    _assert_interval(adapter, 0.5)

    # 窗口仍含 2 条长尾，但本次是快响应，不得再次升档
    adapter.record_response_duration(0.2)
    adapter.record_response_duration(0.2)
    _assert_interval(adapter, 0.5)
