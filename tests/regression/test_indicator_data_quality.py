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
        INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
        VALUES
            ('600519', '2023-06-30', '2023-06-01', 1.0),
            ('600519', '2024-06-30', '2024-06-01', 1.5),
            ('600519', '2024-12-31', '2024-12-01', 2.0)
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
        # P0-4/5: 最新完整期 = 三表核心字段齐备；快照计算只取完整期
        connection.execute(
            """
            INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
            VALUES ('600519', '2025-03-31', 50)
            """
        )

    financials = _calculator(duckdb_store, sqlite_store)._get_latest_financials("600519")

    assert str(financials["report_date"]) == "2025-03-31"
    assert financials["total_assets"] == 1000


def test_ttm_matches_the_prior_year_same_report_period_not_row_position(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES
               ('600519', '2025-09-30', 900), ('600519', '2025-03-31', 200),
               ('600519', '2024-12-31', 1000), ('600519', '2024-09-30', 700),
               ('600519', '2024-06-30', 400), ('600519', '2024-03-31', 100)"""
        )

    ttm = _calculator(duckdb_store, sqlite_store)._get_ttm_data("600519")

    assert ttm["revenue"] == 1200
    assert ttm["_ttm_confidence"] == "strict"


def test_ttm_does_not_use_income_newer_than_snapshot_as_of(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue)
           VALUES ('600519', '2025-03-31', 200), ('600519', '2024-12-31', 1000),
                  ('600519', '2024-03-31', 100), ('600519', '2025-06-30', 500)"""
    )

    ttm = _calculator(duckdb_store, sqlite_store)._get_ttm_data("600519", "2025-03-31")

    assert ttm["report_date"].isoformat() == "2025-03-31"
    assert ttm["revenue"] == 1100


def test_published_income_correction_applies_to_ttm_and_growth_history(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit, deducted_net_profit)
           VALUES ('600519', '2025-12-31', 200, 20, 18), ('600519', '2024-12-31', 100, 10, 9)"""
    )
    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status)
           VALUES ('600519', 'revenue', '2025-12-31', 300, 'verified correction', 'published')"""
    )
    calculator = _calculator(duckdb_store, sqlite_store)

    growth = calculator._calc_growth("600519")
    ttm = calculator._get_ttm_data("600519")

    assert growth["revenue_yoy"] == 2.0
    assert ttm["revenue"] == 300


def test_dividend_yield_uses_only_trailing_twelve_months(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           SELECT '600519', CURRENT_DATE - INTERVAL '13 months', CURRENT_DATE - INTERVAL '14 months', 10.0
           UNION ALL SELECT '600519', CURRENT_DATE - INTERVAL '11 months', CURRENT_DATE - INTERVAL '12 months', 1.0"""
    )

    result = _calculator(duckdb_store, sqlite_store)._calc_dividend_yield("600519", 10.0)

    assert result == 0.1


def test_unverified_dividend_dates_do_not_produce_shareholder_metrics(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, dividend_per_share)
           VALUES ('600519', '2025-12-31', 2.0)"""
    )
    calculator = _calculator(duckdb_store, sqlite_store)

    assert calculator._get_dividend_summary("600519")["latest_dps"] is None
    assert calculator._calc_dividend_yield("600519", 10.0) is None
    assert calculator._calc_consecutive_div_years("600519") is None


def test_missing_debt_components_and_circulating_shares_remain_missing(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    calculator = _calculator(duckdb_store, sqlite_store)
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, total_shares, circ_shares)
           VALUES ('600519', 'Test', 'SSE', 1000, NULL)"""
    )

    assert calculator._get_shares("600519", {})["circ_shares"] is None
    assert calculator._calc_interest_bearing_debt({"short_term_loans": 1.0}) is None
    assert calculator._calc_safety(
        {"total_assets": 100.0, "total_liabilities": 20.0},
        {"operating_profit": 10.0, "financial_expenses": 2.0},
    )["interest_coverage"] is None


def test_ttm_metrics_do_not_fall_back_to_a_partial_reporting_period(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    calculator = _calculator(duckdb_store, sqlite_store)
    financials = {"total_assets": 1000.0, "total_equity": 700.0}
    missing_ttm = {"_ttm_confidence": "missing", "_ttm_reason": "insufficient_history"}

    profitability = calculator._calc_profitability(missing_ttm, financials)
    safety = calculator._calc_safety(financials, missing_ttm)

    assert all(value is None for value in profitability.values())
    assert safety["interest_coverage"] is None


def test_missing_ttm_propagates_to_all_ttm_dependent_valuation_and_payout_metrics(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    calculator = _calculator(duckdb_store, sqlite_store)
    missing_ttm = {"_ttm_confidence": "missing", "_ttm_reason": "insufficient_history"}
    valuation = calculator._calc_valuation(
        "600519", {"close": 10.0}, 1_000.0, 800.0, missing_ttm,
        {"total_equity": 700.0}, {},
    )
    shareholder = calculator._calc_shareholder_return(
        "600519", {}, {"latest_dps": 1.0}, missing_ttm, 1_000.0,
    )

    assert valuation["pe_ttm"] is None
    assert valuation["ps_ttm"] is None
    assert valuation["pcf_ttm"] is None
    assert shareholder["payout_ratio"] is None


def test_indicator_schema_persists_all_emitted_cagr_and_technical_fields(
    duckdb_store: DuckDBStore,
) -> None:
    columns = {
        row["column_name"]
        for row in duckdb_store.read_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'indicator_snapshot'"
        )
    }
    assert {
        "revenue_cagr3", "revenue_cagr5", "net_profit_cagr3", "net_profit_cagr5",
        "deducted_profit_cagr3", "deducted_profit_cagr5", "ma5", "ma10", "ma20",
        "ma60", "ma120", "ma250", "avg_volume", "period_return",
        "annualized_volatility", "max_drawdown", "turnover_rate",
    }.issubset(columns)


def test_refresh_treasury_spreads_updates_only_treasury_columns(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('600519', 'Test', 'SSE', true)"""
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
           (stock_code, report_date, latest_close, latest_price_date,
            calculated_at, pe_ttm, ttm_dividend_yield)
           VALUES ('600519', '2025-12-31', 10.0, '2026-08-31',
                   CURRENT_TIMESTAMP, 25.0, NULL)"""
    )
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', '2026-06-30', '2026-06-01', 2.0)"""
    )
    duckdb_store.write_query(
        """INSERT INTO treasury_yield_curve
           (curve_date, tenor_years, yield_pct, source, fetch_time, raw_hash,
            confidence, batch_id)
           VALUES ('2026-08-31', 10.0, 3.0, 'czb_mof', CURRENT_TIMESTAMP, ?,
                   'strict', 'test-batch')""",
        ["a" * 64],
    )

    report = _calculator(duckdb_store, sqlite_store).refresh_treasury_spreads(["600519"])

    assert report["status"] == "success"
    snapshot = duckdb_store.read_query(
        """SELECT ttm_dividend_yield, div_yield_spread_10y, pe_ttm
           FROM indicator_snapshot WHERE stock_code = '600519'"""
    )[0]
    assert snapshot["ttm_dividend_yield"] == 20.0
    assert snapshot["div_yield_spread_10y"] == 17.0
    assert snapshot["pe_ttm"] == 25.0, "非国债字段必须保持不变"
