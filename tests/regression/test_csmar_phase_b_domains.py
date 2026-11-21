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

import pytest

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
    """金融专用科目域：列名是行业研究接口的契约，改名即破坏。

    2026-11-15 冷热分库纠偏：``financial_sector_items`` **不是静态冷库表** ——
    2025Q2+ 由 ``app/core/supplement.py`` 的 FINANCIAL_SECTOR 补采（eastmoney_f10）
    持续写入，冷库 Parquet 只含 CSMAR ≤2025-03-31 那 5,058 行。故它留在主库物理表
    （DDL 在 schema.py），本用例按热表正常测，不再 skip。
    """
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
    pytest.skip("冷库域，已迁 Parquet（config/cold_archive.yaml）")
    columns = _columns(duckdb_store, "csmar_per_share_history")
    assert {
        "stock_code", "report_date", "bps", "bps_parent", "revenue_per_share",
        "ocf_per_share", "operating_profit_per_share", "ebit_per_share",
        "tangible_asset_per_share", "liability_per_share", "capital_reserve_per_share",
        "surplus_reserve_per_share", "undistributed_profit_per_share",
        "retained_earnings_per_share", "source", "fetch_time", "batch_id",
    } <= columns


# ─── v33~v36：CSMAR 全量导入域（「榨干」收尾）───────────────────────

# 冷库清单（config/cold_archive.yaml）里**已物理切换**的 16 张静态归档表：2026-11-15
# 起主库只有 read_parquet 视图，schema.py 不再声明 DDL，测试库也不建物理表。
CSMAR_COLD_TABLES = (
    "csmar_fi_t1", "csmar_fi_t3", "csmar_fi_t4", "csmar_fi_t5",
    "csmar_fi_t6", "csmar_fi_t8", "csmar_fi_t9",   # fi_t7 已删（杠杆本项目已自算）
    "csmar_fi_t10", "csmar_fi_t11", "csmar_far_finidx",
    "csmar_balance_items", "csmar_income_items", "csmar_cashflow_items",
    "csmar_financial_items", "csmar_per_share_history",
    "csmar_disclosure_metrics",
    "csmar_risk_factors",   # 2026-11-15 第二批：风险治理因子域也迁冷库（17 张）
)
# 2026-11-15 第二批：`csmar_risk_factors` 也已完成物理切换（清单 16 → 17 张），
# DDL 已从 schema.py 删除，因此这里不再有"仍留主库"的 CSMAR 表。
CSMAR_HOT_TABLES: tuple[str, ...] = ()
# 注：csmar_aiq_annual（54 列全与 FI_T* 重叠）与 csmar_fi_t7（杠杆已自算）
# 已在「榨干纠偏」中连同 DDL 一起删除；冷库表于 2026-11-15 物理切换为视图。


def test_csmar_full_import_tables_exist(duckdb_store: DuckDBStore) -> None:
    """v33~v36 留在主库的 CSMAR 表必须存在且带溯源列。

    冷库 16 表不再由 schema.py 建表（改为 read_parquet 视图），由
    ``test_cold_archive_tables_are_not_declared_in_schema`` 反向守护。
    """
    from app.core.storage.schema import DUCKDB_SCHEMA_VERSION

    assert DUCKDB_SCHEMA_VERSION >= 36
    # 2026-11-15：CSMAR 归档域（17 张）已全部转为 Parquet 冷库视图，
    # 验收库里不再由 schema.py 建任何 csmar 物理表，故此处只做版本断言 +
    # 反向守护（见 test_cold_archive_tables_are_not_declared_in_schema）。
    assert CSMAR_HOT_TABLES == ()


