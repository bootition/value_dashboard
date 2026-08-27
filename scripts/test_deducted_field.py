"""
测试 AKShare 利润表接口是否返回扣非净利润
"""
import akshare as ak
import pandas as pd

print("测试 AKShare 利润表接口...\n")

# 测试单只股票
test_stock = "000001"

try:
    print(f"获取 {test_stock} 的利润表数据...")
    df = ak.stock_profit_sheet_by_report_em(symbol=f"SZ{test_stock}")
    
    print(f"总列数: {len(df.columns)}")
    print(f"总行数: {len(df)}")
    
    # 检查是否有扣非净利润字段
    if 'DEDUCT_PARENT_NETPROFIT' in df.columns:
        print(f"\n[OK] 找到扣非净利润字段: DEDUCT_PARENT_NETPROFIT")
        
        # 显示前几行的扣非净利润数据
        sample = df[['SECURITY_CODE', 'REPORT_DATE', 'PARENT_NETPROFIT', 'DEDUCT_PARENT_NETPROFIT']].head(10)
        print(f"\n前10行数据:")
        print(sample.to_string(index=False))
        
        # 统计非空值
        non_null = df['DEDUCT_PARENT_NETPROFIT'].notna().sum()
        print(f"\n非空值数量: {non_null} / {len(df)}")
    else:
        print(f"\n[FAIL] 未找到扣非净利润字段")
        print(f"可用字段: {list(df.columns)}")
    
except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
