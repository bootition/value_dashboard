"""指数估值低频域更新服务（数据补全 2026-08-25，沪深300 ERP 数据前置）

独立数据域：index_valuation 单张 DuckDB 表（PK: index_code+trade_date+source）。
主源乐咕（全历史 PE-TTM），交叉源中证官网（近 20 交易日）——双源并存，
ERP 计算时主源优先、同日期交叉核验披露。

写入语义：
- update_daily：主源全历史 upsert + 交叉源近 20 日 upsert，单事务提交。
- 每日 1~2 次请求（各源单次调用返回全部可得数据），无风控风险。
- 主源失败时保留旧值并记录 retry；交叉源失败不阻断主源落库。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.index_valuation_adapter import CSIndexIndexAdapter, LeguleguIndexAdapter
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["IndexValuationUpdater", "DEFAULT_INDEX_CODES"]

DEFAULT_INDEX_CODES = ["000300"]

REFRESH_MARKER_KEY = "index_valuation_last_refresh"

_CN_TZ = timezone(timedelta(hours=8))


def _cn_today() -> date:
    """本地（UTC+8）今天。"""
    return datetime.now(_CN_TZ).date()


class IndexValuationUpdater:
    """指数估值低频更新执行器（乐咕主源 + 中证官网交叉核验）。"""

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
            raise PathIsolationError(
                "IndexValuationUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("IndexValuationUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        # 两个独立适配器实例（各自限速），不经 AdapterManager 以便双源并存
        self._primary = LeguleguIndexAdapter(rate_limit=1.0)
        self._cross = CSIndexIndexAdapter(rate_limit=1.0)

    # ─── 日终更新 ─────────────────────────────────────────────────

    def update_daily(self, index_codes: list[str] | None = None) -> dict[str, Any]:
        """抓取主源（全历史）+ 交叉源（近 20 日）并原子 upsert。

        - 主源失败 → 保留旧值，登记 retry，整次返回 failed。
        - 交叉源失败 → 不阻断主源落库，结果中披露 cross_error。
        """
        codes = index_codes or DEFAULT_INDEX_CODES
        report: dict[str, Any] = {"status": "success", "indexes": {}}
        for index_code in codes:
            primary_result = self._primary.fetch(FetchRequest(
                data_type="index_valuation", stock_codes=[index_code],
            ))
            index_report: dict[str, Any] = {"primary_rows": 0, "cross_rows": 0}
            if primary_result.metadata.error:
                index_report["primary_error"] = primary_result.metadata.error
                self._record_retry(index_code, primary_result.metadata.error)
                report["status"] = "failed"
                report["indexes"][index_code] = index_report
                continue

            batch_id = uuid.uuid4().hex
            fetch_time = datetime.now(UTC)
            with self.duck.transaction() as conn:
                if primary_result.data:
                    conn.executemany(
                        """INSERT INTO index_valuation
                           (index_code, trade_date, pe_ttm, pb, div_yield, source,
                            fetch_time, raw_hash, confidence, batch_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(index_code, trade_date, source) DO UPDATE SET
                             pe_ttm=excluded.pe_ttm, pb=excluded.pb, div_yield=excluded.div_yield,
                             fetch_time=excluded.fetch_time, raw_hash=excluded.raw_hash,
                             confidence=excluded.confidence, batch_id=excluded.batch_id""",
                        [
                            [
                                index_code,
                                row.get("trade_date"),
                                row.get("pe_ttm"),
                                row.get("pb"),
                                row.get("div_yield"),
                                primary_result.metadata.source,
                                fetch_time,
                                primary_result.metadata.raw_response_hash,
                                primary_result.metadata.confidence,
                                batch_id,
                            ]
                            for row in primary_result.data
                        ],
                    )
                    index_report["primary_rows"] = len(primary_result.data)
                else:
                    self._record_missing(index_code, "source_empty")

            # 交叉源：失败不阻断主源
            try:
                cross_result = self._cross.fetch(FetchRequest(
                    data_type="index_valuation", stock_codes=[index_code],
                ))
                if cross_result.metadata.error:
                    index_report["cross_error"] = cross_result.metadata.error
                elif cross_result.data:
                    with self.duck.transaction() as conn:
                        conn.executemany(
                            """INSERT INTO index_valuation
                               (index_code, trade_date, pe_ttm, pb, div_yield, source,
                                fetch_time, raw_hash, confidence, batch_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(index_code, trade_date, source) DO UPDATE SET
                                 pe_ttm=excluded.pe_ttm, pb=excluded.pb, div_yield=excluded.div_yield,
                                 fetch_time=excluded.fetch_time, raw_hash=excluded.raw_hash,
                                 confidence=excluded.confidence, batch_id=excluded.batch_id""",
                            [
                                [
                                    index_code,
                                    row.get("trade_date"),
                                    row.get("pe_ttm"),
                                    row.get("pb"),
                                    row.get("div_yield"),
                                    cross_result.metadata.source,
                                    fetch_time,
                                    cross_result.metadata.raw_response_hash,
                                    cross_result.metadata.confidence,
                                    batch_id,
                                ]
                                for row in cross_result.data
                            ],
                        )
                    index_report["cross_rows"] = len(cross_result.data)
            except Exception as error:  # noqa: BLE001
                index_report["cross_error"] = f"{type(error).__name__}: {error}"

            report["indexes"][index_code] = index_report
        return report

    def refresh_if_due(self) -> dict[str, Any]:
        """低频自动集成入口：每日最多一次（UTC+8）。

        当日已刷新则直接 skip，不发起任何网络请求；未刷新时执行 update_daily
        并记录 marker。任何失败均不抛异常（由调用方兜底）。
        """
        if self._refreshed_today():
            return {"status": "skipped", "reason": "refreshed_today"}
        report = self.update_daily()
        if report["status"] == "success":
            self._mark_refreshed()
        return report

    def _refreshed_today(self) -> bool:
        value = self._last_refresh_value()
        if not value:
            return False
        try:
            refreshed = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return False
        return refreshed.astimezone(_CN_TZ).date() >= _cn_today()

    def _last_refresh_value(self) -> str | None:
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?", [REFRESH_MARKER_KEY]
        )
        return rows[0].get("value") if rows else None

    def _mark_refreshed(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [REFRESH_MARKER_KEY, now, now],
            )

    # ─── retry / missing 维护 ─────────────────────────────────────

    def _record_retry(self, index_code: str, error: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, '{}')
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [index_code, "index_valuation", "legulegu", error[:500],
                     datetime.now(UTC).isoformat()],
                )
        except Exception as e:
            logger.warning("记录指数估值失败信息失败: %s", e)

    def _record_missing(self, index_code: str, reason_code: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [index_code, "index_valuation", reason_code],
                )
        except Exception as e:
            logger.warning("记录指数估值缺失信息失败: %s", e)

    # ─── 只读状态报告 ─────────────────────────────────────────────

    def status_report(self) -> dict[str, Any]:
        """返回指数估值域的覆盖与队列状态（只读，不抓取）。"""
        try:
            rows = self.duck.read_query(
                """SELECT source, index_code, COUNT(*) AS n,
                          MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
                   FROM index_valuation
                   GROUP BY source, index_code
                   ORDER BY source, index_code"""
            )
        except Exception as error:
            return {"status": "error", "error": str(error)}
        coverage = [
            {
                "source": row["source"],
                "index_code": row["index_code"],
                "rows": row["n"],
                "min_date": str(row["min_date"]),
                "max_date": str(row["max_date"]),
            }
            for row in rows
        ]
        retry_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM retry_list WHERE data_type = 'index_valuation'",
        )[0]["count"]
        return {
            "status": "ok",
            "coverage": coverage,
            "retry_open": retry_open,
        }
