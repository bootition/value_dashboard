"""验证扣非净利润数据"""
import duckdb

conn = duckdb.connect('data/valuedashboard.duckdb', read_only=True)

print("扣非净利润统计:")
result = conn.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN deducted_net_profit IS NOT NULL THEN 1 ELSE 0 END) as with_deducted
    FROM income_statement 
    WHERE report_date > '2025-03-31'
""").fetchone()

print(f"  总行数: {result[0]}")
print(f"  有扣非净利润: {result[1]} ({result[1]/result[0]*100:.1f}%)")

print("\n示例数据:")
sample = conn.execute("""
    SELECT stock_code, report_date, parent_net_profit, deducted_net_profit
    FROM income_statement
    WHERE report_date > '2025-03-31'
      AND deducted_net_profit IS NOT NULL
    ORDER BY report_date DESC
    LIMIT 5
""").fetchall()

for r in sample:
    parent = f"{r[2]:,.0f}" if r[2] else "NULL"
    deducted = f"{r[3]:,.0f}" if r[3] else "NULL"
    print(f"  {r[0]} {r[1]}: 归母={parent}, 扣非={deducted}")

conn.close()
