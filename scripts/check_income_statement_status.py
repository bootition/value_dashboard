"""检查数据库中利润表数据状态"""
import duckdb
from pathlib import Path

# 2026-08-14 红队 P3：不再依赖 CWD——锚定脚本所在仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "valuedashboard.duckdb"

conn = duckdb.connect(str(DB_PATH), read_only=True)

print("=== 数据库中利润表数据状态 ===\n")

# 1. 总体统计
print("【总体统计】")
result = conn.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN deducted_net_profit IS NOT NULL THEN 1 ELSE 0 END) as with_deducted,
        SUM(CASE WHEN deducted_net_profit IS NULL THEN 1 ELSE 0 END) as without_deducted
    FROM income_statement
""").fetchone()

print(f"  总行数: {result[0]:,}")
print(f"  有扣非净利润: {result[1]:,} ({result[1]/result[0]*100:.1f}%)")
print(f"  无扣非净利润: {result[2]:,} ({result[2]/result[0]*100:.1f}%)")

# 2. 按时间段统计
print("\n【按时间段统计】")
result = conn.execute("""
    SELECT 
        CASE 
            WHEN report_date > '2025-03-31' THEN 'AKShare 补充 (2025Q2+)'
            ELSE 'CSMAR 历史 (1990-2025Q1)'
        END as period,
        COUNT(*) as total,
        SUM(CASE WHEN deducted_net_profit IS NOT NULL THEN 1 ELSE 0 END) as with_deducted,
        SUM(CASE WHEN deducted_net_profit IS NULL THEN 1 ELSE 0 END) as without_deducted
    FROM income_statement
    GROUP BY period
    ORDER BY period
""").fetchall()

for r in result:
    print(f"\n  {r[0]}:")
    print(f"    总行数: {r[1]:,}")
    print(f"    有扣非净利润: {r[2]:,} ({r[2]/r[1]*100:.1f}%)")
    print(f"    无扣非净利润: {r[3]:,} ({r[3]/r[1]*100:.1f}%)")

# 3. 检查 CSMAR 历史数据中是否有扣非净利润字段
print("\n\n【CSMAR 历史数据检查】")
result = conn.execute("""
    SELECT stock_code, report_date, parent_net_profit, deducted_net_profit
    FROM income_statement
    WHERE report_date <= '2025-03-31'
      AND deducted_net_profit IS NOT NULL
    LIMIT 5
""").fetchall()

if result:
    print("  CSMAR 历史数据中有扣非净利润:")
    for r in result:
        parent = f"{r[2]:,.0f}" if r[2] else "NULL"
        deducted = f"{r[3]:,.0f}" if r[3] else "NULL"
        print(f"    {r[0]} {r[1]}: 归母={parent}, 扣非={deducted}")
else:
    print("  CSMAR 历史数据中没有扣非净利润")

# 4. 检查 AKShare 补充数据
print("\n\n【AKShare 补充数据检查】")
result = conn.execute("""
    SELECT stock_code, report_date, parent_net_profit, deducted_net_profit
    FROM income_statement
    WHERE report_date > '2025-03-31'
      AND deducted_net_profit IS NOT NULL
    ORDER BY report_date DESC
    LIMIT 10
""").fetchall()

print(f"  AKShare 补充数据中有扣非净利润的示例:")
for r in result:
    parent = f"{r[2]:,.0f}" if r[2] else "NULL"
    deducted = f"{r[3]:,.0f}" if r[3] else "NULL"
    print(f"    {r[0]} {r[1]}: 归母={parent}, 扣非={deducted}")

# 5. 检查数据完整性
print("\n\n【数据完整性检查】")
result = conn.execute("""
    SELECT 
        COUNT(DISTINCT stock_code) as stock_count,
        MIN(report_date) as min_date,
        MAX(report_date) as max_date
    FROM income_statement
""").fetchone()

print(f"  股票数量: {result[0]:,}")
print(f"  日期范围: {result[1]} ~ {result[2]}")

conn.close()
