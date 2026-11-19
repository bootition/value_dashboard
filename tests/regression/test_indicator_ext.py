"""扩展指标域 indicator_ext 回归（schema v25，2026-09-19）。

用最小种子数据验证三族指标的**计算口径**，防止将来有人改动公式而无人察觉：

- 周转率族：累计损益 ÷ 期末余额（CSMAR FI_T4「A」式）
- EBIT / EBITDA：CSMAR FI_T5 明确定义的公式
- 杠杆族：CSMAR FI_T7 明确定义的公式
- 自由现金流：本项目口径（经营现金流净额 − 资本支出）
- 缺失输入 → 对应列为 NULL（不得估算或回退）

播种统一走 `tests/support/seed.py` 的**按域工厂**（`reports/140` §17.7 第 1 条）：
工厂只负责「怎么把行放进去」（主键、provenance 必填列、日期写法），
**数值一律逐字写在本文件里** —— 「放什么」才是这些用例的全部意义。
"""

from __future__ import annotations

from app.core.indicators.extended import ExtendedIndicatorBuilder
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from tests.support.seed import Seed

STOCK = "600519"
REPORT_DATE = "2024-12-31"


def _seed(store: DuckDBStore) -> None:
    """一只最小股票的全套输入：三表 + 间接法折旧摊销 + 资本支出。"""
    seed = Seed(store)
    seed.income(STOCK, REPORT_DATE, revenue=1000.0, cost_of_revenue=400.0, net_profit=300.0,
                income_tax=50.0, financial_expenses=20.0, total_profit=370.0)
    seed.balance(STOCK, REPORT_DATE, accounts_receivable=100.0, inventory=200.0, accounts_payable=50.0,
                 total_current_assets=600.0, fixed_assets=400.0, total_assets=2000.0,
                 total_equity=1200.0)
    seed.cash_flow(STOCK, REPORT_DATE, cf_from_operating=380.0)
    # 折旧摊销三项口径（CSMAR FI_T6.F061201B）
    seed.cash_indirect(STOCK, REPORT_DATE, fixed_asset_depreciation=60.0,
                       intangible_asset_amortization=10.0, long_term_prepaid_amortization=5.0)
    seed.cash_activity(STOCK, REPORT_DATE, capex=80.0)


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
    seed = Seed(duckdb_store)
    seed.income("000001", REPORT_DATE, revenue=500.0, net_profit=100.0, income_tax=20.0,
                financial_expenses=10.0, total_profit=130.0)
    seed.balance("000001", REPORT_DATE, total_assets=1000.0, total_equity=500.0)
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
    seed = Seed(duckdb_store)
    seed.income("000002", REPORT_DATE, revenue=100.0, total_profit=0.0)
    seed.balance("000002", REPORT_DATE, accounts_receivable=0.0, total_assets=1000.0)
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
    seed = Seed(duckdb_store)
    seed.income("000003", REPORT_DATE, revenue=200.0)
    seed.balance("000003", REPORT_DATE, total_assets=400.0)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    assert len(duckdb_store.read_query("SELECT 1 FROM indicator_ext")) == 2
    builder.build(codes=[STOCK])
    codes = {r["stock_code"] for r in duckdb_store.read_query("SELECT stock_code FROM indicator_ext")}
    assert codes == {STOCK, "000003"}


# ─── rows_written 的真实读数（2026-11-17 独立审计问题 1）───────────────
# 旧写法 `after - before if not target else after` 是**假读数**：
#   · 全量重建后 before == after → 报 0（实际重写整表）；
#   · 按股重建 → 报整表行数（正式库一次 force_codes=[689009] 报 361393）。
# 下列用例把"实际写入语义"钉死：全量 = rows_after；按股 = 目标股票的行数
# （每期一行，不是股票数）；空目标清单 = 0 且 skipped。


