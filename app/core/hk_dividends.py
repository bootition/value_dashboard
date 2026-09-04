"""港股分红低频域更新服务（2026-09-04，总市场分红融资比数据前置）

独立数据域：`hk_dividends` 单张 DuckDB 表，覆盖 A+H 两地上市公司的
港股历史现金分红。A 股代码 → 港股代码映射来自
`app/core/ah_hk_mapping.py`（名称精确匹配 + 人工覆写快照）。

写入语义（沿用 funding/business 域纪律）：
- 单股事务原子替换：同一港股代码的分红记录在一个 DuckDB 事务内
  DELETE → INSERT；
- 网络错误 → 整股失败，保留旧值，写入 retry_list（去重）；
- 合法空（源无分红记录）→ 保留旧值，写入 missing_list；
- 港股 IPO/配股/供股融资不在本域，缺失绝不伪造。

域边界：
- 不写 stock_meta / indicator_snapshot / source_audit / readiness；
- 不触发 A 股指标快照重算；现有 A 股 readiness 语义完全不变。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.ah_hk_mapping import build_ah_hk_mapping
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["HKDividendUpdater"]

RETRY_DATA_TYPE = "hk_dividends"
MISSING_FIELD = "hk_dividends"
MISSING_MAPPING_FIELD = "hk_dividends_mapping"

_HK_DIVIDEND_COLUMNS = (
    "stock_code",
    "ex_date",
    "announcement_date",
    "report_period",
    "plan_explain",
    "dividend_per_share_hkd",
    "dividend_per_share_cny",
    "transfer_end_date",
    "dividend_date",
    "source",
    "fetch_time",
    "raw_response_hash",
    "confidence",
    "batch_id",
)


class HKDividendUpdater:
    """港股分红低频更新执行器（A+H 双地上市股票）。"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        adapter: AdapterManager | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError(
                "HKDividendUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("HKDividendUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        # 默认经 AdapterManager 路由（eastmoney_hk_dividend 0.5s/请求）；
        # 测试注入最小 fake adapter。
        self.adapter = adapter or AdapterManager()

    # ─── 映射 ─────────────────────────────────────────────────────

    def resolve_mapping(
        self,
        *,
        ah_spot_rows: list[dict[str, Any]] | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Build/refresh A→HK mapping and return its report."""
        report = build_ah_hk_mapping(
            self.duck,
            ah_spot_rows=ah_spot_rows,
            refresh=refresh,
        )
        if report.get("warning"):
            logger.warning(report["warning"])
        return report

    # ─── 单股原子更新 ─────────────────────────────────────────────

    def update_stock(
        self,
        stock_code: str,
        *,
        mapping: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Fetch HK dividends for one A-share code and atomically replace rows.

        `mapping` is optional for tests/callers that already resolved it;
        otherwise this method resolves the full snapshot mapping once.
        """
        if mapping is None:
            mapping = self.resolve_mapping()["mapping"]
        mapped = mapping.get(stock_code)
        if mapped is None:
            self._record_missing(stock_code, MISSING_MAPPING_FIELD, "no_ah_hk_mapping")
            return {
                "status": "unmapped",
                "stock_code": stock_code,
                "error": "no_ah_hk_mapping",
                "retained": True,
            }

        hk_code = str(mapped["hk_code"]).zfill(5)
        result = self.adapter.fetch(FetchRequest(
            data_type="hk_dividends", stock_codes=[hk_code],
        ))
        if result.metadata.error:
            self._record_retry(
                stock_code, result.metadata.source, result.metadata.error,
            )
            return {
                "status": "failed",
                "stock_code": stock_code,
                "hk_code": hk_code,
                "error": result.metadata.error,
                "retained": True,
            }

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(UTC)
        rows = self._dedupe_rows(result.data, hk_code)
        if rows:
            with self.duck.transaction() as conn:
                conn.execute(
                    "DELETE FROM hk_dividends WHERE stock_code = ?", [hk_code],
                )
                conn.executemany(
                    f"""INSERT INTO hk_dividends
                        ({', '.join(_HK_DIVIDEND_COLUMNS)})
                       VALUES ({', '.join('?' for _ in _HK_DIVIDEND_COLUMNS)})""",
                    [
                        [
                            row["stock_code"],
                            row.get("ex_date"),
                            row.get("announcement_date"),
                            row.get("report_period"),
                            row.get("plan_explain"),
                            row.get("dividend_per_share_hkd"),
                            row.get("dividend_per_share_cny"),
                            row.get("transfer_end_date"),
                            row.get("dividend_date"),
                            result.metadata.source,
                            fetch_time,
                            result.metadata.raw_response_hash,
                            result.metadata.confidence,
                            batch_id,
                        ]
                        for row in rows
                    ],
                )
            self._resolve_missing(stock_code, MISSING_FIELD)
            self._resolve_retry(stock_code)
        else:
            # 合法空响应：源当前无该股分红记录。保留既有数据，
            # 只登记 missing，避免瞬时接口空窗清空历史。
            self._record_missing(stock_code, MISSING_FIELD, "source_empty")

        return {
            "status": "success",
            "stock_code": stock_code,
            "hk_code": hk_code,
            "batch_id": batch_id,
            "event_rows": len(rows),
            "retained": not rows,
        }

    @staticmethod
    def _dedupe_rows(
        rows: list[dict[str, Any]],
        hk_code: str,
    ) -> list[dict[str, Any]]:
        """Deduplicate adapter rows by the table primary key."""
        seen: set[tuple[Any, ...]] = set()
        deduped: list[dict[str, Any]] = []
        for row in rows:
            key = (
                hk_code,
                row.get("ex_date"),
                row.get("plan_explain") or "",
            )
            if key in seen:
                continue
            seen.add(key)
            normalized = dict(row)
            normalized["stock_code"] = hk_code
            normalized["plan_explain"] = normalized.get("plan_explain") or ""
            deduped.append(normalized)
        return deduped

    # ─── 批量 / 全量 ──────────────────────────────────────────────

    def update_many(
        self,
        stock_codes: list[str],
        *,
        batch_size: int = 50,
        batch_cooldown_seconds: float = 0.0,
        refresh_mapping: bool = False,
        ah_spot_rows: list[dict[str, Any]] | None = None,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Update given A-share codes; adapter rate limit keeps source ≤2 req/s."""
        if not stock_codes:
            return {
                "status": "success", "targeted": 0, "succeeded": 0,
                "unmapped": 0, "failed": 0, "failed_codes": [],
                "mapping_summary": None, "results": {},
            }

        mapping_report = self.resolve_mapping(
            ah_spot_rows=ah_spot_rows,
            refresh=refresh_mapping,
        )
        mapping = mapping_report["mapping"]

        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        unmapped: list[str] = []
        for index, code in enumerate(stock_codes):
            outcome = self.update_stock(code, mapping=mapping)
            results[code] = outcome
            if outcome["status"] == "failed":
                failed.append(code)
            elif outcome["status"] == "unmapped":
                unmapped.append(code)
            if progress_cb is not None:
                progress_cb(code, outcome)
            if batch_size > 0 and (index + 1) % batch_size == 0 \
                    and index + 1 < len(stock_codes):
                logger.info(
                    "港股分红批进度 %d/%d", index + 1, len(stock_codes),
                )
                if batch_cooldown_seconds > 0:
                    time.sleep(batch_cooldown_seconds)

        succeeded = len(stock_codes) - len(failed) - len(unmapped)
        return {
            "status": "success" if not failed and not unmapped else "partial",
            "targeted": len(stock_codes),
            "succeeded": succeeded,
            "failed": len(failed),
            "failed_codes": failed[:20],
            "unmapped": len(unmapped),
            "unmapped_codes": unmapped[:20],
            "mapping_summary": {
                "mapped_stocks": mapping_report["mapped_stocks"],
                "exact_matches": mapping_report["exact_matches"],
                "manual_overrides_used": mapping_report["manual_overrides_used"],
                "source": mapping_report["source"],
                "warning": mapping_report.get("warning"),
            },
            "results": results,
        }

    def _listed_stock_codes(self) -> list[str]:
        rows = self.duck.read_query(
            "SELECT stock_code FROM stock_meta "
            "WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        return [row["stock_code"] for row in rows]

    def update_all(
        self,
        max_stocks: int = 0,
        *,
        refresh_mapping: bool = True,
        mark_refreshed: bool = True,
    ) -> dict[str, Any]:
        """Update all mapped A+H stocks, skipping stocks already covered.

        `max_stocks` is applied to the uncovered subset so bounded
        continuation can advance across runs (funding 2026-08-25 lesson).
        """
        mapping_report = self.resolve_mapping(refresh=refresh_mapping)
        mapping = mapping_report["mapping"]
        if not mapping:
            return {
                "status": "skipped", "reason": "no_ah_hk_mapping",
                "mapping_summary": mapping_report,
            }

        covered = {
            row["stock_code"]
            for row in self.duck.read_query(
                "SELECT DISTINCT stock_code FROM hk_dividends"
            )
        }
        pending = [
            code for code, mapped in mapping.items()
            if mapped["hk_code"] not in covered
        ]
        if not pending:
            return {
                "status": "skipped", "reason": "all_hk_dividends_covered",
                "targeted": 0, "mapping_summary": mapping_report,
            }
        if max_stocks > 0:
            pending = pending[:max_stocks]

        report = self.update_many(
            pending,
            refresh_mapping=False,
            ah_spot_rows=None,
        )
        report["mapping_summary"] = mapping_report
        if mark_refreshed and report["status"] in {"success", "partial"}:
            self._mark_refreshed()
        return report

    def refresh_if_due(self, max_stocks: int = 0) -> dict[str, Any]:
        """Bounded auto-integration entry point (not wired into A-share readiness)."""
        return self.update_all(max_stocks=max_stocks, mark_refreshed=False)

    def _mark_refreshed(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                ["hk_dividends_last_refresh", now, now],
            )

    # ─── retry / missing 维护 ─────────────────────────────────────

    def _record_retry(self, stock_code: str, adapter: str, error: str) -> None:
        """Write retry_list with the project-wide unique-request constraint."""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count,
                        last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, '{}')
                       ON CONFLICT(stock_code, data_type, adapter, extra_json)
                       DO UPDATE SET
                         error=excluded.error,
                         last_attempt=excluded.last_attempt""",
                    [stock_code, RETRY_DATA_TYPE, adapter, error[:500],
                     datetime.now(UTC).isoformat()],
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("记录港股分红 retry 失败: %s", error)

    def _record_missing(self, stock_code: str, field_name: str, reason_code: str) -> None:
        """Write missing_list (one open row per stock+field)."""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [stock_code, field_name, reason_code],
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("记录港股分红 missing 失败: %s", error)

    def _resolve_missing(self, stock_code: str, field_name: str) -> None:
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = ? AND resolved_at IS NULL""",
                [datetime.now(UTC).isoformat(), stock_code, field_name],
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("解决港股分红 missing 失败: %s", error)

    def _resolve_retry(self, stock_code: str) -> None:
        try:
            self.sqlite.execute(
                """DELETE FROM retry_list
                   WHERE stock_code = ? AND data_type = ?""",
                [stock_code, RETRY_DATA_TYPE],
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("清理港股分红 retry 失败: %s", error)

    # ─── 只读状态报告（CLI check-only） ───────────────────────────

    def status_report(self) -> dict[str, Any]:
        """Return HK dividend coverage and queues without fetching anything."""
        mapping_report = self.resolve_mapping()
        try:
            rows_by_hk = {
                row["stock_code"]: row["n"]
                for row in self.duck.read_query(
                    """SELECT stock_code, COUNT(*) AS n
                       FROM hk_dividends GROUP BY stock_code"""
                )
            }
            totals = self.duck.read_query(
                "SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stocks "
                "FROM hk_dividends"
            )[0]
        except Exception as error:  # noqa: BLE001
            return {
                "status": "error",
                "error": str(error),
                "mapping_summary": mapping_report,
            }

        covered_stocks = [
            {
                "stock_code": a_code,
                "hk_code": mapped["hk_code"],
                "a_name": mapped["a_name"],
                "hk_name": mapped["hk_name"],
                "rows": rows_by_hk.get(mapped["hk_code"], 0),
            }
            for a_code, mapped in sorted(mapping_report["mapping"].items())
            if mapped["hk_code"] in rows_by_hk
        ]
        uncovered_stocks = [
            {
                "stock_code": a_code,
                "hk_code": mapped["hk_code"],
                "a_name": mapped["a_name"],
                "hk_name": mapped["hk_name"],
            }
            for a_code, mapped in sorted(mapping_report["mapping"].items())
            if mapped["hk_code"] not in rows_by_hk
        ]
        try:
            retry_open = self.sqlite.query(
                "SELECT COUNT(*) AS count FROM retry_list WHERE data_type = ?",
                [RETRY_DATA_TYPE],
            )[0]["count"]
            missing_open = self.sqlite.query(
                """SELECT COUNT(*) AS count FROM missing_list
                   WHERE field_name IN (?, ?) AND resolved_at IS NULL""",
                [MISSING_FIELD, MISSING_MAPPING_FIELD],
            )[0]["count"]
        except Exception as error:  # noqa: BLE001
            retry_open, missing_open = -1, -1
            logger.warning("读取港股分红队列状态失败: %s", error)

        return {
            "status": "ok",
            "rows": totals["rows"],
            "hk_stocks_with_rows": totals["stocks"],
            "mapped_stocks": mapping_report["mapped_stocks"],
            "covered_stocks": len(covered_stocks),
            "uncovered_mapped_stocks": len(uncovered_stocks),
            "covered_stock_codes": covered_stocks,
            "uncovered_mapped_stock_codes": uncovered_stocks,
            "retry_open": retry_open,
            "missing_open": missing_open,
            "mapping_summary": mapping_report,
        }
