"""构建 VD 数据包（2026-09-19 首版）。

按数据形态分两层：**A 类 = 可由外来数据包覆盖、本项目提供更新/扩展版；
B 类 = 外来数据包不覆盖、本项目独有的数据**。

包的形态
--------
    vd-datapackage-<版本>-<导出日>/
    ├── manifest.json           装箱单：版本/导出时间/as-of/逐文件 sha256+行数/来源分级/已知问题
    ├── README.md               人读说明
    ├── SOURCES.md              数据来源说明
    ├── DATA_DICTIONARY.csv     字段字典（中文名/单位/口径）
    ├── load.sql                DuckDB 一键装载
    ├── A_updates/              基础财务与自算指标（含更新/扩展）
    ├── B_supplements/          本项目独有数据（行情/事件/参考/统计）
    └── lineage/                溯源（--with-lineage）

来源标注
--------
每张表登记 `source_class`（自采 / 公开接口 / 历史归档），纯属数据来源描述，
便于使用者判断数据性质与更新时间，不构成任何使用限制。

安全
----
- 纯只读导出：不改任何现有数据；写入只发生在目标目录。
- 导出前检查写锁（自动更新进行中时导出会读到半成品快照）。
- 逐表 COPY 到临时文件后原子改名，失败不留半成品。
- **不含个人数据**（自选/筛选规则/ETF 流水/预算等一律不导出）。

用法
----
    python scripts/export_data_package.py --plan                 # 只列计划
    python scripts/export_data_package.py --out D:/vd-package    # 导出
    python scripts/export_data_package.py --with-lineage         # 含溯源层
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

PACKAGE_VERSION = "2026.09"

# (目录层, 库内表名, 包内输出名, 来源性质, 说明)
# 库内表名与输出名分开：内部标识保持不变，包内用中性命名。
A_TABLES: list[tuple[str, str, str, str]] = [
    ("A_updates", "balance_sheet", "balance_sheet", "混合", "资产负债表（历史段 + 本项目续写）"),
    ("A_updates", "income_statement", "income_statement", "混合", "利润表（历史段 + 本项目续写）"),
    ("A_updates", "cash_flow", "cash_flow", "混合", "现金流量表（历史段 + 本项目续写）"),
    ("A_updates", "indicator_snapshot", "indicator_snapshot", "自算", "★ 自算主链快照 61 列"),
    ("A_updates", "indicator_ext", "indicator_ext", "自算", "★ 自算扩展指标 51 列"),
    ("A_updates", "financial_report_dates", "financial_report_dates", "历史归档", "年报公布日期"),
    ("A_updates", "company_employee_history", "company_employee_history", "混合", "员工人数历史 + 当前快照"),
    # indicator_ext 的直接输入（缺了它们，包内指标无法复现/核验）
    ("A_updates", "cash_flow_indirect", "cash_flow_indirect", "混合", "间接法经营现金流（折旧摊销等，indicator_ext 输入）"),
    ("A_updates", "cash_flow_activity", "cash_flow_activity", "混合", "现金流补充科目（资本支出等，indicator_ext 输入）"),
    ("A_updates", "balance_sheet_ext", "balance_sheet_ext", "公开接口", "资产负债表补充科目（indicator_ext 输入）"),
    ("A_updates", "csmar_disclosure_metrics", "disclosure_metrics", "历史归档", "官方披露指标（含加权 ROE）"),
    ("A_updates", "csmar_risk_factors", "risk_factors", "历史归档", "风险治理因子"),
]
B_TABLES: list[tuple[str, str, str, str]] = [
    ("B_supplements", "price_daily_raw", "price_daily_raw", "自采", "★ 日线行情（原始价）"),
    ("B_supplements", "price_daily_qfq", "price_daily_qfq", "自采", "★ 日线行情（前复权）"),
    ("B_supplements", "xdxr", "xdxr", "自采", "除权除息（真实除权日）"),
    ("B_supplements", "dividends", "dividends", "自采", "分红事件"),
    ("B_supplements", "hk_dividends", "hk_dividends", "自采", "港股分红（A+H）"),
    ("B_supplements", "share_capital_history", "share_capital_history", "自采", "★ 股本变动明细"),
    ("B_supplements", "funding_events", "funding_events", "公开接口", "融资事件（东财 F10 为主）"),
    ("B_supplements", "buyback_events", "buyback_events", "公开接口", "回购注销"),
    ("B_supplements", "company_profile", "company_profile", "公开接口", "公司资料（含员工数）"),
    ("B_supplements", "business_breakdown", "business_breakdown", "公开接口", "主营构成"),
    ("B_supplements", "treasury_yield_curve", "treasury_yield_curve", "自采", "国债收益率曲线（财政部）"),
    ("B_supplements", "index_valuation", "index_valuation", "公开接口", "指数估值（乐咕/申万/中证）"),
    ("B_supplements", "etf_daily", "etf_daily", "公开接口", "ETF 行情（同花顺）"),
    ("B_supplements", "research_statistics", "research_statistics", "自采", "★ 历史统计域"),
    ("B_supplements", "stock_meta", "stock_meta", "公开接口", "股票主数据"),
]
LINEAGE_TABLES: list[tuple[str, str, str, str, str]] = [
    ("lineage", "fetch_batch", "fetch_batch", "自采", "抓取批次台账（每批一行的汇总溯源）"),
    ("lineage", "source_audit", "source_audit", "自采", "★ 逐行溯源：每行数据来自哪个源/批次/时刻"),
]

KNOWN_ISSUES = [
    "财务三表历史段（≤2025-03-31）来自外部数据包；2025Q2 起由本项目爬虫续写",
    "6 个金融监管指标仅覆盖 38 家银行 + 35 家券商（其余金融股该指标不适用）",
    "indicator_ext 的 leverage_* 覆盖率约 73%：亏损公司杠杆无业务含义，如实为 NULL",
    "business_breakdown 已过滤未来报告期（东财曾返回尚未结束的报告期）",
    "年报公布日期仅年度频率，不含季报/中报公布日",
    "ETF 行情（etf_daily）更新滞后于个股行情",
    "本包不含个人数据（自选/筛选规则/ETF 流水/预算等）",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _export_table(conn, table: str, layer: str, root: Path, *, output_name: str | None = None) -> dict:
    name = output_name or table
    layer_dir = root / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    target = layer_dir / f"{name}.parquet"
    temporary = layer_dir / f".{name}.{uuid.uuid4().hex}.tmp"
    quoted = str(temporary).replace("'", "''")
    conn.execute(f'COPY (SELECT * FROM "{table}") TO \'{quoted}\' (FORMAT PARQUET, COMPRESSION ZSTD)')
    temporary.replace(target)
    rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return {
        "file": f"{layer}/{name}.parquet",
        "rows": int(rows),
        "size_bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def _export_sqlite_table(sqlite_path: Path, table: str, layer: str, root: Path) -> dict:
    import pandas as pd

    layer_dir = root / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    target = layer_dir / f"{table}.parquet"
    temporary = layer_dir / f".{table}.{uuid.uuid4().hex}.tmp"
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        frame = pd.read_sql_query(f'SELECT * FROM "{table}"', connection)
    finally:
        connection.close()
    frame.to_parquet(temporary, compression="zstd", index=False)
    temporary.replace(target)
    return {
        "file": f"{layer}/{table}.parquet",
        "rows": int(len(frame)),
        "size_bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def _write_text(path: Path, content: str) -> dict:
    path.write_text(content, encoding="utf-8")
    return {"file": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 VD 数据包")
    ap.add_argument("--out", type=Path, default=None, help="输出目录")
    ap.add_argument("--plan", action="store_true", help="只列计划，不导出")
    ap.add_argument("--with-lineage", action="store_true", help="含溯源层（fetch_batch + 缺失清单）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    todo = A_TABLES + B_TABLES + (LINEAGE_TABLES if args.with_lineage else [])
    if args.plan:
        logger.info("数据包计划：%d 张表", len(todo))
        for layer, table, tier, note in todo:
            logger.info("  [%s] %-22s %-10s %s", tier, table, layer, note)
        logger.info("输出目录默认：<项目>/.planning/vd-datapackage-%s-<导出日>", PACKAGE_VERSION)
        return

    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到自动更新写锁生效——此时导出的会是半成品快照。请等更新结束后重试。")
        raise SystemExit(3)

    stamp = datetime.now().strftime("%Y%m%d")
    root = args.out or (PROJECT_ROOT / ".planning" / f"vd-datapackage-{PACKAGE_VERSION}-{stamp}")
    root.mkdir(parents=True, exist_ok=True)

    duck = DuckDBStore(paths=paths)
    files: dict[str, dict] = {}
    as_of: dict[str, str] = {}

    with duck.read_connection() as conn:
        for layer, table, output_name, source_class, note in todo:
            try:
                files[f"{layer}/{output_name}"] = {
                    **_export_table(conn, table, layer, root, output_name=output_name),
                    "source_class": source_class, "note": note,
                }
                logger.info("  已导出 %-24s → %s", output_name, layer)
            except Exception as error:  # noqa: BLE001
                logger.warning("  跳过 %s：%s", table, error)
        for key, sql in (
            ("price_latest", "SELECT CAST(MAX(trade_date) AS VARCHAR) FROM price_daily_raw"),
            ("financial_latest", "SELECT CAST(MAX(report_date) AS VARCHAR) FROM income_statement"),
            ("indicator_latest", "SELECT CAST(MAX(report_date) AS VARCHAR) FROM indicator_ext"),
        ):
            with contextlib.suppress(Exception):
                as_of[key] = conn.execute(sql).fetchone()[0]

    # 溯源层的 SQLite 台账
    if args.with_lineage:
        for table in ("missing_list", "retry_list"):
            try:
                files[f"lineage/{table}"] = {
                    **_export_sqlite_table(paths.sqlite_path, table, "lineage", root),
                    "source_class": "自采", "note": "质量披露台账",
                }
                logger.info("  已导出 %s（SQLite）", table)
            except Exception as error:  # noqa: BLE001
                logger.warning("  跳过 %s：%s", table, error)

    manifest = {
        "format_version": 1,
        "package_version": PACKAGE_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "source_project": "value-dashboard",
        "as_of": as_of,
        "source_classes": {
            "自采": "本项目自行采集（行情/事件/公告等）",
            "自算": "本项目基于原始数据自行计算的指标",
            "公开接口": "第三方公开接口的加工品",
            "历史归档": "外部数据包的历史段，截止 2025-03-31",
            "混合": "历史段与本项目续写合并",
        },
        "contents": {
            "A_updates": "基础财务与自算指标（含更新与扩展）",
            "B_supplements": "本项目独有数据（行情/事件/参考/统计）",
            "lineage": "溯源与质量披露（可选层）",
        },
        "files": files,
        "known_issues": KNOWN_ISSUES,
        "not_included": ["个人数据（自选/筛选规则/ETF 流水/预算）", "原始源文件", "原始响应字节"],
    }
    files["manifest.json"] = _write_text(root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))

    readme = f"""# VD 数据包 {PACKAGE_VERSION}

