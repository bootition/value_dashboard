from __future__ import annotations

from datetime import datetime, timedelta

from app.core.financial_period import CN_TZ
from app.web.api.stock_detail import build_freshness_metadata


def test_freshness_uses_price_and_snapshot_age_not_financial_reporting_gap() -> None:
    # 2026-11-12：年龄按**中国交易日**计算（d7bf840）。UTC 与 CN 在
    # 00:00–08:00 相差一天，用 UTC "今天" 会让断言随运行时刻漂移。
    today = datetime.now(CN_TZ).date()
    freshness = build_freshness_metadata(
        financial_date=today - timedelta(days=90),
        price_date=today - timedelta(days=15),
        calculated_at=datetime.now(CN_TZ) - timedelta(days=14),
        data_version="test",
    )

    assert freshness["financial_age_days"] == 90
    assert freshness["price_age_days"] == 15
    assert freshness["snapshot_age_days"] == 14
    assert freshness["stale_warning"] is True


def test_freshness_flags_stale_financials_with_current_price_and_snapshot() -> None:
    today = datetime.now(CN_TZ).date()
    freshness = build_freshness_metadata(
        financial_date=today - timedelta(days=730),
        price_date=today,
        calculated_at=datetime.now(CN_TZ),
        data_version="test",
    )

    assert freshness["financial_age_days"] == 730
    assert freshness["stale_warning"] is True
