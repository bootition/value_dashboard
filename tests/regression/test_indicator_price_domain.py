from __future__ import annotations

from app.core.indicators.calculator import IndicatorCalculator
from tests.conftest import insert_minimum_screenable_data
from tests.support.seed import Seed


def test_price_sensitive_recompute_skips_unchanged_and_updates_only_price_fields(
    duckdb_store, sqlite_store,
) -> None:
    with duckdb_store.write_connection() as conn:
        Seed(duckdb_store).join(conn).stock(
            "000001",
            name="fixture",
            exchange="SZSE",
            is_listed=True,
            is_st=False,
            is_suspended=False,
        )
    insert_minimum_screenable_data(duckdb_store)
    # 保证价格域更新前有干净快照
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    with duckdb_store.write_connection() as conn:
        conn.execute(
            """UPDATE indicator_snapshot SET
                   pe_ttm = 7.5, pb_mrq = 1.25, cumulative_dividend_amount = 123.0
               WHERE stock_code = '000001'"""
        )

    # 价格与快照完全一致 → 直接跳过，基本面字段不被动。
    unchanged = calculator.compute_price_sensitive_for_codes(["000001"])
    assert unchanged["status"] == "success"
    assert unchanged["skipped"] == 1
    after = duckdb_store.read_query(
        "SELECT latest_close, pe_ttm, cumulative_dividend_amount FROM indicator_snapshot WHERE stock_code='000001'"
    )[0]
    assert after["pe_ttm"] == 7.5
    assert after["cumulative_dividend_amount"] == 123.0

    # 新交易日价格变化 → 只重算价格域，报告期与累计分红字段保持原值。
    with duckdb_store.write_connection() as conn:
        # 留（第 6 类·库侧相对时间）：`CURRENT_DATE + INTERVAL '1 day'` 是"**明天**"的
        # 交易日 —— 用例要的是"价格比库里的最后一天更新"，固定字面量过了那天就变成
        # 历史日期，价格域根本不会触发重算，用例会静默退化。raw/qfq 两行同一口径一起留。
        conn.execute(
            """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
               VALUES ('000001', CURRENT_DATE + INTERVAL '1 day', 11, 200)"""
        )
        conn.execute(
            """INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume)
               VALUES ('000001', CURRENT_DATE + INTERVAL '1 day', 11, 200)"""
        )
    changed = calculator.compute_price_sensitive_for_codes(["000001"], force_codes={"000001"})
    assert changed["status"] == "success"
    updated = duckdb_store.read_query(
        """SELECT report_date, latest_close, latest_price_date,
                  pe_ttm, cumulative_dividend_amount, calculated_at
           FROM indicator_snapshot WHERE stock_code='000001'"""
    )[0]
    assert updated["latest_close"] == 11
    assert updated["report_date"].isoformat() == "2025-12-31"
    assert updated["cumulative_dividend_amount"] == 123.0
    assert updated["pe_ttm"] is not None
    assert updated["calculated_at"] is not None


def test_funding_dividend_field_refresh_updates_only_three_columns(
    duckdb_store, sqlite_store,
) -> None:
    insert_minimum_screenable_data(duckdb_store)
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    with duckdb_store.write_connection() as conn:
        # 迁移守则：原 SQL 的 `fetch_time = CURRENT_TIMESTAMP` 显式换成 `trace_now=True`
        # （"刚抓的融资事件"），否则会静默退化成固定的过去时间戳。
        Seed(duckdb_store, trace_now=True).join(conn).fundings(
            "000001",
            [
                (
                    "ipo",
                    "2015-01-10",
                    100,
                    {
                        "announce_date": "2015-01-01",
                        "issue_price": 10,
                        "raise_funds": None,
                        "raise_funds_net": 1000,
                        "source": "cninfo_funding",
                        "raw_hash": "h",
                        "confidence": "strict",
                        "batch_id": "fixture",
                    },
                )
            ],
        )
        conn.execute(
            """UPDATE indicator_snapshot SET pe_ttm = 7.5 WHERE stock_code='000001'"""
        )

    report = calculator.refresh_funding_dividend_fields(["000001"])
    assert report["status"] == "success"
    row = duckdb_store.read_query(
        """SELECT cumulative_financing_amount, dividend_financing_ratio_pct, pe_ttm
           FROM indicator_snapshot WHERE stock_code='000001'"""
    )[0]
    assert row["cumulative_financing_amount"] == 1000
    assert row["pe_ttm"] == 7.5
