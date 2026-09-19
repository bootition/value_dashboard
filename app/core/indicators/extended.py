"""扩展指标域构建器（`indicator_ext`，schema v25，2026-09-19）。

定位
----
本项目原有的 `indicator_snapshot`（主链快照）只覆盖约 20 个价值投资核心指标，
**完全缺失**三族常用指标：

1. **周转率族**（应收账款/存货/应付账款/流动资产/固定资产/总资产/股东权益周转率、营业周期）
   —— 判断「生意质量」的核心：存货是否积压、是否被客户占款、能占用供应商多久。
2. **自由现金流族**（折旧摊销、资本支出、自由现金流、自由现金流率）
   —— 判断「利润是不是真钱」。此前**无法编制**，因为本项目只导入了直接法
   现金流量表的主干科目，既无折旧摊销（间接法）也无资本支出（投资活动）。
3. **杠杆族**（EBIT、EBITDA、财务杠杆、经营杠杆、综合杠杆）
   —— 判断「赚的时候放大多少、亏的时候放大多少」。

数据来源（依赖链）
------------------
    balance_sheet ────┐
    income_statement ─┼→ 周转率族、EBIT
    cash_flow ────────┤→ 经营现金流
    cash_flow_indirect ┤→ 折旧摊销 ─┐
    cash_flow_activity ┘→ 资本支出 ─┴→ EBITDA、杠杆族、自由现金流

其中 `cash_flow_indirect` / `cash_flow_activity` 由两个来源共同填充：
CSMAR C17（1997Q2–2025Q1）与东方财富 F10（2025Q2 起，见
`scripts/fetch_cashflow_supplement.py`），按 (stock_code, report_date) 行级共存。

口径裁定（config/csmar_field_verdict.json）
------------------------------------------
- 周转率族 = green：CSMAR FI_T4 的公式只使用本项目已有科目 → **自算**
- 杠杆族 / EBITDA = yellow：公式透明但依赖折旧摊销 → **自算**，不直接采用 CSMAR 值
- 自由现金流 = yellow：CSMAR 的企业自由现金流公式含未定义的「息前税后利润」，
  故本表采用**本项目口径**：`经营现金流净额 − 资本支出`

业务无意义护栏
--------------
与 `calculator.py` 的 `pe<=1000` / `pb<=200` / `|roe|<=1` 同思路：
触发护栏时写 NULL，宁可如实缺失也不展示会误导人的数字。见下方常量。

域纪律
------
- **不写 `indicator_snapshot`**，不触碰主链口径与 readiness 门禁；
- 输入缺失时对应列为 NULL（如实缺失，**不做估算、不回退**）；
- 每次构建按 (stock_code, report_date) 全量替换，原子性由单条事务保证。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.update_lock import exclusive_update

logger = logging.getLogger(__name__)

DATA_VERSION = 1
SOURCE = "derived_calculator"

# ─── 业务无意义护栏 ────────────────────────────────────────────────
# 1) 自由现金流率：金融业（银行/券商/保险）的经营现金流包含客户资金与存贷款
#    净变动，除以营业收入会得到 1000%+ 的荒谬值（2026-09-19 实测 600061
#    国投资本 = 1302.45，即 130245%）。|FCF率| > 500% 判为业务无意义。
FCF_MARGIN_MAX_ABS = 5.0
# 2) 营业周期：周转率接近 0 时天数发散；超过 10 年（3650 天）判为无意义。
OPERATING_CYCLE_MAX_DAYS = 3650.0
# 3) 杠杆族：EBIT 或利润总额 ≤ 0（亏损）时，财务/经营/综合杠杆的符号与数值
#    都没有业务含义（2026-09-19 实测 504 只亏损股出现负的经营杠杆）。
# 4) 杠杆数值上限：分母（EBIT 或利润总额）为正但极小时杠杆会发散
#    （实测最大值：经营杠杆 190348、财务杠杆 33332、综合杠杆 77514）。
#    分布为 中位 1.1-1.5 / p99 8-22 / p99.9 47-133，|值|>100 已属病理值。
#    注意必须用 ABS 判定：负值（EBIT 为负而利润总额为正）会绕过 `<= cap`。
LEVERAGE_MAX = 100.0
# 5) 费用率：分母（营业收入）极小时比率发散；|费用率| > 500% 判为无意义。
EXPENSE_RATIO_MAX_ABS = 5.0
# 6) 权益乘数 = 总资产/总权益；权益趋 0 时发散。>100 判为无意义（净资产为负时
#    乘数为负，同样无业务含义，故用 ABS）。
EQUITY_MULTIPLIER_MAX = 100.0

# 周转率族：累计损益 ÷ 期末余额（CSMAR FI_T4「A」式）
TURNOVER_RATIOS: tuple[tuple[str, str, str], ...] = (
    ("receivables_turnover", "i.revenue", "b.accounts_receivable"),
    ("inventory_turnover", "i.cost_of_revenue", "b.inventory"),
    ("accounts_payable_turnover", "i.cost_of_revenue", "b.accounts_payable"),
    ("current_asset_turnover", "i.revenue", "b.total_current_assets"),
    ("fixed_asset_turnover", "i.revenue", "b.fixed_assets"),
    ("total_asset_turnover", "i.revenue", "b.total_assets"),
    ("equity_turnover", "i.revenue", "b.total_equity"),
)

# 报告期累计天数（营业周期用；与 CSMAR「计算期天数」口径一致）
_PERIOD_DAYS = """
    CASE strftime(i.report_date, '%m-%d')
        WHEN '12-31' THEN 365 WHEN '06-30' THEN 181
        WHEN '09-30' THEN 273 WHEN '03-31' THEN 90
        ELSE CAST(dayofyear(i.report_date) AS DOUBLE)
    END
