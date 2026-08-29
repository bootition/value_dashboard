"""个股业务概览低频域更新服务（reports/67, reports/68 §6）

独立数据域：company_profile / business_breakdown 两张 DuckDB 表，
独立东财 F10 适配器实例与限速，失败保留旧值并记录独立 retry/missing，
绝不进入 stock_meta / indicator_snapshot / 筛选池 / readiness。

写入语义（reports/68 §5 架构与安全门槛）：
- 单股事务原子替换：一只股票的资料 + 构成在同一个 DuckDB 事务内完成。
- 任一侧网络错误 → 整股失败，保留旧值，写入 retry_list（去重）。
- 空/畸形响应（合法 missing）→ 保留旧值，写入 missing_list（去重/可解决）。
- 新数据到达时解决对应未解决 missing 条目。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
from app.core.adapters.manager import AdapterManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["BusinessOverviewUpdater"]

REFRESH_MARKER_KEY = "business_overview_last_refresh"
RETRY_DATA_TYPES = ("company_profile", "business_breakdown")


class BusinessOverviewUpdater:
    """业务概览低频更新执行器。

    构造必须经由注入的 stores 或 validated paths（与其余业务服务一致）；
    无数据库 profile 时 fail-closed（PathIsolationError）。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        adapter: EastMoneyF10Adapter | AdapterManager | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError(
                "BusinessOverviewUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("BusinessOverviewUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        # 默认经 AdapterManager 路由，以获得按 eastmoney_f10 隔离的限速与熔断；
        # 测试仍可注入最小 fake adapter。
        self.adapter = adapter or AdapterManager()

    # ─── 配置 ─────────────────────────────────────────────────────

    @staticmethod
    def _load_config(key: str, *, default: Any) -> Any:
        try:
            from app.core.config import Config
            cfg = Config.current()
            update_cfg = cfg.get("update", {})
            if isinstance(update_cfg, dict) and key in update_cfg:
                return update_cfg[key]
        except Exception:
            pass
        return default

    # ─── 单股原子更新 ─────────────────────────────────────────────

    def update_stock(self, stock_code: str) -> dict[str, Any]:
        """抓取并原子替换单股业务概览；失败保留旧值并记录 retry/missing。

        Returns:
            报告 dict：{"status": "success"|"failed", "stock_code", ...}
        """
        profile_result = self.adapter.fetch(FetchRequest(
            data_type="company_profile", stock_codes=[stock_code],
        ))
        breakdown_result = self.adapter.fetch(FetchRequest(
            data_type="business_breakdown", stock_codes=[stock_code],
        ))

        errors: list[str] = []
        for data_type, result in (
            ("company_profile", profile_result),
            ("business_breakdown", breakdown_result),
        ):
            if result.metadata.error:
                errors.append(f"{data_type}: {result.metadata.error}")
                self._record_retry(
                    stock_code, data_type, result.metadata.source, result.metadata.error,
                )
        if errors:
            return {
                "status": "failed",
                "stock_code": stock_code,
                "error": "; ".join(errors),
                "retained": True,
            }

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(UTC)
        # 单股事务原子替换：资料与构成同一事务提交，任一侧异常整体回滚
        with self.duck.transaction() as conn:
            if profile_result.data:
                self._replace_profile(conn, stock_code, profile_result, batch_id, fetch_time)
            if breakdown_result.data:
                self._replace_breakdown(conn, stock_code, breakdown_result, batch_id, fetch_time)

        # 提交后维护 missing 状态（解决已补上的，登记仍然缺失的）
        for data_type, result in (
            ("company_profile", profile_result),
            ("business_breakdown", breakdown_result),
        ):
            if result.data:
                self._resolve_missing(stock_code, data_type)
            else:
                self._record_missing(stock_code, data_type, "source_empty")
        # 成功后清理该股票已解决的重试条目（去重语义：retry 队列只保留失败）
        self._resolve_retry(stock_code)

        return {
            "status": "success",
            "stock_code": stock_code,
            "batch_id": batch_id,
            "profile_rows": len(profile_result.data),
            "breakdown_rows": len(breakdown_result.data),
        }

    def _replace_profile(
        self,
        conn: Any,
        stock_code: str,
        result: Any,
        batch_id: str,
        fetch_time: datetime,
    ) -> None:
        """在事务内删除旧资料并写入新资料。"""
        conn.execute("DELETE FROM company_profile WHERE stock_code = ?", [stock_code])
        row = result.data[0]
        conn.execute(
            """INSERT INTO company_profile
               (stock_code, code, name, org_name, profile, scope, employee_num,
                csrc_industry, trade_market, source, fetch_time, raw_hash, confidence, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                stock_code,
                row.get("code"),
                row.get("name"),
                row.get("org_name"),
                row.get("profile"),
                row.get("scope"),
                row.get("employee_num"),
                row.get("csrc_industry"),
                row.get("trade_market"),
                result.metadata.source,
                fetch_time,
                result.metadata.raw_response_hash,
                result.metadata.confidence,
                batch_id,
            ],
        )

    def _replace_breakdown(
        self,
        conn: Any,
        stock_code: str,
        result: Any,
        batch_id: str,
        fetch_time: datetime,
    ) -> None:
        """在事务内原子替换该股票来源返回报告期的主营构成。

        仅删除本次来源返回的报告期（通常是最近一期），保留更早的历史期，
        使 business_breakdown 随刷新自然累积"历史可得数据"（reports/68 §5）。
        """
        report_dates = sorted({row.get("report_date") for row in result.data})
        if report_dates:
            slots = ", ".join("?" for _ in report_dates)
            conn.execute(
                f"DELETE FROM business_breakdown WHERE stock_code = ? AND report_date IN ({slots})",
                [stock_code, *report_dates],
            )
        conn.executemany(
            """INSERT INTO business_breakdown
               (stock_code, report_date, type, item_name, amount, ratio, rank,
                source, fetch_time, raw_hash, confidence, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                [
                    stock_code,
                    row.get("report_date"),
                    row.get("type"),
                    row.get("item_name"),
                    row.get("amount"),
                    row.get("ratio"),
                    row.get("rank"),
                    result.metadata.source,
                    fetch_time,
                    result.metadata.raw_response_hash,
                    result.metadata.confidence,
                    batch_id,
                ]
                for row in result.data
            ],
        )

    # ─── 批量 / 全量 ──────────────────────────────────────────────

    def update_many(
        self,
        stock_codes: list[str],
        *,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """逐股更新给定股票，返回汇总报告。"""
        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for code in stock_codes:
            outcome = self.update_stock(code)
            results[code] = outcome
            if outcome["status"] != "success":
                failed.append(code)
            if progress_cb is not None:
                progress_cb(code, outcome)
        return {
            "status": "success" if not failed else "partial",
            "targeted": len(stock_codes),
            "succeeded": len(stock_codes) - len(failed),
            "failed": len(failed),
            "failed_codes": failed[:20],
            "results": results,
        }

    def _listed_stock_codes(self) -> list[str]:
        rows = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        return [row["stock_code"] for row in rows]

    def update_all(self, max_stocks: int = 0, *, mark_refreshed: bool = True) -> dict[str, Any]:
        """更新全部上市股票（研究优先名单优先，失败不阻断整体）。

        业务概览是低频域：通常随财报期后触发或按月检查（reports/67 §4.5）。
        """
        codes = self._listed_stock_codes()
        if not codes:
            return {"status": "skipped", "reason": "no_listed_stocks"}
        try:
            priority_codes = {
                row["stock_code"]
                for row in self.sqlite.query("SELECT DISTINCT stock_code FROM watchlist")
            }
        except Exception:
            priority_codes = set()
        if priority_codes:
            codes = sorted(codes, key=lambda code: (code not in priority_codes, code))
        if max_stocks > 0:
            codes = codes[:max_stocks]

        report = self.update_many(codes)
        if mark_refreshed and report["status"] in {"success", "partial"}:
            self._mark_refreshed()
        return report

    def refresh_if_due(
        self,
        max_stocks: int = 0,
        *,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """低频自动集成入口：仅当启用且刷新间隔到期时执行（最小安全）。

        - 默认启用，但每轮只处理有界数量，避免低频源拖慢启动更新。
        - 关闭 / 未到期时返回 skipped，不触发任何网络请求。
        """
        enabled = bool(self._load_config("business_overview_auto_enabled", default=True))
        if not enabled:
            return {"status": "skipped", "reason": "auto_disabled"}
        interval_days = int(self._load_config(
            "business_overview_refresh_interval_days", default=30,
        ))
        due_codes = self._due_stock_codes(interval_days)
        if not due_codes:
            return {
                "status": "skipped",
                "reason": "refreshed_within_interval",
                "interval_days": interval_days,
            }
        configured_max = int(self._load_config(
            "business_overview_max_stocks_per_run", default=20,
        ))
        limit = max_stocks or configured_max
        # 覆盖率低时进入追赶模式：每轮处理更多只，但仍为有界任务。
        catchup_threshold = int(self._load_config(
            "business_overview_catchup_threshold", default=4000,
        ))
        catchup_max = int(self._load_config(
            "business_overview_catchup_max_stocks_per_run", default=500,
        ))
        if len(due_codes) >= catchup_threshold and not max_stocks:
            limit = max(limit, catchup_max)
        targets = due_codes[:limit]
        if progress_cb is None:
            report = self.update_many(targets)
        else:
            completed = 0

            def cb(code: str, outcome: dict[str, Any]) -> None:
                nonlocal completed
                completed += 1
                progress_cb(code, {
                    **outcome,
                    "done": completed,
                    "total": len(targets),
                })

            report = self.update_many(targets, progress_cb=cb)
        if report["status"] in {"success", "partial"}:
            self._mark_refreshed()
        return report

    def _due_stock_codes(self, interval_days: int) -> list[str]:
        """Return missing or stale listed stocks, with watchlist stocks first.

        Selecting only due stocks lets the bounded startup task continue through
        the universe on later launches instead of repeatedly refreshing the same
        first page.
        """
        cutoff = datetime.now(UTC) - timedelta(days=interval_days)
        rows = self.duck.read_query(
            """SELECT m.stock_code,
                      LEAST(p.fetch_time, b.fetch_time) AS oldest_fetch
               FROM stock_meta m
               LEFT JOIN company_profile p ON p.stock_code = m.stock_code
               LEFT JOIN (
                   SELECT stock_code, MAX(fetch_time) AS fetch_time
                   FROM business_breakdown GROUP BY stock_code
               ) b ON b.stock_code = m.stock_code
               WHERE m.is_listed IS TRUE
                 AND (p.stock_code IS NULL OR b.stock_code IS NULL
                      OR p.fetch_time < ? OR b.fetch_time < ?)
               ORDER BY oldest_fetch NULLS FIRST, m.stock_code""",
            [cutoff, cutoff],
        )
        codes = [row["stock_code"] for row in rows]
        source_order = {code: index for index, code in enumerate(codes)}
        try:
            priority_codes = {
                row["stock_code"]
                for row in self.sqlite.query("SELECT DISTINCT stock_code FROM watchlist")
            }
        except Exception:
            priority_codes = set()
        return sorted(codes, key=lambda code: (code not in priority_codes, source_order[code]))

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

    def _record_retry(self, stock_code: str, data_type: str, adapter: str, error: str) -> None:
        """写 retry_list，利用 uq_retry_list_request 去重（ON CONFLICT 更新）。"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, '{}')
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [stock_code, data_type, adapter, error[:500],
                     datetime.now(UTC).isoformat()],
                )
        except Exception as e:
            logger.warning("记录业务概览失败信息失败: %s", e)

    def _record_missing(self, stock_code: str, field_name: str, reason_code: str) -> None:
        """写 missing_list（去重：每股票+字段仅一条未解决，uq_missing_list_stock_field_open）。"""
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
        except Exception as e:
            logger.warning("记录业务概览缺失信息失败: %s", e)

    def _resolve_missing(self, stock_code: str, field_name: str) -> None:
        """数据已到达时解决对应未解决 missing 条目。"""
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = ? AND resolved_at IS NULL""",
                [datetime.now(UTC).isoformat(), stock_code, field_name],
            )
        except Exception as e:
            logger.warning("解决业务概览缺失信息失败: %s", e)

    def _resolve_retry(self, stock_code: str) -> None:
        """数据已到达时清理该股票的待重试条目。"""
        try:
            self.sqlite.execute(
                """DELETE FROM retry_list
                   WHERE stock_code = ? AND data_type IN (?, ?) AND adapter = ?""",
                [stock_code, RETRY_DATA_TYPES[0], RETRY_DATA_TYPES[1], "eastmoney_f10"],
            )
        except Exception as e:
            logger.warning("清理业务概览重试条目失败: %s", e)

    # ─── 只读状态报告（CLI check-only） ───────────────────────────

    def status_report(self) -> dict[str, Any]:
        """返回业务概览域的覆盖与队列状态（只读，不抓取）。"""
        try:
            listed = self.duck.read_query(
                "SELECT COUNT(*) AS count FROM stock_meta WHERE is_listed IS TRUE"
            )[0]["count"]
            profiled = self.duck.read_query(
                "SELECT COUNT(DISTINCT stock_code) AS count FROM company_profile"
            )[0]["count"]
            breakdown = self.duck.read_query(
                "SELECT COUNT(DISTINCT stock_code) AS count FROM business_breakdown"
            )[0]["count"]
        except Exception as error:
            return {"status": "error", "error": str(error)}
        retry_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM retry_list WHERE data_type IN (?, ?)",
            list(RETRY_DATA_TYPES),
        )[0]["count"]
        missing_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM missing_list WHERE resolved_at IS NULL",
        )[0]["count"]
        last_refresh = None
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?", [REFRESH_MARKER_KEY]
        )
        if rows:
            last_refresh = rows[0].get("value")
        return {
            "status": "ok",
            "listed_stocks": listed,
            "profile_covered": profiled,
            "breakdown_covered": breakdown,
            "last_refresh": last_refresh,
            "retry_open": retry_open,
            "missing_open": missing_open,
        }
