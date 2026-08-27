"""
将 CSV 临时文件导入 DuckDB 数据库

功能：
1. 读取 *_akshare_temp.csv 文件
2. 填充缺失字段（如 deducted_net_profit）
3. 导入到 DuckDB 数据库
4. 验证导入结果

用法：
    python scripts/import_csv_to_db.py [--db PATH]
"""

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _prepare_frame(
    df: pd.DataFrame, column_order: list[str], *, key_column: str = "report_date",
) -> pd.DataFrame:
    """Validate keys and make DataFrame order match the explicit SQL target list."""
    required = {"stock_code", key_column}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")
    df = df.copy()
    for column in column_order:
        if column not in df.columns:
            df[column] = None
    if df[key_column].isna().any() or (df["stock_code"].astype(str).str.strip() == "").any():
        raise ValueError(f"CSV contains empty stock_code or {key_column}")
    if df.duplicated(["stock_code", key_column]).any():
        raise ValueError(f"CSV contains duplicate stock_code/{key_column} keys")
    return df[column_order]

def _upsert_frame(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    df: pd.DataFrame,
) -> None:
    """Merge one CSV by primary key without deleting unrelated history.

    Missing source columns become NULL in the staging DataFrame, but COALESCE
    prevents them from erasing a value already held in the analytical table.
    This is intentionally not a source-lineage publisher; the CLI path remains
    disabled until canonical audited ingestion is used.
    """
    columns = list(df.columns)
    date_column = "ex_date" if table == "dividends" else "report_date"
    keys = ["stock_code", date_column]
    updates = [column for column in columns if column not in keys]
    assignment_sql = ", ".join(
        f"{column} = COALESCE(excluded.{column}, {table}.{column})"
        for column in updates
    )
    column_sql = ", ".join(columns)
    conn.execute(
        f"""INSERT INTO {table} ({column_sql})
            SELECT {column_sql} FROM df
            ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignment_sql}"""
    )


def import_balance_sheet(conn: duckdb.DuckDBPyConnection, csv_path: Path) -> int:
    """导入资产负债表"""
    print("\n=== 导入资产负债表 ===")

    df = pd.read_csv(csv_path, dtype={"stock_code": str})
    print(f"读取 {len(df)} 行")
    
    # 确保 stock_code 格式正确
    df["stock_code"] = df["stock_code"].str.zfill(6)
    
    # 填充缺失字段
    if "report_type" not in df.columns:
        df["report_type"] = None
    if "raw_data" not in df.columns:
        df["raw_data"] = None
    
    column_order = [
        'stock_code', 'report_date', 'report_type', 'monetary_funds', 'trading_financial_assets',
        'notes_receivable', 'accounts_receivable', 'prepayments', 'other_receivables', 'inventory',
        'contract_assets', 'total_current_assets', 'long_term_equity_investment', 'fixed_assets',
        'construction_in_progress', 'right_of_use_assets', 'intangible_assets', 'goodwill',
        'deferred_tax_assets', 'total_non_current_assets', 'total_assets', 'short_term_loans',
        'notes_payable', 'accounts_payable', 'prepayments_received', 'contract_liabilities',
        'employee_benefits_payable', 'taxes_payable', 'total_current_liabilities', 'long_term_loans',
        'bonds_payable', 'lease_liabilities', 'total_non_current_liabilities', 'total_liabilities',
        'paid_in_capital', 'capital_reserve', 'surplus_reserve', 'undistributed_profit',
        'minority_interest', 'total_equity', 'total_equity_parent', 'raw_data',
    ]
    df = _prepare_frame(df, column_order)
    _upsert_frame(conn, "balance_sheet", df)
    
    count = conn.execute("SELECT COUNT(*) FROM balance_sheet WHERE report_date > '2025-03-31'").fetchone()[0]
    print(f"导入完成: {count} 行")
    return count


