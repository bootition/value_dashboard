"""补采域核心逻辑（2026-11-14，依据 S1 D29「手工链」自动接入）

本模块把三个一次性脚本的核心逻辑抽成**可调用函数**，让自动更新主流程
（`app/core/update.py::_run_incremental_update_flow`）与 CLI 共用同一份实现：

1. `cash_flow_supplement` —— 折旧摊销族（`cash_flow_indirect`）+ 资本支出
   （`cash_flow_activity`）；来源 `scripts/fetch_cashflow_supplement.py`
2. `balance_sheet_ext`   —— 其他应付款 / 一年内到期非流动负债 / 其他应收款合计；
   来源 `scripts/fetch_balance_sheet_ext.py`
3. `deducted_profit`     —— 扣非净利润（只 UPDATE `income_statement` 既有行）；
   来源 `scripts/fetch_deducted_profit.py`

为什么必须抽出来：不是"调度缺失"，是**续传判据错了**
----------------------------------------------------
旧脚本的断点续传判据是「该股在本表里有**任意** `source='eastmoney_f10'` 行就跳过」。
第一次跑完（当时源只到 2026-06-30）之后，全市场股票都被永久判为"已有本源"，
**即使手动再跑也不会补 2026-09-30** —— 即"跑了也白跑"。2026-11-14 实测
`fetch_cashflow_supplement.py` dry-run 仍报"待补 40 只 / 已有本源 5540"，
这 40 只是从未取到数的，5540 只里的 Q3 缺口永远不会被补。

现在统一改成「**本源最新报告期 < 目标最新报告期 → 进 pending**」，
并对"抓了仍然没有该期数据"的股票做**失败冷却**（默认 7 天），
避免它们在队首吃掉每轮上限、把其余股票饿死。

域纪律
------
- 只写 `source='eastmoney_f10'` 的行；`cash_flow_indirect`/`cash_flow_activity`/
  `balance_sheet_ext` 按 (stock_code, report_date) 先删同源再插（幂等）；
- `deducted_profit` **只 UPDATE 既有行的 NULL 值，绝不 INSERT / DELETE**，
  因此字段名若与源不一致时结果是"没更新"，不会写坏主链三表；
- 不写 `indicator_snapshot` / `readiness`；失败保留旧值并如实计数；
- 每轮都有 `max_stocks` 与 `deadline_seconds` 双上限，保证更新周期不被拖长。
"""

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，本块登记全部原文小节号）
# S1  reports/119 D29「手工链」 / P1-3 —— 三个一次性脚本逻辑抽成可调用函数（续传判据错，不是调度缺失）；
#     P1-3：2026Q3 主链 `balance_sheet.fixed_assets` 一度只剩 1.8%，故只回填 NULL。
# S2  reports/122 §A —— 金融股专用三表 2025Q2+ 与公允价值变动收益回填；
#     另含列名语义历史包袱（`exchange_gain` 等实测结果）。
# S3  reports/124 —— `f"{spec.endpoint}:{batch_id}"[:32]` 截断导致任务键碰撞的修复。
# S4  reports/126 —— 冷却过滤不能让"待补"凭空消失：必须区分「真的没得补」与「有得补但都在冷却」。
# S5  reports/131 §9.2 P0-3 / P0-6 —— P0-3：东财 F10 `xjllbAjaxNew` 现金流量表全量字段映射改为
#     按**整表**列集合落库（不再只列已映射的 10 列 / 1 列），同目标列多候选源字段；
#     P0-6：保险股判定（源响应自带 ORG_TYPE，`sw_level2='保险Ⅱ'` 优先）与保险合同负债改道，
#     并裁决「不改 schema」（专用列未建，需主控加列）。
# S6  reports/135 —— 事故修复后的落库纪律：字段映射是**单一真相**（解析层与落库层共用）；
#     源返回了该期就必须交给落库层，**全 NULL 的行也要**；`replace_source_rows` 先删同源再插；
#     公开别名供空值原因清扫器取两张补充表的数值列。
# S7  reports/139 §5.1 / §7 前置 1 —— 金融/行业判据不再手写，改由共享判据实现。


from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import requests

from app.core.financial_period import cn_today
from app.core.sector import is_financial_sql
from app.core.storage.duckdb_store import DuckDBStore, archive_raw_response_if_absent
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVITY_COLUMNS",
    "ACTIVITY_VALUE_COLUMNS",
    "BALANCE_SHEET_EXT",
    "CASHFLOW_SUPPLEMENT",
    "CASH_FLOW_STRUCTURAL_MISSING",
    "CASH_FLOW_UNMAPPED_BY_DECISION",
    "CASHFLOW_ORG_FIELD_MAPS",
    "CONTRACT_LIABILITIES_COLUMN",
    "INDIRECT_COLUMNS",
    "INDIRECT_VALUE_COLUMNS",
    "CSMAR_CUTOFF",
    "ContractLiabilityGuardResult",
    "DEDUCTED_PROFIT",
    "EXCHANGE_GAIN",
    "FINANCIAL_SECTOR",
    "INDIRECT_FIELD_MAP_FINANCIAL",
    "INSURANCE_CONTRACT_LIABILITY_COLUMN",
    "INSURANCE_CONTRACT_LIABILITY_REJECT_REASON",
    "INSURANCE_SW_LEVEL2",
    "REGULATORY_METRICS",
    "SPECS",
    "RegulatoryMetricsUpdater",
    "SupplementFetcher",
    "SupplementSpec",
    "cash_flow_structural_missing_columns",
    "em_symbol",
    "guard_contract_liabilities",
    "is_insurance_contract_liability_source",
    "is_insurance_stock",
    "recent_quarter_ends",
]

SOURCE = "eastmoney_f10"
CONFIDENCE = "strict"
BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
# CSMAR C17 的截止报告期：只补这之后，避免与 CSMAR 行重复覆盖。
CSMAR_CUTOFF = date(2025, 3, 31)
# 东财 F10 `*AjaxNew` 端点单次最多接受 5 个报告期（传更多仍只回 5 期）。
PERIODS_PER_REQUEST = 5
MAX_RETRY = 3
# 失败冷却：抓过但仍无该期数据的股票在 N 天内不再进队（防止饿死队尾）。
ATTEMPT_STATE_PREFIX = "supplement_attempted:"
ATTEMPT_STATE_KEEP_DAYS = 30

#: 落库行里的**非数值列**（元数据）。`_persist_rows` 判"这一行是不是全空"时
#: 只看其余列 —— `fetch_time`/`batch_id` 这些永远非空，算进来会让空行变成"有值"。
#: 2026-11-17（依据 S6）：这里必须是**单一真相**，解析层与落库层共用。
_ROW_META_COLUMNS: frozenset[str] = frozenset({
    "stock_code", "report_date", "report_type", "source", "fetch_time",
    "raw_response_hash", "confidence", "batch_id", "raw_data",
})

# ─── 数据集标识（update.py / CLI 共用）───────────────────────────────────
CASHFLOW_SUPPLEMENT = "cash_flow_supplement"
BALANCE_SHEET_EXT = "balance_sheet_ext"
DEDUCTED_PROFIT = "deducted_profit"
FIXED_ASSET_SUPPLEMENT = "fixed_assets_supplement"
REGULATORY_METRICS = "regulatory_metrics"
# 2026-11-15 新增（依据 S2）：金融股专用三表 2025Q2+ / 公允价值变动收益回填
FINANCIAL_SECTOR = "financial_sector_items"
EXCHANGE_GAIN = "exchange_gain_supplement"

# ─── 东财 F10 `xjllbAjaxNew` 现金流量表全量字段映射（2026-11-16，依据 S5 §9.2 P0-3）
#
# 事故：`CSMAR_CUTOFF = 2025-03-31` 之后的报告期全走东财补充管线，而该管线只按
# 10 个字段（间接法）+ 1 个字段（capex）写列 —— 实测 FY2025 年报：
#   `cash_flow_activity` 5,576 行里 20/21 个数值列全空（只有 capex 有值）；
#   `cash_flow_indirect` 5,579 行里 19/29 个数值列全空。
# 后果：EBITDA / EV-EBITDA / 经营杠杆 / FCF 质量 / Owner Earnings 在 2025 年
# 全部失去「折旧摊销 + 营运资本变动」加项。
#
# 列名来源（**不是猜的**）：2026-11-16 实测东财 `xjllbAjaxNew` 响应，用 CSMAR
# 覆盖期内的 2024-12-31 行做**逐值反查**（40 只股票 × 三套模板 ORG_TYPE
# 通用/证券/银行/保险），每个目标列只认「值完全相等」的源字段：
#   例：600519 2024-12-31 `cash_ending_balance` = 164,297,949,257.83
#       东财 `END_CASH` 同值（`END_CCE` 是「现金及现金等价物」合计，不等于本列）。
# 三套模板（companyType=4 通用 254 列 / =1 证券 252 列 / =3 银行 316 列 /
# =2 保险 256 列）字段名并不相同，同名不同义也有：保险模板的 `FA_IR_DEPR`
# 是「固定资产折旧+投资性房地产折旧」合计，而 CSMAR `fixed_asset_depreciation`
# 对应的是更细的 `FIXED_ASSET_DEPR`（601318 实测 4,555,000,000 vs 合计 9,860,000,000）。
# 因此**通用模板**用本表，金融专用模板用下面的 `INDIRECT_FIELD_MAP_FINANCIAL` 覆盖。
#
# 纪律：两张表都只在「源有值」时写入，且 `cash_flow_*` 走 replace_source_rows
# （按股票先删同源再插），绝不覆盖 CSMAR 行；解析时同一目标列**先到先得**，
# 行业覆盖表排在前，通用表只在该目标列仍为空时生效。
INDIRECT_FIELD_MAP: dict[str, str] = {
    # 折旧摊销族（既有，2026-11-14 实测确认）
    "FA_IR_DEPR": "fixed_asset_depreciation",
    "IR_DEPR": "investment_property_depreciation",
    "USERIGHT_ASSET_AMORTIZE": "right_of_use_asset_depreciation",
    "IA_AMORTIZE": "intangible_asset_amortization",
    "LPE_AMORTIZE": "long_term_prepaid_amortization",
    "ASSET_IMPAIRMENT": "asset_impairment_provision",
    "FAIRVALUE_CHANGE_LOSS": "fair_value_change_loss",
    "INVEST_LOSS": "investment_loss",
    "DISPOSAL_LONGASSET_LOSS": "disposal_long_term_asset_loss",
    "NETCASH_OPERATE": "cf_from_operating_indirect",
    # 2026-11-16 补齐（依据 S5 P0-3）：净利润 / 减值 / 财务费用 / 递延所得税 /
    # 营运资本三项 / 其他调整 / 债务与租赁 / 现金及现金等价物期初期末
    "NETPROFIT": "net_profit",
    "FA_SCRAP_LOSS": "fixed_asset_scrap_loss",
    "FINANCE_EXPENSE": "financial_expense",
    "DT_ASSET_REDUCE": "deferred_tax_asset_decrease",
    "DT_LIAB_ADD": "deferred_tax_liability_increase",
    "INVENTORY_REDUCE": "inventory_decrease",
    "OPERATE_RECE_REDUCE": "operating_receivable_decrease",
    "OPERATE_PAYABLE_ADD": "operating_payable_increase",
    "OTHER": "other_adjustment",
    "DEBT_TRANSFER_CAPITAL": "debt_to_capital",
    "CONVERT_BOND_1YEAR": "convertible_bond_due_within_1y",
    "FINLEASE_OBTAIN_FA": "finance_lease_fixed_assets",
    "END_CASH": "cash_ending_balance",
    "BEGIN_CASH": "cash_beginning_balance",
    "END_CASH_EQUIVALENTS": "cash_equivalent_ending",
    "BEGIN_CASH_EQUIVALENTS": "cash_equivalent_beginning",
    "CCE_ADD": "cash_equivalent_net_increase",
}
# 金融专用模板（ORG_TYPE 银行 / 保险）的覆盖项：**优先于** `INDIRECT_FIELD_MAP`。
# 逐值核对（2026-11-16，CSMAR 2024-12-31）：
# - 601318 中国平安 `fixed_asset_depreciation` = 4,555,000,000 = `FIXED_ASSET_DEPR`
#   （同响应的 `FA_IR_DEPR` = 9,860,000,000 = 固定资产折旧 + 投资性房地产折旧合计）；
# - 601318 `deferred_tax_asset_decrease` = −1,535,000,000 = `DEFER_TAX`
#   （金融模板不给 `DT_ASSET_REDUCE`/`DT_LIAB_ADD` 两项拆分，CSMAR 也整笔记在借方）；
# - 600036 招商银行 `asset_impairment_provision` = −2,789,000,000 =
#   `OTHER_ASSET_IMPAIRMENT`（同响应的 `ASSET_IMPAIRMENT` 是 40,819,000,000，不是本列）。
INDIRECT_FIELD_MAP_FINANCIAL: dict[str, str] = {
    "FIXED_ASSET_DEPR": "fixed_asset_depreciation",
    "OTHER_ASSET_IMPAIRMENT": "asset_impairment_provision",
    # 信用减值损失：仅银行/保险模板有独立行（通用/证券模板把它并进资产减值准备）
    "CREDIT_IMPAIRMENT_INCOME": "credit_impairment_loss",
    "DEFER_TAX": "deferred_tax_asset_decrease",
}
# 资本支出 + 投资/筹资活动现金流量净额与明细 → cash_flow_activity 列名。
# 2026-11-16 同法逐值反查（40 只股票，命中率均在 26–39/40）。
ACTIVITY_FIELD_MAP: dict[str, str] = {
    "CONSTRUCT_LONG_ASSET": "capex",
    "WITHDRAW_INVEST": "investment_recovered",
    "RECEIVE_INVEST_INCOME": "investment_income_cash",
    "DISPOSAL_LONG_ASSET": "disposal_long_asset_cash",
    "DISPOSAL_SUBSIDIARY_OTHER": "disposal_subsidiary_cash",
    "RECEIVE_OTHER_INVEST": "other_investing_inflow",
    "TOTAL_INVEST_INFLOW": "investing_inflow_total",
    "INVEST_PAY_CASH": "investment_paid",
    "OBTAIN_SUBSIDIARY_OTHER": "acquire_subsidiary_cash",
    "PAY_OTHER_INVEST": "other_investing_outflow",
    "TOTAL_INVEST_OUTFLOW": "investing_outflow_total",
    "ACCEPT_INVEST_CASH": "equity_investment_received",
    "RECEIVE_LOAN_CASH": "borrow_received",
    "ISSUE_BOND": "bond_issued",
    "RECEIVE_OTHER_FINANCE": "other_financing_inflow",
    "TOTAL_FINANCE_INFLOW": "financing_inflow_total",
    "PAY_DEBT_CASH": "debt_repaid",
    "ASSIGN_DIVIDEND_PORFIT": "dividend_interest_paid",
    "PAY_OTHER_FINANCE": "other_financing_outflow",
    "TOTAL_FINANCE_OUTFLOW": "financing_outflow_total",
}
#: 按源行 `ORG_TYPE` 选择行业覆盖表。未列出的行业（通用/证券/信托/金控…）
#: 一律回落到 `INDIRECT_FIELD_MAP` —— 证券模板（companyType=1，252 列）与通用
#: 模板字段名一致（2026-11-16 实测 600030/002736：`FA_IR_DEPR`、`DT_ASSET_REDUCE`
#: 都在，`FIXED_ASSET_DEPR`/`IR_DEPR` 都不在）。
CASHFLOW_ORG_FIELD_MAPS: dict[str, dict[str, str]] = {
    "银行": INDIRECT_FIELD_MAP_FINANCIAL,
    "保险": INDIRECT_FIELD_MAP_FINANCIAL,
}
# 保险模板把直接法现金流量表的三个「其他」科目拆/并成了不同名字，用**非空项求和**
# 表达「两套名字互斥、取到哪个算哪个」。逐值核对（2026-11-16，CSMAR 2024-12-31）：
# - 601628 中国人寿：TOTAL_INVEST_OUTFLOW 2,777,049,000,000 =
#   INVEST_PAY_CASH 2,750,929 + CONSTRUCT_LONG_ASSET 3,979 +
#   PURCHASE_SUBSIDIARY_OTHER 13,724 + PAY_BUY_RESALE 8,417（亿元，完全闭合），
#   而 CSMAR 把后两项分别记在 acquire_subsidiary_cash / other_investing_outflow；
# - 601628：TOTAL_FINANCE_OUTFLOW 129,002,000,000 = PAY_DEBT_CASH 35,138 +
#   ASSIGN_DIVIDEND_PORFIT 26,915 + PAY_SELL_REPO 65,875 + PAY_OTHER_FINANCE 1,074，
#   CSMAR 的 other_financing_outflow = 66,949,000,000 = 后两项之和。
CASHFLOW_ORG_DERIVED_SUMS: dict[str, dict[str, tuple[str, ...]]] = {
    "保险": {
        "acquire_subsidiary_cash": ("PURCHASE_SUBSIDIARY_OTHER", "OBTAIN_SUBSIDIARY_OTHER"),
        "other_investing_outflow": ("PAY_BUY_RESALE", "PAY_OTHER_INVEST"),
        "other_financing_outflow": ("PAY_SELL_REPO", "PAY_OTHER_FINANCE"),
    },
}

