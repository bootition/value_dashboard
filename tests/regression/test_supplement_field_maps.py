"""补采字段映射 + 保险股「保险合同负债」守卫的回归测试（2026-11-16，reports/131 §9.2）。

守住的底线
----------
**P0-3（FY2025 明细两表半空壳）**
1. 两张表落库的列名必须**真实存在**于库里（`information_schema.columns`）——
   防止再出现"映射表里写了个不存在的列"这种假字段事故；
2. `cash_flow_indirect` / `cash_flow_activity` 的每个数值列必须**要么有映射、
   要么在 `CASH_FLOW_STRUCTURAL_MISSING` 里显式登记**，不允许有"无声的 NULL"；
3. 一次真响应解析后，原先恒空的折旧摊销 / 营运资本 / 投资筹资科目必须写出值，
   且**只填不覆盖**既有非空值；
4. 没有 capex 的股票也要写活动表行（旧实现只在 capex 有值时才写）。

**P0-6（保险股保险合同负债写进 contract_liabilities）**
5. `sw_level2='保险Ⅱ'` 的股票，含「保险合同负债」的响应**不得**写进
   `balance_sheet.contract_liabilities`（解析层 + 写库层双重拦截）；
6. 同一份响应下非保险股照常写入（守卫不能误伤）；
7. 修复脚本：临时库 3 行（保险 2 / 非保险 1）→ dry-run 一行不动 →
   `--yes` 只清保险那 2 行，且非保险行、pre-CAS25 行、低占比行都不动。

全部使用临时 DuckDB/SQLite 夹具，网络一律 monkeypatch，**绝不触碰正式库**。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.core.supplement import (
    _ACTIVITY_VALUE_COLUMNS,
    _INDIRECT_VALUE_COLUMNS,
    ACTIVITY_COLUMNS,
    CASH_FLOW_STRUCTURAL_MISSING,
    CASHFLOW_SUPPLEMENT,
    CONTRACT_LIABILITIES_COLUMN,
    INDIRECT_COLUMNS,
    INSURANCE_CONTRACT_LIABILITY_COLUMN,
    INSURANCE_SW_LEVEL2,
    SPECS,
    SupplementFetcher,
    SupplementSpec,
    cash_flow_structural_missing_columns,
    guard_contract_liabilities,
    is_insurance_contract_liability_source,
    is_insurance_stock,
)

FETCH_TIME = datetime(2026, 11, 16, 12, 0, 0)
LATEST = "2026-09-30"
OLD = "2026-06-30"
#: 两表的非数值列（元数据）
_INDIRECT_META = {
    "stock_code", "report_date", "report_type", "source", "fetch_time",
    "raw_response_hash", "confidence", "batch_id", "raw_data",
}
_ACTIVITY_META = {
    "stock_code", "report_date", "report_type", "source", "fetch_time",
    "raw_response_hash", "confidence", "batch_id",
}


# ─── 夹具 ─────────────────────────────────────────────────────────────


def _seed_stock(
    duck,
    code: str,
    name: str,
    *,
    sw_level1: str | None = "非银金融",
    sw_level2: str | None = INSURANCE_SW_LEVEL2,
    csrc_l1: str | None = "金融业",
    exchange: str = "SSE",
) -> None:
    duck.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, listing_date, is_listed, is_st,
                is_suspended, sw_level1, sw_level2, csrc_l1, total_shares, circ_shares)
           VALUES (?, ?, ?, '2010-01-05', TRUE, FALSE, FALSE, ?, ?, ?, 100, 80)""",
        [code, name, exchange, sw_level1, sw_level2, csrc_l1],
    )


def _seed_trio(duck, code: str, report_date: str = LATEST) -> None:
    """主链三表各一行（`pending_codes` 的股票池要 JOIN income_statement）。"""
    duck.write_query(
        """INSERT INTO income_statement (stock_code, report_date, report_type, revenue)
           VALUES (?, CAST(? AS DATE), 'quarterly', 1000)""",
        [code, report_date],
    )
    duck.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets,
                                     total_liabilities, total_equity)
           VALUES (?, CAST(? AS DATE), 5000, 2000, 3000)""",
        [code, report_date],
    )
    duck.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES (?, CAST(? AS DATE), 100)""",
        [code, report_date],
    )


def _seed_csmar_indirect(duck, code: str, report_date: str, *, net_profit: float) -> None:
    """CSMAR 覆盖期的间接法行（`period_floor` 保护范围内，本源绝不能改写它）。"""
    duck.write_query(
        """INSERT INTO cash_flow_indirect
               (stock_code, report_date, report_type, net_profit,
                source, fetch_time, raw_response_hash, confidence, batch_id)
           VALUES (?, CAST(? AS DATE), 'annual', ?,
                   'csmar', TIMESTAMP '2026-09-20 00:00:00', 'h', 'strict', 'b')""",
        [code, report_date, net_profit],
    )


def _table_columns(duck, table: str) -> set[str]:
    rows = duck.read_query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [table],
    )
    return {str(row["column_name"]) for row in rows}


# ─── P0-3：字段映射表 ↔ 真实表结构 ───────────────────────────────────