由 value-dashboard 导出于 {manifest['exported_at'][:19]}（UTC）。

## 包内结构

- **`A_updates/`** —— 基础财务与自算指标：
  三张报表（更新至 {as_of.get('financial_latest','?')}）、自算指标快照与扩展指标（51 列）、
  年报公布日期、员工人数历史、官方披露指标、风险因子。
- **`B_supplements/`** —— 本项目独有数据：
  日线行情（{as_of.get('price_latest','?')} 至今）、除权除息、分红、股本变动明细、
  融资、回购、公司资料、主营构成、国债曲线、指数估值、ETF、历史统计域、股票主数据。
- **`lineage/`** —— 溯源台账与质量披露（含 --with-lineage 时生成）。

## 快速使用

```bash
duckdb vd.duckdb < load.sql     # 建好全部视图，直接用 SQL 查
```

## 数据来源

每张表在 `manifest.json` 的 `source_class` 里标注来源性质
（自采 / 自算 / 公开接口 / 历史归档 / 混合），详见 `SOURCES.md`。

## 已知问题

见 `manifest.json` 的 `known_issues`，或下面摘要：

""" + "\n".join(f"- {item}" for item in KNOWN_ISSUES) + f"""

## 规模

- 表数：{len([v for v in files.values() if str(v.get('file','')).endswith('.parquet')])}
- 合计大小：{sum(v.get('size_bytes', 0) for v in files.values()) / 1024 / 1024:.1f} MB
"""
    files["README.md"] = _write_text(root / "README.md", readme)

    sources_text = """# 数据来源说明

