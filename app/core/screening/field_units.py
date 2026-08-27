"""字段单位元数据（单一来源，2026-08-14 红队 F3）。

筛选快照中两类百分比口径并存（历史事实，reports/80 簇 A/B/C）：
- `pct`      底层存小数比例（0.0529 = 5.29%）：条件输入需 ÷100，
             展示需 ×100 加 %（roe、net_profit_cagr3、period_return 等）。
- `percent`  底层存百分数（5.29 = 5.29%）：条件输入原样比较，
             展示直接加 %（ttm_dividend_yield、div_yield_spread_*、
             turnover_rate）。
- `ratio`    本身即倍数（流动比率等）；`price` 价格；其余 plain。

该映射通过 /api/screening/indicators 下发给前端，前端在
screening-format.ts 静态集合之外以运行时元数据覆盖（applyIndicatorUnits），
杜绝再次出现"前端集合与后端存储口径漂移"。

注意：改变任何字段的存储口径时，必须同步改本文件与 calculator 写入处，
否则会重新引入 100 倍误差。
"""

from __future__ import annotations

# 小数比例存储（与前端 screening-format.ts PCT_FIELDS 对齐）
PCT_DECIMAL_FIELDS: frozenset[str] = frozenset({
    "roe", "roa", "roic",
    "gross_margin", "net_margin", "debt_ratio",
    "revenue_yoy", "net_profit_yoy", "deducted_profit_yoy",
    "revenue_cagr3", "revenue_cagr5", "net_profit_cagr3", "net_profit_cagr5",
    "deducted_profit_cagr3", "deducted_profit_cagr5",
    "dividend_yield", "goodwill_ratio", "payout_ratio",
    "period_return", "annualized_volatility", "max_drawdown",
})

# 百分数原值存储（如 5.29 = 5.29%；与前端 PERCENT_FIELDS 对齐）
PERCENT_STORED_FIELDS: frozenset[str] = frozenset({
    "dividend_financing_ratio_pct",
    "ttm_dividend_yield",
    "div_yield_spread_0p25y", "div_yield_spread_0p5y", "div_yield_spread_1y",
    "div_yield_spread_2y", "div_yield_spread_3y", "div_yield_spread_5y",
    "div_yield_spread_7y", "div_yield_spread_10y", "div_yield_spread_30y",
    "turnover_rate",
    # 标准化资产负债表中的银行/证券监管比率按百分数原值存储（如 12 = 12%）
    "balance.core_tier1_capital_adequacy_ratio",
    "balance.tier1_capital_adequacy_ratio",
    "balance.capital_adequacy_ratio",
    "balance.non_performing_loan_ratio",
    "balance.provision_coverage_ratio",
    "balance.risk_coverage_ratio",
})

RATIO_FIELDS: frozenset[str] = frozenset({
    "current_ratio", "quick_ratio", "cf_to_net_profit", "interest_coverage",
})

PRICE_FIELDS: frozenset[str] = frozenset({
    "latest_close", "open", "high", "low", "close",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
})


def field_unit(field: str) -> str:
    """Return the unit class for a snapshot/indicator field.

    Values: pct | percent | price | ratio | plain（前端 FieldFormat 同义）。
    """
    if field in PERCENT_STORED_FIELDS:
        return "percent"
    if field in PCT_DECIMAL_FIELDS:
        return "pct"
    if field in PRICE_FIELDS:
        return "price"
    if field in RATIO_FIELDS:
        return "ratio"
    return "plain"
