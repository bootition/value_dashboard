"""个股业务概览低频域更新服务（依据 S1, 依据 S2 §6）

独立数据域：company_profile / business_breakdown 两张 DuckDB 表，
独立东财 F10 适配器实例与限速，失败保留旧值并记录独立 retry/missing，
绝不进入 stock_meta / indicator_snapshot / 筛选池 / readiness。

写入语义（依据 S2 §5 架构与安全门槛）：
- 单股事务原子替换：一只股票的资料 + 构成在同一个 DuckDB 事务内完成。
- 任一侧网络错误 → 整股失败，保留旧值，写入 retry_list（去重）。
- 空/畸形响应（合法 missing）→ 保留旧值，写入 missing_list（去重/可解决）。
- 新数据到达时解决对应未解决 missing 条目。
"""

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，不再重复报告号）
# S1  reports/67 / §4.5 —— 个股业务概览独立低频域：通常随财报期后触发或按月检查（§4.5）；
#     不进主链 readiness。
# S2  reports/68 §5 / §6 —— 写入语义的架构与安全门槛（§5）；business_breakdown 随刷新
#     自然累积「历史可得数据」（§5）；统计域口径见 §6。


from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
from app.core.adapters.manager import AdapterManager
from app.core.storage.duckdb_store import DuckDBStore, is_index_inconsistency_error
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["BusinessOverviewUpdater"]

REFRESH_MARKER_KEY = "business_overview_last_refresh"
RETRY_DATA_TYPES = ("company_profile", "business_breakdown")

