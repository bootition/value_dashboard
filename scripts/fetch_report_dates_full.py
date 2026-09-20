"""补全财报公布日域 financial_report_dates（2026-09-20）。

问题
----
`financial_report_dates` 此前只有**年报**公布日（74,509 条，report_date 月份全为 12），
且止于 2024-12-31 —— 2025 年报公布日缺失，「当时可见」回测在 2025 年之后失效。

数据源
------
东方财富 datacenter `RPT_F10_FINANCE_MAINFINADATA` 的 `NOTICE_DATE`（公告日期），
**每个报告期都有**（年报/中报/一季报/三季报）。实测招商银行 102 期完整；
接口支持 `SECUCODE in (...)` 批量查询。

收益
----
- 补上 2025 年报及之后所有期的公布日
- 把「当时可见」从**年度精度升级为季度精度**
- 历史归档域已有的深历史年报公布日保留不动

策略
----
**只插入 (stock_code, report_date) 尚不存在的记录** —— 既补新期，又不覆盖已验证的历史。

用法:
    python scripts/fetch_report_dates_full.py            # dry-run
    python scripts/fetch_report_dates_full.py --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active, exclusive_update  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SOURCE = "eastmoney_f10"
URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
BATCH = 40
PAGE_SIZE = 5000
COLUMNS = ["stock_code", "report_date", "announce_date", "source", "fetch_time", "batch_id"]


def _secucode(code: str, exchange: str | None) -> str:
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get((exchange or "").upper())
    if suffix is None:
        suffix = "BJ" if code.startswith(("920", "43", "83", "87")) else (
            "SH" if code.startswith(("60", "68", "90", "9")) else "SZ")
    return f"{code}.{suffix}"


def _fetch_batch(session: requests.Session, secucodes: list[str]) -> list[dict]:
    quoted = ",".join(f'"{s}"' for s in secucodes)
    params = {
        "reportName": REPORT_NAME,
        "columns": "SECUCODE,REPORT_DATE,NOTICE_DATE",
        "filter": f"(SECUCODE in ({quoted}))",
        "pageNumber": "1",
        "pageSize": str(PAGE_SIZE),
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
    }
    for attempt in range(3):
        try:
            resp = session.get(URL, params=params, timeout=40)
            resp.raise_for_status()
            return (resp.json().get("result") or {}).get("data") or []
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return []
            time.sleep(2.0 * (attempt + 1))
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description="补全财报公布日域")
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

    universe = [(r["stock_code"], r["exchange"]) for r in duck.read_query(
        "SELECT stock_code, COALESCE(exchange,'') AS exchange FROM stock_meta ORDER BY stock_code")]
    logger.info("股票池 %d 只，每批 %d 只 → 约 %d 个请求",
                len(universe), BATCH, (len(universe) + BATCH - 1) // BATCH)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0",
                            "Referer": "https://emweb.securities.eastmoney.com/"})

    records: list[dict] = []
    failed_batches = 0
    for start in range(0, len(universe), BATCH):
        chunk = universe[start:start + BATCH]
        code_map = {_secucode(code, ex): code for code, ex in chunk}
        rows = _fetch_batch(session, list(code_map))
        if not rows:
            failed_batches += 1
        for row in rows:
            code = code_map.get(str(row.get("SECUCODE") or ""))
            if code is None:
                continue
            try:
                report_date = date.fromisoformat(str(row.get("REPORT_DATE") or "")[:10])
                notice_date = date.fromisoformat(str(row.get("NOTICE_DATE") or "")[:10])
            except ValueError:
                continue
            if notice_date <= report_date:
                continue
            records.append({"stock_code": code, "report_date": report_date,
                            "announce_date": notice_date})
        time.sleep(0.35)
        if start % (BATCH * 20) == 0:
            logger.info("  进度 %d/%d，累计 %d 条", start + len(chunk), len(universe), len(records))

    frame = pd.DataFrame(records).drop_duplicates(subset=["stock_code", "report_date"])
    frame["report_date"] = pd.to_datetime(frame["report_date"])
    frame["announce_date"] = pd.to_datetime(frame["announce_date"])
    if frame.empty:
        logger.error("未取到任何数据，中止。")
        raise SystemExit(2)
    logger.info("抓取完成：%d 条 / %d 只 / 失败批次 %d",
                len(frame), frame["stock_code"].nunique(), failed_batches)
    logger.info("  报告期覆盖：%s ~ %s", frame["report_date"].min(), frame["report_date"].max())
    logger.info("  月份分布：%s", frame["report_date"].dt.month.value_counts().sort_index().to_dict())
    if not args.yes:
        logger.info("[DRY RUN] 未写库")
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-reportdates-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "financial_report_dates.parquet").replace("'", "''")
        conn.execute(f"COPY financial_report_dates TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    batch_id = uuid.uuid4().hex
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    frame["source"] = SOURCE
    frame["fetch_time"] = fetched_at
    frame["batch_id"] = batch_id

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register("_frd", frame[COLUMNS])
        try:
            before = conn.execute("SELECT COUNT(*) FROM financial_report_dates").fetchone()[0]
            conn.execute(
                "INSERT INTO financial_report_dates BY NAME "
                "SELECT * FROM _frd f WHERE NOT EXISTS ("
                "  SELECT 1 FROM financial_report_dates t "
                "  WHERE t.stock_code = f.stock_code AND t.report_date = f.report_date)"
            )
            after = conn.execute("SELECT COUNT(*) FROM financial_report_dates").fetchone()[0]
        finally:
            conn.unregister("_frd")
        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "financial_report_dates", SOURCE, "em-datacenter-mainfinadata",
             fetched_at, batch_id[:32], len(frame),
             f"{frame['report_date'].min().date()}~{frame['report_date'].max().date()}", "strict"],
        )

    evidence = {"task": "补全财报公布日域（季度精度）",
                "executed_at": datetime.now(UTC).isoformat(),
                "fetched": len(frame), "inserted": after - before,
                "before": before, "after": after, "failed_batches": failed_batches,
                "backup_dir": str(backup), "batch_id": batch_id}
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r21_report_dates_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成：%d → %d（新增 %d 条）；证据: %s", before, after, after - before, ev_path)


if __name__ == "__main__":
    main()
