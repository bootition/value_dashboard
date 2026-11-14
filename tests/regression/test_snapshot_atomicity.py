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


def test_snapshot_hard_blocks_when_the_whole_small_library_is_broken(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """股本门禁语义三段演进（本用例随契约同步更新，勿回退）：

    1. 2026-11-12 之前（fail-closed 一票否决）：任一只股本违规 → ``ready=False``
       → ``status=rejected``，全市场一条不写。生产代价：26 只缺口票里 5 只新股
       越过 90 天豁免线后，5,580 只快照自 10-30 起再没发布过。
    2. 2026-11-12（契约 §3/§4）：违规票逐股隔离，其余照常发布。本用例当时
       断言 ``partial / publish_gate_quarantined``——因为唯一一只上市股票就是
       违规票，"其余"是空集。
    3. 2026-11-13 P1：零容忍"整库被隔离"。``max(50, 1%)`` 在小库上形同虚设
       （<101 只时恒为 50），于是补占比判据「≥1/3 上市股同时违规 = 系统性」
       → ``hard_block``。本 fixture 是 1 只库、100% 违规 → 命中占比判据，
       断言回到 ``rejected / publish_gate_failed``（但理由已不同：这次是
       "系统性错乱"，不是"任一只即否决"）。
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

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["gate"]["hard_block"] is True
    # 命中的是占比判据（1/1 ≥ 1/3），不是绝对阈值（1 ≤ 50）
    assert report["gate"]["hard_block_rule"] == "share"
    assert report["success"] == 0
    # 整批拒绝：隔离股与非隔离股的旧行都保持原样
    assert duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001"}]
    # 逐股隔离路径（partial + publish_gate_quarantined）由
    # test_fix20261112_gate_contract.py 的非系统性规模用例覆盖。


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
