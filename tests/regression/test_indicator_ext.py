"""扩展指标域 indicator_ext 回归（schema v25，2026-09-19）。

用最小种子数据验证三族指标的**计算口径**，防止将来有人改动公式而无人察觉：

- 周转率族：累计损益 ÷ 期末余额（CSMAR FI_T4「A」式）
- EBIT / EBITDA：CSMAR FI_T5 明确定义的公式
- 杠杆族：CSMAR FI_T7 明确定义的公式
- 自由现金流：本项目口径（经营现金流净额 − 资本支出）
- 缺失输入 → 对应列为 NULL（不得估算或回退）
"""

from __future__ import annotations

from app.core.indicators.extended import ExtendedIndicatorBuilder
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet

STOCK = "600519"
REPORT_DATE = "2024-12-31"


def _seed(store: DuckDBStore) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue, cost_of_revenue, "
            "net_profit, income_tax, financial_expenses, total_profit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [STOCK, REPORT_DATE, 1000.0, 400.0, 300.0, 50.0, 20.0, 370.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, accounts_receivable, inventory, "
            "accounts_payable, total_current_assets, fixed_assets, total_assets, total_equity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [STOCK, REPORT_DATE, 100.0, 200.0, 50.0, 600.0, 400.0, 2000.0, 1200.0],
        )
        conn.execute(
            "INSERT INTO cash_flow (stock_code, report_date, cf_from_operating) VALUES (?, ?, ?)",
            [STOCK, REPORT_DATE, 380.0],
        )
        conn.execute(
            "INSERT INTO cash_flow_indirect (stock_code, report_date, fixed_asset_depreciation, "
            "intangible_asset_amortization, long_term_prepaid_amortization, source, fetch_time, "
            "raw_response_hash, confidence, batch_id) VALUES (?, ?, ?, ?, ?, 'csmar', ?, 'h', 'strict', 'b')",
            [STOCK, REPORT_DATE, 60.0, 10.0, 5.0, "2026-09-19 00:00:00"],
        )
        conn.execute(
            "INSERT INTO cash_flow_activity (stock_code, report_date, capex, source, fetch_time, "
            "raw_response_hash, confidence, batch_id) VALUES (?, ?, ?, 'csmar', ?, 'h', 'strict', 'b')",
            [STOCK, REPORT_DATE, 80.0, "2026-09-19 00:00:00"],
        )


def _row(store: DuckDBStore) -> dict:
    rows = store.read_query(
        "SELECT * FROM indicator_ext WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
        [STOCK, REPORT_DATE],
    )
    assert rows, "indicator_ext 未生成该股票行"
    return rows[0]


def test_extended_indicators_compute_expected_values(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    report = builder.build()
    assert report["status"] == "success"
    assert report["rows_after"] == 1

    row = _row(duckdb_store)
    # 周转率族：累计损益 ÷ 期末余额
    assert row["receivables_turnover"] == 10.0      # 1000 / 100
    assert row["inventory_turnover"] == 2.0         # 400 / 200
    assert row["accounts_payable_turnover"] == 8.0  # 400 / 50
    assert row["current_asset_turnover"] == 1000.0 / 600.0
    assert row["fixed_asset_turnover"] == 2.5       # 1000 / 400
    assert row["total_asset_turnover"] == 0.5       # 1000 / 2000
    assert row["equity_turnover"] == 1000.0 / 1200.0
    # 折旧摊销 = 60 + 10 + 5（CSMAR FI_T6.F061201B 三项口径）
    assert row["depreciation_amortization"] == 75.0
    # 自由现金流 = 经营现金流 380 − 资本支出 80
    assert row["free_cash_flow"] == 300.0
    assert row["fcf_margin"] == 0.3
    # EBIT = 300 + 50 + 20 = 370；EBITDA = 370 + 75 = 445
    assert row["ebit"] == 370.0
    assert row["ebitda"] == 445.0
    # 杠杆：EBIT/利润总额 = 1；EBITDA/EBIT = 445/370；EBITDA/利润总额 = 445/370
    assert row["leverage_financial"] == 1.0
    assert abs(row["leverage_operating"] - 445.0 / 370.0) < 1e-9
    assert abs(row["leverage_total"] - 445.0 / 370.0) < 1e-9


def test_missing_indirect_statement_yields_null_not_estimate(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """缺间接法时 EBITDA/杠杆必须 NULL，不得回退估算或补零。"""
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue, net_profit, "
            "income_tax, financial_expenses, total_profit) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["000001", REPORT_DATE, 500.0, 100.0, 20.0, 10.0, 130.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_equity) "
            "VALUES (?, ?, ?, ?)",
            ["000001", REPORT_DATE, 1000.0, 500.0],
        )
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '000001'")[0]
    assert row["total_asset_turnover"] == 0.5
    assert row["depreciation_amortization"] is None
    assert row["ebitda"] is None
    assert row["leverage_operating"] is None
    assert row["free_cash_flow"] is None


def test_zero_denominator_yields_null(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """分母为 0 时 MUST NULL，不得产生 inf。"""
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue, total_profit) "
            "VALUES (?, ?, ?, ?)",
            ["000002", REPORT_DATE, 100.0, 0.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, accounts_receivable, total_assets) "
            "VALUES (?, ?, ?, ?)",
            ["000002", REPORT_DATE, 0.0, 1000.0],
        )
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    row = duckdb_store.read_query("SELECT * FROM indicator_ext WHERE stock_code = '000002'")[0]
    assert row["receivables_turnover"] is None
    assert row["leverage_financial"] is None


def test_rebuild_is_idempotent(duckdb_store: DuckDBStore, database_paths: DatabasePathSet) -> None:
    _seed(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    first = builder.build()
    second = builder.build()
    assert first["rows_after"] == second["rows_after"]
    assert second["rows_after"] == 1


def test_scoped_rebuild_only_touches_given_codes(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    _seed(duckdb_store)
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO income_statement (stock_code, report_date, revenue) VALUES (?, ?, ?)",
            ["000003", REPORT_DATE, 200.0],
        )
        conn.execute(
            "INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES (?, ?, ?)",
            ["000003", REPORT_DATE, 400.0],
        )
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    assert len(duckdb_store.read_query("SELECT 1 FROM indicator_ext")) == 2
    builder.build(codes=[STOCK])
    codes = {r["stock_code"] for r in duckdb_store.read_query("SELECT stock_code FROM indicator_ext")}
    assert codes == {STOCK, "000003"}
