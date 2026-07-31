"""scripts/repair_dividends.py — 分红与公司行动补建（沪深 BaoStock / 北交所 THS+TDX）

Closes the corporate_action_dividend_lineage gap:
  - SSE/SZSE: BaoStock dividends (ex_date + plan announce date + per-share cash)
              through the canonical single-transaction write path.
  - BSE:      THS implementation announcements + existing TDX xdxr ex-dates,
              matched by date proximity and per-share amount agreement.
  - All exchanges: re-fetch xdxr via TDX (upsert, idempotent).

A dividend row is only written when it has an authoritative ex-date and an
announcement date; nothing is fabricated. Stocks whose sources report no
dividend event are recorded as missing (source_incomplete), not invented.

Usage (requires explicit profile env):
  python scripts/repair_dividends.py [--only-gap] [--max-stocks N] [--resume]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.adapters.baostock_adapter import BaoStockAdapter
from app.core.adapters.tdx_adapter import TDXAdapter
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("repair_dividends")

PROGRESS_INTERVAL = 25
DIVIDEND_HISTORY_START_YEAR = 2000
XIDR_MATCH_DAYS = 60
XIDR_AMOUNT_TOLERANCE = 0.10

_RE_CASH = re.compile(r"10派([\d.]+)")
_RE_SEND = re.compile(r"10送([\d.]+)")
_RE_TRANSFER = re.compile(r"10转([\d.]+)")


class DividendRepairer:
    def __init__(
        self,
        *,
        only_gap: bool,
        max_stocks: int,
        resume: bool,
        evidence_dir: Path,
    ) -> None:
        self.paths = resolve_and_validate_paths()
        self.duck = DuckDBStore(paths=self.paths)
        self.sqlite = SQLiteStore(paths=self.paths)
        self._init = DataInitializer(duck=self.duck, sqlite=self.sqlite)
        self._baostock = BaoStockAdapter(rate_limit=0.1, reuse_session=True)
        self._tdx = TDXAdapter(0.1)
        self.only_gap = only_gap
        self.max_stocks = max_stocks
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.evidence_dir / f"repair_state_{self.paths.env.value}_dividends.json"
        self.completed: set[str] = set()
        if resume and self.state_path.exists():
            self.completed = set(json.loads(self.state_path.read_text(encoding="utf-8")).get("completed", []))
            logger.info("Resume: %d codes already completed", len(self.completed))

    def _gap_codes(self) -> set[str]:
        rows = self.duck.read_query(
            """SELECT m.stock_code FROM stock_meta m
               WHERE m.is_listed IS TRUE
                 AND NOT (
                   EXISTS (SELECT 1 FROM xdxr x WHERE x.stock_code = m.stock_code)
                   AND EXISTS (SELECT 1 FROM dividends d
                               WHERE d.stock_code = m.stock_code AND d.announcement_date IS NOT NULL)
                 )"""
        )
        return {str(r["stock_code"]) for r in rows}

    def _fetch_bse_dividends(self, code: str) -> list[dict[str, Any]]:
        """THS implementation announcements + TDX xdxr ex-date/amount matching."""
        import akshare as ak

        try:
            frame = ak.stock_fhps_detail_ths(symbol=code)
        except Exception as error:
            logger.warning("ths dividends %s failed: %s", code, error)
            return []

        xdxr_rows = self.duck.read_query(
            "SELECT event_date, fenhong, songzhuangu FROM xdxr "
            "WHERE stock_code = ? AND category = 1 AND fenhong IS NOT NULL",
            [code],
        )
        xdxr_events: list[tuple[Any, float]] = [
            (r["event_date"], float(r["fenhong"])) for r in xdxr_rows
        ]
        if not xdxr_events:
            logger.info("bse %s has no xdxr dividend events to anchor ex-dates", code)
            return []

        records: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            plan = str(row.get("分红方案说明") or "")
            if not plan or "不分配" in plan or "不转增" in plan:
                continue
            announce_date = str(row.get("实施公告日") or "")
            if not announce_date or announce_date in {"NaT", "nan", "None"}:
                continue
            try:
                announce = datetime.strptime(announce_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            cash = float(_RE_CASH.search(plan).group(1)) / 10.0 if _RE_CASH.search(plan) else None
            send = float(_RE_SEND.search(plan).group(1)) / 10.0 if _RE_SEND.search(plan) else None
            transfer = float(_RE_TRANSFER.search(plan).group(1)) / 10.0 if _RE_TRANSFER.search(plan) else None
            if cash is None and send is None and transfer is None:
                continue

            best: tuple[Any, float] | None = None
            for event_date, fenhong in xdxr_events:
                if isinstance(event_date, str):
                    event_date = datetime.strptime(event_date[:10], "%Y-%m-%d").date()
                days = abs((event_date - announce).days)
                if days <= XIDR_MATCH_DAYS and (cash is None or abs(fenhong - cash) / max(fenhong, 1e-9) <= XIDR_AMOUNT_TOLERANCE):
                    if best is None or days < best[1]:
                        best = (event_date, days)
            if best is None:
                logger.info("bse %s no matching xdxr anchor for %s (%s)", code, announce_date, plan)
                continue
            records.append({
                "stock_code": code,
                "ex_date": str(best[0])[:10],
                "announcement_date": announce_date,
                "dividend_per_share": cash,
                "stock_dividend": send,
                "transfer_share": transfer,
                "rights_issue": None,
                "rights_issue_price": None,
            })
        return records

    def _repair_stock(self, code: str) -> dict[str, Any]:
        exchange = self.duck.read_query(
            "SELECT exchange FROM stock_meta WHERE stock_code = ?", [code]
        )[0]["exchange"]

        # 1) dividends
        dividend_rows: list[dict[str, Any]] = []
        if exchange == "BSE":
            dividend_rows = self._fetch_bse_dividends(code)
        else:
            result = self._baostock_fetch_dividends(code)
            if result.metadata.error and not result.data:
                return {"status": "failed", "reason": result.metadata.error}
            dividend_rows = result.data

        # 2) xdxr (all exchanges, TDX)
        from app.core.adapters.base import FetchRequest

        xdxr_result = self._tdx.fetch(FetchRequest(data_type="xdxr", stock_codes=[code]))

        written_div = 0
        written_xdxr = 0
        if dividend_rows:
            source_result = result if exchange != "BSE" else self._make_bse_result(code, dividend_rows)
            try:
                with self.duck.transaction() as conn:
                    self._init._upsert_dividend_rows(conn, code, dividend_rows)
                    batch_id = self._init._record_batch_in_connection(
                        conn, source_result, "dividends", len(dividend_rows),
                    )
                    self._init._record_field_audit_in_connection(
                        conn, source_result, dividend_rows, code, "ex_date", batch_id,
                    )
                written_div = len(dividend_rows)
            except Exception as error:
                logger.error("dividends write failed for %s: %s", code, error)
                return {"status": "failed", "reason": f"dividends write: {error}"}

        if xdxr_result.data and not xdxr_result.metadata.error:
            try:
                with self.duck.transaction() as conn:
                    self._init._upsert_xdxr_rows(conn, code, xdxr_result.data)
                    batch_id = self._init._record_batch_in_connection(
                        conn, xdxr_result, "xdxr", len(xdxr_result.data),
                    )
                    self._init._record_field_audit_in_connection(
                        conn, xdxr_result, xdxr_result.data, code, "event_date", batch_id,
                    )
                written_xdxr = len(xdxr_result.data)
            except Exception as error:
                logger.error("xdxr write failed for %s: %s", code, error)

        has_div_lineage = bool(
            self.duck.read_query(
                "SELECT 1 FROM dividends WHERE stock_code = ? AND announcement_date IS NOT NULL LIMIT 1",
                [code],
            )
        )
        has_xdxr = bool(self.duck.read_query("SELECT 1 FROM xdxr WHERE stock_code = ? LIMIT 1", [code]))
        return {
            "status": "success" if (has_div_lineage or has_xdxr) else "no_events",
            "dividends_written": written_div,
            "xdxr_written": written_xdxr,
        }

    def _baostock_fetch_dividends(self, code: str) -> Any:
        from app.core.adapters.base import FetchRequest

        return self._baostock.fetch(FetchRequest(data_type="dividends", stock_codes=[code]))

    def _make_bse_result(self, code: str, rows: list[dict[str, Any]]) -> Any:
        from app.core.adapters.base import FetchResult, SourceMetadata
        import hashlib

        raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="ths",
                fetch_time=datetime.now(timezone.utc),
                raw_response_hash=hashlib.sha256(raw).hexdigest(),
                confidence="approximate",
                api_version="ths-dividend-1",
                row_count=len(rows),
            ),
            raw_response=raw,
        )

    def run(self) -> int:
        universe = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        all_codes = [str(r["stock_code"]) for r in universe]
        gap_codes = self._gap_codes()
        targets = list(gap_codes if self.only_gap else set(all_codes) - self.completed)
        if self.only_gap:
            targets = [c for c in targets if c not in self.completed]
        else:
            targets = [c for c in targets if c not in self.completed]
        if self.max_stocks > 0:
            targets = targets[: self.max_stocks]
        logger.info("targets=%d (gap=%d, only_gap=%s)", len(targets), len(gap_codes), self.only_gap)

        stats = {"success": 0, "failed": 0, "no_events": 0, "div_rows": 0, "xdxr_rows": 0}
        failed_codes: list[str] = []
        for i, code in enumerate(targets):
            outcome = self._repair_stock(code)
            if outcome["status"] == "failed":
                stats["failed"] += 1
                failed_codes.append(code)
                logger.warning("failed %s: %s", code, outcome.get("reason"))
            elif outcome["status"] == "no_events":
                stats["no_events"] += 1
            else:
                stats["success"] += 1
            stats["div_rows"] += outcome.get("dividends_written", 0)
            stats["xdxr_rows"] += outcome.get("xdxr_written", 0)
            self.completed.add(code)
            if (i + 1) % PROGRESS_INTERVAL == 0:
                self._persist()
                logger.info("progress %d/%d stats=%s", i + 1, len(targets), stats)

        self._persist()
        report = {
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            "env": self.paths.env.value,
            "only_gap": self.only_gap,
            "targets": len(targets),
            "gap_before": len(gap_codes),
            "stats": stats,
            "failed_codes": failed_codes[:100],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        evidence = self.evidence_dir / f"dividend_repair_{report['run_id']}.json"
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("evidence written to %s", evidence)
        return 0 if stats["failed"] == 0 else 2

    def _persist(self) -> None:
        self.state_path.write_text(
            json.dumps({"completed": sorted(self.completed)}, ensure_ascii=False), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-gap", action="store_true")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, default=Path("scripts/evidence"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(DividendRepairer(
        only_gap=args.only_gap,
        max_stocks=args.max_stocks,
        resume=args.resume,
        evidence_dir=args.evidence_dir,
    ).run())


if __name__ == "__main__":
    main()
