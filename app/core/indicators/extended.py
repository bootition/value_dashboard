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
from typing import TYPE_CHECKING, Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.update_lock import exclusive_update

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from app.core.storage.path_policy import DatabasePathSet

logger = logging.getLogger(__name__)

DATA_VERSION = 1
SOURCE = "derived_calculator"
#: 输入指纹落在 data_refresh_state 的键（自动更新用它判断"该不该重建"）
FINGERPRINT_STATE_KEY = "indicator_ext_input_fingerprint"

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
# 11) v38 事件类指标护栏
DILUTION_MAX = 5.0          # 近3年融资稀释率（>500% 视为数据异常）
BUYBACK_MAX = 1.0           # 回购占比不可能超过总股本
BONUS_MAX = 20.0            # 近3年送转比例合计上限
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
# 统一 EBIT 表达式（2026-09-20）：首选标准式，缺失时回退「利润总额 + 财务费用」。
# **必须所有用到 EBIT 的指标共用同一表达式** —— 首版只改了 ebit 列，
# 杠杆公式仍用 _EBIT_READY 判定，导致回退失效（覆盖率原地不动）。
# 用 f-string **直接展开**：外层 SQL 模板的 .format() 只替换一层，
# 若这里留 {占位符} 会原样进入 SQL 导致语法错误（首版踩过，覆盖率掉到 0）。
_EBIT_EXPR = (
    f"(CASE WHEN {_EBIT_READY} THEN {_EBIT} "
    f"WHEN i.total_profit IS NOT NULL "
    f"THEN i.total_profit + COALESCE(i.financial_expenses, 0) END)"
)
_EBIT_AVAILABLE = f"(i.total_profit IS NOT NULL OR {_EBIT_READY})"
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
        -- EBIT 口径（2026-09-20 补回退）：首选「净利润 + 所得税 + 财务费用」；
        -- 金融企业（保险/券商）利润表不单列所得税与财务费用，实测 144 只因此缺失
        -- → 回退用「利润总额 + 财务费用」（这 144 只的利润总额 100% 可用）。
        -- 两式在标准利润表下等价：利润总额 = 净利润 + 所得税。
        {_EBIT_EXPR} AS ebit,
        ebitda_val.ebitda AS ebitda,
        -- 财务杠杆 = EBIT / 利润总额（CSMAR FI_T7.F070101B）；亏损时无业务含义
        CASE WHEN {_EBIT_AVAILABLE} AND i.total_profit > 0 AND {_EBIT_EXPR} > 0
                  AND ABS({_EBIT_EXPR} / i.total_profit) <= {LEVERAGE_MAX}
             THEN {_EBIT_EXPR} / i.total_profit END AS leverage_financial,
        -- 经营杠杆 = EBITDA / EBIT（CSMAR FI_T7.F070201B）；EBIT<=0 时无业务含义
        CASE WHEN {_EBIT_AVAILABLE} AND {_DEP_READY} AND {_EBIT_EXPR} > 0
                  AND ABS(({_EBIT_EXPR} + {_DEPRECIATION}) / {_EBIT_EXPR}) <= {LEVERAGE_MAX}
             THEN ({_EBIT_EXPR} + {_DEPRECIATION}) / {_EBIT_EXPR} END AS leverage_operating,
        -- 综合杠杆 = EBITDA / 利润总额（CSMAR FI_T7.F070301B）；亏损时无业务含义
        CASE WHEN {_EBIT_AVAILABLE} AND {_DEP_READY} AND i.total_profit > 0
                  AND ABS(({_EBIT_EXPR} + {_DEPRECIATION}) / i.total_profit) <= {LEVERAGE_MAX}
             THEN ({_EBIT_EXPR} + {_DEPRECIATION}) / i.total_profit END AS leverage_total,
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
        -- ─── v38：事件类指标（把「在库但没用起来」的数据落地）────────
        -- 无融资事件 = 三年零稀释，语义上就是 0（funding_events 含 IPO，
        -- 已验证覆盖 100% 的在市股票，故"窗口内无记录"等价于"窗口内无融资"）。
        CASE WHEN sh.total_shares > 0
                  AND COALESCE(fe.issued, 0) / sh.total_shares <= {DILUTION_MAX}
             THEN COALESCE(fe.issued, 0) / sh.total_shares END AS dilution_3y,
        -- 同上，记为 0。注意 buyback_events 覆盖 51.8%（回购为自愿披露），
        -- 未记录不等于绝对没回购 —— 该口径局限已在域说明中登记。
        CASE WHEN sh.total_shares > 0
                  AND COALESCE(bb.bought, 0) / sh.total_shares <= {BUYBACK_MAX}
             THEN COALESCE(bb.bought, 0) / sh.total_shares END AS buyback_ratio_3y,
        CASE WHEN COALESCE(xr.bonus, 0) <= {BONUS_MAX} THEN xr.bonus END AS bonus_share_ratio_3y,
        seg.top_share AS top_segment_share,
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
        -- 近 3 年融资发行股数合计（真实稀释，不含送转）
        SELECT SUM(NULLIF(f.issue_shares, 0)) AS issued FROM funding_events f
        WHERE f.stock_code = i.stock_code
          AND f.list_date BETWEEN i.report_date - INTERVAL 3 YEAR AND i.report_date
    ) fe ON true
    LEFT JOIN LATERAL (
        -- 近 3 年回购股数合计
        SELECT SUM(NULLIF(b.buyback_shares, 0)) AS bought FROM buyback_events b
        WHERE b.stock_code = i.stock_code
          AND COALESCE(b.start_date, b.announce_date)
              BETWEEN i.report_date - INTERVAL 3 YEAR AND i.report_date
    ) bb ON true
    LEFT JOIN LATERAL (
        -- 近 3 年送转比例合计（每 10 股送转 X 股 → 换算成比例）
        SELECT SUM(COALESCE(x.songzhuangu, 0)) / 10.0 AS bonus FROM xdxr x
        WHERE x.stock_code = i.stock_code
          AND x.event_date BETWEEN i.report_date - INTERVAL 3 YEAR AND i.report_date
    ) xr ON true
    LEFT JOIN LATERAL (
        -- 最新可见报告期的最大业务收入占比（业务集中度）。
        -- **不用** business_breakdown.ratio：实测该字段并非干净占比
        -- （179 行 >100%，最大 1136%；7,226 个 (股票,期) 组合计 >105）。
        -- 改为用 amount 自算 最大业务 / 该期合计，天然落在 (0, 1]。
        -- 只在**正收入**分段上计算：分业务收入可以为负（亏损业务），
        -- 用 MAX/SUM(全量) 会得出 >1 的伪占比（实测最大 11.36）。
        SELECT CASE WHEN SUM(CASE WHEN sb.amount > 0 THEN sb.amount END) > 0
                    THEN MAX(CASE WHEN sb.amount > 0 THEN sb.amount END)
                         / SUM(CASE WHEN sb.amount > 0 THEN sb.amount END) END AS top_share
        FROM business_breakdown sb
        WHERE sb.stock_code = i.stock_code AND sb.type = 2
          AND sb.report_date = (SELECT MAX(sb2.report_date) FROM business_breakdown sb2
                                WHERE sb2.stock_code = i.stock_code AND sb2.type = 2
                                  AND sb2.report_date <= i.report_date)
    ) seg ON true
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
        SELECT CASE WHEN {_EBIT_AVAILABLE} AND {_DEP_READY}
                    THEN {_EBIT_EXPR} + {_DEPRECIATION} END AS ebitda
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

    def __init__(self, duck: DuckDBStore, paths: DatabasePathSet | None = None) -> None:
        self._duck = duck
        self._paths = paths

    def _lock_path(self):
        """更新锁所在路径：优先注入的 DatabasePathSet，回退 DuckDB 自身路径。

        自动更新主流程（`app/core/update.py`）只持有 duck/sqlite 两个 store，
        没有 DatabasePathSet；用 `duck.db_path` 得到的锁文件位置与之等价，
        且 `exclusive_update` 对同一进程是重入的（主流程已持锁不会卡死）。
        """
        return self._paths.duckdb_path if self._paths is not None else self._duck.db_path

    def build(
        self,
        *,
        codes: list[str] | None = None,
        force_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """重建 indicator_ext（全量 / 指定股票 / 仅股本变更股）。

        Args:
            codes: 显式指定的股票清单（``vd data compute-extended-indicators --codes``）。
            force_codes: 「股本链变了但派生值还是旧的」股票清单（reports/131
                §9.2 P0-9 修法②），由 :func:`rebuild_decision` 或
                :func:`share_capital_drift_codes` 给出。与 ``codes`` 互斥：
                ``codes`` 优先。传**空列表**表示"没有要重建的股票"，直接返回
                ``skipped``，绝不退化成全量重建（那会把一次 140 ms 的探针变成
                40 万行整表重写）。

        两者都为 None（默认）→ 全量重建，行为与 2026-11-16 之前完全一致。
        """
        target: list[str] | None = None
        scope = "all"
        if codes is not None:
            target = list(dict.fromkeys(str(code) for code in codes if str(code).strip()))
            scope = "codes"
        elif force_codes is not None:
            target = list(dict.fromkeys(str(code) for code in force_codes if str(code).strip()))
            scope = "force_codes"
        if target is not None and not target:
            report = {
                "status": "skipped",
                "reason": "no_target_codes",
                "rows_before": int(self._duck.read_query("SELECT COUNT(*) AS n FROM indicator_ext")[0]["n"]),
                "rows_after": int(self._duck.read_query("SELECT COUNT(*) AS n FROM indicator_ext")[0]["n"]),
                "rows_written": 0,
                "scope": scope,
                "filled": {},
                "report_date_range": [],
                "data_version": DATA_VERSION,
            }
            logger.info("indicator_ext 构建跳过（目标清单为空）: %s", report)
            return report

        calculated_at = datetime.now(UTC).replace(tzinfo=None)
        sql = build_select_sql(target)
        with exclusive_update(self._lock_path()), self._duck.write_connection() as conn:
            before = conn.execute("SELECT COUNT(*) FROM indicator_ext").fetchone()[0]
            if target:
                quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in target)
                conn.execute(f"DELETE FROM indicator_ext WHERE stock_code IN ({quoted})")
            else:
                conn.execute("DELETE FROM indicator_ext")
            conn.execute(sql, [calculated_at, SOURCE, DATA_VERSION])
            after = conn.execute("SELECT COUNT(*) FROM indicator_ext").fetchone()[0]
            # rows_written 必须报**本次实际写入的行数**，不是全表 diff 也不是股票数：
            #   · 全量重建：整表都是本次写的 → rows_after；
            #   · 按股重建：只数目标股票的行（一只股票每期一行，可能是多行）。
            # 旧写法 `after - before if not target else after` 在正式库上会把
            # force_codes=[689009] 报成 361393 行、把全量重建报成 0 行。
            if target:
                rows_written = conn.execute(
                    f"SELECT COUNT(*) FROM indicator_ext WHERE stock_code IN ({quoted})"
                ).fetchone()[0]
            else:
                rows_written = after
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
            "rows_written": int(rows_written),
            "scope": scope,
            "codes": target,
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


