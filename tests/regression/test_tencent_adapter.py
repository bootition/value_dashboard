from __future__ import annotations

from app.core.adapters.base import FetchRequest
from app.core.adapters.tencent_adapter import TencentAdapter


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": {
                "bj920000": {
                    "qfqday": [["2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174"]],
                }
            }
        }


def test_tencent_bse_qfq_prices_are_normalized_and_archivable(monkeypatch) -> None:
    monkeypatch.setattr("app.core.adapters.tencent_adapter.requests.get", lambda *args, **kwargs: _Response())

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
