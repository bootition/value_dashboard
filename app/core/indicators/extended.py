"""扩展指标域构建器（`indicator_ext`，schema v25，2026-09-19）。

定位
----
本项目原有的 `indicator_snapshot`（主链快照）只覆盖约 20 个价值投资核心指标，
**完全缺失**三族常用指标：

1. **周转率族**（应收账款/存货/应付账款/流动资产/固定资产/总资产/股东权益周转率、营业周期）
   —— 判断「生意质量」的核心：存货是否积压、是否被客户占款、能占用供应商多久。
2. **自由现金流族**（折旧摊销、资本支出、自由现金流、自由现金流率）
   —— 判断「利润是不是真钱」。此前**无法编制**，因为本项目只导入了直接法
   现金流量表的主干科目，既无折旧摊销（间接法）也无资本支出（直接法投资活动）。
3. **杠杆族**（EBIT、EBITDA、财务杠杆、经营杠杆、综合杠杆）
   —— 判断「赚的时候放大多少、亏的时候放大多少」。

数据来源（依赖链）
------------------
    balance_sheet ─┐
    income_statement ─┼→ 周转率族、EBIT
    cash_flow ────────┤→ 经营现金流
    cash_flow_indirect ┤→ 折旧摊销  ─┐
    cash_flow_activity ┘→ 资本支出  ─┴→ EBITDA、杠杆族、自由现金流

口径裁定（config/csmar_field_verdict.json）
------------------------------------------
- 周转率族 = green：CSMAR FI_T4 的公式只使用本项目已有科目 → **自算**
  （口径：累计损益 ÷ 期末余额，即 CSMAR「A」式）
- 杠杆族 / EBITDA = yellow：公式透明但依赖折旧摊销 → **自算**，不直接采用 CSMAR 值
- 自由现金流 = yellow：CSMAR 的企业自由现金流公式含未定义的「息前税后利润」，
  故本表采用**本项目口径**：`经营现金流净额 − 资本支出`，并在列注释中标注

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

# 周转率族：累计损益 ÷ 期末余额（CSMAR FI_T4「A」式）
# 每项为 (目标列, 分子表达式, 分母表达式)
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
_PERIOD_DAYS_SQL = """
    CASE strftime(i.report_date, '%m-%d')
        WHEN '12-31' THEN 365 WHEN '06-30' THEN 181
        WHEN '09-30' THEN 273 WHEN '03-31' THEN 90
        ELSE CAST(dayofyear(i.report_date) AS DOUBLE)
    END
