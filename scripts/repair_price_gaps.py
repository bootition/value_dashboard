"""scripts/repair_price_gaps.py — 回填日历门禁超限股票的历史价格缺口

Targets (from audit): 600062/600714 raw 历史缺口 (BaoStock 全历史 raw),
000560 qfq 2017-2020 缺口 (Tencent qfq 全历史). Canonical lineage writes.

Usage (formal profile):
  python scripts/repair_price_gaps.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.adapters.baostock_adapter import BaoStockAdapter
from app.core.adapters.tencent_adapter import TencentAdapter
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("repair_price_gaps")

# (stock_code, table, adjust, start_date, source)
TARGETS = [
    ("600062", "price_daily_raw", "raw", "2005-01-01", "baostock"),
    ("600714", "price_daily_raw", "raw", "2012-01-01", "baostock"),
    ("000560", "price_daily_qfq", "qfq", "2017-01-01", "tencent"),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    init = DataInitializer(duck=duck, sqlite=sqlite)
    baostock = BaoStockAdapter(rate_limit=0.15, reuse_session=True)
    tencent = TencentAdapter(0.25)

    report: dict[str, Any] = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        "env": paths.env.value,
        "rows": {},
        "finished_at": None,
    }
    try:
        for code, table, adjust, start, source in TARGETS:
            adapter = baostock if source == "baostock" else tencent
            from app.core.adapters.base import FetchRequest

            # Tencent caps one response at 640 bars (most recent in window);
            # page backwards so every segment is covered.
            windows = [(start, "2026-07-31")]
            if source == "tencent":
                windows = [
                    ("2017-01-01", "2021-12-31"),   # returns ~2019-06..2021-12
                    ("2017-01-01", "2019-05-31"),   # returns ~2017-09..2019-05
                    ("2017-01-01", "2017-08-31"),   # returns ~2017-01..2017-08
                ]
            all_rows: list[dict[str, Any]] = []
            last_error: str | None = None
            for w_start, w_end in windows:
                result = adapter.fetch(
                    FetchRequest(data_type="price_daily", stock_codes=[code],
                                 start_date=w_start, end_date=w_end, adjust=adjust)
                )
                if result.metadata.error:
                    last_error = result.metadata.error
                    logger.warning("%s %s %s~%s failed: %s", code, adjust, w_start, w_end, result.metadata.error)
                    continue
                all_rows.extend(r for r in result.data if r.get("trade_date") and r.get("close") is not None)
            rows = all_rows
            if not rows:
                report["rows"][code] = {"status": "failed", "error": last_error or "empty"}
                logger.warning("%s %s: no rows", code, adjust)
                continue
            rows.sort(key=lambda r: str(r["trade_date"]))
            logger.info("%s %s: %d rows (first %s last %s)", code, adjust, len(rows),
                        rows[0]["trade_date"], rows[-1]["trade_date"])
            try:
                with duck.transaction() as conn:
                    if table == "price_daily_raw":
                        conn.executemany(
                            """INSERT INTO price_daily_raw
                               (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                                 open=COALESCE(excluded.open, price_daily_raw.open),
                                 high=COALESCE(excluded.high, price_daily_raw.high),
                                 low=COALESCE(excluded.low, price_daily_raw.low),
                                 close=COALESCE(excluded.close, price_daily_raw.close),
                                 volume=COALESCE(excluded.volume, price_daily_raw.volume),
                                 turnover=COALESCE(excluded.turnover, price_daily_raw.turnover),
                                 turnover_rate=COALESCE(excluded.turnover_rate, price_daily_raw.turnover_rate)""",
                            [[code, r.get("trade_date"), r.get("open"), r.get("high"), r.get("low"),
                              r.get("close"), r.get("volume"), r.get("turnover"), r.get("turnover_rate")]
                             for r in rows],
                        )
                    else:
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
                    batch_id = init._record_batch_in_connection(conn, result, table, len(rows))
                    init._record_field_audit_in_connection(conn, result, rows, code, "trade_date", batch_id)
                report["rows"][code] = {"status": "success", "rows": len(rows)}
            except Exception as error:  # noqa: BLE001
                logger.error("%s persist failed: %s", code, error)
                report["rows"][code] = {"status": "failed", "error": str(error)}
    finally:
        baostock.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    evidence = Path("scripts/evidence") / f"price_gaps_repair_{report['run_id']}.json"
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("evidence: %s", evidence)


if __name__ == "__main__":
    main()
