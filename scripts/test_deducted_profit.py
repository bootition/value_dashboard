"""
测试 AKShare 是否提供扣非净利润数据
"""
import akshare as ak
import pandas as pd

print("测试 AKShare 扣非净利润接口...\n")

# 测试单只股票
test_stock = "000001"

try:
    print(f"1. 测试 stock_profit_sheet_by_report_em (利润表)")
    df = ak.stock_profit_sheet_by_report_em(symbol=f"SZ{test_stock}")
    print(f"   列数: {len(df.columns)}")
    
    # 查找扣非净利润相关字段
    deducted_cols = [col for col in df.columns if 'DEDUCT' in col.upper() or 'NET' in col.upper()]
    print(f"   相关字段: {deducted_cols[:10]}")
    
    # 显示所有列名
    print(f"\n   所有列名:")
    for i, col in enumerate(df.columns, 1):
        print(f"   {i:2d}. {col}")
    
except Exception as e:
    print(f"   错误: {e}")

print("\n" + "="*70)

try:
    print(f"\n2. 测试 stock_financial_analysis_indicator (财务指标)")
    df2 = ak.stock_financial_analysis_indicator(symbol=f"SZ{test_stock}")
    print(f"   列数: {len(df2.columns)}")
    
    # 查找扣非净利润相关字段
    deducted_cols2 = [col for col in df2.columns if 'DEDUCT' in col.upper() or 'NET' in col.upper()]
    print(f"   相关字段: {deducted_cols2}")
    
    # 显示前20列
    print(f"\n   前20列:")
    for i, col in enumerate(df2.columns[:20], 1):
        print(f"   {i:2d}. {col}")
    
except Exception as e:
    print(f"   错误: {e}")

print("\n" + "="*70)

# 检查是否有专门的扣非净利润接口
try:
    print(f"\n3. 检查是否有扣非净利润专用接口")
    # 尝试查找相关函数
    funcs = [f for f in dir(ak) if 'deduct' in f.lower() or 'profit' in f.lower()]
    print(f"   相关函数: {funcs[:20]}")
except Exception as e:
    print(f"   错误: {e}")
