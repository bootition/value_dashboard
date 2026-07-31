"""检查 AKShare 补充数据的字段映射"""
import duckdb

conn = duckdb.connect('data/valuedashboard.duckdb', read_only=True)

print("=== AKShare 补充数据字段检查 ===\n")

result = conn.execute("""
    SELECT 
        stock_code,
        report_date,
        revenue,
        total_operating_revenue,
        net_profit,
        parent_net_profit,
        deducted_net_profit,
        basic_eps
    FROM income_statement
    WHERE report_date > '2025-03-31'
    ORDER BY report_date DESC
    LIMIT 10
""").fetchall()

print("字段值示例:")
for r in result:
    print(f"\n  {r[0]} {r[1]}:")
    print(f"    revenue: {r[2]}")
    print(f"    total_operating_revenue: {r[3]}")
    print(f"    net_profit: {r[4]}")
    print(f"    parent_net_profit: {r[5]}")
    print(f"    deducted_net_profit: {r[6]}")
    print(f"    basic_eps: {r[7]}")

conn.close()
