"""构建 VD 数据包（2026-09-19 首版）。

按用户要求的结构：**A 类 = CSMAR 有、我们提供更新/扩展版；B 类 = CSMAR 没有、我们独有**。

包的形态
--------
    vd-datapackage-<版本>-<导出日>/
    ├── manifest.json           装箱单：版本/导出时间/as-of/逐文件 sha256+行数/来源分级/已知问题
    ├── README.md               人读说明
    ├── LICENSE_AND_SOURCES.md  许可三级（A 可分发 / B 注明来源 / C 禁止外发）
    ├── DATA_DICTIONARY.csv     字段字典（中文名/单位/口径）
    ├── load.sql                DuckDB 一键装载
    ├── A_updates/              CSMAR 有、我们更新或扩展的
    ├── B_supplements/          CSMAR 完全没有、我们独有的
    └── lineage/                溯源（--with-lineage）

许可分级（红线）
----------------
    A 级 自爬数据 + 自算指标                        → 可自由分发
    B 级 第三方公开源的加工品（东财补充）           → 可分发，须注明来源
    C 级 CSMAR 衍生值与其字段字典                   → **禁止外发**，默认不打包

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

# (目录层, 表名, 许可级别, 说明)
A_TABLES: list[tuple[str, str, str, str]] = [
    ("A_updates", "balance_sheet", "C", "资产负债表（历史段含 CSMAR 导入值）"),
    ("A_updates", "income_statement", "C", "利润表（历史段含 CSMAR 导入值）"),
    ("A_updates", "cash_flow", "C", "现金流量表（历史段含 CSMAR 导入值）"),
    ("A_updates", "indicator_snapshot", "A", "★ 自算主链快照 61 列"),
    ("A_updates", "indicator_ext", "A", "★ 自算扩展指标 51 列"),
    ("A_updates", "financial_report_dates", "C", "年报公布日期（CSMAR Annodt）"),
    ("A_updates", "company_employee_history", "C", "员工人数历史（CSMAR Nstaff + 东财快照）"),
    ("A_updates", "csmar_disclosure_metrics", "C", "官方披露指标（含加权 ROE）"),
    ("A_updates", "csmar_risk_factors", "C", "风险治理因子"),
]
B_TABLES: list[tuple[str, str, str, str]] = [
    ("B_supplements", "price_daily_raw", "A", "★ 日线行情（原始价）"),
    ("B_supplements", "price_daily_qfq", "A", "★ 日线行情（前复权）"),
    ("B_supplements", "xdxr", "A", "除权除息（真实除权日）"),
    ("B_supplements", "dividends", "A", "分红事件"),
    ("B_supplements", "hk_dividends", "A", "港股分红（A+H）"),
    ("B_supplements", "share_capital_history", "A", "★ 股本变动明细"),
    ("B_supplements", "funding_events", "B", "融资事件（东财 F10 为主）"),
    ("B_supplements", "buyback_events", "B", "回购注销"),
    ("B_supplements", "company_profile", "B", "公司资料（含员工数）"),
    ("B_supplements", "business_breakdown", "B", "主营构成"),
    ("B_supplements", "treasury_yield_curve", "A", "国债收益率曲线（财政部）"),
    ("B_supplements", "index_valuation", "B", "指数估值（乐咕/申万/中证）"),
    ("B_supplements", "etf_daily", "B", "ETF 行情（同花顺）"),
    ("B_supplements", "research_statistics", "A", "★ 历史统计域"),
    ("B_supplements", "stock_meta", "B", "股票主数据"),
]
LINEAGE_TABLES: list[tuple[str, str, str, str]] = [
    ("lineage", "fetch_batch", "A", "抓取批次台账"),
]

KNOWN_ISSUES = [
    "财务三表 ≤2025-03-31 的历史段源自 CSMAR C17（商业授权数据），按 C 级禁止对外分发",
    "CSMAR 数据截止 2025-03-31；2025Q2 起由本项目爬虫续写",
    "6 个金融监管指标仅覆盖 38 家银行 + 35 家券商（其余金融股该指标不适用）",
    "indicator_ext 的 leverage_* 覆盖率约 73%：亏损公司杠杆无业务含义，如实为 NULL",
    "business_breakdown 已过滤未来报告期（东财曾返回尚未结束的报告期）",
    "年报公布日期仅年度频率（CSMAR 不提供季报/中报公布日）",
    "ETF 行情（etf_daily）更新滞后于个股行情",
    "本包不含个人数据（自选/筛选规则/ETF 流水/预算等）",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _export_table(conn, table: str, layer: str, root: Path) -> dict:
    layer_dir = root / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    target = layer_dir / f"{table}.parquet"
    temporary = layer_dir / f".{table}.{uuid.uuid4().hex}.tmp"
    quoted = str(temporary).replace("'", "''")
    conn.execute(f'COPY (SELECT * FROM "{table}") TO \'{quoted}\' (FORMAT PARQUET, COMPRESSION ZSTD)')
    temporary.replace(target)
    rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return {
        "file": f"{layer}/{table}.parquet",
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
        for layer, table, tier, note in todo:
            try:
                files[f"{layer}/{table}"] = {**_export_table(conn, table, layer, root), "tier": tier, "note": note}
                logger.info("  已导出 %-22s → %s", table, layer)
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
                files[f"lineage/{table}"] = {**_export_sqlite_table(paths.sqlite_path, table, "lineage", root),
                                             "tier": "A", "note": "质量披露台账"}
                logger.info("  已导出 %s（SQLite）", table)
            except Exception as error:  # noqa: BLE001
                logger.warning("  跳过 %s：%s", table, error)

    manifest = {
        "format_version": 1,
        "package_version": PACKAGE_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "source_project": "value-dashboard",
        "as_of": as_of,
        "license_tiers": {
            "A": "自爬数据与自算指标 —— 可自由分发",
            "B": "第三方公开源加工品 —— 可分发，须注明来源",
            "C": "CSMAR C17 衍生值 —— **禁止对外分发**（商业授权数据）",
        },
        "contents": {
            "A_updates": "CSMAR 数据包有、但本项目提供更新/扩展/自算版本",
            "B_supplements": "CSMAR 数据包完全没有、本项目独有的数据",
            "lineage": "溯源与质量披露（可选层）",
        },
        "files": files,
        "known_issues": KNOWN_ISSUES,
        "not_included": ["个人数据（自选/筛选规则/ETF 流水/预算）", "CSMAR 原始文件", "原始响应字节"],
    }
    files["manifest.json"] = _write_text(root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))

    readme = f"""# VD 数据包 {PACKAGE_VERSION}

