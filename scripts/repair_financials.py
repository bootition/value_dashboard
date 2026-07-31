"""scripts/repair_financials.py — 全市场最新期财务核心字段 lineage 重建（Sina）

Rebuilds replayable lineage for the readiness-relevant core financial fields at
the latest report period for every currently listed stock. Values are upserted
through the canonical single-transaction path (business rows + fetch_batch +
raw_response_archive + source_audit). num=1 keeps archive payloads small.

Usage (requires explicit profile env):
  python scripts/repair_financials.py [--max-stocks N] [--resume] [--rate-limit 0.35]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.adapters.base import FetchRequest
from app.core.adapters.sina_adapter import SinaAdapter
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("repair_financials")

PROGRESS_INTERVAL = 25
STATEMENTS = ["balance_sheet", "income_statement", "cash_flow"]


class FinancialRepairer:
    def __init__(
        self,
        *,
        max_stocks: int,
        resume: bool,
        rate_limit: float,
        evidence_dir: Path,
    ) -> None:
        self.paths = resolve_and_validate_paths()
        self.duck = DuckDBStore(paths=self.paths)
        self.sqlite = SQLiteStore(paths=self.paths)
        self._init = DataInitializer(duck=self.duck, sqlite=self.sqlite)
        self._sina = SinaAdapter(rate_limit=rate_limit)
        self.max_stocks = max_stocks
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.evidence_dir / f"repair_state_{self.paths.env.value}_financials.json"
        self.completed: set[str] = set()
        if resume and self.state_path.exists():
            self.completed = set(json.loads(self.state_path.read_text(encoding="utf-8")).get("completed", []))
            logger.info("Resume: %d codes already completed", len(self.completed))

    def _repair_stock(self, code: str) -> dict[str, Any]:
        outcomes: list[dict[str, Any]] = []
        for data_type in STATEMENTS:
            result = self._sina.fetch(
                FetchRequest(data_type=data_type, stock_codes=[code], extra_params={"num": "1"})
            )
            if result.metadata.error or not result.data:
                outcomes.append({
                    "data_type": data_type, "status": "failed",
                    "error": result.metadata.error or "empty",
                })
                continue
            rows = [row for row in result.data if row.get("report_date")]
            if not rows:
                outcomes.append({"data_type": data_type, "status": "failed", "error": "no report_date"})
                continue
            try:
                with self.duck.transaction() as conn:
                    for row in rows:
                        self._init._upsert_financial_row(conn, data_type, code, row)
                    batch_id = self._init._record_batch_in_connection(
                        conn, result, data_type, len(rows),
                    )
                    self._init._record_field_audit_in_connection(
                        conn, result, rows, code, "report_date", batch_id,
                    )
                outcomes.append({"data_type": data_type, "status": "success", "rows": len(rows)})
            except Exception as error:
                outcomes.append({"data_type": data_type, "status": "failed", "error": str(error)})
        failed = [o for o in outcomes if o["status"] != "success"]
        return {
            "status": "success" if not failed else "partial",
            "outcomes": outcomes,
        }

    def run(self) -> int:
        universe = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        targets = [str(r["stock_code"]) for r in universe if str(r["stock_code"]) not in self.completed]
        if self.max_stocks > 0:
            targets = targets[: self.max_stocks]
        logger.info("financial repair targets=%d", len(targets))

        stats = {"success": 0, "partial": 0, "rows": 0}
        failed_codes: list[str] = []
        for i, code in enumerate(targets):
            outcome = self._repair_stock(code)
            stats["success" if outcome["status"] == "success" else "partial"] += 1
            for o in outcome["outcomes"]:
                stats["rows"] += o.get("rows", 0)
                if o["status"] != "success":
                    failed_codes.append(f"{code}:{o['data_type']}")
            self.completed.add(code)
            if (i + 1) % PROGRESS_INTERVAL == 0:
                self._persist()
                logger.info("progress %d/%d stats=%s", i + 1, len(targets), stats)

        self._persist()
        report = {
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            "env": self.paths.env.value,
            "targets": len(targets),
            "stats": stats,
            "failed": len(failed_codes),
            "failed_samples": failed_codes[:50],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        evidence = self.evidence_dir / f"financial_repair_{report['run_id']}.json"
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("evidence written to %s", evidence)
        return 0 if not failed_codes else 2

    def _persist(self) -> None:
        self.state_path.write_text(
            json.dumps({"completed": sorted(self.completed)}, ensure_ascii=False), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rate-limit", type=float, default=0.35)
    parser.add_argument("--evidence-dir", type=Path, default=Path("scripts/evidence"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(FinancialRepairer(
        max_stocks=args.max_stocks,
        resume=args.resume,
        rate_limit=args.rate_limit,
        evidence_dir=args.evidence_dir,
    ).run())


if __name__ == "__main__":
    main()