# ─── 自动更新门控：什么时候必须重建 indicator_ext ────────────────────────
#
# reports/119 D28 的教训：派生域没有"谁触发重建"的判据，就会在一个季度内
# 变成死条件（ext 停在 2026-06-30，55 项扩展筛选字段全为 NULL）。
# 这里把判据收敛为**两个明确条件**：
#   1) `max(indicator_ext.report_date) < 最新完整三表报告期` → 必须重建；
#   2) 否则只在输入指纹变化时重建（三表/补采表的最新报告期或行数变化，
#      **外加** `share_capital_history` 的 400 天尾段摘要 —— 见下方
#      `share_capital_state`，这是 P0-9 事故的直接修法）。
# 指纹**不含**日线价格：`report_date_close` 依赖行情，但日线每天变化，
# 若纳入指纹会让 35 万行整表每晚重建一次；报告期推进时自然会重建一次。

#: 三表"完整报告期"的覆盖率下限：最新一期的三表齐备股票数必须达到历史上
#  单期最大股票数的该比例，才算这个报告期已经完整发布（避免"个别抢先
#  披露的股票"把完整期提前一个季度）。
TRIO_COMPLETE_RATIO = 0.5

#: 参与输入指纹的表：只放**报表期粒度**的输入。事件表（分红/融资/回购）
#: 与日线价格不参与——它们的变化不该按日触发整表重建。
#:
#: **2026-11-16（reports/131 §9.2 P0-9）**：`share_capital_history` 必须进指纹。
#: 它没有 report_date，走 :func:`share_capital_state` 的**尾段 400 天摘要**，
#: 不按整表（24.5 万行）算指纹。事故现场：689009（九号公司 CDR）全链带
#: `change_reason='unit_rescale:10'`（股本链后来被 ×10 校正），但 indicator_ext
#: 是校正**之前**构建的 —— 旧指纹里没有股本链，`_refresh_indicator_ext()` 于是
#: 判"输入没变、不必重建"，24 期 `market_cap_at_report` 与 14 个每股族派生列
#: 静默错 10 倍（2025-12-31 应 400.4 亿、表内 40.04 亿）。
_INPUT_TABLES: tuple[str, ...] = (
    "income_statement",
    "balance_sheet",
    "cash_flow",
    "cash_flow_indirect",
    "cash_flow_activity",
    "balance_sheet_ext",
)

