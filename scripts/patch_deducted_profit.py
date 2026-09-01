"""
补充扣非净利润数据

脚本功能：
1. 读取现有的 income_statement CSV 文件
2. 对每只股票重新调用 AKShare 接口获取扣非净利润
3. 更新 CSV 文件
4. 重新导入数据库

用法：
    python scripts/patch_deducted_profit.py [--sample N]
"""

import argparse
import sys
import time
from pathlib import Path

import akshare as ak
import pandas as pd

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CSMAR_CUTOFF = "2025-03-31"
RATE_LIMIT = 0.5


def _to_em_symbol(code: str) -> str:
    """将股票代码转换为东方财富格式"""
    code = code.strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"SH{code}"
    elif code.startswith(("0", "2", "3")):
        return f"SZ{code}"
    elif code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SH{code}"


def _strip_code(code: str) -> str:
    """标准化股票代码"""
    return code.strip().zfill(6)


def _parse_report_date(rd) -> str | None:
    """解析报告日期"""
    if rd is None or (isinstance(rd, float) and pd.isna(rd)):
        return None
    s = str(rd).strip()
    if len(s) >= 10:
        return s[:10]
    return None


def patch_deducted_profit(csv_path: Path, sample: int = 0) -> int:
    """补充扣非净利润数据"""
    print(f"\n=== 补充扣非净利润数据 ===")
    print(f"CSV 文件: {csv_path}")
    
    # 读取现有数据
    df = pd.read_csv(csv_path, dtype={"stock_code": str})
    df["stock_code"] = df["stock_code"].str.zfill(6)
    
    print(f"现有数据: {len(df)} 行, {df['stock_code'].nunique()} 只股票")
    
    # 获取需要处理的股票列表
    stock_codes = df['stock_code'].unique().tolist()
    if sample > 0:
        stock_codes = stock_codes[:sample]
        print(f"样本模式: 只处理前 {sample} 只股票")
    
    # 添加扣非净利润列（如果不存在）
    if "deducted_net_profit" not in df.columns:
        df["deducted_net_profit"] = None
        print("已添加 deducted_net_profit 列")
    
    # 逐只股票获取扣非净利润
    updated_count = 0
    errors = 0
    
    for i, code in enumerate(stock_codes):
        em_symbol = _to_em_symbol(code)
        
        try:
            time.sleep(RATE_LIMIT)
            api_df = ak.stock_profit_sheet_by_report_em(symbol=em_symbol)
            
            if api_df is None or len(api_df) == 0:
                continue
            
            # 提取扣非净利润数据
            for _, row in api_df.iterrows():
                rd = _parse_report_date(row.get("REPORT_DATE"))
                if rd is None or rd <= CSMAR_CUTOFF:
                    continue
                
                deducted = row.get("DEDUCT_PARENT_NETPROFIT")
                if deducted is not None and not pd.isna(deducted):
                    # 更新 DataFrame
                    mask = (df["stock_code"] == code) & (df["report_date"] == rd)
                    if mask.any():
                        df.loc[mask, "deducted_net_profit"] = float(deducted)
                        updated_count += 1
            
            if (i + 1) % 50 == 0:
                print(f"  进度: {i + 1}/{len(stock_codes)}, 已更新 {updated_count} 条")
        
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  [WARN] {code} 失败: {e}")
    
    print(f"\n完成: 更新 {updated_count} 条, {errors} 个错误")
    
    # 保存更新后的数据
    df.to_csv(csv_path, index=False)
    print(f"已保存到 {csv_path}")
    
    return updated_count


