"""事故出处：reports/117 / 2026-11-12。

Regression: 指标全量发布不得被单只"输入缺口"股票一票否决。

2026-11-12 生产发现（与 `share_capital_integrity` 门禁同一类缺陷）：全量发布前
`compute_snapshot_for_all` 只要发现**任何**一只非新股的上市股票缺核心三表或
价格，就整批返回 `partial / minimum_data_not_ready`，全市场 5,580 只快照一律
不发布。这等于让个别数据缺口把整站指标冻结。

新语义（与门禁同阈值同政策）：
- 缺口只数 <= ``max(50, 1%)``：逐股排除，其余照常发布，
  返回 `partial + minimum_data_not_ready`，并披露 `missing_input_codes`；
- 缺口规模超过阈值：视为系统性缺失，整批拒绝（保留上一代快照）。
"""

from __future__ import annotations

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def _seed(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, code: str, *, complete: bool) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, listing_date, is_st, is_suspended,
                total_shares, circ_shares)
           VALUES (?, ?, 'SZSE', '2020-01-01', false, false, 100, 80)""",
        [code, f"Test{code}"],
    )
    if complete:
        insert_minimum_screenable_data(duckdb_store, code)
    else:
        # 价格齐备但三表为空 → 非新股的核心输入缺口。
        with duckdb_store.write_connection() as connection:
            connection.execute(
                """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
                   SELECT ?, trade_date, 10, 100
                   FROM generate_series(
                       CURRENT_DATE - INTERVAL '6 years', CURRENT_DATE, INTERVAL '1 day'
                   ) AS calendar(trade_date)
                   WHERE EXTRACT(ISODOW FROM trade_date) < 6
                   ON CONFLICT DO NOTHING""",
                [code],
            )
            connection.execute(
                """INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume)
                   SELECT ?, trade_date, 10, 100
                   FROM generate_series(
                       CURRENT_DATE - INTERVAL '6 years', CURRENT_DATE, INTERVAL '1 day'
                   ) AS calendar(trade_date)
                   WHERE EXTRACT(ISODOW FROM trade_date) < 6
                   ON CONFLICT DO NOTHING""",
                [code],
            )


def test_complete_stocks_are_published_even_when_one_input_gap_exists(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed(duckdb_store, sqlite_store, "000001", complete=True)
    _seed(duckdb_store, sqlite_store, "000002", complete=True)
    _seed(duckdb_store, sqlite_store, "000003", complete=False)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # 缺口股票保留上一代快照行，用于验证"不被删除"。
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close, latest_price_date)
           VALUES ('000003', '2025-06-30', 9, DATE '2025-06-30')"""
    )

    report = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store).compute_snapshot_for_all()

    assert report["status"] == "partial", report
    assert report["reason"] == "minimum_data_not_ready"
    assert report["missing_input_codes"] == ["000003"]
    assert report["missing_input_count"] == 1
    assert report["success"] == 2

    published = {
        row["stock_code"]
        for row in duckdb_store.read_query(
            "SELECT stock_code FROM indicator_snapshot WHERE report_date = DATE '2025-12-31'"
        )
    }
    assert published == {"000001", "000002"}, published
    kept = duckdb_store.read_query(
        "SELECT report_date FROM indicator_snapshot WHERE stock_code = '000003'"
    )
    assert [str(row["report_date"])[:10] for row in kept] == ["2025-06-30"]


def test_systemic_input_gap_still_blocks_the_whole_publish(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    """缺口规模超过阈值 → 仍整批拒绝（防止"大部分股票没数据"被悄悄发布）。"""
    from app.core import data_quality

    monkeypatch.setattr(data_quality, "GATE_HARD_BLOCK_MIN", 0, raising=True)
    monkeypatch.setattr(data_quality, "GATE_HARD_BLOCK_RATIO", 0.0, raising=True)

    _seed(duckdb_store, sqlite_store, "000001", complete=True)
    _seed(duckdb_store, sqlite_store, "000002", complete=False)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)

    report = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store).compute_snapshot_for_all()

    assert report["status"] == "partial"
    assert report["reason"] == "minimum_data_not_ready"
    assert report["success"] == 0
    assert report["failed_codes"] == ["000002"]