# 唯一需要「索引 ↔ 堆」自愈的表：本域每次刷新都要先 DELETE 该股的历史构成，
# DELETE 的索引维护在 commit 里逐条核对 —— 索引一旦与堆失联就在这里 FATAL。
_BUSINESS_OVERVIEW_INDEX_TABLE = "business_breakdown"


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
        # 索引自愈状态：同一进程只重建一次二级索引（更新子进程 = 一轮，故每轮一次）。
        self._index_reindexed = False

    # ─── 配置 ─────────────────────────────────────────────────────

    @staticmethod
    def _load_config(key: str, *, default: Any) -> Any:
        try:
            from app.core.config import Config
            cfg = Config.current()
            update_cfg = cfg.get_value("update", {})
            if isinstance(update_cfg, dict) and key in update_cfg:
                return update_cfg[key]
        except Exception:
            pass
        return default

    # ─── 索引 ↔ 堆自愈 ────────────────────────────────────────────

    def _ensure_index_fresh(self, *, reason: str) -> dict[str, Any] | None:
        """本轮第一次写库前重建 `business_breakdown` 的二级索引（2026-11-21 起是 no-op）。

        为什么是"无条件重建"而不是"先检测再修"：
        * 失联的是**二级索引**（`idx_business_breakdown_stock_report`）。DuckDB 没有
          "把索引条目读出来跟堆逐行比对"的入口：`EXPLAIN`/`EXPLAIN ANALYZE` 只能说明
          计划**用没用**索引（实测在 22.9M 行的 `source_audit` 上真会走 `Type: Index Scan`），
          读不出索引里**有哪些行**；`INSERT ... ON CONFLICT` 探针只查主键 ART，
          对二级索引**完全盲**（实测：探针报 consistent，紧接着的 DELETE 照样
          FATAL）。唯一能"测出来"的办法就是真的 DELETE —— 而 DOOM 就在那里。
        * **根上已经没有了（schema v50，reports/151）**：30 个二级索引全部删除
          （`DROPPED_SECONDARY_INDEXES`），`business_breakdown` 上不再有任何二级索引，
          所以 `reindex_table()` 现在是**安全 no-op**（`indexes_rebuilt == []`）。
          保留这条调用是为了"如果有人又建了索引"时行为不变（幂等重建），
          不是为了现在还有索引可修：删掉它们的前提是**列与真实查询形状不匹配**
          （代码里没有 `col = 字面量` 这种查询），不是因为"优化器弃之不用"。
        * 重建只要 0.45s（101 万行），而一轮更新 ~900s。**把检测问题换成幂等的
          重建**，代价 0.05%，收益是这一类猝死整类消失。
        * 幂等：索引由堆数据遍历生成，重建后条目与堆逐行对应；重复执行结果相同。
        """
        if self._index_reindexed:
            return None
        result = self.duck.reindex_table(_BUSINESS_OVERVIEW_INDEX_TABLE)
        self._index_reindexed = True
        logger.info("business_breakdown 二级索引已重建（%s）: %s", reason, result)
        return result

    # ─── 单股原子更新 ─────────────────────────────────────────────

    def update_stock(self, stock_code: str) -> dict[str, Any]:
        """抓取并原子替换单股业务概览；失败保留旧值并记录 retry/missing。

        Returns:
            报告 dict：{"status": "success"|"failed", "stock_code", ...}
        """
        self._ensure_index_fresh(reason="update_stock")
        profile_result = self._fetch_one(stock_code, "company_profile")
        breakdown_result = self._fetch_one(stock_code, "business_breakdown")
        return self._persist_fetched(stock_code, profile_result, breakdown_result)

    def _fetch_one(self, stock_code: str, data_type: str) -> Any:
        """抓取单个 F10 数据面；适配器抛异常时转成失败结果，不中断批处理。"""
        try:
            return self.adapter.fetch(FetchRequest(
                data_type=data_type, stock_codes=[stock_code],
            ))
        except Exception as error:
            logger.warning("抓取 %s %s 失败: %s", stock_code, data_type, error)
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="eastmoney_f10",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash="",
                    confidence="missing",
                    error=str(error),
                ),
            )

    def _persist_fetched(
        self,
        stock_code: str,
        profile_result: Any,
        breakdown_result: Any,
    ) -> dict[str, Any]:
        """按现有原子语义持久化一对已抓取结果。"""
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

        def write_pair() -> None:
            # 单股事务原子替换：资料与构成同一事务提交，任一侧异常整体回滚
            with self.duck.transaction() as conn:
                if profile_result.data:
                    self._replace_profile(conn, stock_code, profile_result, batch_id, fetch_time)
                if breakdown_result.data:
                    self._replace_breakdown(
                        conn, stock_code, breakdown_result, batch_id, fetch_time,
                    )

        try:
            write_pair()
        except Exception as error:
            # 索引 ↔ 堆失联时 DuckDB 在 commit 里抛内部 FATAL；不重建的话
            # 下一次 DELETE 还会撞同一个不变量，甚至直接 fail-fast 打死进程
            # （0xC0000409，无 traceback）。这里尽量就地自愈并重试一次，
            # 把"整轮更新猝死"降级成"这一股重试一次"。
            if not is_index_inconsistency_error(error):
                raise
            logger.error(
                "business_breakdown 索引与堆数据失联（DuckDB 内部 FATAL），重建索引后重试: %s",
                error,
            )
            logger.warning("business_breakdown 索引已重建: %s",
                           self.duck.reindex_table(_BUSINESS_OVERVIEW_INDEX_TABLE))
            self._index_reindexed = True
            try:
                write_pair()
            except Exception as retry_error:
                if not is_index_inconsistency_error(retry_error):
                    raise
                # 二级索引重建没能解决 ⇒ 堆侧的 ART 也坏了，只能整表重建。
                logger.error(
                    "重建二级索引后仍失联，退化为整表重建: %s", retry_error,
                )
                logger.warning("business_breakdown 整表已重建: %s",
                               self.duck.rebuild_table(_BUSINESS_OVERVIEW_INDEX_TABLE))
                write_pair()

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
        使 business_breakdown 随刷新自然累积"历史可得数据"（依据 S2 §5）。
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
        """更新给定股票，返回汇总报告。

        抓取按 business_overview_concurrency 并发入队；EastMoneyF10Adapter
        的全局限速锁仍保证源侧不超过 2 req/s。DuckDB 写入始终在主线程
        逐股串行提交，避免同进程多写者竞争。
        """
        if not stock_codes:
            return {
                "status": "success", "targeted": 0, "succeeded": 0,
                "failed": 0, "failed_codes": [], "results": {},
            }
        self._ensure_index_fresh(reason="update_many")
        concurrency = max(
            1, int(self._load_config("business_overview_concurrency", default=4)),
        )
        if concurrency <= 1 or len(stock_codes) <= 1:
            return self._update_many_serial(stock_codes, progress_cb=progress_cb)

        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        data_types = ("company_profile", "business_breakdown")
        pending: dict[Future[Any], tuple[str, str]] = {}
        partial: dict[str, dict[str, Any]] = {}
        max_inflight = max(concurrency * 4, 8)
        iterator = (
            (stock_code, data_type)
            for stock_code in stock_codes
            for data_type in data_types
        )

        def submit_next(executor: ThreadPoolExecutor) -> None:
            for _ in range(max_inflight - len(pending)):
                try:
                    stock_code, data_type = next(iterator)
                except StopIteration:
                    return
                future = executor.submit(self._fetch_one, stock_code, data_type)
                pending[future] = (stock_code, data_type)

        with ThreadPoolExecutor(
            max_workers=min(concurrency, max_inflight),
            thread_name_prefix="vd-business-overview",
        ) as executor:
            submit_next(executor)
            while pending:
                for future in as_completed(tuple(pending)):
                    stock_code, data_type = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        result = FetchResult(
                            data=[],
                            metadata=SourceMetadata(
                                source="eastmoney_f10",
                                fetch_time=datetime.now(UTC),
                                raw_response_hash="",
                                confidence="missing",
                                error=str(error),
                            ),
                        )
                    partial.setdefault(stock_code, {})[data_type] = result
                    submit_next(executor)
                    if len(partial.get(stock_code, {})) == len(data_types):
                        outcome = self._persist_fetched(
                            stock_code,
                            partial[stock_code]["company_profile"],
                            partial[stock_code]["business_breakdown"],
                        )
                        partial.pop(stock_code, None)
                        results[stock_code] = outcome
                        if outcome["status"] != "success":
                            failed.append(stock_code)
                        if progress_cb is not None:
                            progress_cb(stock_code, outcome)

        return {
            "status": "success" if not failed else "partial",
            "targeted": len(stock_codes),
            "succeeded": len(stock_codes) - len(failed),
            "failed": len(failed),
            "failed_codes": failed[:20],
            "results": results,
        }

    def _update_many_serial(
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

        业务概览是低频域：通常随财报期后触发或按月检查（依据 S1 §4.5）。
        max_stocks>0 时只处理“缺失/陈旧”子集，不能对全市场列表前 N 只
        反复刷新，否则 CLI 加速命令会把已覆盖的前缀刷了一遍又一遍。
        """
        if max_stocks > 0:
            interval_days = int(self._load_config(
                "business_overview_refresh_interval_days", default=30,
            ))
            codes = self._due_stock_codes(interval_days)
            if not codes:
                return {"status": "skipped", "reason": "no_due_stocks"}
        else:
            # max_stocks=0 是显式全量刷新语义，不能因为“当前无 due”而跳过。
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
        # 已确认源缺失（profile/breakdown 均无数据）的股票在重试窗口内出队，
        # 避免北交所等无 F10 覆盖的股票每轮占住限速额度与队头位置。
        try:
            missing_cutoff = datetime.now(UTC) - timedelta(days=max(0, int(
                self._load_config(
                    "business_overview_missing_retry_days", default=7,
                )
            )))
            blocked_rows = self.sqlite.query(
                """SELECT stock_code FROM missing_list
                   WHERE field_name IN ('company_profile', 'business_breakdown')
                     AND resolved_at IS NULL
                     AND detected_at >= ?
                   GROUP BY stock_code
                   HAVING COUNT(DISTINCT field_name) = 2""",
                [missing_cutoff.isoformat()],
            )
            blocked = {row["stock_code"] for row in blocked_rows}
            codes = [code for code in codes if code not in blocked]
        except Exception as error:
            logger.warning("查询业务概览 missing 缓存失败: %s", error)
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
