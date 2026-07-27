"""直接计算指标快照"""
import duckdb
from datetime import datetime

conn = duckdb.connect('data/valuedashboard.duckdb')
print(f"开始: {datetime.now()}")

sql = """
SELECT 
    b.stock_code,
    b.report_date,
    -- 估值
    CASE WHEN p.close > 0 AND COALESCE(b.paid_in_capital,0) > 0 AND i.parent_net_profit > 0 
         THEN p.close * COALESCE(b.paid_in_capital,0) / i.parent_net_profit END as pe_ttm,
    CASE WHEN p.close > 0 AND COALESCE(b.paid_in_capital,0) > 0 AND b.total_equity > 0 
         THEN p.close * COALESCE(b.paid_in_capital,0) / b.total_equity END as pb_mrq,
    CASE WHEN p.close > 0 AND COALESCE(b.paid_in_capital,0) > 0 AND i.revenue > 0 
         THEN p.close * COALESCE(b.paid_in_capital,0) / i.revenue END as ps_ttm,
    CASE WHEN p.close > 0 AND COALESCE(b.paid_in_capital,0) > 0 AND c.cf_from_operating > 0
         THEN p.close * COALESCE(b.paid_in_capital,0) / c.cf_from_operating END as pcf_ttm,
    CASE WHEN p.close > 0 AND d.dps > 0 THEN d.dps / p.close END as dividend_yield,
    p.close * COALESCE(b.paid_in_capital,0) as total_market_cap,
    p.close * COALESCE(b.paid_in_capital,0) as circ_market_cap,
    -- 盈利
    CASE WHEN b.total_equity > 0 THEN i.parent_net_profit / b.total_equity END as roe,
    CASE WHEN b.total_assets > 0 THEN i.net_profit / b.total_assets END as roa,
    CASE WHEN i.revenue > 0 THEN (i.revenue - COALESCE(i.cost_of_revenue,0)) / i.revenue END as gross_margin,
    CASE WHEN i.revenue > 0 THEN i.net_profit / i.revenue END as net_margin,
    CASE WHEN b.total_equity + b.total_liabilities > 0 THEN i.net_profit / (b.total_equity + b.total_liabilities) END as roic,
    CASE WHEN i.net_profit != 0 THEN c.cf_from_operating / NULLIF(i.net_profit,0) END as cf_to_net_profit,
    -- 安全
    CASE WHEN b.total_assets > 0 THEN b.total_liabilities / b.total_assets END as debt_ratio,
    CASE WHEN b.total_current_liabilities > 0 THEN b.total_current_assets / b.total_current_liabilities END as current_ratio,
    CASE WHEN b.total_current_liabilities > 0 THEN (b.total_current_assets - COALESCE(b.inventory,0)) / b.total_current_liabilities END as quick_ratio,
    CASE WHEN b.total_assets > 0 THEN COALESCE(b.goodwill,0) / b.total_assets END as goodwill_ratio,
    -- 股息
    d.dps,
    NULL as consecutive_div_years,
    -- 行情
    p.close as latest_close,
    p.trade_date as latest_price_date,
    CURRENT_TIMESTAMP as calculated_at,
    'v2' as data_version
FROM balance_sheet b
INNER JOIN (
    SELECT stock_code, MAX(report_date) as max_date
    FROM balance_sheet WHERE total_assets > 0
    GROUP BY stock_code
) latest ON b.stock_code = latest.stock_code AND b.report_date = latest.max_date
LEFT JOIN income_statement i ON b.stock_code = i.stock_code AND b.report_date = i.report_date
LEFT JOIN cash_flow c ON b.stock_code = c.stock_code AND b.report_date = c.report_date
LEFT JOIN (
    SELECT stock_code, close, trade_date
    FROM price_daily_raw
    WHERE (stock_code, trade_date) IN (
        SELECT stock_code, MAX(trade_date) FROM price_daily_raw GROUP BY stock_code
    )
) p ON b.stock_code = p.stock_code
LEFT JOIN (
    SELECT stock_code, SUM(dividend_per_share) as dps
    FROM dividends
    WHERE CAST(ex_date AS VARCHAR) NOT LIKE '%12-31'
      AND CAST(ex_date AS VARCHAR) NOT LIKE '%06-30'
      AND ex_date >= '2025-01-01'
    GROUP BY stock_code
) d ON b.stock_code = d.stock_code
"""

conn.execute("DELETE FROM indicator_snapshot")
conn.execute("INSERT INTO indicator_snapshot BY NAME " + sql)
count = conn.execute("SELECT COUNT(*) FROM indicator_snapshot").fetchone()[0]

# 抽样
s = conn.execute("""
    SELECT stock_code, report_date, pe_ttm, pb_mrq, roe, dividend_yield 
    FROM indicator_snapshot WHERE stock_code='600519'
""").fetchone()
if s:
    print(f"茅台: PE={s[2]:.1f}, PB={s[3]:.1f}, ROE={s[4]:.1%}" if s[2] else f"茅台: PE=N/A")

print(f"指标快照: {count} 行")
print(f"完成: {datetime.now()}")
conn.close()
