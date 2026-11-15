"""分块导入 CSMAR 行业历史 / 报告元数据（2026-11-15，reports/121）。

为什么单独一个脚本
------------------
`app/core/industry_history.py` 的 `import_csmar_*` 会先把**整个** dta 解析进内存、
最后一次性 upsert。302,959 行 × 5 列在 Python dict 里约 300 MB，加上 DuckDB 写入
容易触发本机已知的 `PyEval_SaveThread ... GIL released` 致命错误（2026-11-14 起
在 `executemany`、长时间抓取循环上各复现过一次）。

本脚本按 **读取 → 解析 → 落库** 逐块流水：内存里同时只有一块（默认 5 万行），
每块独立事务，重复执行幂等（PK + ON CONFLICT）。崩了重跑即可，不会留下半行。

用法
----
    python scripts/import_industry_history.py --kind industry        # dry-run
    python scripts/import_industry_history.py --kind industry --yes  # 写库
    python scripts/import_industry_history.py --kind metadata --yes
    python scripts/import_industry_history.py --kind both --yes --chunk-size 50000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core import industry_history as ih  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def _run_industry(duck: DuckDBStore, *, dry_run: bool, chunk_size: int) -> dict:
    started = time.time()
    total_rows = 0
    total_written = 0
    counters: dict[str, int] = {}
    dates: list = []
    stocks: set[str] = set()
    for chunk in ih.iter_dta_chunks(ih.DEFAULT_INDUSTRY_DTA, ih.INDUSTRY_DTA_COLUMNS, chunk_size=chunk_size):
        rows = ih.parse_csmar_industry_rows(chunk, counters=counters)
        total_rows += len(rows)
        if not rows:
            continue
        dates.extend(row["effective_date"] for row in rows)
        stocks.update(row["stock_code"] for row in rows)
        if not dry_run:
            total_written += ih.upsert_industry_history(duck, rows, source=ih.CSMAR_INDUSTRY_SOURCE)
        logger.info("industry 块写入 %d 行（累计 %d）", len(rows), total_rows)
    return {
        "kind": "industry_history",
        "dry_run": dry_run,
        "rows_parsed": total_rows,
        "rows_written": total_written,
        "stocks": len(stocks),
        "date_range": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
        "counters": counters,
        "elapsed_seconds": round(time.time() - started, 1),
    }


def _run_metadata(duck: DuckDBStore, *, dry_run: bool, chunk_size: int) -> dict:
    started = time.time()
    total_rows = 0
    total_written = 0
    counters: dict[str, int] = {}
    dates: list = []
    stocks: set[str] = set()
    for chunk in ih.iter_dta_chunks(ih.DEFAULT_METADATA_DTA, ih.REPORT_DTA_COLUMNS, chunk_size=chunk_size):
        rows = ih.parse_csmar_report_metadata_rows(chunk, counters=counters)
        total_rows += len(rows)
        if not rows:
            continue
        dates.extend(row["report_date"] for row in rows)
        stocks.update(row["stock_code"] for row in rows)
        if not dry_run:
            total_written += ih.upsert_report_metadata(duck, rows)
        logger.info("metadata 块写入 %d 行（累计 %d）", len(rows), total_rows)
    return {
        "kind": "report_metadata",
        "dry_run": dry_run,
        "rows_parsed": total_rows,
        "rows_written": total_written,
        "stocks": len(stocks),
        "date_range": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
        "counters": counters,
        "elapsed_seconds": round(time.time() - started, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="分块导入 CSMAR 行业历史与报告元数据")
    ap.add_argument("--kind", choices=("industry", "metadata", "both"), default="both")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--chunk-size", type=int, default=50_000)
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    reports = []
    if args.kind in {"industry", "both"}:
        reports.append(_run_industry(duck, dry_run=not args.yes, chunk_size=args.chunk_size))
    if args.kind in {"metadata", "both"}:
        reports.append(_run_metadata(duck, dry_run=not args.yes, chunk_size=args.chunk_size))

    logger.info(json.dumps({
        "action": "import_industry_history",
        "dry_run": not args.yes,
        "reports": reports,
        "finished_at": datetime.now(UTC).isoformat(),
    }, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
