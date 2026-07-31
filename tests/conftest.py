"""Safe pytest configuration for audit regression tests."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import (
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    canonicalize_path,
)
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore


def insert_minimum_screenable_data(store: DuckDBStore, stock_code: str = "000001") -> None:
    """Populate the non-metadata inputs required by the screening readiness gate."""
    with store.write_connection() as connection:
        connection.execute(
            """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
               SELECT ?, trade_date, 10, 100
               FROM generate_series(
                   CURRENT_DATE - INTERVAL '6 years', CURRENT_DATE, INTERVAL '1 day'
               ) AS calendar(trade_date)
               WHERE EXTRACT(ISODOW FROM trade_date) < 6
               ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume)
               SELECT ?, trade_date, 10, 100
               FROM generate_series(
                   CURRENT_DATE - INTERVAL '6 years', CURRENT_DATE, INTERVAL '1 day'
               ) AS calendar(trade_date)
               WHERE EXTRACT(ISODOW FROM trade_date) < 6
               ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO balance_sheet
                   (stock_code, report_date, total_assets, total_liabilities, total_equity)
               VALUES (?, '2025-12-31', 100, 20, 80) ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        connection.execute(
            """UPDATE stock_meta SET total_shares = 100, circ_shares = 80
               WHERE stock_code = ?""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close, latest_price_date, calculated_at)
               VALUES (?, '2025-12-31', 10, CURRENT_DATE, CURRENT_TIMESTAMP)
               ON CONFLICT (stock_code, report_date) DO UPDATE SET
                   latest_close = excluded.latest_close,
                   latest_price_date = excluded.latest_price_date,
                   calculated_at = excluded.calculated_at""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
               VALUES (?, '2025-12-31', 100, 10) ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
               VALUES (?, '2025-12-31', 10) ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
               VALUES (?, '2025-06-30', '2025-05-01', 1) ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        connection.execute(
            """INSERT INTO xdxr (stock_code, event_date, category, fenhong)
               VALUES (?, '2025-06-30', 1, 1) ON CONFLICT DO NOTHING""",
            [stock_code],
        )
        batches = (
            ("price_daily_raw", "latest_close", "CURRENT_DATE"),
            ("price_daily_qfq", "latest_close", "CURRENT_DATE"),
            ("balance_sheet", "total_assets", "DATE '2025-12-31'"),
            ("balance_sheet", "total_liabilities", "DATE '2025-12-31'"),
            ("balance_sheet", "total_equity", "DATE '2025-12-31'"),
            ("income_statement", "revenue", "DATE '2025-12-31'"),
            ("income_statement", "parent_net_profit", "DATE '2025-12-31'"),
            ("cash_flow", "cf_from_operating", "DATE '2025-12-31'"),
            ("indicator_snapshot", "latest_close", "DATE '2025-12-31'"),
        )
        for data_type, field_name, report_date in batches:
            payload = f"fixture:{stock_code}:{data_type}".encode("ascii")
            digest = hashlib.sha256(payload).hexdigest()
            batch_id = f"fixture-{stock_code}-{data_type}"
            connection.execute(
                """INSERT INTO raw_response_archive
                   (raw_response_hash, source, fetch_time, payload, integrity_verified)
                   VALUES (?, 'fixture', CURRENT_TIMESTAMP - INTERVAL '1 day', ?, TRUE) ON CONFLICT DO NOTHING""",
                [digest, payload],
            )
            connection.execute(
                """INSERT INTO fetch_batch
                   (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash, row_count, confidence)
                   VALUES (?, ?, 'fixture', '1', CURRENT_TIMESTAMP - INTERVAL '1 day', ?, 1, 'strict')""",
                [batch_id, data_type, digest],
            )
            connection.execute(
                f"""INSERT INTO source_audit
                   (stock_code, field_name, report_date, value, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
                   VALUES (?, ?, {report_date}, 1, 'fixture', ?, CURRENT_TIMESTAMP - INTERVAL '1 day', ?, 'strict')""",
                [stock_code, field_name, batch_id, digest],
            )


def insert_matching_trading_calendar(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    """Persist the exact QFQ dates used by the screenability fixture."""
    dates = duck.read_query(
        "SELECT CAST(trade_date AS VARCHAR) AS trade_date FROM price_daily_qfq ORDER BY trade_date"
    )
    with sqlite.transaction() as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO trading_dates (trade_date) VALUES (?)",
            [(row["trade_date"],) for row in dates],
        )


@pytest.fixture
def database_paths(tmp_path: Path) -> DatabasePathSet:
    """Return a validated database pair inside the wrapper-owned test root."""
    run_root = Path(os.environ["VD_TEST_RUN_ROOT"])
    if not run_root.is_dir():
        raise RuntimeError("wrapper-created VD_TEST_RUN_ROOT is missing")

    canonical_run_root = canonicalize_path(run_root)
    canonical_tmp = canonicalize_path(tmp_path)
    if canonical_run_root not in canonical_tmp.parents:
        raise PathIsolationError(f"tmp_path escaped VD_TEST_RUN_ROOT: {canonical_tmp}")

    return DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=canonical_tmp / "valuedashboard.duckdb",
        sqlite_path=canonical_tmp / "valuedashboard.sqlite",
        run_root=canonical_tmp,
    ).validate()


@pytest.fixture
def duckdb_store(database_paths: DatabasePathSet) -> DuckDBStore:
    """Return an isolated DuckDB store initialized inside pytest's temp directory."""
    store = DuckDBStore(paths=database_paths)
    init_duckdb_schema(store)
    return store


@pytest.fixture
def sqlite_store(database_paths: DatabasePathSet) -> SQLiteStore:
    """Return an isolated SQLite store initialized inside pytest's temp directory."""
    store = SQLiteStore(paths=database_paths)
    init_sqlite_schema(store)
    return store


@pytest.fixture
def sample_stock_code() -> str:
    """样本股票代码: 贵州茅台"""
    return "600519"
