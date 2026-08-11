"""财政部国债收益率曲线低频域更新服务（P3，reports/68 §4）

独立基准域：treasury_yield_curve 单表，独立财政部适配器实例与限速，
失败保留旧值并记录独立 retry/missing，绝不进入 A 股表、筛选池或 readiness。

写入语义（reports/68 §4 硬门槛）：
- 未来日期拒绝：任何晚于本地（UTC+8）今天的曲线点不得入库。
- 历史回填：单期限一次请求（czbQueryYz），单期限事务内原子替换。
- 日终增量：czbQueryXy 单日全曲线，upsert 合并。
- 对齐：取不晚于价格日的最近曲线点，最大陈旧 5 个自然日；超限视为不可用。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from app.core.adapters.base import FetchRequest
from app.core.adapters.czb_mof_adapter import KEY_TENORS, TreasuryMofAdapter
from app.core.adapters.manager import AdapterManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["TreasuryCurveUpdater", "KEY_TENORS"]

REFRESH_MARKER_KEY = "treasury_curve_last_refresh"
RETRY_DATA_TYPE = "treasury_yield_curve"
MAX_STALENESS_DAYS = 5  # reports/68 §4.3：最大陈旧 5 个自然日
HISTORY_START = "2006-01-01"  # 曲线自 2006-03-01 起（阶段0 实测）

_CN_TZ = timezone(timedelta(hours=8))


def _cn_today() -> date:
    return datetime.now(_CN_TZ).date()


class TreasuryCurveUpdater:
    """国债曲线低频更新执行器（注入 stores 或 validated paths，fail-closed）。"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        adapter: TreasuryMofAdapter | AdapterManager | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError(
                "TreasuryCurveUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("TreasuryCurveUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        # 默认经 AdapterManager 路由（独立限速与熔断）；测试可注入 fake
        self.adapter = adapter or AdapterManager()

    # ─── 历史回填（单期限原子替换） ────────────────────────────────

    def backfill(
        self,
        tenors: list[float] | None = None,
        *,
        max_tenors: int = 0,
        progress_cb: Callable[[float, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """对每个期限拉全历史并原子替换该期限数据。

        未来日期在适配器层拒绝；失败保留旧值并写 retry；空结果写 missing。
        """
        targets = [t for t in (tenors or list(KEY_TENORS)) if t in KEY_TENORS]
        # P3-8 修复（reports/73）：用户显式传了期限但全部非法 → 明确失败，
        # 不再静默空转返回 success。
        if tenors and not targets:
            return {
                "status": "failed",
                "reason": "no_valid_tenors",
                "requested": [float(t) for t in tenors],
                "supported": list(KEY_TENORS),
                "targeted": 0,
            }
        if max_tenors > 0:
            targets = targets[:max_tenors]

        results: dict[float, dict[str, Any]] = {}
        failed: list[float] = []
        for tenor in targets:
            outcome = self._backfill_one(tenor)
            results[tenor] = outcome
            if outcome["status"] != "success":
                failed.append(tenor)
            if progress_cb is not None:
                progress_cb(tenor, outcome)

        return {
            "status": "success" if not failed else ("failed" if not results or len(failed) == len(targets) else "partial"),
            "targeted": len(targets),
            "succeeded": len(targets) - len(failed),
            "failed": failed,
            "results": results,
        }

    def _backfill_one(self, tenor: float) -> dict[str, Any]:
        result = self.adapter.fetch(FetchRequest(
            data_type="treasury_yield_curve",
            extra_params={
                "mode": "history", "tenor": tenor,
                "start": HISTORY_START, "end": str(_cn_today()),
            },
        ))
        if result.metadata.error:
            self._record_retry(tenor, result.metadata.error)
            return {
                "status": "failed", "tenor": tenor,
                "error": result.metadata.error, "retained": True,
            }

        if not result.data:
            self._record_missing(tenor, "source_empty")
            return {
                "status": "failed", "tenor": tenor,
                "reason": "source_empty", "retained": True,
            }

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(timezone.utc)
        curve_dates = sorted({row["curve_date"] for row in result.data})
        with self.duck.transaction() as conn:
            conn.execute(
                "DELETE FROM treasury_yield_curve WHERE tenor_years = ?",
                [tenor],
            )
            conn.executemany(
                """INSERT INTO treasury_yield_curve
                   (curve_date, tenor_years, yield_pct, source, fetch_time,
                    raw_hash, confidence, batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    [
                        row["curve_date"], row["tenor_years"], row["yield_pct"],
                        result.metadata.source, fetch_time,
                        result.metadata.raw_response_hash,
                        result.metadata.confidence, batch_id,
                    ]
                    for row in result.data
                ],
            )

        self._resolve_missing(tenor)
        self._resolve_retry(tenor)
        return {
            "status": "success", "tenor": tenor, "batch_id": batch_id,
            "rows": len(result.data),
            "date_range": [curve_dates[0], curve_dates[-1]],
        }

    # ─── 日终增量 ─────────────────────────────────────────────────

    def update_daily(
        self,
        work_dates: list[str] | None = None,
        *,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """按日抓取当日全曲线并 upsert。

        非工作日/未发布 → 保留旧值并登记 missing；未来日期拒绝；
        已存在且更旧的数据不会被回退覆盖（仅当新数据到达时更新）。
        """
        targets = work_dates or [str(_cn_today())]
        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for work_time in targets:
            try:
                work_date = date.fromisoformat(str(work_time)[:10])
            except ValueError:
                failed.append(work_time)
                results[work_time] = {"status": "failed", "reason": "invalid_date"}
                continue
            if work_date > _cn_today():
                failed.append(work_time)
                results[work_time] = {"status": "failed", "reason": "future_date"}
                continue
            outcome = self._update_daily_one(work_date)
            results[work_time] = outcome
            if outcome["status"] != "success":
                failed.append(work_time)
            if progress_cb is not None:
                progress_cb(work_time, outcome)

        return {
            "status": "success" if not failed else ("failed" if not results or len(failed) == len(targets) else "partial"),
            "targeted": len(targets),
            "succeeded": len(targets) - len(failed),
            "failed": failed,
            "results": results,
        }

    def _update_daily_one(self, work_date: date) -> dict[str, Any]:
        result = self.adapter.fetch(FetchRequest(
            data_type="treasury_yield_curve",
            extra_params={"mode": "daily", "work_time": str(work_date)},
        ))
        if result.metadata.error:
            self._record_retry(None, result.metadata.error, work_date=str(work_date))
            return {"status": "failed", "work_date": str(work_date),
                    "error": result.metadata.error, "retained": True}

        if not result.data:
            self._record_missing(None, "source_empty", work_date=str(work_date))
            return {"status": "failed", "work_date": str(work_date),
                    "reason": "source_empty", "retained": True}

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(timezone.utc)
        with self.duck.transaction() as conn:
            conn.executemany(
                """INSERT INTO treasury_yield_curve
                   (curve_date, tenor_years, yield_pct, source, fetch_time,
                    raw_hash, confidence, batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (curve_date, tenor_years) DO UPDATE SET
                     yield_pct = excluded.yield_pct,
                     source = excluded.source,
                     fetch_time = excluded.fetch_time,
                     raw_hash = excluded.raw_hash,
                     confidence = excluded.confidence,
                     batch_id = excluded.batch_id""",
                [
                    [
                        row["curve_date"], row["tenor_years"], row["yield_pct"],
                        result.metadata.source, fetch_time,
                        result.metadata.raw_response_hash,
                        result.metadata.confidence, batch_id,
                    ]
                    for row in result.data
                ],
            )

        self._resolve_missing(None, work_date=str(work_date))
        self._resolve_retry(None, work_date=str(work_date))
        return {"status": "success", "work_date": str(work_date),
                "batch_id": batch_id, "rows": len(result.data)}

    # ─── 低频自动刷新 ─────────────────────────────────────────────

    def refresh_if_due(
        self,
        *,
        max_tenors_backfill: int = 0,
    ) -> dict[str, Any]:
        """低频自动集成：每日最多一次（P3-4 修复，reports/73）。

        标记 key 记录上次刷新时间；当日（UTC+8）已刷新则直接 skip，
        不发起任何网络请求。未到期/未刷新时：更新当日曲线，并对近 30 天
        无数据的关键期限执行历史回填。任何失败均不抛异常。
        """
        if self._refreshed_today():
            return {
                "status": "skipped", "reason": "refreshed_today",
                "last_refresh": self._last_refresh_value(),
            }
        try:
            daily = self.update_daily()
            stale_tenors = self._tenors_missing_recently(days=30)
            backfill_report: dict[str, Any] = {"status": "skipped", "reason": "no_stale_tenors"}
            if stale_tenors:
                backfill_report = self.backfill(
                    stale_tenors, max_tenors=max_tenors_backfill,
                )
            self._mark_refreshed()
            return {
                "status": "success"
                if daily["status"] != "failed" and backfill_report["status"] != "failed"
                else "partial",
                "daily": daily,
                "backfill": backfill_report,
            }
        except Exception as error:
            logger.warning("国债曲线刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

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

    def _tenors_missing_recently(self, days: int = 30) -> list[float]:
        cutoff = str(_cn_today() - timedelta(days=days))
        rows = self.duck.read_query(
            """SELECT tenor_years, MAX(curve_date) AS latest
               FROM treasury_yield_curve
               GROUP BY tenor_years"""
        )
        present = {
            float(row["tenor_years"]): str(row["latest"])
            for row in rows if row.get("latest") is not None
        }
        return [
            tenor for tenor in KEY_TENORS
            if present.get(tenor, "1970-01-01") < cutoff
        ]

    def _mark_refreshed(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [REFRESH_MARKER_KEY, now, now],
            )

    # ─── 对齐与查询（只读，供研究与快照计算） ─────────────────────

    def align(
        self,
        price_date: str | date,
        tenor: float = 10.0,
    ) -> dict[str, Any]:
        """取不晚于 price_date 的最近曲线点；>5 自然日陈旧视为不可用。"""
        price_date = str(price_date)[:10]
        rows = self.duck.read_query(
            """SELECT curve_date, yield_pct
               FROM treasury_yield_curve
               WHERE tenor_years = ? AND curve_date <= ?
               ORDER BY curve_date DESC LIMIT 1""",
            [tenor, price_date],
        )
        if not rows:
            return {
                "status": "missing", "curve_date": None, "yield_pct": None,
                "staleness_days": None, "reason": "curve_missing",
            }
        curve_date = str(rows[0]["curve_date"])[:10]
        staleness = (date.fromisoformat(price_date) - date.fromisoformat(curve_date)).days
        if staleness > MAX_STALENESS_DAYS:
            return {
                "status": "stale", "curve_date": curve_date,
                "yield_pct": None, "staleness_days": staleness,
                "reason": "curve_stale",
            }
        return {
            "status": "ok", "curve_date": curve_date,
            "yield_pct": float(rows[0]["yield_pct"]),
            "staleness_days": staleness, "reason": None,
        }

    def align_many(
        self,
        price_dates: list[str | date],
        tenors: list[float],
    ) -> dict[tuple[str, float], dict[str, Any]]:
        """批量对齐（P3-9 修复，reports/73）：单次 SQL 查询全部
        (价格日 × 期限) 组合的最近曲线点，消除逐项 N+1 查询。"""
        results: dict[tuple[str, float], dict[str, Any]] = {}
        unique_dates = sorted({str(price_date)[:10] for price_date in price_dates})
        if not unique_dates or not tenors:
            return results
        rows = self.duck.read_query(
            """WITH days AS (
                   SELECT UNNEST(?) AS price_date
               ), combos AS (
                   SELECT d.price_date, t.tenor
                   FROM days d, (SELECT UNNEST(?) AS tenor) t
               )
               SELECT c.price_date, c.tenor, a.curve_date, a.yield_pct
               FROM combos c
               LEFT JOIN LATERAL (
                   SELECT curve_date, yield_pct
                   FROM treasury_yield_curve
                   WHERE tenor_years = c.tenor
                     AND curve_date <= CAST(c.price_date AS DATE)
                   ORDER BY curve_date DESC LIMIT 1
               ) a ON TRUE""",
            [unique_dates, list(tenors)],
        )
        for row in rows:
            key = (str(row["price_date"])[:10], float(row["tenor"]))
            curve_date = row.get("curve_date")
            if curve_date is None or row.get("yield_pct") is None:
                results[key] = {
                    "status": "missing", "curve_date": None, "yield_pct": None,
                    "staleness_days": None, "reason": "curve_missing",
                }
                continue
            curve_date = str(curve_date)[:10]
            staleness = (
                date.fromisoformat(key[0]) - date.fromisoformat(curve_date)
            ).days
            if staleness > MAX_STALENESS_DAYS:
                results[key] = {
                    "status": "stale", "curve_date": curve_date,
                    "yield_pct": None, "staleness_days": staleness,
                    "reason": "curve_stale",
                }
                continue
            results[key] = {
                "status": "ok", "curve_date": curve_date,
                "yield_pct": float(row["yield_pct"]),
                "staleness_days": staleness, "reason": None,
            }
        return results

    def status_report(self) -> dict[str, Any]:
        """只读覆盖与队列状态（不抓取）。"""
        try:
            curve_dates = self.duck.read_query(
                "SELECT MIN(curve_date) AS earliest, MAX(curve_date) AS latest "
                "FROM treasury_yield_curve"
            )[0]
            tenors = self.duck.read_query(
                "SELECT COUNT(DISTINCT tenor_years) AS count FROM treasury_yield_curve"
            )[0]["count"]
        except Exception as error:
            return {"status": "error", "error": str(error)}
        retry_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM retry_list WHERE data_type = ?",
            [RETRY_DATA_TYPE],
        )[0]["count"]
        # P3-7 修复（reports/73）：missing 统计按国债域过滤（stock_code=
        # '__market__' 且 field_name 以 treasury_curve 开头），与 retry_open
        # 口径一致，不再混入全库股票缺失数。
        missing_open = self.sqlite.query(
            """SELECT COUNT(*) AS count FROM missing_list
               WHERE resolved_at IS NULL
                 AND stock_code = ?
                 AND field_name LIKE ?""",
            ["__market__", "treasury_curve%"],
        )[0]["count"]
        last_refresh = None
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?", [REFRESH_MARKER_KEY]
        )
        if rows:
            last_refresh = rows[0].get("value")
        return {
            "status": "ok",
            "earliest_curve_date": str(curve_dates.get("earliest")) if curve_dates.get("earliest") else None,
            "latest_curve_date": str(curve_dates.get("latest")) if curve_dates.get("latest") else None,
            "tenor_count": tenors,
            "last_refresh": last_refresh,
            "retry_open": retry_open,
            "missing_open": missing_open,
        }

    # ─── retry / missing 维护 ─────────────────────────────────────

    def _record_retry(
        self, tenor: float | None, error: str, *, work_date: str | None = None,
    ) -> None:
        extra = {"mode": "history", "tenor": tenor} if tenor is not None else \
            {"mode": "daily", "work_date": work_date}
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, ?)
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    ["__market__", RETRY_DATA_TYPE, "czb_mof", error[:500],
                     datetime.now(timezone.utc).isoformat(),
                     __import__("json").dumps(extra, ensure_ascii=False)],
                )
        except Exception as e:
            logger.warning("记录国债曲线失败信息失败: %s", e)

    def _record_missing(
        self, tenor: float | None, reason_code: str, *, work_date: str | None = None,
    ) -> None:
        field_name = f"treasury_curve_{tenor}" if tenor is not None else f"treasury_curve_daily_{work_date}"
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    ["__market__", field_name, reason_code],
                )
        except Exception as e:
            logger.warning("记录国债曲线缺失信息失败: %s", e)

    def _resolve_missing(
        self, tenor: float | None, *, work_date: str | None = None,
    ) -> None:
        field_name = f"treasury_curve_{tenor}" if tenor is not None else f"treasury_curve_daily_{work_date}"
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = ? AND resolved_at IS NULL""",
                [datetime.now(timezone.utc).isoformat(), "__market__", field_name],
            )
        except Exception as e:
            logger.warning("解决国债曲线缺失信息失败: %s", e)

    def _resolve_retry(self, tenor: float | None, *, work_date: str | None = None) -> None:
        try:
            self.sqlite.execute(
                """DELETE FROM retry_list
                   WHERE stock_code = ? AND data_type = ? AND adapter = ?""",
                ["__market__", RETRY_DATA_TYPE, "czb_mof"],
            )
        except Exception as e:
            logger.warning("清理国债曲线重试条目失败: %s", e)