def test_every_spec_column_exists_in_database(duckdb_store) -> None:
    """任何 spec 声明的列都必须真实存在（防"映射到假字段"再次上线）。

    2026-11-16 之前 `INDIRECT_COLUMNS` 只列了 10 个已映射列，其余 19 列
    （净利润、财务费用、营运资本三项、现金及等价物期初期末…）永远写 NULL，
    却没有任何地方声明"这些列不采"。本用例把「列名真实性」钉死。
    """
    problems: list[str] = []
    for key, spec in SPECS.items():
        for table, columns in spec.columns.items():
            existing = _table_columns(duckdb_store, table)
            assert existing, f"{key}: 目标表 {table} 不存在"
            for column in columns:
                if column not in existing:
                    problems.append(f"{key} → {table}.{column}")
    assert not problems, f"spec 声明了库里不存在的列：{problems}"


def test_field_map_targets_are_declared_columns(duckdb_store) -> None:
    """field_map 的目标列必须出现在该 spec 的落库列集合里（否则写了也会被丢）。"""
    problems: list[str] = []
    for key, spec in SPECS.items():
        declared: set[str] = set()
        for columns in spec.columns.values():
            declared |= set(columns)
        maps = [spec.field_map]
        if spec.org_field_maps:
            maps.extend(spec.org_field_maps.values())
        for mapping in maps:
            for column in mapping.values():
                if column not in declared:
                    problems.append(f"{key}: {column}")
        if spec.org_derived_sums:
            for derived in spec.org_derived_sums.values():
                for column in derived:
                    if column not in declared:
                        problems.append(f"{key}: derived {column}")
    assert not problems, f"field_map 指向了未声明落库的列：{sorted(set(problems))}"


def test_cash_flow_columns_are_mapped_or_explicitly_structural_missing() -> None:
    """每个数值列要么有映射，要么在结构性缺失清单里 —— 不留"无声的 NULL"。"""
    spec = SPECS[CASHFLOW_SUPPLEMENT]
    for table, value_columns in (
        ("cash_flow_indirect", _INDIRECT_VALUE_COLUMNS),
        ("cash_flow_activity", _ACTIVITY_VALUE_COLUMNS),
    ):
        mapped = {column for column in spec.field_map.values()}
        for mapped_by_org in (spec.org_field_maps or {}).values():
            mapped |= set(mapped_by_org.values())
        for derived in (spec.org_derived_sums or {}).values():
            mapped |= set(derived)
        structural: set[str] = set()
        for org_type in (None, "银行", "保险", "证券", "通用"):
            structural |= set(
                cash_flow_structural_missing_columns(table, org_type=org_type)
            )
        unmapped = [
            column for column in value_columns
            if column not in mapped and column not in structural
        ]
        assert not unmapped, (
            f"{table} 有既没映射、也没登记结构性缺失的列：{unmapped}"
            "（这会让它们静默恒 NULL，正是 reports/131 P0-3 的形态）"
        )


def test_structural_missing_registry_is_documented_and_narrow() -> None:
    """结构性缺失清单本身要可核对：只登记确定无解的列，且每条都有原因。"""
    assert set(CASH_FLOW_STRUCTURAL_MISSING) == {
        "cash_flow_indirect", "cash_flow_activity",
    }
    # 东财四套模板都没有「未确认的投资损失」
    assert "unconfirmed_investment_loss" in CASH_FLOW_STRUCTURAL_MISSING["cash_flow_indirect"]
    # 汇率变动与 other_cash_effect 口径不一致（银行股实测不等值），拒绝映射
    assert "other_cash_effect" in CASH_FLOW_STRUCTURAL_MISSING["cash_flow_activity"]
    # 通用/证券模板确实没有独立的「信用减值损失」行
    assert "credit_impairment_loss" in cash_flow_structural_missing_columns(
        "cash_flow_indirect", org_type="通用",
    )
    # 但银行/保险模板有 CREDIT_IMPAIRMENT_INCOME → 不该被登记成结构性缺失
    assert "credit_impairment_loss" not in cash_flow_structural_missing_columns(
        "cash_flow_indirect", org_type="保险",
    )
    for table, reasons in CASH_FLOW_STRUCTURAL_MISSING.items():
        for column, reason in reasons.items():
            assert column, table
            assert len(reason) >= 20, f"{table}.{column} 的原因未写明依据：{reason!r}"


def test_value_column_tuples_match_spec_columns() -> None:
    """`_INDIRECT_VALUE_COLUMNS` / `_ACTIVITY_VALUE_COLUMNS` 与落库列集合一致。"""
    spec = SPECS[CASHFLOW_SUPPLEMENT]
    assert set(spec.columns["cash_flow_indirect"]) == set(INDIRECT_COLUMNS)
    assert set(spec.columns["cash_flow_activity"]) == set(ACTIVITY_COLUMNS)
    assert set(INDIRECT_COLUMNS) == set(_INDIRECT_VALUE_COLUMNS) | _INDIRECT_META
    assert set(ACTIVITY_COLUMNS) == set(_ACTIVITY_VALUE_COLUMNS) | _ACTIVITY_META


# ─── P0-3：真实响应 → 解析结果 ────────────────────────────────────────