#: 东财三套模板都**没有**对应科目的列（结构性命中不了，不是"没抓到"）。
#: 登记责任：判据在 `app/core/quality/null_reason_sweeper.py`（`NullReason.SOURCE_EMPTY`），
#: 落库只经离线 CLI `scripts/classify_null_reasons.py`（默认 dry-run，`--yes` 才写）——
#: 不是更新链里的一步，所以本模块只给清单、等运维脚本刷新。
#: 本模块只负责给出「哪些列是结构性缺失」这一事实清单 + 原因，**不写**
#: `indicator_null_reason`（那张表的唯一写入者是清扫器，见 null_reasons.py 约束 2）。
CASH_FLOW_STRUCTURAL_MISSING: dict[str, dict[str, str]] = {
    "cash_flow_indirect": {
        "unconfirmed_investment_loss": (
            "东财 xjllbAjaxNew 四套模板（通用/证券/银行/保险）都没有"
            "「未确认的投资损失」科目；CSMAR 该列本身也只有 2010–2016 有值"
            "（2016 年后源即停披露），属公认的结构性空列。"
        ),
        "credit_impairment_loss": (
            "通用/证券模板的补充资料只有合并的「资产减值准备」"
            "（ASSET_IMPAIRMENT→asset_impairment_provision），没有单独的"
            "「信用减值损失」行；只有银行/保险模板的 CREDIT_IMPAIRMENT_INCOME 可映射。"
        ),
        "investment_property_depreciation": (
            "银行/保险模板没有 IR_DEPR 独立行（已并入 FA_IR_DEPR 合计），"
            "因此该列对金融股结构性缺失；通用/证券模板不受影响。"
        ),
    },
    "cash_flow_activity": {
        "other_cash_effect": (
            "东财**有**对应字段 RATE_CHANGE_EFFECT（汇率变动对现金的影响），但团队判定"
            "它与 CSMAR `other_cash_effect` 的口径不完全相等（600382 2021 年报两值相等"
            " −962,944,449.95；601169 2018 年报不等：CSMAR −749,000,000 vs 东财"
            " 486,000,000）⇒ **主动不映射**（semantic mismatch），不是「源没有该字段」。"
            "NullReason 枚举没有 SEMANTIC_MISMATCH / NOT_MAPPED，原因写 detail 供主控裁决。"
        ),
    },
}

#: `CASH_FLOW_STRUCTURAL_MISSING` 里**不是**「源没有这个字段」、而是「源有该字段但
#: 口径不符 / 团队主动不映射」的列（`表名.列名`）。清扫器据此改写 detail：
#: 枚举新增取值会破坏既有契约，因此原因码仍用 `SOURCE_EMPTY`，语义靠 detail 说清。
CASH_FLOW_UNMAPPED_BY_DECISION: frozenset[str] = frozenset({
    "cash_flow_activity.other_cash_effect",
})


#: 金融专用模板（银行/保险）**有**对应科目、因此不算结构性缺失的列。
#: `credit_impairment_loss` ← 银行/保险模板的 `CREDIT_IMPAIRMENT_INCOME`（已映射）。
_STRUCTURAL_MISSING_FINANCIAL_EXEMPT: frozenset[str] = frozenset({"credit_impairment_loss"})
#: 只在金融专用模板下才结构性缺失的列（通用/证券模板有独立行）。
_STRUCTURAL_MISSING_FINANCIAL_ONLY: frozenset[str] = frozenset(
    {"investment_property_depreciation"}
)


def cash_flow_structural_missing_columns(
    table: str, *, org_type: str | None = None,
) -> tuple[str, ...]:
    """该表「源侧结构性拿不到」的列清单（供 null_reason 清扫器登记 SOURCE_EMPTY）。

    Args:
        table: `cash_flow_indirect` / `cash_flow_activity`。
        org_type: 源行 `ORG_TYPE`（银行/保险/证券/通用）。三套模板的科目集不同，
            `org_type` 决定两族例外：
            - `credit_impairment_loss` 只有银行/保险模板拿得到（`CREDIT_IMPAIRMENT_INCOME`）；
            - `investment_property_depreciation` 只有通用/证券模板有 `IR_DEPR` 独立行。
    """
    reasons = CASH_FLOW_STRUCTURAL_MISSING.get(table, {})
    financial = org_type in {"银行", "保险"}
    columns = []
    for column in reasons:
        if column in _STRUCTURAL_MISSING_FINANCIAL_ONLY and not financial:
            continue
        if column in _STRUCTURAL_MISSING_FINANCIAL_EXEMPT and financial:
            continue
        columns.append(column)
    return tuple(columns)
# 资产负债表补充科目 → balance_sheet_ext 列名
BALANCE_EXT_FIELD_MAP: dict[str, str] = {
    "TOTAL_OTHER_PAYABLE": "other_payables",
    "NONCURRENT_LIAB_1YEAR": "non_current_liab_due_1y",
    "TOTAL_OTHER_REC": "total_other_receivable",
}
# 扣非净利润 → income_statement.deducted_net_profit（字段名由旧脚本实测确认：
# akshare `stock_profit_sheet_by_report_em` 返回的原始列名）
DEDUCTED_FIELD_MAP: dict[str, str] = {
    "DEDUCT_PARENT_NETPROFIT": "deducted_net_profit",
}
# 公允价值变动收益 → income_statement.exchange_gain。
# ⚠️ 列名与语义的历史包袱（2026-11-15 实测，依据 S2）：`exchange_gain` 这个
# 列**存的是公允价值变动收益**（schema.py 注释、CSMAR `B001301000`、akshare
# `FAIRVALUE_CHANGE_INCOME` 三方一致，且 600028/600030 逐值核对相等），而不是
# 汇兑收益。新浪适配器把「汇兑收益」映射到本列属于口径错误，且新浪只在 ~194 只
# 股票上返回该科目 → 造成 52.1%→3.5% 的"断崖"。东财 F10 `lrbAjaxNew` 同时有
# `FAIRVALUE_CHANGE_INCOME`（本列语义）与 `EXCHANGE_INCOME`（汇兑收益），
# 这里按**列既有语义**取前者，只回填 NULL。
EXCHANGE_GAIN_FIELD_MAP: dict[str, str] = {
    "FAIRVALUE_CHANGE_INCOME": "exchange_gain",
}
EXCHANGE_GAIN_COLUMNS: tuple[str, ...] = ("stock_code", "report_date", "exchange_gain")

# 两张现金流量明细表的**全部数值列**（顺序与 schema 物理列序一致）。
# 2026-11-16（依据 S5 P0-3）：原实现只列了已映射的 10 列 / 1 列，
# 于是未映射的列永远写 NULL 且没有任何显式声明。现在按整表列集合落库，
# 源侧拿不到的列由 `CASH_FLOW_STRUCTURAL_MISSING` 显式登记。
_INDIRECT_VALUE_COLUMNS: tuple[str, ...] = (
    "net_profit",
    "credit_impairment_loss",
    "unconfirmed_investment_loss",
    "asset_impairment_provision",
    "fixed_asset_depreciation",
    "investment_property_depreciation",
    "right_of_use_asset_depreciation",
    "intangible_asset_amortization",
    "long_term_prepaid_amortization",
    "disposal_long_term_asset_loss",
    "fixed_asset_scrap_loss",
    "fair_value_change_loss",
    "financial_expense",
    "investment_loss",
    "deferred_tax_asset_decrease",
    "deferred_tax_liability_increase",
    "inventory_decrease",
    "operating_receivable_decrease",
    "operating_payable_increase",
    "other_adjustment",
    "cf_from_operating_indirect",
    "debt_to_capital",
    "convertible_bond_due_within_1y",
    "finance_lease_fixed_assets",
    "cash_ending_balance",
    "cash_beginning_balance",
    "cash_equivalent_ending",
    "cash_equivalent_beginning",
    "cash_equivalent_net_increase",
)
_ACTIVITY_VALUE_COLUMNS: tuple[str, ...] = (
    "capex",
    "investment_recovered",
    "investment_income_cash",
    "disposal_long_asset_cash",
    "disposal_subsidiary_cash",
    "other_investing_inflow",
    "investing_inflow_total",
    "investment_paid",
    "acquire_subsidiary_cash",
    "other_investing_outflow",
    "investing_outflow_total",
    "equity_investment_received",
    "borrow_received",
    "bond_issued",
    "other_financing_inflow",
    "financing_inflow_total",
    "debt_repaid",
    "dividend_interest_paid",
    "other_financing_outflow",
    "financing_outflow_total",
    "other_cash_effect",
)

INDIRECT_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type",
    *_INDIRECT_VALUE_COLUMNS,
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id", "raw_data",
)
ACTIVITY_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type",
    *_ACTIVITY_VALUE_COLUMNS,
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
)
#: 公开别名（2026-11-17，依据 S6）：空值原因清扫器要把这两张表的**数值列**
#: 登记成 `cash_flow_indirect.<列>` / `cash_flow_activity.<列>` 字段。
#: 单一真相仍是上面的私有元组，清扫器只读这里，绝不另抄一份列名。
INDIRECT_VALUE_COLUMNS = _INDIRECT_VALUE_COLUMNS
ACTIVITY_VALUE_COLUMNS = _ACTIVITY_VALUE_COLUMNS
BALANCE_EXT_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type",
    *BALANCE_EXT_FIELD_MAP.values(),
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
)
DEDUCTED_COLUMNS: tuple[str, ...] = ("stock_code", "report_date", "deducted_net_profit")
# 固定资产净额 → balance_sheet.fixed_assets。
# 2026-11-14 审查（依据 S1 P1-3）：2026Q3 主链 `balance_sheet.fixed_assets`
# 只剩 102/5,575（1.8%），而上期 99.9% —— 新浪 Q3 返回的资产负债表没有这个键
# （抽 000408 raw_data：Q2 有、Q3 无）。东财 F10 `zcfzbAjaxNew` 有该字段
# （同一接口已在补 other_payables），复用它**只回填 NULL**。
FIXED_ASSET_FIELD_MAP: dict[str, str] = {
    "FIXED_ASSET": "fixed_assets",
}
FIXED_ASSET_COLUMNS: tuple[str, ...] = ("stock_code", "report_date", "fixed_assets")