def test_cold_archive_tables_are_not_declared_in_schema() -> None:
    """16 张冷库表不得再出现在 DUCKDB_SCHEMA_V1 的 CREATE TABLE 里。

    物理切换的硬前提：``init_duckdb_schema()`` 每次启动都跑，若这里仍有
    CREATE TABLE / CREATE INDEX / ALTER TABLE，重启会把表建回来或直接报错。
    """
    from app.core.storage.schema import DUCKDB_SCHEMA_V1

    for table in CSMAR_COLD_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in DUCKDB_SCHEMA_V1, (
            f"{table} 已迁 Parquet 冷库，schema.py 不应再建物理表"
        )
    assert "CREATE TABLE IF NOT EXISTS csmar_risk_factors" not in DUCKDB_SCHEMA_V1, (
        "csmar_risk_factors 已于 2026-11-15 迁冷库，schema.py 不应再建物理表"
    )
    # financial_sector_items 是**热表**（2025Q2+ 东财补采在写），必须留在主库
    # 物理表里 —— 它一旦被误判成冷库表，补采就写不进去、582 行热数据也会消失。
    assert "CREATE TABLE IF NOT EXISTS financial_sector_items" in DUCKDB_SCHEMA_V1, (
        "financial_sector_items 含 2025Q2+ 东财补采热数据，DDL 必须留在 schema.py"
    )
    # 2026-11-21：v50 起库里**不再有任何二级索引**（30 个全删，见
    # `test_schema_secondary_index_removal.py`）。表还在，但建表语句里不许再出现
    # 该索引 —— 反过来断言，防止有人把索引加回来。
    assert "idx_financial_sector_items_stock" not in DUCKDB_SCHEMA_V1


def test_accounting_identity_anchor_available(duckdb_store: DuckDBStore) -> None:
    """v36 补录「负债与所有者权益总计」，用于会计恒等式对账（资产 = 负债 + 权益）。"""
    pytest.skip("冷库域，已迁 Parquet（config/cold_archive.yaml）")
    columns = _columns(duckdb_store, "csmar_balance_items")
    assert "A004000000" in columns


def test_csmar_full_tables_not_exposed_to_screening() -> None:
    """全量归档域**不进筛选界面**：数据截止 2025Q1，且与自算指标大量重叠，
    放进字段选择器会制造「同概念多套口径」的伪选择。"""
    from app.core.screening.engine import EXTENDED_COLUMNS, NORMALIZED_FIELDS, SNAPSHOT_COLUMNS

    known = EXTENDED_COLUMNS | SNAPSHOT_COLUMNS | NORMALIZED_FIELDS
    for field in ("F050501B", "F090101B", "D000103000", "B005000000", "A001211000"):
        assert field not in known, f"{field} 不应出现在筛选字段表中"


def test_official_weighted_roe_is_imported(duckdb_store: DuckDBStore) -> None:
    """官方「加权平均ROE」（证监会披露口径）已入库 —— 本项目的 roe 是简单口径。"""
    pytest.skip("冷库域，已迁 Parquet（config/cold_archive.yaml）")
    columns = _columns(duckdb_store, "csmar_disclosure_metrics")
    assert {
        "roe_weighted", "roe_weighted_deducted", "non_recurring_gain_loss",
        "eps_basic", "eps_diluted", "eps_deducted_basic",
    } <= columns


def test_risk_factors_imported(duckdb_store: DuckDBStore) -> None:
    """风险治理因子域已于 2026-11-15 迁冷库（17 张），验收库不再建该表。

    列口径的守护改在冷库包上做：``test_cold_archive_switch`` 校验
    manifest 的 rows/columns/sha256；这里只断言主库不再声明它。
    """
    from app.core.storage.schema import DUCKDB_SCHEMA_V1

    assert "CREATE TABLE IF NOT EXISTS csmar_risk_factors" not in DUCKDB_SCHEMA_V1
    assert not _columns(duckdb_store, "csmar_risk_factors")


def test_csmar_redundant_cleanup_kept_only_primary_variants() -> None:
    """清理后：每概念只保留主口径，且不再有纯冗余表。

    背景：为追求 100% 导入曾把 223 个「同概念不同算法」的变体列一并入库，
    用户指出「无价值的冗余不应进库」。清理后 csmar_aiq_annual / csmar_fi_t7
    已删除，变体列裁剪到每概念一个主口径。
    """
    from app.core.storage.schema import DUCKDB_SCHEMA_V1

    assert "csmar_aiq_annual" not in DUCKDB_SCHEMA_V1, "AIQ 表 54 列全部与 FI_T* 重叠，应已删除"
    assert "csmar_fi_t7" not in DUCKDB_SCHEMA_V1, "杠杆表本项目已自算，应已删除"
    # 冷库表 2026-11-15 起不再建物理表（主库改 read_parquet 视图），17 张全部覆盖。
    for migrated in CSMAR_COLD_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {migrated}" not in DUCKDB_SCHEMA_V1
