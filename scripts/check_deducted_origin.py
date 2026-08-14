"""深入检查扣非净利润的来源"""
import duckdb
from pathlib import Path

# 2026-08-14 红队 P3：不再依赖 CWD——锚定脚本所在仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "valuedashboard.duckdb"

conn = duckdb.connect(str(DB_PATH), read_only=True)

print("=== 扣非净利润来源调查 ===\n")

# 1. 统计各个比例的扣非净利润
print("【1. 扣非净利润值分布 (CSMAR 时期)】")
result = conn.execute("""
    SELECT 
        CASE 
            WHEN deducted_net_profit IS NULL THEN 'NULL'
            WHEN deducted_net_profit = 0 THEN '=0'
            WHEN deducted_net_profit > 0 THEN '>0'
            WHEN deducted_net_profit < 0 THEN '<0'
        END as value_range,
        COUNT(*) as cnt
    FROM income_statement
    WHERE report_date <= '2025-03-31'
    GROUP BY value_range
    ORDER BY cnt DESC
""").fetchall()

for r in result:
    print(f"  {r[0]:6s}: {r[1]:>10,} 行")

# 2. 检查扣非净利润和归母净利润是否一致
print("\n【2. 扣非净利润 vs 归母净利润】")
result = conn.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN deducted_net_profit = parent_net_profit THEN 1 ELSE 0 END) as same,
        SUM(CASE WHEN deducted_net_profit IS NOT NULL AND parent_net_profit IS NOT NULL AND deducted_net_profit != parent_net_profit THEN 1 ELSE 0 END) as different
    FROM income_statement
    WHERE report_date <= '2025-03-31'
      AND deducted_net_profit IS NOT NULL
""").fetchone()

print(f"  有扣非净利润的行数: {result[0]:,}")
print(f"  其中扣非=归母: {result[1]:,} ({result[1]/result[0]*100:.1f}%)")
print(f"  其中扣非≠归母: {result[2]:,} ({result[2]/result[0]*100:.1f}%)")

# 3. 抽查几行数据
print("\n【3. 抽查】")
samples = conn.execute("""
    SELECT stock_code, report_date, parent_net_profit, net_profit, deducted_net_profit
    FROM income_statement
    WHERE report_date <= '2025-03-31'
      AND deducted_net_profit IS NOT NULL
    ORDER BY report_date DESC
    LIMIT 10
""").fetchall()

for r in samples:
    print(f"  {r[0]} {r[1]}: 归母={r[2]:,.0f}, 净利润={r[3]:,.0f}, 扣非={r[4]:,.0f}")

conn.close()
