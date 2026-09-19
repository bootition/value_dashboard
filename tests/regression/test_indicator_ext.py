"""扩展指标域 indicator_ext 回归（schema v25，2026-09-19）。

用最小种子数据验证三族指标的**计算口径**，防止将来有人改动公式而无人察觉：

- 周转率族：累计损益 ÷ 期末余额（CSMAR FI_T4「A」式）
- EBIT / EBITDA：CSMAR FI_T5 明确定义的公式
- 杠杆族：CSMAR FI_T7 明确定义的公式
- 自由现金流：本项目口径（经营现金流净额 − 资本支出）
- 缺失输入 → 对应列为 NULL（不得估算或回退）
"""

from __future__ import annotations

from app.core.indicators.extended import ExtendedIndicatorBuilder
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet

STOCK = "600519"
REPORT_DATE = "2024-12-31"


def _seed(store: DuckDBStore) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue, cost_of_revenue, "
            "net_profit, income_tax, financial_expenses, total_profit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [STOCK, REPORT_DATE, 1000.0, 400.0, 300.0, 50.0, 20.0, 370.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, accounts_receivable, inventory, "
            "accounts_payable, total_current_assets, fixed_assets, total_assets, total_equity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [STOCK, REPORT_DATE, 100.0, 200.0, 50.0, 600.0, 400.0, 2000.0, 1200.0],
        )
        conn.execute(
            "INSERT INTO cash_flow (stock_code, report_date, cf_from_operating) VALUES (?, ?, ?)",
            [STOCK, REPORT_DATE, 380.0],
        )
        conn.execute(
            "INSERT INTO cash_flow_indirect (stock_code, report_date, fixed_asset_depreciation, "
            "intangible_asset_amortization, long_term_prepaid_amortization, source, fetch_time, "
            "raw_response_hash, confidence, batch_id) VALUES (?, ?, ?, ?, ?, 'csmar', ?, 'h', 'strict', 'b')",
            [STOCK, REPORT_DATE, 60.0, 10.0, 5.0, "2026-09-19 00:00:00"],
        )
        conn.execute(
            "INSERT INTO cash_flow_activity (stock_code, report_date, capex, source, fetch_time, "
            "raw_response_hash, confidence, batch_id) VALUES (?, ?, ?, 'csmar', ?, 'h', 'strict', 'b')",
            [STOCK, REPORT_DATE, 80.0, "2026-09-19 00:00:00"],
        )


def _row(store: DuckDBStore) -> dict:
    rows = store.read_query(
        "SELECT * FROM indicator_ext WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
        [STOCK, REPORT_DATE],
    )
    assert rows, "indicator_ext 未生成该股票行"
    return rows[0]