def import_income_statement(conn: duckdb.DuckDBPyConnection, csv_path: Path) -> int:
    """导入利润表"""
    print("\n=== 导入利润表 ===")
    
    df = pd.read_csv(csv_path, dtype={"stock_code": str})
    print(f"读取 {len(df)} 行")
    
    # 确保 stock_code 格式正确
    df["stock_code"] = df["stock_code"].str.zfill(6)
    
    # 填充缺失字段
    if "report_type" not in df.columns:
        df["report_type"] = None
    if "raw_data" not in df.columns:
        df["raw_data"] = None
    
    # 处理扣非净利润缺失
    if "deducted_net_profit" not in df.columns:
        print("[WARN] 扣非净利润字段缺失，填充 NULL")
        df["deducted_net_profit"] = None
    
    # 重新排列列顺序，使其与 INSERT 语句一致
    column_order = [
        'stock_code', 'report_date', 'report_type',
        'total_operating_revenue', 'revenue', 'total_operating_cost', 'cost_of_revenue',
        'taxes_and_surcharges', 'selling_expenses', 'administrative_expenses',
        'rd_expenses', 'financial_expenses', 'interest_expense', 'interest_income',
        'asset_impairment_loss', 'credit_impairment_loss', 'exchange_gain',
        'investment_income', 'operating_profit', 'non_operating_income',
        'non_operating_expenses', 'total_profit', 'income_tax', 'net_profit',
        'parent_net_profit', 'minority_shareholder_profit', 'deducted_net_profit',
        'basic_eps', 'diluted_eps', 'raw_data'
    ]
    df = _prepare_frame(df, column_order)
    _upsert_frame(conn, "income_statement", df)
    
    count = conn.execute("SELECT COUNT(*) FROM income_statement WHERE report_date > '2025-03-31'").fetchone()[0]
    print(f"导入完成: {count} 行")
    return count


def import_cash_flow(conn: duckdb.DuckDBPyConnection, csv_path: Path) -> int:
    """导入现金流量表"""
    print("\n=== 导入现金流量表 ===")
    
    df = pd.read_csv(csv_path, dtype={"stock_code": str})
    print(f"读取 {len(df)} 行")
    
    # 确保 stock_code 格式正确
    df["stock_code"] = df["stock_code"].str.zfill(6)
    
    # 填充缺失字段
    if "report_type" not in df.columns:
        df["report_type"] = None
    if "raw_data" not in df.columns:
        df["raw_data"] = None
    
    column_order = [
        'stock_code', 'report_date', 'report_type', 'cash_received_sales', 'taxes_refunded',
        'other_operating_cf_in', 'total_operating_cf_in', 'cash_paid_goods', 'cash_paid_employees',
        'cash_paid_taxes', 'other_operating_cf_out', 'total_operating_cf_out', 'cf_from_operating',
        'cf_from_investing', 'cf_from_financing', 'exchange_rate_effect', 'cf_net', 'cash_beginning',
        'cash_ending', 'raw_data',
    ]
    df = _prepare_frame(df, column_order)
    _upsert_frame(conn, "cash_flow", df)
    
    count = conn.execute("SELECT COUNT(*) FROM cash_flow WHERE report_date > '2025-03-31'").fetchone()[0]
    print(f"导入完成: {count} 行")
    return count


def import_dividends(conn: duckdb.DuckDBPyConnection, csv_path: Path) -> int:
    """导入分红表"""
    print("\n=== 导入分红表 ===")
    
    df = pd.read_csv(csv_path, dtype={"stock_code": str})
    print(f"读取 {len(df)} 行")
    
    # 确保 stock_code 格式正确
    df["stock_code"] = df["stock_code"].str.zfill(6)
    
    # 填充缺失字段
    for col in ["announcement_date", "dividend_per_share", "stock_dividend", 
                "transfer_share", "rights_issue", "rights_issue_price"]:
        if col not in df.columns:
            df[col] = None

    column_order = [
        'stock_code', 'ex_date', 'announcement_date', 'dividend_per_share', 'stock_dividend',
        'transfer_share', 'rights_issue', 'rights_issue_price',
    ]
    df = _prepare_frame(df, column_order, key_column="ex_date")
    _upsert_frame(conn, "dividends", df)
    
    count = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
    print(f"导入完成: {count} 行")
    return count