#: 一只制造业股票的东财 `xjllbAjaxNew` 行（字段名 2026-11-16 实测，值取自 600519 量级）
_EM_ROW_GENERAL = {
    "REPORT_DATE": f"{LATEST} 00:00:00",
    "ORG_TYPE": "通用",
    "NETPROFIT": 85_310_324_833.67,
    "ASSET_IMPAIRMENT": -1_200_000_000.0,
    "FA_IR_DEPR": 1_893_338_311.91,
    "IR_DEPR": 12_000_000.0,
    "USERIGHT_ASSET_AMORTIZE": 55_797_324.89,
    "IA_AMORTIZE": 289_613_682.99,
    "LPE_AMORTIZE": 20_637_734.49,
    "DISPOSAL_LONGASSET_LOSS": -3_106_625.19,
    "FA_SCRAP_LOSS": 5_491_211.47,
    "FAIRVALUE_CHANGE_LOSS": -76_162_597.66,
    "FINANCE_EXPENSE": 28_734_948.29,
    "INVEST_LOSS": -561_176.14,
    "DT_ASSET_REDUCE": -1_082_462_282.39,
    "DT_LIAB_ADD": -27_759_411.59,
    "INVENTORY_REDUCE": -7_084_136_638.71,
    "OPERATE_RECE_REDUCE": -10_983_881_858.27,
    "OPERATE_PAYABLE_ADD": -6_806_428_089.04,
    "OTHER": None,
    "NETCASH_OPERATE": 61_522_204_989.35,
    "DEBT_TRANSFER_CAPITAL": None,
    "CONVERT_BOND_1YEAR": None,
    "FINLEASE_OBTAIN_FA": None,
    "END_CASH": 117_952_629_447.72,
    "BEGIN_CASH": 164_297_949_257.83,
    "END_CASH_EQUIVALENTS": 8_472_980_000.0,
    "BEGIN_CASH_EQUIVALENTS": 5_672_140_000.0,
    "CCE_ADD": -43_544_479_810.11,
    "CONSTRUCT_LONG_ASSET": 3_127_594_916.41,
    "WITHDRAW_INVEST": 33_455_136_000.0,
    "RECEIVE_INVEST_INCOME": 111_683_655.9,
    "DISPOSAL_LONG_ASSET": 155_949.5,
    "RECEIVE_OTHER_INVEST": 24_750_303.39,
    "TOTAL_INVEST_INFLOW": 33_591_725_908.79,
    "INVEST_PAY_CASH": 62_097_000_000.0,
    "PAY_OTHER_INVEST": 9_029_941.27,
    "TOTAL_INVEST_OUTFLOW": 65_233_624_857.68,
    "ACCEPT_INVEST_CASH": 2_000_000.0,
    "RECEIVE_LOAN_CASH": 650_000_000.0,
    "ISSUE_BOND": 35_000_000_000.0,
    "RECEIVE_OTHER_FINANCE": 409_433_728.3,
    "TOTAL_FINANCE_INFLOW": 45_000_000_000.0,
    "PAY_DEBT_CASH": 351_380_000.0,
    "ASSIGN_DIVIDEND_PORFIT": 67_241_733_345.45,
    "PAY_OTHER_FINANCE": 6_185_347_863.42,
    "TOTAL_FINANCE_OUTFLOW": 73_427_081_208.87,
    "RATE_CHANGE_EFFECT": 2_295_358.3,
}


def _parse(duck, code: str, rows: list[dict], *, spec: SupplementSpec | None = None):
    fetcher = SupplementFetcher(duck=duck)
    return fetcher._parse_rows(
        spec or SPECS[CASHFLOW_SUPPLEMENT], code, rows,
        batch_id="test", fetched_at=FETCH_TIME, src_hash="test",
    )


def test_parse_fills_previously_null_cash_flow_columns(duckdb_store) -> None:
    """P0-3 核心：原恒空的列必须解析出值（FY2025 年报半空壳的直接回归）。"""
    parsed = _parse(duckdb_store, "600519", [dict(_EM_ROW_GENERAL)])
    indirect = parsed["cash_flow_indirect"][0]
    activity = parsed["cash_flow_activity"][0]

    expected_indirect = {
        "net_profit": 85_310_324_833.67,
        "fixed_asset_depreciation": 1_893_338_311.91,
        "investment_property_depreciation": 12_000_000.0,
        "right_of_use_asset_depreciation": 55_797_324.89,
        "intangible_asset_amortization": 289_613_682.99,
        "long_term_prepaid_amortization": 20_637_734.49,
        "asset_impairment_provision": -1_200_000_000.0,
        "fixed_asset_scrap_loss": 5_491_211.47,
        "fair_value_change_loss": -76_162_597.66,
        "financial_expense": 28_734_948.29,
        "investment_loss": -561_176.14,
        "deferred_tax_asset_decrease": -1_082_462_282.39,
        "deferred_tax_liability_increase": -27_759_411.59,
        "inventory_decrease": -7_084_136_638.71,
        "operating_receivable_decrease": -10_983_881_858.27,
        "operating_payable_increase": -6_806_428_089.04,
        "cf_from_operating_indirect": 61_522_204_989.35,
        "cash_ending_balance": 117_952_629_447.72,
        "cash_beginning_balance": 164_297_949_257.83,
        "cash_equivalent_ending": 8_472_980_000.0,
        "cash_equivalent_beginning": 5_672_140_000.0,
        "cash_equivalent_net_increase": -43_544_479_810.11,
    }
    for column, value in expected_indirect.items():
        assert indirect[column] == pytest.approx(value), column

    expected_activity = {
        "capex": 3_127_594_916.41,
        "investment_recovered": 33_455_136_000.0,
        "investment_income_cash": 111_683_655.9,
        "disposal_long_asset_cash": 155_949.5,
        "other_investing_inflow": 24_750_303.39,
        "investing_inflow_total": 33_591_725_908.79,
        "investment_paid": 62_097_000_000.0,
        "other_investing_outflow": 9_029_941.27,
        "investing_outflow_total": 65_233_624_857.68,
        "equity_investment_received": 2_000_000.0,
        "borrow_received": 650_000_000.0,
        "bond_issued": 35_000_000_000.0,
        "other_financing_inflow": 409_433_728.3,
        "financing_inflow_total": 45_000_000_000.0,
        "debt_repaid": 351_380_000.0,
        "dividend_interest_paid": 67_241_733_345.45,
        "other_financing_outflow": 6_185_347_863.42,
        "financing_outflow_total": 73_427_081_208.87,
    }
    for column, value in expected_activity.items():
        assert activity[column] == pytest.approx(value), column
    # 源里没有对应科目的列如实为 NULL（不猜、不填 0）
    assert indirect["unconfirmed_investment_loss"] is None
    assert indirect["other_adjustment"] is None
    # 汇率变动 ≠ other_cash_effect（口径不一致，拒绝映射）
    assert activity["other_cash_effect"] is None


