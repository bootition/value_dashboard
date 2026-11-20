from __future__ import annotations

from app.core.data_quality import screening_readiness_cache_key
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite


def test_screening_readiness_cache_key_covers_financials_overrides_dividends(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline

    Seed(duckdb_store).balance("000001", "2025-12-31", total_assets=100)
    after_financial = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_financial and after_financial != baseline

    SeedSqlite(sqlite_store).override(
        "000001",
        "total_assets",
        200,
        report_date="2025-12-31",
        reason="verified",
        status="published",
    )
    after_override = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_override and after_override != after_financial

    Seed(duckdb_store).dividends("000001", [("2025-06-30", "2025-05-01", 1)])
    after_dividend = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_dividend and after_dividend != after_override

    SeedSqlite(sqlite_store).trading_dates(["2025-06-30"])
    after_calendar = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_calendar and after_calendar != after_dividend