def verify_import(conn: duckdb.DuckDBPyConnection):
    """验证导入结果"""
    print("\n=== 验证导入结果 ===")
    
    tables = ["balance_sheet", "income_statement", "cash_flow", "dividends"]
    
    for table in tables:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if table == "dividends":
            new_count = conn.execute(f"""
                SELECT COUNT(*) FROM {table}
                WHERE CAST(ex_date AS VARCHAR) NOT LIKE '%12-31'
                  AND CAST(ex_date AS VARCHAR) NOT LIKE '%06-30'
            """).fetchone()[0]
        else:
            new_count = conn.execute(f"""
                SELECT COUNT(*) FROM {table}
                WHERE report_date > '2025-03-31'
            """).fetchone()[0]
        
        print(f"{table}: 总计 {total:,} 行, 新增 {new_count:,} 行")
    
    # 抽样检查
    print("\n抽样检查:")
    sample = conn.execute("""
        SELECT stock_code, report_date, total_assets, total_liabilities
        FROM balance_sheet
        WHERE report_date > '2025-03-31'
        ORDER BY report_date DESC
        LIMIT 3
    """).fetchall()
    
    for row in sample:
        assets = f"{row[2]:,.0f}" if row[2] else "NULL"
        liabilities = f"{row[3]:,.0f}" if row[3] else "NULL"
        print(f"  {row[0]} {row[1]}: 资产={assets}, 负债={liabilities}")


def main():
    parser = argparse.ArgumentParser(description="CSV 导入 DuckDB")
    parser.add_argument("--db", type=Path, help="DuckDB 路径（必须与已验证运行环境一致）")
    parser.add_argument("--skip-financials", action="store_true")
    parser.add_argument("--skip-dividends", action="store_true")
    args = parser.parse_args()
    
    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))
    if args.db is not None and args.db.resolve(strict=False) != paths.duckdb_path:
        parser.error("--db must match the validated VD_DUCKDB_PATH")
    parser.error(
        "Direct CSV publication is disabled: this script cannot atomically retain "
        "source payloads, fetch batches, and field audits. Use the canonical "
        "audited ingestion workflow."
    )

    print(f"数据库: {paths.duckdb_path}")

    store = DuckDBStore(paths=paths)
    with store.transaction() as conn:
        data_dir = PROJECT_ROOT / "data"
        
        if not args.skip_financials:
            # 导入资产负债表
            bs_csv = data_dir / "balance_sheet_akshare_temp.csv"
            if bs_csv.exists():
                import_balance_sheet(conn, bs_csv)
            else:
                print(f"[WARN] 文件不存在: {bs_csv}")
            
            # 导入利润表
            is_csv = data_dir / "income_statement_akshare_temp.csv"
            if is_csv.exists():
                import_income_statement(conn, is_csv)
            else:
                print(f"[WARN] 文件不存在: {is_csv}")
            
            # 导入现金流量表
            cf_csv = data_dir / "cash_flow_akshare_temp.csv"
            if cf_csv.exists():
                import_cash_flow(conn, cf_csv)
            else:
                print(f"[WARN] 文件不存在: {cf_csv}")
        
        if not args.skip_dividends:
            # 导入分红表
            div_csv = data_dir / "dividends_akshare_temp.csv"
            if div_csv.exists():
                import_dividends(conn, div_csv)
            else:
                print(f"[WARN] 文件不存在: {div_csv}")
        
        verify_import(conn)
        
        print("\n[PASS] 导入完成")
        print("\n说明:")
        print("- 扣非净利润 (deducted_net_profit) 字段已填充 NULL")
        print("- 后续可从 CSMAR 历史数据或其他来源补充")
        print("- 不影响其他指标计算")


if __name__ == "__main__":
    main()