def test_parse_uses_bank_template_overrides(duckdb_store) -> None:
    """银行/保险模板的折旧/减值/递延所得税字段名与通用模板不同，必须按行业取值。"""
    row = {
        "REPORT_DATE": f"{LATEST} 00:00:00",
        "ORG_TYPE": "银行",
        "NETPROFIT": 149_559_000_000.0,
        # 银行模板：FA_IR_DEPR 是「固定资产折旧」；FIXED_ASSET_DEPR 为空
        "FA_IR_DEPR": 10_803_000_000.0,
        "FIXED_ASSET_DEPR": None,
        "ASSET_IMPAIRMENT": 40_819_000_000.0,      # 不是 asset_impairment_provision
        "OTHER_ASSET_IMPAIRMENT": -2_789_000_000.0,
        "DEFER_TAX": 164_000_000.0,
        "DT_ASSET_REDUCE": None,
        "NETCASH_OPERATE": 447_023_000_000.0,
        "CONSTRUCT_LONG_ASSET": 10_000_000.0,
    }
    parsed = _parse(duckdb_store, "600036", [row])
    indirect = parsed["cash_flow_indirect"][0]
    assert indirect["fixed_asset_depreciation"] == pytest.approx(10_803_000_000.0)
    assert indirect["asset_impairment_provision"] == pytest.approx(-2_789_000_000.0)
    assert indirect["deferred_tax_asset_decrease"] == pytest.approx(164_000_000.0)


def test_parse_prefers_specialised_template_field_for_insurer(duckdb_store) -> None:
    """保险模板里 `FA_IR_DEPR` 是合计、`FIXED_ASSET_DEPR` 才是本列语义 → 取后者。"""
    row = {
        "REPORT_DATE": f"{LATEST} 00:00:00",
        "ORG_TYPE": "保险",
        "NETPROFIT": 146_733_000_000.0,
        "FA_IR_DEPR": 9_860_000_000.0,        # 固定资产折旧 + 投资性房地产折旧
        "FIXED_ASSET_DEPR": 4_555_000_000.0,  # CSMAR fixed_asset_depreciation
        "USERIGHT_ASSET_AMORTIZE": 4_930_000_000.0,
        "DEFER_TAX": -1_535_000_000.0,
        "NETCASH_OPERATE": 382_474_000_000.0,
        "CONSTRUCT_LONG_ASSET": 1_000_000.0,
    }
    parsed = _parse(duckdb_store, "601318", [row])
    indirect = parsed["cash_flow_indirect"][0]
    assert indirect["fixed_asset_depreciation"] == pytest.approx(4_555_000_000.0)
    assert indirect["deferred_tax_asset_decrease"] == pytest.approx(-1_535_000_000.0)


def test_parse_writes_activity_row_without_capex(duckdb_store) -> None:
    """旧实现只在 capex 有值时才写活动表 → 没有资本支出的股票整行丢失。"""
    row = {
        "REPORT_DATE": f"{LATEST} 00:00:00",
        "ORG_TYPE": "通用",
        "NETCASH_OPERATE": 1.0,
        "CONSTRUCT_LONG_ASSET": None,
        "PAY_DEBT_CASH": 7_000_000.0,
    }
    parsed = _parse(duckdb_store, "000002", [row])
    activity = parsed["cash_flow_activity"]
    assert len(activity) == 1
    assert activity[0]["capex"] is None
    assert activity[0]["debt_repaid"] == pytest.approx(7_000_000.0)


