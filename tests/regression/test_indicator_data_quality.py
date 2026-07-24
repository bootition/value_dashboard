from __future__ import annotations

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _calculator(store: DuckDBStore, sqlite: SQLiteStore) -> IndicatorCalculator:
    return IndicatorCalculator(duck=store, sqlite=sqlite)


def test_dividend_summary_sums_only_latest_year(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """
        INSERT INTO dividends (stock_code, ex_date, dividend_per_share)
        VALUES
            ('600519', '2023-06-30', 1.0),
            ('600519', '2024-06-30', 1.5),
            ('600519', '2024-12-31', 2.0)
        """
    )

    summary = _calculator(duckdb_store, sqlite_store)._get_dividend_summary("600519")

    assert summary["years_with_dividend"] == 2
    assert summary["latest_dps"] == 3.5


def test_dividend_summary_returns_empty_values_when_no_records(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    summary = _calculator(duckdb_store, sqlite_store)._get_dividend_summary("000001")

    assert summary["total_records"] == 0
    assert summary["latest_dps"] is None


def test_latest_financials_ignore_newer_shell_row(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """
            INSERT INTO balance_sheet
                (stock_code, report_date, total_assets, total_liabilities,
                 total_equity, total_equity_parent, paid_in_capital)
            VALUES
                ('600519', '2025-03-31', 1000, 300, 700, 700, 100),
                ('600519', '2026-03-31', NULL, NULL, NULL, NULL, NULL)
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

    financials = _calculator(duckdb_store, sqlite_store)._get_latest_financials("600519")

    assert str(financials["report_date"]) == "2025-03-31"
    assert financials["total_assets"] == 1000
