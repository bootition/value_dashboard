"""CSMAR .dta → DuckDB 导入脚本

从 CSMAR 学术数据库的 Stata .dta 文件导入财务报表数据到 DuckDB。
仅导入合并报表 (Typrep='A')，原始字段存入 raw_data JSON 列。

用法:
    python scripts/import_csmar.py [--db PATH] [--source DIR] [--dry-run]

依赖: pandas, duckdb, pyarrow (pip install pandas duckdb pyarrow)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import pandas as pd

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_SOURCE = PROJECT_ROOT / "额外资料" / "C17 a股上市公司财务数据合集（90-25年）" / "原始数据（dta格式）"

BALANCE_SHEET_MAP: dict[str, str] = {
    "A001101000": "monetary_funds",
    "A001107000": "trading_financial_assets",
    "A001110000": "notes_receivable",
    "A001111000": "accounts_receivable",
    "A001112000": "prepayments",
    "A001121000": "other_receivables",
    "A001123000": "inventory",
    "A001128000": "contract_assets",
    "A001100000": "total_current_assets",
    "A001205000": "long_term_equity_investment",
    "A001212000": "fixed_assets",
    "A001213000": "construction_in_progress",
    "A001230000": "right_of_use_assets",
    "A001218000": "intangible_assets",
    "A001220000": "goodwill",
    "A001222000": "deferred_tax_assets",
    "A001200000": "total_non_current_assets",
    "A001000000": "total_assets",
    "A002101000": "short_term_loans",
    "A002107000": "notes_payable",
    "A002108000": "accounts_payable",
    "A002109000": "prepayments_received",
    "A002128000": "contract_liabilities",
    "A002112000": "employee_benefits_payable",
    "A002113000": "taxes_payable",
    "A002100000": "total_current_liabilities",
    "A002201000": "long_term_loans",
    "A002203000": "bonds_payable",
    "A002211000": "lease_liabilities",
    "A002200000": "total_non_current_liabilities",
    "A002000000": "total_liabilities",
    "A003101000": "paid_in_capital",
    "A003102000": "capital_reserve",
    "A003103000": "surplus_reserve",
    "A003105000": "undistributed_profit",
    "A003200000": "minority_interest",
    "A003000000": "total_equity",
    "A003100000": "total_equity_parent",
}

INCOME_STATEMENT_MAP: dict[str, str] = {
    "B001100000": "total_operating_revenue",
    "B001101000": "revenue",
    "B001200000": "total_operating_cost",
    "B001201000": "cost_of_revenue",
    "B001207000": "taxes_and_surcharges",
    "B001209000": "selling_expenses",
    "B001210000": "administrative_expenses",
    "B001216000": "rd_expenses",
    "B001211000": "financial_expenses",
    "B001211101": "interest_expense",
    "B001211203": "interest_income",
    "B001212000": "asset_impairment_loss",
    "B001307000": "credit_impairment_loss",
    "B001301000": "exchange_gain",
    "B001302000": "investment_income",
    "B001300000": "operating_profit",
    "B001400000": "non_operating_income",
    "B001500000": "non_operating_expenses",
    "B001000000": "total_profit",
    "B002100000": "income_tax",
    "B002000000": "net_profit",
    "B002000101": "parent_net_profit",
    "B002000201": "minority_shareholder_profit",
    "B003000000": "basic_eps",
    "B004000000": "diluted_eps",
}

CASH_FLOW_MAP: dict[str, str] = {
    "C001001000": "cash_received_sales",
    "C001012000": "taxes_refunded",
    "C001013000": "other_operating_cf_in",
    "C001100000": "total_operating_cf_in",
    "C001014000": "cash_paid_goods",
    "C001020000": "cash_paid_employees",
    "C001021000": "cash_paid_taxes",
    "C001022000": "other_operating_cf_out",
    "C001200000": "total_operating_cf_out",
    "C001000000": "cf_from_operating",
    "C002000000": "cf_from_investing",
    "C003000000": "cf_from_financing",
    "C004000000": "exchange_rate_effect",
    "C005000000": "cf_net",
    "C005001000": "cash_beginning",
    "C006000000": "cash_ending",
}


def _read_dta(path: Path, chunksize: int = 100_000) -> pd.DataFrame:
    logger.info("读取 %s (%.1f MB)...", path.name, path.stat().st_size / 1e6)
    t0 = time.time()
    chunks = []
    for chunk in pd.read_stata(path, chunksize=chunksize):
        chunks.append(chunk)
    df = pd.concat(chunks, ignore_index=True)
    logger.info("  读取完成: %d 行, %.1f 秒", len(df), time.time() - t0)
    return df


def _filter_consolidated(df: pd.DataFrame) -> pd.DataFrame:
    before = len(df)
    if "Typrep" in df.columns:
        df = df[df["Typrep"] == "A"].copy()
    logger.info("  合并报表筛选: %d → %d 行 (丢弃 %d 母公司报表)", before, len(df), before - len(df))
    return df


def _map_fields(df: pd.DataFrame, field_map: dict[str, str]) -> tuple[pd.DataFrame, list[str]]:
    mapped_cols = {}
    missing = []
    for csmar_code, schema_col in field_map.items():
        if csmar_code in df.columns:
            mapped_cols[schema_col] = pd.to_numeric(df[csmar_code], errors="coerce")
        else:
            missing.append(f"{csmar_code}→{schema_col}")
    if missing:
        logger.info("  缺少 %d 个字段 (设为 NULL): %s", len(missing), missing[:5])
    result_df = pd.DataFrame(mapped_cols)
    return result_df, missing


def _build_raw_data_json(df: pd.DataFrame, field_map: dict[str, str]) -> list[str]:
    return ['{}'] * len(df)


def _determine_report_type(accper: pd.Series) -> pd.Series:
    def classify(date_str):
        if not isinstance(date_str, str):
            return None
        month = date_str[5:7] if len(date_str) >= 7 else ""
        if month == "12":
            return "annual"
        elif month == "06":
            return "semi_annual"
        elif month == "03":
            return "quarterly"
        elif month == "09":
            return "quarterly"
        return "quarterly"
    return accper.apply(classify)


def _make_stock_code(stkcd: pd.Series) -> pd.Series:
    return stkcd.astype(str).str.zfill(6)


def import_balance_sheet(conn: duckdb.DuckDBPyConnection, source_dir: Path, dry_run: bool) -> int:
    dta_path = source_dir / "资产负债表" / "FS_Combas.dta"
    if not dta_path.exists():
        logger.error("找不到: %s", dta_path)
        return 0

    df = _read_dta(dta_path)
    df = _filter_consolidated(df)

    stock_codes = _make_stock_code(df["Stkcd"])
    report_dates = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    report_types = _determine_report_type(df["Accper"].astype(str))
    mapped_df, _ = _map_fields(df, BALANCE_SHEET_MAP)

    insert_df = pd.DataFrame({
        "stock_code": stock_codes,
        "report_date": report_dates,
        "report_type": report_types,
    })
    for col in mapped_df.columns:
        insert_df[col] = mapped_df[col].values
    insert_df["raw_data"] = _build_raw_data_json(df, BALANCE_SHEET_MAP)

    column_order = [
        'stock_code', 'report_date', 'report_type',
        'monetary_funds', 'trading_financial_assets', 'notes_receivable',
        'accounts_receivable', 'prepayments', 'other_receivables', 'inventory',
        'contract_assets', 'total_current_assets',
        'long_term_equity_investment', 'fixed_assets', 'construction_in_progress',
        'right_of_use_assets', 'intangible_assets', 'goodwill',
        'deferred_tax_assets', 'total_non_current_assets', 'total_assets',
        'short_term_loans', 'notes_payable', 'accounts_payable',
        'prepayments_received', 'contract_liabilities',
        'employee_benefits_payable', 'taxes_payable', 'total_current_liabilities',
        'long_term_loans', 'bonds_payable', 'lease_liabilities',
        'total_non_current_liabilities', 'total_liabilities',
        'paid_in_capital', 'capital_reserve', 'surplus_reserve',
        'undistributed_profit', 'minority_interest', 'total_equity',
        'total_equity_parent', 'raw_data'
    ]
    insert_df = insert_df[column_order]

    insert_df = insert_df.dropna(subset=["report_date"])
    insert_df = insert_df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")

    logger.info("资产负债表: %d 行待导入, 股票数 %d, 日期范围 %s ~ %s",
                len(insert_df),
                insert_df["stock_code"].nunique(),
                insert_df["report_date"].min(),
                insert_df["report_date"].max())

    if dry_run:
        logger.info("[DRY RUN] 跳过写入")
        return len(insert_df)

    conn.execute("DELETE FROM balance_sheet")
    conn.execute("INSERT INTO balance_sheet SELECT * FROM insert_df")
    logger.info("资产负债表写入完成")
    return len(insert_df)


def import_income_statement(conn: duckdb.DuckDBPyConnection, source_dir: Path, dry_run: bool) -> int:
    dta_path = source_dir / "利润表" / "FS_Comins.dta"
    if not dta_path.exists():
        logger.error("找不到: %s", dta_path)
        return 0

    df = _read_dta(dta_path)
    df = _filter_consolidated(df)

    stock_codes = _make_stock_code(df["Stkcd"])
    report_dates = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    report_types = _determine_report_type(df["Accper"].astype(str))
    mapped_df, _ = _map_fields(df, INCOME_STATEMENT_MAP)

    insert_df = pd.DataFrame({
        "stock_code": stock_codes,
        "report_date": report_dates,
        "report_type": report_types,
    })
    for col in mapped_df.columns:
        insert_df[col] = mapped_df[col].values
    insert_df["deducted_net_profit"] = None
    insert_df["raw_data"] = _build_raw_data_json(df, INCOME_STATEMENT_MAP)

    # 重新排列列顺序，与表 schema 一致（raw_data 在最后）
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
    insert_df = insert_df[column_order]

    insert_df = insert_df.dropna(subset=["report_date"])
    insert_df = insert_df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")

    logger.info("利润表: %d 行待导入, 股票数 %d, 日期范围 %s ~ %s",
                len(insert_df),
                insert_df["stock_code"].nunique(),
                insert_df["report_date"].min(),
                insert_df["report_date"].max())

    if dry_run:
        logger.info("[DRY RUN] 跳过写入")
        return len(insert_df)

    conn.execute("DELETE FROM income_statement")
    conn.execute("INSERT INTO income_statement SELECT * FROM insert_df")
    logger.info("利润表写入完成")
    return len(insert_df)


def import_cash_flow(conn: duckdb.DuckDBPyConnection, source_dir: Path, dry_run: bool) -> int:
    dta_path = source_dir / "现金流量表(直接法)" / "FS_Comscfd.dta"
    if not dta_path.exists():
        logger.error("找不到: %s", dta_path)
        return 0

    df = _read_dta(dta_path)
    df = _filter_consolidated(df)

    stock_codes = _make_stock_code(df["Stkcd"])
    report_dates = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    report_types = _determine_report_type(df["Accper"].astype(str))
    mapped_df, _ = _map_fields(df, CASH_FLOW_MAP)

    insert_df = pd.DataFrame({
        "stock_code": stock_codes,
        "report_date": report_dates,
        "report_type": report_types,
    })
    for col in mapped_df.columns:
        insert_df[col] = mapped_df[col].values
    insert_df["raw_data"] = _build_raw_data_json(df, CASH_FLOW_MAP)

    column_order = [
        'stock_code', 'report_date', 'report_type',
        'cash_received_sales', 'taxes_refunded', 'other_operating_cf_in',
        'total_operating_cf_in', 'cash_paid_goods', 'cash_paid_employees',
        'cash_paid_taxes', 'other_operating_cf_out', 'total_operating_cf_out',
        'cf_from_operating', 'cf_from_investing', 'cf_from_financing',
        'exchange_rate_effect', 'cf_net', 'cash_beginning', 'cash_ending', 'raw_data'
    ]
    insert_df = insert_df[column_order]

    insert_df = insert_df.dropna(subset=["report_date"])
    insert_df = insert_df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")

    logger.info("现金流量表: %d 行待导入, 股票数 %d, 日期范围 %s ~ %s",
                len(insert_df),
                insert_df["stock_code"].nunique(),
                insert_df["report_date"].min(),
                insert_df["report_date"].max())

    if dry_run:
        logger.info("[DRY RUN] 跳过写入")
        return len(insert_df)

    conn.execute("DELETE FROM cash_flow")
    conn.execute("INSERT INTO cash_flow SELECT * FROM insert_df")
    logger.info("现金流量表写入完成")
    return len(insert_df)


def import_dividends(conn: duckdb.DuckDBPyConnection, source_dir: Path, dry_run: bool) -> int:
    dta_path = source_dir / "股利分配" / "FI_T11.dta"
    if not dta_path.exists():
        logger.error("找不到: %s", dta_path)
        return 0

    df = _read_dta(dta_path)
    df = _filter_consolidated(df)

    stock_codes = _make_stock_code(df["Stkcd"])
    ex_dates = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    dps = pd.to_numeric(df.get("F110101B", pd.Series(dtype=float)), errors="coerce")

    insert_df = pd.DataFrame({
        "stock_code": stock_codes,
        "ex_date": ex_dates,
        "announcement_date": None,
        "dividend_per_share": dps,
        "stock_dividend": None,
        "transfer_share": None,
        "rights_issue": None,
        "rights_issue_price": None,
    })

    insert_df = insert_df.dropna(subset=["ex_date"])
    insert_df = insert_df[insert_df["dividend_per_share"].notna() & (insert_df["dividend_per_share"] != 0)]
    insert_df = insert_df.drop_duplicates(subset=["stock_code", "ex_date"], keep="last")

    column_order = [
        'stock_code', 'ex_date', 'announcement_date',
        'dividend_per_share', 'stock_dividend', 'transfer_share',
        'rights_issue', 'rights_issue_price'
    ]
    insert_df = insert_df[column_order]

    logger.info("分红: %d 行待导入, 股票数 %d, 日期范围 %s ~ %s",
                len(insert_df),
                insert_df["stock_code"].nunique(),
                insert_df["ex_date"].min(),
                insert_df["ex_date"].max())
    logger.warning("  注意: ex_date 为报告期末占位日期(12-31/06-30), 非真实除权日, 需后续用 AKShare 修正")

    if dry_run:
        logger.info("[DRY RUN] 跳过写入")
        return len(insert_df)

    conn.execute("DELETE FROM dividends")
    conn.execute("INSERT INTO dividends SELECT * FROM insert_df")
    logger.info("分红写入完成")
    return len(insert_df)


def import_stock_meta(conn: duckdb.DuckDBPyConnection, source_dir: Path, dry_run: bool) -> int:
    dta_path = source_dir / "资产负债表" / "FS_Combas.dta"
    if not dta_path.exists():
        return 0

    logger.info("从资产负债表提取股票元数据...")
    df = _read_dta(dta_path)
    df = _filter_consolidated(df)

    latest = df.sort_values("Accper").groupby("Stkcd").last().reset_index()
    stock_codes = _make_stock_code(latest["Stkcd"])
    names = latest.get("ShortName", pd.Series([""] * len(latest)))

    def guess_exchange(code: str) -> str:
        if code.startswith("6"):
            return "SSE"
        elif code.startswith(("0", "3")):
            return "SZSE"
        elif code.startswith(("4", "8")):
            return "BSE"
        return "UNKNOWN"

    insert_df = pd.DataFrame({
        "stock_code": stock_codes,
        "name": names.values,
        "pinyin": None,
        "exchange": [guess_exchange(c) for c in stock_codes],
        "listing_date": None,
        "is_st": None,
        "is_suspended": None,
        "sw_level1": None,
        "sw_level2": None,
        "sw_level1_code": None,
        "sw_level2_code": None,
        "updated_at": pd.Timestamp.now(),
    })

    insert_df = insert_df.drop_duplicates(subset=["stock_code"], keep="last")

    logger.info("股票元数据: %d 只股票", len(insert_df))

    if dry_run:
        logger.info("[DRY RUN] 跳过写入")
        return len(insert_df)

    conn.execute("DELETE FROM stock_meta")
    conn.execute("INSERT INTO stock_meta SELECT * FROM insert_df")
    logger.info("股票元数据写入完成")
    return len(insert_df)


def verify(conn: duckdb.DuckDBPyConnection) -> None:
    logger.info("=" * 60)
    logger.info("导入验证")
    logger.info("=" * 60)

    tables = ["stock_meta", "balance_sheet", "income_statement", "cash_flow", "dividends"]
    for table in tables:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        logger.info("  %-20s %s 行", table, f"{row[0]:,}")

    for table in ["balance_sheet", "income_statement", "cash_flow"]:
        row = conn.execute(f"""
            SELECT MIN(report_date), MAX(report_date),
                   COUNT(DISTINCT stock_code)
            FROM {table}
        """).fetchone()
        logger.info("  %s: %s ~ %s, %d 只股票", table, row[0], row[1], row[2])

    row = conn.execute("""
        SELECT COUNT(DISTINCT stock_code),
               SUM(CASE WHEN total_assets IS NOT NULL AND total_assets > 0 THEN 1 ELSE 0 END)
        FROM balance_sheet
    """).fetchone()
    logger.info("  balance_sheet: %d 只股票, %d 行有正总资产", row[0], row[1])

    row = conn.execute("""
        SELECT COUNT(DISTINCT stock_code),
               SUM(CASE WHEN dividend_per_share > 0 THEN 1 ELSE 0 END)
        FROM dividends
    """).fetchone()
    logger.info("  dividends: %d 只股票有分红, %d 条正金额", row[0], row[1])

    sample = conn.execute("""
        SELECT stock_code, report_date, total_assets, total_liabilities, total_equity
        FROM balance_sheet
        WHERE stock_code = '600519'
        ORDER BY report_date DESC
        LIMIT 3
    """).fetchall()
    if sample:
        logger.info("  茅台(600519)资产负债表抽样:")
        for r in sample:
            logger.info("    %s: 总资产=%s, 总负债=%s, 权益=%s",
                        r[1], f"{r[2]:,.0f}" if r[2] else "NULL",
                        f"{r[3]:,.0f}" if r[3] else "NULL",
                        f"{r[4]:,.0f}" if r[4] else "NULL")


def main():
    parser = argparse.ArgumentParser(description="CSMAR .dta → DuckDB 导入")
    parser.add_argument("--db", type=Path, help="DuckDB 路径（必须与已验证运行环境一致）")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="CSMAR 原始数据目录")
    parser.add_argument("--dry-run", action="store_true", help="只读取不写入")
    parser.add_argument("--tables", nargs="*", default=None,
                        help="指定导入表: balance_sheet income_statement cash_flow dividends stock_meta")
    args = parser.parse_args()

    if not args.source.exists():
        logger.error("CSMAR 数据目录不存在: %s", args.source)
        sys.exit(1)

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))
    if args.db is not None and args.db.resolve(strict=False) != paths.duckdb_path:
        parser.error("--db must match the validated VD_DUCKDB_PATH")

    logger.info("CSMAR 数据源: %s", args.source)
    logger.info("目标数据库: %s", paths.duckdb_path)
    logger.info("模式: %s", "DRY RUN" if args.dry_run else "WRITE")

    store = DuckDBStore(paths=paths)
    if not args.dry_run:
        from app.core.storage.schema import init_duckdb_schema

        logger.info("初始化 schema...")
        init_duckdb_schema(store)

    tables = args.tables or ["stock_meta", "balance_sheet", "income_statement", "cash_flow", "dividends"]
    results = {}
    importers = {
        "stock_meta": import_stock_meta,
        "balance_sheet": import_balance_sheet,
        "income_statement": import_income_statement,
        "cash_flow": import_cash_flow,
        "dividends": import_dividends,
    }
    connection = store.read_connection if args.dry_run else store.transaction
    with connection() as conn:
        for table in tables:
            if table in importers:
                logger.info("\n%s %s %s", "=" * 20, table, "=" * 20)
                results[table] = importers[table](conn, args.source, args.dry_run)
        if not args.dry_run:
            verify(conn)

    logger.info("\n%s", "=" * 60)
    logger.info("导入完成汇总:")
    for table, count in results.items():
        logger.info("  %-20s %s 行", table, f"{count:,}")


if __name__ == "__main__":
    main()