本文件描述包内各表的数据来源与采集方式，便于判断数据性质与更新时间。

## 自采

本项目自行采集：

- 日线行情、除权除息（腾讯 / BaoStock / TDX）
- 分红事件、股本变动明细、公告台账（CNINFO 巨潮）
- 国债收益率曲线（财政部）

## 自算

本项目基于原始数据自行计算的指标：

- `indicator_snapshot`（主链快照 61 列）
- `indicator_ext`（扩展指标 51 列）
- `research_statistics`（历史分位 / z-score）

## 公开接口

第三方公开接口的加工品：

- 公司资料、主营构成、融资事件、回购（东方财富 F10）
- 指数估值（乐咕乐股 / 申万 / 中证指数官网）
- ETF 行情（同花顺公开接口）
- 股票主数据（东方财富）

## 历史归档

历史段数据（**截止 2025-03-31**），由外部数据包提供：

- `balance_sheet` / `income_statement` / `cash_flow` 的历史部分
- `financial_report_dates`（年报公布日期）
- `company_employee_history` 的历史部分
- `disclosure_metrics`（官方披露指标，含加权 ROE）
- `risk_factors`（风险治理因子）

2025-04-01 起的数据由本项目爬虫续写，与历史段无缝拼接。

## 未包含

- 个人数据（自选列表、筛选规则、ETF 持仓流水、预算设置等）
- 原始源文件
- 原始响应字节
"""
    files["SOURCES.md"] = _write_text(root / "SOURCES.md", sources_text)

    # 注意：files 的**键**是 "层/表名"（无扩展名），扩展名在 value 的 file 字段里。
    # 2026-09-19 首版曾误用键做 endswith('.parquet') 判断，导致 load.sql 生成为空。
    parquet_files = sorted(
        str(v["file"]) for v in files.values() if str(v.get("file", "")).endswith(".parquet")
    )
    load_lines = ["-- VD 数据包一键装载（DuckDB）", "-- 用法: duckdb vd.duckdb < load.sql", ""]
    for key in parquet_files:
        view = key.replace("/", "_").replace(".parquet", "")
        load_lines.append(
            f"CREATE OR REPLACE VIEW {view} AS "
            f"SELECT * FROM read_parquet('{key}');"
        )
    files["load.sql"] = _write_text(root / "load.sql", "\n".join(load_lines) + "\n")
    manifest["files"] = files
    _write_text(root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))

    total_mb = sum(v.get("size_bytes", 0) for v in files.values()) / 1024 / 1024
    logger.info("数据包构建完成：%s", root)
    logger.info("  表 %d 张 / 合计 %.1f MB", len(parquet_files), total_mb)
    by_class: dict[str, int] = {}
    for value in files.values():
        key = value.get("source_class")
        if key:
            by_class[key] = by_class.get(key, 0) + 1
    logger.info("  来源构成：%s", by_class)


if __name__ == "__main__":
    main()
