"""指标空值原因清扫器：把「NULL」拆成 6 种可解释的原因。

为什么（docs/reports/119 §5）
---------------------------
`indicator_snapshot` / `indicator_ext` 的 NULL 混了 6 种含义，前端统一渲染成「—」。
清扫器负责**逐单元格**给出确定性原因并写入稀疏表 `indicator_null_reason`，
让「没有」和「坏了」第一次可分。

清扫范围（2026-11-14 裁决 2）
----------------------------
1. `snapshot` → `indicator_snapshot` 的 56 个可筛选字段；
2. `ext`      → `indicator_ext` 的 55 个可筛选字段；
3. `income` / `balance` / `cashflow` → 源三表字段，**带前缀**写入同一张稀疏表
   （`income.deducted_net_profit`、`balance.fixed_assets`、…，与 `missing_list`
   既有命名一致，不另建表）；
4. `cash_flow_indirect` / `cash_flow_activity` → 现金流量表补充资料两张表
   （2026-11-17，reports/135）。域内结构性缺失的列（`CASH_FLOW_STRUCTURAL_MISSING`）
   一律登记 `SOURCE_EMPTY` + 依据 detail，按 `report_date` 落库。
   为什么必须扩到源表：审计抓到的"有源、没调度"重灾区（扣非、固定资产、6 个银行
   监管指标）都发生在源表；只清扫指标表的话它们永远归因不到。
   源表同一 (股票, 报告期) 可能有多条 `report_type`，按 `any_value` 聚合——
   任何一条有值即视为"这个单元格有值"。

分类纪律（比覆盖率更重要）
--------------------------
**判不出就不写。** 每条规则都必须在正式库上验证过"精度"：既看命中数（有多少
NULL 能被解释），也看反例（字段有值却满足同一条件 ⇒ 说明这个条件不是真因）。
反例率高的规则一律不进本模块，宁可归入 `unclassified` 让人看见，也不制造假结论。

判定顺序（越具体越靠前，先命中先落库）
--------------------------------------
    1. NOT_APPLICABLE        该类公司本就没有这个指标（无分红/无商誉/亏损谈杠杆/
                             非金融公司谈资本充足率）
    2. INVALID_DENOMINATOR   分母 ≤0（权益为负、营收为 0）
    3. SOURCE_EMPTY          源不披露：源表行抓到了、但科目本身没有值
                             （三季报不披露现金流量表补充资料 ⇒ 折旧摊销整列为空）
    4. NOT_DISPATCHED(源没抓到) 折旧摊销族：该股该期连 `cash_flow_indirect` 行都没有
                             （输入缺失正是没抓到造成的，所以排在"该算没算"之前）
    5. NOT_COMPUTED          输入列为空 ⇒ 该算没算（detail 写明缺哪一列）；
                             或行情尾部完整却没算技术指标（detail 写明未重算）
    6. NOT_COMPUTED(报告期未对齐) ext 最新报告期 < snapshot 最新报告期、**且该字段
                             本期覆盖率 <50%** ⇒ 扩展指标整期未按新报告期重算
                             （2026-11-14 P0 的形态）。闸门理由见 `_misaligned_fields`。
    7. NOT_DISPATCHED(整期级) 本期覆盖率 ≤1%（或断崖式塌陷）、且近两期里有一期
                             覆盖率 >50% ⇒ 本期没人去抓（口径见 `_not_dispatched_fields`；
                             折旧摊销族被排除，改由第 3/6 条逐股判定）
    8. NOT_DISPATCHED(个股级) 6 个银行监管指标：该股上一期/再上一期有值、本期为空
                             （专抓 38 家银行这种"稀疏字段"，整期级永远凑不到 50%；
                              普通科目不套用，因为"上期有值本期 NULL"可能只是科目归零）
    9. 其余 → unclassified（不写库，进统计供人排查）

刻意不判的
----------
`MASKED_UNTRUSTED`：遮蔽的真值在 `app/core/data_quality.py::indicator_trust`，
清扫器再判一次必然与之打架，只在枚举里保留取值。

用法
----
    from app.core.quality.null_reason_sweeper import sweep

    result = sweep(store, domain="ext", write=False)   # 默认只读
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from app.core.quality.null_reasons import (
    DOMAIN_EXT,
    DOMAIN_SNAPSHOT,
    DOMAINS,
    SOURCE_DOMAIN_TABLES,
    SOURCE_DOMAINS,
    NullReason,
    NullReasonRow,
    domain_fields,
    upsert_reasons,
)
from app.core.sector import is_bank_sql, is_financial_sql
from app.core.storage.duckdb_store import DuckDBStore
from app.core.supplement import (
    CASH_FLOW_STRUCTURAL_MISSING,
    CASH_FLOW_UNMAPPED_BY_DECISION,
    INSURANCE_SW_LEVEL2,
    cash_flow_structural_missing_columns,
)

logger = logging.getLogger(__name__)

# 域 → 承载该域字段的表（快照是「每股一行（最新报告期）」，ext 是「每股每报告期一行」，
# 源表域是「每股每报告期可能多行 report_type」）
_DOMAIN_TABLE: dict[str, str] = {
    DOMAIN_SNAPSHOT: "indicator_snapshot",
    DOMAIN_EXT: "indicator_ext",
    **SOURCE_DOMAIN_TABLES,
}
_INDICATOR_DOMAINS: tuple[str, ...] = (DOMAIN_SNAPSHOT, DOMAIN_EXT)

# 从源三表 + 分红表取证据时用到的列。全部加 `src_` 前缀，
# 避免与指标表同名列（如 revenue / net_profit 在未来下沉进指标表时）撞车。
_SOURCE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "income_statement": (
        "revenue",
        "cost_of_revenue",
        "net_profit",
        "operating_profit",
        "total_profit",
        "interest_expense",
        "selling_expenses",
        "administrative_expenses",
        "rd_expenses",
        "financial_expenses",
    ),
    "balance_sheet": (
        "total_assets",
        "total_equity",
        "total_equity_parent",
        "total_liabilities",
        "total_current_assets",
        "total_current_liabilities",
        "inventory",
        "goodwill",
        "accounts_receivable",
        "accounts_payable",
        "fixed_assets",
        "paid_in_capital",
        "capital_reserve",
        "intangible_assets",
        # 有息负债的四个来源科目：全为空时 interest_bearing_debt 根本算不出来
        "short_term_loans",
        "long_term_loans",
        "bonds_payable",
        "lease_liabilities",
    ),
    "cash_flow": ("cash_received_sales", "cf_from_operating"),
}

# ─── 阈值（每条都对应一次正式库实测，改动前先看注释） ────────────────
#
# NOT_DISPATCHED 整期级口径（2026-11-14 裁决 1）：
#   字面规则「本期 100% 为空」会被"1~20 只股票有值"躲过去，而这次审计真正要抓的
#   就是这种"近零覆盖"：balance.fixed_assets@2026-09-30 只有 1.8%（上期 99.9%）、
#   6 个银行监管指标 0%、income.deducted_net_profit 0%（上期 0.4%、再上期 80.2%）。
#   故改为「本期覆盖率 ≤1% 且（上一期 >50% 或 再上一期 >50%）」，再补一条"断崖"
#   条件（本期 ≤10% 且 ≤参考期的 1/10）兜住 99.9% → 1.8% 这种腰斩形态。
_NOT_DISPATCHED_NEAR_ZERO = 0.01
_NOT_DISPATCHED_CLIFF_LIMIT = 0.10
_NOT_DISPATCHED_CLIFF_RATIO = 0.10
_NOT_DISPATCHED_REFERENCE_RATIO = 0.50
_NOT_DISPATCHED_LOOKBACK_PERIODS = 2
# 报告期未对齐时的字段闸门：本期覆盖率低于它才认定"这一期确实没算该字段"。
# 调成 1.0 即退化为"错配时 ext 全部字段都算未对齐"的字面规则（见 _misaligned_fields）。
_MISALIGNED_FIELD_COVERAGE_LIMIT = 0.5
# 技术指标族：最近 60 个交易日里该股复权行数 ≥21 说明序列本身是够的，
# 此时 NULL 只能是"补齐后没重算"（2026-11-14 实测 5,578/5,585 只 qfq 完整，
# 却只有 994 只有值 → 链路问题，不是数据缺失）。
_PRICE_HISTORY_TRADING_DAYS = 60
_PRICE_HISTORY_MIN_ROWS = 21
# 6 个银行监管指标（只对金融股成立），用于"非金融公司不适用"判定
_REGULATORY_FIELDS: tuple[str, ...] = (
    "balance.core_tier1_capital_adequacy_ratio",
    "balance.tier1_capital_adequacy_ratio",
    "balance.capital_adequacy_ratio",
    "balance.non_performing_loan_ratio",
    "balance.provision_coverage_ratio",
    "balance.risk_coverage_ratio",
)
_TECHNICAL_FIELDS: tuple[str, ...] = ("period_return", "annualized_volatility", "max_drawdown")
# 依赖"现金流量表补充资料·折旧摊销"的字段族（2026-11-14 三季报实测）：
# 三季报通常**不披露补充资料**，东财只给经营现金流净额 → 这一族整列掉到 0.1%，
# 与"没去抓"长得一模一样，必须靠 cash_flow_indirect 的实际抓取情况区分。
_DEPRECIATION_FIELDS: tuple[str, ...] = (
    "depreciation_amortization",
    "ebitda",
    "ev_ebitda",
    "leverage_operating",
    "leverage_total",
    "ebitda_to_liabilities",
)
# 折旧摊销在补充资料里的三个来源科目（判定"源是否披露了折旧摊销"）
_DEPRECIATION_SOURCE_TABLE = "cash_flow_indirect"
_DEPRECIATION_SOURCE_COLUMNS: tuple[str, ...] = (
    "fixed_asset_depreciation",
    "intangible_asset_amortization",
    "long_term_prepaid_amortization",
)
# 个股级 NOT_DISPATCHED 的 detail 前缀（统计时要与整期级区分）
_PER_STOCK_DISPATCH_PREFIX = "该股在"


class PredicateOp(StrEnum):
    """谓词运算符（刻意只留四种：够用、一眼看懂、可测）。"""

    IS_NULL = "is_null"
    LE = "le"
    EQ = "eq"
    GE = "ge"


@dataclass(frozen=True)
class Predicate:
    """一个证据谓词：`column <op> value`，`label` 是中文名（拼进 detail）。"""

    column: str
    op: PredicateOp
    label: str
    value: float | None = None

    def matches(self, row: Mapping[str, Any]) -> bool:
        actual = row.get(self.column)
        if self.op is PredicateOp.IS_NULL:
            return actual is None
        # 证据值本身缺失时不能反过来断定"分母无效/行情足够"——那属于无证据
        if actual is None:
            return False
        try:
            numeric = float(actual)
        except (TypeError, ValueError):
            return False
        threshold = float(self.value if self.value is not None else 0.0)
        if self.op is PredicateOp.LE:
            return numeric <= threshold
        if self.op is PredicateOp.GE:
            return numeric >= threshold
        return numeric == threshold


@dataclass(frozen=True)
class RuleGroup:
    """一组"任一命中即成立"的谓词 + detail 模板（`{labels}` 会被替换成命中项中文名）。"""

    predicates: tuple[Predicate, ...]
    detail: str

    def render(self, matched: Sequence[Predicate]) -> str:
        labels: list[str] = []
        for predicate in matched:
            if predicate.label not in labels:
                labels.append(predicate.label)
        return self.detail.format(labels="、".join(labels))


def _le(column: str, label: str, value: float = 0.0) -> Predicate:
    return Predicate(column=column, op=PredicateOp.LE, label=label, value=value)


def _is_null(column: str, label: str) -> Predicate:
    return Predicate(column=column, op=PredicateOp.IS_NULL, label=label)


def _eq(column: str, label: str, value: float = 0.0) -> Predicate:
    return Predicate(column=column, op=PredicateOp.EQ, label=label, value=value)


def _ge(column: str, label: str, value: float) -> Predicate:
    return Predicate(column=column, op=PredicateOp.GE, label=label, value=value)


# ─── 规则表（每条都带正式库实测精度，2026-11-14） ─────────────────────

# indicator_snapshot（56 个可筛选字段，最新报告期 2026-09-30，5,573 行）
SNAPSHOT_NOT_APPLICABLE: dict[str, RuleGroup] = {
    # 实测：2,802/2,802 命中，反例 0 —— 商誉未列示的公司占一半，这是覆盖率的真因。
    "goodwill_ratio": RuleGroup(
        predicates=(_is_null("src_goodwill", "商誉"),),
        detail="无商誉：资产负债表未列示商誉，商誉占净资产比率无业务含义",
    ),
    # 实测：1,878/1,968 命中，反例 16（1,972 只有值里 0.3%）—— 口径就是 TTM，够硬。
    "dividend_yield": RuleGroup(
        predicates=(_eq("div_count_ttm", "近12个月分红记录"),),
        detail="近 12 个月无分红记录，股息率无业务含义",
    ),
    "ttm_dividend_yield": RuleGroup(
        predicates=(_eq("div_count_ttm", "近12个月分红记录"),),
        detail="近 12 个月无分红记录，TTM 股息率无业务含义",
    ),
    # 实测：787/1,129 命中，反例 0 —— 5 年窗口是"连续分红年数"的自然口径。
    "consecutive_div_years": RuleGroup(
        predicates=(_eq("div_count_5y", "近5年分红记录"),),
        detail="近 5 年无分红记录，连续分红年数无业务含义",
    ),
    # 实测：134/168、135/1,832 命中，反例 2/1 —— dps/payout 用的是「史上最近一次分红」，
    # 因此窗口必须放到全历史，用 TTM 窗口会冤枉 1,700+ 只老分红股。
    "dps": RuleGroup(
        predicates=(_eq("div_count_all", "全历史分红记录"),),
        detail="全历史无分红记录，每股股利无业务含义",
    ),
    "payout_ratio": RuleGroup(
        predicates=(_eq("div_count_all", "全历史分红记录"),),
        detail="全历史无分红记录，派息率无业务含义",
    ),
}
# 利差族与 TTM 股息率同源（实测缺失集合完全一致：均为 1,968 行），一并归为不适用。
for _spread_field in (
    "div_yield_spread_0p25y",
    "div_yield_spread_0p5y",
    "div_yield_spread_1y",
    "div_yield_spread_2y",
    "div_yield_spread_3y",
    "div_yield_spread_5y",
    "div_yield_spread_7y",
    "div_yield_spread_10y",
    "div_yield_spread_30y",
):
    SNAPSHOT_NOT_APPLICABLE[_spread_field] = RuleGroup(
        predicates=(_eq("div_count_ttm", "近12个月分红记录"),),
        detail="近 12 个月无分红记录，没有 TTM 股息率可减国债基准",
    )

SNAPSHOT_INVALID_DENOMINATOR: dict[str, RuleGroup] = {
    # 实测：98/98、151/151 命中，反例 0
    "current_ratio": RuleGroup(
        predicates=(_le("src_total_current_liabilities", "流动负债"),),
        detail="分母无效：{labels} ≤0",
    ),
    "quick_ratio": RuleGroup(
        predicates=(_le("src_total_current_liabilities", "流动负债"),),
        detail="分母无效：{labels} ≤0",
    ),
    # 实测：roe 54/188、pb_mrq 56/63 命中，反例 1/3（不足 0.1%）
    "roe": RuleGroup(
        predicates=(
            _le("src_total_equity", "股东权益"),
            _le("src_total_equity_parent", "归母权益"),
        ),
        detail="分母无效：{labels} ≤0",
    ),
    "pb_mrq": RuleGroup(
        predicates=(
            _le("src_total_equity", "股东权益"),
            _le("src_total_equity_parent", "归母权益"),
        ),
        detail="分母无效：{labels} ≤0",
    ),
    # 实测：营收 ≤0 在最新期几乎不存在（各 1 只），但规则本身精确，保留兜底
    "gross_margin": RuleGroup(
        predicates=(_le("src_revenue", "营业收入"),), detail="分母无效：{labels} ≤0"
    ),
    "net_margin": RuleGroup(
        predicates=(_le("src_revenue", "营业收入"),), detail="分母无效：{labels} ≤0"
    ),
    "ps_ttm": RuleGroup(
        predicates=(_le("src_revenue", "营业收入"),), detail="分母无效：{labels} ≤0"
    ),
}

SNAPSHOT_NOT_COMPUTED: dict[str, RuleGroup] = {
    # 实测：4,916/4,917 命中、反例 0 —— roic 覆盖率只有 11.8% 的真因就是没人算有息负债。
    "roic": RuleGroup(
        predicates=(_is_null("interest_bearing_debt", "有息负债（本表 interest_bearing_debt）"),),
        detail="输入缺失：{labels}为空",
    ),
    # 实测：284/615 命中、反例 0
    "interest_coverage": RuleGroup(
        predicates=(_is_null("src_interest_expense", "利息费用"),), detail="输入缺失：{labels}为空"
    ),
    # 实测：93/183 命中、反例 0
    "gross_margin": RuleGroup(
        predicates=(_is_null("src_cost_of_revenue", "营业成本"),), detail="输入缺失：{labels}为空"
    ),
    # 实测：net_margin 5/69、roa 5/62、cf_to_net_profit 5/1,790 命中，反例 0
    "net_margin": RuleGroup(
        predicates=(_is_null("src_net_profit", "净利润"),), detail="输入缺失：{labels}为空"
    ),
    "roa": RuleGroup(
        predicates=(_is_null("src_net_profit", "净利润"),), detail="输入缺失：{labels}为空"
    ),
    "cf_to_net_profit": RuleGroup(
        predicates=(_is_null("src_net_profit", "净利润"),), detail="输入缺失：{labels}为空"
    ),
    # 实测：tca/tcl 各 98/98 命中、反例 0（金融类报表没有流动口径）
    "current_ratio": RuleGroup(
        predicates=(
            _is_null("src_total_current_assets", "流动资产"),
            _is_null("src_total_current_liabilities", "流动负债"),
        ),
        detail="输入缺失：{labels}为空",
    ),
    # 实测：存货为空 151/151 命中、反例 0
    "quick_ratio": RuleGroup(
        predicates=(
            _is_null("src_inventory", "存货"),
            _is_null("src_total_current_assets", "流动资产"),
        ),
        detail="输入缺失：{labels}为空",
    ),
    # 实测：203/4,916 命中、反例 0 —— 四个来源科目全未列示时这个数只能空着。
    "interest_bearing_debt": RuleGroup(
        predicates=(
            _is_null(
                "src_interest_bearing_debt_reported",
                "有息负债来源科目（短期借款/长期借款/应付债券/租赁负债）",
            ),
        ),
        detail="输入缺失：{labels}全为空",
    ),
}
# 技术指标族（2026-11-14 追加规则 3）：复权序列尾部完整却没值 ⇒ 补齐后没重算。
# 实测：5,578/5,585 只 qfq 完整，却只有 994 只有值（覆盖率 17.8%），是链路问题。
for _technical_field in _TECHNICAL_FIELDS:
    SNAPSHOT_NOT_COMPUTED[_technical_field] = RuleGroup(
        predicates=(
            _ge(
                "src_qfq_days_60d",
                "近60个交易日复权行数",
                _PRICE_HISTORY_MIN_ROWS,
            ),
        ),
        detail="复权序列尾部不连续或补齐后未重算",
    )

# indicator_ext（55 个可筛选字段，最新报告期 2026-06-30，5,544 行）
EXT_NOT_APPLICABLE: dict[str, RuleGroup] = {
    # 实测：1,414/1,419 命中，反例 0。
    # 注意 leverage_operating 不能用"利润总额≤0 或 EBIT≤0"：EBITDA/EBIT 与利润总额无关，
    # 加了利润总额条件会制造 164 个反例，故按字段各自取最精确的条件。
    "leverage_financial": RuleGroup(
        predicates=(
            _le("src_total_profit", "利润总额"),
            _le("ebit", "EBIT"),
        ),
        detail="利润总额 ≤0 或 EBIT ≤0，财务杠杆（EBIT/利润总额）无业务含义",
    ),
    "leverage_operating": RuleGroup(
        predicates=(_le("ebit", "EBIT"),),
        detail="EBIT ≤0，经营杠杆（EBITDA/EBIT）无业务含义",
    ),
    "leverage_total": RuleGroup(
        predicates=(_le("src_total_profit", "利润总额"),),
        detail="利润总额 ≤0，综合杠杆（EBITDA/利润总额）无业务含义",
    ),
    # 实测：1,402/1,429 命中，反例 0
    "ocf_to_operating_profit": RuleGroup(
        predicates=(_le("src_operating_profit", "营业利润"),),
        detail="营业利润 ≤0，经营现金流对营业利润比率无业务含义",
    ),
}

EXT_INVALID_DENOMINATOR: dict[str, RuleGroup] = {
    # 实测：734/847 命中，反例 0（亏损公司谈 EV/EBITDA 没有意义）
    "ev_ebitda": RuleGroup(predicates=(_le("ebitda", "EBITDA"),), detail="分母无效：{labels} ≤0"),
    # 实测：46/52 命中，反例 0
    "debt_to_equity": RuleGroup(
        predicates=(_le("src_total_equity", "股东权益"),), detail="分母无效：{labels} ≤0"
    ),
    # 实测：122/131 命中，反例 0（有形净资产 = 权益 − 无形资产 − 商誉）
    "tangible_net_debt_ratio": RuleGroup(
        predicates=(_le("src_tangible_equity", "有形净资产"),), detail="分母无效：{labels} ≤0"
    ),
    # 实测：48/72 命中，反例 0
    "book_to_market": RuleGroup(
        predicates=(_le("src_total_equity_parent", "归母权益"),), detail="分母无效：{labels} ≤0"
    ),
}

EXT_NOT_COMPUTED: dict[str, RuleGroup] = {
    # 实测命中/反例（2026-06-30）：研发费 345/0、销售费 163/0、管理费 102/0、财务费 103/0、
    # 应收账款 56/0、存货 157/0、流动资产 102/0、固定资产 3/0、归母权益 58/0、
    # 资本公积 13/0、销售收现 102/0 —— 全是"输入列为空 ⇒ 该算没算"。
    "rd_expense_ratio": RuleGroup(predicates=(_is_null("src_rd_expenses", "研发费用"),),
                                  detail="输入缺失：{labels}为空"),
    "selling_expense_ratio": RuleGroup(predicates=(_is_null("src_selling_expenses", "销售费用"),),
                                       detail="输入缺失：{labels}为空"),
    "admin_expense_ratio": RuleGroup(
        predicates=(_is_null("src_administrative_expenses", "管理费用"),),
        detail="输入缺失：{labels}为空",
    ),
    "finance_expense_ratio": RuleGroup(
        predicates=(_is_null("src_financial_expenses", "财务费用"),), detail="输入缺失：{labels}为空"
    ),
    "receivables_turnover": RuleGroup(
        predicates=(_is_null("src_accounts_receivable", "应收账款"),), detail="输入缺失：{labels}为空"
    ),
    "inventory_turnover": RuleGroup(
        predicates=(_is_null("src_inventory", "存货"),), detail="输入缺失：{labels}为空"
    ),
    # 应付账款周转率 = 营业成本 / 平均应付账款，两边任一缺失都算不出来
    "accounts_payable_turnover": RuleGroup(
        predicates=(
            _is_null("src_accounts_payable", "应付账款"),
            _is_null("src_cost_of_revenue", "营业成本"),
        ),
        detail="输入缺失：{labels}为空",
    ),
    "current_asset_turnover": RuleGroup(
        predicates=(_is_null("src_total_current_assets", "流动资产"),),
        detail="输入缺失：{labels}为空",
    ),
    "current_asset_ratio": RuleGroup(
        predicates=(_is_null("src_total_current_assets", "流动资产"),),
        detail="输入缺失：{labels}为空",
    ),
    "cash_ratio": RuleGroup(
        predicates=(_is_null("src_total_current_assets", "流动资产"),),
        detail="输入缺失：{labels}为空",
    ),
    "conservative_quick_ratio": RuleGroup(
        predicates=(_is_null("src_total_current_assets", "流动资产"),),
        detail="输入缺失：{labels}为空",
    ),
    "fixed_asset_ratio": RuleGroup(
        predicates=(_is_null("src_fixed_assets", "固定资产"),), detail="输入缺失：{labels}为空"
    ),
    "fixed_asset_turnover": RuleGroup(
        predicates=(_is_null("src_fixed_assets", "固定资产"),), detail="输入缺失：{labels}为空"
    ),
    "operating_cycle_days": RuleGroup(
        predicates=(
            _is_null("src_accounts_receivable", "应收账款"),
            _is_null("src_inventory", "存货"),
        ),
        detail="输入缺失：{labels}为空",
    ),
    "bps_parent": RuleGroup(
        predicates=(_is_null("src_total_equity_parent", "归母权益"),), detail="输入缺失：{labels}为空"
    ),
    "capital_reserve_per_share": RuleGroup(
        predicates=(_is_null("src_capital_reserve", "资本公积"),), detail="输入缺失：{labels}为空"
    ),
    "cash_content_of_revenue": RuleGroup(
        predicates=(_is_null("src_cash_received_sales", "销售商品收到的现金"),),
        detail="输入缺失：{labels}为空",
    ),
    # 自表列作输入：EBITDA 依赖折旧摊销，EV/EBITDA 又依赖 EBITDA
    "ebitda": RuleGroup(
        predicates=(_is_null("depreciation_amortization", "折旧摊销"),), detail="输入缺失：{labels}为空"
    ),
    "ebitda_to_liabilities": RuleGroup(
        predicates=(_is_null("depreciation_amortization", "折旧摊销"),), detail="输入缺失：{labels}为空"
    ),
    "ev_ebitda": RuleGroup(
        predicates=(_is_null("ebitda", "EBITDA"),), detail="输入缺失：{labels}为空"
    ),
    "leverage_operating": RuleGroup(
        predicates=(_is_null("ebitda", "EBITDA"),), detail="输入缺失：{labels}为空"
    ),
    "leverage_total": RuleGroup(
        predicates=(_is_null("ebitda", "EBITDA"),), detail="输入缺失：{labels}为空"
    ),
    "leverage_financial": RuleGroup(
        predicates=(_is_null("ebit", "EBIT"),), detail="输入缺失：{labels}为空"
    ),
    "ocf_to_operating_profit": RuleGroup(
        predicates=(_is_null("src_operating_profit", "营业利润"),), detail="输入缺失：{labels}为空"
    ),
    "ebit": RuleGroup(
        predicates=(
            _is_null("src_operating_profit", "营业利润"),
            _is_null("src_total_profit", "利润总额"),
        ),
        detail="输入缺失：{labels}为空",
    ),
    "fcf_margin": RuleGroup(
        predicates=(_is_null("free_cash_flow", "自由现金流"),), detail="输入缺失：{labels}为空"
    ),
    # 时点估值族：报告期市值依赖报告期收盘价
    "tobin_q": RuleGroup(
        predicates=(_is_null("market_cap_at_report", "报告期市值"),), detail="输入缺失：{labels}为空"
    ),
    "book_to_market": RuleGroup(
        predicates=(_is_null("market_cap_at_report", "报告期市值"),), detail="输入缺失：{labels}为空"
    ),
    "market_cap_at_report": RuleGroup(
        predicates=(_is_null("report_date_close", "报告期收盘价"),), detail="输入缺失：{labels}为空"
    ),
    "accruals": RuleGroup(
        predicates=(_is_null("src_net_profit", "净利润"),), detail="输入缺失：{labels}为空"
    ),
    "profit_per_employee": RuleGroup(
        predicates=(_is_null("src_net_profit", "净利润"),), detail="输入缺失：{labels}为空"
    ),
}

# 依赖折旧摊销的字段族（2026-11-14 三季报真实形态）：
#   三季报通常不披露"现金流量表补充资料"，东财只给经营现金流净额 → 折旧摊销整列为空，
#   于是这一族在 2026-09-30 掉到 0.04%~0.09%，而上期 99.7%。这**不是**"没去抓"：
#   抓取是成功的（`cash_flow_indirect` 5,574 行、`cf_from_operating_indirect` 5,574 非空），
#   只是源里就没有折旧摊销明细。所以：
#   - 有补充资料行、但三个折旧摊销科目全空 → SOURCE_EMPTY（源没披露）
#   - 连补充资料行都没有 → NOT_DISPATCHED（我们没抓到）
#   - 这一族同时被排除在"整期级 NOT_DISPATCHED"之外（见 _not_dispatched_fields），
#     否则 0.09% 会被兜底规则判成"没去抓"，与上面这条直接打架。
EXT_SOURCE_EMPTY: dict[str, RuleGroup] = {
    field_name: RuleGroup(
        predicates=(_eq("src_cfi_depreciation_missing", "现金流量表补充资料折旧摊销", 1),),
        detail="该期现金流量表补充资料未披露折旧摊销（三季报常见）",
    )
    for field_name in _DEPRECIATION_FIELDS
}

EXT_SOURCE_MISSING: dict[str, RuleGroup] = {
    field_name: RuleGroup(
        predicates=(_eq("src_cfi_rows", "现金流量表补充资料(cash_flow_indirect)", 0),),
        detail="该股该期没有 cash_flow_indirect 行：现金流量表补充资料未抓到",
    )
    for field_name in _DEPRECIATION_FIELDS
}

# 现金流量补充域（2026-11-17，reports/135）：`CASH_FLOW_STRUCTURAL_MISSING` 是
# 「东财四套模板都拿不到哪些列」的事实清单，这里把它变成**可落库的规则**。
#
# 为什么这件事必须在清扫器里做：事实清单一直只被 `supplement.py` 和测试引用，
# `indicator_null_reason` 里连一个 `cash_flow*` 字段名都没有 —— FY2025 的
# `other_cash_effect` / `unconfirmed_investment_loss` 就是"无声 NULL"（报告点名
# 要消灭的形态）。规则按域装配，落库时 `upsert_reasons(replace_scope=(report_date, domain))`
# 天然按报告期存，不是只存最新一期。
#
# 两族例外的处理（与 `cash_flow_structural_missing_columns(org_type=...)` 同源）：
# - `investment_property_depreciation` 只在银行/保险模板下结构性缺失；
#   通用/证券模板有 `IR_DEPR` 独立行，NULL 可能只是公司没有投资性房地产 —— 判不出就不写
#   （用 `src_is_eastmoney_bank_insurer` 精确到银行/保险，不把券商/多元金融算进来）；
# - `credit_impairment_loss` 在通用/证券模板结构性缺失；银行/保险模板虽已映射，但
#   有行没值同样是"源没提供"，故对全部行登记 SOURCE_EMPTY。
def _cashflow_structural_source_empty_rules() -> dict[str, dict[str, RuleGroup]]:
    """`{domain: {带前缀字段: RuleGroup}}`，detail 直接引用事实清单里的依据。"""
    rules: dict[str, dict[str, RuleGroup]] = {
        domain: {} for domain in CASH_FLOW_STRUCTURAL_MISSING
    }
    for table, reasons in CASH_FLOW_STRUCTURAL_MISSING.items():
        general = set(cash_flow_structural_missing_columns(table, org_type="通用"))
        financial = set(cash_flow_structural_missing_columns(table, org_type="银行"))
        financial_only = financial - general
        for column, reason in reasons.items():
            field_name = f"{table}.{column}"
            if column in financial_only:
                predicates = (
                    _eq("src_is_eastmoney_bank_insurer", "东财本源行·银行/保险模板", 1),
                )
            else:
                predicates = (_eq("src_is_eastmoney", "东财本源行", 1),)
            if field_name in CASH_FLOW_UNMAPPED_BY_DECISION:
                # 语义纠正（reports/135）：这**不是**"源没有字段"，而是源有字段、
                # 口径与 CSMAR 不完全相等，团队主动不映射。NullReason 枚举没有
                # SEMANTIC_MISMATCH / NOT_MAPPED（新增会破坏既有契约），
                # 因此原因码仍用 SOURCE_EMPTY，语义写进 detail 供主控裁决。
                detail = (
                    "【口径不符·主动不映射，非「源无此字段」】" + reason
                )
            else:
                detail = "【源侧结构性缺失（东财模板无此科目）】" + reason
            rules[table][field_name] = RuleGroup(predicates=predicates, detail=detail)
    return rules


_CASHFLOW_STRUCTURAL_SOURCE_EMPTY: dict[str, dict[str, RuleGroup]] = (
    _cashflow_structural_source_empty_rules()
)

# 源表域：6 个银行监管指标只对**银行**成立，非银行的 NULL 是**不适用**而不是缺失。
# 实测（2026-09-30）：6 个字段非空数均为 0，不满足"上期/再上期 >50%"的调度条件，
# 若不按行业分流，这 3.3 万个单元格会全部落进 unclassified。
# 2026-11-18 修正（reports/139 第二轮验收 P2）：旧判据是「非金融 → 不适用」，
# 但**券商/保险同样没有**资本充足率/不良率 —— 实测 121 只金融股里 501 个单元格
# （非银金融 472 + 银行 29）因此落进 unclassified。改为按「非银行 → 不适用」。
BALANCE_NOT_APPLICABLE: dict[str, RuleGroup] = {
    field_name: RuleGroup(
        predicates=(_eq("src_is_bank", "银行", 0),),
        detail="只有银行披露资本充足率/不良率等监管指标，非银行公司该字段本就不适用",
    )
    for field_name in _REGULATORY_FIELDS
}


# 源表域：v48 新增的补采科目（reports/139 §11.4 / D58）里**确定不适用**的那两列。
# 银行模板没有「累计折旧」列、且「固定资产原值」与通用模板口径不同（实测出现
# `原值 < 净值`，如 000722 FY2025），所以这两列对银行一律 NOT_APPLICABLE；
# 其余 5 个补采科目的 NULL 属于"回填未覆盖/源无该科目"，交给覆盖率规则与
# NOT_DISPATCHED 判定，不在这里硬编码。
BALANCE_SUBJECT_NOT_APPLICABLE: dict[str, RuleGroup] = {
    "balance.fixed_assets_gross": RuleGroup(
        predicates=(_eq("src_is_bank", "银行", 1),),
        detail="银行模板的「固定资产原值」与通用模板不是同一口径（实测原值<净值），该字段对银行不适用",
    ),
    "balance.accumulated_depreciation": RuleGroup(
        predicates=(_eq("src_is_bank", "银行", 1),),
        detail="银行模板没有「累计折旧」列（只在附注），该字段对银行不适用",
    ),
}


@dataclass
class SweepResult:
    """一次清扫的结果（dry-run 与写库共用同一份统计）。"""

    domain: str
    report_date: date | None = None
    table: str | None = None
    rows_scanned: int = 0
    null_cells: int = 0
    planned_rows: int = 0
    written_rows: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    by_field: dict[str, dict[str, int]] = field(default_factory=dict)
    unclassified: dict[str, int] = field(default_factory=dict)
    not_dispatched_fields: tuple[str, ...] = ()
    per_stock_dispatched_cells: int = 0
    report_period_misaligned: bool = False
    report_period_detail: str | None = None
    duration_seconds: float = 0.0
    dry_run: bool = True

    @property
    def unclassified_cells(self) -> int:
        return sum(self.unclassified.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "table": self.table,
            "report_date": str(self.report_date) if self.report_date else None,
            "dry_run": self.dry_run,
            "rows_scanned": self.rows_scanned,
            "null_cells": self.null_cells,
            "planned_rows": self.planned_rows,
            "written_rows": self.written_rows,
            "by_reason": dict(sorted(self.by_reason.items())),
            "by_field": {
                name: dict(sorted(buckets.items()))
                for name, buckets in sorted(self.by_field.items())
            },
            "unclassified": dict(sorted(self.unclassified.items())),
            "unclassified_cells": self.unclassified_cells,
            "not_dispatched_fields": list(self.not_dispatched_fields),
            "per_stock_dispatched_cells": self.per_stock_dispatched_cells,
            "report_period_misaligned": self.report_period_misaligned,
            "report_period_detail": self.report_period_detail,
            "duration_seconds": round(self.duration_seconds, 3),
        }


def _quote(identifier: str) -> str:
    """标识符加引号（字段名来自 engine 常量，不来自用户输入）。"""
    return '"' + identifier.replace('"', '""') + '"'


def _plain_field(domain: str, field_name: str) -> str:
    """`income.deducted_net_profit` → `deducted_net_profit`（指标域原样返回）。"""
    if domain in SOURCE_DOMAINS and field_name.startswith(f"{domain}."):
        return field_name[len(domain) + 1 :]
    return field_name


def _shift_years(value: date, years: int) -> date:
    """往前推 N 年（报告期都是季末，2-29 不会出现，仍留兜底）。"""
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _cell_frame_sql(domain: str, fields: Sequence[str]) -> str:
    """一个报告期的「每股一行」单元格视图。

    源表同一 (股票, 报告期) 可能有 annual/quarterly/中文 report_type 多条记录，
    用 `any_value` 聚合（DuckDB 的 any_value 忽略 NULL）：**任何一条有值即视为有值**，
    这样"这个单元格有没有值"的口径与指标层一致。
    顺带带上 `src_is_financial`（行业判定），供"非金融公司不适用"用；
    没有行业记录的股票给 NULL —— 判不出就不写。
    """
    table = _DOMAIN_TABLE[domain]
    if domain in SOURCE_DOMAINS:
        aggregated = ", ".join(
            f"any_value({_quote(_plain_field(domain, name))}) AS {_quote(name)}" for name in fields
        )
        # 行业判定：2026-11-18（reports/139 §5.3 / §7 前置 1）改为调用
        # `app.core.sector.is_financial_sql` —— 全仓唯一实现。
        #
        # 旧写法把 `sw_level1 IN ('银行','非银金融','保险')` + `csrc_l1 LIKE '%金融%'`
        # + 股票名 LIKE（%银行%/%证券%/%保险%）三路并起来，实测有两类错误：
        #  ① `sw_level1` 里**不存在 '保险'** 这个取值（保险是 `sw_level2='保险Ⅱ'`），
        #     那一段是死代码，只会让读代码的人以为保险已被覆盖；
        #  ② 名称兜底把同花顺(300033)/指南针(300803)/大智慧(601519) 等
        #     **软件股**判成金融股 —— 它们本就没有资本充足率/不良率，
        #     该落 `NOT_APPLICABLE` 的 NULL 被这句兜底挤进了 unclassified。
        # 正式库实测（reports/139 §6）：`sw_level1` = 银行 42 / 非银金融 79 / NULL 751，
        # 旧兜底相对新判据多出的 7 只里只有 2 只真券商（宏源证券/海通证券，均已退市、
        # 无当期行），其余 5 只全是软件/创投/电力 —— 去掉兜底只增加精度，不漏报。
        #
        # `meta.stock_code IS NULL`（该股没有行业记录）仍返回 NULL：「判不出就不写」，
        # 绝不能因为 is_financial_sql 对 NULL 行业返回 FALSE 就把"判不出"降级成
        # "非金融"，凭空写出 NOT_APPLICABLE。
        financial_expr = (
            "CASE WHEN meta.stock_code IS NULL THEN NULL "
            f"WHEN {is_financial_sql('meta')} THEN 1 ELSE 0 END"
        )
        extra_flags = ""
        if domain in CASH_FLOW_STRUCTURAL_MISSING:
            # 源表域里的"结构性缺失"是**东财模板**的事实；CSMAR 行不能用同一句话
            # 归因（那边是"导入没映射"，属于 NOT_DISPATCHED）。把来源与
            # "东财 + 金融模板"两个组合证据算好，规则里直接用单谓词匹配。
            aggregated += (
                ", any_value(CASE WHEN source = 'eastmoney_f10' THEN 1 ELSE 0 END) "
                "AS src_is_eastmoney"
            )
            # `investment_property_depreciation` 的结构性缺失只发生在**银行/保险**
            # 专用模板（通用/证券模板有 IR_DEPR 独立行）。stock_meta 的行业口径里
            # 券商/多元金融也算"非银金融"，直接用 `src_is_financial` 会把它们的
            # NULL 误标成"模板缺列" —— 所以这里单独按 银行（sw_level1）/保险
            # （`sw_level2=保险Ⅱ`）/公司名 判定，与 supplement 的 ORG_TYPE 口径对齐。
            bank_insurer_expr = (
                "CASE WHEN COALESCE(meta.sw_level1, '') = '银行' "
                f"OR COALESCE(meta.sw_level2, '') = '{INSURANCE_SW_LEVEL2}' "
                "OR COALESCE(meta.name, '') LIKE '%银行%' "
                "OR COALESCE(meta.name, '') LIKE '%保险%' "
                "THEN 1 ELSE 0 END"
            )
            extra_flags = (
                ", aggregated.src_is_eastmoney"
                ", CASE WHEN aggregated.src_is_eastmoney = 1"
                f" AND ({bank_insurer_expr}) = 1 THEN 1 ELSE 0 END"
                " AS src_is_eastmoney_bank_insurer"
            )
        return (
            f"WITH aggregated AS (SELECT stock_code, {aggregated} FROM {table} "
            f"WHERE report_date = ? GROUP BY stock_code) "
            # `src_row_present`：该股该期在本表**有行**（1=抓到了）。
            f"SELECT aggregated.*, 1 AS src_row_present, "
            f"{financial_expr} AS src_is_financial"
            f", CASE WHEN {is_bank_sql('meta')} THEN 1 ELSE 0 END AS src_is_bank"
            f"{extra_flags} FROM aggregated "
            f"LEFT JOIN stock_meta meta USING (stock_code)"
        )
    return f"SELECT * FROM {table} WHERE report_date = ?"


def _load_cell_rows(
    store: DuckDBStore, domain: str, report_date: date, fields: Sequence[str]
) -> list[dict[str, Any]]:
    return store.read_query(_cell_frame_sql(domain, fields), [report_date])


def _coverage_from_rows(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> dict[str, int]:
    counts = dict.fromkeys(fields, 0)
    for row in rows:
        for name in fields:
            if row.get(name) is not None:
                counts[name] += 1
    return counts


def _previous_report_dates(
    store: DuckDBStore, table: str, report_date: date, count: int
) -> list[date]:
    rows = store.read_query(
        f"SELECT DISTINCT report_date FROM {table} WHERE report_date < ? "
        f"ORDER BY report_date DESC LIMIT ?",
        [report_date, count],
    )
    return [row["report_date"] for row in rows]


def _domain_report_date(store: DuckDBStore, table: str, requested: date | str | None) -> date | None:
    if requested is not None:
        return requested if isinstance(requested, date) else date.fromisoformat(str(requested))
    rows = store.read_query(f"SELECT MAX(report_date) AS latest FROM {table}")
    return rows[0]["latest"] if rows else None


def _not_dispatched_fields(
    *,
    fields: Sequence[str],
    report_date: date,
    total_rows: int,
    current: Mapping[str, int],
    history: Sequence[tuple[date, int, Mapping[str, int]]],
    excluded: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """整期级"本期没人抓"：本期覆盖率 ≤1%（或断崖）且近两期里有一期正常覆盖。

    口径（2026-11-14 裁决 1 及其实测依据）：
    - 绝对阈值 1%：字面规则"100% 为空"会被"1~20 只股票有值"躲过去，
      而近零覆盖恰恰是**最需要报警**的形态（`income.deducted_net_profit`@2026-09-30 = 0%）。
    - 参考期只要"上一期或再上一期"有一期覆盖率 >50% 即可 ——
      只死盯上一期会漏掉 `deducted_net_profit`（上一期 0.4%，再上一期 80.2%）。
    - 断崖条件（本期 ≤10% 且 ≤参考期的 1/10）：兜住 `balance.fixed_assets` 99.9% → 1.8%
      这种"还剩下一点"的形态，1.8% 并不 ≤1%。
    """
    if total_rows == 0 or not history:
        return {}
    references = [(day, rows, counts) for day, rows, counts in history if rows > 0]
    if not references:
        return {}
    details: dict[str, str] = {}
    for name in fields:
        if name in excluded:
            # 折旧摊销族：整列空可能是"源不披露"（三季报），必须逐股看补充资料到底抓没抓到，
            # 这一族由 EXT_SOURCE_EMPTY / EXT_SOURCE_MISSING 两条规则负责。
            continue
        coverage = current.get(name, 0) / total_rows
        best_day: date | None = None
        best_ratio = 0.0
        for day, rows, counts in references:
            ratio = counts.get(name, 0) / rows
            if ratio > best_ratio:
                best_day, best_ratio = day, ratio
        if best_day is None or best_ratio <= _NOT_DISPATCHED_REFERENCE_RATIO:
            continue
        near_zero = coverage <= _NOT_DISPATCHED_NEAR_ZERO
        cliff = (
            coverage <= _NOT_DISPATCHED_CLIFF_LIMIT
            and coverage <= best_ratio * _NOT_DISPATCHED_CLIFF_RATIO
        )
        if not (near_zero or cliff):
            continue
        shape = "近零覆盖" if near_zero else "断崖式塌陷"
        details[name] = (
            f"本期({report_date})该字段覆盖率仅 {coverage * 100:.1f}%（{shape}），"
            f"参考期 {best_day} 覆盖率 {best_ratio * 100:.1f}%，判定本期未调度抓取/计算"
        )
    return details


def _present_stocks(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> dict[str, set[str]]:
    """某期每字段"有值的股票集合"（个股级 NOT_DISPATCHED 的原始证据）。"""
    present: dict[str, set[str]] = {name: set() for name in fields}
    for row in rows:
        stock_code = str(row.get("stock_code") or "")
        for name in fields:
            if row.get(name) is not None:
                present[name].add(stock_code)
    return present


def _per_stock_dispatched(
    *,
    fields: Sequence[str],
    history: Sequence[tuple[date, Mapping[str, set[str]]]],
) -> dict[str, dict[str, date]]:
    """源表个股级"本期没抓"：该股在历史期有值、本期为空。

    为什么需要个股级：整期级规则看的是"全体覆盖率"，抓不到"只有 38 家银行有值"
    这种稀疏字段（6 个监管指标本期 0%、上一期 0.7%，永远凑不到 50%）。
    个股级证据更硬 —— **这只股票上一期明明有值**，本期空了就是没抓。
    返回 `{字段: {股票: 最近有值的报告期}}`。
    """
    evidence: dict[str, dict[str, date]] = {name: {} for name in fields}
    for day, present in history:
        for name in fields:
            for stock_code in present.get(name, ()):
                # history 按从近到远传入，先写入的是更近的期
                evidence[name].setdefault(stock_code, day)
    return evidence


def _report_period_gap(store: DuckDBStore) -> tuple[date | None, date | None]:
    snapshot = store.read_query("SELECT MAX(report_date) AS latest FROM indicator_snapshot")
    ext = store.read_query("SELECT MAX(report_date) AS latest FROM indicator_ext")
    snapshot_latest = snapshot[0]["latest"] if snapshot else None
    ext_latest = ext[0]["latest"] if ext else None
    return snapshot_latest, ext_latest


def _misaligned_fields(
    *,
    fields: Sequence[str],
    total_rows: int,
    current: Mapping[str, int],
    detail: str,
    coverage_limit: float,
) -> dict[str, str]:
    """报告期未对齐时，只把**整期基本没算**的字段判为 NOT_COMPUTED。

    为什么加覆盖率闸门（保守原则）：ext 最新期落后 snapshot 一期时，
    整张表的 NULL 都可能被归因为"没按新报告期重算"，但若某字段本期已有
    96% 的值，那些零星 NULL 显然是别的原因（个股缺科目），硬扣上"报告期未对齐"
    就是编原因。只有本期覆盖率低于闸门的字段，才说明这一期确实没算它。
    """
    if total_rows == 0:
        return {}
    gated: dict[str, str] = {}
    for name in fields:
        coverage = current.get(name, 0) / total_rows
        if coverage < coverage_limit:
            gated[name] = detail
    return gated


def _matches(predicates: Sequence[Predicate], row: Mapping[str, Any]) -> list[Predicate]:
    return [predicate for predicate in predicates if predicate.matches(row)]


def _classify_cell(
    *,
    field_name: str,
    row: Mapping[str, Any],
    not_applicable: dict[str, RuleGroup],
    source_empty: dict[str, RuleGroup],
    invalid_denominator: dict[str, RuleGroup],
    not_computed: dict[str, RuleGroup],
    source_missing: dict[str, RuleGroup],
    per_stock_dispatched: Mapping[str, date],
    not_dispatched: Mapping[str, str],
    misaligned_detail: str | None,
) -> tuple[NullReason, str] | None:
    """单个 NULL 单元格 → (原因, detail)；判不出返回 None（**不写库**）。"""
    rule = not_applicable.get(field_name)
    if rule is not None:
        matched = _matches(rule.predicates, row)
        if matched:
            return NullReason.NOT_APPLICABLE, rule.render(matched)

    rule = invalid_denominator.get(field_name)
    if rule is not None:
        matched = _matches(rule.predicates, row)
        if matched:
            return NullReason.INVALID_DENOMINATOR, rule.render(matched)

    # 源没披露，比"该算没算"更上游：抓到了源表行却没有这个科目，责任不在调度/计算
    rule = source_empty.get(field_name)
    if rule is not None:
        matched = _matches(rule.predicates, row)
        if matched:
            return NullReason.SOURCE_EMPTY, rule.render(matched)

    # 连源表行都没有 ⇒ 没抓到；这也比"输入缺失"更靠上（输入缺失正是没抓到造成的）
    rule = source_missing.get(field_name)
    if rule is not None:
        matched = _matches(rule.predicates, row)
        if matched:
            return NullReason.NOT_DISPATCHED, rule.render(matched)

    rule = not_computed.get(field_name)
    if rule is not None:
        matched = _matches(rule.predicates, row)
        if matched:
            return NullReason.NOT_COMPUTED, rule.render(matched)

    if misaligned_detail is not None:
        return NullReason.NOT_COMPUTED, misaligned_detail

    # 整期级先于个股级：整期塌陷时全体单元格共用同一个根因，detail 也就统一了；
    # 个股级只在"整期看起来正常、个别股票丢了值"时兜底（如 38 家银行的监管指标）。
    dispatch_detail = not_dispatched.get(field_name)
    if dispatch_detail is not None:
        return NullReason.NOT_DISPATCHED, dispatch_detail

    if per_stock_dispatched:
        stock_code = str(row.get("stock_code") or "")
        had_value_on = per_stock_dispatched.get(stock_code)
        if had_value_on is not None:
            return (
                NullReason.NOT_DISPATCHED,
                f"{_PER_STOCK_DISPATCH_PREFIX} {had_value_on} 有值、本期为空，判定本期未调度抓取",
            )

    return None


def _evidence_sql(
    table: str, *, with_price_history: bool, with_cash_flow_indirect: bool = False
) -> str:
    """构造「指标表 ⨝ 源三表 ⨝ 分红统计（⨝ 复权行情）」的单条证据查询。

    为什么一条 SQL 取全证据：DuckDB 里 5.5k×几十列的联表是毫秒级，
    而逐字段逐股票回查会变成几万次连接。取回 Python 后再按规则表判定，
    规则就是纯数据（可读、可测、可审计）。
    """
    source_ctes: list[str] = []
    for source_table, columns in _SOURCE_EVIDENCE.items():
        projected = ", ".join(
            f"any_value({_quote(column)}) AS src_{column}" for column in columns
        )
        source_ctes.append(
            f"{source_table}_evidence AS (SELECT stock_code, {projected} "
            f"FROM {source_table} WHERE report_date = ? GROUP BY stock_code)"
        )
    # 分红证据：三个窗口一次算完。
    # - ttm：股息率口径（近 12 个月）
    # - 5y：连续分红年数口径
    # - all：dps/payout 口径（"史上最近一次分红"）
    # 全历史无记录的公司 LEFT JOIN 后为 NULL，故外面统一 COALESCE 成 0。
    dividend_cte = (
        "dividend_evidence AS (SELECT stock_code, COUNT(*) AS div_count_all, "
        "COUNT(*) FILTER (WHERE COALESCE(ex_date, announcement_date) > ?) AS div_count_5y, "
        "COUNT(*) FILTER (WHERE COALESCE(ex_date, announcement_date) > ?) AS div_count_ttm "
        "FROM dividends WHERE COALESCE(ex_date, announcement_date) <= ? GROUP BY stock_code)"
    )
    # 现金流量表补充资料：判"源没披露"还是"我们没抓"的唯一证据（见 EXT_SOURCE_EMPTY）。
    # - src_cfi_rows：该股该期有没有补充资料行（>0 = 我们抓到了）
    # - src_cfi_depreciation_missing：抓到行、但三个折旧摊销科目全空 → 源没披露
    #   （只在这种情况下为 1；没抓到行时为 NULL，交给 NOT_DISPATCHED 那条规则）
    cfi_ctes = ""
    cfi_select = ""
    cfi_join = ""
    if with_cash_flow_indirect:
        depreciation_present = " OR ".join(
            f"{_quote(column)} IS NOT NULL" for column in _DEPRECIATION_SOURCE_COLUMNS
        )
        cfi_ctes = (
            f", cash_flow_indirect_evidence AS ("
            f"SELECT stock_code, COUNT(*) AS src_cfi_rows, "
            f"MAX(CASE WHEN {depreciation_present} THEN 1 ELSE 0 END) "
            f"AS src_cfi_has_depreciation "
            f"FROM {_DEPRECIATION_SOURCE_TABLE} WHERE report_date = ? GROUP BY stock_code)"
        )
        cfi_select = (
            ", COALESCE(cash_flow_indirect_evidence.src_cfi_rows, 0) AS src_cfi_rows"
            ", CASE WHEN COALESCE(cash_flow_indirect_evidence.src_cfi_rows, 0) = 0 THEN NULL"
            " WHEN COALESCE(cash_flow_indirect_evidence.src_cfi_has_depreciation, 0) = 0 THEN 1"
            " ELSE 0 END AS src_cfi_depreciation_missing"
        )
        cfi_join = " LEFT JOIN cash_flow_indirect_evidence USING (stock_code)"
    price_ctes = ""
    price_select = ""
    price_join = ""
    if with_price_history:
        # 只取"最近 60 个交易日"这一段：先取尾部 60 个交易日（走 trade_date 过滤，
        # 17.5M 行的表 0.2s 内出结果），再按股票数行数。锚点是全市场最新交易日，
        # 因为要判的就是"行情尾部是否连续"。
        price_ctes = (
            ", recent_qfq_dates AS ("
            "SELECT DISTINCT trade_date FROM price_daily_qfq "
            "WHERE trade_date >= (SELECT MAX(trade_date) FROM price_daily_qfq) - INTERVAL 200 DAY "
            f"ORDER BY trade_date DESC LIMIT {_PRICE_HISTORY_TRADING_DAYS}), "
            "price_evidence AS ("
            "SELECT q.stock_code, COUNT(*) AS src_qfq_days_60d "
            "FROM price_daily_qfq q "
            "WHERE q.trade_date IN (SELECT trade_date FROM recent_qfq_dates) "
            "GROUP BY q.stock_code)"
        )
        price_select = ", COALESCE(price_evidence.src_qfq_days_60d, 0) AS src_qfq_days_60d"
        price_join = " LEFT JOIN price_evidence USING (stock_code)"
    joins = "".join(
        f" LEFT JOIN {source_table}_evidence USING (stock_code)"
        for source_table in _SOURCE_EVIDENCE
    )
    # 有形净资产 = 权益 − 无形资产 − 商誉（教材口径，商誉必须先扣掉）
    return (
        f"WITH indicator AS (SELECT * FROM {table} WHERE report_date = ?), "
        + ", ".join(source_ctes)
        + f", {dividend_cte}"
        + cfi_ctes
        + price_ctes
        + " SELECT indicator.*, "
        + ", ".join(
            f"{source_table}_evidence.* EXCLUDE (stock_code)" for source_table in _SOURCE_EVIDENCE
        )
        + ", COALESCE(dividend_evidence.div_count_all, 0) AS div_count_all"
        + ", COALESCE(dividend_evidence.div_count_5y, 0) AS div_count_5y"
        + ", COALESCE(dividend_evidence.div_count_ttm, 0) AS div_count_ttm"
        + ", (COALESCE(balance_sheet_evidence.src_total_equity, 0)"
        + " - COALESCE(balance_sheet_evidence.src_intangible_assets, 0)"
        + " - COALESCE(balance_sheet_evidence.src_goodwill, 0)) AS src_tangible_equity"
        # 有息负债的四个来源科目"全部未列示"才说明这个数根本算不出来；
        # 单个科目为空不代表算不出（实测：应付债券为空的 4,758 只里，657 只仍有值）。
        + ", CASE WHEN balance_sheet_evidence.src_short_term_loans IS NULL"
        + " AND balance_sheet_evidence.src_long_term_loans IS NULL"
        + " AND balance_sheet_evidence.src_bonds_payable IS NULL"
        + " AND balance_sheet_evidence.src_lease_liabilities IS NULL"
        + " THEN NULL ELSE 1 END AS src_interest_bearing_debt_reported"
        + cfi_select
        + price_select
        + f" FROM indicator{joins}{cfi_join}{price_join}"
        + " LEFT JOIN dividend_evidence USING (stock_code)"
    )


def _build_evidence_rows(
    store: DuckDBStore,
    table: str,
    report_date: date,
    *,
    with_price_history: bool,
    with_cash_flow_indirect: bool = False,
) -> list[dict[str, Any]]:
    five_year_cut = _shift_years(report_date, 5)
    one_year_cut = _shift_years(report_date, 1)
    params: list[Any] = [
        report_date,  # indicator
        report_date,  # income_statement
        report_date,  # balance_sheet
        report_date,  # cash_flow
    ]
    if with_cash_flow_indirect:
        params.append(report_date)  # cash_flow_indirect
    params.extend(
        [
            five_year_cut,  # 分红 5 年窗口下界
            one_year_cut,  # 分红 TTM 窗口下界
            report_date,  # 分红窗口上限
        ]
    )
    return store.read_query(
        _evidence_sql(
            table,
            with_price_history=with_price_history,
            with_cash_flow_indirect=with_cash_flow_indirect,
        ),
        params,
    )


def _load_domain_rows(
    store: DuckDBStore, domain: str, report_date: date, fields: Sequence[str]
) -> list[dict[str, Any]]:
    """取某域某报告期参与判定的行（指标域带证据列，源表域按股票聚合）。"""
    if domain in SOURCE_DOMAINS:
        return _load_cell_rows(store, domain, report_date, fields)
    return _build_evidence_rows(
        store,
        _DOMAIN_TABLE[domain],
        report_date,
        with_price_history=domain == DOMAIN_SNAPSHOT,
        with_cash_flow_indirect=domain == DOMAIN_EXT,
    )


@dataclass(frozen=True)
class _DomainRules:
    """一个域的规则装配（每类原因一组；空 dict = 该域不用这类规则）。"""

    not_applicable: dict[str, RuleGroup]
    source_empty: dict[str, RuleGroup] = field(default_factory=dict)
    invalid_denominator: dict[str, RuleGroup] = field(default_factory=dict)
    not_computed: dict[str, RuleGroup] = field(default_factory=dict)
    source_missing: dict[str, RuleGroup] = field(default_factory=dict)


def _rules_for_domain(domain: str) -> _DomainRules:
    if domain == DOMAIN_SNAPSHOT:
        return _DomainRules(
            not_applicable=SNAPSHOT_NOT_APPLICABLE,
            invalid_denominator=SNAPSHOT_INVALID_DENOMINATOR,
            not_computed=SNAPSHOT_NOT_COMPUTED,
        )
    if domain == DOMAIN_EXT:
        return _DomainRules(
            not_applicable=EXT_NOT_APPLICABLE,
            source_empty=EXT_SOURCE_EMPTY,
            invalid_denominator=EXT_INVALID_DENOMINATOR,
            not_computed=EXT_NOT_COMPUTED,
            source_missing=EXT_SOURCE_MISSING,
        )
    # 现金流量补充域：结构性缺失是**确定事实**（东财模板里就没有这个科目），
    # 东财行 + 该列为 NULL ⇒ SOURCE_EMPTY。其余字段不套用覆盖率规则（见 sweep），
    # 判不出的保持 unclassified。
    structural = _CASHFLOW_STRUCTURAL_SOURCE_EMPTY.get(domain)
    if structural is not None:
        return _DomainRules(not_applicable={}, source_empty=structural)
    # 源表域：目前只登记了"非金融公司不适用银行监管指标"这一条确定规则；
    # 其余源字段的原因主要来自覆盖率（整期级/个股级 NOT_DISPATCHED）。
    not_applicable = {}
    if domain == "balance":
        not_applicable = {**BALANCE_NOT_APPLICABLE, **BALANCE_SUBJECT_NOT_APPLICABLE}
    return _DomainRules(not_applicable=not_applicable)


def sweep(
    store: DuckDBStore,
    *,
    domain: str,
    report_date: date | str | None = None,
    write: bool = False,
    misaligned_coverage_limit: float = _MISALIGNED_FIELD_COVERAGE_LIMIT,
) -> SweepResult:
    """清扫一个域，返回统计（`write=True` 才落库，默认只读）。

    dry-run 与真写走**同一条**分类路径：先算全量 → 再决定要不要 upsert，
    避免"预演说 3 万行、真写 2 万行"这种对不上账的结果。
    """
    if domain not in DOMAINS:
        raise ValueError(f"unknown indicator domain: {domain!r} (expect one of {DOMAINS})")
    started = time.monotonic()
    table = _DOMAIN_TABLE[domain]
    fields = tuple(sorted(domain_fields(domain)))
    resolved_date = _domain_report_date(store, table, report_date)
    result = SweepResult(domain=domain, report_date=resolved_date, table=table, dry_run=not write)
    if resolved_date is None:
        result.duration_seconds = time.monotonic() - started
        return result

    rules = _rules_for_domain(domain)

    # 本期单元格 + 近两期历史（覆盖率与个股级证据都从这三帧里算）
    rows = _load_domain_rows(store, domain, resolved_date, fields)
    result.rows_scanned = len(rows)
    current = _coverage_from_rows(rows, fields)
    # 历史帧只读一次：覆盖率（整期级规则）与"哪些股票有值"（个股级规则）共用
    history_frames: list[tuple[date, list[dict[str, Any]]]] = [
        (previous_date, _load_cell_rows(store, domain, previous_date, fields))
        for previous_date in _previous_report_dates(
            store, table, resolved_date, _NOT_DISPATCHED_LOOKBACK_PERIODS
        )
    ]
    history = [
        (previous_date, len(previous_rows), _coverage_from_rows(previous_rows, fields))
        for previous_date, previous_rows in history_frames
    ]

    # 现金流量补充资料两表**不套用整期级覆盖率规则**（2026-11-17，reports/135）。
    # 理由（正式库实测）：`cash_flow_indirect` @2025-09-30 有 5,580 行、
    # `cf_from_operating_indirect` 基本全有值，但补充资料族（折旧摊销/营运资本）
    # 三季报普遍不披露 → 整期级规则会把 13+ 个字段判成"没去抓"（77,345 格），
    # 真实原因却是"源没披露"。这两张表的行**已经抓到了**，"有没有值"是源侧事实，
    # 不需要用覆盖率反推。故这里只保留逐行可证明的原因（结构性缺失 SOURCE_EMPTY）；
    # 判不出的保持 unclassified，绝不批量编 NOT_DISPATCHED。
    # （指标域 `ext` 的折旧摊销族用 EXT_SOURCE_EMPTY/EXT_SOURCE_MISSING 区分，
    #  那条链不受影响。）
    if domain in _CASHFLOW_STRUCTURAL_SOURCE_EMPTY:
        not_dispatched: dict[str, str] = {}
    else:
        excluded_fields = (
            frozenset(_DEPRECIATION_FIELDS) if domain == DOMAIN_EXT else frozenset()
        )
        not_dispatched = _not_dispatched_fields(
            fields=fields,
            report_date=resolved_date,
            total_rows=len(rows),
            current=current,
            history=history,
            excluded=excluded_fields,
        )
    result.not_dispatched_fields = tuple(sorted(not_dispatched))

    # 个股级证据：**只对 6 个银行监管指标**成立（"这只股票上期有值、本期空了"）。
    # 为什么不像整期级那样全字段适用：普通科目"上期有值、本期 NULL"可能压根不是没抓
    # —— 债券还完了、交易性金融资产清仓了，科目本身就消失了。监管指标不同：它要么
    # 抓到要么没抓到，不存在"科目归零"，所以个股级证据在这里是硬的
    # （实测：全字段套用会多写 2,917 格，其中只有 228 格是监管指标）。
    per_stock_evidence: dict[str, dict[str, date]] = {}
    per_stock_fields = tuple(name for name in fields if name in _REGULATORY_FIELDS)
    if per_stock_fields:
        per_stock_evidence = _per_stock_dispatched(
            fields=per_stock_fields,
            history=[
                (previous_date, _present_stocks(previous_rows, per_stock_fields))
                for previous_date, previous_rows in history_frames
            ],
        )

    misaligned_by_field: dict[str, str] = {}
    if domain == DOMAIN_EXT:
        snapshot_latest, ext_latest = _report_period_gap(store)
        if snapshot_latest is not None and ext_latest is not None and ext_latest < snapshot_latest:
            result.report_period_misaligned = True
            result.report_period_detail = (
                f"报告期未对齐：indicator_ext 最新 {ext_latest} < indicator_snapshot 最新"
                f" {snapshot_latest}，扩展指标未按新报告期重算"
            )
            misaligned_by_field = _misaligned_fields(
                fields=fields,
                total_rows=len(rows),
                current=current,
                detail=result.report_period_detail,
                coverage_limit=misaligned_coverage_limit,
            )

    planned: list[NullReasonRow] = []
    for field_name in fields:
        field_history = per_stock_evidence.get(field_name, {})
        for row in rows:
            if row.get(field_name) is not None:
                continue
            result.null_cells += 1
            verdict = _classify_cell(
                field_name=field_name,
                row=row,
                not_applicable=rules.not_applicable,
                source_empty=rules.source_empty,
                invalid_denominator=rules.invalid_denominator,
                not_computed=rules.not_computed,
                source_missing=rules.source_missing,
                per_stock_dispatched=field_history,
                not_dispatched=not_dispatched,
                misaligned_detail=misaligned_by_field.get(field_name),
            )
            if verdict is None:
                result.unclassified[field_name] = result.unclassified.get(field_name, 0) + 1
                continue
            reason, detail = verdict
            if reason is NullReason.NOT_DISPATCHED and detail.startswith(_PER_STOCK_DISPATCH_PREFIX):
                result.per_stock_dispatched_cells += 1
            planned.append(
                NullReasonRow(
                    stock_code=str(row["stock_code"]),
                    report_date=resolved_date,
                    field_name=field_name,
                    reason_code=reason,
                    detail=detail,
                )
            )
            result.by_reason[reason.value] = result.by_reason.get(reason.value, 0) + 1
            buckets = result.by_field.setdefault(field_name, {})
            buckets[reason.value] = buckets.get(reason.value, 0) + 1

    result.planned_rows = len(planned)
    if write:
        # 空结果也要清掉旧记录：字段全部修好时，"原因"必须跟着消失
        result.written_rows = upsert_reasons(
            store, planned, replace_scope=(resolved_date, domain)
        )

    result.duration_seconds = time.monotonic() - started
    logger.info(
        "null reason sweep %s %s: rows=%s nulls=%s planned=%s unclassified=%s (%.2fs, write=%s)",
        domain,
        resolved_date,
        result.rows_scanned,
        result.null_cells,
        result.planned_rows,
        result.unclassified_cells,
        result.duration_seconds,
        write,
    )
    return result


def sweep_all(
    store: DuckDBStore,
    *,
    report_date: date | str | None = None,
    write: bool = False,
) -> dict[str, SweepResult]:
    """对全部域依次清扫（snapshot 先于 ext：ext 的对齐判定依赖 snapshot 的最新期）。"""
    return {
        domain: sweep(store, domain=domain, report_date=report_date, write=write)
        for domain in DOMAINS
    }