# ─── 金融股专用三表（2026-11-15，依据 S2 §A）─────────────────────────
#
# 背景：`financial_sector_items` 5,058 行 / 25 个业务列（含主键与元数据共 33 列，
# 以 pragma 实测为准）只覆盖到 2025-03-31（CSMAR C17 截止），2025Q2 之后
# 银行/券商/保险的专用科目**一个来源都没有**。
#
# 实测（2026-11-15，东财 F10 `zcfzbAjaxNew` / `lrbAjaxNew`）：
# - 金融股必须先解析 `companyType`（银行=3 / 保险=2 / 证券=1 / 通用=4），
#   `companyType=4` 对金融股的**资产负债表直接返回空**、利润表返回通用模板
#   （行名不同：无 `INTEREST_NI`/`FEE_COMMISSION_NI`），因此本 spec 打开
#   `resolve_company_type`，先取页面 `hidctype` 再发请求。
# - 响应行自带 `ORG_TYPE`（"银行"/"证券"/"保险"），据此选择按行业字段映射，
#   避免同名不同义（券商 `CUSTOMER_DEPOSIT` 是「客户资金存款」资产科目，
#   绝不能写进「吸收存款」）。
# - 2025-03-31 逐值比对：工行/招行 14 个银行科目与 CSMAR **完全相等**
#  （ratio=1.000000），中信证券 9 个券商科目同样完全相等。
FINANCIAL_SECTOR_FIELD_MAP_BANK: dict[str, str] = {
    "CASH_DEPOSIT_PBC": "cash_and_cb_balance",       # 现金及存放中央银行款项
    "DEPOSIT_INTERBANK": "due_from_banks",           # 存放同业款项
    "LOAN_ADVANCE": "loans_and_advances",            # 发放贷款及垫款净额
    "LOAN_PBC": "borrowing_from_cb",                 # 向中央银行借款
    "IOFI_DEPOSIT": "interbank_deposits",            # 同业及其他金融机构存放款项
    "ACCEPT_DEPOSIT": "customer_deposits",           # 吸收存款
    "INTEREST_INCOME": "interest_income",            # 利息收入
    "INTEREST_EXPENSE": "interest_expense",          # 利息支出
    "INTEREST_NI": "net_interest_income",            # 利息净收入
    "FEE_COMMISSION_NI": "fee_commission_income_net",  # 手续费及佣金净收入
    "LEND_FUND": "interbank_lending",                # 拆出资金
    "BUY_RESALE_FINASSET": "reverse_repo_assets",    # 买入返售金融资产
    "BORROW_FUND": "interbank_borrowing",            # 拆入资金
    "SELL_REPO_FINASSET": "repo_liabilities",        # 卖出回购金融资产款
}
FINANCIAL_SECTOR_FIELD_MAP_BROKER: dict[str, str] = {
    "SETTLE_EXCESS_RESERVE": "settlement_reserve",           # 结算备付金
    "CUSTOMER_EXCESS_RESERVE": "customer_settlement_reserve",  # 其中：客户备付金
    "REFUND_DEPOSIT_PAY": "margin_deposits_paid",            # 存出保证金
    "AGENT_TRADE_SECURITY": "client_securities_deposits",    # 代理买卖证券款
    "AGENT_UNDERWRITE_SECURITY": "underwriting_securities",  # 代理承销证券款
    "INTEREST_NI": "net_interest_income",                    # 利息净收入
    "FEE_COMMISSION_NI": "fee_commission_income_net",        # 手续费及佣金净收入
    "LEND_FUND": "interbank_lending",                        # 拆出资金
    "BUY_RESALE_FINASSET": "reverse_repo_assets",            # 买入返售金融资产
    "BORROW_FUND": "interbank_borrowing",                    # 拆入资金
    "SELL_REPO_FINASSET": "repo_liabilities",                # 卖出回购金融资产款
}
FINANCIAL_SECTOR_FIELD_MAP_INSURER: dict[str, str] = {
    "EARNED_PREMIUM": "earned_premiums",  # 已赚保费
}
# 按 ORG_TYPE 选择的字段映射；未命中的行业（信托/金控/期货/租赁 → "通用"）
# 用空映射：抓得到行但没有本表科目，如实计 no_data，绝不猜。
FINANCIAL_SECTOR_ORG_FIELD_MAPS: dict[str, dict[str, str]] = {
    "银行": FINANCIAL_SECTOR_FIELD_MAP_BANK,
    "证券": FINANCIAL_SECTOR_FIELD_MAP_BROKER,
    "保险": FINANCIAL_SECTOR_FIELD_MAP_INSURER,
}
# 派生科目：东财银行模板没有「吸收存款及同业存放」小计，但
# 2025-03-31 实测 CSMAR 该列 == 吸收存款 + 同业存放（工行/招行差值均为 0），
# 因此按两项之和派生，而不是留空。
FINANCIAL_SECTOR_ORG_DERIVED_SUMS: dict[str, dict[str, tuple[str, ...]]] = {
    "银行": {"deposits_and_interbank": ("ACCEPT_DEPOSIT", "IOFI_DEPOSIT")},
}
FINANCIAL_SECTOR_VALUE_COLUMNS: tuple[str, ...] = (
    "cash_and_cb_balance", "due_from_banks", "loans_and_advances", "borrowing_from_cb",
    "deposits_and_interbank", "interbank_deposits", "customer_deposits",
    "interest_income", "interest_expense", "net_interest_income",
    "premiums_receivable", "insurance_contract_reserve", "policyholder_deposits",
    "earned_premiums", "claim_payments_net",
    "settlement_reserve", "customer_settlement_reserve", "margin_deposits_paid",
    "client_securities_deposits", "underwriting_securities", "fee_commission_income_net",
    "interbank_lending", "reverse_repo_assets", "interbank_borrowing", "repo_liabilities",
)
FINANCIAL_SECTOR_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type",
    *FINANCIAL_SECTOR_VALUE_COLUMNS,
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
)
# 只补金融行业股票：申万一级「银行」「非银金融」。CSMAR 的 78 只金融股
# （38 银行 + 36 券商 + 1 保险 + 1 信托 + 2 金控）全部落在这个口径内
#（其中 3 只已退市：中天金融/宏源证券/海通证券，被 `is_listed` 过滤掉）。
#
# 2026-11-18（依据 S7 §5.1 / §7 前置 1）：判据**不再手写**，改由
# `app.core.sector.is_financial_sql` 生成 —— 仓库里曾有三份互不一致的手写金融股
# 判据（本处、`roce.py`、`null_reason_sweeper.py`），同一只股票在不同指标里
# 「是不是金融」结论不同。这里保留常量名（外部按名 import），语义不变：
# 申万一级命中即金融；申万一级缺失时用证监会门类「金融业」兜底；NULL 安全。
FINANCIAL_SECTOR_UNIVERSE_PREDICATE = is_financial_sql("m")

# ─── P0-6：保险股的「保险合同负债」不得写进通用科目 contract_liabilities ────
#
# 事故（依据 S5 §9.2 P0-6）：601318 中国平安 / 601319 中国人保 /
# 601336 新华保险 / 601601 中国太保 / 601628 中国人寿 五只保险股，
# FY2022–FY2025 共 19 行 `balance_sheet.contract_liabilities` 有值，
# 却占**全市场合同负债合计的 42%–68%**（2026-11-16 复核已达 50%–78%）。
# 根因：上游金融专用科目「保险合同负债」被按名称/序号匹配到通用科目
# `contract_liabilities`。出现年份恰是新保险合同准则 CAS25 的实施年
#（A+H 险企 2022、其余 2023）—— 只有实施新准则的公司才有这一行，可反证。
#
# 本模块提供**写库前守卫**：只要股票被判定为保险股（`sw_level2='保险Ⅱ'`
# 或源行 ORG_TYPE 为「保险」），「保险合同负债」这个科目就
#  ① 绝不进 `contract_liabilities`；
#  ② 若 `financial_sector_items.insurance_contract_liability` 列存在，改写进去；
#  ③ 列不存在（2026-11-16 现状）→ 值不落库，并显式登记原因（不留无声 NULL）。
INSURANCE_SW_LEVEL1 = "非银金融"
#: 申万二级行业名（DB 实测字符串，注意是全角罗马数字 Ⅱ）
INSURANCE_SW_LEVEL2 = "保险Ⅱ"
#: 上游用来指代「保险合同负债」的字段名/中文标签（大小写不敏感匹配）
INSURANCE_CONTRACT_LIABILITY_SOURCES: tuple[str, ...] = (
    "保险合同负债",
    "保险合同负债合计",
    "INSURANCE_CONTRACT_LIAB",
    "INSURANCE_CONTRACT_LIABILITY",
    "INSURANCE_CONTRACT_RESERVE_LIAB",
)
#: 被禁止承接该科目的通用列
CONTRACT_LIABILITIES_COLUMN = "contract_liabilities"
#: 该科目真正该落的专用列（`financial_sector_items`）。
#: ⚠️ 2026-11-16 实测正式库**没有**这一列 —— 按 依据 S5 裁决「不改 schema」，
#: 这里只登记常量；主控加列后 `_adapt_spec_to_schema()` 会自动启用映射。
INSURANCE_CONTRACT_LIABILITY_COLUMN = "insurance_contract_liability"
INSURANCE_CONTRACT_LIABILITY_TABLE = "financial_sector_items"
#: 守卫拒绝写入时登记到 `indicator_null_reason.reason_code` 的原因码。
#: 复用既有枚举里的 `SOURCE_EMPTY`（语义：源侧这个值没有可写的合法归宿）。
INSURANCE_CONTRACT_LIABILITY_REJECT_REASON = "SOURCE_EMPTY"
#: CAS25「保险合同负债」→ 专用列。列不存在时**不得**并入
#: `FINANCIAL_SECTOR_FIELD_MAP_INSURER`，否则 INSERT BY NAME 会直接报列不存在。
FINANCIAL_SECTOR_FIELD_MAP_INSURER_CAS25: dict[str, str] = {
    "保险合同负债": INSURANCE_CONTRACT_LIABILITY_COLUMN,
    "INSURANCE_CONTRACT_LIAB": INSURANCE_CONTRACT_LIABILITY_COLUMN,
}


def is_insurance_contract_liability_source(field_name: Any) -> bool:
    """该上游字段名/中文标签是否指代「保险合同负债」。"""
    text = str(field_name or "").strip().upper().replace(" ", "")
    if not text:
        return False
    return any(token.upper().replace(" ", "") in text for token in INSURANCE_CONTRACT_LIABILITY_SOURCES)


def is_insurance_stock(
    *,
    sw_level1: str | None = None,
    sw_level2: str | None = None,
    org_type: str | None = None,
    name: str | None = None,
) -> bool:
    """保险股判定（任一命中即算，宁可多拦不可漏拦）。

    判据优先级：申万二级 `保险Ⅱ` → 源行 `ORG_TYPE='保险'` → 公司名含「保险」。
    `sw_level1='非银金融'` 单独**不**足以判定（券商/多元金融也是非银金融）。
    """
    if (sw_level2 or "").strip() == INSURANCE_SW_LEVEL2:
        return True
    if "保险" in str(org_type or ""):
        return True
    if sw_level1 is not None and str(sw_level1).strip() == "保险":
        return True
    return "保险" in str(name or "")


@dataclass(frozen=True)
class ContractLiabilityGuardResult:
    """写库前守卫的判定结果。"""

    #: 放行后可写的列 → 值（被拒的列已从中摘除）
    values: dict[str, float | None]
    #: 被拒绝写入的列（目前只会是 `contract_liabilities`）
    rejected_columns: tuple[str, ...] = ()
    #: 应当改写到专用列的值（专用列不存在时为空）
    rerouted: Mapping[str, float] = field(default_factory=dict)
    #: 列 → 登记 `indicator_null_reason` 用的原因文案
    reasons: Mapping[str, str] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return bool(self.rejected_columns)


def guard_contract_liabilities(
    values: Mapping[str, float | None],
    *,
    source_fields: Mapping[str, str] | None = None,
    is_insurance: bool,
    available_columns: Collection[str] | None = None,
) -> ContractLiabilityGuardResult:
    """写库前守卫：保险股的「保险合同负债」绝不落 `contract_liabilities`。

    Args:
        values: 目标列 → 待写值。
        source_fields: 目标列 → 该值实际来自的上游字段名（用于识别科目来源）。
        is_insurance: 该股是否为保险股（调用方按 sw_level2/ORG_TYPE 判定）。
        available_columns: 目标表真实列集合；含 `insurance_contract_liability`
            时把值改写到该专用列，否则只拒绝并登记原因。

    Returns:
        `ContractLiabilityGuardResult`；非保险股原样放行。
    """
    guarded = dict(values)
    if not is_insurance:
        return ContractLiabilityGuardResult(values=guarded)
    if CONTRACT_LIABILITIES_COLUMN not in guarded:
        return ContractLiabilityGuardResult(values=guarded)
    hit_source = str((source_fields or {}).get(CONTRACT_LIABILITIES_COLUMN) or "").strip()
    if not is_insurance_contract_liability_source(hit_source):
        # 值不是从「保险合同负债」来的（保险股也可能真有合同负债）：放行。
        return ContractLiabilityGuardResult(values=guarded)
    rejected_value = guarded.pop(CONTRACT_LIABILITIES_COLUMN)
    rerouted: dict[str, float] = {}
    target = INSURANCE_CONTRACT_LIABILITY_COLUMN
    if available_columns is not None and target in available_columns and rejected_value is not None:
        rerouted[target] = rejected_value
        guarded[target] = rejected_value
        detail = (
            f"「{hit_source}」是 CAS25 保险合同负债，已按保险股口径改写"
            f" {INSURANCE_CONTRACT_LIABILITY_TABLE}.{target}（不是合同负债）"
        )
    else:
        detail = (
            f"「{hit_source}」是 CAS25 保险合同负债，禁止写入 "
            f"{CONTRACT_LIABILITIES_COLUMN}（保险股口径）；"
            f"专用列 {INSURANCE_CONTRACT_LIABILITY_TABLE}.{target} 尚未建列，"
            "需主控加列后由补充管线改写（reports/131 §9.2 P0-6）"
        )
    return ContractLiabilityGuardResult(
        values=guarded,
        rejected_columns=(CONTRACT_LIABILITIES_COLUMN,),
        rerouted=rerouted,
        reasons={CONTRACT_LIABILITIES_COLUMN: detail},
    )