def _seed_financial_pair(store: DuckDBStore, code: str, report_date: str) -> None:
    """只为扩展指标构建所需的最小 income × balance 组合。"""
    Seed(store).screenable(
        code,
        report_date,
        income={"revenue": 1000.0, "net_profit": 100.0},
        balance={"total_assets": 2000.0, "total_equity": 1200.0},
    )


def _seed_two_codes_two_periods(store: DuckDBStore) -> None:
    for code in ("600519", "000003"):
        for report_date in ("2024-12-31", "2023-12-31"):
            _seed_financial_pair(store, code, report_date)


def test_full_rebuild_reports_rows_after_as_rows_written(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed_two_codes_two_periods(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)

    initial = builder.build()
    assert initial["rows_before"] == 0
    assert initial["rows_written"] == 4

    # **已填充表上的全量重建**才是正式库形态：before == after == 4，
    # 旧写法 after-before 会报 0（审计实测正是 rows_before=2 rows_after=2 → 0）。
    report = builder.build()

    assert report["status"] == "success"
    assert report["rows_before"] == 4
    assert report["rows_after"] == 4
    assert report["rows_written"] == 4, (
        "全量重写了 4 行就必须报 4；after-before 在重建场景恒为 0"
    )


def test_scoped_rebuild_reports_target_rows_not_stock_or_table_count(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed_two_codes_two_periods(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()

    force_report = builder.build(force_codes=["600519"])
    codes_report = builder.build(codes=["000003"])

    for report in (force_report, codes_report):
        assert report["status"] == "success"
        assert report["rows_before"] == 4
        assert report["rows_after"] == 4
        # 1 只股票 × 2 期 = 2 行；旧写法会报整表 4 行。
        assert report["rows_written"] == 2, (
            f"按股重建必须报目标股票的实际行数，实际报告: {report['rows_written']}"
        )

    # 目标清单非空但库里没有该股 → 实际写入 0 行，不是 skipped、也不是全表行数。
    absent_report = builder.build(codes=["999999"])
    assert absent_report["status"] == "success"
    assert absent_report["rows_after"] == 4
    assert absent_report["rows_written"] == 0


def test_empty_target_skipped_reports_zero_rows_written(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed_two_codes_two_periods(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()

    report = builder.build(force_codes=[])

    assert report["status"] == "skipped"
    assert report["reason"] == "no_target_codes"
    assert report["rows_before"] == report["rows_after"] == 4
    assert report["rows_written"] == 0


# ─── 业务无意义护栏（2026-09-19 补齐折旧摊销后新增）────────────────────
# 与 calculator.py 的 pe<=1000 / pb<=200 / |roe|<=1 同思路：触发护栏写 NULL，
# 宁可如实缺失也不展示会误导人的数字。


def test_loss_making_company_has_null_leverage(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """亏损（利润总额<=0）时杠杆无业务含义，必须为 NULL。"""
    seed = Seed(duckdb_store)
    seed.income("600001", REPORT_DATE, revenue=100.0, net_profit=-50.0, income_tax=0.0,
                financial_expenses=5.0, total_profit=-45.0)
    seed.balance("600001", REPORT_DATE, total_assets=500.0)
    seed.cash_indirect("600001", REPORT_DATE, fixed_asset_depreciation=20.0)
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
    seed = Seed(duckdb_store)
    seed.income("600002", REPORT_DATE, revenue=100.0)
    seed.balance("600002", REPORT_DATE, total_assets=500.0)
    seed.cash_flow("600002", REPORT_DATE, cf_from_operating=200000.0)
    # 数据源换成东财 F10（真实来源之一）：工厂的 source 默认值必须可被调用点覆盖。
    seed.cash_activity("600002", REPORT_DATE, capex=1000.0, source="eastmoney_f10")
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600002'")[0]
    assert row["free_cash_flow"] == 199000.0   # 绝对值照常
    assert row["fcf_margin"] is None           # 1990 倍 > 5 倍上限


def test_extreme_leverage_is_nulled_and_normal_one_kept(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """EBIT 为正但极小时杠杆会发散（实测最大 19 万）→ |杠杆| > 100 必须 NULL；
    正常量级（2 倍、11 倍）必须保留。"""
    seed = Seed(duckdb_store)
    # 正常量级：EBIT = -9 + 0 + 9.1 = 0.1，利润总额 0.05
    seed.income("600003", REPORT_DATE, revenue=100.0, net_profit=-9.0, income_tax=0.0,
                financial_expenses=9.1, total_profit=0.05)
    seed.balance("600003", REPORT_DATE, total_assets=500.0)
    seed.cash_indirect("600003", REPORT_DATE, fixed_asset_depreciation=1.0)

    # 散发量级：EBIT = -9.999 + 0 + 10.0 = 0.001，利润总额 1e-6
    seed.income("600006", REPORT_DATE, revenue=100.0, net_profit=-9.999, income_tax=0.0,
                financial_expenses=10.0, total_profit=1e-6)
    seed.balance("600006", REPORT_DATE, total_assets=500.0)
    seed.cash_indirect("600006", REPORT_DATE, fixed_asset_depreciation=1.0)

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
    seed = Seed(duckdb_store)
    seed.income("600004", REPORT_DATE, revenue=100.0, net_profit=0.5, income_tax=0.0,
                financial_expenses=-200.0, total_profit=0.05)
    seed.balance("600004", REPORT_DATE, total_assets=500.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600004'")[0]
    # EBIT = 0.5 + 0 - 200 = -199.5 → EBIT <= 0，财务杠杆应为 NULL
    assert row["leverage_financial"] is None


def test_zero_denominator_yields_null_for_new_columns(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """新列同样不得产生 inf/NaN。"""
    seed = Seed(duckdb_store)
    seed.income("600005", REPORT_DATE, revenue=0.0, net_profit=10.0, income_tax=0.0,
                financial_expenses=0.0, total_profit=10.0)
    seed.balance("600005", REPORT_DATE, total_assets=500.0)
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
    seed = Seed(duckdb_store)
    seed.income("600007", REPORT_DATE, revenue=1000.0, net_profit=100.0, income_tax=0.0,
                financial_expenses=0.0, total_profit=100.0)
    seed.balance("600007", REPORT_DATE, total_assets=2000.0, total_equity=1000.0,
                 total_equity_parent=1000.0, surplus_reserve=100.0, undistributed_profit=300.0,
                 total_current_assets=500.0, fixed_assets=800.0)
    # 报告期(2024-12-31)之前生效的是 100 股；200 股在报告期之后才生效，
    # 若误用「最新股数」就会得到 bps=5 而不是 10。
    seed.shares("600007", [("2010-01-01", 100.0), ("2026-01-01", 200.0)])
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
    seed = Seed(duckdb_store)
    # 费用率失真：收入 1 元、销售费用 100 元（10000%）
    seed.income("600008", REPORT_DATE, revenue=1.0, selling_expenses=100.0)
    # 权益乘数失真：权益 0.01、资产 1000（10 万倍）
    seed.income("600009", REPORT_DATE, revenue=100.0)
    seed.balance("600008", REPORT_DATE, total_assets=500.0, total_equity=100.0)
    seed.balance("600009", REPORT_DATE, total_assets=1000.0, total_equity=0.01)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    a = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600008'")[0]
    b = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600009'")[0]
    assert a["selling_expense_ratio"] is None      # 100 倍 > 5 倍上限
    assert b["equity_multiplier"] is None          # 100000 倍 > 100 上限


# ─── v27：大股东占款（治理红旗）──────────────────────────────────────

def test_shareholder_occupation_uses_ext_pair(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """大股东占款 = (其他应收款合计 − 其他应付款) / 总资产。

    分子两侧必须同源配对（都用 balance_sheet_ext 的合计项），
    否则主链 other_receivables（净额）与应付合计口径不一致会低估占款。
    """
    seed = Seed(duckdb_store)
    seed.income("600010", REPORT_DATE, revenue=100.0)
    # 主链净额故意设成误导值
    seed.balance("600010", REPORT_DATE, total_assets=1000.0, other_receivables=999.0)
    seed.balance_ext("600010", REPORT_DATE, other_payables=100.0, total_other_receivable=400.0,
                     source="eastmoney_f10")
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600010'")[0]
    assert row["shareholder_occupation"] == 0.3    # (400-100)/1000，不是 (999-100)/1000


def test_shareholder_occupation_null_when_payables_missing(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """缺其他应付款时必须 NULL —— 不得用「0 应付」冒充，否则占款被高估。"""
    seed = Seed(duckdb_store)
    seed.income("600011", REPORT_DATE, revenue=100.0)
    seed.balance("600011", REPORT_DATE, total_assets=1000.0, other_receivables=300.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600011'")[0]
    assert row["shareholder_occupation"] is None


# ─── v28：人效（分母取最接近报告期的员工数）─────────────────────────

def test_per_employee_uses_closest_headcount(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """人均指标的分母必须取「观测日最接近报告期」的员工数。

    若一律用当前快照，历史期就会变成「今天的员工数 ÷ 当年的收入」
    （ops-knowledge-base D23 同类口径错误）。这里给两个观测点：
      2015-12-31 → 100 人（历史，应被 2015 报告期选中）
      2026-09-17 → 500 人（当前快照，应被最近报告期选中）
    """
    seed = Seed(duckdb_store)
    seed.income("600020", "2015-12-31", revenue=10000.0, net_profit=1000.0)
    seed.income("600020", "2024-12-31", revenue=50000.0, net_profit=5000.0)
    seed.balance("600020", "2015-12-31", total_assets=1000.0)
    seed.balance("600020", "2024-12-31", total_assets=2000.0)
    seed.headcounts("600020", [("2015-12-31", 100), ("2026-09-17", 500, "eastmoney_f10")])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    rows = {
        str(r["report_date"]): r
        for r in duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600020'")
    }
    hist = rows["2015-12-31"]
    assert hist["employee_count"] == 100.0              # 时点员工数，不是 500
    assert hist["revenue_per_employee"] == 100.0        # 10000 / 100
    assert hist["profit_per_employee"] == 10.0          # 1000 / 100
    latest = rows["2024-12-31"]
    # 2024-12-31 距 2015-12-31 为 3287 天、距 2026-09-17 为 625 天 → 选当前快照 500 人
    assert latest["employee_count"] == 500.0
    assert latest["revenue_per_employee"] == 100.0      # 50000 / 500


def test_per_employee_null_without_headcount(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """没有任何员工数记录时，人均指标必须 NULL，不得用 1 或 0 冒充。"""
    seed = Seed(duckdb_store)
    seed.income("600021", REPORT_DATE, revenue=100.0, net_profit=10.0)
    seed.balance("600021", REPORT_DATE, total_assets=500.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600021'")[0]
    assert row["employee_count"] is None
    assert row["revenue_per_employee"] is None
    assert row["profit_per_employee"] is None


# ─── v30：时点市值与估值衍生（绕开 D23）──────────────────────────────

def test_market_cap_uses_report_date_price_and_shares(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """报告期市值 = 报告期当日原始收盘价 × 报告期时点股本。

    这是对 ops-knowledge-base **D23**（历史市值误用当前股本）的正面绕开：
    若用 stock_meta.total_shares（当前股本）或最新收盘价，历史估值全错。
    构造三个干扰项：
      · 报告期收盘价 10，最新收盘价 99（应取 10）
      · 报告期时点股本 100，报告期后增至 400（应取 100）
    """
    seed = Seed(duckdb_store)
    seed.income("600030", REPORT_DATE, revenue=1000.0, net_profit=100.0, income_tax=0.0,
                financial_expenses=0.0, total_profit=100.0)
    seed.balance("600030", REPORT_DATE, total_assets=5000.0, total_liabilities=2000.0,
                 total_equity=3000.0, total_equity_parent=3000.0, short_term_loans=0.0,
                 long_term_loans=0.0, bonds_payable=0.0, monetary_funds=500.0)
    seed.shares("600030", [("2000-01-01", 100.0), ("2099-01-01", 400.0)])
    seed.prices("600030", [("2019-12-31", 8.0), ("2024-12-31", 10.0), ("2026-09-17", 99.0)])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600030'")[0]
    assert row["report_date_close"] == 10.0            # 报告期当日价，不是最新价 99
    assert row["market_cap_at_report"] == 1000.0       # 10 × 100（时点股本），不是 10 × 400
    # 托宾Q = (1000 + 2000) / 5000 = 0.6
    assert abs(row["tobin_q"] - 0.6) < 1e-9
    # 账面市值比 = 3000 / 1000 = 3
    assert abs(row["book_to_market"] - 3.0) < 1e-9


def test_valuation_null_without_price(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """没有报告期价格时，市值与全套估值衍生必须 NULL，不得用最新价冒充。"""
    seed = Seed(duckdb_store)
    seed.income("600031", REPORT_DATE, revenue=1000.0, net_profit=100.0)
    seed.balance("600031", REPORT_DATE, total_assets=5000.0, total_liabilities=2000.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600031'")[0]
    assert row["report_date_close"] is None
    assert row["market_cap_at_report"] is None
    assert row["tobin_q"] is None
    assert row["book_to_market"] is None


# ─── v32：每股净资产口径修正（交叉核验驱动）────────────────────────

def test_bps_uses_total_equity_and_bps_parent_uses_parent_equity(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """2026-09-19 用 CSMAR FI_T9 交叉核验发现：本项目 bps 名为「每股净资产」
    却误用归母权益，一致率仅 25%。修正后两个概念各自成列：

      bps        每股净资产         = 股东权益合计 / 股数（CSMAR F091001A，95.03%）
      bps_parent 归属母公司每股净资产 = 归母权益   / 股数（CSMAR F091701A，95.04%）

    本测试用「有少数股东权益」的场景把两者区分开（无少数股东时二者相等，
    测不出回归）。
    """
    seed = Seed(duckdb_store)
    seed.income("600040", REPORT_DATE, revenue=1000.0)
    seed.balance("600040", REPORT_DATE, total_assets=5000.0, total_equity=1200.0,
                 total_equity_parent=1000.0, minority_interest=200.0)
    seed.shares("600040", [("2000-01-01", 100.0)])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600040'")[0]
    assert row["bps"] == 12.0          # 1200 / 100（股东权益合计，含少数股东）
    assert row["bps_parent"] == 10.0   # 1000 / 100（归母权益）


def test_per_share_history_domain_not_exposed_to_screening() -> None:
    """FI_T9 历史域**本身**不进筛选字段表（同概念两套口径会造成伪选择）；
    但其中「有价值且可自算」的概念已在 v37 用本项目数据自算后暴露
    （如 tangible_asset_per_share）。"""
    from app.core.screening.engine import EXTENDED_COLUMNS

    assert "bps_parent" in EXTENDED_COLUMNS          # 自算列可用
    # 2026-09-19 v37 起 tangible_asset_per_share 也已由本项目自算并上界面
    # （v37 的定位就是「把 CSMAR 有价值的概念自算出来」），故此处改为断言
    # FI_T9 里**仍有未自算**的独有列不暴露。
    assert "operating_profit_per_share" not in EXTENDED_COLUMNS
    assert "ebit_per_share" not in EXTENDED_COLUMNS


# ─── v37：CSMAR 有价值新概念的自算落地 ──────────────────────────────

def test_v37_solvency_and_cash_quality_indicators(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """v37 的偿债细分 / 现金质量 / 应计 / 每股明细，公式取自 CSMAR 说明书，
    但用本项目数据自算 —— 因此覆盖最新报告期、可直接进筛选界面。"""
    seed = Seed(duckdb_store)
    seed.income("600050", REPORT_DATE, revenue=1000.0, net_profit=100.0, income_tax=0.0,
                financial_expenses=0.0, total_profit=100.0, operating_profit=120.0)
    seed.balance("600050", REPORT_DATE, total_assets=5000.0, total_liabilities=2000.0,
                 total_equity=3000.0, total_current_liabilities=800.0, monetary_funds=300.0,
                 notes_receivable=50.0, accounts_receivable=150.0, intangible_assets=200.0,
                 goodwill=100.0, capital_reserve=400.0)
    seed.cash_flow("600050", REPORT_DATE, cf_from_operating=500.0, cash_ending=250.0,
                   cash_received_sales=1100.0)
    seed.shares("600050", [("2000-01-01", 100.0)])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600050'")[0]
    assert row["cash_ratio"] == 250.0 / 800.0                  # 现金及等价物期末 / 流动负债
    assert row["conservative_quick_ratio"] == (300 + 50 + 150) / 800.0
    assert row["debt_to_equity"] == 2000.0 / 3000.0            # 负债合计 / 所有者权益
    assert row["tangible_net_debt_ratio"] == 2000.0 / (3000 - 200 - 100)
    assert row["ocf_to_liabilities"] == 500.0 / 2000.0
    assert row["cash_content_of_revenue"] == 1100.0 / 1000.0   # 销售收现 / 营业收入
    assert row["ocf_to_operating_profit"] == 500.0 / 120.0     # 经营现金流 / 营业利润
    assert row["accruals"] == (100.0 - 500.0) / 5000.0         # (净利 − 经营现金流)/总资产
    assert row["tangible_asset_per_share"] == (5000 - 200 - 100) / 100
    assert row["liability_per_share"] == 2000.0 / 100
    assert row["capital_reserve_per_share"] == 400.0 / 100


def test_loss_making_has_null_ocf_to_operating_profit(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """营业利润 <= 0 时「营业利润现金净含量」无业务含义 → NULL。"""
    seed = Seed(duckdb_store)
    seed.income("600051", REPORT_DATE, revenue=1000.0, operating_profit=-50.0)
    seed.balance("600051", REPORT_DATE, total_assets=5000.0)
    seed.cash_flow("600051", REPORT_DATE, cf_from_operating=500.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600051'")[0]
    assert row["ocf_to_operating_profit"] is None


# ─── v37 指标护栏（2026-09-19 复审补加）─────────────────────────────

def test_v37_guards_bound_business_meaningless_extremes(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """复审发现 v37 的 12 列**漏加护栏**，实测出现 cash_ratio 最大 21 万、
    accruals 最大 2.4 万（其数学上限本是 ±2）等业务无意义极值。
    本测试用「分母趋 0」的种子把护栏钉死。"""
    seed = Seed(duckdb_store)
    # 流动负债 0.01、现金 100 万 → 现金比率 1 亿倍（应被护栏置空）
    seed.income("600060", REPORT_DATE, revenue=1000.0, net_profit=100.0, operating_profit=100.0)
    seed.balance("600060", REPORT_DATE, total_assets=5000.0, total_liabilities=2000.0,
                 total_equity=3000.0, total_current_liabilities=0.01, monetary_funds=1000000.0)
    seed.cash_flow("600060", REPORT_DATE, cf_from_operating=300.0, cash_ending=1000000.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600060'")[0]
    assert row["cash_ratio"] is None                 # 1 亿倍 > 100 上限
    assert row["conservative_quick_ratio"] is None
    assert row["debt_to_equity"] is not None          # 2000/3000 正常，保留
    # 应计项目 = (100 − 300)/5000 = -0.04，在 ±2 内，保留
    assert row["accruals"] is not None


def test_accruals_is_bounded_by_two(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """应计项目 = (净利 − 经营现金流) / 总资产，数学上限是 ±2。
    复审前实测出现 23,509 —— 说明总资产极小导致发散，护栏必须拦住。"""
    seed = Seed(duckdb_store)
    seed.income("600061", REPORT_DATE, revenue=1.0, net_profit=-1000.0)
    seed.balance("600061", REPORT_DATE, total_assets=0.01)
    seed.cash_flow("600061", REPORT_DATE, cf_from_operating=0.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600061'")[0]
    assert row["accruals"] is None   # -100000 远超 ±2


def test_negative_equity_yields_null_debt_ratios(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """净资产为负时，产权比率与有形净值债务率都没有业务含义 → NULL。"""
    seed = Seed(duckdb_store)
    seed.income("600062", REPORT_DATE, revenue=100.0)
    seed.balance("600062", REPORT_DATE, total_assets=100.0, total_liabilities=300.0, total_equity=-200.0)
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600062'")[0]
    assert row["debt_to_equity"] is None
    assert row["tangible_net_debt_ratio"] is None


# ─── v38：事件类指标（把「在库没用到」的数据落地）───────────────────

def test_v38_event_indicators(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """近3年融资稀释 / 回购占比 / 送转 / 最大业务占比。

    系统性审查发现 funding_events / buyback_events / xdxr / business_breakdown
    四份数据（约 103 万行）「在库、进包，但零派生指标」。
    """
    seed = Seed(duckdb_store)
    seed.income("600070", REPORT_DATE, revenue=1000.0)
    seed.balance("600070", REPORT_DATE, total_assets=5000.0)
    seed.shares("600070", [("2000-01-01", 1000.0)])
    # 近 3 年内融资发行 200 股；窗口外（5 年前）发行 900 股应被排除
    seed.fundings("600070", [("a_placement", "2023-06-30", 200), ("ipo", "2014-01-01", 900)])
    seed.buyback("600070", "2023-01-01", 50)
    # 送转：xdxr.songzhuangu 存的是**每股**送转比例（D53 / reports/139 §5.3），
    # 所以「每 10 股送 2 股」写入 0.2（不是 2.0），累计比例仍是 0.2。
    seed.bonus("600070", "2023-05-01", 0.2)
    # 三段业务：1000 / 600 / 400 → 最大占比 0.5；另加一个负数段不应破坏占比
    seed.segments("600070", "2023-12-31",
                  [("A", 1000, 50), ("B", 600, 30), ("C", 400, 20), ("D", -200, -10)])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600070'")[0]
    assert row["dilution_3y"] == 200 / 1000        # 只统计窗口内的 200，不含 5 年前的 900
    assert row["buyback_ratio_3y"] == 50 / 1000
    assert row["bonus_share_ratio_3y"] == 0.2      # 0.2 股/股 = 每 10 股送 2 股（每股口径）
    # 只在正收入段上算：1000 / (1000+600+400) = 0.5（负数段不影响）
    assert row["top_segment_share"] == 0.5


def test_v38_no_financing_means_zero_dilution(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """窗口内无融资事件 = 零稀释。funding_events 含 IPO（覆盖 100% 在市股票），
    故「窗口内无记录」等价于「窗口内无融资」，应记 0 而不是 NULL
    （否则「近3年无稀释」这个筛选条件会因 NULL 而失效）。"""
    seed = Seed(duckdb_store)
    seed.income("600071", REPORT_DATE, revenue=100.0)
    seed.balance("600071", REPORT_DATE, total_assets=5000.0)
    seed.shares("600071", [("2000-01-01", 1000.0)])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600071'")[0]
    assert row["dilution_3y"] == 0.0
    assert row["buyback_ratio_3y"] == 0.0


def test_top_segment_share_never_exceeds_one(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """回归：分业务收入可为负，用 MAX/SUM(全量) 会得出 >1 的伪占比
    （真实库实测最大 11.36）。改为只在正收入段上计算，保证 ≤ 1。"""
    seed = Seed(duckdb_store)
    seed.income("600072", REPORT_DATE, revenue=100.0)
    seed.balance("600072", REPORT_DATE, total_assets=5000.0)
    seed.segments("600072", "2023-12-31", [("A", 100, 100), ("B", -90, -90)])
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '600072'")[0]
    assert row["top_segment_share"] == 1.0   # 而非 100/(100-90) = 10