def test_refresh_keeps_csmar_rows_and_only_writes_own_source(
    duckdb_store, monkeypatch,
) -> None:
    """只填不覆盖的纪律：CSMAR 期的行原样保留，本源只在自己的行上写。

    `cash_flow_*` 的主键是 (stock_code, report_date)，且 `period_floor` 保证
    本源永不碰 CSMAR 覆盖期 —— 这里把两条一起钉住：CSMAR 行的值一个字节不动，
    本源行按股票先删同源再插（幂等），且新写的列是本次补齐的全部科目。
    CSMAR 期的数值由 `_seed_csmar_indirect()` 写入（`net_profit = 999`）。
    """
    _seed_stock(
        duckdb_store, "600519", "贵州茅台",
        sw_level1="食品饮料", sw_level2="白酒", csrc_l1="制造业",
    )
    _seed_trio(duckdb_store, "600519")
    _seed_csmar_indirect(duckdb_store, "600519", "2025-03-31", net_profit=999.0)
    fetcher = SupplementFetcher(duck=duckdb_store)

    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        lambda self, spec, code, exchange, dates, **kwargs: [dict(_EM_ROW_GENERAL)],
    )
    report = fetcher.refresh(CASHFLOW_SUPPLEMENT, periods=[LATEST], deadline_seconds=30)
    assert report["status"] == "success", report

    csmar_rows = duckdb_store.read_query(
        """SELECT net_profit, source FROM cash_flow_indirect
           WHERE stock_code = '600519' AND report_date = DATE '2025-03-31'"""
    )
    assert len(csmar_rows) == 1
    assert csmar_rows[0]["source"] == "csmar"
    assert csmar_rows[0]["net_profit"] == 999, "CSMAR 期覆盖的数值绝不能被改写"

    own_rows = duckdb_store.read_query(
        """SELECT net_profit, operating_payable_increase, source
           FROM cash_flow_indirect
           WHERE stock_code = '600519' AND report_date = CAST(? AS DATE)""",
        [LATEST],
    )
    assert len(own_rows) == 1, "本源行不能重复插入（主键 1 行）"
    assert own_rows[0]["source"] == "eastmoney_f10"
    assert own_rows[0]["net_profit"] == pytest.approx(85_310_324_833.67)
    assert own_rows[0]["operating_payable_increase"] == pytest.approx(-6_806_428_089.04)


def test_refresh_rewrites_own_stale_source_row(duckdb_store, monkeypatch) -> None:
    """上一轮的「半空壳」本源行会被本轮整行重写（幂等），旧同源行不残留。

    `delete_scope='all'`：按股票删掉**全部** `eastmoney_f10` 行再插本轮结果。
    这条纪律保证"半空壳"不会因为主键冲突而留在库里；代价是抓取窗口之外的
    旧本源行会一并消失（窗口 = 最近 `backfill_periods` 个季末）。
    """
    _seed_stock(
        duckdb_store, "600519", "贵州茅台",
        sw_level1="食品饮料", sw_level2="白酒", csrc_l1="制造业",
    )
    _seed_trio(duckdb_store, "600519")
    # 上一轮本源行：只有 capex，其余列全空（正是 P0-3 的"半空壳"形态）
    duckdb_store.write_query(
        """INSERT INTO cash_flow_activity
               (stock_code, report_date, report_type, capex, source, fetch_time,
                raw_response_hash, confidence, batch_id)
           VALUES ('600519', CAST(? AS DATE), 'quarterly', 1, 'eastmoney_f10',
                   TIMESTAMP '2026-09-20 00:00:00', 'h', 'strict', 'b')""",
        [OLD],
    )
    fetcher = SupplementFetcher(duck=duckdb_store)
    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        lambda self, spec, code, exchange, dates, **kwargs: [dict(_EM_ROW_GENERAL)],
    )
    report = fetcher.refresh(CASHFLOW_SUPPLEMENT, periods=[LATEST], deadline_seconds=30)
    assert report["status"] == "success", report

    stale = duckdb_store.read_query(
        """SELECT COUNT(*) AS n FROM cash_flow_activity
           WHERE stock_code = '600519' AND source = 'eastmoney_f10'
             AND report_date <> CAST(? AS DATE)""",
        [LATEST],
    )
    assert stale[0]["n"] == 0, "旧同源行必须被删掉（否则与新行并存，主键冲突/脏数据）"
    rows = duckdb_store.read_query(
        """SELECT capex, dividend_interest_paid, investing_inflow_total
           FROM cash_flow_activity
           WHERE stock_code = '600519' AND report_date = CAST(? AS DATE)""",
        [LATEST],
    )
    assert len(rows) == 1, "同源同主键只能有一行"
    assert rows[0]["capex"] == pytest.approx(3_127_594_916.41)
    assert rows[0]["dividend_interest_paid"] == pytest.approx(67_241_733_345.45)
    assert rows[0]["investing_inflow_total"] == pytest.approx(33_591_725_908.79)


# ─── P0-6：保险股守卫 ─────────────────────────────────────────────────


def test_is_insurance_stock_predicates() -> None:
    assert is_insurance_stock(sw_level1="非银金融", sw_level2=INSURANCE_SW_LEVEL2)
    assert is_insurance_stock(org_type="保险")
    assert is_insurance_stock(name="中国平安保险(集团)股份有限公司")
    # 券商/多元金融不是保险股
    assert not is_insurance_stock(sw_level1="非银金融", sw_level2="证券Ⅱ", name="中信证券")
    assert not is_insurance_stock(sw_level1="非银金融", sw_level2="多元金融", name="中航产融")


def test_insurance_contract_liability_source_detection() -> None:
    for token in ("保险合同负债", "保险合同负债合计", "INSURANCE_CONTRACT_LIAB",
                  "insurance_contract_liability"):
        assert is_insurance_contract_liability_source(token), token
    for token in ("合同负债", "CONTRACT_LIAB", "预收款项", "", None):
        assert not is_insurance_contract_liability_source(token), token


