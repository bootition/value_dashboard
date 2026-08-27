from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.web.api.stock_detail import build_freshness_metadata


def test_freshness_uses_price_and_snapshot_age_not_financial_reporting_gap() -> None:
    today = datetime.now(UTC).date()
    freshness = build_freshness_metadata(
        financial_date=today - timedelta(days=90),
        price_date=today - timedelta(days=15),
        calculated_at=datetime.now(UTC) - timedelta(days=14),
        data_version="test",
    )

    assert freshness["financial_age_days"] == 90
    assert freshness["price_age_days"] == 15
    assert freshness["snapshot_age_days"] == 14
    assert freshness["stale_warning"] is True


def test_freshness_flags_stale_financials_with_current_price_and_snapshot() -> None:
    today = datetime.now(UTC).date()
    freshness = build_freshness_metadata(
        financial_date=today - timedelta(days=730),
        price_date=today,
        calculated_at=datetime.now(UTC),
        data_version="test",
    )

    assert freshness["financial_age_days"] == 730
    assert freshness["stale_warning"] is True
