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
# 10) v37 指标护栏（2026-09-19 复审补加）。
# 复审发现 v37 的 12 列**漏加护栏**，产生了大量业务无意义极值：
#   cash_ratio 最大 213,591、accruals 最大 24,129（应计项目的数学上限是 2）、
#   ocf_to_operating_profit 最小 -57,361。原因是分母趋 0 时比率发散。
# 与既有 pe<=1000 / |roe|<=1 同思路：触发即 NULL，绝不展示会误导人的数字。
CASH_RATIO_MAX = 100.0          # 现金及等价物 / 流动负债
QUICK_RATIO_MAX = 100.0         # 保守速动比率
DEBT_TO_EQUITY_MAX = 100.0      # 产权比率（权益<=0 时无业务含义，另行置 NULL）
TANGIBLE_DEBT_MAX = 100.0       # 有形净值债务率（有形净值<=0 时无业务含义）
OCF_TO_LIAB_MAX = 100.0         # 经营现金流 / 负债
EBITDA_TO_LIAB_MAX = 100.0      # EBITDA / 负债
CASH_CONTENT_MAX = 10.0         # 销售收现 / 营业收入（正常 0.5~1.5）
OCF_TO_OP_PROFIT_MAX = 100.0    # 经营现金流 / 营业利润
ACCRUALS_MAX = 2.0              # (净利−经营现金流)/总资产：数学上限即 ±2
PER_SHARE_MAX = 1.0e6           # 每股类上限（元/股）
# 5) 费用率：分母（营业收入）极小时比率发散；|费用率| > 500% 判为无意义。
EXPENSE_RATIO_MAX_ABS = 5.0
# 6) 权益乘数 = 总资产/总权益；权益趋 0 时发散。>100 判为无意义（净资产为负时
#    乘数为负，同样无业务含义，故用 ABS）。
EQUITY_MULTIPLIER_MAX = 100.0
# 7) 大股东占款 = (其他应收款 − 其他应付款) / 总资产（CSMAR BDT_FinIndex.ShareholdersOccupy
#    同口径）。合计项已归一到总资产，绝对值 > 200% 判为异常（口径不匹配或数据错）。
SHAREHOLDER_OCCUPATION_MAX_ABS = 2.0
# 8) 人均指标：员工数极少（如 1 人）时人均额会失真；|人均创收| > 10 亿元/人
#    或 |人均创利| > 5 亿元/人 判为口径异常。
REVENUE_PER_EMPLOYEE_MAX = 1e9
PROFIT_PER_EMPLOYEE_MAX = 5e8
# 9) 估值衍生（v30）：托宾Q / 账面市值比 / EV-EBITDA 的合理上界。
#    分母（总资产/市值/EBITDA）趋 0 或为负时这些倍数没有业务含义。
TOBIN_Q_MAX = 100.0
BOOK_TO_MARKET_MAX = 10.0
# 注意：EV/EBITDA **允许为负**且负值有意义 —— 现金超过「市值+有息负债」的
# 净现金公司，其企业价值为负，代表「市场把主业定价为零甚至倒贴」
# （实测长虹美菱 2026H1：货币资金 99.02 亿 > 市值 55.92 亿 + 有息负债 9.48 亿）。
# 用 ABS 判定只是为了拦掉分母趋 0 导致的病态值，不是禁止负值。
EV_EBITDA_MAX = 1000.0

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
        ebitda_val.ebitda AS ebitda,
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
        -- 每股净资产 = 股东权益合计 / 股数（CSMAR FI_T9.F091001A 口径）。
        -- 2026-09-19 交叉核验修正：此处原先误用归母权益，与 CSMAR 一致率仅 25%；
        -- 改用股东权益合计后 95.03%（1% 容差 96.67%）。
        CASE WHEN sh.total_shares > 0 AND b.total_equity IS NOT NULL
             THEN b.total_equity / sh.total_shares END AS bps,
        -- 归属母公司每股净资产 = 归母权益 / 股数（CSMAR FI_T9.F091701A，一致率 95.04%）
        CASE WHEN sh.total_shares > 0 AND b.total_equity_parent IS NOT NULL
             THEN b.total_equity_parent / sh.total_shares END AS bps_parent,
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
        -- ─── 治理红旗（v27）──────────────────────────────────────────
        -- 大股东占款 = (其他应收款 − 其他应付款) / 总资产。
        -- 正值越大，说明大股东/关联方占用上市公司资金越多。
        -- 分子优先用 balance_sheet_ext 的合计项（与应付侧同源配对）；缺失时回退到
        -- 主链 other_receivables（净额），两侧口径不完全对齐时如实可能低估。
        CASE WHEN b.total_assets IS NULL OR b.total_assets = 0 THEN NULL
             WHEN COALESCE(bsx.total_other_receivable, b.other_receivables) IS NULL
                  OR bsx.other_payables IS NULL THEN NULL
             WHEN ABS((COALESCE(bsx.total_other_receivable, b.other_receivables)
                       - bsx.other_payables) / b.total_assets) > {SHAREHOLDER_OCCUPATION_MAX_ABS}
                  THEN NULL
             ELSE (COALESCE(bsx.total_other_receivable, b.other_receivables)
                   - bsx.other_payables) / b.total_assets
        END AS shareholder_occupation,
        -- ─── 人效（v28）─────────────────────────────────────────────
        -- 分母取「观测日最接近报告期」的员工数（过去优先）：
        --   · 历史期 → CSMAR FAR_Finidx.Nstaff 的当年员工数（时点正确）
        --   · 最近期 → company_profile 的当前快照（CSMAR 只到 2024 年报）
        -- 若一律用当前快照，历史期就会「今天的员工数 ÷ 当年的收入」（D23 同类错误）。
        -- ─── 报告期市值与估值衍生（v30）─────────────────────────────
        -- 市值 = 报告期当日原始收盘价 × 报告期时点股本。
        -- **不得**用 stock_meta.total_shares（当前股本）——那是 D23 已知缺陷的口径。
        CASE WHEN sh.total_shares > 0 THEN px.close * sh.total_shares END AS market_cap_at_report,
        -- 托宾Q = (股权市值 + 总负债) / 总资产（与 CSMAR 市值A 口径一致：含负债）
        CASE WHEN sh.total_shares > 0 AND px.close IS NOT NULL
                  AND b.total_assets > 0 AND b.total_liabilities IS NOT NULL
                  AND (px.close * sh.total_shares + b.total_liabilities) / b.total_assets
                      <= {TOBIN_Q_MAX}
             THEN (px.close * sh.total_shares + b.total_liabilities) / b.total_assets
        END AS tobin_q,
        -- 账面市值比 = 归母权益 / 股权市值
        CASE WHEN sh.total_shares > 0 AND px.close > 0
                  AND COALESCE(b.total_equity_parent, b.total_equity) > 0
                  AND COALESCE(b.total_equity_parent, b.total_equity)
                      / (px.close * sh.total_shares) <= {BOOK_TO_MARKET_MAX}
             THEN COALESCE(b.total_equity_parent, b.total_equity)
                  / (px.close * sh.total_shares)
        END AS book_to_market,
        -- 企业价值倍数 = (股权市值 + 有息负债 − 货币资金) / EBITDA
        CASE WHEN sh.total_shares > 0 AND px.close IS NOT NULL
                  AND ebitda_val.ebitda IS NOT NULL AND ebitda_val.ebitda > 0
                  AND ABS((px.close * sh.total_shares
                           + COALESCE(b.short_term_loans, 0) + COALESCE(b.long_term_loans, 0)
                           + COALESCE(b.bonds_payable, 0) + COALESCE(bsx.non_current_liab_due_1y, 0)
                           - COALESCE(b.monetary_funds, 0)) / ebitda_val.ebitda)
                      <= {EV_EBITDA_MAX}
             THEN (px.close * sh.total_shares
                   + COALESCE(b.short_term_loans, 0) + COALESCE(b.long_term_loans, 0)
                   + COALESCE(b.bonds_payable, 0) + COALESCE(bsx.non_current_liab_due_1y, 0)
                   - COALESCE(b.monetary_funds, 0)) / ebitda_val.ebitda
        END AS ev_ebitda,
        px.close AS report_date_close,
        -- ─── v37：CSMAR 有价值新概念的自算落地 ────────────────────────
        -- 公式取自 CSMAR 说明书（见 .planning 的 csmar_formulas.json），
        -- 但用本项目数据计算 → 覆盖最新报告期，可直接进筛选界面。
        CASE WHEN b.total_current_liabilities > 0 AND c.cash_ending IS NOT NULL
                  AND ABS(c.cash_ending / b.total_current_liabilities) <= {CASH_RATIO_MAX}
             THEN c.cash_ending / b.total_current_liabilities END AS cash_ratio,
        CASE WHEN b.total_current_liabilities > 0
                  AND ABS((COALESCE(b.monetary_funds,0) + COALESCE(b.trading_financial_assets,0)
                       + COALESCE(b.notes_receivable,0) + COALESCE(b.accounts_receivable,0))
                      / b.total_current_liabilities) <= {QUICK_RATIO_MAX}
             THEN (COALESCE(b.monetary_funds,0) + COALESCE(b.trading_financial_assets,0)
                   + COALESCE(b.notes_receivable,0) + COALESCE(b.accounts_receivable,0))
                  / b.total_current_liabilities END AS conservative_quick_ratio,
        CASE WHEN b.total_equity > 0 AND ABS(b.total_liabilities / b.total_equity) <= {DEBT_TO_EQUITY_MAX}
             THEN b.total_liabilities / b.total_equity END AS debt_to_equity,
        CASE WHEN (b.total_equity - COALESCE(b.intangible_assets,0) - COALESCE(b.goodwill,0)) > 0
                  AND ABS(b.total_liabilities
                          / (b.total_equity - COALESCE(b.intangible_assets,0) - COALESCE(b.goodwill,0)))
                      <= {TANGIBLE_DEBT_MAX}
             THEN b.total_liabilities
                  / (b.total_equity - COALESCE(b.intangible_assets,0) - COALESCE(b.goodwill,0))
        END AS tangible_net_debt_ratio,
        CASE WHEN b.total_liabilities > 0 AND c.cf_from_operating IS NOT NULL
                  AND ABS(c.cf_from_operating / b.total_liabilities) <= {OCF_TO_LIAB_MAX}
             THEN c.cf_from_operating / b.total_liabilities END AS ocf_to_liabilities,
        CASE WHEN b.total_liabilities > 0 AND ebitda_val.ebitda IS NOT NULL
                  AND ABS(ebitda_val.ebitda / b.total_liabilities) <= {EBITDA_TO_LIAB_MAX}
             THEN ebitda_val.ebitda / b.total_liabilities END AS ebitda_to_liabilities,
        -- 营业收入现金含量：销售商品提供劳务收到的现金 / 营业收入（CSMAR FI_T6.F060201B）
        CASE WHEN i.revenue > 0 AND c.cash_received_sales IS NOT NULL
                  AND ABS(c.cash_received_sales / i.revenue) <= {CASH_CONTENT_MAX}
             THEN c.cash_received_sales / i.revenue END AS cash_content_of_revenue,
        -- 营业利润现金净含量：经营现金流净额 / 营业利润（CSMAR FI_T6.F060401B）
        CASE WHEN i.operating_profit > 0 AND c.cf_from_operating IS NOT NULL
                  AND ABS(c.cf_from_operating / i.operating_profit) <= {OCF_TO_OP_PROFIT_MAX}
             THEN c.cf_from_operating / i.operating_profit END AS ocf_to_operating_profit,
        -- 应计项目（本项目口径，简化式）：(净利润 − 经营现金流) / 总资产。
        -- 正值越大说明利润里"没收到钱"的部分越多，是盈余质量的负面信号。
        CASE WHEN b.total_assets > 0 AND i.net_profit IS NOT NULL
                  AND c.cf_from_operating IS NOT NULL
                  AND ABS((i.net_profit - c.cf_from_operating) / b.total_assets) <= {ACCRUALS_MAX}
             THEN (i.net_profit - c.cf_from_operating) / b.total_assets END AS accruals,
        -- 每股族（分母同为「当时股数」）
        CASE WHEN sh.total_shares > 0
                  AND (b.total_assets - COALESCE(b.intangible_assets,0) - COALESCE(b.goodwill,0))
                      / sh.total_shares <= {PER_SHARE_MAX}
             THEN (b.total_assets - COALESCE(b.intangible_assets,0) - COALESCE(b.goodwill,0))
                  / sh.total_shares END AS tangible_asset_per_share,
        CASE WHEN sh.total_shares > 0 AND b.total_liabilities >= 0
                  AND b.total_liabilities / sh.total_shares <= {PER_SHARE_MAX}
             THEN b.total_liabilities / sh.total_shares END AS liability_per_share,
        CASE WHEN sh.total_shares > 0 AND b.capital_reserve IS NOT NULL
                  AND ABS(b.capital_reserve / sh.total_shares) <= {PER_SHARE_MAX}
             THEN b.capital_reserve / sh.total_shares END AS capital_reserve_per_share,
        eh.employee_count,
        CASE WHEN eh.employee_count > 0 AND ABS(i.revenue / eh.employee_count) <= {REVENUE_PER_EMPLOYEE_MAX}
             THEN i.revenue / eh.employee_count END AS revenue_per_employee,
        CASE WHEN eh.employee_count > 0 AND i.net_profit IS NOT NULL
                  AND ABS(i.net_profit / eh.employee_count) <= {PROFIT_PER_EMPLOYEE_MAX}
             THEN i.net_profit / eh.employee_count END AS profit_per_employee,
        CAST(? AS TIMESTAMP) AS calculated_at,
        ? AS source,
        ? AS data_version
    FROM income_statement i
    JOIN balance_sheet b
      ON b.stock_code = i.stock_code AND b.report_date = i.report_date
    LEFT JOIN LATERAL (
        SELECT p.close FROM price_daily_raw p
        WHERE p.stock_code = i.stock_code AND p.trade_date <= i.report_date
        ORDER BY p.trade_date DESC LIMIT 1
    ) px ON true
    LEFT JOIN cash_flow c
      ON c.stock_code = i.stock_code AND c.report_date = i.report_date
    LEFT JOIN cash_flow_indirect ci
      ON ci.stock_code = i.stock_code AND ci.report_date = i.report_date
    LEFT JOIN LATERAL (
        SELECT CASE WHEN {_EBIT_READY} AND {_DEP_READY} THEN {_EBIT} + {_DEPRECIATION} END AS ebitda
    ) ebitda_val ON true
    LEFT JOIN cash_flow_activity ca
      ON ca.stock_code = i.stock_code AND ca.report_date = i.report_date
    LEFT JOIN balance_sheet_ext bsx
      ON bsx.stock_code = i.stock_code AND bsx.report_date = i.report_date
    LEFT JOIN LATERAL (
        SELECT eh.employee_count FROM company_employee_history eh
        WHERE eh.stock_code = i.stock_code
        ORDER BY ABS(date_diff('day', eh.report_date, i.report_date)),
                 CASE WHEN eh.report_date <= i.report_date THEN 0 ELSE 1 END
        LIMIT 1
    ) eh ON true
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