def test_guard_rejects_insurer_contract_liability() -> None:
    """纯函数层：保险股 + 保险合同负债来源 → 从 contract_liabilities 摘除。"""
    result = guard_contract_liabilities(
        {CONTRACT_LIABILITIES_COLUMN: 5.36091e12},
        source_fields={CONTRACT_LIABILITIES_COLUMN: "保险合同负债"},
        is_insurance=True,
        available_columns=None,
    )
    assert CONTRACT_LIABILITIES_COLUMN not in result.values
    assert result.rejected_columns == (CONTRACT_LIABILITIES_COLUMN,)
    assert result.reasons[CONTRACT_LIABILITIES_COLUMN]
    # 专用列没有 → 不改写，只拒绝
    assert not result.rerouted

    # 专用列已由主控加上 → 改写而不是丢弃
    rerouted = guard_contract_liabilities(
        {CONTRACT_LIABILITIES_COLUMN: 5.36091e12},
        source_fields={CONTRACT_LIABILITIES_COLUMN: "保险合同负债"},
        is_insurance=True,
        available_columns={INSURANCE_CONTRACT_LIABILITY_COLUMN},
    )
    assert CONTRACT_LIABILITIES_COLUMN not in rerouted.values
    assert rerouted.values[INSURANCE_CONTRACT_LIABILITY_COLUMN] == pytest.approx(5.36091e12)

    # 保险股但值真的来自「合同负债」→ 放行（不能误伤）
    genuine = guard_contract_liabilities(
        {CONTRACT_LIABILITIES_COLUMN: 800_000_000.0},
        source_fields={CONTRACT_LIABILITIES_COLUMN: "CONTRACT_LIAB"},
        is_insurance=True,
    )
    assert genuine.values[CONTRACT_LIABILITIES_COLUMN] == pytest.approx(800_000_000.0)

    # 非保险股：即便是「保险合同负债」字段也照旧（不归本守卫管）
    other = guard_contract_liabilities(
        {CONTRACT_LIABILITIES_COLUMN: 1.0},
        source_fields={CONTRACT_LIABILITIES_COLUMN: "保险合同负债"},
        is_insurance=False,
    )
    assert other.values[CONTRACT_LIABILITIES_COLUMN] == pytest.approx(1.0)


def _contract_liability_spec() -> SupplementSpec:
    """模拟上游误映射：把「保险合同负债」直接对到通用列 `contract_liabilities`。"""
    return SupplementSpec(
        key="insurance_contract_liability_probe",
        data_type="insurance_contract_liability_probe",
        endpoint="zcfzbAjaxNew",
        api_version="test/1",
        field_map={"保险合同负债": CONTRACT_LIABILITIES_COLUMN},
        columns={
            "balance_sheet": (
                "stock_code", "report_date", CONTRACT_LIABILITIES_COLUMN,
            ),
        },
        write_mode="update_existing",
        pending_mode="missing_field",
        judge_table="balance_sheet",
        period_floor=date(2025, 3, 31),
        fills_null_only=True,
    )


def test_insurer_contract_liability_is_not_written_to_contract_liabilities(
    duckdb_store,
) -> None:
    """P0-6 核心：`sw_level2='保险Ⅱ'` + 含保险合同负债的响应 → 合同负债保持 NULL。"""
    _seed_stock(duckdb_store, "601318", "中国平安")
    _seed_trio(duckdb_store, "601318", LATEST)
    spec = _contract_liability_spec()
    fetcher = SupplementFetcher(duck=duckdb_store)
    rows = [{
        "REPORT_DATE": f"{LATEST} 00:00:00",
        "ORG_TYPE": "保险",
        "保险合同负债": 5_360_910_000_000.0,
    }]

    parsed = fetcher._parse_rows(
        spec, "601318", rows, batch_id="t", fetched_at=FETCH_TIME, src_hash="t",
    )
    csv_rows = parsed["balance_sheet"]
    assert csv_rows, "解析层不该把整行丢掉（否则守卫变成静默失败）"
    assert csv_rows[0].get(CONTRACT_LIABILITIES_COLUMN) is None, (
        "解析层必须拦住保险股的保险合同负债"
    )
    assert fetcher.guard_rejections, "拦截必须留下可核对的记录"
    assert fetcher.guard_rejections[0]["reason_code"] == "SOURCE_EMPTY"

    # 即便绕过解析层（模拟别处写入），写库层也必须拦住
    rejections_before = len(fetcher.guard_rejections)
    fetcher._persist_rows(
        spec,
        {"balance_sheet": [{
            "stock_code": "601318",
            "report_date": date(2026, 9, 30),
            CONTRACT_LIABILITIES_COLUMN: 5_360_910_000_000.0,
        }]},
        batch_id="t", fetched_at=FETCH_TIME, src_hash="t", periods=[LATEST],
    )
    assert len(fetcher.guard_rejections) > rejections_before, (
        "写库层守卫必须真的触发并留痕（否则只是恰好没写）"
    )
    written = duckdb_store.read_query(
        """SELECT contract_liabilities FROM balance_sheet
           WHERE stock_code = '601318' AND report_date = CAST(? AS DATE)""",
        [LATEST],
    )
    assert written[0]["contract_liabilities"] is None


