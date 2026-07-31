"""数据完整性验证脚本

验证 CSMAR 导入 + AKShare 补齐后的数据质量：
1. 检查各表行数
2. 检查关键日期范围
3. 检查空壳行比例
4. 抽样验证茅台等代表性股票
5. 检查分红日期是否已修正

用法:
    python scripts/verify_data_completeness.py [--db PATH]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb

DEFAULT_DB = PROJECT_ROOT / "data" / "valuedashboard.duckdb"
CSMAR_CUTOFF = "2025-03-31"


def check_table_counts(conn: duckdb.DuckDBPyConnection) -> None:
    """检查各表行数"""
    print("\n=== 表行数 ===")
    tables = ["stock_meta", "balance_sheet", "income_statement", "cash_flow", "dividends"]
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:20s}: {count:>10,} 行")


def check_date_ranges(conn: duckdb.DuckDBPyConnection) -> None:
    """检查关键日期范围"""
    print("\n=== 日期范围 ===")
    for table in ["balance_sheet", "income_statement", "cash_flow"]:
        row = conn.execute(f"""
            SELECT MIN(report_date), MAX(report_date), COUNT(DISTINCT stock_code)
            FROM {table}
        """).fetchone()
        print(f"  {table:20s}: {row[0]} ~ {row[1]} ({row[2]} 只股票)")

    row = conn.execute("""
        SELECT MIN(ex_date), MAX(ex_date), COUNT(DISTINCT stock_code)
        FROM dividends
    """).fetchone()
    print(f"  {'dividends':20s}: {row[0]} ~ {row[1]} ({row[2]} 只股票)")


def check_shell_rows(conn: duckdb.DuckDBPyConnection) -> None:
    """检查空壳行比例"""
    print("\n=== 空壳行检查 ===")
    for table in ["balance_sheet", "income_statement", "cash_flow"]:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if table == "balance_sheet":
            shell = conn.execute(f"""
                SELECT COUNT(*) FROM {table}
                WHERE total_assets IS NULL OR total_assets = 0
            """).fetchone()[0]
        elif table == "income_statement":
            shell = conn.execute(f"""
                SELECT COUNT(*) FROM {table}
                WHERE revenue IS NULL OR revenue = 0
            """).fetchone()[0]
        else:
            shell = conn.execute(f"""
                SELECT COUNT(*) FROM {table}
                WHERE cf_from_operating IS NULL
            """).fetchone()[0]
        ratio = shell / total * 100 if total > 0 else 0
        status = "✓" if ratio < 5 else "⚠"
        print(f"  {table:20s}: {shell:>6,} / {total:>6,} ({ratio:5.2f}%) {status}")


def check_new_data(conn: duckdb.DuckDBPyConnection) -> None:
    """检查 2025Q2+ 新增数据"""
    print(f"\n=== 2025Q2+ 新增数据 ===")
    for table in ["balance_sheet", "income_statement", "cash_flow"]:
        count = conn.execute(f"""
            SELECT COUNT(*) FROM {table}
            WHERE report_date > '{CSMAR_CUTOFF}'
        """).fetchone()[0]
        stocks = conn.execute(f"""
            SELECT COUNT(DISTINCT stock_code) FROM {table}
            WHERE report_date > '{CSMAR_CUTOFF}'
        """).fetchone()[0]
        print(f"  {table:20s}: {count:>6,} 行, {stocks:>4} 只股票")


def check_dividend_dates(conn: duckdb.DuckDBPyConnection) -> None:
    """检查分红日期修正情况"""
    print("\n=== 分红日期修正 ===")
    total = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
    placeholder = conn.execute("""
        SELECT COUNT(*) FROM dividends
        WHERE CAST(ex_date AS VARCHAR) LIKE '%12-31'
           OR CAST(ex_date AS VARCHAR) LIKE '%06-30'
    """).fetchone()[0]
    real_dates = total - placeholder
    ratio = real_dates / total * 100 if total > 0 else 0
    print(f"  总计: {total:,} 条")
    print(f"  真实除权日: {real_dates:,} 条 ({ratio:.1f}%)")
    print(f"  占位日期: {placeholder:,} 条 ({100-ratio:.1f}%)")


def check_sample_stocks(conn: duckdb.DuckDBPyConnection) -> None:
    """抽样验证代表性股票"""
    print("\n=== 抽样验证 ===")
    samples = [
        ("600519", "贵州茅台"),
        ("000858", "五粮液"),
        ("000001", "平安银行"),
        ("601318", "中国平安"),
    ]
    for code, name in samples:
        row = conn.execute(f"""
            SELECT report_date, total_assets, revenue, net_profit
            FROM balance_sheet
            WHERE stock_code = '{code}'
            ORDER BY report_date DESC
            LIMIT 1
        """).fetchone()
        if row:
            print(f"  {code} {name}:")
            print(f"    最新财报: {row[0]}")
            print(f"    总资产: {row[1]:,.0f}" if row[1] else "    总资产: NULL")
            print(f"    营收: {row[2]:,.0f}" if row[2] else "    营收: NULL")
            print(f"    净利润: {row[3]:,.0f}" if row[3] else "    净利润: NULL")
        else:
            print(f"  {code} {name}: 无数据")


def main():
    parser = argparse.ArgumentParser(description="数据完整性验证")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    print(f"数据库: {args.db}")
    print(f"验证时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    conn = duckdb.connect(str(args.db), read_only=True)
    try:
        check_table_counts(conn)
        check_date_ranges(conn)
        check_shell_rows(conn)
        check_new_data(conn)
        check_dividend_dates(conn)
        check_sample_stocks(conn)

        print("\n=== 验证完成 ===")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