"""

# EBIT = 净利润 + 所得税费用 + 财务费用（CSMAR FI_T5.F050601B 定义）
_EBIT = "(i.net_profit + i.income_tax + i.financial_expenses)"
# 折旧摊销 = 固定资产折旧 + 无形资产摊销 + 长期待摊费用摊销（CSMAR FI_T6.F061201B 定义）
_DEPRECIATION = """(COALESCE(ci.fixed_asset_depreciation, 0)
                    + COALESCE(ci.intangible_asset_amortization, 0)
                    + COALESCE(ci.long_term_prepaid_amortization, 0))"""
_EBIT_READY = ("i.net_profit IS NOT NULL AND i.income_tax IS NOT NULL "
               "AND i.financial_expenses IS NOT NULL")
_DEP_READY = ("(ci.fixed_asset_depreciation IS NOT NULL "
              "OR ci.intangible_asset_amortization IS NOT NULL "
              "OR ci.long_term_prepaid_amortization IS NOT NULL)")
_CYCLE_DAYS = (f"(({_PERIOD_DAYS}) / NULLIF(i.revenue / NULLIF(b.accounts_receivable, 0), 0)"
               f" + ({_PERIOD_DAYS}) / NULLIF(i.cost_of_revenue / NULLIF(b.inventory, 0), 0))")


def build_select_sql(codes: list[str] | None = None) -> str:
    """生成 indicator_ext 的构建 SELECT。

    以 income_statement × balance_sheet 内连接为骨架（两者都有才计算），
    再左连接现金流三张表——现金流缺失时相关列自然为 NULL。
    """
    turnover_cols = ",\n        ".join(
        f"CASE WHEN {den} IS NULL OR {den} = 0 THEN NULL ELSE ({num}) / ({den}) END AS {alias}"
        for alias, num, den in TURNOVER_RATIOS
    )
    expense_ratios = ", ".join(
        f"CASE WHEN i.revenue IS NULL OR i.revenue = 0 THEN NULL "
        f"WHEN ABS((i.{col}) / i.revenue) > {EXPENSE_RATIO_MAX_ABS} THEN NULL "
        f"ELSE (i.{col}) / i.revenue END AS {alias}"
        for alias, col in (
            ("selling_expense_ratio", "selling_expenses"),
            ("admin_expense_ratio", "administrative_expenses"),
            ("rd_expense_ratio", "rd_expenses"),
            ("finance_expense_ratio", "financial_expenses"),
        )
    )
    code_filter = ""
    if codes:
        quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in codes)
        code_filter = f"\n      AND i.stock_code IN ({quoted})"

    return f"""
    INSERT INTO indicator_ext BY NAME
    SELECT
        i.stock_code,
        i.report_date,
        {turnover_cols},
        CASE WHEN ({_CYCLE_DAYS}) > {OPERATING_CYCLE_MAX_DAYS} THEN NULL
             ELSE ({_CYCLE_DAYS}) END AS operating_cycle_days,
        CASE WHEN {_DEP_READY} THEN {_DEPRECIATION} END AS depreciation_amortization,
        ca.capex,
        c.cf_from_operating AS operating_cash_flow,
        CASE WHEN c.cf_from_operating IS NULL OR ca.capex IS NULL THEN NULL
             ELSE c.cf_from_operating - ca.capex END AS free_cash_flow,
        CASE WHEN c.cf_from_operating IS NULL OR ca.capex IS NULL
                  OR i.revenue IS NULL OR i.revenue = 0 THEN NULL
             WHEN ABS((c.cf_from_operating - ca.capex) / i.revenue) > {FCF_MARGIN_MAX_ABS} THEN NULL
             ELSE (c.cf_from_operating - ca.capex) / i.revenue END AS fcf_margin,
        CASE WHEN {_EBIT_READY} THEN {_EBIT} END AS ebit,
        CASE WHEN {_EBIT_READY} AND {_DEP_READY} THEN {_EBIT} + {_DEPRECIATION} END AS ebitda,
        -- 财务杠杆 = EBIT / 利润总额（CSMAR FI_T7.F070101B）；亏损时无业务含义
        CASE WHEN {_EBIT_READY} AND i.total_profit > 0 AND {_EBIT} > 0
                  AND ABS({_EBIT} / i.total_profit) <= {LEVERAGE_MAX}
             THEN {_EBIT} / i.total_profit END AS leverage_financial,
        -- 经营杠杆 = EBITDA / EBIT（CSMAR FI_T7.F070201B）；EBIT<=0 时无业务含义
        CASE WHEN {_EBIT_READY} AND {_DEP_READY} AND {_EBIT} > 0
                  AND ABS(({_EBIT} + {_DEPRECIATION}) / {_EBIT}) <= {LEVERAGE_MAX}
             THEN ({_EBIT} + {_DEPRECIATION}) / {_EBIT} END AS leverage_operating,
        -- 综合杠杆 = EBITDA / 利润总额（CSMAR FI_T7.F070301B）；亏损时无业务含义
        CASE WHEN {_EBIT_READY} AND {_DEP_READY} AND i.total_profit > 0
                  AND ABS(({_EBIT} + {_DEPRECIATION}) / i.total_profit) <= {LEVERAGE_MAX}
             THEN ({_EBIT} + {_DEPRECIATION}) / i.total_profit END AS leverage_total,
        -- ─── 每股族（v26）────────────────────────────────────────────
        -- 分母用「当时股数」：share_capital_history 中 effective_date <= 报告期的
        -- 最近一笔。**不得**用 stock_meta.total_shares（当前股本），否则历史期
        -- 每股指标会被后期增发/回购污染（ops-knowledge-base D23 同类口径问题）。
        CASE WHEN sh.total_shares > 0 THEN
            COALESCE(b.total_equity_parent, b.total_equity) / sh.total_shares END AS bps,
        CASE WHEN sh.total_shares > 0 THEN i.revenue / sh.total_shares END AS revenue_per_share,
        CASE WHEN sh.total_shares > 0 THEN c.cf_from_operating / sh.total_shares END AS ocf_per_share,
        CASE WHEN sh.total_shares > 0 THEN
            (COALESCE(b.surplus_reserve, 0) + COALESCE(b.undistributed_profit, 0)) / sh.total_shares
        END AS retained_earnings_per_share,
        -- ─── 费用率族（v26）──────────────────────────────────────────
        {expense_ratios},
        -- ─── 结构族（v26）───────────────────────────────────────────
        CASE WHEN b.total_assets > 0 AND b.total_current_assets IS NOT NULL
             THEN b.total_current_assets / b.total_assets END AS current_asset_ratio,
        CASE WHEN b.total_assets > 0 AND b.fixed_assets IS NOT NULL
             THEN b.fixed_assets / b.total_assets END AS fixed_asset_ratio,
        CASE WHEN b.total_equity IS NOT NULL AND b.total_equity <> 0
                  AND ABS(b.total_assets / b.total_equity) <= {EQUITY_MULTIPLIER_MAX}
             THEN b.total_assets / b.total_equity END AS equity_multiplier,
        CAST(? AS TIMESTAMP) AS calculated_at,
        ? AS source,
        ? AS data_version
    FROM income_statement i
    JOIN balance_sheet b
      ON b.stock_code = i.stock_code AND b.report_date = i.report_date
    LEFT JOIN cash_flow c
      ON c.stock_code = i.stock_code AND c.report_date = i.report_date
    LEFT JOIN cash_flow_indirect ci
      ON ci.stock_code = i.stock_code AND ci.report_date = i.report_date
    LEFT JOIN cash_flow_activity ca
      ON ca.stock_code = i.stock_code AND ca.report_date = i.report_date
    LEFT JOIN LATERAL (
        SELECT sch.total_shares FROM share_capital_history sch
        WHERE sch.stock_code = i.stock_code AND sch.effective_date <= i.report_date
        ORDER BY sch.effective_date DESC LIMIT 1
    ) sh ON true
    WHERE i.report_date IS NOT NULL{code_filter}
    """


class ExtendedIndicatorBuilder:
    """把周转率/自由现金流/杠杆三族写入 indicator_ext（独立低频域）。"""

    def __init__(self, duck: DuckDBStore, paths) -> None:
        self._duck = duck
        self._paths = paths

    def build(self, *, codes: list[str] | None = None) -> dict[str, Any]:
        """全量（或按指定股票）重建 indicator_ext。"""
        calculated_at = datetime.now(UTC).replace(tzinfo=None)
        sql = build_select_sql(codes)
        with exclusive_update(self._paths.duckdb_path), self._duck.write_connection() as conn:
            before = conn.execute("SELECT COUNT(*) FROM indicator_ext").fetchone()[0]
            if codes:
                quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in codes)
                conn.execute(f"DELETE FROM indicator_ext WHERE stock_code IN ({quoted})")
            else:
                conn.execute("DELETE FROM indicator_ext")
            conn.execute(sql, [calculated_at, SOURCE, DATA_VERSION])
            after = conn.execute("SELECT COUNT(*) FROM indicator_ext").fetchone()[0]
            filled = conn.execute(
                """SELECT COUNT(*) FILTER (WHERE free_cash_flow IS NOT NULL) AS fcf,
                          COUNT(*) FILTER (WHERE total_asset_turnover IS NOT NULL) AS turnover,
                          COUNT(*) FILTER (WHERE leverage_operating IS NOT NULL) AS lev,
                          MIN(report_date) AS d0, MAX(report_date) AS d1
                   FROM indicator_ext"""
            ).fetchone()
        report = {
            "status": "success",
            "rows_before": int(before),
            "rows_after": int(after),
            "rows_written": int(after - before) if not codes else int(after),
            "scope": "codes" if codes else "all",
            "filled": {
                "free_cash_flow": int(filled[0]),
                "total_asset_turnover": int(filled[1]),
                "leverage_operating": int(filled[2]),
            },
            "report_date_range": [str(filled[3]), str(filled[4])],
            "data_version": DATA_VERSION,
        }
        logger.info("indicator_ext 构建完成: %s", report)
        return report