def test_non_insurer_is_not_affected_by_guard(duckdb_store) -> None:
    """同一份响应下，非保险股照常写入（守卫不得误伤）。"""
    _seed_stock(
        duckdb_store, "600519", "贵州茅台",
        sw_level1="食品饮料", sw_level2="白酒", csrc_l1="制造业",
    )
    _seed_trio(duckdb_store, "600519", LATEST)
    spec = _contract_liability_spec()
    fetcher = SupplementFetcher(duck=duckdb_store)
    rows = [{"REPORT_DATE": f"{LATEST} 00:00:00", "保险合同负债": 8_006_739_780.94}]

    parsed = fetcher._parse_rows(
        spec, "600519", rows, batch_id="t", fetched_at=FETCH_TIME, src_hash="t",
    )
    assert parsed["balance_sheet"][0][CONTRACT_LIABILITIES_COLUMN] == pytest.approx(
        8_006_739_780.94, rel=0, abs=1e-6,
    )
    assert not fetcher.guard_rejections

    fetcher._persist_rows(
        spec, parsed, batch_id="t", fetched_at=FETCH_TIME, src_hash="t", periods=[LATEST],
    )
    written = duckdb_store.read_query(
        """SELECT contract_liabilities FROM balance_sheet
           WHERE stock_code = '600519' AND report_date = CAST(? AS DATE)""",
        [LATEST],
    )
    assert written[0]["contract_liabilities"] == pytest.approx(8_006_739_780.94)


def test_insurance_contract_liability_column_auto_enables_cas25_when_present(
    duckdb_store,
) -> None:
    """专用列存在时（schema v44 起）**自动启用** CAS25 映射。

    2026-11-17 翻转：原用例断言"列不存在 → 不启用"，其自带提示写着
    「若已加列，请改为断言反向行为」。schema v44 已加
    `financial_sector_items.insurance_contract_liability`（`reports/131` P0-6），
    所以现在钉的是另一半契约：**列在、映射必须真的接上**（不能永远关着），
    且不需要再改代码。
    """
    from app.core.supplement import FINANCIAL_SECTOR

    fetcher = SupplementFetcher(duck=duckdb_store)
    available = fetcher._available_columns(SPECS[FINANCIAL_SECTOR].columns)
    assert INSURANCE_CONTRACT_LIABILITY_COLUMN in (
        available.get("financial_sector_items") or set()
    ), "schema v44 应已加该列；若被移除，请把本用例翻回'不启用'分支"

    adapted = fetcher._adapt_spec_to_schema(SPECS[FINANCIAL_SECTOR], available)
    insurer_map = (adapted.org_field_maps or {}).get("保险") or {}
    assert INSURANCE_CONTRACT_LIABILITY_COLUMN in insurer_map.values()
    assert INSURANCE_CONTRACT_LIABILITY_COLUMN in adapted.columns["financial_sector_items"]
    # 幂等：再适配一次不应重复叠加
    twice = fetcher._adapt_spec_to_schema(adapted, available)
    assert twice is adapted


def test_adapt_spec_enables_cas25_mapping_once_column_exists(duckdb_store) -> None:
    """主控加列后，代码无需再改就该自动把「保险合同负债」写到专用列（前向兼容）。"""
    from app.core.supplement import FINANCIAL_SECTOR

    spec = SPECS[FINANCIAL_SECTOR]
    table = "financial_sector_items"
    duckdb_store.execute_script(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
        f"{INSURANCE_CONTRACT_LIABILITY_COLUMN} DOUBLE"
    )
    fetcher = SupplementFetcher(duck=duckdb_store)
    available = fetcher._available_columns(spec.columns)
    assert INSURANCE_CONTRACT_LIABILITY_COLUMN in available[table]

    adapted = fetcher._adapt_spec_to_schema(spec, available)
    insurer_map = (adapted.org_field_maps or {}).get("保险") or {}
    assert insurer_map["保险合同负债"] == INSURANCE_CONTRACT_LIABILITY_COLUMN
    assert INSURANCE_CONTRACT_LIABILITY_COLUMN in adapted.columns[table]
    # 幂等：再适配一次不重复加列
    again = fetcher._adapt_spec_to_schema(adapted, available)
    assert again.columns[table].count(INSURANCE_CONTRACT_LIABILITY_COLUMN) == 1
    # 原始 SPECS 不能被就地改动（refresh 每轮都从 SPECS 出发）
    assert INSURANCE_CONTRACT_LIABILITY_COLUMN not in spec.columns[table]


# ─── P0-6：一次性修复脚本 ─────────────────────────────────────────────


