"""全面审查：数据库、代码、配置、前端"""
import duckdb, sys
from pathlib import Path

PROJECT = Path('D:/Mr.Q/掌控经济/value-dashboard')
sys.path.insert(0, str(PROJECT))

print("=" * 60)
print("  全面审查 - 距正式使用还缺什么")
print("=" * 60)

# ═══════════════════════════════════════
# 1. 数据库
# ═══════════════════════════════════════
print("\n### 1. 数据库 ###")
c = duckdb.connect(str(PROJECT / 'data' / 'valuedashboard.duckdb'), read_only=True)

tables = c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
for t in tables:
    name = t[0]
    cnt = c.execute(f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0]
    print(f"  {name:25s} {cnt:>10,}")

# 检查每个表的数据范围
print("\n  数据范围:")
for t in ['stock_meta', 'price_daily_raw', 'price_daily_qfq', 'indicator_snapshot']:
    if t == 'stock_meta':
        r = c.execute(f"SELECT COUNT(*), COUNT(DISTINCT exchange) FROM {t}").fetchone()
        print(f"  {t}: {r[0]}只, {r[1]}个交易所")
    elif 'price' in t:
        r = c.execute(f"SELECT COUNT(*), MIN(trade_date), MAX(trade_date), COUNT(DISTINCT stock_code) FROM {t}").fetchone()
        print(f"  {t}: {r[0]}行, {r[1]}~{r[2]}, {r[3]}只")
    elif t == 'indicator_snapshot':
        r = c.execute(f"SELECT COUNT(*), COUNT(DISTINCT stock_code), MIN(calculated_at), MAX(calculated_at) FROM {t}").fetchone()
        print(f"  {t}: {r[0]}行, {r[1]}只, calc={r[2]}~{r[3]}")

# 检查 stock_meta 缺失字段
print("\n  stock_meta 缺失率:")
for col in ['listing_date', 'sw_level1', 'sw_level2', 'is_st', 'is_suspended', 'pinyin']:
    nulls = c.execute(f"SELECT COUNT(*) FROM stock_meta WHERE {col} IS NULL").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM stock_meta").fetchone()[0]
    pct = nulls/total*100
    flag = "  !!!" if pct > 80 else ""
    print(f"    {col:20s}: {100-pct:.0f}% 有值, {pct:.0f}% NULL{flag}")

# 检查关键财务表
print("\n  财务表完整性:")
for t in ['balance_sheet', 'income_statement', 'cash_flow']:
    r = c.execute(f"""
        SELECT MIN(report_date), MAX(report_date), COUNT(DISTINCT stock_code)
        FROM {t}
    """).fetchone()
    print(f"    {t}: {r[0]}~{r[1]}, {r[2]}只")

# 分红表
r = c.execute("SELECT COUNT(*), SUM(CASE WHEN CAST(ex_date AS VARCHAR) LIKE '%12-31' OR CAST(ex_date AS VARCHAR) LIKE '%06-30' THEN 1 ELSE 0 END) FROM dividends").fetchone()
print(f"    dividends: {r[0]:,}行, 占位日期{r[1]:,}行({r[1]/r[0]*100:.0f}%)")

# 检查 income_statement 关键列
for col in ['parent_net_profit', 'deducted_net_profit', 'revenue', 'net_profit', 'basic_eps']:
    cnt = c.execute(f"SELECT COUNT(*) FROM income_statement WHERE {col} IS NOT NULL").fetchone()[0]
    nulls = c.execute("SELECT COUNT(*) FROM income_statement").fetchone()[0]
    pct = cnt/nulls*100
    flag = "  !!!" if pct < 90 else ""
    print(f"    income.{col}: {pct:.0f}% 非空{flag}")

# SQLite
c.close()
sqlite_path = PROJECT / 'data' / 'valuedashboard.sqlite'
if sqlite_path.exists():
    import sqlite3
    sc = sqlite3.connect(str(sqlite_path))
    tables = sc.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"\n  SQLite: {len(tables)} 张表")
    for t in tables:
        cnt = sc.execute(f"SELECT COUNT(*) FROM \"{t[0]}\"").fetchone()[0]
        print(f"    {t[0]:30s} {cnt:>8,}")
    sc.close()
else:
    print("\n  SQLite: 文件不存在 !!!")

# ═══════════════════════════════════════
# 2. 代码完整性
# ═══════════════════════════════════════
print("\n### 2. 代码完整性 ###")

# 检查核心模块是否能导入
modules = [
    'app.core.config', 'app.core.storage.duckdb_store',
    'app.core.storage.sqlite_store', 'app.core.storage.schema',
    'app.core.storage.path_policy', 'app.core.indicators.calculator',
    'app.core.screening.engine', 'app.core.dsl.engine',
    'app.core.backfill', 'app.core.update', 'app.core.init',
]
for mod in modules:
    try:
        __import__(mod)
        print(f"  {mod:40s} OK")
    except Exception as e:
        print(f"  {mod:40s} ERROR: {str(e)[:60]}")

# 检查前端
frontend_dist = PROJECT / 'frontend' / 'dist'
if frontend_dist.exists():
    print(f"\n  前端 dist: OK ({len(list(frontend_dist.rglob('*')))} files)")
else:
    print(f"\n  前端 dist: 不存在 !!!")

# 检查配置
config_files = ['config/default.yaml', 'config/user.yaml', 'pyproject.toml']
for cf in config_files:
    cfp = PROJECT / cf
    print(f"  {cf}: {'OK' if cfp.exists() else 'MISSING !!!'}")

# ═══════════════════════════════════════
# 3. 总结
# ═══════════════════════════════════════
print("\n" + "=" * 60)
print("  审查完成")
print("=" * 60)
