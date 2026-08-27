"""scripts/repair_bse_qfq.py — BSE qfq 全历史回填（腾讯分页适配器）

Fills qfq history for BSE stocks whose qfq table is empty or shallow, using
the paging-capable TencentAdapter. Canonical lineage writes.

Usage (formal profile):
  python scripts/repair_bse_qfq.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.adapters.base import FetchRequest
from app.core.adapters.tencent_adapter import TencentAdapter
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("repair_bse_qfq")

PROGRESS_INTERVAL = 20
START = "2020-01-01"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    init = DataInitializer(duck=duck, sqlite=sqlite)
    tencent = TencentAdapter(0.25)

    # BSE stocks whose qfq lacks recent history
    targets = duck.read_query(
        """SELECT m.stock_code, m.listing_date FROM stock_meta m
           WHERE m.is_listed IS TRUE AND m.exchange = 'BSE'
             AND (
               (SELECT MAX(trade_date) FROM price_daily_qfq q WHERE q.stock_code = m.stock_code) IS NULL
               OR (SELECT COUNT(*) FROM price_daily_qfq q WHERE q.stock_code = m.stock_code AND q.close IS NOT NULL) < 100
             )
           ORDER BY m.stock_code"""
    )
    codes = [str(r["stock_code"]) for r in targets]
    logger.info("BSE qfq 回填目标: %d 只", len(codes))

    report: dict[str, Any] = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        "env": paths.env.value,
        "targets": len(codes),
        "stats": {"success": 0, "failed": 0, "rows": 0},
        "failed_codes": [],
        "finished_at": None,
    }

    for i, code in enumerate(codes):
        start = START
        try:
            result = tencent.fetch(
                FetchRequest(data_type="price_daily", stock_codes=[code],
                             start_date=start, end_date="2026-07-31", adjust="qfq")
            )
            if result.metadata.error or not result.data:
                report["stats"]["failed"] += 1
                report["failed_codes"].append(code)
                logger.warning("%s qfq fetch failed: %s", code, result.metadata.error)
                continue
            rows = [r for r in result.data if r.get("trade_date") and r.get("close") is not None]
            if not rows:
                report["stats"]["failed"] += 1
                report["failed_codes"].append(code)
                continue
            with duck.transaction() as conn:
                conn.executemany(
                    """INSERT INTO price_daily_qfq
                       (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                         open=COALESCE(excluded.open, price_daily_qfq.open),
                         high=COALESCE(excluded.high, price_daily_qfq.high),
                         low=COALESCE(excluded.low, price_daily_qfq.low),
                         close=COALESCE(excluded.close, price_daily_qfq.close),
                         volume=COALESCE(excluded.volume, price_daily_qfq.volume),
                         turnover=COALESCE(excluded.turnover, price_daily_qfq.turnover),
                         turnover_rate=COALESCE(excluded.turnover_rate, price_daily_qfq.turnover_rate)""",
                    [[code, r.get("trade_date"), r.get("open"), r.get("high"), r.get("low"),
                      r.get("close"), r.get("volume"), r.get("turnover"), r.get("turnover_rate")]
                     for r in rows],
                )
                batch_id = init._record_batch_in_connection(conn, result, "price_daily_qfq", len(rows))
                init._record_field_audit_in_connection(conn, result, rows, code, "trade_date", batch_id)
            report["stats"]["success"] += 1
            report["stats"]["rows"] += len(rows)
        except Exception as error:  # noqa: BLE001
            report["stats"]["failed"] += 1
            report["failed_codes"].append(code)
            logger.error("%s failed: %s", code, error)
        if (i + 1) % PROGRESS_INTERVAL == 0:
            logger.info("progress %d/%d %s", i + 1, len(codes), report["stats"])

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    evidence = Path("scripts/evidence") / f"bse_qfq_repair_{report['run_id']}.json"
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("evidence: %s %s", evidence, report["stats"])


if __name__ == "__main__":
    main()