#: 股本链指纹窗口（天）。取 400 天而不是整表：一是"最近一年多有没有人动过
#: 股本链"已足以回答"派生表是不是旧的"，二是整表 24.5 万行做摘要会随历史
#: 增长线性变慢，而 400 天窗口恒定（正式库实测 13,933 行 / 11–25 ms）。
SHARE_CAPITAL_WINDOW_DAYS = 400

#: 股本链输入表。**故意不放进 `_INPUT_TABLES`**：那个循环假设表有 `report_date`
#: 列（`SELECT COUNT(*), MAX(report_date)`），而 `share_capital_history` 用
#: `effective_date`，只能走 :func:`share_capital_state` 的窗口摘要。
SHARE_CAPITAL_TABLE = "share_capital_history"

#: `market_cap_at_report` 与「重算值」的相对偏差容忍度。1e-9 = 仅浮点噪声；
#: 任何真实股本变更（哪怕是 0.4% 的股权激励）都会超过它。
SHARE_CAPITAL_DRIFT_TOLERANCE = 1e-9


def latest_complete_trio_period(duck: DuckDBStore) -> str | None:
    """最新"三表齐备"的报告期（按覆盖率门限判定，不取绝对值最大值）。"""
    try:
        rows = duck.read_query(
            """SELECT i.report_date AS report_date, COUNT(*) AS stocks
               FROM income_statement i
               JOIN balance_sheet b
                 ON b.stock_code = i.stock_code AND b.report_date = i.report_date
               JOIN cash_flow c
                 ON c.stock_code = i.stock_code AND c.report_date = i.report_date
               GROUP BY i.report_date
               ORDER BY i.report_date DESC"""
        )
    except Exception as error:  # noqa: BLE001 - 判据失败按"未知"处理
        logger.warning("查询三表完整报告期失败: %s", error)
        return None
    if not rows:
        return None
    peak = max(int(row["stocks"] or 0) for row in rows)
    if peak <= 0:
        return None
    for row in rows:  # report_date 降序：第一个达标的即最新完整期
        if int(row["stocks"] or 0) >= peak * TRIO_COMPLETE_RATIO:
            return str(row["report_date"])[:10]
    return None


