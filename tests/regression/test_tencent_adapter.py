from __future__ import annotations

import time

from app.core.adapters.base import FetchRequest
from app.core.adapters.tencent_adapter import TencentAdapter


class _Response:
    def __init__(self, bars: list | None = None) -> None:
        self._bars = bars

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": {
                "bj920000": {
                    "qfqday": self._bars
                    if self._bars is not None
                    else [["2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174"]],
                }
            }
        }


class _PagedResponse:
    """First call returns one bar, later calls return nothing (source exhausted)."""

    def __init__(self) -> None:
        self.calls = 0

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            bars = [["2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174"]]
        else:
            bars = []
        return {"data": {"bj920000": {"qfqday": bars}}}


def test_tencent_bse_qfq_prices_are_normalized_and_archivable(monkeypatch) -> None:
    responder = _PagedResponse()
    monkeypatch.setattr(
        "app.core.adapters.tencent_adapter.requests.get",
        lambda *args, **kwargs: responder,
    )

    result = TencentAdapter(rate_limit=0).fetch(
        FetchRequest(
            data_type="price_daily", stock_codes=["920000"],
            start_date="2026-07-29", end_date="2026-07-31", adjust="qfq",
        )
    )

    assert result.metadata.error is None
    assert result.metadata.source == "tencent"
    assert result.metadata.raw_response_hash
    assert result.raw_response
    assert result.data == [{
        "stock_code": "920000", "trade_date": "2026-07-31",
        "open": 14.16, "close": 14.58, "high": 15.1, "low": 14.1,
        "volume": 3317400.0, "turnover": None, "turnover_rate": None,
    }]


def test_tencent_pages_back_when_source_has_older_data(monkeypatch) -> None:
    responder = _PagedResponse()
    monkeypatch.setattr(
        "app.core.adapters.tencent_adapter.requests.get",
        lambda *args, **kwargs: responder,
    )

    result = TencentAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="price_daily", stock_codes=["920000"], adjust="qfq")
    )

    assert result.metadata.error is None
    assert len(result.data) == 1
    assert responder.calls == 2  # first page + exhausted probe


def test_tencent_bse_qfq_falls_back_to_raw_when_no_adjustment_exists(monkeypatch) -> None:
    class RawOnlyResponse(_Response):
        def json(self) -> dict:
            return {
                "data": {
                    "bj920000": {
                        "day": [["2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174"]],
                    }
                }
            }

    monkeypatch.setattr("app.core.adapters.tencent_adapter.requests.get", lambda *args, **kwargs: RawOnlyResponse())
    result = TencentAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="price_daily", stock_codes=["920000"], adjust="qfq")
    )

    assert result.metadata.error is None
    assert result.data[0]["close"] == 14.58


def test_tencent_price_deadline_bounds_http_timeout(monkeypatch) -> None:
    seen: dict[str, float] = {}

    def capture(*args, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return _Response()

    monkeypatch.setattr("app.core.adapters.tencent_adapter.requests.get", capture)
    result = TencentAdapter(rate_limit=0).fetch(
        FetchRequest(
            data_type="price_daily",
            stock_codes=["920000"],
            adjust="qfq",
            extra_params={"deadline_monotonic": time.monotonic() + 0.5},
        )
    )

    assert result.metadata.error is None
    assert 0 < seen["timeout"] <= 0.5
