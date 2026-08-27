"""scripts/repair_prices.py — 全市场价格新鲜度更新与历史缺口回填

Two phases:
  backfill    — fetch [start_expected, today] for stocks whose raw/qfq history
                does not reach back to 2021-01-01 (or the listing date if later)
                OR whose 2021+ window is significantly short (mid-history gaps).
  incremental — fetch [local_max+1, today] for every currently listed stock so
                raw and qfq both reach the latest trading day.
  both        — backfill targets use the full window; all other stocks use the
                incremental window.

Adapter routing (per task spec):
  SSE/SZSE raw+qfq  -> baostock (single reused session)
  BSE raw           -> tdx
  BSE qfq           -> tencent (returns day bars when no adjustment factor)
Fallbacks: SSE/SZSE raw: tdx -> tencent; SSE/SZSE qfq: tencent; BSE raw: tencent.

Write policy (mirrors app/core/init.py canonical path):
  one DuckDB transaction per stock that commits price_daily_raw + price_daily_qfq
  + fetch_batch + raw_response_archive + source_audit together, keeping old
  values (COALESCE) and archiving the raw provider response by content hash.

Usage (requires explicit profile env):
  python scripts/repair_prices.py --phase both [--max-stocks N] [--codes-file f]
      [--resume] [--rate-limit 0.1]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.adapters.baostock_adapter import BaoStockAdapter
from app.core.adapters.base import FetchRequest, FetchResult
from app.core.adapters.tdx_adapter import TDXAdapter
from app.core.adapters.tencent_adapter import TencentAdapter
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("repair_prices")

BACKFILL_START = "2021-01-01"
WINDOW_COVERAGE_FRACTION = 0.85  # share of expected trading days required to trust history
PROGRESS_INTERVAL = 25  # persist resume state every N stocks


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _is_bse(code: str) -> bool:
    return code[:1] in ("4", "8", "9")


class PriceRepairer:
    """Execute the full-market price repair with canonical lineage writes."""

    def __init__(
        self,
        *,
        phase: str,
        max_stocks: int,
        codes_file: Path | None,
        resume: bool,
        rate_limit: float,
        evidence_dir: Path,
        retry_codes: list[str] | None = None,
    ) -> None:
        self.paths = resolve_and_validate_paths()
        self.duck = DuckDBStore(paths=self.paths)
        self.sqlite = SQLiteStore(paths=self.paths)
        self.phase = phase
        self.max_stocks = max_stocks
        self.codes_file = codes_file
        self.evidence_dir = evidence_dir
        self.rate_limit = rate_limit

        self._baostock = BaoStockAdapter(rate_limit=rate_limit, reuse_session=True)
        self._tdx = TDXAdapter(0.1)
        self._tencent = TencentAdapter(0.2)
        self._init = DataInitializer(duck=self.duck, sqlite=self.sqlite)

        self.run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.evidence_dir / f"repair_state_{self.paths.env.value}_{phase}.json"
        self.completed: set[str] = set()
        if resume and self.state_path.exists():
            self.completed = set(json.loads(self.state_path.read_text(encoding="utf-8")).get("completed", []))
            logger.info("Resume: %d codes already completed", len(self.completed))
        if retry_codes:
            for code in retry_codes:
                self.completed.discard(code)
            logger.info("Retry: %d codes re-queued", len(retry_codes))

    # ─── target selection ──────────────────────────────────────────

    def _load_universe(self) -> list[dict[str, Any]]:
        rows = self.duck.read_query(
            "SELECT stock_code, exchange, listing_date FROM stock_meta "
            "WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        return [dict(r) for r in rows]

    def _trading_days(self) -> set[str]:
        """Full trading calendar from SQLite (duckdb keeps no calendar)."""
        try:
            rows = self.sqlite.query("SELECT trade_date FROM trading_dates")
            return {str(r["trade_date"])[:10] for r in rows}
        except Exception:
            return set()

    def _window_row_counts(self, table: str) -> dict[str, int]:
        rows = self.duck.read_query(
            f"SELECT stock_code, COUNT(*) AS n FROM {table} "
            f"WHERE trade_date >= DATE '{BACKFILL_START}' GROUP BY stock_code"
        )
        return {str(r["stock_code"]): int(r["n"]) for r in rows}

    def _backfill_targets(
        self, universe: list[dict[str, Any]], calendar: set[str], today: str
    ) -> dict[str, dict[str, Any]]:
        """Stocks whose raw/qfq history is incomplete since 2021-01-01 (or listing)."""
        raw_counts = self._window_row_counts("price_daily_raw")
        qfq_counts = self._window_row_counts("price_daily_qfq")
        min_dates = self._min_dates("price_daily_raw")
        qfq_min_dates = self._min_dates("price_daily_qfq")

        targets: dict[str, dict[str, Any]] = {}
        for stock in universe:
            code = stock["stock_code"]
            listing = str(stock.get("listing_date") or "")[:10]
            start_expected = max(BACKFILL_START, listing) if listing and listing >= BACKFILL_START else BACKFILL_START
            if start_expected > today:
                continue
            expected = sum(1 for d in calendar if start_expected <= d <= today)
            if expected <= 0:
                expected = 1
            threshold = max(1, int(expected * WINDOW_COVERAGE_FRACTION))
            raw_ok = (
                raw_counts.get(code, 0) >= threshold
                and min_dates.get(code, "9999-12-31") <= start_expected
            )
            qfq_ok = (
                qfq_counts.get(code, 0) >= threshold
                and qfq_min_dates.get(code, "9999-12-31") <= start_expected
            )
            if not raw_ok or not qfq_ok:
                targets[code] = {"stock_code": code, "exchange": stock["exchange"],
                                 "start_expected": start_expected}
        return targets

    def _min_dates(self, table: str) -> dict[str, str]:
        rows = self.duck.read_query(
            f"SELECT stock_code, MIN(trade_date) AS d FROM {table} GROUP BY stock_code"
        )
        return {str(r["stock_code"]): str(r["d"])[:10] for r in rows}

    def _local_max_dates(self, codes: list[str]) -> dict[str, str]:
        """Per-code minimum of raw/qfq max trade_date (start point for incremental)."""
        result: dict[str, str] = {}
        for table in ("price_daily_raw", "price_daily_qfq"):
            if not codes:
                break
            rows = self.duck.read_query(
                f"SELECT stock_code, MAX(trade_date) AS d FROM {table} "
                f"WHERE stock_code IN ({','.join('?' for _ in codes)}) GROUP BY stock_code",
                codes,
            )
            for r in rows:
                code = str(r["stock_code"])
                day = str(r["d"])[:10]
                result[code] = min(result.get(code, "9999-12-31"), day) if r["d"] is not None else result.get(code, "9999-12-31")
        return {code: (day if day != "9999-12-31" else BACKFILL_START) for code, day in result.items()}

    # ─── fetching ───────────────────────────────────────────────────

    def _fetch_price(
        self, code: str, adjust: str, start: str, end: str
    ) -> tuple[FetchResult | None, str, str]:
        """Try the routing chain; return (result, source, status).

        status: "ok" — data returned; "empty" — legal empty (no bars in window);
                "failed" — every adapter errored.
        """
        is_bse = _is_bse(code)
        if is_bse:
            chain = (
                [("tencent", self._tencent), ("tdx", self._tdx)]
                if adjust == "raw"
                else [("tencent", self._tencent)]
            )
        else:
            chain = (
                [("baostock", self._baostock), ("tdx", self._tdx), ("tencent", self._tencent)]
                if adjust == "raw"
                else [("baostock", self._baostock), ("tencent", self._tencent)]
            )
        last_result: FetchResult | None = None
        last_name = "unknown"
        for name, adapter in chain:
            try:
                result = adapter.fetch(
                    FetchRequest(
                        data_type="price_daily",
                        stock_codes=[code],
                        start_date=start,
                        end_date=end,
                        adjust=adjust,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — adapters return errors, never raise
                logger.warning("  %s %s %s raised: %s", name, code, adjust, exc)
                last_name = name
                continue
            last_result, last_name = result, name
            if result.metadata.error is None and result.data:
                return result, name, "ok"
        if last_result is not None and last_result.metadata.error is None:
            return last_result, last_name, "empty"
        logger.warning("  all adapters failed for %s %s", code, adjust)
        return None, last_name, "failed"

    # ─── persistence ────────────────────────────────────────────────

    def _persist_pair(self, code: str, raw_res: FetchResult, qfq_res: FetchResult) -> None:
        with self.duck.transaction() as conn:
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
                self._row_tuples(code, raw_res.data),
            )
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
                self._row_tuples(code, qfq_res.data),
            )
            raw_batch_id = self._init._record_batch_in_connection(
                conn, raw_res, "price_daily_raw", len(raw_res.data)
            )
            self._init._record_field_audit_in_connection(
                conn, raw_res, raw_res.data, code, "trade_date", raw_batch_id
            )
            qfq_batch_id = self._init._record_batch_in_connection(
                conn, qfq_res, "price_daily_qfq", len(qfq_res.data)
            )
            self._init._record_field_audit_in_connection(
                conn, qfq_res, qfq_res.data, code, "trade_date", qfq_batch_id
            )

    @staticmethod
    def _row_tuples(code: str, rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [
            (
                code,
                r.get("trade_date"),
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                r.get("volume"),
                r.get("turnover"),
                r.get("turnover_rate"),
            )
            for r in rows
        ]

    # ─── failure bookkeeping (SQLite) ───────────────────────────────

    def _clear_price_retries(self, code: str) -> None:
        """修复成功后移除该股价格域的历史重试记录，防止页面继续显示已解决任务。"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """DELETE FROM retry_list
                       WHERE stock_code = ? AND data_type = 'price_daily'""",
                    [code],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("clear retry_list for %s failed: %s", code, exc)

    def _record_failure(self, code: str, adapter: str, error: str, adjust: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, ?)
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [code, "price_daily", adapter, error[:500],
                     datetime.now(UTC).isoformat(), json.dumps({"adjust": adjust})],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("retry_list write failed for %s: %s", code, exc)

    def _record_missing(self, code: str, reason: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO missing_list (stock_code, field_name, reason_code) VALUES (?, ?, ?)",
                    [code, "price_daily", reason],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("missing_list write failed for %s: %s", code, exc)

    # ─── run ────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        report: dict[str, Any] = {
            "run_id": self.run_id,
            "env": self.paths.env.value,
            "phase": self.phase,
            "started_at": datetime.now(UTC).isoformat(),
            "duckdb_path": str(self.paths.duckdb_path),
            "targets": {"requested": 0, "backfill_start": BACKFILL_START},
            "success": 0,
            "failed": 0,
            "skipped_empty": 0,
            "skipped_up_to_date": 0,
            "derived_qfq_from_raw": 0,
            "rows_written": {"raw": 0, "qfq": 0},
            "failed_codes": [],
            "empty_codes": [],
            "sources": {"raw": Counter(), "qfq": Counter()},
        }

        universe = self._load_universe()
        by_code = {u["stock_code"]: u for u in universe}
        today = _today()
        calendar = self._trading_days()

        backfill_targets: dict[str, dict[str, Any]] = {}
        if self.phase in ("backfill", "both"):
            backfill_targets = self._backfill_targets(universe, calendar, today)
            logger.info("backfill targets (start/mid-history gaps): %d", len(backfill_targets))

        if self.codes_file is not None:
            codes = [
                line.strip()
                for line in self.codes_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            targets = [by_code[c] for c in codes if c in by_code]
            logger.info("codes-file targets: %d", len(targets))
        elif self.phase == "backfill":
            targets = [u for u in universe if u["stock_code"] in backfill_targets]
        else:
            targets = universe
            logger.info("incremental targets (all listed): %d", len(targets))

        if self.max_stocks > 0:
            targets = targets[: self.max_stocks]

        report["targets"]["requested"] = len(targets)
        report["targets"]["backfill_count"] = len(backfill_targets)

        local_maxes = (
            self._local_max_dates([t["stock_code"] for t in targets])
            if self.phase in ("incremental", "both")
            else {}
        )

        rows_raw = 0
        rows_qfq = 0
        total = len(targets)

        for i, target in enumerate(targets, start=1):
            code = target["stock_code"]
            if code in self.completed:
                continue
            listing = str(target.get("listing_date") or "")[:10]
            start_expected = (
                max(BACKFILL_START, listing) if listing and listing >= BACKFILL_START else BACKFILL_START
            )

            if self.phase == "backfill":
                start = start_expected
            else:
                local_max = local_maxes.get(code)
                if local_max and local_max >= today:
                    report["skipped_up_to_date"] += 1
                    self.completed.add(code)
                    continue
                if self.phase == "incremental" or code not in backfill_targets:
                    start = (
                        (datetime.strptime(local_max, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                        if local_max
                        else start_expected
                    )
                else:
                    start = start_expected

            if start > today:
                self.completed.add(code)
                continue

            raw_fetch = self._fetch_price(code, "raw", start, today)
            if _is_bse(code):
                # No free source serves BSE qfq history: tencent only holds the
                # 920-segment (1 bar) and akshare eastmoney is rejected by the
                # provider. BSE securities rarely have adjustment events, so qfq
                # is derived from the tdx raw response (per task: raw == qfq is
                # acceptable). Lineage stays honest: the qfq fetch_batch and
                # field audit reference the same archived tdx raw payload.
                if raw_fetch[2] == "ok":
                    qfq_fetch = raw_fetch
                else:
                    qfq_fetch = self._fetch_price(code, "qfq", start, today)
            else:
                qfq_fetch = self._fetch_price(code, "qfq", start, today)

            if raw_fetch[2] == "failed" or qfq_fetch[2] == "failed":
                if raw_fetch[2] == "failed":
                    self._record_failure(code, raw_fetch[1], "all adapters failed", "raw")
                if qfq_fetch[2] == "failed":
                    self._record_failure(code, qfq_fetch[1], "all adapters failed", "qfq")
                report["failed"] += 1
                report["failed_codes"].append(code)
                self.completed.add(code)
                self._save_state()
                continue

            if raw_fetch[2] == "empty" or qfq_fetch[2] == "empty":
                if raw_fetch[2] == "empty" and qfq_fetch[2] == "empty":
                    report["skipped_empty"] += 1
                    report["empty_codes"].append(code)
                    self._record_missing(code, "no_bars_in_window")
                else:
                    report["failed"] += 1
                    report["failed_codes"].append(code)
                    self._record_failure(
                        code,
                        raw_fetch[1] if raw_fetch[2] == "empty" else qfq_fetch[1],
                        "one side empty; pair not persisted",
                        "raw" if raw_fetch[2] == "empty" else "qfq",
                    )
                self.completed.add(code)
                self._save_state()
                continue

            raw_res, raw_source, _ = raw_fetch
            qfq_res, qfq_source, _ = qfq_fetch
            assert raw_res is not None and qfq_res is not None

            try:
                self._persist_pair(code, raw_res, qfq_res)
                self._clear_price_retries(code)
            except Exception as exc:  # noqa: BLE001
                logger.error("  persist failed for %s: %s", code, exc)
                self._record_failure(code, "duckdb", str(exc)[:500], "raw")
                report["failed"] += 1
                report["failed_codes"].append(code)
                self.completed.add(code)
                self._save_state()
                continue

            rows_raw += len(raw_res.data)
            rows_qfq += len(qfq_res.data)
            report["success"] += 1
            report["sources"]["raw"][raw_source] += 1
            report["sources"]["qfq"][qfq_source] += 1
            if _is_bse(code):
                report["derived_qfq_from_raw"] += 1
            self.completed.add(code)
            if i % PROGRESS_INTERVAL == 0 or i == total:
                self._save_state()
                logger.info(
                    "progress %d/%d  ok=%d fail=%d empty=%d uptodate=%d",
                    i, total, report["success"], report["failed"],
                    report["skipped_empty"], report["skipped_up_to_date"],
                )

        report["rows_written"]["raw"] = rows_raw
        report["rows_written"]["qfq"] = rows_qfq
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["duration_seconds"] = round(time.monotonic() - started, 1)
        report["sources"]["raw"] = dict(report["sources"]["raw"])
        report["sources"]["qfq"] = dict(report["sources"]["qfq"])
        self._save_state(final=True)
        return report

    def _save_state(self, final: bool = False) -> None:
        state = {
            "run_id": self.run_id,
            "phase": self.phase,
            "final": final,
            "updated_at": datetime.now(UTC).isoformat(),
            "completed": sorted(self.completed),
        }
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def close(self) -> None:
        try:
            self._baostock.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("baostock close: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Full-market price freshness + history repair")
    parser.add_argument("--phase", choices=["incremental", "backfill", "both"], default="both")
    parser.add_argument("--max-stocks", type=int, default=0, help="0 = all targets")
    parser.add_argument("--codes-file", type=Path, default=None, help="one stock code per line")
    parser.add_argument("--resume", action="store_true", help="resume from saved state")
    parser.add_argument("--rate-limit", type=float, default=0.1, help="baostock rate limit seconds")
    parser.add_argument("--evidence-dir", type=Path, default=Path("scripts/evidence"))
    parser.add_argument("--retry-codes", nargs="*", default=None, help="re-queue codes from completed")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    repairer = PriceRepairer(
        phase=args.phase,
        max_stocks=args.max_stocks,
        codes_file=args.codes_file,
        resume=args.resume,
        rate_limit=args.rate_limit,
        evidence_dir=args.evidence_dir,
        retry_codes=args.retry_codes,
    )
    try:
        report = repairer.run()
    finally:
        repairer.close()

    evidence_path = repairer.evidence_dir / f"price_repair_{repairer.run_id}.json"
    evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("evidence written: %s", evidence_path)


if __name__ == "__main__":
    main()
