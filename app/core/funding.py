"""融资事件低频域更新服务（数据补全 2026-08-25，分红融资比指标的数据前置）

独立数据域：funding_events 单张 DuckDB 表，覆盖 IPO 首发 / A 股增发 / 配股
三类历史融资事件；独立适配器实例与限速（CNINFO IPO 1.5s、东财 F10 0.5s），
失败保留旧值并记录独立 retry/missing，绝不进入 stock_meta / 筛选池 / readiness。

写入语义（沿用 business.py 域纪律）：
- 单股事务原子替换：该股全部融资事件在同一 DuckDB 事务内删除重建。
- 任一侧网络错误 → 整股失败，保留旧值，写入 retry_list（去重）。
- 合法空（北交所无东财交叉源 / CNINFO 无 IPO 记录）→ 保留旧值，写入 missing_list。
- 批量节奏：batch_size 只后冷却 batch_cooldown_seconds（东财批 50 + 30s
  为 reports/75 验证的安全组合），全量跨多轮有界续传，严禁硬闯。

防封纪律（reports/58/61/75 提炼）：
- 东财只用 emweb host（BonusFinancing），不对 push2/push2his 发任何请求；
- 限速 ≤2 req/s（eastmoney_f10 0.5s）、CNINFO 对齐 cninfo_capital 1.5s；
- 连续失败 ≥5 次由 AdapterManager 熔断器兜底（5 分钟冷却）。
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
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["FundingUpdater"]

RETRY_DATA_TYPES = ("ipo_funding", "placement_funding")


class FundingUpdater:
    """融资事件低频更新执行器（IPO/增发/配股）。

    构造必须经由注入的 stores 或 validated paths（与其余业务服务一致）；
    无数据库 profile 时 fail-closed（PathIsolationError）。
    """

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
                "FundingUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("FundingUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        # 默认经 AdapterManager 路由（eastmoney_f10 / cninfo_funding 独立限速与熔断）；
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
        """抓取并原子替换单股全部融资事件；失败保留旧值并记录 retry/missing。

        Returns:
            报告 dict：{"status": "success"|"failed", "stock_code", ...}
        """
        ipo_result = self.adapter.fetch(FetchRequest(
            data_type="ipo_funding", stock_codes=[stock_code],
        ))
        placement_result = self.adapter.fetch(FetchRequest(
            data_type="placement_funding", stock_codes=[stock_code],
        ))

        errors: list[str] = []
        for data_type, result in (
            ("ipo_funding", ipo_result),
            ("placement_funding", placement_result),
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
        # 单股事务原子替换：IPO+增发+配股同一事务提交，任一侧异常整体回滚
        all_rows: list[dict[str, Any]] = list(ipo_result.data) + list(placement_result.data)
        with self.duck.transaction() as conn:
            conn.execute("DELETE FROM funding_events WHERE stock_code = ?", [stock_code])
            if all_rows:
                conn.executemany(
                    """INSERT INTO funding_events
                       (stock_code, event_type, announce_date, list_date, issue_price,
                        issue_shares, raise_funds, raise_funds_net, derived, source,
                        fetch_time, raw_hash, confidence, batch_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        [
                            stock_code,
                            row.get("event_type"),
                            row.get("announce_date"),
                            row.get("list_date"),
                            row.get("issue_price"),
                            row.get("issue_shares"),
                            row.get("raise_funds"),
                            row.get("raise_funds_net"),
                            bool(row.get("derived", False)),
                            # 来源按数据类区分：IPO 来自 cninfo_funding，
                            # 增发/配股来自 eastmoney_f10
                            "cninfo_funding" if row.get("event_type") == "ipo" else "eastmoney_f10",
                            fetch_time,
                            (
                                ipo_result.metadata.raw_response_hash
                                if row.get("event_type") == "ipo"
                                else placement_result.metadata.raw_response_hash
                            ),
                            (
                                ipo_result.metadata.confidence
                                if row.get("event_type") == "ipo"
                                else placement_result.metadata.confidence
                            ),
                            batch_id,
                        ]
                        for row in all_rows
                    ],
                )

        # 提交后维护 missing 状态（解决已补上的，登记仍然缺失的）
        for data_type, result in (
            ("ipo_funding", ipo_result),
            ("placement_funding", placement_result),
        ):
            if result.data:
                self._resolve_missing(stock_code, data_type)
            else:
                self._record_missing(stock_code, data_type, "source_empty")
        # 成功后清理该股票已解决的重试条目
        self._resolve_retry(stock_code)

        return {
            "status": "success",
            "stock_code": stock_code,
            "batch_id": batch_id,
            "event_rows": len(all_rows),
            "ipo_rows": len(ipo_result.data),
            "placement_rows": len(placement_result.data),
        }

    # ─── 批量 / 全量 ──────────────────────────────────────────────

    def update_many(
        self,
        stock_codes: list[str],
        *,
        batch_size: int = 50,
        batch_cooldown_seconds: float = 30.0,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """逐股更新给定股票，按批冷却（东财批 50 + 30s 安全组合）。

        批间冷却既是防封纪律（reports/75），也给 CNINFO IPO 请求留出喘息。
        """
        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for index, code in enumerate(stock_codes):
            outcome = self.update_stock(code)
            results[code] = outcome
            if outcome["status"] != "success":
                failed.append(code)
            if progress_cb is not None:
                progress_cb(code, outcome)
            if batch_cooldown_seconds > 0 and (index + 1) % batch_size == 0 \
                    and index + 1 < len(stock_codes):
                logger.info(
                    "融资域批间冷却 %.0fs（已完成 %d/%d）", batch_cooldown_seconds, index + 1, len(stock_codes),
                )
                time.sleep(batch_cooldown_seconds)
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
        """更新全部上市股票（有界续传：max_stocks>0 时只处理前 N 只）。

        融资事件是历史低频数据：一次补齐后日常无需频繁刷新（仅新股增量）。
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

        # 跳过已有覆盖的股票（有界续传的断点：无 funding_events 者优先）。
        # max_stocks 必须作用在“未覆盖子集”上，而不是全市场列表的前 N 只：
        # 否则前 N 只一旦覆盖完毕，后续轮次会永远重复跳过同一前缀，
        # 全量续传无法推进（2026-08-25 数据补全实测发现）。
        covered = {
            row["stock_code"]
            for row in self.duck.read_query(
                "SELECT DISTINCT stock_code FROM funding_events"
            )
        }
        pending = [code for code in codes if code not in covered]
        if not pending:
            return {"status": "skipped", "reason": "all_funding_covered"}
        if max_stocks > 0:
            pending = pending[:max_stocks]
        report = self.update_many(pending)
        if mark_refreshed and report["status"] in {"success", "partial"}:
            self._mark_refreshed()
        return report

    def refresh_if_due(self, max_stocks: int = 0) -> dict[str, Any]:
        """低频自动集成入口：每轮有界处理，避免拖慢启动更新。"""
        enabled = bool(self._load_config("funding_auto_enabled", default=True))
        if not enabled:
            return {"status": "skipped", "reason": "auto_disabled"}
        configured_max = int(self._load_config("funding_max_stocks_per_run", default=300))
        limit = max_stocks or configured_max
        report = self.update_all(max_stocks=limit, mark_refreshed=False)
        return report

    def _mark_refreshed(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                ["funding_last_refresh", now, now],
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
            logger.warning("记录融资事件失败信息失败: %s", e)

    def _record_missing(self, stock_code: str, field_name: str, reason_code: str) -> None:
        """写 missing_list（去重：每股票+字段仅一条未解决）。"""
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
            logger.warning("记录融资事件缺失信息失败: %s", e)

    def _resolve_missing(self, stock_code: str, field_name: str) -> None:
        """数据已到达时解决对应未解决 missing 条目。"""
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = ? AND resolved_at IS NULL""",
                [datetime.now(UTC).isoformat(), stock_code, field_name],
            )
        except Exception as e:
            logger.warning("解决融资事件缺失信息失败: %s", e)

    def _resolve_retry(self, stock_code: str) -> None:
        """数据已到达时清理该股票的待重试条目。"""
        try:
            self.sqlite.execute(
                """DELETE FROM retry_list
                   WHERE stock_code = ? AND data_type IN (?, ?)""",
                [stock_code, RETRY_DATA_TYPES[0], RETRY_DATA_TYPES[1]],
            )
        except Exception as e:
            logger.warning("清理融资事件重试条目失败: %s", e)

    # ─── 只读状态报告（CLI check-only） ───────────────────────────

    def status_report(self) -> dict[str, Any]:
        """返回融资域的覆盖与队列状态（只读，不抓取）。"""
        try:
            listed = self.duck.read_query(
                "SELECT COUNT(*) AS count FROM stock_meta WHERE is_listed IS TRUE"
            )[0]["count"]
            covered = self.duck.read_query(
                "SELECT COUNT(DISTINCT stock_code) AS count FROM funding_events"
            )[0]["count"]
            by_type = self.duck.read_query(
                """SELECT event_type, COUNT(*) AS n
                   FROM funding_events GROUP BY event_type ORDER BY event_type"""
            )
        except Exception as error:
            return {"status": "error", "error": str(error)}
        retry_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM retry_list WHERE data_type IN (?, ?)",
            list(RETRY_DATA_TYPES),
        )[0]["count"]
        missing_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM missing_list "
            "WHERE field_name IN (?, ?) AND resolved_at IS NULL",
            list(RETRY_DATA_TYPES),
        )[0]["count"]
        return {
            "status": "ok",
            "listed_stocks": listed,
            "funding_covered": covered,
            "events_by_type": {row["event_type"]: row["n"] for row in by_type},
            "retry_open": retry_open,
            "missing_open": missing_open,
        }
