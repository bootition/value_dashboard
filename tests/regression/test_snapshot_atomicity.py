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
            INSERT INTO stock_meta (stock_code, name, exchange, total_shares, circ_shares)
            VALUES ('000001', 'one', 'SSE', 100, 80), ('000002', 'two', 'SSE', 100, 80)
            """
        )
        connection.execute(
            """
            INSERT INTO balance_sheet
                (stock_code, report_date, total_assets, total_liabilities, total_equity)
            VALUES
                ('000001', '2025-03-31', 100.0, 20.0, 80.0),
                ('000002', '2025-03-31', 100.0, 20.0, 80.0)
            """
        )
        connection.execute(
            """
            INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
            VALUES ('000001', '2025-03-31', 100.0, 10.0), ('000002', '2025-03-31', 100.0, 10.0)
            """
        )
        connection.execute(
            """
            INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
            VALUES ('000001', '2025-03-31', 1.0), ('000002', '2025-03-31', 1.0)
            """
        )
        connection.execute(
            """
            INSERT INTO price_daily_raw (stock_code, trade_date, close)
            VALUES ('000001', '2025-03-31', 10.0), ('000002', '2025-03-31', 10.0)
            """
        )
        connection.execute(
            """
            INSERT INTO price_daily_qfq (stock_code, trade_date, close)
            VALUES ('000001', '2025-03-31', 10.0), ('000002', '2025-03-31', 10.0)
            """
        )


def test_snapshot_refuses_to_publish_when_the_universe_lacks_minimum_data(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange)
           VALUES ('000001', 'one', 'SSE')"""
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
           VALUES ('OLD001', '2024-12-31', 10.0)"""
    )

    report = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store).compute_snapshot_for_all()

    assert report["status"] == "partial"
    assert report["reason"] == "minimum_data_not_ready"
    assert duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001"}]


def test_incremental_snapshot_replaces_only_changed_stock_atomically(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close) VALUES
           ('000001', '2025-03-31', 10), ('000002', '2025-03-31', 20)"""
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 15},
    )

    report = calculator.compute_snapshot_for_codes(
        ["000001"], publish_gate=lambda duck, sqlite: {"ready": True},
    )

    assert report["status"] == "success"
    assert duckdb_store.read_query(
        "SELECT stock_code, latest_close FROM indicator_snapshot ORDER BY stock_code"
    ) == [
        {"stock_code": "000001", "latest_close": 15.0},
        {"stock_code": "000002", "latest_close": 20.0},
    ]


@pytest.mark.parametrize(
    ("exchange", "missing", "should_publish"),
    [
        ("SSE", "financial", False),
        ("SSE", "raw", False),
        ("SSE", "qfq", False),
        ("BSE", "qfq", False),
    ],
)
def test_snapshot_readiness_gate_covers_each_required_input(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
    exchange: str,
    missing: str,
    should_publish: bool,
) -> None:
    _insert_ready_stock(duckdb_store, exchange=exchange, omit=missing)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
           VALUES ('OLD001', '2024-12-31', 10.0)"""
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all()
    rows = duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    )

    if should_publish:
        assert report["status"] == "success"
        assert rows == [{"stock_code": "000001"}]
    else:
        assert report["status"] == "partial"
        assert report["reason"] == "minimum_data_not_ready"
        assert rows == [{"stock_code": "OLD001"}]


def test_snapshot_refuses_to_publish_when_share_capital_relation_is_broken(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_ready_stock(duckdb_store, exchange="SSE", omit="")
    duckdb_store.write_query(
        "UPDATE stock_meta SET circ_shares = 200 WHERE stock_code = '000001'"
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
           VALUES ('OLD001', '2024-12-31', 10.0)"""
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all()

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["gate"]["reason"] == "share_capital_integrity"
    assert report["gate"]["share_capital_violations"] == ["000001"]
    assert duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001"}]


def test_snapshot_publish_gate_is_injectable(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_ready_stock(duckdb_store, exchange="SSE", omit="")
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all(
        publish_gate=lambda duck, sqlite: {"ready": False, "reason": "injected_gate"}
    )

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["gate"] == {"ready": False, "reason": "injected_gate"}
    assert duckdb_store.read_query("SELECT COUNT(*) AS cnt FROM indicator_snapshot") == [
        {"cnt": 0}
    ]


def _insert_ready_stock(store: DuckDBStore, *, exchange: str, omit: str) -> None:
    with store.write_connection() as connection:
        connection.execute(
            """INSERT INTO stock_meta (stock_code, name, exchange, total_shares, circ_shares)
               VALUES ('000001', 'one', ?, 100, 80)""",
            [exchange],
        )
        if omit != "financial":
            connection.execute(
                """INSERT INTO balance_sheet
                       (stock_code, report_date, total_assets, total_liabilities, total_equity)
                   VALUES ('000001', '2025-03-31', 100.0, 20.0, 80.0)"""
            )
            connection.execute(
                """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
                   VALUES ('000001', '2025-03-31', 100.0, 10.0)"""
            )
            connection.execute(
                """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
                   VALUES ('000001', '2025-03-31', 1.0)"""
            )
        if omit != "raw":
            connection.execute(
                """INSERT INTO price_daily_raw (stock_code, trade_date, close)
                   VALUES ('000001', '2025-03-31', 10.0)"""
            )
        if omit != "qfq":
            connection.execute(
                """INSERT INTO price_daily_qfq (stock_code, trade_date, close)
                   VALUES ('000001', '2025-03-31', 10.0)"""
            )
