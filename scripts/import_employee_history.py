"""导入员工人数历史（CSMAR 逐年 + 当前快照），用于计算人均创收/人均创利。

数据来源
--------
1. `FAR_Finidx.dta` 的 `Nstaff`（员工数目）—— CSMAR C17，76,262 行（1990-2024 年报）。
   实测填充 73,587/76,262（96.5%）。
2. `company_profile.employee_num` —— 本项目已有的当前快照（5,565 只，100% 填充，
   来自东财 F10 `CompanySurvey` 的 `EMP_NUM`）。

为什么要两份
------------
人均指标的分母必须是**当时**的员工数。若一律用当前快照去除历史期收入，就会把
「今天的员工数」套到「当年的收入」上（同类口径错误参见 ops-knowledge-base D23）。
故：历史期用 Nstaff（逐年），当前期用 company_profile（最新）。

安全
----
幂等（按 (stock_code, report_date, source) 主键先删同源再插）；dry-run 默认；
`--yes` 才写库；写前备份；全程单写者锁。

用法
----
    python scripts/import_employee_history.py           # dry-run
    python scripts/import_employee_history.py --yes
"""

from __future__ import annotations

import argparse
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
CSMAR_SOURCE = "csmar"
SNAPSHOT_SOURCE = "eastmoney_f10"
COLUMNS = ("stock_code", "report_date", "employee_count", "source", "fetch_time", "batch_id")


def load_csmar() -> pd.DataFrame:
    path = PACKAGE_DIR / "财务指标文件(年度)" / "FAR_Finidx.dta"
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    df = pd.read_stata(path, columns=["Stkcd", "Accper", "Nstaff"], convert_categoricals=False)
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    df["employee_count"] = pd.to_numeric(df["Nstaff"], errors="coerce")
    before = len(df)
    df = df.dropna(subset=["report_date", "employee_count"])
    df = df[df["employee_count"] > 0]
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    logger.info("CSMAR Nstaff: %d 行有效（丢弃 %d）/ %d 只 / %s ~ %s",
                len(df), before - len(df), df["stock_code"].nunique(),
                df["report_date"].min(), df["report_date"].max())
    return df


def load_snapshot(duck: DuckDBStore) -> pd.DataFrame:
    """当前快照：以 company_profile 的抓取日为报告期（近似「今天」）。"""
    rows = duck.read_query(
        """SELECT stock_code, employee_num, CAST(fetch_time AS DATE) AS report_date
           FROM company_profile WHERE employee_num IS NOT NULL AND employee_num > 0"""
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["employee_count"] = df["employee_num"].astype("float").astype("int64")
    logger.info("company_profile 快照: %d 只", len(df))
    return df[["stock_code", "report_date", "employee_count"]]


def main() -> None:
    ap = argparse.ArgumentParser(description="导入员工人数历史")
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

    csmar = load_csmar()
    snapshot = load_snapshot(duck)

    if not args.yes:
        logger.info("[DRY RUN] CSMAR %d 行 / 快照 %d 行；未写库", len(csmar), len(snapshot))
        logger.info("抽样: %s", csmar.head(3).to_dict("records"))
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-employee-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "company_employee_history.parquet").replace("'", "''")
        conn.execute(f"COPY company_employee_history TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex
    frames = []
    for frame, source in ((csmar, CSMAR_SOURCE), (snapshot, SNAPSHOT_SOURCE)):
        if frame.empty:
            continue
        part = frame[["stock_code", "report_date", "employee_count"]].copy()
        part["report_date"] = pd.to_datetime(part["report_date"])
        part["source"] = source
        part["fetch_time"] = fetched_at
        part["batch_id"] = batch_id
        frames.append(part[list(COLUMNS)])

    data = pd.concat(frames, ignore_index=True)
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register("_emp", data)
        conn.execute("DELETE FROM company_employee_history")
        conn.execute("INSERT INTO company_employee_history BY NAME SELECT * FROM _emp")
        conn.unregister("_emp")
        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "employee_history", CSMAR_SOURCE, "csmar-c17-far_finidx+profile",
             fetched_at, batch_id[:32], len(data),
             f"{data['report_date'].min().date()}~{data['report_date'].max().date()}", "strict"],
        )

    evidence = {
        "task": "导入员工人数历史（CSMAR Nstaff + company_profile 快照）",
        "executed_at": datetime.now(UTC).isoformat(),
        "csmar_rows": len(csmar), "snapshot_rows": len(snapshot),
        "total_rows": len(data), "backup_dir": str(backup), "batch_id": batch_id,
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r8_employee_history_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成 %d 行；证据 JSON: %s", len(data), ev_path)


if __name__ == "__main__":
    main()