def test_extended_indicators_compute_expected_values(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    report = builder.build()
    assert report["status"] == "success"
    assert report["rows_after"] == 1

    row = _row(duckdb_store)
    # 周转率族：累计损益 ÷ 期末余额
    assert row["receivables_turnover"] == 10.0      # 1000 / 100
    assert row["inventory_turnover"] == 2.0         # 400 / 200
    assert row["accounts_payable_turnover"] == 8.0  # 400 / 50
    assert row["current_asset_turnover"] == 1000.0 / 600.0
    assert row["fixed_asset_turnover"] == 2.5       # 1000 / 400
    assert row["total_asset_turnover"] == 0.5       # 1000 / 2000
    assert row["equity_turnover"] == 1000.0 / 1200.0
    # 折旧摊销 = 60 + 10 + 5（CSMAR FI_T6.F061201B 三项口径）
    assert row["depreciation_amortization"] == 75.0
    # 自由现金流 = 经营现金流 380 − 资本支出 80
    assert row["free_cash_flow"] == 300.0
    assert row["fcf_margin"] == 0.3
    # EBIT = 300 + 50 + 20 = 370；EBITDA = 370 + 75 = 445
    assert row["ebit"] == 370.0
    assert row["ebitda"] == 445.0
    # 杠杆：EBIT/利润总额 = 1；EBITDA/EBIT = 445/370；EBITDA/利润总额 = 445/370
    assert row["leverage_financial"] == 1.0
    assert abs(row["leverage_operating"] - 445.0 / 370.0) < 1e-9
    assert abs(row["leverage_total"] - 445.0 / 370.0) < 1e-9


def test_missing_indirect_statement_yields_null_not_estimate(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """缺间接法时 EBITDA/杠杆必须 NULL，不得回退估算或补零。"""
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue, net_profit, "
            "income_tax, financial_expenses, total_profit) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["000001", REPORT_DATE, 500.0, 100.0, 20.0, 10.0, 130.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_equity) "
            "VALUES (?, ?, ?, ?)",
            ["000001", REPORT_DATE, 1000.0, 500.0],
        )
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '000001'")[0]
    assert row["total_asset_turnover"] == 0.5
    assert row["depreciation_amortization"] is None
    assert row["ebitda"] is None
    assert row["leverage_operating"] is None
    assert row["free_cash_flow"] is None


def test_zero_denominator_yields_null(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """分母为 0 时 MUST NULL，不得产生 inf。"""
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue, total_profit) "
            "VALUES (?, ?, ?, ?)",
            ["000002", REPORT_DATE, 100.0, 0.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, accounts_receivable, total_assets) "
            "VALUES (?, ?, ?, ?)",
            ["000002", REPORT_DATE, 0.0, 1000.0],
        )
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '000002'")[0]
    assert row["receivables_turnover"] is None
    assert row["leverage_financial"] is None


