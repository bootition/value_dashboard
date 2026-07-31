"""AKShare 数据补齐脚本

用 AKShare/东方财富接口补齐 CSMAR 截止日期 (2025-03-31) 之后的财报数据,
并用真实除权日替换 CSMAR 的分红占位日期。

用法:
    python scripts/supplement_akshare.py [--db PATH] [--sample N] [--skip-financials] [--skip-dividends]

依赖: akshare, pandas, duckdb
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import akshare as ak
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

CSMAR_CUTOFF = "2025-03-31"
RATE_LIMIT = 0.5

BALANCE_SHEET_FIELD_MAP: dict[str, str] = {
    "MONETARYFUNDS": "monetary_funds",
    "TRADE_FINASSET": "trading_financial_assets",
    "NOTE_RECE": "notes_receivable",
    "ACCOUNTS_RECE": "accounts_receivable",
    "PREPAYMENT": "prepayments",
    "OTHER_RECE": "other_receivables",
    "INVENTORY": "inventory",
    "CONTRACT_ASSET": "contract_assets",
    "TOTAL_CURRENT_ASSETS": "total_current_assets",
    "LONG_EQUITY_INVEST": "long_term_equity_investment",
    "FIXED_ASSET": "fixed_assets",
    "CIP": "construction_in_progress",
    "USERIGHT_ASSET": "right_of_use_assets",
    "INTANGIBLE_ASSET": "intangible_assets",
    "GOODWILL": "goodwill",
    "DEFER_TAX_ASSET": "deferred_tax_assets",
    "TOTAL_NONCURRENT_ASSETS": "total_non_current_assets",
    "TOTAL_ASSETS": "total_assets",
    "SHORT_LOAN": "short_term_loans",
    "NOTE_PAYABLE": "notes_payable",
    "NOTE_ACCOUNTS_PAYABLE": "accounts_payable",
    "ADVANCE_RECEIVABLES": "prepayments_received",
    "CONTRACT_LIAB": "contract_liabilities",
    "STAFF_SALARY_PAYABLE": "employee_benefits_payable",
    "TAX_PAYABLE": "taxes_payable",
    "TOTAL_CURRENT_LIAB": "total_current_liabilities",
    "LONG_LOAN": "long_term_loans",
    "BOND_PAYABLE": "bonds_payable",
    "LEASE_LIAB": "lease_liabilities",
    "TOTAL_NONCURRENT_LIAB": "total_non_current_liabilities",
    "TOTAL_LIABILITIES": "total_liabilities",
    "SHARE_CAPITAL": "paid_in_capital",
    "CAPITAL_RESERVE": "capital_reserve",
    "SURPLUS_RESERVE": "surplus_reserve",
    "UNASSIGN_RPOFIT": "undistributed_profit",
    "MINORITY_EQUITY": "minority_interest",
    "TOTAL_EQUITY": "total_equity",
    "TOTAL_PARENT_EQUITY": "total_equity_parent",
}

INCOME_FIELD_MAP: dict[str, str] = {
    "TOTAL_OPERATE_INCOME": "total_operating_revenue",
    "OPERATE_INCOME": "revenue",
    "TOTAL_OPERATE_COST": "total_operating_cost",
    "OPERATE_COST": "cost_of_revenue",
    "OPERATE_TAX": "taxes_and_surcharges",
    "SALE_EXPENSE": "selling_expenses",
    "MANAGE_EXPENSE": "administrative_expenses",
    "RESEARCH_EXPENSE": "rd_expenses",
    "FINANCE_EXPENSE": "financial_expenses",
    "INTEREST_EXPENSE": "interest_expense",
    "INTEREST_INCOME": "interest_income",
    "ASSET_IMPAIRMENT_LOSS": "asset_impairment_loss",
    "CREDIT_IMPAIRMENT_LOSS": "credit_impairment_loss",
    "FAIRVALUE_CHANGE_INCOME": "exchange_gain",
    "INVEST_INCOME": "investment_income",
    "OPERATE_PROFIT": "operating_profit",
    "NON_OPERATE_INCOME": "non_operating_income",
    "NON_OPERATE_EXPENSE": "non_operating_expenses",
    "TOTAL_PROFIT": "total_profit",
    "INCOME_TAX": "income_tax",
    "NETPROFIT": "net_profit",
    "PARENT_NETPROFIT": "parent_net_profit",
    "MINORITY_INCOME": "minority_shareholder_profit",
    "DEDUCT_PARENT_NETPROFIT": "deducted_net_profit",
    "BASIC_EPS": "basic_eps",
    "DILUTED_EPS": "diluted_eps",
}

CASH_FLOW_FIELD_MAP: dict[str, str] = {
    "SALES_SERVICES": "cash_received_sales",
    "TAX_REFUND": "taxes_refunded",
    "RECEIVE_OTHER_OPERATE": "other_operating_cf_in",
    "TOTAL_OPERATE_INFLOW": "total_operating_cf_in",
    "BUY_SERVICES": "cash_paid_goods",
    "STAFF_CASH_PAY": "cash_paid_employees",
    "ALL_TAX_PAY": "cash_paid_taxes",
    "PAY_OTHER_OPERATE": "other_operating_cf_out",
    "TOTAL_OPERATE_OUTFLOW": "total_operating_cf_out",
    "NETCASH_OPERATE": "cf_from_operating",
    "NETCASH_INVEST": "cf_from_investing",
    "NETCASH_FINANCE": "cf_from_financing",
    "RATE_CHANGE_EFFECT": "exchange_rate_effect",
    "CCE_ADD": "cf_net",
    "BEGIN_CCE": "cash_beginning",
    "END_CCE": "cash_ending",
}


def _to_em_symbol(code: str) -> str:
    code = code.strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"SH{code}"
    elif code.startswith(("0", "2", "3")):
        return f"SZ{code}"
    elif code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SH{code}"


def _strip_code(code: str) -> str:
    return code.strip().zfill(6)


def _parse_report_date(rd) -> str | None:
    if rd is None or (isinstance(rd, float) and pd.isna(rd)):
        return None
    s = str(rd).strip()
    if len(s) >= 10:
        return s[:10]
    return None


def _wait_rate_limit():
    time.sleep(RATE_LIMIT)


def _load_existing_csv(save_path: Path) -> pd.DataFrame | None:
    """从已存在的 CSV 文件加载数据"""
    if save_path.exists() and save_path.stat().st_size > 0:
        try:
            df = pd.read_csv(save_path, dtype={"stock_code": str})
            df["stock_code"] = df["stock_code"].str.zfill(6)
            logger.info("  从 %s 恢复 %d 条已有数据", save_path, len(df))
            return df
        except Exception as e:
            logger.warning("  读取 %s 失败: %s", save_path, e)
    return None


def _save_to_csv(df: pd.DataFrame, save_path: Path) -> None:
    """保存数据到 CSV"""
    # 确保 stock_code 列是字符串类型，避免被解析为整数
    if "stock_code" in df.columns:
        df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    df.to_csv(save_path, index=False)
    logger.info("  已保存 %d 条数据到 %s", len(df), save_path)


def supplement_financials(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    api_func,
    field_map: dict[str, str],
    stock_codes: list[str],
    dry_run: bool,
    fetch_only: bool = False,
    import_only: bool = False,
) -> int:
    save_path = PROJECT_ROOT / "data" / f"{table_name}_akshare_temp.csv"
    
    if import_only:
        # 只从 CSV 导入
        logger.info("从 %s 导入 %s...", save_path, table_name)
        existing_df = _load_existing_csv(save_path)
        if existing_df is None or len(existing_df) == 0:
            logger.warning("  没有找到可导入的数据")
            return 0
        insert_df = existing_df
    else:
        # 抓取数据
        logger.info("抓取 %s (%d 只股票)...", table_name, len(stock_codes))
        
        # 恢复已有数据
        existing_df = _load_existing_csv(save_path)
        existing_codes = set()
        if existing_df is not None and len(existing_df) > 0:
            existing_codes = set(existing_df["stock_code"].unique())
            logger.info("  已有 %d 只股票的数据，跳过", len(existing_codes))
        
        all_rows = []
        if existing_df is not None:
            all_rows = existing_df.to_dict("records")
        
        errors = 0
        for i, code in enumerate(stock_codes):
            plain_code = _strip_code(code)
            
            # 跳过已抓取的股票
            if plain_code in existing_codes:
                continue
            
            em_symbol = _to_em_symbol(plain_code)
            try:
                _wait_rate_limit()
                df = api_func(symbol=em_symbol)
                if df is None or len(df) == 0:
                    continue

                for _, row in df.iterrows():
                    rd = _parse_report_date(row.get("REPORT_DATE"))
                    if rd is None or rd <= CSMAR_CUTOFF:
                        continue

                    mapped = {"stock_code": plain_code, "report_date": rd}
                    for em_col, schema_col in field_map.items():
                        val = row.get(em_col)
                        if val is not None and not (isinstance(val, float) and pd.isna(val)):
                            try:
                                mapped[schema_col] = float(val)
                            except (ValueError, TypeError):
                                mapped[schema_col] = None
                        else:
                            mapped[schema_col] = None

                    mapped["report_type"] = None
                    mapped["raw_data"] = None
                    all_rows.append(mapped)

                # 每 50 只股票保存一次
                if (i + 1) % 50 == 0:
                    _save_to_csv(pd.DataFrame(all_rows), save_path)
                    logger.info("  进度: %d/%d, 已收集 %d 行新数据", i + 1, len(stock_codes), len(all_rows))

            except Exception as e:
                errors += 1
                if errors <= 5:
                    logger.warning("  %s(%s) 失败: %s", table_name, em_symbol, e)

        logger.info("  完成: %d 行新数据, %d 个错误", len(all_rows), errors)
        
        if not all_rows:
            return 0
        
        # 保存最终结果
        insert_df = pd.DataFrame(all_rows)
        _save_to_csv(insert_df, save_path)
        
        if fetch_only:
            logger.info("  数据已保存到 %s，跳过数据库导入", save_path)
            return len(insert_df)
    
    # 导入数据库
    if dry_run:
        logger.info("[DRY RUN] %s: %d 行待插入", table_name, len(insert_df))
        return len(insert_df)

    raise RuntimeError(
        "Direct AKShare publication is disabled: use the canonical audited ingestion "
        "path to persist source payloads, fetch batches, and field audits atomically."
    )

    cols = list(insert_df.columns)

    existing = conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE report_date > '{CSMAR_CUTOFF}'").fetchone()[0]
    if existing > 0:
        csv_stocks = insert_df["stock_code"].nunique()
        existing_stocks = conn.execute(
            f"SELECT COUNT(DISTINCT stock_code) AS cnt FROM {table_name} WHERE report_date > '{CSMAR_CUTOFF}'"
        ).fetchone()[0]
        if existing_stocks > 0 and csv_stocks < existing_stocks * 0.5:
            raise ValueError(
                f"{table_name}: CSV covers {csv_stocks} stocks but DB has {existing_stocks} "
                f"post-cutoff stocks. Refusing delete-and-replace."
            )
        conn.execute(f"DELETE FROM {table_name} WHERE report_date > '{CSMAR_CUTOFF}'")
        logger.info("  已删除 %d 行旧补充数据", existing)

    conn.execute(f"INSERT INTO {table_name} ({', '.join(cols)}) SELECT * FROM insert_df")
    logger.info("  %s 写入完成: %d 行", table_name, len(insert_df))
    return len(insert_df)


def supplement_dividends(
    conn: duckdb.DuckDBPyConnection,
    stock_codes: list[str],
    dry_run: bool,
) -> int:
    logger.info("补齐分红真实除权日 (%d 只股票)...", len(stock_codes))
    all_rows = []
    errors = 0

    for i, code in enumerate(stock_codes):
        plain_code = _strip_code(code)
        try:
            _wait_rate_limit()
            df = ak.stock_dividend_cninfo(symbol=plain_code)
            if df is None or len(df) == 0:
                continue

            for _, row in df.iterrows():
                ex_date_raw = row.get("除权日") or row.get("除权除息日")
                if ex_date_raw is None or (isinstance(ex_date_raw, float) and pd.isna(ex_date_raw)):
                    continue
                ex_date_str = str(ex_date_raw).strip()
                if not ex_date_str or ex_date_str == "nan" or ex_date_str == "NaT" or len(ex_date_str) < 10:
                    continue
                ex_date = ex_date_str[:10]
                try:
                    datetime.strptime(ex_date, "%Y-%m-%d")
                except ValueError:
                    continue

                announce_date_raw = row.get("实施方案公告日期")
                announce_date = None
                if announce_date_raw is not None and not (isinstance(announce_date_raw, float) and pd.isna(announce_date_raw)):
                    announce_date = str(announce_date_raw).strip()[:10]

                cash_raw = row.get("派息比例")
                dps = None
                if cash_raw is not None and not (isinstance(cash_raw, float) and pd.isna(cash_raw)):
                    try:
                        dps = float(cash_raw) / 10.0
                    except (ValueError, TypeError):
                        pass

                bonus_raw = row.get("送股比例")
                stock_div = None
                if bonus_raw is not None and not (isinstance(bonus_raw, float) and pd.isna(bonus_raw)):
                    try:
                        stock_div = float(bonus_raw) / 10.0
                    except (ValueError, TypeError):
                        pass

                transfer_raw = row.get("转增比例")
                transfer_share = None
                if transfer_raw is not None and not (isinstance(transfer_raw, float) and pd.isna(transfer_raw)):
                    try:
                        transfer_share = float(transfer_raw) / 10.0
                    except (ValueError, TypeError):
                        pass

                all_rows.append({
                    "stock_code": plain_code,
                    "ex_date": ex_date,
                    "announcement_date": announce_date,
                    "dividend_per_share": dps,
                    "stock_dividend": stock_div,
                    "transfer_share": transfer_share,
                    "rights_issue": None,
                    "rights_issue_price": None,
                })

            if (i + 1) % 100 == 0:
                logger.info("  进度: %d/%d, 已收集 %d 条分红", i + 1, len(stock_codes), len(all_rows))

        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning("  dividends(%s) 失败: %s", plain_code, e)

    logger.info("  完成: %d 条分红记录, %d 个错误", len(all_rows), errors)

    if not all_rows:
        return 0

    insert_df = pd.DataFrame(all_rows)
    
    save_path = Path(__file__).resolve().parents[1] / "data" / "dividends_akshare_temp.csv"
    insert_df.to_csv(save_path, index=False)
    logger.info("  中间结果已保存到 %s", save_path)

    if dry_run:
        logger.info("[DRY RUN] dividends: %d 条待插入", len(insert_df))
        return len(insert_df)

    # 不删除已有 CSMAR 分红数据，AKShare 的真实日期会和占位日期共存
    # 应用层应优先使用非 12-31/06-30 的真实除权日
    conn.execute("INSERT INTO dividends SELECT * FROM insert_df")
    logger.info("  dividends 写入完成: %d 条 (CSMAR 数据保留, 真实日期追加)", len(insert_df))
    return len(insert_df)


def verify(conn: duckdb.DuckDBPyConnection) -> None:
    logger.info("=" * 60)
    logger.info("补齐后验证")
    logger.info("=" * 60)

    for table in ["balance_sheet", "income_statement", "cash_flow"]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        row_new = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE report_date > '{CSMAR_CUTOFF}'").fetchone()
        logger.info("  %s: 总计 %s 行, 其中 2025Q2+ 有 %s 行", table, f"{row[0]:,}", f"{row_new[0]:,}")

    row = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()
    logger.info("  dividends: %s 条", f"{row[0]:,}")

    row = conn.execute("""
        SELECT MIN(ex_date), MAX(ex_date), COUNT(DISTINCT stock_code)
        FROM dividends WHERE CAST(ex_date AS VARCHAR) NOT LIKE '%12-31' AND CAST(ex_date AS VARCHAR) NOT LIKE '%06-30'
    """).fetchone()
    logger.info("  dividends 真实除权日: %s ~ %s, %d 只股票有非占位日期", row[0], row[1], row[2])

    sample = conn.execute("""
        SELECT stock_code, report_date, total_assets
        FROM balance_sheet
        WHERE stock_code = '600519' AND report_date > '2025-03-31'
        ORDER BY report_date DESC LIMIT 3
    """).fetchall()
    if sample:
        logger.info("  茅台 2025Q2+ 资产负债表:")
        for r in sample:
            logger.info("    %s: total_assets=%s", r[1], f"{r[2]:,.0f}" if r[2] else "NULL")

    sample = conn.execute("""
        SELECT stock_code, ex_date, dividend_per_share
        FROM dividends WHERE stock_code = '600519'
        ORDER BY ex_date DESC LIMIT 5
    """).fetchall()
    if sample:
        logger.info("  茅台分红 (真实除权日):")
        for r in sample:
            logger.info("    %s: dps=%s", r[1], r[2])


def main():
    parser = argparse.ArgumentParser(description="AKShare 数据补齐")
    parser.add_argument("--db", type=Path, help="DuckDB 路径（必须与已验证运行环境一致）")
    parser.add_argument("--sample", type=int, default=0, help="只处理前 N 只股票 (0=全部)")
    parser.add_argument("--skip-financials", action="store_true")
    parser.add_argument("--skip-dividends", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))
    if args.db is not None and args.db.resolve(strict=False) != paths.duckdb_path:
        parser.error("--db must match the validated VD_DUCKDB_PATH")
    if not args.dry_run:
        parser.error(
            "Direct AKShare publication is disabled: this script cannot atomically "
            "retain source payloads, fetch batches, and field audits. Run with "
            "--dry-run only, then publish through the canonical audited ingestion workflow."
        )

    logger.info("数据库: %s", paths.duckdb_path)
    logger.info("CSMAR 截止: %s", CSMAR_CUTOFF)
    logger.info("模式: %s", "DRY RUN" if args.dry_run else "WRITE")

    store = DuckDBStore(paths=paths)
    connection = store.read_connection if args.dry_run else store.transaction
    with connection() as conn:
        stock_codes_df = conn.execute("SELECT DISTINCT stock_code FROM stock_meta ORDER BY stock_code").fetchdf()
        stock_codes = stock_codes_df["stock_code"].tolist()
        logger.info("股票总数: %d", len(stock_codes))

        if args.sample > 0:
            stock_codes = stock_codes[:args.sample]
            logger.info("样本模式: 只处理前 %d 只", len(stock_codes))
        
        # 从资产负债表 CSV 提取活跃股票，跳过无数据退市股
        bs_csv = PROJECT_ROOT / "data" / "balance_sheet_akshare_temp.csv"
        existing = _load_existing_csv(bs_csv)
        if existing is not None and len(existing) > 0:
            live_codes = sorted(existing["stock_code"].unique())
            skipped = len(stock_codes) - len(live_codes)
            logger.info("活跃股票: %d (跳过 %d 只无数据退市股)", len(live_codes), skipped)
            stock_codes = live_codes

        if not args.skip_financials:
            supplement_financials(conn, "balance_sheet", ak.stock_balance_sheet_by_report_em, BALANCE_SHEET_FIELD_MAP, stock_codes, args.dry_run)
            supplement_financials(conn, "income_statement", ak.stock_profit_sheet_by_report_em, INCOME_FIELD_MAP, stock_codes, args.dry_run)
            supplement_financials(conn, "cash_flow", ak.stock_cash_flow_sheet_by_report_em, CASH_FLOW_FIELD_MAP, stock_codes, args.dry_run)

        if not args.skip_dividends:
            supplement_dividends(conn, stock_codes, args.dry_run)

        if not args.dry_run:
            verify(conn)


if __name__ == "__main__":
    main()