由 value-dashboard 导出于 {manifest['exported_at'][:19]}（UTC）。

## 包内结构（按「CSMAR 数据包有没有」二分）

- **`A_updates/`** —— CSMAR 数据包里**有**的，但本项目提供**更新 / 扩展 / 自算**版本：
  三张报表（爬到 {as_of.get('financial_latest','?')}，CSMAR 只到 2025-03-31）、
  自算指标快照与扩展指标（51 列）、年报公布日期、员工人数历史、官方披露指标、风险因子。
- **`B_supplements/`** —— CSMAR 数据包里**完全没有**、本项目独有的：
  日线行情（{as_of.get('price_latest','?')} 至今）、除权除息、分红、股本变动明细、
  融资、回购、公司资料、主营构成、国债曲线、指数估值、ETF、历史统计域、股票主数据。
- **`lineage/`** —— 溯源台账与质量披露（含 --with-lineage 时生成）。

## 快速使用

```bash
duckdb vd.duckdb < load.sql     # 建好全部视图，直接用 SQL 查
```

## 许可

**务必先读 `LICENSE_AND_SOURCES.md`。** 包内数据分 A/B/C 三级，
其中 **C 级（CSMAR 衍生值）禁止对外分发**。

## 已知问题

见 `manifest.json` 的 `known_issues`，或下面摘要：

""" + "\n".join(f"- {item}" for item in KNOWN_ISSUES) + f"""

## 规模

- 表数：{len([v for v in files.values() if str(v.get('file','')).endswith('.parquet')])}
- 合计大小：{sum(v.get('size_bytes', 0) for v in files.values()) / 1024 / 1024:.1f} MB
"""
    files["README.md"] = _write_text(root / "README.md", readme)

    license_text = """# 许可与数据来源（务必阅读）

本包数据按来源分三级。**对外分发前必须确认不含 C 级，或已获得相应授权。**

## A 级 —— 可自由分发

本项目自行采集与自算的数据：

- 日线行情、除权除息（腾讯 / BaoStock / TDX）
- 分红事件、股本变动明细、公告台账（CNINFO 巨潮）
- 国债收益率曲线（财政部）
- 全部自算指标：`indicator_snapshot`、`indicator_ext`、`research_statistics`
- 溯源台账：`fetch_batch`、`missing_list`、`retry_list`

## B 级 —— 可分发，须注明来源

第三方公开接口的加工品：

- 公司资料、主营构成、融资事件、回购、员工人数快照（东方财富 F10）
- 指数估值（乐咕乐股 / 申万 / 中证指数官网）
- ETF 行情（同花顺公开接口）
- 股票主数据（东方财富）

## C 级 —— **禁止对外分发**

来自 CSMAR C17 商业数据包（`额外资料/`，付费授权）的衍生值：

- `balance_sheet` / `income_statement` / `cash_flow` 中 **report_date ≤ 2025-03-31** 的历史段
- `financial_report_dates`（年报公布日期）
- `company_employee_history` 中 1990-2024 段
- `csmar_disclosure_metrics`、`csmar_risk_factors`
- 以及任何以 `csmar_` 前缀命名的表

**这些数据仅限在获得 CSMAR 授权的范围内使用，不得再分发、不得公开发布。**

## 未包含

- 个人数据（自选列表、筛选规则、ETF 持仓流水、预算设置等）
- CSMAR 原始 `.dta` 文件
- 原始响应字节（如需请另行申请导出）
"""
    files["LICENSE_AND_SOURCES.md"] = _write_text(root / "LICENSE_AND_SOURCES.md", license_text)

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
    logger.info("  含 C 级表：%d（对外分发前请先剔除）",
                len([k for k in files.values() if k.get("tier") == "C"]))


if __name__ == "__main__":
    main()
