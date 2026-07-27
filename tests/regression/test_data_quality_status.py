from __future__ import annotations

from app.core.data_quality import _data_dates, build_data_quality_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore


def test_data_quality_status_exposes_untrusted_and_stale_data(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_quality_fixture(duckdb_store, sqlite_store)

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["dates"]["price"] == "2026-07-17"
    assert status["dates"]["balance_sheet"]["latest_record"] == "2026-03-31"
    assert status["dates"]["balance_sheet"]["latest_complete"] == "2025-03-31"
    assert status["dividends"]["unverified_period_end_rows"] == 2
    assert status["lineage"]["invalid_hash_rows"] == 1
    assert status["lineage"]["orphan_batch_rows"] == 1
    assert status["operations"]["unpublished_overrides"] == 1
    assert status["operations"]["running_jobs"] == 1
    assert {
        "FINANCIAL_SHELL_ROWS",
        "SNAPSHOT_STALE",
        "DIVIDEND_DATES_UNVERIFIED",
        "LINEAGE_INVALID",
        "UNPUBLISHED_OVERRIDES",
        "STALE_RUNNING_JOBS",
    }.issubset(set(status["warning_codes"]))


def test_data_dates_supports_snapshot_schema_before_freshness_migration(
    database_paths: DatabasePathSet,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    duck.execute_script(
        """
        CREATE TABLE price_daily_raw (trade_date DATE);
        CREATE TABLE balance_sheet (
            report_date DATE,
            total_assets DOUBLE,
            total_liabilities DOUBLE,
            total_equity DOUBLE,
            total_equity_parent DOUBLE
        );
        CREATE TABLE income_statement (
            report_date DATE,
            revenue DOUBLE,
            parent_net_profit DOUBLE
        );
        CREATE TABLE cash_flow (report_date DATE, cf_from_operating DOUBLE);
        CREATE TABLE indicator_snapshot (report_date DATE);
        INSERT INTO indicator_snapshot VALUES ('2025-03-31');
        """
    )

    dates = _data_dates(duck)

    assert dates["indicator_snapshot"] == {
        "latest_complete": "2025-03-31",
        "calculated_at": None,
    }


def _insert_quality_fixture(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    with duck.write_connection() as connection:
        connection.execute(
            """
            INSERT INTO price_daily_raw (stock_code, trade_date, close)
            VALUES ('600519', '2026-07-17', 1500)
            """
        )
        connection.execute(
            """
            INSERT INTO balance_sheet
                (stock_code, report_date, total_assets, total_liabilities, total_equity)
            VALUES
                ('600519', '2025-03-31', 1000, 300, 700),
                ('600519', '2026-03-31', NULL, NULL, NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO income_statement
                (stock_code, report_date, revenue, parent_net_profit)
            VALUES
                ('600519', '2025-03-31', 500, 100),
                ('600519', '2026-03-31', NULL, 110)
            """
        )
        connection.execute(
            """
            INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
            VALUES ('600519', '2025-03-31', 120)
            """
        )
        connection.execute(
            """
            INSERT INTO indicator_snapshot (stock_code, report_date, latest_price_date)
            VALUES ('600519', '2025-03-31', '2026-07-17')
            """
        )
        connection.execute(
            """
            INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
            VALUES
                ('600519', '2023-12-31', NULL, 1.0),
                ('600519', '2024-06-30', NULL, 2.0)
            """
        )
        connection.execute(
            """
            INSERT INTO source_audit
                (stock_code, field_name, source, fetch_batch_id, fetch_time,
                 raw_response_hash, confidence)
            VALUES
                ('600519', 'total_assets', 'local_cache', 'orphan', now(), '', 'missing')
            """
        )

    with sqlite.transaction() as connection:
        connection.execute(
            """
            INSERT INTO manual_overrides
                (stock_code, field_name, override_value, reason, status)
            VALUES ('600519', 'total_assets', 2000, 'draft', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO job_logs (job_type, status, started_at)
            VALUES ('backfill', 'running', '2025-01-01T00:00:00')
            """
        )
