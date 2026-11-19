"""指标数据质量回归（口径如实缺失 / TTM 对齐 / 分红窗口）。

播种：能表达的都走 `tests/support/seed.py` 的按域工厂。**仍留内联 SQL 的三类**：
1. `with duckdb_store.write_connection()` 事务块内的多行写入 —— 工厂自带写锁，
   嵌套进已持有写锁的事务会撞锁，故保留；
2. `dividends` / `treasury_yield_curve` / `manual_overrides` 这类工厂还没有方法的表；
3. 依赖 `CURRENT_DATE`（相对「今天」构造 13/11 个月前的分红）的行 —— 工厂的
   日期列是字面量，表达不了相对日期。
"""

from __future__ import annotations

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed


def _calculator(store: DuckDBStore, sqlite: SQLiteStore) -> IndicatorCalculator:
    return IndicatorCalculator(duck=store, sqlite=sqlite)


def test_dividend_summary_sums_only_latest_year(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    Seed(duckdb_store).dividends(
        "600519",
        [
            ("2023-06-30", "2023-06-01", 1.0),
            ("2024-06-30", "2024-06-01", 1.5),
            ("2024-12-31", "2024-12-01", 2.0),
        ],
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
    # 保留内联：三表要在**同一个** write_connection 事务里一起写入（这个用例
    # 本身就是「最新期是空壳行」的场景），工厂每次自带写锁，嵌套会撞锁。
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
    # 保留内联：同上 —— write_connection() 事务块内写入，工厂会撞写锁。
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
    # 2025-06-30 在快照 as-of 之后：TTM 不得把它算进来（否则 2025-03-31 口径错位）。
    seed = Seed(duckdb_store)
    seed.income("600519", "2025-03-31", revenue=200)
    seed.income("600519", "2024-12-31", revenue=1000)
    seed.income("600519", "2024-03-31", revenue=100)
    seed.income("600519", "2025-06-30", revenue=500)

    ttm = _calculator(duckdb_store, sqlite_store)._get_ttm_data("600519", "2025-03-31")

    assert ttm["report_date"].isoformat() == "2025-03-31"
    assert ttm["revenue"] == 1100


def test_published_income_correction_applies_to_ttm_and_growth_history(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    seed = Seed(duckdb_store)
    seed.income("600519", "2025-12-31", revenue=200, parent_net_profit=20, deducted_net_profit=18)
    seed.income("600519", "2024-12-31", revenue=100, parent_net_profit=10, deducted_net_profit=9)
    # 保留内联：`manual_overrides` 是 SQLite 侧的表（已发布人工修正），不在 DuckDB 工厂范围。
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
    # 保留内联：日期是 SQL 侧 `CURRENT_DATE - INTERVAL '13 months'` 的**日历月**
    # 运算（相对今天构造 13/11 个月前的分红，让用例不随日历漂移）。要换成工厂调用
    # 就得在 Python 侧重造日历月（`timedelta` 只能按天，得引 `dateutil.relativedelta`），
    # 等于给种子再立一份日期口径，而且"今天"会从库里时钟挪到进程时钟 —— 不是缺 API。
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
    # `announcement_date` 传 None 是**故意**的（未证公告日 → 股东回报指标必须
    # 整项缺失）；工厂不会替它填 ex_date。
    Seed(duckdb_store).dividends("600519", [("2025-12-31", None, 2.0)])
    calculator = _calculator(duckdb_store, sqlite_store)

    assert calculator._get_dividend_summary("600519")["latest_dps"] is None
    assert calculator._calc_dividend_yield("600519", 10.0) is None
    assert calculator._calc_consecutive_div_years("600519") is None


def test_missing_debt_components_and_circulating_shares_remain_missing(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    calculator = _calculator(duckdb_store, sqlite_store)
    # circ_shares 故意留 NULL：没有流通股本时流通市值/换手类指标必须保持缺失。
    Seed(duckdb_store).stock(
        "600519", name="Test", exchange="SSE", total_shares=1000, circ_shares=None,
    )

    assert calculator._get_shares("600519", {})["circ_shares"] is None
    # 2026-11-14 修复：缺「应付债券」按 0 计（多数公司本来就没有发债），
    # 只有三项全空才返回 None。旧行为把 roic 覆盖率压到 11.8%。
    assert calculator._calc_interest_bearing_debt({"short_term_loans": 1.0}) == 1.0
    assert calculator._calc_interest_bearing_debt(
        {"short_term_loans": 1.0, "long_term_loans": 2.0}
    ) == 3.0
    assert calculator._calc_interest_bearing_debt(
        {"short_term_loans": 1.0, "long_term_loans": 2.0, "bonds_payable": None}
    ) == 3.0
    assert calculator._calc_interest_bearing_debt({}) is None
    assert calculator._calc_interest_bearing_debt(
        {"short_term_loans": None, "long_term_loans": None, "bonds_payable": None}
    ) is None
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
    Seed(duckdb_store).stock("600519", name="Test", exchange="SSE", is_listed=True)
    # 只种本用例要校验的三列：latest_close/latest_price_date 是 refresh 的筛选与
    # 对齐依据，pe_ttm 必须保持不变，ttm_dividend_yield 由 refresh 重算；
    # `calculated_at` 由生产方在 UPDATE 时回写，这里不编造。
    Seed(duckdb_store).snapshot(
        "600519", "2025-12-31",
        latest_close=10.0, latest_price_date="2026-08-31", pe_ttm=25.0,
    )
    Seed(duckdb_store).dividends("600519", [("2026-06-30", "2026-06-01", 2.0)])
    # 曲线要"刚抓的"（原来的 SQL 侧 `CURRENT_TIMESTAMP`）→ 显式开 `trace_now`；
    # raw_hash/batch_id 仍是调用点原来的值。
    Seed(duckdb_store, trace_now=True).treasury_yield_curve(
        "2026-08-31", 10.0, 3.0, raw_hash="a" * 64, batch_id="test-batch",
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


def test_quick_ratio_treats_missing_inventory_as_zero(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """J5：存货缺失 = 没有存货，速动比率不应因此整项为 NULL。

    2026-11-15 修复前，`quick_ratio` 要求
    `total_current_assets` / `inventory` / `total_current_liabilities` 三项
    全非空；但银行/保险/互联网/服务业本来就没有存货（源里为 NULL）。
    实测 2026-09-30 有 53 只因此被压成 NULL（覆盖率 97.2% → 98.2%）。
    """
    calculator = _calculator(duckdb_store, sqlite_store)
    financials = {
        "total_assets": 500.0,
        "total_liabilities": 200.0,
        "total_current_assets": 100.0,
        "total_current_liabilities": 80.0,
        "inventory": None,
    }

    safety = calculator._calc_safety(financials, {})

    assert safety["current_ratio"] == 1.25
    assert safety["quick_ratio"] == 1.25  # 存货按 0 计

    with_inventory = dict(financials, inventory=20.0)
    assert calculator._calc_safety(with_inventory, {})["quick_ratio"] == 1.0

    # 流动资产/流动负债缺失仍必须保持 NULL：不能把分母或分子按 0 兜底。
    missing_assets = dict(financials, total_current_assets=None)
    assert calculator._calc_safety(missing_assets, {})["quick_ratio"] is None
    assert calculator._calc_safety(missing_assets, {})["current_ratio"] is None

    zero_liabilities = dict(financials, total_current_liabilities=0.0)
    assert calculator._calc_safety(zero_liabilities, {})["quick_ratio"] is None