def _seed_repair_fixture(duck) -> None:
    """保险 2 行 + 非保险 1 行 + 一行「保险股但早于 CAS25」。"""
    _seed_stock(duck, "601318", "中国平安")
    _seed_stock(duck, "601628", "中国人寿")
    _seed_stock(
        duck, "600519", "贵州茅台",
        sw_level1="食品饮料", sw_level2="白酒", csrc_l1="制造业",
    )
    _seed_trio(duck, "601318", "2025-12-31")
    _seed_trio(duck, "601628", "2025-12-31")
    _seed_trio(duck, "600519", "2025-12-31")
    duck.write_query(
        """UPDATE balance_sheet SET contract_liabilities = 5360910000000,
                                   total_liabilities = 11653115000000
           WHERE stock_code = '601318'"""
    )
    duck.write_query(
        """UPDATE balance_sheet SET contract_liabilities = 5825026000000,
                                   total_liabilities = 6248298000000
           WHERE stock_code = '601628'"""
    )
    duck.write_query(
        """UPDATE balance_sheet SET contract_liabilities = 8006739780.94,
                                   total_liabilities = 49875590112.37
           WHERE stock_code = '600519'"""
    )
    # 保险股、早于 CAS25 → 旧准则口径的真实合同负债，必须保留
    duck.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets,
                                      total_liabilities, total_equity, contract_liabilities)
           VALUES ('601318', DATE '2013-12-31', 600000000000, 537217000000,
                   60000000000, 49156000000)"""
    )


def test_repair_script_dry_run_then_yes(duckdb_store, database_paths) -> None:
    """dry-run 一行不动；--yes 只清保险那 2 行，非保险行与 pre-CAS25 行保留。"""
    from scripts import repair_insurance_contract_liability as repair_script

    _seed_repair_fixture(duckdb_store)

    def _snapshot() -> dict[str, float | None]:
        rows = duckdb_store.read_query(
            """SELECT stock_code, CAST(report_date AS VARCHAR) AS d, contract_liabilities
               FROM balance_sheet ORDER BY stock_code, report_date"""
        )
        return {f"{row['stock_code']}@{row['d'][:10]}": row["contract_liabilities"]
                for row in rows}

    before = _snapshot()
    selected = repair_script.select_repair_rows(duckdb_store)
    # 判据命中：2 只保险股 FY2025（pre-CAS25 那行被 report_date 条件排除）
    assert {row["stock_code"] for row in selected} == {"601318", "601628"}
    assert len(selected) == 2

    dry = repair_script.repair(
        duckdb_store, yes=False, backup=False, lock_path=database_paths.duckdb_path,
    )
    assert dry["mode"] == "dry_run"
    assert dry["written"] == 0
    assert _snapshot() == before, "dry-run 绝不能写库"

    report = repair_script.repair(
        duckdb_store, yes=True, backup=False, lock_path=database_paths.duckdb_path,
    )
    assert report["written"] == 2
    assert report["remaining_after"] == 0
    assert report["scope_breach_rows"] == 0

    after = _snapshot()
    assert after["601318@2025-12-31"] is None
    assert after["601628@2025-12-31"] is None
    # 非保险股不受影响
    assert after["600519@2025-12-31"] == pytest.approx(8_006_739_780.94)
    # 保险股但早于 CAS25 的行也不受影响
    assert after["601318@2013-12-31"] == pytest.approx(49_156_000_000.0)


def test_repair_script_is_idempotent_and_scoped(duckdb_store, database_paths) -> None:
    """重复跑不再改动；`--codes` 能把范围收窄到指定股票。"""
    from scripts import repair_insurance_contract_liability as repair_script

    _seed_repair_fixture(duckdb_store)
    report = repair_script.repair(
        duckdb_store, yes=True, backup=False, lock_path=database_paths.duckdb_path,
    )
    assert report["written"] == 2
    again = repair_script.repair(
        duckdb_store, yes=True, backup=False, lock_path=database_paths.duckdb_path,
    )
    assert again["written"] == 0
    assert again["selected"]["rows"] == 0

    # 再塞一行，用 --codes 只处理 601628
    duckdb_store.write_query(
        """UPDATE balance_sheet SET contract_liabilities = 5825026000000
           WHERE stock_code = '601628'"""
    )
    duckdb_store.write_query(
        """UPDATE balance_sheet SET contract_liabilities = 5360910000000
           WHERE stock_code = '601318'"""
    )
    scoped = repair_script.repair(
        duckdb_store, yes=True, codes=["601628"], backup=False,
        lock_path=database_paths.duckdb_path,
    )
    assert scoped["written"] == 1
    left = duckdb_store.read_query(
        """SELECT stock_code, contract_liabilities FROM balance_sheet
           WHERE report_date = DATE '2025-12-31' ORDER BY stock_code"""
    )
    values = {row["stock_code"]: row["contract_liabilities"] for row in left}
    assert values["601628"] is None
    assert values["601318"] == pytest.approx(5_360_910_000_000.0)


def test_repair_script_sql_contains_the_self_proving_predicate() -> None:
    """脚本导出的 SQL 必须自带判据（供报告引用与人工复核）。"""
    from scripts import repair_insurance_contract_liability as repair_script

    select_sql = repair_script.SELECT_REPAIR_SQL
    assert INSURANCE_SW_LEVEL2 in select_sql
    assert repair_script.CAS25_START in select_sql
    assert str(repair_script.MIN_LIABILITY_SHARE) in select_sql
    assert f"{CONTRACT_LIABILITIES_COLUMN} IS NOT NULL" in select_sql
    update_sql = repair_script.UPDATE_REPAIR_SQL
    assert "SET contract_liabilities = NULL" in update_sql
    assert INSURANCE_SW_LEVEL2 in update_sql
    # 清理目标只该是 contract_liabilities，不该顺手写别的列
    assert update_sql.count("= NULL") == 1
    assert Path(repair_script.__file__).name == "repair_insurance_contract_liability.py"
