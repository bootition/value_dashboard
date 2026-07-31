"""检查 CSMAR 利润表 .dta 文件中的字段"""
import pandas as pd
from pathlib import Path

# CSMAR 利润表文件路径
csmar_income_path = Path(r'D:\Mr.Q\掌控经济\value-dashboard\额外资料\C17 a股上市公司财务数据合集（90-25年）\原始数据（dta格式）\利润表\FS_Comins.dta')

print(f"检查 CSMAR 利润表文件: {csmar_income_path.name}")
print(f"文件大小: {csmar_income_path.stat().st_size / 1e6:.1f} MB\n")

# 使用 iterator 读取前10行
print("读取前10行数据...")
reader = pd.read_stata(csmar_income_path, iterator=True, chunksize=10)
sample = reader.read()
reader.close()

# 获取字段列表
print("=== 利润表字段列表 ===")
columns = sample.columns
for i, col in enumerate(columns, 1):
    print(f"{i:3d}. {col}")

print(f"\n总字段数: {len(columns)}")

# 查找扣非净利润相关字段
print("\n=== 查找扣非净利润相关字段 ===")
deduct_keywords = ['deduct', 'netprofit', 'net', 'profit']
found = []
for col in columns:
    col_lower = col.lower()
    if any(keyword in col_lower for keyword in deduct_keywords):
        found.append(col)
        print(f"  可能相关: {col}")

if not found:
    print("  未找到相关字段")

# 显示样本数据
print("\n=== 样本数据（前3行）===")
print(sample.head(3).to_string())