def share_capital_state(
    duck: DuckDBStore,
    *,
    window_days: int = SHARE_CAPITAL_WINDOW_DAYS,
) -> dict[str, Any]:
    """`share_capital_history` 的**尾段窗口摘要**（P0-9 输入指纹用）。

    以表内最新 `effective_date` 为锚点，只对 `[anchor - window_days, anchor]`
    的行做 `stock_code:total_shares:effective_date` 摘要（MD5）。这样：

    - 摘要**随任何股本变更而变化**（新增/修改一行都会进 STRING_AGG）；
    - 成本恒定：正式库 400 天窗口 ≈ 13,933 行 / 11–25 ms，不随 24.5 万行
      历史增长；整表摘要会随历史线性变慢；
    - 只覆盖"近期股本"：2016 年的历史股本不会被后人重抓，不需要进指纹。

    摘要 SQL（`STRING_AGG ... ORDER BY` 保证顺序稳定，否则同一份数据会算出
    不同 MD5，指纹就永久"变化"、每轮都全量重建）::

        WITH anchor AS (SELECT MAX(effective_date) AS d FROM share_capital_history)
        SELECT COUNT(*) AS rows,
               MAX(h.effective_date) AS latest,
               COUNT(DISTINCT h.stock_code) AS stocks,
               MD5(STRING_AGG(
                   h.stock_code || ':' || CAST(h.total_shares AS VARCHAR)
                                || ':' || CAST(h.effective_date AS VARCHAR),
                   '|' ORDER BY h.stock_code, h.effective_date, h.total_shares
               )) AS digest
        FROM share_capital_history h, anchor
        WHERE h.effective_date >= anchor.d - INTERVAL '<window_days>' DAY

    任何异常（表缺失/损坏）都退化为空摘要，绝不因为指纹探针失败阻断主链。
    """
    days = max(int(window_days), 0)
    try:
        row = duck.read_query(
            f"""
            WITH anchor AS (SELECT MAX(effective_date) AS d FROM share_capital_history)
            SELECT COUNT(*) AS rows,
                   MAX(h.effective_date) AS latest,
                   COUNT(DISTINCT h.stock_code) AS stocks,
                   COALESCE(MD5(STRING_AGG(
                       h.stock_code || ':' || CAST(h.total_shares AS VARCHAR)
                       || ':' || CAST(h.effective_date AS VARCHAR),
                       '|' ORDER BY h.stock_code, h.effective_date, h.total_shares
                   )), '') AS digest
            FROM share_capital_history h, anchor
            WHERE h.effective_date >= anchor.d - INTERVAL '{days}' DAY
            """
        )[0]
    except Exception as error:  # noqa: BLE001 - 指纹探针失败按"无股本输入"处理
        logger.debug("读取 share_capital_history 状态失败: %s", error)
        return {"window_days": days, "rows": 0, "latest": None, "stocks": 0, "digest": ""}
    return {
        "window_days": days,
        "rows": int(row["rows"] or 0),
        "latest": str(row["latest"])[:10] if row.get("latest") else None,
        "stocks": int(row["stocks"] or 0),
        "digest": str(row["digest"] or ""),
    }