def test_rebuild_is_idempotent(duckdb_store: DuckDBStore, database_paths: DatabasePathSet) -> None:
    _seed(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    first = builder.build()
    second = builder.build()
    assert first["rows_after"] == second["rows_after"]
    assert second["rows_after"] == 1


def test_scoped_rebuild_only_touches_given_codes(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed(duckdb_store)
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue) VALUES (?, ?, ?)",
            ["000003", REPORT_DATE, 200.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
            ["000003", REPORT_DATE, 400.0],
        )
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    assert len(duckdb_store.read_query("SELECT 1 FROM indicator_ext")) == 2
    builder.build(codes=[STOCK])
    codes = {r["stock_code"] for r in duckdb_store.read_query("SELECT stock_code FROM indicator_ext")}
    assert codes == {STOCK, "000003"}


# ─── 业务无意义护栏（2026-09-19 补齐折旧摊销后新增）────────────────────
# 与 calculator.py 的 pe<=1000 / pb<=200 / |roe|<=1 同思路：触发护栏写 NULL，
# 宁可如实缺失也不展示会误导人的数字。

def _seed_income(store: DuckDBStore, code: str, **kw) -> None:
    cols = ["stock_code", "report_date", *kw.keys()]
    values = [code, REPORT_DATE, *kw.values()]
    placeholders = ", ".join("?" for _ in cols)
    with store.transaction() as conn:
        conn.execute(f"INSERT INTO income_statement ({', '.join(cols)}) VALUES ({placeholders})", values)


def test_loss_making_company_has_null_leverage(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """亏损（利润总额<=0）时杠杆无业务含义，必须为 NULL。"""
    _seed_income(duckdb_store, "600001", revenue=100.0, net_profit=-50.0,
                 income_tax=0.0, financial_expenses=5.0, total_profit=-45.0)
    with duckdb_store.transaction() as conn:
        conn.execute("INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
                     ["600001", REPORT_DATE, 500.0])
        conn.execute(
            """INSERT INTO cash_flow_indirect (stock_code, report_date, fixed_asset_depreciation,
               source, fetch_time, raw_response_hash, confidence, batch_id)
               VALUES (?, ?, 20.0, 'csmar', '2026-09-19 00:00:00', 'h', 'strict', 'b')""",
            ["600001", REPORT_DATE],
        )
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600001'")[0]
    assert row["ebit"] == -45.0            # EBIT 本身照常计算
    assert row["ebitda"] == -25.0
    assert row["leverage_financial"] is None
    assert row["leverage_operating"] is None   # EBIT <= 0
    assert row["leverage_total"] is None


def test_absurd_fcf_margin_is_nulled(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """金融股经营现金流含客户资金，FCF率会得到 1000%+ 的荒谬值 → 必须 NULL。"""
    _seed_income(duckdb_store, "600002", revenue=100.0)
    with duckdb_store.transaction() as conn:
        conn.execute("INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
                     ["600002", REPORT_DATE, 500.0])
        conn.execute("INSERT INTO cash_flow (stock_code, report_date, cf_from_operating) VALUES (?, ?, ?)",
                     ["600002", REPORT_DATE, 200000.0])
        conn.execute(
            """INSERT INTO cash_flow_activity (stock_code, report_date, capex,
               source, fetch_time, raw_response_hash, confidence, batch_id)
               VALUES (?, ?, 1000.0, 'eastmoney_f10', '2026-09-19 00:00:00', 'h', 'strict', 'b')""",
            ["600002", REPORT_DATE],
        )
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600002'")[0]
    assert row["free_cash_flow"] == 199000.0   # 绝对值照常
    assert row["fcf_margin"] is None           # 1990 倍 > 5 倍上限


def test_extreme_leverage_is_nulled_and_normal_one_kept(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """EBIT 为正但极小时杠杆会发散（实测最大 19 万）→ |杠杆| > 100 必须 NULL；
    正常量级（2 倍、11 倍）必须保留。"""
    # 正常量级：EBIT = -9 + 0 + 9.1 = 0.1，利润总额 0.05
    _seed_income(duckdb_store, "600003", revenue=100.0, net_profit=-9.0,
                 income_tax=0.0, financial_expenses=9.1, total_profit=0.05)
    with duckdb_store.transaction() as conn:
        conn.execute("INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
                     ["600003", REPORT_DATE, 500.0])
        conn.execute(
            """INSERT INTO cash_flow_indirect (stock_code, report_date, fixed_asset_depreciation,
               source, fetch_time, raw_response_hash, confidence, batch_id)
               VALUES (?, ?, 1.0, 'csmar', '2026-09-19 00:00:00', 'h', 'strict', 'b')""",
            ["600003", REPORT_DATE],
        )

    # 散发量级：EBIT = -9.999 + 0 + 10.0 = 0.001，利润总额 1e-6
    _seed_income(duckdb_store, "600006", revenue=100.0, net_profit=-9.999,
                 income_tax=0.0, financial_expenses=10.0, total_profit=1e-6)
    with duckdb_store.transaction() as conn:
        conn.execute("INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
                     ["600006", REPORT_DATE, 500.0])
        conn.execute(
            """INSERT INTO cash_flow_indirect (stock_code, report_date, fixed_asset_depreciation,
               source, fetch_time, raw_response_hash, confidence, batch_id)
               VALUES (?, ?, 1.0, 'csmar', '2026-09-19 00:00:00', 'h', 'strict', 'b')""",
            ["600006", REPORT_DATE],
        )

    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()

    normal = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600003'")[0]
    assert abs(normal["leverage_financial"] - 2.0) < 1e-9
    assert abs(normal["leverage_operating"] - 11.0) < 1e-9

    absurd = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600006'")[0]
    assert abs(absurd["ebit"] - 0.001) < 1e-9   # 原始值照常计算
    assert absurd["leverage_financial"] is None  # 1000 倍 > 100
    assert absurd["leverage_operating"] is None  # 1001 倍 > 100


def test_negative_leverage_does_not_bypass_the_cap(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """回归：负值会绕过 `<= cap` 判定（-3739 <= 100 成立），必须用 ABS。"""
    _seed_income(duckdb_store, "600004", revenue=100.0, net_profit=0.5,
                 income_tax=0.0, financial_expenses=-200.0, total_profit=0.05)
    with duckdb_store.transaction() as conn:
        conn.execute("INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
                     ["600004", REPORT_DATE, 500.0])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600004'")[0]
    # EBIT = 0.5 + 0 - 200 = -199.5 → EBIT <= 0，财务杠杆应为 NULL
    assert row["leverage_financial"] is None


def test_zero_denominator_yields_null_for_new_columns(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """新列同样不得产生 inf/NaN。"""
    _seed_income(duckdb_store, "600005", revenue=0.0, net_profit=10.0,
                 income_tax=0.0, financial_expenses=0.0, total_profit=10.0)
    with duckdb_store.transaction() as conn:
        conn.execute("INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
                     ["600005", REPORT_DATE, 500.0])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600005'")[0]
    assert row["total_asset_turnover"] == 0.0
    assert row["equity_turnover"] is None


# ─── v26：每股族 / 费用率族 / 结构族 ────────────────────────────────

def test_per_share_uses_point_in_time_shares_not_current(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """每股指标的分母必须是「当时股数」（effective_date <= 报告期），
    不得用 stock_meta.total_shares（当前股本）。

    这是本项目反复踩过的口径坑（ops-knowledge-base D23：用当前股本算历史市值
    会把增发高估、回购低估）。这里用两次股本变更来钉死语义：
      2010-01-01 起 100 股 → 2026-01-01 起 200 股
      报告期应使用 100 股，而不是 200 股。
    """
    _seed_income(duckdb_store, "600007", revenue=1000.0, net_profit=100.0,
                 income_tax=0.0, financial_expenses=0.0, total_profit=100.0)
    with duckdb_store.transaction() as conn:
        conn.execute(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_equity,
               total_equity_parent, surplus_reserve, undistributed_profit, total_current_assets,
               fixed_assets) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ["600007", REPORT_DATE, 2000.0, 1000.0, 1000.0, 100.0, 300.0, 500.0, 800.0],
        )
        # 报告期(2024-12-31)之前生效的是 100 股；200 股在报告期之后才生效，
        # 若误用「最新股数」就会得到 bps=5 而不是 10。
        for eff, shares in (("2010-01-01", 100.0), ("2026-01-01", 200.0)):
            conn.execute(
                """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, source, raw_hash, batch_id)
                   VALUES (?, CAST(? AS DATE), ?, 'csmar', 'h', 'b')""",
                ["600007", eff, shares],
            )
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600007'")[0]
    assert row["bps"] == 10.0                    # 1000 / 100（当时股数），不是 1000/200=5
    assert row["revenue_per_share"] == 10.0      # 1000 / 100
    assert row["retained_earnings_per_share"] == 4.0   # (100+300)/100
    # 费用率族与结构族
    assert row["current_asset_ratio"] == 0.25    # 500/2000
    assert row["fixed_asset_ratio"] == 0.4       # 800/2000
    assert row["equity_multiplier"] == 2.0       # 2000/1000


def test_expense_ratio_guard_and_structure_guard(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """费用率分母趋 0 时发散；权益乘数权益趋 0 时发散 —— 均须置 NULL。"""
    # 费用率失真：收入 1 元、销售费用 100 元（10000%）
    _seed_income(duckdb_store, "600008", revenue=1.0, selling_expenses=100.0)
    # 权益乘数失真：权益 0.01、资产 1000（10 万倍）
    _seed_income(duckdb_store, "600009", revenue=100.0)
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_equity) VALUES (?, ?, ?, ?)",
            ["600008", REPORT_DATE, 500.0, 100.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_equity) VALUES (?, ?, ?, ?)",
            ["600009", REPORT_DATE, 1000.0, 0.01],
        )
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    a = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600008'")[0]
    b = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600009'")[0]
    assert a["selling_expense_ratio"] is None      # 100 倍 > 5 倍上限
    assert b["equity_multiplier"] is None          # 100000 倍 > 100 上限
