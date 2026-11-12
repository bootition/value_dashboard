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

    with pytest.raises(RuntimeError, match="injected failure"), duckdb_store.transaction() as connection:
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
        """INSERT INTO stock_meta (stock_code, name, exchange, total_shares, circ_shares)
           VALUES ('000001', 'one', 'SSE', 100, 80)"""
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


def test_incremental_snapshot_keeps_old_row_for_failed_stock(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close) VALUES
           ('000001', '2025-03-31', 10), ('000002', '2025-03-31', 20)"""
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)

    def calculate(stock_code: str) -> dict[str, str | float]:
        if stock_code == "000002":
            raise RuntimeError("injected calculation failure")
        return {"stock_code": stock_code, "report_date": "2025-03-31", "latest_close": 15}

    monkeypatch.setattr(calculator, "compute_all_for_stock", calculate)

    report = calculator.compute_snapshot_for_codes(
        ["000001", "000002"], publish_gate=lambda duck, sqlite: {"ready": True},
    )

    assert report["status"] == "partial"
    assert report["success"] == 1
    assert report["failed_codes"] == ["000002"]
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


def test_snapshot_quarantines_the_only_broken_share_capital_stock(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-11-12 契约 §3/§4：股本关系破坏不再整批 rejected。

    旧语义（fail-closed 一票否决）：任一只股本违规 → ``ready=False`` →
    ``status=rejected / reason=publish_gate_failed``，全市场一条不写。
    生产代价：26 只缺口票里 5 只新股越过 90 天豁免线后，5,580 只快照
    自 10-30 起再没发布过（前端指标全 null、筛选 409）。

    新语义：违规数 ≤ 阈值 → ``hard_block=False``，违规票逐股隔离
    （``quarantined_codes``），其余照常发布并返回
    ``status=partial / reason=publish_gate_quarantined``；被隔离股票的
    旧快照行保留不动。本用例里唯一一只上市股票就是违规票，因此"其余"
    为空集，断言也随之变为 partial 而非 rejected。
    """
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

    assert report["status"] == "partial"
    assert report["reason"] == "publish_gate_quarantined"
    assert report["quarantined_codes"] == ["000001"]
    assert report["quarantined_count"] == 1
    assert report["success"] == 0
    # 隔离股的旧行不得被删除，且不会被新值覆盖。
    assert duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001"}]


def test_snapshot_hard_blocks_when_the_gate_reports_a_systemic_violation(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约 §4.1：只有 ``hard_block`` 才整批拒绝，保留上一代快照。"""
    _insert_ready_stock(duckdb_store, exchange="SSE", omit="")
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all(
        publish_gate=lambda duck, sqlite: {
            "ready": False,
            "hard_block": True,
            "reason": "share_capital_integrity",
            "quarantined_codes": [],
            "violations": [],
            "violation_count": 999,
        }
    )

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["gate"]["reason"] == "share_capital_integrity"
    assert duckdb_store.read_query("SELECT COUNT(*) AS cnt FROM indicator_snapshot") == [
        {"cnt": 0}
    ]


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

    # 契约 §4.3：ready 字段不再单独决定拒绝，注入的门禁必须显式 hard_block。
    report = calculator.compute_snapshot_for_all(
        publish_gate=lambda duck, sqlite: {
            "ready": False, "hard_block": True, "reason": "injected_gate",
        }
    )

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["gate"] == {"ready": False, "hard_block": True, "reason": "injected_gate"}
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
