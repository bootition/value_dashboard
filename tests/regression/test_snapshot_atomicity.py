from __future__ import annotations

from datetime import date

import pytest

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_duckdb_transaction_rolls_back_on_exception(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600519', 'old', 'SSE')"
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        with duckdb_store.transaction() as connection:
            connection.execute("UPDATE stock_meta SET name = 'new' WHERE stock_code = '600519'")
            raise RuntimeError("injected failure")

    rows = duckdb_store.read_query(
        "SELECT name FROM stock_meta WHERE stock_code = '600519'"
    )
    assert rows == [{"name": "old"}]


def test_snapshot_failure_preserves_published_rows(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_snapshot_sources(duckdb_store)
    duckdb_store.write_query(
        """
        INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
        VALUES ('OLD001', '2024-12-31', 10.0)
        """
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)

    def calculate(stock_code: str) -> dict[str, str | float]:
        if stock_code == "000002":
            raise RuntimeError("injected calculation failure")
        return {"stock_code": stock_code, "report_date": "2025-03-31", "latest_close": 20.0}

    monkeypatch.setattr(calculator, "compute_all_for_stock", calculate)

    report = calculator.compute_snapshot_for_all(batch_size=1)

    rows = duckdb_store.read_query(
        "SELECT stock_code, report_date, latest_close FROM indicator_snapshot ORDER BY stock_code"
    )
    assert report["status"] == "partial"
    assert rows == [
        {"stock_code": "OLD001", "report_date": date(2024, 12, 31), "latest_close": 10.0}
    ]


def test_successful_snapshot_replaces_published_rows_atomically(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_snapshot_sources(duckdb_store)
    duckdb_store.write_query(
        """
        INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
        VALUES ('OLD001', '2024-12-31', 10.0)
        """
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)

    def calculate(stock_code: str) -> dict[str, str | float]:
        return {"stock_code": stock_code, "report_date": "2025-03-31", "latest_close": 20.0}

    monkeypatch.setattr(calculator, "compute_all_for_stock", calculate)

    report = calculator.compute_snapshot_for_all(batch_size=1)

    rows = duckdb_store.read_query(
        "SELECT stock_code, latest_close FROM indicator_snapshot ORDER BY stock_code"
    )
    assert report["status"] == "success"
    assert rows == [
        {"stock_code": "000001", "latest_close": 20.0},
        {"stock_code": "000002", "latest_close": 20.0},
    ]


def _insert_snapshot_sources(store: DuckDBStore) -> None:
    with store.write_connection() as connection:
        connection.execute(
            """
            INSERT INTO balance_sheet (stock_code, report_date)
            VALUES ('000001', '2025-03-31'), ('000002', '2025-03-31')
            """
        )
        connection.execute(
            """
            INSERT INTO income_statement (stock_code, report_date)
            VALUES ('000001', '2025-03-31'), ('000002', '2025-03-31')
            """
        )