@dataclass(frozen=True)
class SupplementSpec:
    """一个补采数据集的抓取/解析/落库描述（声明式，避免三份近似代码）。"""

    key: str
    data_type: str
    endpoint: str
    api_version: str
    field_map: Mapping[str, str]
    columns: Mapping[str, tuple[str, ...]]
    #: replace_source_rows = 按股票删同源再插；update_existing = 只填 NULL 列
    write_mode: str
    #: latest_period = 本源最新报告期落后目标期即待补；missing_field = 列仍有 NULL
    pending_mode: str
    #: 判据表（latest_period 模式用它判断"该股本源最新报告期"）
    judge_table: str
    #: 待补报告期的下界（只补 CSMAR 未覆盖期）
    period_floor: date | None = None
    fills_null_only: bool = False
    #: 按行业（源行 `ORG_TYPE`：银行/证券/保险…）覆盖 `field_map`。
    #: 同名不同义（券商 `CUSTOMER_DEPOSIT` 是客户资金存款）必须在这里分开映射。
    org_field_maps: Mapping[str, Mapping[str, str]] | None = None
    #: 行业映射是「覆盖层」还是「整体替换」：
    #: - False（默认，金融股专用三表）：命中行业的表只写该行业科目，未命中即 no_data；
    #: - True（现金流量表补充域）：行业表只列**与通用模板不同名/不同义**的字段，
    #:   其余仍走 `field_map`。命中项排在前面，按「先到先得」优先取胜。
    org_field_maps_merge: bool = False
    #: 按行业派生科目：{行业: {目标列: (源字段1, 源字段2, …)}}，取非空项求和。
    org_derived_sums: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None
    #: 待补股票池 SQL 谓词（用 `m.` 前缀引用 stock_meta）；None=全市场。
    universe_predicate: str | None = None
    #: 抓取前先解析页面 `hidctype` 得到 companyType（金融股必须）。
    resolve_company_type: bool = False
    #: `resolve_periods` 默认回溯的季末数（>PERIODS_PER_REQUEST 会分块请求）。
    backfill_periods: int = PERIODS_PER_REQUEST
    #: replace 模式的删除范围：all=该股全部同源行；batch_periods=仅本批报告期。
    #: 用 batch_periods 时旧报告期行不会因后续抓取窗口前移而被删掉。
    delete_scope: str = "all"
    #: 需要合并抓取的端点（金融股三表 = 资产负债表 + 利润表）；None=只用 endpoint。
    endpoints: tuple[str, ...] | None = None


SPECS: dict[str, SupplementSpec] = {
    CASHFLOW_SUPPLEMENT: SupplementSpec(
        key=CASHFLOW_SUPPLEMENT,
        data_type="cash_flow_supplement",
        endpoint="xjllbAjaxNew",
        api_version="eastmoney-f10-xjllb/1",
        field_map={**INDIRECT_FIELD_MAP, **ACTIVITY_FIELD_MAP},
        columns={
            "cash_flow_indirect": INDIRECT_COLUMNS,
            "cash_flow_activity": ACTIVITY_COLUMNS,
        },
        write_mode="replace_source_rows",
        pending_mode="latest_period",
        judge_table="cash_flow_activity",
        # 2026-11-16（依据 S5 P0-3）：金融专用模板（ORG_TYPE 银行/保险）
        # 的折旧/减值/递延所得税字段名与通用模板不同，按行业覆盖解析。
        org_field_maps=CASHFLOW_ORG_FIELD_MAPS,
        org_field_maps_merge=True,
        org_derived_sums=CASHFLOW_ORG_DERIVED_SUMS,
    ),
    BALANCE_SHEET_EXT: SupplementSpec(
        key=BALANCE_SHEET_EXT,
        data_type="balance_sheet_ext",
        endpoint="zcfzbAjaxNew",
        api_version="eastmoney-f10-zcfzb/1",
        field_map=dict(BALANCE_EXT_FIELD_MAP),
        columns={"balance_sheet_ext": BALANCE_EXT_COLUMNS},
        write_mode="replace_source_rows",
        pending_mode="latest_period",
        judge_table="balance_sheet_ext",
    ),
    FIXED_ASSET_SUPPLEMENT: SupplementSpec(
        key=FIXED_ASSET_SUPPLEMENT,
        data_type="fixed_assets_supplement",
        endpoint="zcfzbAjaxNew",
        api_version="eastmoney-f10-zcfzb-fixed/1",
        field_map=dict(FIXED_ASSET_FIELD_MAP),
        columns={"balance_sheet": FIXED_ASSET_COLUMNS},
        write_mode="update_existing",
        pending_mode="missing_field",
        judge_table="balance_sheet",
        period_floor=CSMAR_CUTOFF,
        fills_null_only=True,
    ),
    DEDUCTED_PROFIT: SupplementSpec(
        key=DEDUCTED_PROFIT,
        data_type="deducted_profit",
        endpoint="lrbAjaxNew",
        api_version="eastmoney-f10-lrb/1",
        field_map=dict(DEDUCTED_FIELD_MAP),
        columns={"income_statement": DEDUCTED_COLUMNS},
        write_mode="update_existing",
        pending_mode="missing_field",
        judge_table="income_statement",
        period_floor=CSMAR_CUTOFF,
        fills_null_only=True,
    ),
    # 金融股专用三表：2025Q2 起（CSMAR 止于 2025-03-31）。
    # 抓两个端点（资产负债表 + 利润表）→ 写同一张 `financial_sector_items`。
    FINANCIAL_SECTOR: SupplementSpec(
        key=FINANCIAL_SECTOR,
        data_type="financial_sector_items",
        endpoint="zcfzbAjaxNew",  # 说明性；实际两个端点都抓，见 _fetch_rows
        api_version="eastmoney-f10-financial-sector/1",
        field_map={},  # 按 ORG_TYPE 选择，见 org_field_maps
        columns={"financial_sector_items": FINANCIAL_SECTOR_COLUMNS},
        write_mode="replace_source_rows",
        pending_mode="latest_period",
        judge_table="financial_sector_items",
        period_floor=CSMAR_CUTOFF,
        org_field_maps={
            org: dict(mapping)
            for org, mapping in FINANCIAL_SECTOR_ORG_FIELD_MAPS.items()
        },
        org_derived_sums={
            org: dict(mapping)
            for org, mapping in FINANCIAL_SECTOR_ORG_DERIVED_SUMS.items()
        },
        universe_predicate=FINANCIAL_SECTOR_UNIVERSE_PREDICATE,
        resolve_company_type=True,
        backfill_periods=8,
        delete_scope="batch_periods",
        endpoints=("zcfzbAjaxNew", "lrbAjaxNew"),
    ),
    # 公允价值变动收益（列 `exchange_gain` 的真实语义）只回填 NULL。
    EXCHANGE_GAIN: SupplementSpec(
        key=EXCHANGE_GAIN,
        data_type="exchange_gain_supplement",
        endpoint="lrbAjaxNew",
        api_version="eastmoney-f10-lrb-exchange-gain/1",
        field_map=dict(EXCHANGE_GAIN_FIELD_MAP),
        columns={"income_statement": EXCHANGE_GAIN_COLUMNS},
        write_mode="update_existing",
        pending_mode="missing_field",
        judge_table="income_statement",
        period_floor=CSMAR_CUTOFF,
        fills_null_only=True,
        backfill_periods=8,
    ),
}


def em_symbol(code: str, exchange: str | None = None) -> str:
    """证券代码 → 东方财富 symbol。

    优先用 `stock_meta.exchange`；**不要用代码前缀猜** —— 北交所新代码
    920xxx 以 9 开头，按"9→沪市"的旧规则会被错判为上交所（2026-09-19
    实测 336 只北交所股票因此取数失败）。
    """
    code = str(code).strip().zfill(6)
    prefix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get((exchange or "").upper())
    if prefix is None:  # exchange 缺失时的兜底：北交所优先
        if code.startswith(("920", "43", "83", "87")):
            prefix = "BJ"
        elif code.startswith(("60", "68", "90", "9")):
            prefix = "SH"
        else:
            prefix = "SZ"
    return f"{prefix}{code}"


def recent_quarter_ends(today: date | None = None, n: int = PERIODS_PER_REQUEST) -> list[str]:
    """最近 n 个已过去的季末报告期（含当季，若已过季末）。"""
    today = today or cn_today()
    ends: list[date] = []
    for year in (today.year, today.year - 1):
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            ends.append(date(year, month, day))
    ordered = sorted({end for end in ends if end <= today}, reverse=True)
    return [end.isoformat() for end in ordered[:n]]


def report_type_of(report_date: date) -> str:
    """报告期 → report_type 文案（与主链三表口径一致）。"""
    return {12: "annual", 6: "semi_annual"}.get(report_date.month, "quarterly")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


_LOCAL = threading.local()


def _session() -> requests.Session:
    """每线程一个 Session：requests.Session 不是线程安全的。"""
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _LOCAL.session = session
    return session