def share_capital_drift_codes(
    duck: DuckDBStore,
    *,
    window_days: int = SHARE_CAPITAL_WINDOW_DAYS,
    tolerance: float = SHARE_CAPITAL_DRIFT_TOLERANCE,
) -> list[str]:
    """「股本链变了但派生表还是旧的」的股票清单（= 按变更股重建的 force_codes）。

    为什么不用"外部状态表记录每股票股本摘要"：那需要新建表/新列（本轮硬约束
    禁止改结构），且状态一旦丢失就永久漏检。这里改成**自证式探针**：

    只对"股本链在窗口内动过"的股票（正式库 5,549 只）取它在 `indicator_ext`
    的**最新一行**，用与 ``build_select_sql`` 完全相同的口径重算
    ``market_cap_at_report``（ASOF 收盘价 × ASOF 时点股本），与表内值比较；
    相对偏差 > ``tolerance`` 即为陈旧。

    实测（2026-11-16 正式库）：命中 13 只，其中 689009 比值恰为 0.1
    （10 倍单位错），其余 12 只为 0.1%–0.6% 的股本链尾对齐差异；耗时 ~140 ms。

    探针是"有无陈旧"的充分判据而不是充分必要条件：它同时能抓住"价格链事后
    被修复"造成的陈旧，两者都该重建 —— 而它永远**不会**因为派生表新而误报。
    """
    days = max(int(window_days), 0)
    try:
        rows = duck.read_query(
            f"""
            WITH win AS (
                SELECT DISTINCT stock_code FROM share_capital_history
                WHERE effective_date >= (SELECT MAX(effective_date) FROM share_capital_history)
                                     - INTERVAL '{days}' DAY
            ),
            last_ext AS (
                SELECT stock_code, report_date, market_cap_at_report,
                       ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
                FROM indicator_ext
                WHERE stock_code IN (SELECT stock_code FROM win)
            )
            SELECT l.stock_code
            FROM last_ext l
            LEFT JOIN LATERAL (
                SELECT p.close FROM price_daily_raw p
                WHERE p.stock_code = l.stock_code AND p.trade_date <= l.report_date
                ORDER BY p.trade_date DESC LIMIT 1
            ) px ON true
            LEFT JOIN LATERAL (
                SELECT s.total_shares FROM share_capital_history s
                WHERE s.stock_code = l.stock_code AND s.effective_date <= l.report_date
                ORDER BY s.effective_date DESC LIMIT 1
            ) sh ON true
            WHERE l.rn = 1
              AND l.market_cap_at_report IS NOT NULL
              AND px.close IS NOT NULL
              AND sh.total_shares > 0
              AND ABS(l.market_cap_at_report / (px.close * sh.total_shares) - 1) > ?
            ORDER BY l.stock_code
            """,
            [float(tolerance)],
        )
    except Exception as error:  # noqa: BLE001 - 探针失败按"无陈旧"处理，不阻断
        logger.debug("股本链陈旧探针失败: %s", error)
        return []
    return [str(row["stock_code"]) for row in rows]


