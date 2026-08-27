from __future__ import annotations

import sqlite3

import pytest

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_active_override_does_not_change_financials(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_financials(duckdb_store)
    _insert_override(sqlite_store, status="active", value=2000.0)

    financials = IndicatorCalculator(
        duck=duckdb_store,
        sqlite=sqlite_store,
    )._get_latest_financials("600519")

    assert financials["total_assets"] == 1000.0


def test_published_override_changes_financials(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_financials(duckdb_store)
    _insert_override(sqlite_store, status="published", value=2000.0)

    financials = IndicatorCalculator(
        duck=duckdb_store,
        sqlite=sqlite_store,
    )._get_latest_financials("600519")

    assert financials["total_assets"] == 2000.0


def test_duplicate_current_published_override_is_rejected(
    sqlite_store: SQLiteStore,
) -> None:
    _insert_override(sqlite_store, status="published", value=2000.0)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_override(sqlite_store, status="published", value=3000.0)


def _insert_financials(store: DuckDBStore) -> None:
    with store.write_connection() as connection:
        connection.execute(
            """
            INSERT INTO balance_sheet
                (stock_code, report_date, total_assets, total_liabilities, total_equity)
            VALUES ('600519', '2025-03-31', 1000, 300, 700)
            """
        )
        connection.execute(
            """
            INSERT INTO income_statement
                (stock_code, report_date, revenue, parent_net_profit)
            VALUES ('600519', '2025-03-31', 500, 100)
            """
        )
        # P0-4/5: 最新完整期 = 三表核心字段齐备；快照计算只取完整期
        connection.execute(
            """
            INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
            VALUES ('600519', '2025-03-31', 50)
            """
        )


def _insert_override(store: SQLiteStore, status: str, value: float) -> None:
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO manual_overrides
                (stock_code, field_name, report_date, original_value,
                 override_value, reason, status)
            VALUES ('600519', 'total_assets', '2025-03-31', 1000, ?, 'test', ?)
            """,
            [value, status],
        )
