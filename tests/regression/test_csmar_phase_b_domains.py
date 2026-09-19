"""CSMAR C17 Phase B 数据域回归（v24，2026-09-19）。

守住两件事：
1. schema v24 真的建出了 `cash_flow_indirect` 与 `financial_report_dates`
   两表，列名与冻结的语义一致（列名是 DS 与指标层的契约，改名即破坏）；
2. 两个域都带齐溯源列（source / fetch_time / batch_id / confidence），
   延续项目「每格数据有出生证明」的纪律。

数据层断言（间接法与直接法经营现金流一致性等）不放这里——测试库为空库，
数据级校验由 scripts/import_csmar_phase_b.py 的入库证据承担。
"""

from __future__ import annotations

from app.core.storage.duckdb_store import DuckDBStore

INDIRECT_COLUMNS = {
    "stock_code", "report_date", "report_type",
    "net_profit", "credit_impairment_loss", "unconfirmed_investment_loss",
    "asset_impairment_provision", "fixed_asset_depreciation",
    "investment_property_depreciation", "right_of_use_asset_depreciation",
    "intangible_asset_amortization", "long_term_prepaid_amortization",
    "disposal_long_term_asset_loss", "fixed_asset_scrap_loss",
    "fair_value_change_loss", "financial_expense", "investment_loss",
    "deferred_tax_asset_decrease", "deferred_tax_liability_increase",
    "inventory_decrease", "operating_receivable_decrease",
    "operating_payable_increase", "other_adjustment",
    "cf_from_operating_indirect", "debt_to_capital",
    "convertible_bond_due_within_1y", "finance_lease_fixed_assets",
    "cash_ending_balance", "cash_beginning_balance",
    "cash_equivalent_ending", "cash_equivalent_beginning",
    "cash_equivalent_net_increase",
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id", "raw_data",
}


def _columns(store: DuckDBStore, table: str) -> set[str]:
    return {
        row["column_name"]
        for row in store.read_query(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = ?", [table]
        )
    }


def test_schema_v24_version_and_migration_record(duckdb_store: DuckDBStore) -> None:
    from app.core.storage.schema import DUCKDB_SCHEMA_VERSION

    assert DUCKDB_SCHEMA_VERSION >= 24
    rows = duckdb_store.read_query("SELECT MAX(version) AS v FROM schema_migrations")
    assert rows[0]["v"] >= 24


def test_cash_flow_indirect_table_shape(duckdb_store: DuckDBStore) -> None:
    columns = _columns(duckdb_store, "cash_flow_indirect")
    missing = INDIRECT_COLUMNS - columns
    assert not missing, f"cash_flow_indirect 缺列: {sorted(missing)}"


def test_financial_report_dates_table_shape(duckdb_store: DuckDBStore) -> None:
    columns = _columns(duckdb_store, "financial_report_dates")
    assert {"stock_code", "report_date", "announce_date", "source",
            "fetch_time", "batch_id"} <= columns


def test_phase_b_table_primary_keys(duckdb_store: DuckDBStore) -> None:
    """主键是幂等 upsert 的依据，不得被改动。"""
    for table, expected in (
        ("cash_flow_indirect", {"stock_code", "report_date"}),
        ("financial_report_dates", {"stock_code", "report_date", "source"}),
    ):
        rows = duckdb_store.read_query(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
            [table],
        )
        assert rows, f"{table} 缺少主键"
        keys = {str(name) for name in rows[0]["constraint_column_names"]}
        assert keys == expected, f"{table} 主键应为 {expected}，实际 {keys}"


def test_indirect_method_has_depreciation_inputs(duckdb_store: DuckDBStore) -> None:
    """FCF / 杠杆（EBITDA/EBIT）依赖的折旧摊销三项必须在表内。"""
    columns = _columns(duckdb_store, "cash_flow_indirect")
    assert {
        "fixed_asset_depreciation",
        "intangible_asset_amortization",
        "long_term_prepaid_amortization",
    } <= columns


# ─── v28 / v29：员工人数历史域、金融行业专用科目域 ──────────────────

def test_company_employee_history_table_shape(duckdb_store: DuckDBStore) -> None:
    columns = _columns(duckdb_store, "company_employee_history")
    assert {"stock_code", "report_date", "employee_count", "source",
            "fetch_time", "batch_id"} <= columns


def test_financial_sector_items_table_shape(duckdb_store: DuckDBStore) -> None:
    """金融专用科目域：列名是行业研究接口的契约，改名即破坏。"""
    columns = _columns(duckdb_store, "financial_sector_items")
    assert {
        "stock_code", "report_date", "report_type",
        # 银行
        "cash_and_cb_balance", "due_from_banks", "loans_and_advances",
        "borrowing_from_cb", "deposits_and_interbank", "interbank_deposits",
        "customer_deposits", "interest_income", "interest_expense", "net_interest_income",
        # 保险
        "premiums_receivable", "insurance_contract_reserve", "policyholder_deposits",
        "earned_premiums", "claim_payments_net",
        # 证券
        "settlement_reserve", "customer_settlement_reserve", "margin_deposits_paid",
        "client_securities_deposits", "underwriting_securities", "fee_commission_income_net",
        # 其他金融
        "interbank_lending", "reverse_repo_assets", "interbank_borrowing", "repo_liabilities",
        "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
    } <= columns


def test_financial_sector_items_not_exposed_to_screening() -> None:
    """金融专用科目**不得**进入筛选字段表 —— 它们只对金融行业成立，
    放进全市场字段选择器会制造「98% 股票无数据」的伪条件。"""
    from app.core.screening.engine import EXTENDED_COLUMNS, NORMALIZED_FIELDS, SNAPSHOT_COLUMNS

    known = EXTENDED_COLUMNS | SNAPSHOT_COLUMNS | NORMALIZED_FIELDS
    for field in ("customer_deposits", "loans_and_advances", "net_interest_income",
                  "insurance_contract_reserve", "client_securities_deposits"):
        assert field not in known, f"{field} 不应出现在筛选字段表中"


def test_schema_v29_version() -> None:
    from app.core.storage.schema import DUCKDB_SCHEMA_VERSION

    assert DUCKDB_SCHEMA_VERSION >= 29


def test_csmar_per_share_history_table_shape(duckdb_store: DuckDBStore) -> None:
    columns = _columns(duckdb_store, "csmar_per_share_history")
    assert {
        "stock_code", "report_date", "bps", "bps_parent", "revenue_per_share",
        "ocf_per_share", "operating_profit_per_share", "ebit_per_share",
        "tangible_asset_per_share", "liability_per_share", "capital_reserve_per_share",
        "surplus_reserve_per_share", "undistributed_profit_per_share",
        "retained_earnings_per_share", "source", "fetch_time", "batch_id",
    } <= columns
