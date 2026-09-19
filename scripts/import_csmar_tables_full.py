"""全量导入 CSMAR 剩余指标表（v34）。

「榨干数据包」的完整性收口：把此前未接入的 11 张 CSMAR 指标表**整表导入**
（439 个数据字段）。列名保留 CSMAR 原始代码，中文名见 `.planning` 的字段字典。

设计：**从 .dta 文件的实际列推导字段**（减去维度列），不维护字段清单 ——
避免「代码里的清单」与「真实文件」漂移。

域纪律：不进筛选界面（数据截止 2025Q1，且与自算指标大量重叠）。

用法:
    python scripts/import_csmar_tables_full.py            # dry-run
    python scripts/import_csmar_tables_full.py --yes
    python scripts/import_csmar_tables_full.py --yes --only csmar_fi_t9
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
DIM_COLUMNS = {
    "Stkcd", "ShortName", "Accper", "Typrep", "IfCorrect", "DeclareDate", "Source",
    "Indcd", "Indnme", "Indcd1", "Indnme1", "StateType", "IndustryCode", "IndustryName",
    "IndustryCode1", "IndustryName1", "Annodt", "Symbol", "EndDate",
}

# 表名 → .dta 相对路径（由脚本生成，见 .planning 的 gen_ddl.py）
TABLE_FILES: dict[str, str] = {
    "csmar_fi_t1": "偿债能力/FI_T1.dta",
    "csmar_fi_t8": "发展能力/FI_T8.dta",
    "csmar_fi_t9": "每股指标/FI_T9.dta",
    "csmar_fi_t3": "比率结构/FI_T3.dta",
    "csmar_fi_t6": "现金流分析/FI_T6.dta",
    "csmar_fi_t5": "盈利能力/FI_T5.dta",
    "csmar_fi_t10": "相对价值指标/FI_T10.dta",
    "csmar_fi_t4": "经营能力/FI_T4.dta",
    "csmar_fi_t11": "股利分配/FI_T11.dta",
    "csmar_far_finidx": "财务指标文件(年度)/FAR_Finidx.dta",
    "csmar_fi_t7": "风险水平/FI_T7.dta",
}


def _load(table: str, relative: str) -> tuple[pd.DataFrame, list[str], str]:
    path = PACKAGE_DIR / relative
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    df = pd.read_stata(path, convert_categoricals=False)
    fields = [c for c in df.columns if c not in DIM_COLUMNS]
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    df = df.dropna(subset=["report_date"])
    for col in fields:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    logger.info("%s: %d 行 / %d 只 / %d 列 / %s ~ %s", table, len(df),
                df["stock_code"].nunique(), len(fields),
                df["report_date"].min(), df["report_date"].max())
    return df, fields, digest


def main() -> None:
    ap = argparse.ArgumentParser(description="全量导入 CSMAR 剩余指标表")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--only", default="", help="只导入指定表名，逗号分隔")
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

    wanted = {t.strip() for t in args.only.split(",") if t.strip()} or None
    items = [(t, f) for t, f in TABLE_FILES.items() if wanted is None or t in wanted]
    if not items:
        logger.error("没有匹配的表：%s", args.only)
        raise SystemExit(2)

    loaded = [(t, *_load(t, f)) for t, f in items]
    if not args.yes:
        logger.info("[DRY RUN] 共 %d 张表 / %d 列，未写库",
                    len(loaded), sum(len(f) for _, _, f, _ in loaded))
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-csmar-full-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex

    with duck.write_connection() as conn:
        for table, _, _, _ in loaded:
            target = str(backup / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    summary, total_cols = {}, 0
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        for table, df, fields, digest in loaded:
            cols = ["stock_code", "report_date", *fields]
            data = df[cols].copy()
            data["report_date"] = pd.to_datetime(data["report_date"])
            data["source"] = SOURCE
            data["fetch_time"] = fetched_at
            data["batch_id"] = batch_id
            view = f"_full_{table[:20]}"
            conn.register(view, data)
            try:
                conn.execute(f"DELETE FROM {table} WHERE source = '{SOURCE}'")
                conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {view}")
            finally:
                conn.unregister(view)
            summary[table] = {"rows": len(data), "columns": len(fields)}
            total_cols += len(fields)
            conn.execute(
                """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
                   raw_response_hash, row_count, report_date_range, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [f"{batch_id}-{table}", table, SOURCE, "csmar-c17-full", fetched_at, digest[:32],
                 len(data), f"{data['report_date'].min().date()}~{data['report_date'].max().date()}",
                 "strict"],
            )

    evidence = {
        "task": "全量导入 CSMAR 剩余指标表",
        "executed_at": datetime.now(UTC).isoformat(),
        "tables": summary, "total_columns": total_cols,
        "backup_dir": str(backup), "batch_id": batch_id,
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r13_csmar_full_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成 %d 张表 / %d 列；证据: %s", len(summary), total_cols, ev_path)


if __name__ == "__main__":
    main()
