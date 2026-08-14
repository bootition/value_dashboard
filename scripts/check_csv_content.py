"""检查 CSV 文件的实际内容"""
import pandas as pd
from pathlib import Path

# 2026-08-14 红队 P3：不再依赖 CWD——锚定脚本所在仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[1]

df = pd.read_csv(PROJECT_ROOT / "data" / "income_statement_akshare_temp.csv")

print("CSV 列数:", len(df.columns))
print("\n列名:")
for i, col in enumerate(df.columns, 1):
    print(f"  {i:2d}. {col}")

print("\n前3行数据:")
print(df.head(3).to_string())

# 检查特定股票的数据
print("\n\n000001 的数据:")
stock_data = df[df['stock_code'] == '000001']
print(stock_data.to_string())
