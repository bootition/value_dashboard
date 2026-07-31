"""Fetch XDXR (corporate action) records for all listed stocks via TDX.

The xdxr table is currently empty (0 rows), which blocks the
corporate_action_dividend_lineage readiness check and prevents
QFQ price adjustment from working correctly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths
from app.core.storage.schema import init_duckdb_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 50
RATE_LIMIT = 1.0  # seconds between batches for TDX


def repair(duck: DuckDBStore, dry_run: bool, sample: int = 0) -> dict:
    stocks = duck.read_query(
        "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
    )
    codes = [row["stock_code"] for row in stocks]
    if sample > 0:
        codes = codes[:sample]
    logger.info("候选股票: %d", len(codes))

    mgr = AdapterManager()
    total = 0
    errors = 0
    stocks_with_data = 0

    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i : i + BATCH_SIZE]
        try:
            result = mgr.fetch(
                FetchRequest(data_type="xdxr", stock_codes=batch, fetch_date=time.strftime("%Y-%m-%d"))
            )
            if result.metadata.error:
                logger.warning("  批次 %d-%d 错误: %s", i + 1, min(i + BATCH_SIZE, len(codes)), result.metadata.error)
                errors += 1
            elif result.data:
                batch_count = len(result.data)
                total += batch_count
                stocks_with_data += len({r["stock_code"] for r in result.data})
                if not dry_run:
                    existing = duck.read_query(
                        "SELECT stock_code, event_date, category FROM xdxr WHERE stock_code IN ({})".format(
                            ", ".join("?" for _ in batch)
                        ),
                        batch,
                    )
                    existing_keys = {(r["stock_code"], str(r["event_date"])[:10], r["category"]) for r in existing}
                    new_records = [
                        r for r in result.data
                        if (r["stock_code"], str(r.get("event_date", ""))[:10], r.get("category")) not in existing_keys
                    ]
                    if new_records:
                        with duck.transaction() as conn:
                            conn.executemany(
                                """INSERT OR IGNORE INTO xdxr
                                   (stock_code, event_date, category, fenhong, songzhuangu, peigu, peigujia)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                [
                                    (r["stock_code"], r["event_date"], r.get("category"),
                                     r.get("fenhong"), r.get("songzhuangu"), r.get("peigu"), r.get("peigujia"))
                                    for r in new_records
                                ],
                            )

            if (i // BATCH_SIZE + 1) % 10 == 0:
                logger.info("进度: %d/%d 股票, %d 条XDXR", i + len(batch), len(codes), total)

        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning("  批次失败: %s", e)

        time.sleep(RATE_LIMIT)

    logger.info("完成: %d 条XDXR, %d 只有数据股票, %d 错误", total, stocks_with_data, errors)
    result = {"xdxr_rows": total, "stocks_with_data": stocks_with_data, "errors": errors}
    if not dry_run:
        final = duck.read_query("SELECT COUNT(*) AS count FROM xdxr")[0]["count"]
        result["xdxr_table_rows"] = final
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sample", type=int, default=0)
    args = parser.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    store = DuckDBStore(paths=paths)
    init_duckdb_schema(store)
    logger.info("模式: %s 数据库: %s", "DRY RUN" if args.dry_run else "WRITE", paths.duckdb_path)

    result = repair(store, dry_run=args.dry_run, sample=args.sample)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
