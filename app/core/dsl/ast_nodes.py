"""DSL AST 节点定义 (含维度元数据)

每个节点携带维度信息用于校验 (PRD §11.4):
- unit: CNY | ratio | percent | count | mixed
- period_type: cumulative | single_quarter | ttm | point_in_time
- historical_capable: bool (PRD §11.3: 自动推导)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.dsl.derived_fields import field_metadata as _derived_field_metadata
from app.core.dsl.derived_fields import series_break_metadata as _series_break_metadata

Unit = Literal["CNY", "ratio", "percent", "count", "mixed", "unknown"]
PeriodType = Literal["cumulative", "single_quarter", "ttm", "point_in_time", "current_only"]


@dataclass
class ASTNode:
    """AST 基类"""
    unit: Unit = "unknown"
    period_type: PeriodType = "point_in_time"
    historical_capable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "unit": self.unit,
            "period_type": self.period_type,
            "historical_capable": self.historical_capable,
        }


@dataclass
class Literal(ASTNode):
    """数字字面量"""
    value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["value"] = self.value
        return d


@dataclass
class FieldRef(ASTNode):
    """标准化财务字段引用, 如 balance.total_assets@TTM"""
    table: str = ""
    field: str = ""
    period: str = "LATEST"  # TTM/YoY/QoQ/MRQ/LATEST/CAGR

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update({"table": self.table, "field": self.field, "period": self.period})
        return d


@dataclass
class IndicatorRef(ASTNode):
    """内建/已发布指标引用, 如 pe_ttm"""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["name"] = self.name
        return d


@dataclass
class FuncCall(ASTNode):
    """函数调用, 如 rank(pe_ttm), TTM(income.revenue)"""
    func_name: str = ""
    args: list[ASTNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update({
            "func_name": self.func_name,
            "args": [a.to_dict() for a in self.args],
        })
        return d


@dataclass
class BinaryOp(ASTNode):
    """二元运算: +, -, *, /, >, <, AND, OR"""
    op: str = ""
    left: ASTNode | None = None
    right: ASTNode | None = None

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update({
            "op": self.op,
            "left": self.left.to_dict() if self.left else None,
            "right": self.right.to_dict() if self.right else None,
        })
        return d


@dataclass
class UnaryOp(ASTNode):
    """一元运算: 负号"""
    op: str = "-"
    operand: ASTNode | None = None

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update({
            "op": self.op,
            "operand": self.operand.to_dict() if self.operand else None,
        })
        return d


# ─── 字段维度元数据表 ─────────────────────────────────────────────
# 用于校验: 防止百分比与绝对金额比较、累计值与单季度值混用 (PRD §11.4)

FIELD_METADATA: dict[str, dict[str, Any]] = {
    # 资产负债表 (时点值, point_in_time)
    "balance.monetary_funds": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.accounts_receivable": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.inventory": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.total_current_assets": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.total_assets": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.total_current_liabilities": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.total_liabilities": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.total_equity": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.total_equity_parent": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.paid_in_capital": {"unit": "count", "period_type": "point_in_time", "historical_capable": True},
    "balance.goodwill": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.short_term_loans": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.long_term_loans": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.bonds_payable": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.fixed_assets": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    # v48 补采科目（reports/139 §11.4 / D58）：金额单位元、时点值、可看历史。
    # 现货率：`fixed_assets_gross`/`accumulated_depreciation` 只有中报与年报有值（Q1/Q3 结构性 NULL）。
    "balance.fixed_assets_gross": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.accumulated_depreciation": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.other_current_assets": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.receivables_financing": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.long_term_payables": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.investment_property": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.development_expenditure": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "balance.intangible_assets": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},

    # 利润表 (累计值, cumulative)
    "income.revenue": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.cost_of_revenue": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.gross_profit": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.operating_profit": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.net_profit": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.parent_net_profit": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.deducted_net_profit": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.basic_eps": {"unit": "ratio", "period_type": "cumulative", "historical_capable": True},
    "income.selling_expenses": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.administrative_expenses": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.financial_expenses": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.rd_expenses": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.interest_expense": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.investment_income": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.total_profit": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "income.income_tax": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},

    # 现金流量表 (累计值, cumulative)
    "cashflow.cf_from_operating": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "cashflow.cf_from_investing": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "cashflow.cf_from_financing": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
    "cashflow.cf_net": {"unit": "CNY", "period_type": "cumulative", "historical_capable": True},
}

# Keep the DSL registry aligned with every normalized statement column. The
# explicitly listed fields above carry exceptional units; all remaining
# standard statement values are monetary flow/point-in-time values.
for _table, _period_type, _fields in (
    ("balance", "point_in_time", (
        "trading_financial_assets", "notes_receivable", "prepayments", "other_receivables",
        "contract_assets", "long_term_equity_investment", "construction_in_progress",
        "right_of_use_assets", "deferred_tax_assets", "total_non_current_assets", "notes_payable",
        "accounts_payable", "prepayments_received", "contract_liabilities", "employee_benefits_payable",
        "taxes_payable", "total_non_current_liabilities", "lease_liabilities", "capital_reserve",
        "surplus_reserve", "undistributed_profit", "minority_interest",
    )),
    ("income", "cumulative", (
        "total_operating_revenue", "total_operating_cost", "taxes_and_surcharges", "interest_income",
        "asset_impairment_loss", "credit_impairment_loss", "exchange_gain", "non_operating_income",
        "non_operating_expenses", "minority_shareholder_profit", "diluted_eps",
    )),
    ("cashflow", "cumulative", (
        "cash_received_sales", "taxes_refunded", "other_operating_cf_in", "total_operating_cf_in",
        "cash_paid_goods", "cash_paid_employees", "cash_paid_taxes", "other_operating_cf_out",
        "total_operating_cf_out", "exchange_rate_effect", "cash_beginning", "cash_ending",
    )),
):
    for _field in _fields:
        FIELD_METADATA.setdefault(
            f"{_table}.{_field}",
            {"unit": "CNY", "period_type": _period_type, "historical_capable": True},
        )

for _field in (
    "core_tier1_capital_adequacy_ratio",
    "tier1_capital_adequacy_ratio",
    "capital_adequacy_ratio",
    "non_performing_loan_ratio",
    "provision_coverage_ratio",
    "risk_coverage_ratio",
):
    FIELD_METADATA[f"balance.{_field}"] = {
        "unit": "percent", "period_type": "point_in_time", "historical_capable": True,
    }

# ─── 跨年口径断点：派生字段 + 原始列断点登记（reports/131 §9.2 P0-7 / P0-8）──
# 派生字段（SQL 真源在 app/core/dsl/derived_fields.py，DSL 代码生成与筛选引擎共用）：
#   income.impairment_loss_positive           资产减值损失（损失为正，跨 2018 可比）
#   income.credit_impairment_loss_positive    信用减值损失（损失为正，跨 2018 可比）
#   balance.advance_from_customers            预收款项 + 合同负债（合并口径，跨 2020 连续）
#   balance.advance_from_customers_ratio      客户占款率 =(预收+合同负债)/总资产
#   balance.interest_bearing_debt             有息负债 = 短借+长借+应付债券（绝对值，元）
#   balance.interest_bearing_debt_ratio       有息负债率 = 有息负债/总资产（段永平口径）
#   balance.net_cash_to_assets                净现金/总资产 =(货币资金−有息负债)/总资产
#   balance.interest_bearing_debt_to_liabilities  有息负债/总负债（负债质量，段永平口径）
# 原始列（income.asset_impairment_loss / credit_impairment_loss /
# balance.prepayments_received / contract_liabilities）**不删不改**，只加
# `series_break` 元数据：校验器据此给出"跨断点不可直接比较"的 warning，
# 并指向对应的派生字段。整列标 deprecated 会让断点另一侧的历史查询全部失效。
for _derived_key, _derived_meta in _derived_field_metadata().items():
    FIELD_METADATA[_derived_key] = _derived_meta
for _break_key, _break_meta in _series_break_metadata().items():
    _break_entry = FIELD_METADATA.get(_break_key)
    if _break_entry is not None:  # 原始列必须已在上面登记过
        _break_entry["series_break"] = _break_meta

# 内建指标维度元数据
#
# ⚠️ `unit` 只描述**存储量纲**，不是展示格式（2026-11-25，reports/167）：
#   - `pct` / `ratio`  无量纲、存小数（0.2034 = 20.34%）：
#                      roe / debt_ratio / dividend_yield / revenue_cagr5 …
#   - `percent`        无量纲、存百分数（20.34 = 20.34%）：
#                      ttm_dividend_yield / div_yield_spread_* / turnover_rate /
#                      银行监管比率（core_tier1_capital_adequacy_ratio …）
#   - `CNY` / `count`  金额 / 股数
# `pct` 与 `ratio` 差别只在展示形态，维度上等价（校验器按等价类比较，见
# `validator.DIMENSION_CLASS`）；`percent` 是**另一种量纲**，与它们差 100 倍，
# 直接比较必须报错。
#
# 存储口径的真源是 `app/core/screening/field_units.py`（PCT_DECIMAL_FIELDS /
# PERCENT_STORED_FIELDS / RATIO_FIELDS），这里是 DSL 侧镜像。历史漂移的代价：
# 曾有 13 个指标声明 `percent` 却存小数，于是 `dividend_yield > ttm_dividend_yield`
# 这类跨量纲比较被**放行**并静默差 100 倍。加字段时先查 field_units 再写这里。
INDICATOR_METADATA: dict[str, dict[str, Any]] = {
    # 估值 (current_only, 依赖最新收盘价)
    "pe_ttm": {"unit": "ratio", "period_type": "current_only", "historical_capable": False},
    "pb_mrq": {"unit": "ratio", "period_type": "current_only", "historical_capable": False},
    "ps_ttm": {"unit": "ratio", "period_type": "current_only", "historical_capable": False},
    "pcf_ttm": {"unit": "ratio", "period_type": "current_only", "historical_capable": False},
    "dividend_yield": {"unit": "pct", "period_type": "current_only", "historical_capable": False},
    "total_market_cap": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "circ_market_cap": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    # 盈利
    "roe": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    "roa": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    "gross_margin": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    "net_margin": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    "roic": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    # ROCE（schema v42，reports/130）：口径 C，年报序列中位数，不剔现金、无护栏
    "roce_latest": {"unit": "ratio", "period_type": "current_only", "historical_capable": True},
    "roce_5y_median": {"unit": "ratio", "period_type": "current_only", "historical_capable": True},
    "roce_10y_median": {"unit": "ratio", "period_type": "current_only", "historical_capable": True},
    "roce_5y_years": {"unit": "count", "period_type": "current_only", "historical_capable": True},
    "roce_20y_median": {"unit": "ratio", "period_type": "current_only", "historical_capable": True},
    "roce_10y_years": {"unit": "count", "period_type": "current_only", "historical_capable": True},
    "roce_20y_years": {"unit": "count", "period_type": "current_only", "historical_capable": True},
    "roce_eligible": {"unit": "count", "period_type": "current_only", "historical_capable": False},
    "cf_to_net_profit": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    # 成长
    # 成长（2026-11-16 reports/131 §9.2 P0-4）：同比字段改 TTM 口径写入，
    # 与详情页 metric-history 的 ttm 分支同源；年报口径另存 `*_yoy_fy`（快照列，
    # 暂不作为可筛 DSL 指标暴露）。period_type 必须同步声明为 ttm，否则
    # 校验层会把它当成"仅当前值"而拒绝合法的 TTM 组合。
    "revenue_yoy": {"unit": "pct", "period_type": "ttm", "historical_capable": True},
    "net_profit_yoy": {"unit": "pct", "period_type": "ttm", "historical_capable": True},
    "deducted_profit_yoy": {"unit": "pct", "period_type": "ttm", "historical_capable": True},
    "revenue_cagr3": {"unit": "pct", "period_type": "current_only", "historical_capable": True},
    "revenue_cagr5": {"unit": "pct", "period_type": "current_only", "historical_capable": True},
    "net_profit_cagr3": {"unit": "pct", "period_type": "current_only", "historical_capable": True},
    "net_profit_cagr5": {"unit": "pct", "period_type": "current_only", "historical_capable": True},
    "deducted_profit_cagr3": {"unit": "pct", "period_type": "current_only", "historical_capable": True},
    "deducted_profit_cagr5": {"unit": "pct", "period_type": "current_only", "historical_capable": True},
    # 安全
    "debt_ratio": {"unit": "ratio", "period_type": "point_in_time", "historical_capable": True},
    "current_ratio": {"unit": "ratio", "period_type": "point_in_time", "historical_capable": True},
    "quick_ratio": {"unit": "ratio", "period_type": "point_in_time", "historical_capable": True},
    "interest_bearing_debt": {"unit": "CNY", "period_type": "point_in_time", "historical_capable": True},
    "interest_coverage": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    "goodwill_ratio": {"unit": "ratio", "period_type": "point_in_time", "historical_capable": True},
    # 股东回报
    "payout_ratio": {"unit": "ratio", "period_type": "ttm", "historical_capable": True},
    "dps": {"unit": "CNY", "period_type": "current_only", "historical_capable": True},
    "consecutive_div_years": {"unit": "count", "period_type": "current_only", "historical_capable": True},
    # 分红融资比数据前置（2026-08-25）
    "cumulative_dividend_amount": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "cumulative_financing_amount": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "dividend_financing_ratio_pct": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    # 行情
    "latest_close": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "turnover_rate": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "ma5": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "ma10": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "ma20": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "ma60": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "ma120": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "ma250": {"unit": "CNY", "period_type": "current_only", "historical_capable": False},
    "avg_volume": {"unit": "count", "period_type": "current_only", "historical_capable": False},
    "period_return": {"unit": "pct", "period_type": "current_only", "historical_capable": False},
    "annualized_volatility": {"unit": "pct", "period_type": "current_only", "historical_capable": False},
    "max_drawdown": {"unit": "pct", "period_type": "current_only", "historical_capable": False},
    # 国债基准与股息率利差（P3，reports/68；当前值，历史统计属 P4）
    "ttm_dividend_yield": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_0p25y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_0p5y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_1y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_2y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_3y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_5y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_7y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_10y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
    "div_yield_spread_30y": {"unit": "percent", "period_type": "current_only", "historical_capable": False},
}
