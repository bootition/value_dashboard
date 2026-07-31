"""CSV 数据完整性审查"""
import pandas as pd
from datetime import datetime

def audit_csv(name, path, pk_cols, key_cols, check_min_date=None):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    
    df = pd.read_csv(path, dtype={"stock_code": str})
    
    # 1. stock_code 格式
    bad_codes = df[~df["stock_code"].str.match(r'^\d{6}$')]
    if len(bad_codes) > 0:
        print(f"  [FAIL] stock_code 格式错误: {len(bad_codes)} 行")
        print(f"         示例: {bad_codes['stock_code'].unique()[:5]}")
    else:
        print(f"  [PASS] stock_code 格式正确 (全部6位)")
    
    # 2. 主键检查
    dup = df.duplicated(subset=pk_cols, keep=False)
    if dup.sum() > 0:
        print(f"  [FAIL] 重复主键: {dup.sum()} 行")
    else:
        print(f"  [PASS] 无重复主键")
    
    # 3. 行数和股票数
    print(f"  行数: {len(df):,}  股票数: {df['stock_code'].nunique():,}")
    
    # 4. 日期检查
    if "report_date" in df.columns:
        dates = pd.to_datetime(df["report_date"])
        print(f"  日期: {dates.min().date()} ~ {dates.max().date()}")
        if check_min_date and dates.min() <= pd.Timestamp(check_min_date):
            print(f"  [WARN] 发现早于 {check_min_date} 的数据")
    
    if "ex_date" in df.columns:
        placeholder = df[df["ex_date"].str.contains("12-31|06-30", na=False)]
        if len(placeholder) > 0:
            print(f"  [WARN] 占位日期: {len(placeholder)} 行")
    
    # 5. 关键字段非空率
    for col in key_cols:
        if col in df.columns:
            non_null = df[col].notna().sum()
            null_pct = (len(df) - non_null) / len(df) * 100
            status = "PASS" if null_pct < 10 else "WARN" if null_pct < 30 else "FAIL"
            print(f"  [{status}] {col}: {non_null}/{len(df)} 非空 ({null_pct:.1f}% NULL)")
    
    # 6. 抽样
    print(f"  抽样 (前3只股票):")
    for code in df["stock_code"].unique()[:3]:
        rows = df[df["stock_code"] == code]
        print(f"    {code}: {len(rows)} 行")


# 审查所有 CSV
audit_csv(
    "资产负债表", 
    "data/balance_sheet_akshare_temp.csv",
    pk_cols=["stock_code", "report_date"],
    key_cols=["total_assets", "total_liabilities", "total_equity"],
    check_min_date="2025-03-31",
)

audit_csv(
    "利润表", 
    "data/income_statement_akshare_temp.csv",
    pk_cols=["stock_code", "report_date"],
    key_cols=["revenue", "net_profit", "parent_net_profit", "deducted_net_profit"],
    check_min_date="2025-03-31",
)

audit_csv(
    "现金流量表", 
    "data/cash_flow_akshare_temp.csv",
    pk_cols=["stock_code", "report_date"],
    key_cols=["cf_from_operating", "cf_from_investing", "cf_from_financing"],
    check_min_date="2025-03-31",
)

audit_csv(
    "分红", 
    "data/dividends_akshare_temp.csv",
    pk_cols=["stock_code", "ex_date"],
    key_cols=["dividend_per_share", "ex_date"],
)

print(f"\n{'='*60}")
print(f"  审查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*60}")
