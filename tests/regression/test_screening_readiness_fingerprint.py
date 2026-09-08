from __future__ import annotations

from app.core.data_quality import screening_readiness_cache_key
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_screening_readiness_cache_key_covers_financials_overrides_dividends(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline

    duckdb_store.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets)
           VALUES ('000001', '2025-12-31', 100)"""
    )
    after_financial = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_financial and after_financial != baseline

    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status)
           VALUES ('000001', 'total_assets', '2025-12-31', 200, 'verified', 'published')"""
    )
    after_override = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_override and after_override != after_financial

    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('000001', '2025-06-30', '2025-05-01', 1)"""
    )
    after_dividend = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_dividend and after_dividend != after_override

    sqlite_store.execute(
        "INSERT INTO trading_dates (trade_date) VALUES ('2025-06-30')"
    )
    after_calendar = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_calendar and after_calendar != after_dividend