def _fingerprint_parts(fingerprint: str | None) -> dict[str, str]:
    """把 ``表:值`` 形式的指纹串拆成 dict（``|`` 分隔，值里不含 ``|``）。"""
    parts: dict[str, str] = {}
    for chunk in str(fingerprint or "").split("|"):
        key, separator, value = chunk.partition(":")
        if separator:
            parts[key.strip()] = value.strip()
    return parts


def input_state(duck: DuckDBStore) -> dict[str, Any]:
    """采集 indicator_ext 的输入状态（指纹 + 报告期 + 行数），供门控判断。

    只做聚合查询（COUNT/MAX + 400 天股本摘要），在正式库为亚秒级；不做任何写入。
    """
    tables: dict[str, dict[str, Any]] = {}
    for table in _INPUT_TABLES:
        try:
            row = duck.read_query(
                f"SELECT COUNT(*) AS rows, MAX(report_date) AS latest FROM {table}"
            )[0]
        except Exception as error:  # noqa: BLE001 - 表缺失/损坏按空表处理
            logger.debug("读取 %s 输入状态失败: %s", table, error)
            row = {"rows": 0, "latest": None}
        tables[table] = {
            "rows": int(row["rows"] or 0),
            "latest": str(row["latest"])[:10] if row.get("latest") else None,
        }
    share_capital = share_capital_state(duck)
    try:
        ext = duck.read_query(
            """SELECT COUNT(*) AS rows, MAX(report_date) AS latest,
                      MAX(calculated_at) AS calculated_at
               FROM indicator_ext"""
        )[0]
    except Exception as error:  # noqa: BLE001
        logger.debug("读取 indicator_ext 状态失败: %s", error)
        ext = {"rows": 0, "latest": None, "calculated_at": None}
    fingerprint_parts = [
        f"{table}:{state['rows']}:{state['latest']}"
        for table, state in sorted(tables.items())
    ]
    # 股本链摘要进指纹（P0-9）：它是**报表期粒度之外**的唯一输入，
    # 用窗口摘要而不是整表 COUNT/MAX，否则"改了一行股本"这种事件永远看不见。
    fingerprint_parts.append(
        f"{SHARE_CAPITAL_TABLE}:"
        f"{share_capital['rows']}:{share_capital['latest']}:{share_capital['digest']}"
    )
    return {
        "fingerprint": "|".join(fingerprint_parts),
        "tables": tables,
        "share_capital": share_capital,
        "complete_period": latest_complete_trio_period(duck),
        "ext_rows": int(ext["rows"] or 0),
        "ext_max_period": str(ext["latest"])[:10] if ext.get("latest") else None,
        "ext_calculated_at": (
            str(ext["calculated_at"]) if ext.get("calculated_at") else None
        ),
    }


