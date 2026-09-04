"""ETF 行情与跟踪指数分位采集服务（2026-09-05，同花顺官方 Financial-API）

写入 DuckDB etf_daily：
- 日线行情（close/open/high/low/volume/turnover），单窗口 ≤5 自然年；
- 跟踪指数 PE-TTM 五年分位（同日期合并），供无指数估值历史的 ETF
  （港股科技/中概互联等）兜底信号。

语义边界：
- 源失败保留旧值并登记 retry（data_type=etf_daily, adapter=ths）。
- 每日低频：每只 ETF 最多 2 次请求；免费额度纪律（不做实时轮询）。
- 独立于 A 股 readiness；失败绝不阻断个股主链。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.ths_adapter import ThsAdapter, normalize_thscode
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["EtfPriceUpdater", "MAX_YEARS"]

MAX_YEARS = 5  # THS ETF 日线端点单窗口最长 5 自然年


class EtfPriceUpdater:
    """ETF 日线 + 跟踪指数五年分位更新执行器（ths 单实例限速 0.5s）。"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("EtfPriceUpdater requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("EtfPriceUpdater stores do not match injected paths")
        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        self._ths = ThsAdapter(rate_limit=0.5)

    # ─── 单只更新 ──────────────────────────────────────────────────────

    def update_etf(self, etf_code: str, *, years: int = MAX_YEARS) -> dict[str, Any]:
        years = max(1, min(int(years), MAX_YEARS))
        thscode = normalize_thscode(etf_code)
        end = date.today()
        start = end - timedelta(days=365 * years)

        daily = self._ths.fetch(FetchRequest(
            data_type="etf_daily", stock_codes=[thscode],
            start_date=start.isoformat(), end_date=end.isoformat(),
        ))
        if daily.metadata.error:
            self._record_retry(etf_code, daily.metadata.error)
            return {"status": "failed", "error": daily.metadata.error}

        track = self._ths.fetch(FetchRequest(
            data_type="etf_track_percentile", stock_codes=[thscode],
            start_date=start.isoformat(), end_date=end.isoformat(),
        ))
        if track.metadata.error:
            logger.warning("ETF %s 跟踪分位抓取失败(非致命): %s", etf_code, track.metadata.error)
        track_by_date = {
            row.get("trade_date"): row.get("track_index_pe_ttm_five_year_percentile")
            for row in track.data if row.get("trade_date")
        }

        if not daily.data:
            self._record_missing(etf_code, "source_empty")
            return {"status": "missing", "rows": 0}

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(UTC)
        with self.duck.transaction() as conn:
            conn.executemany(
                """INSERT INTO etf_daily
                   (etf_code, trade_date, close_price, open_price, high_price, low_price,
                    volume, turnover, track_pe_ttm_five_year_percentile,
                    source, fetch_time, raw_hash, confidence, batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(etf_code, trade_date, source) DO UPDATE SET
                     close_price=excluded.close_price, open_price=excluded.open_price,
                     high_price=excluded.high_price, low_price=excluded.low_price,
                     volume=excluded.volume, turnover=excluded.turnover,
                     track_pe_ttm_five_year_percentile=excluded.track_pe_ttm_five_year_percentile,
                     fetch_time=excluded.fetch_time, raw_hash=excluded.raw_hash,
                     confidence=excluded.confidence, batch_id=excluded.batch_id""",
                [
                    [
                        etf_code,
                        row.get("trade_date"),
                        row.get("close_price"),
                        row.get("open_price"),
                        row.get("high_price"),
                        row.get("low_price"),
                        row.get("volume"),
                        row.get("turnover"),
                        track_by_date.get(row.get("trade_date")),
                        daily.metadata.source,
                        fetch_time,
                        daily.metadata.raw_response_hash,
                        daily.metadata.confidence,
                        batch_id,
                    ]
                    for row in daily.data
                ],
            )
        return {
            "status": "success",
            "rows": len(daily.data),
            "track_rows": len(track.data),
            "track_error": track.metadata.error,
        }

    def update_all(self, codes: list[str] | None = None, *, years: int = MAX_YEARS) -> dict[str, Any]:
        """更新全部启用 ETF（或指定代码），单实例限速逐只串行。"""
        metas = self.sqlite.query(
            "SELECT etf_code, enabled FROM etf_meta ORDER BY etf_code"
        )
        selected = [row["etf_code"] for row in metas if bool(row["enabled"])]
        if codes:
            selected = [c for c in codes if c in set(selected)]
        report: dict[str, Any] = {"status": "success", "etfs": {}, "summary": {
            "success": 0, "missing": 0, "failed": 0,
        }}
        for etf_code in selected:
            try:
                outcome = self.update_etf(etf_code, years=years)
            except Exception as error:  # noqa: BLE001
                outcome = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
            report["etfs"][etf_code] = outcome
            report["summary"][outcome["status"]] = report["summary"].get(outcome["status"], 0) + 1
        if report["summary"]["failed"]:
            report["status"] = "partial"
        return report

    # ─── retry / missing 维护 ───────────────────────────────────────────

    def _record_retry(self, etf_code: str, error: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, 'etf_daily', 'ths', ?, 0, ?, '{}')
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [etf_code, error[:500], datetime.now(UTC).isoformat()],
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("记录 ETF 行情失败信息失败: %s", e)

    def _record_missing(self, etf_code: str, reason_code: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, 'etf_daily', ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [etf_code, reason_code],
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("记录 ETF 行情缺失信息失败: %s", e)

    # ─── 只读状态 ───────────────────────────────────────────────────────

    def status_report(self) -> dict[str, Any]:
        try:
            rows = self.duck.read_query(
                """SELECT etf_code, COUNT(*) AS n, MIN(trade_date) AS min_date,
                          MAX(trade_date) AS max_date,
                          COUNT(track_pe_ttm_five_year_percentile) AS track_n
                   FROM etf_daily GROUP BY etf_code ORDER BY etf_code"""
            )
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "error": str(error)}
        return {
            "status": "ok",
            "coverage": [
                {
                    "etf_code": row["etf_code"],
                    "rows": row["n"],
                    "min_date": str(row["min_date"]),
                    "max_date": str(row["max_date"]),
                    "track_rows": row["track_n"],
                }
                for row in rows
            ],
        }
