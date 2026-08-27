from __future__ import annotations

from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_financial_upsert_preserves_existing_values_absent_from_partial_payload(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    with duckdb_store.transaction() as conn:
        conn.execute(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_liabilities)
               VALUES ('000001', '2025-12-31', 100.0, 40.0)"""
        )
        initializer._upsert_financial_row(
            conn,
            "balance_sheet",
            "000001",
            {"REPORT_DATE": "2025-12-31", "TOTAL_ASSETS": 120.0},
        )

    row = duckdb_store.read_query(
        "SELECT total_assets, total_liabilities FROM balance_sheet WHERE stock_code = '000001'"
    )[0]
    assert row == {"total_assets": 120.0, "total_liabilities": 40.0}