def rebuild_decision(
    duck: DuckDBStore,
    *,
    stored_fingerprint: str | None = None,
) -> dict[str, Any]:
    """判断本轮是否需要重建 indicator_ext。

    Returns:
        {"required": bool, "reason": str, "force_codes": list[str],
         "scope": "all" | "codes", **input_state}

        reason ∈ {indicator_ext_empty, ext_period_behind_trio,
                  input_fingerprint_changed, up_to_date}

    ``force_codes``（P0-9 修法②）：当指纹差异**只**来自 ``share_capital_history``
    时，说明报表期没推进、只是有人改了股本链 → 只需按变更股重建。清单来自
    :func:`share_capital_drift_codes` 的自证式探针（重算市值与表内值不符），
    ``scope="codes"`` 时调用方应执行
    ``ExtendedIndicatorBuilder().build(force_codes=decision["force_codes"])``；
    ``scope="all"`` 时仍走全量 ``build()``（report_period 推进、空表等情形）。
    """
    state = input_state(duck)
    complete = state["complete_period"]
    ext_max = state["ext_max_period"]
    if not state["ext_rows"] or not ext_max:
        required, reason = True, "indicator_ext_empty"
    elif complete and ext_max < complete:
        # 硬门禁：派生域落后于最新完整三表期 → 必须重建（D28 的直接修法）。
        required, reason = True, "ext_period_behind_trio"
    elif stored_fingerprint != state["fingerprint"]:
        required, reason = True, "input_fingerprint_changed"
    else:
        required, reason = False, "up_to_date"

    # 指纹差异归因：只差股本链 → 最小重建。首次运行（无基线指纹）不做归因，
    # 否则会把"从没见过任何输入"误判成"股本变了"。
    force_codes: list[str] = []
    changed_inputs: list[str] = []
    if stored_fingerprint is not None:
        before = _fingerprint_parts(stored_fingerprint)
        after = _fingerprint_parts(state["fingerprint"])
        changed_inputs = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        if changed_inputs == [SHARE_CAPITAL_TABLE]:
            force_codes = share_capital_drift_codes(duck)
    share_capital_only = changed_inputs == [SHARE_CAPITAL_TABLE]
    scope = "codes" if (required and force_codes and share_capital_only) else "all"
    return {
        "required": required,
        "reason": reason,
        "force_codes": force_codes,
        "changed_inputs": changed_inputs,
        "scope": scope,
        **state,
    }
