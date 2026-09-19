"""导入 CSMAR 三表剩余科目 + 金融专用科目全量（v35，最后一格）。

- csmar_balance_items / csmar_income_items / csmar_cashflow_items：
  三张报表里本项目未映射的 100 个科目（投资性房地产、库存股、其他综合收益、
  持续经营/终止经营净利润、资产处置收益…）
- csmar_financial_items：金融专用科目全量 109 列（跨三张报表按 (股票,报告期) 合并）

列名保留 CSMAR 代码；中文名见数据包内的字段字典。
域纪律：归档与专项研究用，不进筛选界面。

用法:
    python scripts/import_csmar_statement_items.py            # dry-run
    python scripts/import_csmar_statement_items.py --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active, exclusive_update  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

PACKAGE_DIR = PROJECT_ROOT / "额外资料" / "C17 a股上市公司财务数据合集（90-25年）" / "原始数据（dta格式）"
SOURCE = "csmar"

# 表 → [(dta 文件名, 所在目录), ...]（多来源的表按 key 横向合并）
TABLE_SOURCES: dict[str, list[list[str]]] = {"csmar_balance_items": [["FS_Combas.dta", "资产负债表"]], "csmar_income_items": [["FS_Comins.dta", "利润表"]], "csmar_cashflow_items": [["FS_Comscfd.dta", "现金流量表(直接法)"]], "csmar_financial_items": [["FS_Combas.dta", "资产负债表"], ["FS_Comins.dta", "利润表"], ["FS_Comscfd.dta", "现金流量表(直接法)"]]}

# 表 → 目标列（CSMAR 代码）
TABLE_FIELDS: dict[str, list[str]] = {"csmar_balance_items": ["A001109000", "A001127000", "A001119000", "A001120000", "A001123101", "A001129000", "A001124000", "A001125000", "A001226000", "A001202000", "A001227000", "A001203000", "A001204000", "A001228000", "A001229000", "A001206000", "A001207000", "A001211000", "A001214000", "A001215000", "A001216000", "A001217000", "A001218201", "A001219000", "A001219101", "A001221000", "A001223000", "A002105000", "A002114000", "A002115000", "A002120000", "A002129000", "A002125000", "A002126000", "A002127000", "A002204000", "A002212000", "A002205000", "A002206000", "A002207000", "A002208000", "A002209000", "A002210000", "A003112000", "A003112101", "A003112201", "A003112301", "A003102101", "A003106000", "A003107000", "A003111000"], "csmar_income_items": ["Bbd1102000", "Bbd1102101", "Bbd1102203", "B001305000", "B001302101", "B001302201", "B001303000", "B001306000", "B001308000", "B001304000", "B001400101", "B001500101", "B001500201", "B002200000", "B002300000", "B002000401", "B002000501", "B002000301", "B005000000", "B005000101", "B005000102", "B006000000", "B006000101", "B006000103", "B006000102"], "csmar_cashflow_items": ["C002001000", "C002002000", "C002003000", "C002004000", "C002005000", "C002100000", "C002006000", "C002007000", "C002009000", "C002010000", "C002200000", "C003008000", "C003001000", "C003001101", "C003003000", "C003002000", "C003004000", "C003100000", "C003005000", "C003006000", "C003006101", "C003007000", "C003200000", "C007000000"], "csmar_financial_items": ["A0D2130000", "A0F1132000", "A0F1133000", "A0F1224000", "A0F1232000", "A0F1233000", "A0F2210000", "A0F3108000", "A0F3109000", "A0b1103000", "A0b1104000", "A0b1105000", "A0b1201000", "A0b2102000", "A0b2103000", "A0b2103101", "A0b2103201", "A0d1101101", "A0d1102000", "A0d1102101", "A0d1126000", "A0d1218101", "A0d2101101", "A0d2122000", "A0d2123000", "A0d2202000", "A0f1106000", "A0f1108000", "A0f1122000", "A0f1300000", "A0f2104000", "A0f2106000", "A0f2110000", "A0f2300000", "A0f3104000", "A0i1113000", "A0i1114000", "A0i1115000", "A0i1116000", "A0i1116101", "A0i1116201", "A0i1116301", "A0i1116401", "A0i1209000", "A0i1210000", "A0i1224000", "A0i1225000", "A0i2111000", "A0i2116000", "A0i2117000", "A0i2118000", "A0i2119000", "A0i2119101", "A0i2119201", "A0i2119301", "A0i2119401", "A0i2121000", "A0i2124000", "B0I1214000", "B0d1104000", "B0d1104101", "B0d1104201", "B0d1104301", "B0d1104401", "B0d1104501", "B0f1105000", "B0f1208000", "B0f1213000", "B0i1103000", "B0i1103101", "B0i1103111", "B0i1103203", "B0i1103303", "B0i1202000", "B0i1203000", "B0i1203101", "B0i1203203", "B0i1204000", "B0i1204101", "B0i1204203", "B0i1205000", "B0i1206000", "B0i1208103", "C0F1023000", "C0F1024000", "C0F1025000", "C0F1026000", "C0F1027000", "C0F1028000", "C0F1029000", "C0F1030000", "C0F1031000", "C0F1032000", "C0b1002000", "C0b1003000", "C0b1004000", "C0b1015000", "C0b1016000", "C0d1008000", "C0d1010000", "C0d1011000", "C0f1009000", "C0f1018000", "C0i1005000", "C0i1006000", "C0i1007000", "C0i1017000", "C0i1019000", "C0i2008000"]}


def _read(relative: str, folder: str, fields: list[str]) -> pd.DataFrame:
    path = PACKAGE_DIR / folder / relative
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    # 只请求该文件**实际存在**的列（金融专用列横跨三张表，各自只有一部分）
    with pd.read_stata(path, chunksize=1, convert_categoricals=False) as reader:
        available = set(reader.variable_labels().keys())
    present = [f for f in fields if f in available]
    if not present:
        return None
    df = pd.read_stata(path, columns=["Stkcd", "Accper", *present], convert_categoricals=False)
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    df = df.dropna(subset=["report_date"])
    for col in present:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 CSMAR 三表剩余科目与金融专用科目全量")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    from app.core.storage.schema import init_duckdb_schema
    init_duckdb_schema(duck)

    prepared: list[tuple[str, pd.DataFrame, list[str], str]] = []
    for table, sources in TABLE_SOURCES.items():
        fields = TABLE_FIELDS[table]
        merged = None
        digest = hashlib.sha256()
        for relative, folder in sources:
            digest.update((PACKAGE_DIR / folder / relative).read_bytes())
            part = _read(relative, folder, fields)
            if part is None:
                continue
            merged = part if merged is None else merged.merge(
                part, on=["stock_code", "report_date"], how="outer"
            )
        assert merged is not None
        keep = [c for c in fields if c in merged.columns]
        merged = merged.dropna(subset=keep, how="all")
        prepared.append((table, merged, keep, digest.hexdigest()))
        logger.info("%s: %d 行 / %d 只 / %d 列 / %s ~ %s", table, len(merged),
                    merged["stock_code"].nunique(), len(keep),
                    merged["report_date"].min(), merged["report_date"].max())

    if not args.yes:
        logger.info("[DRY RUN] 共 %d 张表 / %d 列，未写库",
                    len(prepared), sum(len(f) for _, _, f, _ in prepared))
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-csmar-stmt-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex

    with duck.write_connection() as conn:
        for table, _, _, _ in prepared:
            target = str(backup / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    summary = {}
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        for table, df, fields, digest in prepared:
            data = df[["stock_code", "report_date", *fields]].copy()
            data["report_date"] = pd.to_datetime(data["report_date"])
            data["source"] = SOURCE
            data["fetch_time"] = fetched_at
            data["batch_id"] = batch_id
            view = f"_stmt_{table[:20]}"
            conn.register(view, data)
            try:
                conn.execute(f"DELETE FROM {table} WHERE source = '{SOURCE}'")
                conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {view}")
            finally:
                conn.unregister(view)
            summary[table] = {"rows": len(data), "columns": len(fields)}
            conn.execute(
                """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
                   raw_response_hash, row_count, report_date_range, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [f"{batch_id}-{table}", table, SOURCE, "csmar-c17-stmt-items", fetched_at,
                 digest[:32], len(data),
                 f"{data['report_date'].min().date()}~{data['report_date'].max().date()}", "strict"],
            )

    evidence = {"task": "导入 CSMAR 三表剩余科目与金融专用科目全量",
                "executed_at": datetime.now(UTC).isoformat(),
                "tables": summary, "backup_dir": str(backup), "batch_id": batch_id}
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r14_csmar_stmt_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成 %s；证据: %s", summary, ev_path)


if __name__ == "__main__":
    main()