"""


def _ratio(numerator: str, denominator: str, alias: str, multiplier: str = "1") -> str:
    """生成 NULL 安全的比率表达式：分母为 NULL 或 0 时结果为 NULL。"""
    return (
        f"CASE WHEN {denominator} IS NULL OR {denominator} = 0 THEN NULL "
        f"ELSE {multiplier} * ({numerator}) / ({denominator}) END AS {alias}"
    )


def build_select_sql(codes: list[str] | None = None) -> str:
    """生成 indicator_ext 的构建 SELECT。

    以 income_statement × balance_sheet 的内连接为骨架（两者都有才计算），
    再左连接现金流三张表——现金流缺失时相关列自然为 NULL。
    """
    turnover_cols = ",\n           ".join(
        _ratio(num, den, alias) for alias, num, den in TURNOVER_RATIOS
    )
    code_filter = ""
    if codes:
        quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in codes)
        code_filter = f"\n          AND i.stock_code IN ({quoted})"

    return f"""
    INSERT INTO indicator_ext BY NAME
    SELECT
        i.stock_code,
        i.report_date,
        {turnover_cols},
        -- 营业周期 = 应收周转天数 + 存货周转天数 = 计算期天数/周转率 之和
        CASE WHEN i.revenue IS NULL THEN NULL ELSE
            ({_PERIOD_DAYS_SQL}) / NULLIF(i.revenue / NULLIF(b.accounts_receivable, 0), 0)
          + ({_PERIOD_DAYS_SQL}) / NULLIF(i.cost_of_revenue / NULLIF(b.inventory, 0), 0)
        END AS operating_cycle_days,
        -- 折旧摊销（CSMAR FI_T6.F061201B 定义：固定资产折旧+无形资产摊销+长期待摊费用摊销）
        CASE WHEN ci.stock_code IS NULL THEN NULL ELSE
            COALESCE(ci.fixed_asset_depreciation, 0)
          + COALESCE(ci.intangible_asset_amortization, 0)
          + COALESCE(ci.long_term_prepaid_amortization, 0)
        END AS depreciation_amortization,
        ca.capex,
        c.cf_from_operating AS operating_cash_flow,
        -- 自由现金流（本项目口径：经营现金流净额 − 资本支出）
        CASE WHEN c.cf_from_operating IS NULL OR ca.capex IS NULL THEN NULL
             ELSE c.cf_from_operating - ca.capex END AS free_cash_flow,
        CASE WHEN c.cf_from_operating IS NULL OR ca.capex IS NULL
                  OR i.revenue IS NULL OR i.revenue = 0 THEN NULL
             ELSE (c.cf_from_operating - ca.capex) / i.revenue END AS fcf_margin,
        -- EBIT = 净利润 + 所得税费用 + 财务费用（CSMAR FI_T5.F050601B 定义）
        CASE WHEN i.net_profit IS NULL OR i.income_tax IS NULL OR i.financial_expenses IS NULL
             THEN NULL
             ELSE i.net_profit + i.income_tax + i.financial_expenses END AS ebit,
        -- EBITDA = EBIT + 折旧摊销（CSMAR FI_T5.F050801B 定义）
        CASE WHEN i.net_profit IS NULL OR i.income_tax IS NULL OR i.financial_expenses IS NULL
                  OR ci.stock_code IS NULL THEN NULL
             ELSE i.net_profit + i.income_tax + i.financial_expenses
                  + COALESCE(ci.fixed_asset_depreciation, 0)
                  + COALESCE(ci.intangible_asset_amortization, 0)
                  + COALESCE(ci.long_term_prepaid_amortization, 0) END AS ebitda,
        -- 财务杠杆 = EBIT / 利润总额（CSMAR FI_T7.F070101B 定义）
        {_ratio("i.net_profit + i.income_tax + i.financial_expenses", "i.total_profit", "leverage_financial")},
        -- 经营杠杆 = EBITDA / EBIT（CSMAR FI_T7.F070201B 定义）
        CASE WHEN i.net_profit IS NULL OR i.income_tax IS NULL OR i.financial_expenses IS NULL
                  OR ci.stock_code IS NULL THEN NULL
             WHEN (i.net_profit + i.income_tax + i.financial_expenses) = 0 THEN NULL
             ELSE (i.net_profit + i.income_tax + i.financial_expenses
                   + COALESCE(ci.fixed_asset_depreciation, 0)
                   + COALESCE(ci.intangible_asset_amortization, 0)
                   + COALESCE(ci.long_term_prepaid_amortization, 0))
                  / (i.net_profit + i.income_tax + i.financial_expenses) END AS leverage_operating,
        -- 综合杠杆 = EBITDA / 利润总额（CSMAR FI_T7.F070301B 定义）
        CASE WHEN i.net_profit IS NULL OR i.income_tax IS NULL OR i.financial_expenses IS NULL
                  OR ci.stock_code IS NULL OR i.total_profit = 0 THEN NULL
             ELSE (i.net_profit + i.income_tax + i.financial_expenses
                   + COALESCE(ci.fixed_asset_depreciation, 0)
                   + COALESCE(ci.intangible_asset_amortization, 0)
                   + COALESCE(ci.long_term_prepaid_amortization, 0)) / i.total_profit
        END AS leverage_total,
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
