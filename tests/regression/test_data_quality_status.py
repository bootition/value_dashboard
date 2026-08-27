from __future__ import annotations

import hashlib

import pytest

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
    assert status["dividends"]["active_missing_announcement_rows"] == 2
    assert status["lineage"]["invalid_hash_rows"] == 1
    assert status["lineage"]["orphan_batch_rows"] == 1
    assert status["lineage"]["archive_gap_rows"] == 0
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
        "latest_price_date": None,
    }


def test_data_dates_price_uses_latest_available_date(
    duckdb_store: DuckDBStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES
           ('000001', 'A', 'SZSE', TRUE), ('000002', 'B', 'SZSE', TRUE)"""
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"""INSERT INTO {table} (stock_code, trade_date, close) VALUES
                ('000001', '2026-08-05', 10), ('000002', '2026-08-04', 20)"""
        )

    dates = _data_dates(duckdb_store)

    assert dates["price"] == "2026-08-05"


def test_quality_status_flags_missing_inputs_for_a_currently_listed_stock(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'incomplete', 'SZSE', TRUE)"""
    )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["minimum_data_readiness"]["missing_counts"] == {
        "pool_metadata": 1,
        "raw_history": 1,
        "qfq_history": 1,
        "price_freshness": 1,
        "meaningful_volume": 1,
        "financial_period": 1,
        "share_capital": 1,
        "corporate_action_dividend_lineage": 1,
        "snapshot_input_freshness": 1,
        "snapshot_price_coherence": 1,
        "lineage_coverage": 1,
    }
    assert "MINIMUM_DATA_NOT_READY" in status["warning_codes"]


def test_recent_listing_data_gaps_are_disclosed_but_share_capital_still_blocks(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta
             (stock_code, name, exchange, is_listed, listing_date, is_st, is_suspended)
           VALUES ('301999', 'NEW', 'SZSE', TRUE, CURRENT_DATE, FALSE, FALSE)"""
    )

    readiness = build_data_quality_status(duckdb_store, sqlite_store)["minimum_data_readiness"]

    assert readiness["ready"] is True
    assert readiness["missing_counts"]["price_freshness"] == 1
    assert readiness["missing_counts"]["financial_period"] == 1
    assert readiness["missing_counts"]["share_capital"] == 1

    duckdb_store.write_query(
        """UPDATE stock_meta SET total_shares = 100
           WHERE stock_code = '301999'"""
    )

    readiness = build_data_quality_status(duckdb_store, sqlite_store)["minimum_data_readiness"]

    assert readiness["ready"] is False

    duckdb_store.write_query(
        """UPDATE stock_meta SET circ_shares = 50
           WHERE stock_code = '301999'"""
    )

    readiness = build_data_quality_status(duckdb_store, sqlite_store)["minimum_data_readiness"]

    assert readiness["ready"] is True


def test_quality_status_flags_active_dividend_alias_and_lineage_archive_gaps(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
               VALUES ('000001', 'canonical', 'SZSE', TRUE)"""
        )
        connection.execute(
            """INSERT INTO price_daily_raw (stock_code, trade_date, close)
               VALUES ('SZ000001', CURRENT_DATE, 10)"""
        )
        connection.execute(
            """INSERT INTO dividends (stock_code, ex_date, dividend_per_share)
               VALUES ('000001', '2025-08-15', 1)"""
        )
        connection.execute(
            """INSERT INTO fetch_batch
                (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash, row_count, confidence)
               VALUES ('batch-1', 'financial', 'test', 'v1', CURRENT_TIMESTAMP, ?, 1, 'verified')""",
            ["a" * 64],
        )
        connection.execute(
            """INSERT INTO source_audit
                (stock_code, field_name, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
               VALUES ('000001', 'revenue', 'test', 'batch-1', CURRENT_TIMESTAMP, ?, 'verified')""",
            ["b" * 64],
        )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["dividends"]["active_missing_announcement_rows"] == 1
    assert status["lineage"]["audit_archive_gap_rows"] == 1
    assert status["lineage"]["batch_archive_gap_rows"] == 1
    assert status["lineage"]["archive_gap_rows"] == 2
    assert status["code_identity"]["raw_alias_rows"] == 1
    assert {"DIVIDEND_DATES_UNVERIFIED", "LINEAGE_INVALID", "CODE_IDENTITY_ALIAS"}.issubset(
        status["warning_codes"]
    )


def test_quality_status_reports_incompatible_live_schema_without_querying_it(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.execute_script("DROP TABLE raw_response_archive")

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["minimum_data_readiness"]["ready"] is False
    assert status["warning_codes"] == ["LIVE_SCHEMA_INCOMPATIBLE", "MINIMUM_DATA_NOT_READY"]


def test_quality_status_detects_archive_payload_hash_mismatch(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
               VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
            ["a" * 64, b"tampered payload"],
        )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["lineage"]["hash_mismatch_rows"] == 1
    assert "LINEAGE_INVALID" in status["warning_codes"]


def test_archive_hash_check_is_cached_between_status_calls(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    payload = b"original payload"
    digest = hashlib.sha256(payload).hexdigest()
    duckdb_store.write_query(
        """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
           VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
        [digest, payload],
    )

    first = build_data_quality_status(duckdb_store, sqlite_store)
    assert first["lineage"]["hash_mismatch_rows"] == 0
    assert first["lineage"]["archive_hash_check"]["cached"] is False

    duckdb_store.write_query(
        """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
           VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
        ["b" * 64, b"tampered payload"],
    )
    cached = build_data_quality_status(duckdb_store, sqlite_store)
    assert cached["lineage"]["hash_mismatch_rows"] == 0
    assert cached["lineage"]["archive_hash_check"] == {"cached": True, "ttl_seconds": 60}

    refreshed = build_data_quality_status(
        duckdb_store, sqlite_store, force_archive_hash_recheck=True
    )
    assert refreshed["lineage"]["hash_mismatch_rows"] == 1
    assert refreshed["lineage"]["archive_hash_check"]["cached"] is False
    assert "LINEAGE_INVALID" in refreshed["warning_codes"]


def test_archive_hash_check_fails_closed_when_archive_is_unreadable(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_read_query = duckdb_store.read_query

    def fail_on_archive_scan(sql: str, params: list | None = None) -> list:
        if "FROM raw_response_archive WHERE payload IS NOT NULL" in sql:
            raise RuntimeError("archive unreadable")
        return real_read_query(sql, params)

    monkeypatch.setattr(duckdb_store, "read_query", fail_on_archive_scan)

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["lineage"]["hash_mismatch_rows"] == 1
    assert "LINEAGE_INVALID" in status["warning_codes"]


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
            VALUES ('600519', '2025-03-31', '2026-07-01')
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
