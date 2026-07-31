"""scripts/finalize_rebuild.py — 数据重建收尾（价格完成后顺序执行）

Steps (all formal-profile, serialized):
  1. update trading calendar to the latest trading day (BaoStock)
  2. repair dividends/xdxr gaps (scripts/repair_dividends.py --only-gap)
  3. quarantine legacy empty-payload lineage (data_maintenance)
  4. recompute indicator snapshot through the publish gate
  5. run diagnostics (readiness + warning codes) and write evidence JSON

Usage:
  python scripts/finalize_rebuild.py --evidence-dir docs
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.data_maintenance import legacy_quarantine_summary, quarantine_legacy_records
from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("finalize_rebuild")


def update_trading_calendar(manager: AdapterManager, sqlite: SQLiteStore) -> dict:
    result = manager.fetch(FetchRequest(data_type="trading_dates"))
    if result.metadata.error or not result.data:
        return {"status": "failed", "error": result.metadata.error}
    with sqlite.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trading_dates (trade_date TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT OR REPLACE INTO trading_dates (trade_date) VALUES (?)",
            [(r["trade_date"],) for r in result.data],
        )
    latest = max(str(r["trade_date"]) for r in result.data)
    return {"status": "success", "count": len(result.data), "latest": latest}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", default="docs")
    parser.add_argument("--skip-dividends", action="store_true")
    parser.add_argument("--skip-quarantine", action="store_true")
    parser.add_argument("--skip-snapshot", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    report: dict = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        "env": paths.env.value,
        "steps": {},
        "finished_at": None,
    }

    # 1) trading calendar
    manager = AdapterManager()
    report["steps"]["trading_calendar"] = update_trading_calendar(manager, sqlite)
    logger.info("trading calendar: %s", report["steps"]["trading_calendar"]["status"])

    # 2) dividends / xdxr repair
    if not args.skip_dividends:
        from repair_dividends import DividendRepairer

        repairer = DividendRepairer(
            only_gap=True, max_stocks=0, resume=True,
            evidence_dir=Path("scripts/evidence"),
        )
        outcome = repairer.run()
        report["steps"]["dividends"] = {
            "status": "success" if outcome == 0 else "partial",
            "exit_code": outcome,
            "report": json.loads(
                sorted(Path("scripts/evidence").glob("dividend_repair_*.json"))[-1].read_text(encoding="utf-8")
            ) if sorted(Path("scripts/evidence").glob("dividend_repair_*.json")) else None,
        }
        logger.info("dividends repair exit=%d", outcome)

    # 3) legacy lineage quarantine
    if not args.skip_quarantine:
        before = legacy_quarantine_summary(duck)
        result = quarantine_legacy_records(duck)
        report["steps"]["quarantine"] = {"before": before, "quarantined": result}
        logger.info("quarantine: %s", result)

    # 4) snapshot recompute through publish gate
    if not args.skip_snapshot:
        snapshot = IndicatorCalculator(duck=duck, sqlite=sqlite).compute_snapshot_for_all()
        report["steps"]["snapshot"] = snapshot
        logger.info("snapshot: %s %s", snapshot.get("status"), snapshot.get("reason", ""))

    # 5) diagnostics
    from app.core.data_quality import build_data_quality_status, screening_readiness

    report["steps"]["quality"] = build_data_quality_status(duck, sqlite)
    report["steps"]["screening_ready"] = screening_readiness(duck, sqlite)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"evidence-finalize-{report['run_id']}.json"
    evidence_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    logger.info("evidence written to %s", evidence_path)
    logger.info(
        "screening ready=%s warnings=%s",
        report["steps"]["screening_ready"].get("ready"),
        report["steps"]["screening_ready"].get("warning_codes"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
