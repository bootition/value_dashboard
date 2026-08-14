"""调查 2: 分红数据"""
import duckdb
import pandas as pd
from pathlib import Path

# 2026-08-14 红队 P3：不再依赖 CWD——锚定脚本所在仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "valuedashboard.duckdb"

conn = duckdb.connect(str(DB_PATH), read_only=True)

total = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
stocks = conn.execute("SELECT COUNT(DISTINCT stock_code) FROM dividends").fetchone()[0]
placeholder = conn.execute("""
    SELECT COUNT(*) FROM dividends 
    WHERE CAST(ex_date AS VARCHAR) LIKE '%12-31' 
       OR CAST(ex_date AS VARCHAR) LIKE '%06-30'
""").fetchone()[0]

print(f"数据库分红表: {total:,} 行, {stocks:,} 只股票")
print(f"  真实日期: {total-placeholder:,} 行")
print(f"  占位日期: {placeholder:,} 行")

print("\n样本:")
for r in conn.execute("""
    SELECT stock_code, ex_date, dividend_per_share 
    FROM dividends ORDER BY ex_date DESC LIMIT 5
""").fetchall():
    print(f"  {r[0]} {r[1]}: dps={r[2]}")

conn.close()

# 检查 CSMAR FI_T11.dta 文件
csmar_dividend_path = (
    PROJECT_ROOT / "额外资料" / "C17 a股上市公司财务数据合集（90-25年）"
    / "原始数据（dta格式）" / "股利分配" / "FI_T11.dta"
)
if csmar_dividend_path.exists():
    print(f"\nCSMAR 分红文件: {csmar_dividend_path.stat().st_size / 1e6:.1f} MB")
    reader = pd.read_stata(csmar_dividend_path, iterator=True, chunksize=10)
    sample = reader.read()
    print(f"字段数: {len(sample.columns)}")
    print(f"前5行:")
    print(sample.head(5).to_string())
    reader.close()