class SupplementFetcher:
    """补采域执行器：有界、幂等、失败冷却。

    调用方（自动更新 / CLI）只需 `refresh(key, ...)`；网络层在 `_fetch_rows`，
    解析在 `_parse_rows`，落库在 `_persist_rows`，三段都可单独测试/替换。
    """

    def __init__(
        self,
        duck: DuckDBStore,
        sqlite: SQLiteStore | None = None,
    ) -> None:
        self.duck = duck
        self.sqlite = sqlite
        # 2026-11-16（reports/146）：非标准报告期被写库守卫丢弃的计数与样例。
        # 与 `last_skipped_empty_rows` 同类，供上层日志 / 资产体检使用。
        self._dropped_nonstandard_periods = 0
        self._dropped_nonstandard_sample: list[dict[str, Any]] = []

    # ─── 目标报告期 ────────────────────────────────────────────────

    @staticmethod
    def resolve_periods(
        key: str,
        *,
        today: date | None = None,
        periods: Sequence[str] | None = None,
    ) -> list[str]:
        """目标报告期：显式传入优先，否则取最近 `backfill_periods` 个季末。

        `period_floor` 之后的所有期都要，才能把 2025-06-30 这类"CSMAR 截止后
        第一季"补上；`_fetch_rows` 会按接口上限（5 期/请求）自动分块。
        """
        spec = SPECS[key]
        raw = (
            list(periods) if periods
            else recent_quarter_ends(today=today, n=spec.backfill_periods)
        )
        if spec.period_floor is not None:
            raw = [
                item for item in raw
                if date.fromisoformat(str(item)[:10]) > spec.period_floor
            ]
        return raw

    # ─── 待补股票（修掉「有任意本源行就跳过」的续传判据）─────────────

    def pending_codes(
        self,
        key: str,
        periods: Sequence[str],
        *,
        max_stocks: int = 0,
        retry_days: int = 0,
    ) -> list[tuple[str, str]]:
        """返回本轮应补的股票 `[(code, exchange), ...]`。

        判据（两种模式）：
        - `latest_period`：本源最新报告期 < 目标最新报告期；
          旧实现是"本表有任意本源行就跳过"，这会永久跳过所有股票
          （2026-09-19 那次只到 06-30，之后再也补不到 09-30）。
        - `missing_field`：目标期内仍有该列为 NULL 的既有行（扣非）。
        """
        spec = SPECS[key]
        if not periods:
            return []
        latest = str(periods[0])[:10]
        rows = self.duck.read_query(
            self._pending_sql(spec),
            self._pending_params(spec, periods),
        )
        pending: list[tuple[str, str]] = []
        for row in rows:
            if spec.pending_mode == "latest_period":
                # 判据核心：本源**最新报告期**是否已经追平目标最新期。
                # 只要落后（或从来没有本源行）就必须再抓 —— 旧脚本在这里
                # 用的是"有没有任意本源行"，于是永远补不到新报告期。
                have = row.get("max_report_date")
                if have is not None and str(have)[:10] >= latest:
                    continue
            pending.append((str(row["stock_code"]), str(row.get("exchange") or "")))
        # 2026-11-16（依据 S4）：**冷却过滤不能让"待补"凭空消失**。
        # 旧行为：把冷却中的股票直接滤掉 → `pending` 变空 → 上层 update.py 把
        # `no_pending_codes` 当成"已追平" → 水位前进，但待补数一动不动
        # （实测 `balance_sheet_ext` 105 只连续多轮不变，`CHAIN_PROGRESS_STALLED`
        # 因此永久红）。这里把"被冷却挡住的数量"单独记下来，供上层区分
        # 「真的没得补」与「有得补但都在冷却」。
        before_cooldown = len(pending)
        pending = self._filter_cooldown(key, pending, retry_days)
        self._last_cooldown_blocked = before_cooldown - len(pending)
        if max_stocks > 0:
            pending = pending[:max_stocks]
        return pending

    @staticmethod
    def _pending_sql(spec: SupplementSpec) -> str:
        # 股票池过滤：金融股专用三表只对申万「银行/非银金融」跑，不能扫全市场
        #（否则 5,500 只制造业股票会被当成"从来没用本源补过"的待补股）。
        listed_clause = "m.is_listed IS TRUE"
        if spec.universe_predicate:
            listed_clause = f"({listed_clause}) AND ({spec.universe_predicate})"
        if spec.pending_mode == "missing_field":
            # 只关心近期窗口内仍为空的行；更早的报告期源侧不再返回，抓了也补不上。
            # 2026-11-14：原实现硬编码 income_statement.deducted_net_profit，
            # 新增数据集（balance_sheet.fixed_assets）会扫错表 —— 改为按 spec 生成：
            # 判据表取 `judge_table`，待填列取该表 columns 里除主键外的所有列。
            table = spec.judge_table
            fill_columns = [
                column for column in spec.columns[table]
                if column not in ("stock_code", "report_date")
            ]
            null_clause = " OR ".join(f"i.{column} IS NULL" for column in fill_columns)
            return f"""
                SELECT m.stock_code AS stock_code,
                       COALESCE(m.exchange, '') AS exchange,
                       MIN(i.report_date) AS first_gap
                FROM stock_meta m
                JOIN {table} i ON i.stock_code = m.stock_code
                WHERE {listed_clause}
                  AND i.report_date >= CAST(? AS DATE)
                  AND i.report_date <= CAST(? AS DATE)
                  AND ({null_clause})
                GROUP BY m.stock_code, m.exchange
                ORDER BY first_gap DESC, m.stock_code
            """
        # latest_period：LEFT JOIN 判据表，MAX(report_date) 落后即待补。
        return f"""
            WITH universe AS (
                SELECT DISTINCT m.stock_code AS stock_code,
                       COALESCE(m.exchange, '') AS exchange
                FROM stock_meta m
                JOIN income_statement i ON i.stock_code = m.stock_code
                WHERE {listed_clause}
            )
            SELECT u.stock_code AS stock_code,
                   u.exchange AS exchange,
                   MAX(t.report_date) AS max_report_date
            FROM universe u
            LEFT JOIN {spec.judge_table} t
                   ON t.stock_code = u.stock_code AND t.source = ?
            GROUP BY u.stock_code, u.exchange
            ORDER BY (MAX(t.report_date) IS NULL) DESC,
                     MAX(t.report_date) ASC,
                     u.stock_code ASC
        """

    @staticmethod
    def _pending_params(spec: SupplementSpec, periods: Sequence[str]) -> list[Any]:
        if spec.pending_mode == "missing_field":
            return [str(periods[-1])[:10], str(periods[0])[:10]]
        return [SOURCE]

    # ─── 失败冷却（SQLite 持久化，跨轮次生效）────────────────────────

    def _attempt_state_key(self, key: str) -> str:
        return f"{ATTEMPT_STATE_PREFIX}{key}"

    def _load_attempts(self, key: str) -> dict[str, str]:
        if self.sqlite is None:
            return {}
        try:
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = ?",
                [self._attempt_state_key(key)],
            )
            payload = json.loads(rows[0]["value"]) if rows else {}
            return payload if isinstance(payload, dict) else {}
        except Exception as error:  # noqa: BLE001 - 冷却态损坏不得阻断补采
            logger.warning("读取补采失败冷却态失败(%s): %s", key, error)
            return {}

    def _record_attempts(self, key: str, codes: Sequence[str]) -> None:
        if self.sqlite is None or not codes:
            return
        now = datetime.now(UTC)
        attempts = self._load_attempts(key)
        stamp = now.isoformat()
        for code in codes:
            attempts[str(code)] = stamp
        cutoff = now - timedelta(days=ATTEMPT_STATE_KEEP_DAYS)
        pruned = {
            code: value
            for code, value in attempts.items()
            if (moment := _parse_iso(value)) is not None and moment >= cutoff.replace(tzinfo=None)
        }
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value = excluded.value, updated_at = excluded.updated_at""",
                    [self._attempt_state_key(key), json.dumps(pruned), stamp],
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("写入补采失败冷却态失败(%s): %s", key, error)

    @property
    def last_cooldown_blocked(self) -> int:
        """上一轮 `pending_codes()` 里被失败冷却挡住的股票数（>0 表示"有得补但没跑"）。"""
        return int(getattr(self, "_last_cooldown_blocked", 0) or 0)

    @property
    def last_skipped_empty_rows(self) -> int:
        """上一轮落库时因「全列 NULL」被跳过的行数（旧行保留、不插空行）。

        2026-11-17（依据 S6）：这个计数就是"本轮覆盖到的期次里，有几行
        (表, 股票, 报告期) 源侧没给任何有效数据"。它必须可见 —— 事故当时的
        症状正是"删了但没人知道没补回来"。
        """
        return int(getattr(self, "_skipped_empty_rows", 0) or 0)

    @property
    def skipped_empty_sample(self) -> list[dict[str, Any]]:
        """上一轮被跳过的空行样本（最多 10 条，供 CLI/报告核对）。"""
        return list(getattr(self, "_skipped_empty_sample", []) or [])

    def _filter_cooldown(self, key: str, pending: list[tuple[str, str]], retry_days: int) -> list[tuple[str, str]]:
        if retry_days <= 0:
            return pending
        attempts = self._load_attempts(key)
        if not attempts:
            return pending
        cutoff = datetime.now(UTC) - timedelta(days=retry_days)
        blocked = {
            code for code, value in attempts.items()
            if (moment := _parse_iso(value)) is not None
            and moment >= cutoff.replace(tzinfo=None)
        }
        return [item for item in pending if item[0] not in blocked]

    # ─── 网络层（测试通过 monkeypatch 本方法离线化）───────────────────

    def _company_type(self, symbol: str, timeout: float) -> str | None:
        """金融股 `companyType` 与通用企业不同，从页面 hidctype 取真实值。"""
        try:
            page = _session().get(
                f"{BASE}/Index",
                params={"type": "web", "code": symbol.lower()},
                timeout=timeout,
            )
            match = re.search(r'id="hidctype"[^>]*value="([^"]+)"', page.text)
            return match.group(1) if match else None
        except Exception as error:  # noqa: BLE001
            logger.debug("取 %s companyType 失败: %s", symbol, error)
            return None

    def _fetch_rows(
        self,
        spec: SupplementSpec,
        code: str,
        exchange: str,
        dates: Sequence[str],
        *,
        timeout: float = 25.0,
    ) -> list[dict[str, Any]]:
        """拉取单只股票的目标报告期数据；失败返回空列表（逐股失败不中断整轮）。

        - 报告期数 > `PERIODS_PER_REQUEST`（5）时**分块请求**：接口单次最多回 5 期，
          传 6 个日期会静默丢掉最老的一期（2025-06-30 正是第 6 期）。
        - `resolve_company_type` 打开的 spec（金融股）：先用页面 `hidctype`
          解析 companyType（银行=3/保险=2/证券=1），否则 `companyType=4` 对
          金融股的资产负债表直接返回空、利润表返回行名不同的通用模板。
        - 多端点 spec（金融股三表 = 资产负债表 + 利润表）逐端点抓取后合并。
        """
        symbol = em_symbol(code, exchange)
        company_type = "4"
        if spec.resolve_company_type:
            company_type = self._company_type(symbol, timeout) or "4"
        date_list = [str(item)[:10] for item in dates]
        collected: list[dict[str, Any]] = []
        for endpoint in spec.endpoints or (spec.endpoint,):
            for start in range(0, len(date_list), PERIODS_PER_REQUEST):
                chunk = date_list[start:start + PERIODS_PER_REQUEST]
                rows, company_type = self._fetch_endpoint(
                    spec, endpoint, symbol, company_type, chunk, timeout=timeout,
                )
                collected.extend(rows)
        return collected

    def _fetch_endpoint(
        self,
        spec: SupplementSpec,
        endpoint: str,
        symbol: str,
        company_type: str,
        dates: Sequence[str],
        *,
        timeout: float,
    ) -> tuple[list[dict[str, Any]], str]:
        """单个端点/单个日期块；返回 (rows, 生效的 company_type)。"""
        params = {
            "companyType": company_type,
            "reportDateType": "0",
            "reportType": "1",        # 1=按报告期（累计）
            "dates": ",".join(dates),
            "code": symbol,
        }
        for attempt in range(MAX_RETRY):
            try:
                response = _session().get(
                    f"{BASE}/{endpoint}", params=params, timeout=timeout,
                )
                response.raise_for_status()
                rows = response.json().get("data") or []
                if rows:
                    return rows, company_type
                if attempt == 0:
                    detected = self._company_type(symbol, timeout)
                    if detected and detected != company_type:
                        company_type = detected
                        params["companyType"] = detected
                        continue
                return [], company_type
            except Exception as error:  # noqa: BLE001
                if attempt == MAX_RETRY - 1:
                    logger.debug("补采取数失败 %s(%s/%s): %s", symbol, spec.key, endpoint, error)
                    return [], company_type
                time.sleep(1.5 * (attempt + 1) + random.random())
        return [], company_type

    # ─── 解析 ─────────────────────────────────────────────────────

    #: P0-6 守卫本轮拒绝写入的明细（供 CLI/报告展示，不落库）
    def _record_guard_rejection(
        self, *, stock_code: str, report_date: date, source_field: str, detail: str,
    ) -> None:
        rejections = getattr(self, "_guard_rejections", None)
        if rejections is None:
            rejections = []
            self._guard_rejections = rejections
        rejections.append({
            "stock_code": stock_code,
            "report_date": report_date.isoformat(),
            "column": CONTRACT_LIABILITIES_COLUMN,
            "source_field": source_field,
            "reason_code": INSURANCE_CONTRACT_LIABILITY_REJECT_REASON,
            "detail": detail,
        })
        logger.warning(
            "守卫拦截：保险股 %s %s 的「%s」不得写入 %s（%s）",
            stock_code, report_date, source_field, CONTRACT_LIABILITIES_COLUMN,
            INSURANCE_CONTRACT_LIABILITY_REJECT_REASON,
        )

    @property
    def guard_rejections(self) -> list[dict[str, Any]]:
        """P0-6 守卫累计拦截明细（本轮进程内，供报告/CLI 摘要）。"""
        return list(getattr(self, "_guard_rejections", []) or [])

    def _insurer_codes(self, codes: Sequence[str]) -> set[str]:
        """按 `stock_meta` 的行业口径解析保险股代码集合。

        判据（依据 S5 §9.2 P0-6）：`sw_level2='保险Ⅱ'` 优先，其次
        `sw_level1='保险'`、公司名含「保险」。查询失败返回空集（守卫仍由
        源行 `ORG_TYPE` 在解析层兜底，不会因为查不到行业就放行）。
        """
        unique = sorted({str(code) for code in codes if str(code).strip()})
        if not unique:
            return set()
        placeholders = ", ".join("?" for _ in unique)
        try:
            rows = self.duck.read_query(
                f"""SELECT stock_code, sw_level1, sw_level2, name
                    FROM stock_meta WHERE stock_code IN ({placeholders})""",
                unique,
            )
        except Exception as error:  # noqa: BLE001 - 行业查不到不得阻断写库
            logger.warning("守卫读取 stock_meta 行业失败: %s", error)
            return set()
        insurers: set[str] = set()
        for row in rows:
            if is_insurance_stock(
                sw_level1=row.get("sw_level1"),
                sw_level2=row.get("sw_level2"),
                name=row.get("name"),
            ):
                insurers.add(str(row["stock_code"]))
        return insurers

    def _available_columns(self, tables: Collection[str]) -> dict[str, set[str]]:
        """目标表的真实列集合（`information_schema.columns`）。

        查询失败返回 `{}`（= 未知）：调用方必须把"未知"当成"没有该列"处理，
        绝不能凭 spec 里的声明往库里写不存在的列。
        """
        wanted = sorted({str(table) for table in tables})
        if not wanted:
            return {}
        placeholders = ", ".join("?" for _ in wanted)
        try:
            rows = self.duck.read_query(
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_name IN ({placeholders})",
                wanted,
            )
        except Exception as error:  # noqa: BLE001 - 列探测失败保持当前行为
            logger.debug("读取目标表列集合失败: %s", error)
            return {}
        available: dict[str, set[str]] = {table: set() for table in wanted}
        for row in rows:
            available.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
        return available

    def _adapt_spec_to_schema(
        self, spec: SupplementSpec, available: Mapping[str, Collection[str]],
    ) -> SupplementSpec:
        """按目标表**真实**列集合裁剪 spec（主控未加的列绝不写）。

        目前只作用于 P0-6 的 `insurance_contract_liability`：
        - 列不存在（2026-11-16 正式库现状）→ 原样返回，CAS25 映射不启用；
        - 列已由主控加上 → 自动启用 `保险合同负债 → insurance_contract_liability`
          映射并把该列补进 `spec.columns`，无需再改代码。
        """
        if spec.key != FINANCIAL_SECTOR:
            return spec
        table = INSURANCE_CONTRACT_LIABILITY_TABLE
        columns = available.get(table) or set()
        if INSURANCE_CONTRACT_LIABILITY_COLUMN not in columns:
            return spec
        org_maps = spec.org_field_maps or {}
        insurer_map = dict(org_maps.get("保险") or {})
        if any(
            column == INSURANCE_CONTRACT_LIABILITY_COLUMN
            for column in insurer_map.values()
        ):
            return spec  # 幂等：已经适配过
        insurer_map.update(FINANCIAL_SECTOR_FIELD_MAP_INSURER_CAS25)
        target_columns = spec.columns[table]
        if INSURANCE_CONTRACT_LIABILITY_COLUMN not in target_columns:
            target_columns = (*target_columns, INSURANCE_CONTRACT_LIABILITY_COLUMN)
        return replace(
            spec,
            columns={**dict(spec.columns), table: target_columns},
            org_field_maps={**dict(org_maps), "保险": insurer_map},
        )

    def _parse_rows(
        self,
        spec: SupplementSpec,
        code: str,
        rows: Sequence[dict[str, Any]],
        *,
        batch_id: str,
        fetched_at: datetime,
        src_hash: str,
        available_columns: Mapping[str, Collection[str]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """把源返回的原始行解析成按表分组的落库行（列名显式，不依赖列序）。

        多端点 spec（金融股）会把同一报告期的资产负债表行与利润表行**合并**
        成一行；否则两行同主键会在 INSERT 时撞 PK。

        `available_columns`：目标表真实列集合（`{table: {column, …}}`）。仅用于
        P0-6 守卫判断「专用列 insurance_contract_liability 是否已由主控加上」；
        缺省（None）= 未知 = 按"列不存在"处理，绝不写出不存在的列。
        """
        parsed: dict[str, list[dict[str, Any]]] = {table: [] for table in spec.columns}
        floor = spec.period_floor if spec.period_floor is not None else CSMAR_CUTOFF
        # ORG_TYPE 是公司属性，不是单行属性：先定一次，避免同一只股票的两张表
        # （或缺失该键的行）用不同行业映射解析。
        stock_org = next(
            (
                str(row.get("ORG_TYPE") or "").strip()
                for row in rows
                if str(row.get("ORG_TYPE") or "").strip()
            ),
            "",
        )
        # 2026-11-16（依据 S5 P0-6）：保险股判定。源响应自带 ORG_TYPE，
        # 缺该键时由 `refresh()` 用 stock_meta.sw_level2 兜底（见 `_insurer_codes`）。
        stock_is_insurer = is_insurance_stock(org_type=stock_org)
        merged: dict[date, dict[str, Any]] = {}
        for row in rows:
            raw_date = str(row.get("REPORT_DATE") or "")[:10]
            try:
                report_date = date.fromisoformat(raw_date)
            except ValueError:
                continue
            if report_date <= floor:
                continue  # 只补 CSMAR 未覆盖的报告期
            # 按行业选映射：同名不同义（券商 CUSTOMER_DEPOSIT=客户资金存款）。
            em_map: Mapping[str, str] = spec.field_map
            derived: Mapping[str, tuple[str, ...]] = {}
            if spec.org_field_maps is not None:
                org_map = spec.org_field_maps.get(stock_org)
                if org_map is not None and spec.org_field_maps_merge:
                    # 覆盖层：行业专用字段排在前（先到先得），通用字段兜底。
                    em_map = {**dict(org_map), **dict(spec.field_map)}
                elif org_map is not None:
                    em_map = org_map
                if spec.org_derived_sums is not None:
                    derived = spec.org_derived_sums.get(stock_org, {})
            # 2026-11-16（依据 S5 P0-3）：同一目标列可能有多条候选源字段
            #（通用模板 vs 金融专用模板）。解析规则改为**先到先得**：
            # spec.field_map 里靠前的键优先，取到第一个非空值后不再覆盖。
            # 旧实现是 `{column: _to_float(row.get(em_field))}` 的 dict 推导式，
            # 后写的键会静默覆盖先写的键 —— 在"同名列不同义"场景下会写错值。
            values: dict[str, float | None] = {}
            hit_sources: dict[str, str] = {}
            for em_field, column in em_map.items():
                if values.get(column) is not None:
                    continue
                value = _to_float(row.get(em_field))
                if value is None:
                    continue
                values[column] = value
                hit_sources[column] = em_field
            for column, parts in derived.items():
                present = [
                    value
                    for value in (_to_float(row.get(part)) for part in parts)
                    if value is not None
                ]
                if present:
                    values[column] = sum(present)
                    hit_sources[column] = "+".join(parts)
            if (
                not any(value is not None for value in values.values())
                and spec.key != CASHFLOW_SUPPLEMENT
            ):
                # 2026-11-17（依据 S6）：现金流量补充域**必须**把「源返回了该期
                # 但一个科目都没给」的行也交给落库层 —— 只有落库层知道哪些
                # (stock_code, report_date) 是本轮"覆盖到但无有效数据"，从而
                # 既不删旧行也不插空行（`skipped_empty_rows`）。其余域维持原样。
                continue
            # P0-6 写库前守卫：保险股的「保险合同负债」绝不再进通用科目。
            guard = guard_contract_liabilities(
                values,
                source_fields=hit_sources,
                is_insurance=stock_is_insurer,
                available_columns=(
                    available_columns.get(INSURANCE_CONTRACT_LIABILITY_TABLE)
                    if available_columns
                    else None
                ),
            )
            if guard.rejected and CONTRACT_LIABILITIES_COLUMN in hit_sources:
                self._record_guard_rejection(
                    stock_code=code,
                    report_date=report_date,
                    source_field=hit_sources[CONTRACT_LIABILITIES_COLUMN],
                    detail=guard.reasons.get(CONTRACT_LIABILITIES_COLUMN, ""),
                )
            values = guard.values
            base = {
                "stock_code": code,
                "report_date": report_date,
                "report_type": report_type_of(report_date),
                "fetch_time": fetched_at,
                "raw_response_hash": src_hash,
                "confidence": CONFIDENCE,
                "batch_id": batch_id,
            }
            if spec.key == CASHFLOW_SUPPLEMENT:
                # 2026-11-16（依据 S5 P0-3）：按**整表**列集合落库，不再是
                # 只写 field_map 里那几个已映射的列。两张表的数值列清单来自
                # `spec.columns`，源侧没有的列保持 NULL（并已在
                # `CASH_FLOW_STRUCTURAL_MISSING` 里显式登记原因）。
                indirect_columns = [
                    column for column in spec.columns["cash_flow_indirect"]
                    if column not in ("stock_code", "report_date", "report_type",
                                      "source", "fetch_time", "raw_response_hash",
                                      "confidence", "batch_id", "raw_data")
                ]
                indirect = {
                    **base,
                    **{column: values.get(column) for column in indirect_columns},
                    "source": SOURCE,
                    "raw_data": "{}",
                }
                # 2026-11-17（依据 S6）：**全 NULL 的行也要交给落库层**，由
                # `_persist_rows` 统一决定"不删旧行、不插空行、只计数"。
                # 旧实现在这里 `if any(...)` 直接把空行丢掉，落库层看不到它们，
                # 于是 replace 模式照删不误 —— 2025-06-30 的 4 行真实数据就是这么没的。
                parsed["cash_flow_indirect"].append(indirect)
                activity_columns = [
                    column for column in spec.columns["cash_flow_activity"]
                    if column not in ("stock_code", "report_date", "report_type",
                                      "source", "fetch_time", "raw_response_hash",
                                      "confidence", "batch_id")
                ]
                activity = {
                    **base,
                    **{column: values.get(column) for column in activity_columns},
                    "source": SOURCE,
                }
                # 旧实现只在 capex 有值时才写活动表 → 没有资本支出的股票整行丢失。
                parsed["cash_flow_activity"].append(activity)
            elif spec.write_mode == "replace_source_rows":
                # 通用 replace 模式（balance_sheet_ext / 金融股专用三表）：
                # 同报告期的多端点行合并，非空值优先。
                record = merged.setdefault(report_date, {**base, "source": SOURCE})
                record.update({
                    column: value
                    for column, value in values.items()
                    if value is not None
                })
            else:
                # update_existing 模式（扣非净利润 / 固定资产净额…）：
                # 只产出「主键 + 目标列」，由 _persist_rows 做 "只填 NULL" 的 UPDATE。
                # 2026-11-14：原实现硬编码 income_statement/deducted_net_profit，
                # 导致新增数据集（fixed_assets）直接 KeyError，这里改为按 spec 驱动的通用形态。
                target_table = next(iter(spec.columns))
                target_columns = spec.columns[target_table]
                row_values = {
                    column: values.get(column)
                    for column in target_columns
                    if column not in ("stock_code", "report_date")
                }
                parsed.setdefault(target_table, []).append({
                    "stock_code": code,
                    "report_date": report_date,
                    **row_values,
                })
        if merged:
            target_table = next(iter(spec.columns))
            parsed[target_table].extend(merged.values())
        return parsed

    # ─── 落库 ─────────────────────────────────────────────────────

    @property
    def last_dropped_nonstandard_periods(self) -> int:
        """最近一次落库时被丢弃的「非标准报告期」行数（reports/146）。"""
        return self._dropped_nonstandard_periods

    @property
    def last_dropped_nonstandard_sample(self) -> list[dict[str, Any]]:
        """被丢弃行的样例（最多 10 条），用于日志定位。"""
        return list(self._dropped_nonstandard_sample)

    def _drop_nonstandard_periods(
        self, frame: pd.DataFrame, *, table: str,
    ) -> pd.DataFrame:
        """丢掉 `report_date` 不是标准期末（03-31/06-30/09-30/12-31）的行。

        为什么必须挡住（reports/146）：CSMAR 历史批次把「上年年报」重复记成
        「次年 01-01」，全库因此多出 116,109 行重复 + 8,630 行错期。
        报告期只有四种形态，任何其它日期都意味着**解析或口径有错**——
        与其带着伪期次入库让下游 `ORDER BY report_date` / LAG / TTM 全算歪，
        不如在这里丢弃并记数。

        丢弃而不是抛错：一次补采里个别行的日期畸形不该让整批失败；
        计数量挂在 `last_dropped_nonstandard_periods` 供上层日志与体检使用。
        """
        from app.core.report_periods import is_standard_report_date

        if frame.empty:
            return frame
        mask = frame["report_date"].map(
            lambda value: is_standard_report_date(value.date() if hasattr(value, "date") else value)
        )
        dropped = int((~mask).sum())
        self._dropped_nonstandard_periods += dropped
        if dropped and len(self._dropped_nonstandard_sample) < 10:
            for _, row in frame.loc[~mask].head(
                10 - len(self._dropped_nonstandard_sample)
            ).iterrows():
                self._dropped_nonstandard_sample.append({
                    "table": table,
                    "stock_code": str(row.get("stock_code")),
                    "report_date": str(row.get("report_date"))[:10],
                })
        return frame.loc[mask]

    def _strip_insurer_contract_liabilities(
        self, frame: pd.DataFrame, *, table: str,
    ) -> int:
        """把保险股行的 `contract_liabilities` 置 NULL（P0-6 写库前守卫）。

        Returns:
            被置空的行数（0 = 没有保险股，或本来就没有值）。
        """
        if frame.empty:
            return 0
        codes = frame["stock_code"].astype(str).tolist()
        insurers = self._insurer_codes(codes)
        if not insurers:
            return 0
        mask = frame["stock_code"].astype(str).isin(insurers) & frame[
            CONTRACT_LIABILITIES_COLUMN
        ].notna()
        affected = int(mask.sum())
        if affected == 0:
            return 0
        for _, row in frame.loc[mask].iterrows():
            self._record_guard_rejection(
                stock_code=str(row["stock_code"]),
                report_date=pd.Timestamp(row["report_date"]).date()
                if "report_date" in frame.columns else cn_today(),
                source_field=str(CONTRACT_LIABILITIES_COLUMN),
                detail=(
                    "写库前守卫：保险股的 "
                    f"{table}.{CONTRACT_LIABILITIES_COLUMN} 被强制置空"
                    "（reports/131 §9.2 P0-6）"
                ),
            )
        frame.loc[mask, CONTRACT_LIABILITIES_COLUMN] = float("nan")
        return affected

    def _persist_rows(
        self,
        spec: SupplementSpec,
        rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        batch_id: str,
        fetched_at: datetime,
        src_hash: str,
        periods: Sequence[str],
        lineage_payload: bytes | None = None,
    ) -> dict[str, int]:
        """按数据集写入模式落库，返回 {table: 真正写入的行数}。

        `lineage_payload`：本轮请求描述（JSON 字节），会用 `src_hash` 归档进
        `raw_response_archive`，使 `fetch_batch` 的摘要与归档一一对应。

        `replace_source_rows` 的删除纪律（2026-11-17，依据 S6 事故修复）
        -----------------------------------------------------------------
        旧实现按 `report_date IN (batch_periods)`（或整股票）删，然后只插"至少一个
        非空值"的行 —— 一支股票在 A 期有值、B 期全空时，B 期的**旧真实数据**会被
        删掉且补不回来（2025-06-30 的 4 行就是这样没的）。现在的规则：

        1. **只删本轮真的写入了行的组合**：`batch_periods` 按
           `(stock_code, report_date)` 精确匹配；`all` 按"本轮有新数据的股票"整体
           覆盖（保留原有"清掉窗口外旧本源行"的语义），但会**排除**本轮明确
           返回全空的那几期。
        2. **空值保护**：任一 `(stock_code, report_date)` 的新行如果所有数值列都是
           NULL，则**不删旧行、也不插入**，只计入 `skipped_empty_rows`
           （见 `last_skipped_empty_rows`）。

        写库前最后一道 P0-6 守卫：目标表若含 `contract_liabilities` 列，则按
        `stock_meta` 行业口径把**保险股**的那一列强制置空 —— 即使上游解析漏判
        （源行缺 `ORG_TYPE`、或未来的新 spec 误把「保险合同负债」映射到通用列），
        也绝不可能把保险股的保险合同负债写进合同负债。
        """
        written: dict[str, int] = {}
        skipped_total = 0
        skipped_sample: list[dict[str, Any]] = []
        self._skipped_empty_rows = 0
        self._skipped_empty_sample = []
        with self.duck.transaction() as conn:
            for table, columns in spec.columns.items():
                rows = list(rows_by_table.get(table) or [])
                if not rows:
                    written[table] = 0
                    continue
                frame = pd.DataFrame(rows, columns=list(columns))
                if "report_date" in frame.columns:
                    frame["report_date"] = pd.to_datetime(frame["report_date"])
                    # 2026-11-16（reports/146）：非标准报告期一律不落库。
                    # 历史事故：CSMAR 批次把「上年年报」重复记成「次年 01-01」，
                    # 全库留下 116,109 行重复 + 8,630 行错期（详见
                    # scripts/repair_nonstandard_report_dates.py）。这里是
                    # **新数据**的入口守卫，存量由该脚本清理。
                    frame = self._drop_nonstandard_periods(frame, table=table)
                if CONTRACT_LIABILITIES_COLUMN in frame.columns:
                    self._strip_insurer_contract_liabilities(frame, table=table)
                empty_frame = frame.iloc[0:0]
                if spec.write_mode == "replace_source_rows":
                    # 只把「至少一个数值列非空」的行拿去覆盖；全 NULL 行是"本轮
                    # 覆盖到但无有效数据"的证据，用来**保护**旧行，绝不写库。
                    value_columns = [
                        column for column in columns if column not in _ROW_META_COLUMNS
                    ]
                    if value_columns:
                        has_value = frame[value_columns].notna().any(axis=1)
                    else:  # 防御：没有数值列的 spec 不应走到 replace 模式
                        has_value = pd.Series(False, index=frame.index)
                    empty_frame = frame.loc[~has_value]
                    frame = frame.loc[has_value]
                    if not empty_frame.empty:
                        skipped_total += len(empty_frame)
                        if len(skipped_sample) < 10:
                            for _, row in empty_frame.head(
                                10 - len(skipped_sample)
                            ).iterrows():
                                skipped_sample.append({
                                    "table": table,
                                    "stock_code": str(row.get("stock_code")),
                                    "report_date": str(row.get("report_date"))[:10],
                                })
                    if frame.empty:
                        # 本轮这一张表全是空行：一行不删、一行不插。
                        written[table] = 0
                        continue
                # `_supp_empty` 始终注册（空表也要存在），`all` 模式的
                # NOT EXISTS 子句依赖它把"本轮明确返回全空"的期次排除在删除范围外。
                conn.register("_supp_empty", empty_frame[["stock_code", "report_date"]])
                conn.register("_supp_rows", frame)
                try:
                    if spec.write_mode == "replace_source_rows":
                        if spec.delete_scope == "batch_periods":
                            # 只删本轮**真的写入**的 (stock_code, report_date)：
                            # 同一批里某期返回空时，该期的旧行原样保留。
                            conn.execute(
                                f"""DELETE FROM {table} AS t
                                    WHERE t.source = ?
                                      AND EXISTS (
                                          SELECT 1 FROM _supp_rows AS r
                                          WHERE r.stock_code = t.stock_code
                                            AND r.report_date = t.report_date
                                      )""",
                                [SOURCE],
                            )
                        else:
                            # all：按"本轮有新数据的股票"整体覆盖同源行（清掉窗口外
                            # 的旧本源行），但**排除**本轮明确返回全空的那几期 ——
                            # 空值保护在两种 delete_scope 下都必须成立。
                            conn.execute(
                                f"""DELETE FROM {table} AS t
                                    WHERE t.source = ?
                                      AND EXISTS (
                                          SELECT 1 FROM _supp_rows AS r
                                          WHERE r.stock_code = t.stock_code
                                      )
                                      AND NOT EXISTS (
                                          SELECT 1 FROM _supp_empty AS e
                                          WHERE e.stock_code = t.stock_code
                                            AND e.report_date = t.report_date
                                      )""",
                                [SOURCE],
                            )
                        conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _supp_rows")
                    else:
                        # 只填 NULL：既有的 CSMAR / 其他源值保持不动（主链只读纪律）。
                        for column in columns:
                            if column in {"stock_code", "report_date"}:
                                continue
                            null_guard = (
                                f" AND t.{column} IS NULL"
                                if spec.fills_null_only else ""
                            )
                            conn.execute(
                                f"UPDATE {table} AS t SET {column} = r.{column} "
                                "FROM _supp_rows AS r "
                                "WHERE t.stock_code = r.stock_code "
                                "AND t.report_date = r.report_date"
                                f" AND r.{column} IS NOT NULL{null_guard}",
                            )
                finally:
                    conn.unregister("_supp_rows")
                    conn.unregister("_supp_empty")
                written[table] = len(frame)
            # 先归档 lineage payload，再登记批次：否则 batch_hash 侧会判
            # "有摘要、无归档" = 真断裂（正是本轮事故的形态）。
            if lineage_payload is not None:
                archive_raw_response_if_absent(
                    conn,
                    raw_response_hash=src_hash,
                    source=f"supplement:{spec.endpoint}",
                    fetch_time=fetched_at,
                    payload=lineage_payload,
                    api_version=spec.api_version,
                )
            conn.execute(
                """INSERT INTO fetch_batch
                   (batch_id, data_type, source, adapter_version, fetch_time,
                    raw_response_hash, row_count, report_date_range, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    batch_id, spec.data_type, SOURCE, spec.api_version, fetched_at,
                    src_hash, sum(written.values()),
                    f"{str(periods[-1])[:10]}~{str(periods[0])[:10]}", CONFIDENCE,
                ],
            )
        self._skipped_empty_rows = skipped_total
        self._skipped_empty_sample = skipped_sample
        return written

    # ─── 主入口 ───────────────────────────────────────────────────

    def refresh(
        self,
        key: str,
        *,
        periods: Sequence[str] | None = None,
        max_stocks: int = 0,
        concurrency: int = 4,
        timeout: float = 25.0,
        deadline_seconds: float = 0.0,
        retry_days: int = 0,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
        field_map: Mapping[str, str] | None = None,
        force_codes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """有界补采一轮：到期由调用方判断，本方法只管"本轮抓哪些、抓多少"。

        Args:
            max_stocks: 本轮最多处理的股票数（0=不设上限，仅测试/手动用）。
            deadline_seconds: 本轮最大耗时（秒）；到点后剩余股票转 deferred，
                下一轮继续（按 max_report_date 升序，进度只前进不回头）。
            retry_days: 失败冷却天数，抓过但仍无该期数据的股票在冷却期内跳过。
            field_map: 临时覆盖 spec 的字段映射（同一张表可能存在两种口径，
                例如 `exchange_gain` 的"公允价值变动收益 vs 汇兑收益"之辨）；
                只影响解析，不影响待补判据。

        Returns:
            {status, reason, periods, targeted, updated, skipped, failed,
             deferred, rows_written, skipped_empty_rows, failed_codes,
             elapsed_seconds}

            `skipped_empty_rows`：本轮"覆盖到但全列 NULL"的行数（每张表各计一行）。
            这些 (股票, 报告期) 的旧行**原样保留**，不会被删除。
        """
        started = time.time()
        spec = SPECS[key]
        if field_map is not None:
            spec = replace(spec, field_map=dict(field_map))
        # 2026-11-16（依据 S5 P0-6）：按目标表**真实**列集合裁剪 spec。
        # 主控还没加的列（`financial_sector_items.insurance_contract_liability`）
        # 绝不能出现在 INSERT 里，否则整批 `INSERT ... BY NAME` 直接失败。
        self._guard_rejections = []
        available_columns = self._available_columns(spec.columns)
        spec = self._adapt_spec_to_schema(spec, available_columns)
        resolved_periods = self.resolve_periods(key, periods=periods)
        report: dict[str, Any] = {
            "dataset": key,
            "status": "skipped",
            "reason": None,
            "periods": resolved_periods,
            "targeted": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "no_data": 0,
            "deferred": 0,
            "rows_written": {},
            "skipped_empty_rows": 0,
            "skipped_empty_sample": [],
            "failed_codes": [],
            "remaining_pending": 0,
            "elapsed_seconds": 0.0,
        }
        if not resolved_periods:
            report["reason"] = "no_target_periods"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report
        try:
            # 先取"未截断"的完整待补集合：remaining_pending 是调用方判断
            # "这一域是否已追平"的唯一依据（追平才推进水位，否则下一轮继续）。
            all_pending = self.pending_codes(
                key, resolved_periods, retry_days=retry_days,
            )
            pending = all_pending[:max_stocks] if max_stocks > 0 else all_pending
            # 2026-11-17（依据 S5 P0-3）：`force_codes` 用于"判据认为已追平、
            # 但库里该期只有半空壳"的存量修复——pending 判据只看"有没有行"、
            # 不看"行里的列填没填"。显式点名时绕过判据。
            if force_codes:
                known = {code for code, _ in pending}
                wanted = [str(c) for c in force_codes if str(c) not in known]
                if wanted:
                    slots = ", ".join("?" for _ in wanted)
                    rows = self.duck.read_query(
                        f"SELECT stock_code, exchange FROM stock_meta "
                        f"WHERE stock_code IN ({slots})",
                        wanted,
                    )
                    extra = [
                        (str(row["stock_code"]), str(row.get("exchange") or ""))
                        for row in rows
                    ]
                    if extra:
                        pending = list(pending) + extra
                        all_pending = pending
        except Exception as error:  # noqa: BLE001 - 判据查询失败只影响本数据集
            logger.warning("补采待补查询失败(%s): %s", key, error)
            report["status"] = "failed"
            report["reason"] = f"pending_query_failed: {type(error).__name__}: {error}"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report

        # `pending_codes` 已经做过失败冷却过滤与每轮上限截断，这里不再重复。
        if not pending:
            # 2026-11-16（依据 S4）：**必须区分**「真的没得补」与「有得补但都在冷却」。
            # 旧实现两者都报 `no_pending_codes`，而 `update.py` 把 `no_pending_codes`
            # 当成"该域已追平"→ 推进水位 → 待补数永远不动（`balance_sheet_ext`
            # 105 只连续多轮不变就是这么来的），`CHAIN_PROGRESS_STALLED` 因此永久红。
            blocked = self.last_cooldown_blocked
            if blocked > 0:
                report["reason"] = "all_pending_in_cooldown"
                report["status"] = "deferred"
                report["remaining_pending"] = blocked
                report["cooldown_blocked"] = blocked
                logger.info(
                    "补采 %s：%d 只待补全部处于失败冷却，本轮跳过（不计为已追平）",
                    key, blocked,
                )
            else:
                report["reason"] = "no_pending_codes"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report

        batch_id = uuid.uuid4().hex
        fetched_at = datetime.now(UTC).replace(tzinfo=None)
        # 2026-11-15（依据 S3）：这里原来写 `f"{spec.endpoint}:{batch_id}"[:32]`，
        # 产出的是 "xjllbAjaxNew:451ef0802792468b909c" 这种 **32 位非十六进制**串。
        # 数据质量规则按"32 位且形如 MD5 = legacy 历史批次"判定，其余形态一律算
        # **真 lineage 断裂** → LINEAGE_INVALID → 整个快照被遮蔽（实测 13 个批次
        # 把 5,579 只股票的 34 个指标全部打成 null）。
        # 改成内容寻址：对"本轮请求描述"取 SHA256（64 位十六进制），并把该描述
        # 作为 payload 归档，使 fetch_batch.raw_response_hash 与归档一一对应。
        lineage_payload = json.dumps(
            {
                "endpoint": spec.endpoint,
                "endpoints": list(spec.endpoints or ()),
                "batch_id": batch_id,
                "data_type": spec.data_type,
                "source": SOURCE,
                "api_version": spec.api_version,
                "codes": sorted(c for c, _ in pending),
                "periods": [str(p)[:10] for p in periods],
                "fetched_at": fetched_at.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        src_hash = hashlib.sha256(lineage_payload).hexdigest()
        rows_by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in spec.columns}
        fetched_codes: list[str] = []
        failed_codes: list[str] = []
        no_data_codes: list[str] = []
        deferred: list[str] = []

        pool = ThreadPoolExecutor(max_workers=max(1, int(concurrency)))
        futures = {
            pool.submit(
                self._fetch_rows, spec, code, exchange, resolved_periods,
                timeout=timeout,
            ): code
            for code, exchange in pending
        }
        try:
            remaining = set(futures)
            while remaining:
                done, _ = wait(remaining, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in done:
                    remaining.discard(future)
                    code = futures[future]
                    try:
                        rows = future.result()
                    except Exception as error:  # noqa: BLE001 - 逐股失败如实计数
                        logger.debug("补采取数异常 %s: %s", code, error)
                        rows = []
                    if not rows:
                        failed_codes.append(code)
                        continue
                    parsed = self._parse_rows(
                        spec, code, rows,
                        batch_id=batch_id, fetched_at=fetched_at, src_hash=src_hash,
                        available_columns=available_columns,
                    )
                    # 2026-11-17（依据 S6）：解析层现在会把"全 NULL 行"也留下
                    # （作为空值保护的证据），所以不能再用 `any(parsed.values())`
                    # 判"有没有数据"——那会把空行算成有数据。改为看**数值列**。
                    if any(
                        any(
                            value is not None
                            for key, value in row.items()
                            if key not in _ROW_META_COLUMNS
                        )
                        for items in parsed.values()
                        for row in items
                    ):
                        fetched_codes.append(code)
                    else:
                        # 取数成功但源侧一个有效值都没给（无适用科目 / 本期全空）：
                        # 这是"无有效字段"而不是取数失败，分开计数，
                        # 避免金融股补采每轮都报 partial。
                        no_data_codes.append(code)
                    for table, items in parsed.items():
                        rows_by_table[table].extend(items)
                    if progress_cb is not None:
                        progress_cb(code, {
                            "done": len(fetched_codes) + len(failed_codes)
                            + len(no_data_codes),
                            "total": len(pending),
                        })
                if deadline_seconds and (time.time() - started) >= deadline_seconds:
                    deferred = [futures[f] for f in remaining]
                    for future in remaining:
                        future.cancel()
                    remaining.clear()
                    break
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        try:
            written = self._persist_rows(
                spec, rows_by_table,
                batch_id=batch_id, fetched_at=fetched_at, src_hash=src_hash,
                periods=resolved_periods, lineage_payload=lineage_payload,
            ) if any(rows_by_table.values()) else {}
        except Exception as error:  # noqa: BLE001 - 落库失败整批回滚，旧值保留
            logger.warning("补采落库失败(%s): %s", key, error)
            report.update({
                "status": "failed",
                "reason": f"persist_failed: {type(error).__name__}: {error}",
                "targeted": len(pending),
                "failed": len(failed_codes),
                "no_data": len(no_data_codes),
                "failed_codes": failed_codes[:20],
                "elapsed_seconds": round(time.time() - started, 2),
            })
            return report

        # 只把**真正尝试过**的股票记入失败冷却：deferred 是"截止时间到了、
        # 还没来得及抓"，若也记进去会被冷却窗口白挡 7 天。
        self._record_attempts(
            key, list(fetched_codes) + failed_codes + no_data_codes,
        )

        updated = len(fetched_codes)
        if updated == 0:
            if no_data_codes and not failed_codes:
                status = "skipped"
                reason = "no_applicable_fields"
            else:
                status = "failed"
                reason = "no_rows_fetched"
        elif failed_codes or deferred:
            status = "partial"
            reason = "partial_source_failures"
        else:
            status = "success"
            reason = None
        report.update({
            "status": status,
            "reason": reason,
            "targeted": len(pending),
            "updated": updated,
            "failed": len(failed_codes),
            "no_data": len(no_data_codes),
            "deferred": len(deferred),
            "skipped": max(
                0,
                len(pending) - updated - len(failed_codes) - len(no_data_codes)
                - len(deferred),
            ),
            "rows_written": written,
            # 空值保护的可核对读数：本轮有几行源侧全空（旧行保留、未插入）。
            "skipped_empty_rows": self.last_skipped_empty_rows,
            "skipped_empty_sample": self.skipped_empty_sample,
            "failed_codes": failed_codes[:20],
            "no_data_codes": no_data_codes[:20],
            # 本轮之后还剩多少只待补（含本轮 deferred）。调用方据此决定
            # 是否推进水位：还有残留就下一轮继续，否则会被间隔白挡 7 天。
            "remaining_pending": max(0, len(all_pending) - updated),
            "guard_rejections": len(self.guard_rejections),
            "guard_rejection_sample": self.guard_rejections[:10],
            "elapsed_seconds": round(time.time() - started, 2),
        })
        logger.info(
            "补采 %s: targeted=%d updated=%d failed=%d no_data=%d deferred=%d "
            "remaining=%d rows=%s elapsed=%.1fs",
            key, report["targeted"], report["updated"], report["failed"],
            report["no_data"], report["deferred"], report["remaining_pending"],
            written, report["elapsed_seconds"],
        )
        return report


def _parse_iso(value: Any) -> datetime | None:
    """解析 data_refresh_state 里的 ISO 时间戳；无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


# ─── 金融监管指标（6 列）──────────────────────────────────────────────
#
# 与上面三个数据集不同：该域**每次到期全量重抓**（每只金融股 1 次请求，
# 只取最新报告期 1 行），没有"有没有本源行"的续传判据，因此旧脚本不存在
# 上面那个 bug；接入调度时只需补上到期判断 + 每轮上限。

REGULATORY_REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
# ⚠️ 同名不同义：`CQBL` 对**银行**是核心一级资本充足率，对**券商**是另一个
# 概念（东北证券 5.197）。因此按行业分别映射，跨行业一律不写。
REGULATORY_FIELD_MAP_BANK: dict[str, str] = {
    "NEWCAPITALADER": "capital_adequacy_ratio",
    "FIRST_ADEQUACY_RATIO": "tier1_capital_adequacy_ratio",
    "CQBL": "core_tier1_capital_adequacy_ratio",
    "NONPERLOAN": "non_performing_loan_ratio",
    "BLDKBBL": "provision_coverage_ratio",
}
REGULATORY_FIELD_MAP_BROKER: dict[str, str] = {
    "RISK_COVERAGE": "risk_coverage_ratio",
}
REGULATORY_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date",
    *dict.fromkeys([
        *REGULATORY_FIELD_MAP_BANK.values(),
        *REGULATORY_FIELD_MAP_BROKER.values(),
    ]),
)
# 与配置一致的行业口径（按名称识别金融股）
FINANCIAL_LIKE = ("银行", "证券", "保险", "信托", "金融", "金控")


def regulatory_field_map(name: str) -> dict[str, str]:
    """按公司名称判定行业 → 返回适用的字段映射（跨行业不写）。"""
    if "银行" in name:
        return REGULATORY_FIELD_MAP_BANK
    if "证券" in name:
        return REGULATORY_FIELD_MAP_BROKER
    return {}


def datacenter_secucode(code: str, exchange: str | None) -> str:
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get((exchange or "").upper())
    if suffix is None:
        suffix = em_symbol(code, exchange)[:2]
    return f"{code}.{suffix}"


class RegulatoryMetricsUpdater:
    """6 个金融监管指标补采（东财 datacenter 主要指标，1 请求/股票）。

    只对银行/券商抓取（保险/信托/金融/金控该指标本不适用，保持 NULL）；
    缺失如实为空，不估算。每轮全量重抓，靠 `max_stocks` + 截止时间有界。
    """

    def __init__(self, duck: DuckDBStore) -> None:
        self.duck = duck

    def universe(self) -> list[tuple[str, str, str]]:
        """金融类股票 (code, exchange, name) —— 按名称识别，与旧脚本一致。"""
        like = " OR ".join(f"name LIKE '%{keyword}%'" for keyword in FINANCIAL_LIKE)
        rows = self.duck.read_query(
            f"""SELECT stock_code, COALESCE(exchange, '') AS exchange, name
                FROM stock_meta
                WHERE is_listed IS TRUE AND ({like})
                ORDER BY stock_code"""
        )
        return [
            (str(row["stock_code"]), str(row["exchange"]), str(row["name"]))
            for row in rows
            if regulatory_field_map(str(row["name"]))
        ]

    def coverage_gap_codes(self) -> set[str]:
        """最新可得报告期里 6 列仍为空的股票（用于把缺口股排在队首）。"""
        try:
            rows = self.duck.read_query(
                """SELECT stock_code FROM balance_sheet
                   WHERE report_date = (
                       SELECT MAX(report_date) FROM balance_sheet
                   ) AND capital_adequacy_ratio IS NULL
                     AND risk_coverage_ratio IS NULL"""
            )
            return {str(row["stock_code"]) for row in rows}
        except Exception as error:  # noqa: BLE001
            logger.debug("读取监管指标缺口失败: %s", error)
            return set()

    def _fetch_row(
        self, secucode: str, *, timeout: float = 25.0,
    ) -> dict[str, Any] | None:
        """取单只股票最新一期的监管指标原始行（测试 monkeypatch 本方法）。"""
        params = {
            "reportName": REGULATORY_REPORT_NAME, "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": "1", "pageSize": "1",
            "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        }
        session = _session()
        session.headers.setdefault("Referer", "https://emweb.securities.eastmoney.com/")
        for attempt in range(MAX_RETRY):
            try:
                response = session.get(DATACENTER_URL, params=params, timeout=timeout)
                response.raise_for_status()
                rows = (response.json().get("result") or {}).get("data") or []
                return rows[0] if rows else None
            except Exception as error:  # noqa: BLE001
                if attempt == MAX_RETRY - 1:
                    logger.debug("监管指标取数失败 %s: %s", secucode, error)
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def refresh(
        self,
        *,
        max_stocks: int = 200,
        deadline_seconds: float = 0.0,
        timeout: float = 25.0,
        pause_seconds: float = 0.35,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """全量重抓一轮（有界）；只 UPDATE `balance_sheet` 既有行，不 INSERT。"""
        started = time.time()
        report: dict[str, Any] = {
            "status": "skipped",
            "reason": None,
            "targeted": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "deferred": 0,
            "rows_updated": 0,
            "coverage_rows": 0,
            "failed_codes": [],
            "elapsed_seconds": 0.0,
        }
        try:
            universe = self.universe()
        except Exception as error:  # noqa: BLE001
            report["status"] = "failed"
            report["reason"] = f"universe_query_failed: {type(error).__name__}: {error}"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report
        if not universe:
            report["reason"] = "no_financial_stocks"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report
        gap_codes = self.coverage_gap_codes()
        # 缺口股优先，其余按代码序（保证有界续传只前进不回退）。
        ordered = sorted(universe, key=lambda item: (item[0] not in gap_codes, item[0]))
        if max_stocks > 0:
            ordered = ordered[:max_stocks]

        records: list[dict[str, Any]] = []
        failed_codes: list[str] = []
        deferred = 0
        for index, (code, exchange, name) in enumerate(ordered):
            if deadline_seconds and (time.time() - started) >= deadline_seconds:
                deferred = len(ordered) - index
                break
            field_map = regulatory_field_map(name)
            row = self._fetch_row(datacenter_secucode(code, exchange), timeout=timeout)
            if row is None:
                failed_codes.append(code)
                continue
            raw_date = str(row.get("REPORT_DATE") or "")[:10]
            try:
                report_date = date.fromisoformat(raw_date)
            except ValueError:
                failed_codes.append(code)
                continue
            values = {
                column: _to_float(row.get(em_field))
                for em_field, column in field_map.items()
            }
            if not any(value is not None for value in values.values()):
                # 该接口对本股无监管数据（保险/信托常见）：如实跳过并计数，
                # 不写 NULL 覆盖既有值。
                continue
            records.append({
                "stock_code": code, "report_date": report_date, **values,
            })
            if progress_cb is not None:
                progress_cb(code, {"done": index + 1, "total": len(ordered)})
            if pause_seconds > 0:
                time.sleep(pause_seconds)

        written: dict[str, int] = {"rows_updated": 0, "coverage_rows": 0}
        if records:
            try:
                written = self._persist(records)
            except Exception as error:  # noqa: BLE001
                report.update({
                    "status": "failed",
                    "reason": f"persist_failed: {type(error).__name__}: {error}",
                    "targeted": len(ordered),
                    "failed": len(failed_codes),
                    "failed_codes": failed_codes[:20],
                    "elapsed_seconds": round(time.time() - started, 2),
                })
                return report

        missing = len(ordered) - len(records) - len(failed_codes) - deferred
        if not records:
            status = "failed" if failed_codes else "skipped"
        elif failed_codes or deferred:
            status = "partial"
        else:
            status = "success"
        report.update({
            "status": status,
            "reason": None if status in {"success", "skipped"} else "partial_source_failures",
            "targeted": len(ordered),
            "updated": len(records),
            "failed": len(failed_codes),
            "deferred": deferred,
            "skipped": max(0, missing),
            "rows_updated": written["rows_updated"],
            "coverage_rows": written["coverage_rows"],
            "failed_codes": failed_codes[:20],
            "elapsed_seconds": round(time.time() - started, 2),
        })
        if status == "skipped":
            report["reason"] = "no_regulatory_rows"
        logger.info(
            "监管指标补采: targeted=%d updated=%d failed=%d deferred=%d rows=%d elapsed=%.1fs",
            report["targeted"], report["updated"], report["failed"],
            report["deferred"], report["rows_updated"], report["elapsed_seconds"],
        )
        return report

    def _persist(self, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """按 (stock_code, report_date) UPDATE 既有三表行；不 INSERT、不 DELETE。

        Returns:
            {"rows_updated": 本轮写入的候选行数, "coverage_rows": 更新的非空覆盖行数}
            —— 前者是本轮动作量，后者是"资本充足率非空"的全库余额，两者含义
            不同，旧脚本只打印后者容易被读成"本轮更新了 8 千行"。
        """
        frame = pd.DataFrame(list(records), columns=list(REGULATORY_COLUMNS))
        frame["report_date"] = pd.to_datetime(frame["report_date"])
        value_columns = [column for column in REGULATORY_COLUMNS
                         if column not in {"stock_code", "report_date"}]
        with self.duck.transaction() as conn:
            conn.register("_reg_rows", frame)
            try:
                for column in value_columns:
                    conn.execute(
                        f"UPDATE balance_sheet AS t SET {column} = r.{column} "
                        "FROM _reg_rows AS r "
                        "WHERE t.stock_code = r.stock_code "
                        "AND t.report_date = r.report_date "
                        f"AND r.{column} IS NOT NULL",
                    )
                updated = int(conn.execute(
                    "SELECT COUNT(*) FROM balance_sheet "
                    "WHERE capital_adequacy_ratio IS NOT NULL"
                ).fetchone()[0])
                batch_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO fetch_batch
                       (batch_id, data_type, source, adapter_version, fetch_time,
                        raw_response_hash, row_count, report_date_range, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        batch_id, REGULATORY_METRICS, SOURCE,
                        "em-datacenter-mainfinadata",
                        datetime.now(UTC).replace(tzinfo=None), batch_id[:32],
                        len(frame),
                        f"{frame['report_date'].min().date()}~"
                        f"{frame['report_date'].max().date()}",
                        CONFIDENCE,
                    ],
                )
            finally:
                conn.unregister("_reg_rows")
        return {"rows_updated": len(frame), "coverage_rows": updated}
