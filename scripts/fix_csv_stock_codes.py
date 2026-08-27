"""修复 CSV 文件中 stock_code 列的格式问题

问题：pandas 在保存 CSV 时，stock_code 列被解析为整数，导致 000001 变成 1
修复：读取 CSV，将 stock_code 列转换为 6 位字符串格式，重新保存
"""

import pandas as pd
from pathlib import Path

def fix_csv_stock_codes(csv_path: Path) -> None:
    """修复单个 CSV 文件的 stock_code 格式"""
    if not csv_path.exists():
        print(f"文件不存在: {csv_path}")
        return
    
    print(f"处理: {csv_path.name}")
    
    # 读取 CSV，强制 stock_code 为字符串
    df = pd.read_csv(csv_path, dtype={"stock_code": str})
    
    # 修复 stock_code 格式
    if "stock_code" in df.columns:
        df["stock_code"] = df["stock_code"].str.zfill(6)
    
    # 重新保存
    df.to_csv(csv_path, index=False)
    print(f"  修复完成: {len(df)} 行")
    print(f"  股票示例: {df['stock_code'].unique()[:5]}")

def main():
    data_dir = Path(__file__).resolve().parents[1] / "data"
    
    csv_files = [
        "balance_sheet_akshare_temp.csv",
        "income_statement_akshare_temp.csv",
        "cash_flow_akshare_temp.csv",
        "dividends_akshare_temp.csv",
    ]
    
    for csv_file in csv_files:
        csv_path = data_dir / csv_file
        fix_csv_stock_codes(csv_path)
    
    print("\n所有文件修复完成")

if __name__ == "__main__":
    main()