def import_to_db(store: DuckDBStore, csv_path: Path):
    """导入更新后的数据到数据库"""
    print(f"\n=== 导入数据库 ===")
    
    with store.transaction() as conn:
        # 读取 CSV
        df = pd.read_csv(csv_path, dtype={"stock_code": str})
        df["stock_code"] = df["stock_code"].str.zfill(6)
        
        print(f"读取 {len(df)} 行")
        
        # 填充缺失字段
        if "report_type" not in df.columns:
            df["report_type"] = None
        if "raw_data" not in df.columns:
            df["raw_data"] = None
        
        # 删除旧数据
        conn.execute("DELETE FROM income_statement WHERE report_date > '2025-03-31'")
        print("已删除旧数据")
        
        # 导入新数据
        conn.execute("""
            INSERT INTO income_statement (
                stock_code, report_date, report_type,
                total_operating_revenue, revenue, total_operating_cost, cost_of_revenue,
                taxes_and_surcharges, selling_expenses, administrative_expenses,
                rd_expenses, financial_expenses, interest_expense, interest_income,
                asset_impairment_loss, credit_impairment_loss, exchange_gain,
                investment_income, operating_profit, non_operating_income,
                non_operating_expenses, total_profit, income_tax, net_profit,
                parent_net_profit, minority_shareholder_profit, deducted_net_profit,
                basic_eps, diluted_eps, raw_data
            ) SELECT * FROM df
        """)
        
        count = conn.execute("SELECT COUNT(*) FROM income_statement WHERE report_date > '2025-03-31'").fetchone()[0]
        print(f"导入完成: {count} 行")
        
        # 验证
        print("\n=== 验证 ===")
        sample = conn.execute("""
            SELECT stock_code, report_date, parent_net_profit, deducted_net_profit
            FROM income_statement
            WHERE report_date > '2025-03-31'
              AND deducted_net_profit IS NOT NULL
            ORDER BY report_date DESC
            LIMIT 5
        """).fetchall()
        
        print("扣非净利润数据示例:")
        for row in sample:
            parent = f"{row[2]:,.0f}" if row[2] else "NULL"
            deducted = f"{row[3]:,.0f}" if row[3] else "NULL"
            print(f"  {row[0]} {row[1]}: 归母={parent}, 扣非={deducted}")
        
        # 统计
        total = conn.execute("SELECT COUNT(*) FROM income_statement WHERE report_date > '2025-03-31'").fetchone()[0]
        with_deducted = conn.execute("SELECT COUNT(*) FROM income_statement WHERE report_date > '2025-03-31' AND deducted_net_profit IS NOT NULL").fetchone()[0]
        
        print(f"\n统计: {with_deducted} / {total} 行有扣非净利润 ({with_deducted/total*100:.1f}%)")
        
def main():
    parser = argparse.ArgumentParser(description="补充扣非净利润数据")
    parser.add_argument("--db", type=Path, help="DuckDB 路径（必须与已验证运行环境一致）")
    parser.add_argument("--sample", type=int, default=0, help="只处理前 N 只股票（0=全部）")
    # 2026-08-14 红队 P2：破坏性脚本显式确认 + 写前自动备份
    parser.add_argument("--yes", action="store_true", help="确认修改正式数据库（无此参数则交互确认）")
    args = parser.parse_args()
    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))
    if args.db is not None and args.db.resolve(strict=False) != paths.duckdb_path:
        parser.error("--db must match the validated VD_DUCKDB_PATH")
    
    csv_path = PROJECT_ROOT / "data" / "income_statement_akshare_temp.csv"
    
    if not csv_path.exists():
        print(f"[ERROR] CSV 文件不存在: {csv_path}")
        return

    from _maintenance_safety import backup_tables, confirm_destructive

    if not confirm_destructive(args.yes):
        sys.exit(2)
    store = DuckDBStore(paths=paths)
    backup_tables(store, ["income_statement"], tag="patch-deducted-profit")
    
    # 补充扣非净利润数据
    updated = patch_deducted_profit(csv_path, args.sample)
    
    if updated > 0:
        # 导入数据库
        import_to_db(store, csv_path)
        print("\n[OK] 完成")
    else:
        print("\n[WARN] 没有更新任何数据")


if __name__ == "__main__":
    main()
