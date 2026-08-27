"""
数据质量审查脚本 - 检查 CSV 文件是否可以直接导入数据库
"""
import pandas as pd
import json
from pathlib import Path

def check_balance_sheet():
    print("=" * 70)
    print("检查资产负债表 (balance_sheet)")
    print("=" * 70)
    
    df = pd.read_csv('data/balance_sheet_akshare_temp.csv', dtype={'stock_code': str})
    
    # 数据库 schema 定义的列
    schema_columns = [
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
    
    csv_columns = list(df.columns)
    
    # 检查列匹配
    missing_in_csv = set(schema_columns) - set(csv_columns)
    extra_in_csv = set(csv_columns) - set(schema_columns)
    
    print(f"\nCSV 行数: {len(df):,}")
    print(f"CSV 股票数: {df['stock_code'].nunique():,}")
    print(f"CSV 列数: {len(csv_columns)}")
    print(f"Schema 列数: {len(schema_columns)}")
    
    if missing_in_csv:
        print(f"\n❌ CSV 缺失的列: {missing_in_csv}")
    else:
        print(f"\n✅ 所有 schema 列都存在")
    
    if extra_in_csv:
        print(f"\n⚠️  CSV 额外的列: {extra_in_csv}")
    
    # 检查主键
    print(f"\n主键检查:")
    print(f"  stock_code NULL 值: {df['stock_code'].isna().sum()}")
    print(f"  report_date NULL 值: {df['report_date'].isna().sum()}")
    
    # 检查重复主键
    duplicates = df.duplicated(subset=['stock_code', 'report_date'], keep=False)
    dup_count = duplicates.sum()
    if dup_count > 0:
        print(f"  ❌ 重复主键: {dup_count} 行")
        print(f"     示例: {df[duplicates][['stock_code', 'report_date']].head(3).to_string()}")
    else:
        print(f"  ✅ 无重复主键")
    
    # 检查日期格式
    print(f"\n日期格式检查:")
    try:
        pd.to_datetime(df['report_date'])
        print(f"  ✅ report_date 格式正确")
    except Exception as e:
        print(f"  ❌ report_date 格式错误: {e}")
    
    # 检查数值列
    print(f"\n数值列检查:")
    numeric_cols = ['total_assets', 'total_liabilities', 'total_equity']
    for col in numeric_cols:
        if col in df.columns:
            null_count = df[col].isna().sum()
            print(f"  {col}: NULL={null_count} ({null_count/len(df)*100:.1f}%)")
    
    # 抽样检查
    print(f"\n数据抽样:")
    sample = df.sample(min(3, len(df)))
    for _, row in sample.iterrows():
        print(f"  {row['stock_code']} {row['report_date']}: 总资产={row.get('total_assets', 'N/A')}")
    
    return len(missing_in_csv) == 0 and dup_count == 0

def check_income_statement():
    print("\n" + "=" * 70)
    print("检查利润表 (income_statement)")
    print("=" * 70)
    
    df = pd.read_csv('data/income_statement_akshare_temp.csv', dtype={'stock_code': str})
    
    schema_columns = [
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
    
    csv_columns = list(df.columns)
    missing_in_csv = set(schema_columns) - set(csv_columns)
    extra_in_csv = set(csv_columns) - set(schema_columns)
    
    print(f"\nCSV 行数: {len(df):,}")
    print(f"CSV 股票数: {df['stock_code'].nunique():,}")
    
    if missing_in_csv:
        print(f"\n❌ CSV 缺失的列: {missing_in_csv}")
    else:
        print(f"\n✅ 所有 schema 列都存在")
    
    if extra_in_csv:
        print(f"\n⚠️  CSV 额外的列: {extra_in_csv}")
    
    # 检查主键
    duplicates = df.duplicated(subset=['stock_code', 'report_date'], keep=False)
    dup_count = duplicates.sum()
    if dup_count > 0:
        print(f"\n❌ 重复主键: {dup_count} 行")
    else:
        print(f"\n✅ 无重复主键")
    
    # 检查关键字段
    print(f"\n关键字段检查:")
    for col in ['revenue', 'net_profit', 'parent_net_profit']:
        if col in df.columns:
            null_count = df[col].isna().sum()
            print(f"  {col}: NULL={null_count} ({null_count/len(df)*100:.1f}%)")
    
    return len(missing_in_csv) == 0 and dup_count == 0

def check_cash_flow():
    print("\n" + "=" * 70)
    print("检查现金流量表 (cash_flow)")
    print("=" * 70)
    
    df = pd.read_csv('data/cash_flow_akshare_temp.csv', dtype={'stock_code': str})
    
    schema_columns = [
        'stock_code', 'report_date', 'report_type',
        'cash_received_sales', 'taxes_refunded', 'other_operating_cf_in',
        'total_operating_cf_in', 'cash_paid_goods', 'cash_paid_employees',
        'cash_paid_taxes', 'other_operating_cf_out', 'total_operating_cf_out',
        'cf_from_operating', 'cf_from_investing', 'cf_from_financing',
        'exchange_rate_effect', 'cf_net', 'cash_beginning', 'cash_ending', 'raw_data'
    ]
    
    csv_columns = list(df.columns)
    missing_in_csv = set(schema_columns) - set(csv_columns)
    extra_in_csv = set(csv_columns) - set(schema_columns)
    
    print(f"\nCSV 行数: {len(df):,}")
    print(f"CSV 股票数: {df['stock_code'].nunique():,}")
    
    if missing_in_csv:
        print(f"\n❌ CSV 缺失的列: {missing_in_csv}")
    else:
        print(f"\n✅ 所有 schema 列都存在")
    
    if extra_in_csv:
        print(f"\n⚠️  CSV 额外的列: {extra_in_csv}")
    
    duplicates = df.duplicated(subset=['stock_code', 'report_date'], keep=False)
    dup_count = duplicates.sum()
    if dup_count > 0:
        print(f"\n❌ 重复主键: {dup_count} 行")
    else:
        print(f"\n✅ 无重复主键")
    
    return len(missing_in_csv) == 0 and dup_count == 0

def check_dividends():
    print("\n" + "=" * 70)
    print("检查分红表 (dividends)")
    print("=" * 70)
    
    df = pd.read_csv('data/dividends_akshare_temp.csv', dtype={'stock_code': str})
    
    schema_columns = [
        'stock_code', 'ex_date', 'announcement_date',
        'dividend_per_share', 'stock_dividend', 'transfer_share',
        'rights_issue', 'rights_issue_price'
    ]
    
    csv_columns = list(df.columns)
    missing_in_csv = set(schema_columns) - set(csv_columns)
    extra_in_csv = set(csv_columns) - set(schema_columns)
    
    print(f"\nCSV 行数: {len(df):,}")
    print(f"CSV 股票数: {df['stock_code'].nunique():,}")
    
    if missing_in_csv:
        print(f"\n❌ CSV 缺失的列: {missing_in_csv}")
    else:
        print(f"\n✅ 所有 schema 列都存在")
    
    if extra_in_csv:
        print(f"\n⚠️  CSV 额外的列: {extra_in_csv}")
    
    # 检查日期格式
    print(f"\n日期格式检查:")
    try:
        pd.to_datetime(df['ex_date'])
        print(f"  ✅ ex_date 格式正确")
    except Exception as e:
        print(f"  ❌ ex_date 格式错误: {e}")
    
    # 检查占位日期
    placeholder = df[df['ex_date'].str.contains('12-31|06-30', na=False)]
    if len(placeholder) > 0:
        print(f"\n⚠️  发现占位日期: {len(placeholder)} 行")
    else:
        print(f"\n✅ 无占位日期")
    
    return len(missing_in_csv) == 0

def main():
    print("\n" + "=" * 70)
    print("数据质量审查 - 数据库导入兼容性检查")
    print("=" * 70)
    
    results = {}
    results['balance_sheet'] = check_balance_sheet()
    results['income_statement'] = check_income_statement()
    results['cash_flow'] = check_cash_flow()
    results['dividends'] = check_dividends()
    
    print("\n" + "=" * 70)
    print("审查结果汇总")
    print("=" * 70)
    
    all_passed = True
    for table, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"  {table}: {status}")
        if not passed:
            all_passed = False
    
    print("\n" + "=" * 70)
    if all_passed:
        print("✅ 所有表都可以直接导入数据库")
    else:
        print("❌ 部分表存在问题，需要先修复")
    print("=" * 70)

if __name__ == "__main__":
    main()
