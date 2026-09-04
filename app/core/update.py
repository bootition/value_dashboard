"""增量检查与更新 (PRD §7.3, §7.4)

每次启动只进行简单增量检查：
- 是否出现新的交易日
- 是否出现新的公告或财报
- 是否存在待重试任务

失败处理（PRD §7.4 L4）：
- 保留旧值不以空值覆盖
- 生成重试列表
- 生成缺失列表
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.job_status import aggregate_job_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _is_duckdb_fatal(error: Exception) -> bool:
    """DuckDB 致命错误（连接失效类）检测，用于 retry 隔离时的原因标注。

    2026-08-14 红队 F1：ART 索引损坏时 DELETE 抛 FatalException 且连接
    随后 invalidated；此类错误重试无意义，应在原因里引导重建索引。
    """
    text = f"{type(error).__name__} {error}".lower()
    return "fatalexception" in text or "invalidated" in text or "must be restarted" in text


# ─── 公告分类（PRD §7.7）───────────────────────────────────────────
# 只有定期报告/业绩预告/业绩快报类公告触发财务刷新；
# 其他公告只登记入册，不进入财务刷新队列。

_ANNOUNCEMENT_FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "年度报告", "半年度报告", "一季报", "第一季度报告", "三季报", "第三季度报告",
    "业绩预告", "业绩快报",
)
# 财务公告按 CNINFO 类别精确查询，避免全市场全文列表翻页被噪声淹没。
# 顺序即查询顺序：当前处于中报季，半年报优先。
_ANNOUNCEMENT_FINANCIAL_CATEGORIES: tuple[str, ...] = (
    "semi_annual", "q1", "q3", "annual",
)
_ANNOUNCEMENT_CHECK_CURSOR_KEY = "announcement_check_cursor"
_ANNOUNCEMENT_DIVIDEND_KEYWORDS: tuple[str, ...] = (
    "权益分派", "分红", "除权除息", "利润分配",
)


def classify_announcement(title: str | None) -> str:
    """按公告标题分类：financial（触发财务刷新）| dividend（分红除权）| other（只登记）"""
    if not title:
        return "other"
    if any(kw in title for kw in _ANNOUNCEMENT_FINANCIAL_KEYWORDS):
        return "financial"
    if any(kw in title for kw in _ANNOUNCEMENT_DIVIDEND_KEYWORDS):
        return "dividend"
    return "other"


class IncrementalUpdater:
    """增量检查与更新执行器

    PRD §7.3: 每次启动只进行简单增量检查。
    PRD §7.4: 失败时保留旧值，生成重试列表与缺失列表。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        adapter_mgr: AdapterManager | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("IncrementalUpdater requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("IncrementalUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.adapter_mgr = adapter_mgr or AdapterManager()
        self.duck = duck
        self.sqlite = sqlite
        # 增量窗口上限：本地价格距今超过该天数时，raw 也整段重拉
        # （默认 30 天；可由 config/update.incremental_window_days 覆盖）
        self.incremental_window_days: int = self._load_update_config(
            "incremental_window_days", default=30
        )
        # 全市场公告检查的日期回看窗口（默认 3 天）
        self.announcement_lookback_days: int = self._load_update_config(
            "announcement_lookback_days", default=3
        )
        # 公告检查游标缺失时的首次追补窗口（天）。2026-08-28 之前
        # CNINFO 分页 bug 导致中报大量漏检，首次修复需回看整个披露季。
        self.announcement_catchup_days: int = self._load_update_config(
            "announcement_catchup_days", default=45
        )
        # CNINFO 每页实际固定返回 30 条；财务类别在 45 天窗口约 11,000 条，
        # 需要 370+ 页。非财务公告只做最近 3 天注册，页数设上限即可。
        self.announcement_financial_max_pages: int = self._load_update_config(
            "announcement_financial_max_pages", default=450
        )
        self.announcement_general_max_pages: int = self._load_update_config(
            "announcement_general_max_pages", default=40
        )
        # CSRC 行业低频刷新间隔（天）：行业归属变化极低，避免每次更新
        # 逐股查询 CNINFO（约 1.5s/股）占用数小时（默认 30 天）
        self.csrc_refresh_interval_days: int = self._load_update_config(
            "csrc_refresh_interval_days", default=30
        )
        self.financial_detail_backfill_max_stocks_per_run: int = self._load_update_config(
            "financial_detail_backfill_max_stocks_per_run", default=10000
        )
        self.financial_detail_backfill_concurrency: int = self._load_update_config(
            "financial_detail_backfill_concurrency", default=16
        )
        self.financial_detail_backfill_persist_batch_size: int = self._load_update_config(
            "financial_detail_backfill_persist_batch_size", default=50
        )
        self.financial_detail_backfill_tdx_max_stocks_per_run: int = self._load_update_config(
            "financial_detail_backfill_tdx_max_stocks_per_run", default=10
        )
        self.financial_detail_backfill_missing_retry_days: int = self._load_update_config(
            "financial_detail_backfill_missing_retry_days", default=7
        )
        self.buyback_refresh_interval_days: int = self._load_update_config(
            "buyback_refresh_interval_days", default=7
        )
        # 价格批量抓取的并发网络请求数（HTTP 源；socket 源内部强制串行）
        self.price_fetch_concurrency: int = self._load_update_config(
            "price_fetch_concurrency", default=8
        )
        # 连续流水线中的在途股票任务数：慢股票只占用自己的槽位，
        # 不再像固定窗口那样拖住同窗口所有已完成股票。
        self.price_fetch_pipeline_depth: int = self._load_update_config(
            "price_fetch_pipeline_depth", default=64
        )
        # 批量持久化：每批合并到单个 DuckDB 事务，减少 WAL 提交次数
        self.price_fetch_batch_size: int = self._load_update_config(
            "price_fetch_batch_size", default=20
        )
        # 历史统计域全量重建的进程池并行数（只读分析；发布仍在主进程）。
        # 2026-08-28 提速计划：串行重建约需数十分钟，多进程可显著缩短。
        self.research_statistics_parallel_workers: int = self._load_update_config(
            "research_statistics_parallel_workers", default=12
        )
        # 财务三表刷新的并发股票数：网络请求并行，DuckDB 写事务仍由
        # 单写者锁串行，因此并发只缩短网络等待、不会双写。
        self.financial_refresh_concurrency: int = self._load_update_config(
            "financial_refresh_concurrency", default=8
        )
        # 混合批量写：并发抓取后每满 N 只合并为一个 DuckDB 事务。
        self.financial_refresh_batch_size: int = self._load_update_config(
            "financial_refresh_batch_size", default=20
        )
        self.price_fetch_timeout_seconds: int = self._load_update_config(
            "price_fetch_timeout_seconds", default=45
        )
        self.price_fetch_max_concurrency: int = self._load_update_config(
            "price_fetch_max_concurrency", default=16
        )
        self.price_fetch_concurrency_step: int = self._load_update_config(
            "price_fetch_concurrency_step", default=4
        )
        self.price_fetch_scale_up_seconds: int = self._load_update_config(
            "price_fetch_scale_up_seconds", default=300
        )
        # universe 步骤（股票池/上市状态）按日节流：全市场 stock_list 约
        # 51s + listing_info 约 53s，后台线程可接受但无需每轮重拉（默认 1 天）
        self.universe_refresh_interval_days: int = self._load_update_config(
            "universe_refresh_interval_days", default=1
        )

    @staticmethod
    def _update_duckdb_budget() -> tuple[str, int]:
        """更新专用的 DuckDB 低内存预算（4GB/2线程，可配置）。"""
        try:
            from app.core.config import Config
            cfg = Config.current().get_value("update", {})
            budget = str(cfg.get("duckdb_memory_limit") or "4GB") if isinstance(cfg, dict) else "4GB"
            threads = int(cfg.get("duckdb_threads") or 2) if isinstance(cfg, dict) else 2
            return budget, max(1, threads)
        except Exception:
            return "4GB", 2

    @staticmethod
    def _load_update_config(key: str, *, default: int) -> int:
        try:
            from app.core.config import Config
            cfg = Config.current()
            update_cfg = cfg.get_value("update", {})
            value = update_cfg.get(key) if isinstance(update_cfg, dict) else None
            return int(value) if value else default
        except Exception:
            return default

    def run_incremental_check(self, *, include_announcements: bool = True) -> dict[str, Any]:
        """执行增量检查，返回检查报告

        不自动执行更新——只检查并报告需要更新的内容。
        实际更新由 run_incremental_update() 执行。
        """
        report: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "new_trading_days": [],
            "latest_local_price_date": None,
            "announcement_check": None,
            "retry_tasks": [],
            "missing_count": 0,
            "needs_update": False,
        }

        # 1. 检查新交易日
        report["new_trading_days"] = self._check_new_trading_days()
        report["latest_local_price_date"] = self._get_latest_local_price_date()

        # 2. 检查新公告（通过CNINFO最新公告时间对比本地）
        report["announcement_check"] = (
            self._check_new_announcements(persist=False)
            if include_announcements
            else {"status": "deferred", "checked_remote": False, "affected_stock_codes": []}
        )

        # 3. 检查待重试任务
        report["retry_tasks"] = self._check_retry_tasks()

        # 4. 缺失列表
        missing = self.sqlite.query("SELECT COUNT(*) as cnt FROM missing_list")
        report["missing_count"] = missing[0]["cnt"] if missing else 0

        # 判断是否需要更新
        report["needs_update"] = bool(
            report["new_trading_days"]
            or report["retry_tasks"]
            or report["latest_local_price_date"] is None
            or report["announcement_check"]["status"] in {"unavailable", "partial"}
        )
        report["blocked"] = report["announcement_check"]["status"] in {"unavailable", "partial"}

        logger.info(f"增量检查完成: needs_update={report['needs_update']}, "
                     f"new_trading_days={len(report['new_trading_days'])}, "
                     f"retry_tasks={len(report['retry_tasks'])}")

        return report

    def run_incremental_update(
        self,
        max_stocks: int = 0,
        *,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """执行增量更新（跨进程单写者串行）

        Args:
            max_stocks: 最多更新的股票数量（0=全部需要更新的股票）
            progress_cb: 每个步骤完成后回调 (step_name, step_result)，
                供 AutoUpdateController 持久化可操作进度（PRD §7.3）。
            detail_cb: 步骤内的细粒度回调（如逐股价格进度
                {done, total, current, label}），用于实时进度条与日志。

        Returns:
            更新报告；被跨进程锁拒绝时返回 {"status": "skipped",
            "reason": "another_update_running"}。
        """
        logger.info("=" * 60)
        logger.info("开始增量更新 (PRD §7.3)")
        logger.info("=" * 60)

        from app.core.storage.update_lock import UpdateLockError, exclusive_update

        try:
            with exclusive_update(
                self.duck.db_path,
                on_stale_lock=self._reconcile_crashed_incremental_jobs,
            ):
                try:
                    # 自动更新是全库重计算，但只在本线程/子进程生命周期内
                    # 使用 update.duckdb_memory_limit 与 update.duckdb_threads。
                    # 更新进程退出后内存完整归还 OS。该覆盖只允许在专用子进程
                    # 使用，Web 进程内的连接必须保持 database.* 统一配置。
                    budget, threads = self._update_duckdb_budget()
                    with self.duck.memory_limit(
                        budget, threads=threads, preserve_insertion_order=False,
                    ):
                        return self._run_incremental_update_locked(max_stocks, progress_cb, detail_cb)
                finally:
                    close = getattr(self.adapter_mgr, "close", None)
                    if callable(close):
                        close()
                    import gc
                    gc.collect()
        except UpdateLockError:
            logger.warning("增量更新被跨进程锁拒绝：另一更新正在运行")
            return {
                "status": "skipped",
                "reason": "another_update_running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    def _reconcile_crashed_incremental_jobs(self) -> None:
        """Close jobs abandoned by the dead process that owned the update lock."""
        finished_at = datetime.now(UTC).isoformat()
        running = self.sqlite.query(
            """SELECT id, details_json FROM job_logs
               WHERE job_type = 'incremental_update' AND status = 'running'"""
        )
        if not running:
            return
        with self.sqlite.transaction() as connection:
            for job in running:
                try:
                    details = json.loads(job.get("details_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    details = {"legacy_details": job.get("details_json")}
                details["reconciliation"] = {
                    "reason_code": "dead_update_lock_owner",
                    "reconciled_at": finished_at,
                }
                connection.execute(
                    """UPDATE job_logs SET status = 'failed', finished_at = ?, details_json = ?
                       WHERE id = ? AND status = 'running'""",
                    [finished_at, json.dumps(details, ensure_ascii=False), job["id"]],
                )
        logger.warning("已回收死亡更新锁并结算 %d 条悬挂增量作业", len(running))

    def _run_incremental_update_locked(
        self,
        max_stocks: int,
        progress_cb: Callable[[str, dict[str, Any]], None] | None,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """持锁执行更新主体；job_logs 生命周期（P1: 状态页"最近更新"）。"""
        job_id = str(uuid.uuid4())
        started_at = datetime.now(UTC).isoformat()
        job_row_id = self._start_job(job_id, max_stocks, started_at)
        try:
            report = self._run_incremental_update_flow(
                max_stocks, progress_cb, job_id, started_at, detail_cb
            )
        except Exception as error:
            self._finish_job(job_row_id, "failed", {"error": str(error)})
            raise
        self._finish_job(job_row_id, report["status"], report)
        return report

    def _start_job(self, job_id: str, max_stocks: int, started_at: str) -> int:
        """记录增量更新作业开始（PRD §15 最近更新合同）。"""
        with self.sqlite.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO job_logs (job_type, status, started_at, details_json)
                   VALUES ('incremental_update', 'running', ?, ?)""",
                [started_at, json.dumps({"job_id": job_id, "max_stocks": max_stocks}, ensure_ascii=False)],
            )
            return cursor.lastrowid

    def _finish_job(self, job_row_id: int, status: str, details: dict[str, Any]) -> None:
        """结束增量更新作业，保留 partial/skipped 语义供状态页展示。

        2026-08-28：partial 表示部分步骤降级但数据实际已推进，不能记成
        failed，否则状态页"最近一次更新执行"显示 failed、误导用户以为
        本轮更新无效。
        """
        finished_at = datetime.now(UTC).isoformat()
        persisted_status = (
            status if status in {"success", "partial", "skipped"} else "failed"
        )
        self.sqlite.execute(
            """UPDATE job_logs SET status = ?, finished_at = ?, details_json = ?
               WHERE id = ?""",
            [
                persisted_status,
                finished_at,
                json.dumps(details, ensure_ascii=False, default=str),
                job_row_id,
            ],
        )
        price_step = details.get("steps", {}).get("prices", {})
        if price_step.get("total"):
            value = {
                "rate_per_minute": price_step.get("rate_per_minute"),
                "elapsed_seconds": price_step.get("elapsed_seconds"),
                "total": price_step.get("total"),
                "success": price_step.get("success"),
                "failed": price_step.get("failed"),
                "finished_at": finished_at,
            }
            self.sqlite.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                  updated_at=excluded.updated_at""",
                ["price_update_last_rate", json.dumps(value, ensure_ascii=False), finished_at],
            )

    def _run_incremental_update_flow(
        self,
        max_stocks: int,
        progress_cb: Callable[[str, dict[str, Any]], None] | None,
        job_id: str,
        started_at: str,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """更新主流程（调用方已持有跨进程更新锁）。"""
        # 先检查
        # 公告会在价格步骤之后以 persist=True 抓取。前置阶段只判断本地
        # 价格/重试状态，避免一次更新重复抓公告并阻塞每日价格新鲜度。
        check_report = self.run_incremental_check(include_announcements=False)

        report: dict[str, Any] = {
            "check": check_report,
            "started_at": started_at,
            "job_id": job_id,
            "steps": {},
        }

        def report_step(name: str, step: dict[str, Any]) -> dict[str, Any]:
            report["steps"][name] = step
            if progress_cb is not None:
                progress_cb(name, step)
            return step

        def indicator_progress(info: dict[str, Any]) -> None:
            """把指标重算的细粒度进度转成状态页可展示的 live 进度。"""
            if detail_cb is not None:
                detail_cb("indicators", info)

        if check_report["blocked"]:
            report_step("announcements", {
                "status": "partial",
                "reason": "authoritative announcement and financial freshness check is unavailable",
            })

        # 1. 价格新鲜度是每日启动的首要目标。低频股票池/行业或公告源
        # 变慢时，不应阻塞已上市股票的日线补齐。
        if check_report["new_trading_days"]:
            report_step("trading_dates", self._update_trading_dates())
        price_step = report_step("prices", self._update_prices_incremental(max_stocks, detail_cb))
        updated_price_codes = price_step.pop("_updated_codes", [])

        if max_stocks > 0:
            if price_step["status"] == "success" and price_step.get("success", 0) > 0:
                from app.core.indicators.calculator import IndicatorCalculator

                report_step(
                    "indicators",
                    IndicatorCalculator(duck=self.duck, sqlite=self.sqlite).compute_snapshot_for_codes(
                        updated_price_codes, progress_cb=indicator_progress,
                    ),
                )
            report["status"] = aggregate_job_status(report["steps"])
            report["finished_at"] = datetime.now(UTC).isoformat()
            logger.info("有界价格更新完成: %s", report["status"])
            return report

        # PRD §7.7 第 4 项: 股本与上市名单（新股/退市/股本变化），
        # 以及 PRD §24 CSRC 行业分类（低频刷新）。
        # P1: 股本变化后必须触发快照重算，否则 total_market_cap 等
        # 市值类指标仍使用旧快照（口径过期风险）。
        shares_before = self._share_capital_fingerprint()
        report_step("universe", self._refresh_universe_metadata())
        shares_after = self._share_capital_fingerprint()
        share_capital_changed = shares_before != shares_after
        report["share_capital_changed"] = share_capital_changed

        announcement_check = self._check_new_announcements(persist=True, detail_cb=detail_cb)
        report["check"]["announcement_check"] = announcement_check
        # 上一轮财务刷新失败/pending 的公告已持久化在 retry_list。把它们
        # 并入本轮待刷新集合：游标推进后不再重复翻 45 天公告，pending 也不丢。
        pending_items = self._pending_announcement_items()
        pending_codes = sorted({str(item["stock_code"]) for item in pending_items})
        for item in pending_items:
            code = str(item["stock_code"])
            announcement_check.setdefault("affected_announcements", {}).setdefault(code, [])
            existing_ids = {
                str(existing.get("announcement_id"))
                for existing in announcement_check["affected_announcements"][code]
            }
            if item.get("announcement_id") not in existing_ids:
                announcement_check["affected_announcements"][code].append(item)
        announcement_codes = sorted(set(announcement_check.get("affected_stock_codes", [])) | set(pending_codes))
        announcement_check["affected_stock_codes"] = announcement_codes
        # B 股（200/900）不在任何免费财务主源覆盖范围，重复 retry 只会产生
        # 永续失败。与不可补字段同策略：直接入册为已处理、清理 retry，
        # 不进入财务刷新，也不在页面制造噪音。
        unsupported_codes = {
            code for code in announcement_codes
            if self._is_b_share_stock(code)
        }
        if unsupported_codes:
            for code in sorted(unsupported_codes):
                self._mark_announcements_seen(
                    code,
                    announcement_check.get("affected_announcements", {}).get(code, []),
                )
                self._resolve_announcement_retries(code)
            announcement_codes = [
                code for code in announcement_codes
                if code not in unsupported_codes
            ]
            announcement_check["affected_stock_codes"] = announcement_codes
        financials: dict[str, Any] | None = None
        if announcement_codes:
            financials = self._refresh_financials(announcement_codes, detail_cb=detail_cb)
            report_step("financials", financials)
            # 只有同一批公告里出现“权益分派实施”等分红除权公告时，才刷新
            # dividends/xdxr。中报季每天上千份财报公告，逐股重拉分红域会
            # 白白增加数小时请求；非分红财报不需要该步骤。
            market_action_codes = sorted({
                code
                for code, items in announcement_check.get("all_new_announcements", {}).items()
                if any(classify_announcement(item.get("title")) == "dividend" for item in items)
            })
            if market_action_codes:
                actions = self._refresh_market_actions(
                    market_action_codes, detail_cb=detail_cb,
                )
                report_step("market_actions", actions)
            # 只有真正写入新报告期的股票才登记公告；
            # 数据源延迟（pending）或刷新失败（failed）保持公告 pending 并记录重试
            refreshed_codes = set(financials["succeeded_codes"])
            for code, announcements in announcement_check["affected_announcements"].items():
                if code in refreshed_codes:
                    self._mark_announcements_seen(code, announcements)
                    self._resolve_announcement_retries(code)
                else:
                    reason = (
                        "financial data source not yet ready; announcement remains pending"
                        if code in set(financials.get("pending_codes", []))
                        else "financial refresh failed; announcement remains pending"
                    )
                    self._record_failure(
                        code,
                        "announcements",
                        "cninfo",
                        reason,
                        extra_json=json.dumps({"announcement_ids": [item["announcement_id"] for item in announcements]}),
                    )

        # 非财务类公告只登记入册，不触发财务刷新（PRD §7.7）
        all_new = announcement_check.get("all_new_announcements", {})
        financial_set = set(announcement_codes)
        for code, announcements in all_new.items():
            if code in financial_set:
                continue
            self._mark_announcements_seen(code, announcements)

        # 财务公告处理完即推进公告检查游标。失败/pending 已写入
        # retry_list，下一轮直接从 retry_list 续传，不必再翻 45 天公告。
        if (
            announcement_check.get("status") == "available"
            and not announcement_check.get("errors")
        ):
            self._save_announcement_cursor()
            self._resolve_announcement_source_retries()

        financial_step = report["steps"].get("financials", {"status": "success"})
        # 财报明细缺口有界续传：只重抓“核心字段齐全但明细字段缺失”的股票。
        # 该步骤不参与 readiness 阻断，只负责逐步补齐详情页字段。
        detail_backfill = report_step(
            "financial_detail_backfill",
            self._refresh_financial_detail_backfill(detail_cb=detail_cb),
        )
        detail_backfilled_codes = list(detail_backfill.get("succeeded_codes", []))
        # 回购注销事件是“分红融资比（含回购注销）”的输入。此前只在 CLI
        # 手动刷新，自动更新从未调度，指标会随回购进度持续过期。
        buyback_step = report_step("buyback", self._refresh_buyback())
        buyback_changed_codes = list(buyback_step.get("changed_codes", []))
        # 融资事件同样影响 cumulative_financing_amount / 分红融资比，必须在
        # 指标快照重算之前刷新；旧位置（统计域之后）会让新融资数据迟到一轮。
        funding_step = report_step("funding", self._refresh_funding())
        self._resolve_legal_empty_funding_missing()
        funding_changed_codes = list(funding_step.get("changed_codes", []))
        # 国债曲线变化会改变所有股票快照中的 div_yield_spread_*。旧流程在
        # 指标重算之后才刷曲线，利差字段会整轮陈旧；现在提前并触发全量重算。
        treasury_step = report_step("treasury_curve", self._refresh_treasury_curve())
        treasury_changed_codes = []
        if treasury_step.get("curve_changed"):
            try:
                treasury_changed_codes = [
                    row["stock_code"]
                    for row in self.duck.read_query(
                        "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
                    )
                ]
            except Exception as error:
                logger.warning("查询国债利差重算范围失败: %s", error)
        # 6. 重试失败任务（先清理已达标的历史冗余与无重试路径的死循环条目）。
        #    旧位置在统计域之后：重试成功的价格/财务/分红/融资/曲线数据会
        #    再等一整轮才进入快照。现在移到指标重算之前，并把影响快照的
        #    重试成功代码并入本轮重算集合。
        retry_recompute_codes: list[str] = []
        self._cleanup_unretryable_tasks()
        self._cleanup_completed_announcement_retries()
        self._resolve_complete_missing_records()
        if check_report["retry_tasks"]:
            expected_date = self._latest_expected_trading_date(
                datetime.now().strftime("%Y-%m-%d")
            )
            self._cleanup_redundant_retries(expected_date)
            refreshed_tasks = self._check_retry_tasks()
            if refreshed_tasks:
                retries_step = report_step(
                    "retries", self._retry_failed_tasks(refreshed_tasks),
                )
                retry_recompute_codes = list(
                    retries_step.get("recompute_codes", [])
                )

        if share_capital_changed:
            # 股本/上市名单变化可能影响多只股票的市场类指标；保守全量重算。
            # 该路径低频，且与自动更新同锁，不会再现"重算与抓取并发"的竞争。
            from app.core.indicators.calculator import IndicatorCalculator

            snapshot_step = IndicatorCalculator(
                duck=self.duck, sqlite=self.sqlite,
            ).compute_snapshot_for_all(progress_cb=indicator_progress)
            report_step("indicators", snapshot_step)
            if snapshot_step["status"] != "success":
                snapshot_step["reason"] = "price update retained; snapshot publication not ready"
        elif financial_step["status"] in {"success", "partial"} or retry_recompute_codes:
            from app.core.indicators.calculator import IndicatorCalculator

            # 2026-08-28 提速计划：财报只刷新了少数公告股票，只增量重算
            # "价格刚变化 + 财报刚刷新 + 快照日期落后"的并集，不再因为
            # 几只股票发了新财报就全量重算 5,500+ 只。partial 也照常为
            # 已成功写入的股票重算快照；失败股票保留上一代快照。
            stale_snapshot_codes = self._stale_snapshot_codes()
            refreshed_financial_codes = list(
                financials.get("succeeded_codes", []) if financials else []
            )
            codes_to_compute = list(dict.fromkeys([
                *updated_price_codes,
                *refreshed_financial_codes,
                *detail_backfilled_codes,
                *buyback_changed_codes,
                *funding_changed_codes,
                *retry_recompute_codes,
                *stale_snapshot_codes,
            ]))
            if codes_to_compute:
                snapshot_step = IndicatorCalculator(
                    duck=self.duck, sqlite=self.sqlite,
                ).compute_snapshot_for_codes(
                    codes_to_compute, progress_cb=indicator_progress,
                )
                report_step("indicators", snapshot_step)
            if treasury_changed_codes:
                # 国债曲线变化只影响 ttm_dividend_yield / div_yield_spread_*，
                # 无需对全市场完整重算 PE/PB/成长/技术指标；专用批量刷新
                # 在正式库为秒级，取代旧的约 12 分钟全量重算。
                treasury_step = IndicatorCalculator(
                    duck=self.duck, sqlite=self.sqlite,
                ).refresh_treasury_spreads(treasury_changed_codes)
                if codes_to_compute:
                    report_step(
                        "indicators",
                        {**treasury_step, "full_recompute": snapshot_step},
                    )
                else:
                    report_step("indicators", treasury_step)

        # 3. 业务概览低频域（reports/67 独立域）：仅当自动集成启用且间隔到期
        #    时执行；失败保留旧值并进入独立 retry/missing，绝不阻断价格/财务。
        report_step("business_overview", self._refresh_business_overview(detail_cb=detail_cb))

        # 4. 国债曲线已提前到指标快照重算之前执行。

        # 5. 历史股本链 + 统计域（reports/68 P4）：有界续传；失败不阻断其他步骤
        report_step("capital_history", self._refresh_capital_history(detail_cb=detail_cb))
        report_step(
            "research_statistics",
            self._refresh_research_statistics(
                parallel_workers=max(1, int(self.research_statistics_parallel_workers)),
                detail_cb=detail_cb,
            ),
        )

        # 5.1 融资事件/失败重试已提前到指标快照重算之前执行；这里只保留
        #     指数估值每日 1 次低频域。失败保留旧值并进入独立 retry/missing。
        report_step("index_valuation", self._refresh_index_valuation())

        report["status"] = aggregate_job_status(report["steps"])
        report["finished_at"] = datetime.now(UTC).isoformat()

        logger.info(f"增量更新完成: {report['status']}")
        return report

    def _refresh_business_overview(
        self,
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """低频业务概览自动集成入口（最小安全）。

        仅当 update.business_overview_auto_enabled=true 且刷新间隔到期时
        才执行；否则返回 skipped，不产生任何网络请求。任何异常都被捕获，
        绝不让业务概览源失败阻断价格/财务增量更新。
        """
        try:
            from app.core.business import BusinessOverviewUpdater

            def progress(code: str, outcome: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                detail_cb("business_overview", {
                    "step": "business_overview",
                    "label": "业务概览",
                    "done": int(outcome.get("done") or 0),
                    "total": int(outcome.get("total") or 0),
                    "current": code,
                })

            return BusinessOverviewUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_if_due(progress_cb=progress)
        except Exception as error:
            logger.warning("业务概览刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_treasury_curve(self) -> dict[str, Any]:
        """低频国债曲线自动集成入口（最小安全）。

        每日检查当日曲线并补齐近 30 天缺失的关键期限；任何异常都被捕获，
        绝不让国债源失败阻断价格/财务/readiness 增量更新。

        返回 curve_changed，调用方据此重算所有含 div_yield_spread_* 的快照。
        """
        try:
            before = self._treasury_curve_fingerprint()
            from app.core.treasury import TreasuryCurveUpdater

            report = TreasuryCurveUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_if_due()
            after = self._treasury_curve_fingerprint()
            curve_changed = before != after
            return {**report, "curve_changed": curve_changed}
        except Exception as error:
            logger.warning("国债曲线刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _treasury_curve_fingerprint(self) -> str:
        """Cheap content fingerprint of the whole treasury curve table.

        收益率利差快照可能引用任意历史日期的曲线点（停牌/价格滞后股票尤其
        明显），所以不能只比较行数与最大日期；任意已有日期的收益率被
        upsert 修正也必须触发 div_yield_spread_* 重算。
        """
        try:
            rows = self.duck.read_query(
                """SELECT COALESCE(md5(string_agg(
                       CAST(curve_date AS VARCHAR) || ':' ||
                       CAST(tenor_years AS VARCHAR) || ':' ||
                       CAST(yield_pct AS VARCHAR),
                       '|' ORDER BY curve_date, tenor_years
                   )), '') AS fp
                   FROM treasury_yield_curve"""
            )
            return str(rows[0]["fp"]) if rows else ""
        except Exception as error:
            logger.warning("计算国债曲线指纹失败: %s", error)
            return ""

    def _refresh_capital_history(
        self,
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """历史股本链有界续传（P4）：每轮最多 20 只上市股票，按代码序。

        自选优先；失败保留旧值 + retry/missing；绝不阻断价格/财务/readiness。
        """
        try:
            from app.core.capital import CapitalHistoryUpdater

            def progress(code: str, outcome: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                detail_cb("capital_history", {
                    "step": "capital_history",
                    "label": "历史股本",
                    "done": int(outcome.get("done") or 0),
                    "total": int(outcome.get("total") or 0),
                    "current": code,
                })

            return CapitalHistoryUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).update_all(max_stocks=20, progress_cb=progress)
        except Exception as error:
            logger.warning("历史股本链刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_research_statistics(
        self,
        *,
        parallel_workers: int = 1,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """历史统计域重建（P4）：仅当股本链或价格/财务/曲线输入变化时执行。

        以输入指纹为判据避免每轮全量重建；任何异常均不阻断其他步骤。
        """
        try:
            from app.core.statistics import StatisticsBuilder
            builder = StatisticsBuilder(duck=self.duck, sqlite=self.sqlite)
            fingerprint = builder._input_fingerprint()
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = 'research_statistics_fingerprint'"
            )
            if rows and rows[0].get("value") == fingerprint:
                return {"status": "skipped", "reason": "fingerprint_unchanged"}

            def stats_progress(code: str, outcome: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                detail_cb("research_statistics", {
                    "step": "research_statistics",
                    "label": "历史统计",
                    "done": int(outcome.get("done") or 0),
                    "total": int(outcome.get("total") or 0),
                    "current": code,
                })

            report = builder.rebuild_incremental(
                parallel=parallel_workers,
                progress_cb=stats_progress,
            )
            # P4-10 修复（reports/73）：仅全部成功才持久化指纹；
            # partial（部分股票失败）不落指纹，下一轮自动重试失败股。
            if report["status"] == "success":
                with self.sqlite.transaction() as conn:
                    conn.execute(
                        """INSERT INTO data_refresh_state (key, value, updated_at)
                           VALUES ('research_statistics_fingerprint', ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                             value=excluded.value, updated_at=excluded.updated_at""",
                        [fingerprint, datetime.now(UTC).isoformat()],
                    )
            return report
        except Exception as error:
            logger.warning("历史统计域重建失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_funding(self) -> dict[str, Any]:
        """融资事件低频域有界续传（数据补全 2026-08-25）。

        每轮最多 update.funding_max_stocks_per_run（默认 100）只未覆盖上市股票，
        批 50 + 冷却 30s（东财 F10 安全组合）；失败保留旧值 + retry/missing，
        绝不阻断价格/财务/readiness。历史事件一次补齐后日常仅新股增量。

        返回 changed_codes 供调用方重算 cumulative_financing_amount /
        dividend_financing_ratio_pct 快照。
        """
        try:
            before = self.duck.read_query(
                """SELECT stock_code, COALESCE(SUM(raise_funds), 0) AS amount
                   FROM funding_events GROUP BY stock_code"""
            )
            before_by_code = {row["stock_code"]: row["amount"] for row in before}
            from app.core.funding import FundingUpdater

            report = FundingUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_if_due()
            after = self.duck.read_query(
                """SELECT stock_code, COALESCE(SUM(raise_funds), 0) AS amount
                   FROM funding_events GROUP BY stock_code"""
            )
            after_by_code = {row["stock_code"]: row["amount"] for row in after}
            changed_codes = sorted({
                code for code in set(before_by_code) | set(after_by_code)
                if before_by_code.get(code) != after_by_code.get(code)
            })
            return {**report, "changed_codes": changed_codes}
        except Exception as error:
            logger.warning("融资事件刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_index_valuation(self) -> dict[str, Any]:
        """指数估值低频域每日更新（数据补全 2026-08-25）。

        每日最多 1 次（UTC+8 节流，避免频繁重启重复请求第三方源）；
        主源失败保留旧值并登记 retry，绝不阻断价格/财务/readiness。
        """
        try:
            from app.core.index_valuation import IndexValuationUpdater
            return IndexValuationUpdater(
                duck=self.duck, sqlite=self.sqlite,
            ).refresh_if_due()
        except Exception as error:
            logger.warning("指数估值刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _stale_snapshot_codes(self) -> list[str]:
        """Listed stocks whose snapshot is behind raw price OR complete financials.

        财报中报季 catch-up 可能 partial：已成功写入 Q2 三表的股票即使
        价格日期没变，也必须重算快照，否则市值/TTM 仍停留在 Q1。
        """
        try:
            rows = self.duck.read_query(
                """WITH raw AS (
                       SELECT stock_code, MAX(trade_date) AS latest
                       FROM price_daily_raw GROUP BY stock_code
                   ),
                   complete_financials AS (
                       SELECT bs.stock_code, MAX(bs.report_date) AS latest
                       FROM balance_sheet bs
                       JOIN income_statement ic
                         ON ic.stock_code = bs.stock_code
                        AND ic.report_date = bs.report_date
                       JOIN cash_flow cf
                         ON cf.stock_code = bs.stock_code
                        AND cf.report_date = bs.report_date
                       WHERE bs.total_assets IS NOT NULL
                         AND bs.total_liabilities IS NOT NULL
                         AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
                         AND ic.revenue IS NOT NULL
                         AND ic.parent_net_profit IS NOT NULL
                         AND cf.cf_from_operating IS NOT NULL
                       GROUP BY bs.stock_code
                   ),
                   stale AS (
                       SELECT snap.stock_code
                       FROM indicator_snapshot snap
                       JOIN raw ON raw.stock_code = snap.stock_code
                       JOIN stock_meta m ON m.stock_code = snap.stock_code
                       WHERE m.is_listed IS TRUE
                         AND snap.latest_price_date != raw.latest
                       UNION
                       SELECT snap.stock_code
                       FROM indicator_snapshot snap
                       JOIN complete_financials fin
                         ON fin.stock_code = snap.stock_code
                       WHERE fin.latest > snap.report_date
                   )
                   SELECT DISTINCT stock_code FROM stale ORDER BY stock_code"""
            )
            return [row["stock_code"] for row in rows]
        except Exception as error:
            logger.warning("查询过期指标快照失败: %s", error)
            return []

    def _share_capital_fingerprint(self) -> str:
        """Return a cheap fingerprint of the share-capital pool state.

        用于检测 universe 刷新是否实际改变股本/上市名单，从而决定是否
        需要重算市值类指标快照。
        """
        rows = self.duck.read_query(
            """SELECT COALESCE(md5(CAST(string_agg(
                   stock_code || ':' ||
                   COALESCE(CAST(total_shares AS VARCHAR), '') || ':' ||
                   COALESCE(CAST(circ_shares AS VARCHAR), '') || ':' ||
                   CAST(is_listed AS VARCHAR), '|') AS VARCHAR)), '') AS fp
               FROM stock_meta"""
        )
        return rows[0]["fp"] if rows else ""

    def _listing_info_gap_codes(self) -> list[str]:
        """Return listed stocks missing metadata needed by the publish gate."""
        rows = self.duck.read_query(
            """SELECT stock_code FROM stock_meta
               WHERE is_listed IS TRUE
                 AND (listing_date IS NULL OR is_st IS NULL OR is_suspended IS NULL)
               ORDER BY stock_code"""
        )
        return [row["stock_code"] for row in rows]

    def _refresh_universe_metadata(self) -> dict[str, Any]:
        """PRD §7.7 第 4 项: 刷新股票池、上市/ST/停牌/股本与 CSRC 行业。

        通过 DataInitializer 的 canonical 写入路径执行：
        - stock_list: 上市名单（新股加入、退市标记）
        - listing_info: 上市状态/股本元数据
        - csrc_industry: 证监会行业分类（低频：默认 30 天一次）
        各子步骤独立失败降级，不影响价格/财务更新。
        """
        from app.core.init import DataInitializer

        initializer = DataInitializer(duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr)
        steps: dict[str, Any] = {}

        if self._refresh_due("stock_list_last_refresh", self.universe_refresh_interval_days):
            try:
                steps["stock_list"] = initializer._fetch_stock_universe()
                if steps["stock_list"].get("status") == "success":
                    self._mark_refreshed("stock_list_last_refresh")
            except Exception as error:
                logger.warning("股票池刷新失败: %s", error)
                steps["stock_list"] = {"status": "failed", "error": str(error)}
        else:
            steps["stock_list"] = {
                "status": "skipped",
                "reason": "refreshed_within_interval",
                "interval_days": self.universe_refresh_interval_days,
            }

        listing_due = self._refresh_due(
            "listing_info_last_refresh", self.universe_refresh_interval_days
        )
        # 新股今天刚进入 stock_list 时，listing_info 可能还在日内节流窗口；
        # 不能跳过，否则 listing_date/ST/停牌/股本缺失会卡住快照发布。
        gap_codes = self._listing_info_gap_codes()
        if listing_due or gap_codes:
            try:
                steps["listing_info"] = initializer._fetch_listing_info(
                    stock_codes=gap_codes if not listing_due and gap_codes else None
                )
                if listing_due and steps["listing_info"].get("status") == "success":
                    self._mark_refreshed("listing_info_last_refresh")
            except Exception as error:
                logger.warning("上市状态刷新失败: %s", error)
                steps["listing_info"] = {"status": "failed", "error": str(error)}
        else:
            steps["listing_info"] = {
                "status": "skipped",
                "reason": "refreshed_within_interval",
                "interval_days": self.universe_refresh_interval_days,
            }

        csrc_full_due = self._csrc_refresh_due()
        csrc_null_gap = self._csrc_null_gap_exists()
        if csrc_full_due or csrc_null_gap:
            try:
                steps["csrc_industry"] = initializer._fetch_csrc_industry(
                    full_refresh=csrc_full_due,
                )
                if steps["csrc_industry"].get("status") in {"success", "partial"}:
                    self._mark_refreshed("csrc_industry_last_refresh")
            except Exception as error:
                logger.warning("CSRC 行业刷新失败: %s", error)
                steps["csrc_industry"] = {"status": "failed", "error": str(error)}
        else:
            steps["csrc_industry"] = {
                "status": "skipped",
                "reason": "refreshed_within_interval_and_no_unrecorded_null_gap",
                "interval_days": self.csrc_refresh_interval_days,
            }

        statuses = [step.get("status") for step in steps.values()]
        # skipped 是节流命中，不是失败。只要没有 failed/partial 子步骤，
        # 本轮 universe 就是正常完成，避免页面长期显示误导性的 partial。
        if all(status == "skipped" for status in statuses):
            status = "skipped"
        elif all(status in {"success", "skipped"} for status in statuses):
            status = "success"
        elif any(status == "failed" for status in statuses):
            status = "partial"
        else:
            status = "partial"
        return {
            "status": status,
            "steps": steps,
        }

    def _csrc_refresh_due(self) -> bool:
        """Return whether the full CSRC refresh interval has elapsed."""
        return self._refresh_due("csrc_industry_last_refresh", self.csrc_refresh_interval_days)

    def _csrc_null_gap_exists(self) -> bool:
        """Return true when listed stocks still lack CSRC and are not recorded missing.

        新股上市后 csrc_l1/csrc_l2 为空时，不能等 30 天全量刷新才补；
        已由 CNINFO 确认“源无分类”的股票记入 missing_list，不再每轮重试。
        """
        try:
            null_rows = self.duck.read_query(
                """SELECT COUNT(*) AS c FROM stock_meta
                   WHERE is_listed IS TRUE AND csrc_l1 IS NULL"""
            )
            missing_rows = self.sqlite.query(
                """SELECT COUNT(*) AS c FROM missing_list
                   WHERE field_name = 'csrc_industry' AND resolved_at IS NULL"""
            )
        except Exception as error:
            logger.warning("检查 CSRC 未记录缺口失败: %s", error)
            return False
        null_count = int(null_rows[0]["c"]) if null_rows else 0
        missing_count = int(missing_rows[0]["c"]) if missing_rows else 0
        return null_count > missing_count

    def _refresh_due(self, key: str, interval_days: int) -> bool:
        """Return whether a data-domain refresh marker is older than the interval."""
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [key],
        )
        if not rows:
            return True
        raw = rows[0].get("value")
        try:
            last_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return True
        return (datetime.now(UTC).date() - last_date).days >= interval_days

    def _mark_csrc_refreshed(self) -> None:
        """Persist the CSRC refresh date so later runs skip the full scan."""
        self._mark_refreshed("csrc_industry_last_refresh")

    def _mark_refreshed(self, key: str) -> None:
        """Persist a data-domain refresh timestamp (data_refresh_state)."""
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [key, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()],
            )

    def _check_new_trading_days(self) -> list[str]:
        """检查是否有本地没有的新交易日"""
        # 从适配器获取最新交易日历
        result = self.adapter_mgr.fetch(FetchRequest(data_type="trading_dates"))
        if result.metadata.error or not result.data:
            logger.warning(f"获取交易日历失败: {result.metadata.error}")
            return []

        remote_dates = {r["trade_date"] for r in result.data}

        # 获取本地已有的交易日
        try:
            local_rows = self.sqlite.query("SELECT trade_date FROM trading_dates")
            local_dates = {r["trade_date"] for r in local_rows}
        except Exception:
            local_dates = set()

        new_dates = sorted(remote_dates - local_dates)
        if new_dates:
            logger.info(f"发现 {len(new_dates)} 个新交易日: {new_dates[-5:]}")

        return new_dates

    def _get_latest_local_price_date(self) -> str | None:
        """获取本地最新价格日期"""
        try:
            rows = self.duck.read_query(
                "SELECT MAX(trade_date) as latest FROM price_daily_raw"
            )
            if rows and rows[0]["latest"]:
                return str(rows[0]["latest"])
        except Exception as e:
            logger.warning(f"获取最新价格日期失败: {e}")
        return None

    def _is_b_share_stock(self, stock_code: str) -> bool:
        """B 股没有免费财务主源，按代码段或名称后缀识别。

        2026-08-30：日志发现 201872 这类深市 B 股不在 200/900 前缀规则内，
        改为代码段 + stock_meta 名称后缀双重识别。
        """
        if stock_code.startswith(("200", "201", "900")):
            return True
        try:
            rows = self.duck.read_query(
                "SELECT name FROM stock_meta WHERE stock_code = ? "
                "AND (name LIKE '%B' OR name LIKE '%B股')",
                [stock_code],
            )
            return bool(rows)
        except Exception:
            return False

    @staticmethod
    def _current_expected_financial_period(now: datetime | None = None) -> str:
        """Return the report period that should already be published today.

        半年报季（8-10月）期望 06-30；三季报季（11月后）期望 09-30；
        一季报季（5-7月）期望 03-31；其余时间期望上一年年报 12-31。
        """
        current = now or datetime.now(UTC)
        year = current.year
        if current.month >= 11:
            return f"{year}-09-30"
        if current.month >= 8:
            return f"{year}-06-30"
        if current.month >= 5:
            return f"{year}-03-31"
        return f"{year - 1}-12-31"

    def _get_latest_financial_report_date(self, stock_code: str, data_type: str) -> str | None:
        """获取本地最新财务报告期（balance_sheet/income_statement/cash_flow）"""
        table = data_type if data_type in {"balance_sheet", "income_statement", "cash_flow"} else None
        if table is None:
            return None
        try:
            rows = self.duck.read_query(
                f"SELECT MAX(report_date) as latest FROM {table} WHERE stock_code = ?",
                [stock_code],
            )
            if rows and rows[0]["latest"]:
                return str(rows[0]["latest"])
        except Exception as e:
            logger.warning(f"获取 {table} 最新报告期失败: {e}")
        return None

    def _announcement_cursor(self) -> datetime | None:
        """Last successfully persisted announcement-check cursor (UTC)."""
        try:
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = ?",
                [_ANNOUNCEMENT_CHECK_CURSOR_KEY],
            )
            if not rows:
                return None
            value = rows[0].get("value")
            if not value:
                return None
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (ValueError, TypeError):
            return None

    def _save_announcement_cursor(self, at: datetime | None = None) -> None:
        """Advance the durable announcement-check cursor after a clean remote check."""
        cursor = (at or datetime.now(UTC)).isoformat()
        self.sqlite.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                              updated_at=excluded.updated_at""",
            [_ANNOUNCEMENT_CHECK_CURSOR_KEY, cursor, datetime.now(UTC).isoformat()],
        )

    def _check_new_announcements(
        self,
        *,
        persist: bool = True,
        lookback_days: int = 3,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Compare remote announcement IDs without mutating during a check-only run.

        全市场按日期段批量查询（PRD §7.7），替代逐股轮询。2026-08-28 修复：
        - CNINFO 每页固定 30 条；财务公告按半年报/一季报/三季报/年报类别
          分别精确查询，不再依赖噪声极大的全市场全文列表。
        - 持久化检查游标后，日常只查游标之后的窗口；游标缺失时回看
          `announcement_catchup_days`（默认 45 天，覆盖整个中报季）。
        - 非财务公告只查最近 `announcement_lookback_days` 天用于登记。
        """
        now = datetime.now(UTC)
        end_date = now.strftime("%Y-%m-%d")
        general_lookback = (
            lookback_days if lookback_days else self.announcement_lookback_days
        )
        general_start = (now - timedelta(days=general_lookback)).strftime("%Y-%m-%d")
        cursor = self._announcement_cursor() if persist else None
        if cursor is not None:
            financial_start = (cursor - timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            financial_window = (
                getattr(self, "announcement_catchup_days", 45)
                if persist
                else general_lookback
            )
            financial_start = (now - timedelta(days=financial_window)).strftime("%Y-%m-%d")
        financial_max_pages = getattr(self, "announcement_financial_max_pages", 450)
        general_max_pages = getattr(self, "announcement_general_max_pages", 40)

        category_labels = {
            "semi_annual": "半年报", "q1": "一季报", "q3": "三季报",
            "annual": "年报",
        }

        def fetch_items(
            category: str | None, start_date: str, max_pages: int,
        ) -> tuple[list[dict[str, Any]], str | None]:
            label = category_labels.get(category or "", "近期公告")

            def page_progress(info: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                total_pages = int(info.get("total_pages") or 0)
                detail_cb("announcements", {
                    "step": "announcements",
                    "label": f"公告检查·{label}",
                    "done": int(info.get("page") or 0),
                    "total": total_pages,
                    "current": f"第{int(info.get('page') or 0)}页/{total_pages or '?'}",
                })

            try:
                result = self.adapter_mgr.fetch(FetchRequest(
                    data_type="announcements",
                    start_date=start_date,
                    end_date=end_date,
                    extra_params={
                        "category": category,
                        "page_size": 30,
                        "max_pages": max_pages,
                        "progress_cb": page_progress,
                    },
                ))
            except Exception as error:
                return [], str(error)
            return list(result.data or []), result.metadata.error or None

        all_items: list[dict[str, Any]] = []
        errors: list[str] = []
        last_error_source = "cninfo"
        for category in _ANNOUNCEMENT_FINANCIAL_CATEGORIES:
            items, error = fetch_items(
                category, financial_start, financial_max_pages,
            )
            all_items.extend(items)
            if error:
                errors.append(f"{category}: {error}")
        general_items, general_error = fetch_items(
            None, general_start, general_max_pages,
        )
        all_items.extend(general_items)
        if general_error:
            errors.append(f"general: {general_error}")

        # 类别查询与全文查询可能返回同一公告；按公告 ID 去重。
        unique_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in all_items:
            announcement_id = item.get("announcement_id")
            stock_code = item.get("stock_code")
            if not announcement_id or not stock_code:
                continue
            if announcement_id in seen_ids:
                continue
            seen_ids.add(announcement_id)
            unique_items.append(item)

        if errors and persist:
            with contextlib.suppress(Exception):
                self._record_failure(
                    "", "announcements", last_error_source, "; ".join(errors),
                )

        if not unique_items and errors:
            return {
                "status": "unavailable",
                "checked_remote": True,
                "affected_stock_codes": [],
                "affected_announcements": {},
                "all_new_announcements": {},
                "errors": errors,
            }

        # 批量查询本地登记表，避免每一条公告一次 SQL。
        existing_ids: set[str] = set()
        all_ids = [item["announcement_id"] for item in unique_items]
        for offset in range(0, len(all_ids), 400):
            chunk = all_ids[offset:offset + 400]
            placeholders = ", ".join("?" for _ in chunk)
            try:
                rows = self.sqlite.query(
                    f"SELECT announcement_id FROM announcement_registry "
                    f"WHERE announcement_id IN ({placeholders})",
                    chunk,
                )
                existing_ids.update(str(row["announcement_id"]) for row in rows)
            except Exception:
                # 老库缺少该列时退回逐条查询，保证发现功能仍可用。
                existing_ids.update(
                    str(item["announcement_id"]) for item in unique_items
                    if self.sqlite.query(
                        "SELECT 1 FROM announcement_registry WHERE announcement_id = ?",
                        [item["announcement_id"]],
                    )
                )

        affected: set[str] = set()
        affected_announcements: dict[str, list[dict[str, Any]]] = {}
        for item in unique_items:
            announcement_id = str(item["announcement_id"])
            stock_code = str(item["stock_code"])
            if announcement_id in existing_ids:
                continue
            affected.add(stock_code)
            affected_announcements.setdefault(stock_code, []).append(item)

        # 按标题分类，只保留财务类公告触发刷新
        financial_codes: set[str] = set()
        financial_announcements: dict[str, list[dict[str, Any]]] = {}
        for code in sorted(affected):
            financial_items = [
                item for item in affected_announcements[code]
                if classify_announcement(item.get("title")) == "financial"
            ]
            if financial_items:
                financial_codes.add(code)
                financial_announcements[code] = financial_items

        # 游标不在这里推进：只有当本轮发现的财务公告全部成功刷新/入册后，
        # 才允许把“已检查到”的边界前移（见 _run_incremental_update_flow 末尾）。
        # 若中途失败，下一轮会重新发现未入册公告，断点不丢失。
        return {
            "status": "available" if not errors else "partial",
            "checked_remote": True,
            "affected_stock_codes": sorted(financial_codes),
            "affected_announcements": financial_announcements,
            "all_new_announcements": {
                code: affected_announcements[code] for code in sorted(affected)
            },
            "errors": errors,
        }

    def _mark_announcements_seen(self, stock_code: str, announcements: list[dict[str, Any]]) -> None:
        """Register filings only after their matching financial refresh persisted."""
        with self.sqlite.transaction() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO announcement_registry
                   (announcement_id, stock_code, announcement_time, title)
                   VALUES (?, ?, ?, ?)""",
                [
                    (
                        item["announcement_id"], stock_code,
                        str(item.get("announcement_time") or ""), item.get("title"),
                    )
                    for item in announcements
                ],
            )

    def _pending_announcement_items(self) -> list[dict[str, Any]]:
        """Return durable pending financial-announcement markers from retry_list."""
        try:
            rows = self.sqlite.query(
                "SELECT stock_code, extra_json FROM retry_list "
                "WHERE data_type = 'announcements'"
            )
        except Exception:
            return []
        items: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("stock_code") or "").strip()
            if not code:
                continue
            ids: list[str] = []
            try:
                ids = json.loads(row.get("extra_json") or "{}").get("announcement_ids", [])
            except (json.JSONDecodeError, TypeError):
                ids = []
            if not ids:
                ids = [f"pending-{code}"]
            for announcement_id in ids:
                items.append({
                    "stock_code": code,
                    "announcement_id": str(announcement_id),
                    "title": "pending financial announcement",
                    "announcement_time": "",
                })
        return items

    def _resolve_announcement_retries(self, stock_code: str) -> None:
        """Remove announcement pending markers once the stock's refresh succeeded."""
        try:
            self.sqlite.execute(
                "DELETE FROM retry_list WHERE stock_code = ? AND data_type = 'announcements'",
                [stock_code],
            )
        except Exception as error:
            logger.warning("清理公告 retry %s 失败: %s", stock_code, error)

    def _resolve_announcement_source_retries(self) -> None:
        """Remove the global CNINFO outage marker once a later check succeeds.

        Source-level failures are recorded with stock_code='' so per-stock
        pending markers are never conflated; without this cleanup the global
        marker would keep diagnose 永久显示 retry_count=1 even after CNINFO
        recovers.
        """
        try:
            self.sqlite.execute(
                "DELETE FROM retry_list WHERE stock_code = '' AND data_type = 'announcements'"
            )
        except Exception as error:
            logger.warning("清理公告源故障 retry 失败: %s", error)

    def _fetch_financial_trio(
        self, stock_code: str,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]:
        """Fetch latest rows for one stock's three statements without writing."""
        data_types = ("balance_sheet", "income_statement", "cash_flow")
        fetched: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
        outcomes: list[dict[str, Any]] = []
        if self._is_b_share_stock(stock_code):
            return [
                {
                    "status": "failed", "data_type": data_type,
                    "error": "B-share financial source unsupported",
                }
                for data_type in data_types
            ], fetched
        for data_type in data_types:
            try:
                latest_local = self._get_latest_financial_report_date(
                    stock_code, data_type,
                )
                result = self.adapter_mgr.fetch(FetchRequest(
                    data_type=data_type,
                    stock_codes=[stock_code],
                    extra_params={"num": "1"},
                ))
                if result.metadata.error or not result.data:
                    outcomes.append({
                        "status": "failed", "data_type": data_type,
                        "error": result.metadata.error or "empty result",
                    })
                    continue
                new_rows = [
                    row for row in result.data
                    if not latest_local or str(row.get("report_date") or "") > latest_local
                ]
                if not new_rows:
                    outcomes.append({
                        "status": "success", "data_type": data_type,
                        "skipped": True, "latest_local": latest_local,
                    })
                    continue
                fetched[data_type] = (result, new_rows)
                outcomes.append({
                    "status": "success", "data_type": data_type, "skipped": False,
                })
            except Exception as error:
                outcomes.append({
                    "status": "failed", "data_type": data_type, "error": str(error),
                })
        return outcomes, fetched

    @staticmethod
    def _persist_fetched_trio_in_connection(
        initializer: Any,
        conn: Any,
        stock_code: str,
        fetched: dict[str, tuple[Any, list[dict[str, Any]]]],
    ) -> None:
        """Write fetched financial rows + lineage for one stock into conn."""
        for data_type, (_, new_rows) in fetched.items():
            for row in new_rows:
                initializer._upsert_financial_row(conn, data_type, stock_code, row)
        for data_type, (result, new_rows) in fetched.items():
            batch_id = initializer._record_batch_in_connection(
                conn, result, data_type, len(new_rows),
            )
            initializer._record_field_audit_in_connection(
                conn, result, new_rows, stock_code, "report_date", batch_id,
            )

    def _persist_financial_trio(
        self, stock_code: str, outcomes: list[dict[str, Any]],
        fetched: dict[str, tuple[Any, list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """Persist one stock's fetched trio in its own transaction."""
        if not fetched:
            return outcomes
        from app.core.init import DataInitializer

        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        try:
            with self.duck.transaction() as conn:
                self._persist_fetched_trio_in_connection(
                    initializer, conn, stock_code, fetched,
                )
        except Exception as error:
            for outcome in outcomes:
                if outcome.get("status") == "success" and not outcome.get("skipped"):
                    outcome["status"] = "failed"
                    outcome["error"] = str(error)
            return outcomes
        balance = fetched.get("balance_sheet")
        if balance is not None:
            try:
                initializer._record_missing_financial_sector_fields(
                    stock_code, balance[1],
                )
            except Exception:
                logger.debug("记录 %s 金融业缺失字段失败（非致命）", stock_code)
        return outcomes

    def _persist_financial_batch(
        self, items: list[tuple[str, list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]],
    ) -> None:
        """Persist up to batch_size stocks in one transaction; fallback per stock."""
        from app.core.init import DataInitializer

        if not items:
            return
        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        try:
            with self.duck.transaction() as conn:
                for code, _, fetched in items:
                    self._persist_fetched_trio_in_connection(
                        initializer, conn, code, fetched,
                    )
        except Exception as error:
            logger.warning(
                "财务批量写入失败，退化为逐股单事务: %s", error,
            )
            for code, outcomes, fetched in items:
                self._persist_financial_trio(code, outcomes, fetched)
            return
        for code, _, fetched in items:
            balance = fetched.get("balance_sheet")
            if balance is None:
                continue
            try:
                initializer._record_missing_financial_sector_fields(
                    code, balance[1],
                )
            except Exception:
                logger.debug("记录 %s 金融业缺失字段失败（非致命）", code)

    def refetch_financial_trio(self, stock_code: str) -> list[dict[str, Any]]:
        """Refresh one stock's three statements (fetch + single transaction)."""
        outcomes, fetched = self._fetch_financial_trio(stock_code)
        return self._persist_financial_trio(stock_code, outcomes, fetched)

    def _financial_detail_gap_codes(self) -> list[str]:
        """Return stocks whose latest financial rows still miss detail line items.

        核心字段（total_assets/revenue/cf_from_operating）齐备但明细字段为空，
        说明该报告期来自旧的“最小核心集”抓取或旧版解析器。这些股票不阻断
        readiness，但详情页会显示缺口；本方法为有界续传提供队列。
        """
        try:
            rows = self.duck.read_query(
                """
                WITH latest_bs AS (
                    SELECT stock_code, report_date, total_assets,
                           monetary_funds, paid_in_capital, undistributed_profit
                    FROM balance_sheet
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code ORDER BY report_date DESC
                    ) = 1
                ), latest_ic AS (
                    SELECT stock_code, report_date, revenue,
                           cost_of_revenue, interest_income, interest_expense,
                           selling_expenses, administrative_expenses,
                           taxes_and_surcharges, investment_income
                    FROM income_statement
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code ORDER BY report_date DESC
                    ) = 1
                ), latest_cf AS (
                    SELECT stock_code, report_date, cf_from_operating,
                           cash_received_sales, taxes_refunded, cash_paid_goods,
                           cash_paid_employees, cash_paid_taxes,
                           total_operating_cf_in, total_operating_cf_out
                    FROM cash_flow
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code ORDER BY report_date DESC
                    ) = 1
                )
                SELECT m.stock_code
                FROM stock_meta m
                LEFT JOIN latest_bs bs ON bs.stock_code = m.stock_code
                LEFT JOIN latest_ic ic ON ic.stock_code = m.stock_code
                LEFT JOIN latest_cf cf ON cf.stock_code = m.stock_code
                WHERE m.is_listed IS TRUE
                  AND NOT (
                      m.stock_code LIKE '200%'
                      OR m.stock_code LIKE '201%'
                      OR m.stock_code LIKE '900%'
                      OR m.name LIKE '%B'
                      OR m.name LIKE '%B股'
                  )
                  AND (
                      -- 不能用单一字段（如 cost_of_revenue）探测“旧最小核心集”：
                      -- 银行/券商/保险的利润表本来就没有营业成本，单一探测会
                      -- 让这些股票回填成功后仍然留在缺口队列头部，挤占每轮
                      -- 100 个名额，后面的股票永远排不到。改为“行业通用候选
                      -- 字段组全部为空才算旧行”：正常回填成功的股票至少会写入
                      -- paid_in_capital/undistributed_profit、利息或费用类利润
                      -- 字段、职工/税费现金字段之一。
                      (
                          bs.total_assets IS NOT NULL
                          AND COALESCE(
                              bs.monetary_funds, bs.paid_in_capital,
                              bs.undistributed_profit,
                          ) IS NULL
                      )
                      OR (
                          ic.revenue IS NOT NULL
                          AND COALESCE(
                              ic.cost_of_revenue, ic.interest_income,
                              ic.interest_expense, ic.selling_expenses,
                              ic.administrative_expenses,
                              ic.taxes_and_surcharges, ic.investment_income,
                          ) IS NULL
                      )
                      OR (
                          cf.cf_from_operating IS NOT NULL
                          AND COALESCE(
                              cf.cash_received_sales, cf.taxes_refunded,
                              cf.cash_paid_goods, cf.cash_paid_employees,
                              cf.cash_paid_taxes, cf.total_operating_cf_in,
                              cf.total_operating_cf_out,
                          ) IS NULL
                      )
                  )
                ORDER BY m.stock_code
                """
            )
        except Exception as error:
            logger.warning("查询财务明细缺口失败: %s", error)
            return []
        codes = [row["stock_code"] for row in rows]
        # 快速源（sina/akshare）已确认无数据的股票登记
        # financial_detail_backfill missing；在重试窗口内不得再次占住队头，
        # 否则每轮都会落到 40s+ 的 TDX 慢回退上，队列永远推不动。
        try:
            cutoff = datetime.now(UTC) - timedelta(
                days=max(0, self.financial_detail_backfill_missing_retry_days),
            )
            miss_rows = self.sqlite.query(
                """SELECT stock_code FROM missing_list
                   WHERE field_name = 'financial_detail_backfill'
                     AND resolved_at IS NULL
                     AND detected_at >= ?""",
                [cutoff.isoformat()],
            )
            blocked = {row["stock_code"] for row in miss_rows}
            return [code for code in codes if code not in blocked]
        except Exception as error:
            logger.warning("查询财务明细 missing 缓存失败: %s", error)
            return codes

    def _refresh_buyback(self) -> dict[str, Any]:
        """低频全市场回购明细刷新，并返回本次发生变化的股票代码。

        东财回购接口一次返回全市场，刷新成本低；写入后需要重算受影响股票
        的 dividend_financing_ratio_pct 快照。
        """
        if not self._refresh_due("buyback_last_refresh", self.buyback_refresh_interval_days):
            return {
                "status": "skipped",
                "reason": "refreshed_within_interval",
                "interval_days": self.buyback_refresh_interval_days,
            }
        try:
            before = self.duck.read_query(
                """SELECT stock_code, COALESCE(SUM(buyback_amount), 0) AS amount
                   FROM buyback_events GROUP BY stock_code"""
            )
            before_by_code = {row["stock_code"]: row["amount"] for row in before}
            from app.core.buyback import BuybackUpdater

            report = BuybackUpdater(duck=self.duck, sqlite=self.sqlite).refresh_all()
            if report.get("status") != "success":
                return report
            after = self.duck.read_query(
                """SELECT stock_code, COALESCE(SUM(buyback_amount), 0) AS amount
                   FROM buyback_events GROUP BY stock_code"""
            )
            after_by_code = {row["stock_code"]: row["amount"] for row in after}
            changed_codes = sorted({
                code for code in set(before_by_code) | set(after_by_code)
                if before_by_code.get(code) != after_by_code.get(code)
            })
            self._mark_refreshed("buyback_last_refresh")
            return {**report, "changed_codes": changed_codes}
        except Exception as error:
            logger.warning("回购明细刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    _FINANCIAL_DETAIL_DATA_TYPES = ("balance_sheet", "income_statement", "cash_flow")
    _FINANCIAL_DETAIL_FAST_SOURCES = ("sina", "akshare_eastmoney")
    # 明细回填的字段级溯源只保留 screening/readiness 使用的核心口径；
    # 全字段逐行审计会在 source_audit（约 4000 万行）上产生数百万次
    # 索引插入，把回填速度压到 2 只/分钟。
    _FINANCIAL_DETAIL_AUDIT_FIELDS = {
        "total_assets", "total_liabilities", "total_equity",
        "total_equity_parent", "revenue", "parent_net_profit",
        "cf_from_operating", "net_profit", "deducted_net_profit",
        "total_operating_revenue", "cost_of_revenue",
    }

    def _refresh_financial_detail_backfill(
        self,
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """有界并发回填三大表明细（2026-08-31 提速版）。

        旧实现逐股串行 refetch_one：每股三次独立 DuckDB 事务提交，
        并且源缺口股会逐次落到 40s+ 的 TDX 慢回退；正式库实测约
        2 只/分钟。新实现：
        - 网络抓取并发（sina -> akshare 快速失败，不再默认走 TDX）；
        - 每 N 只合并为一个 DuckDB 事务，摊销每次约 7s 的提交开销；
        - 快速源确认无数据的股票登记 missing 并在 7 天内出队；
        - TDX 仅对每轮少量快速源缺口做补齐，不再阻塞大队列。
        """
        started = time.monotonic()
        limit = max(0, int(self.financial_detail_backfill_max_stocks_per_run))
        if limit <= 0:
            return {"status": "skipped", "reason": "backfill_disabled"}
        codes = self._financial_detail_gap_codes()
        if not codes:
            return {
                "status": "success", "targeted": 0, "succeeded": 0,
                "failed": 0, "source_missing": 0,
            }
        targets = codes[:limit]

        from app.core.init import DataInitializer

        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        succeeded_codes: list[str] = []
        failed_codes: list[str] = []
        missing_codes: list[str] = []
        buffer: list[tuple[str, dict[str, Any]]] = []
        tdx_budget = max(0, int(self.financial_detail_backfill_tdx_max_stocks_per_run))
        tdx_attempted = 0
        concurrency = max(1, min(int(self.financial_detail_backfill_concurrency), 16))
        batch_size = max(1, int(self.financial_detail_backfill_persist_batch_size))

        def persist_buffer(items: list[tuple[str, dict[str, Any]]]) -> None:
            nonlocal succeeded_codes, failed_codes
            if not items:
                return
            try:
                with self.duck.transaction() as conn:
                    for code, fetched in items:
                        self._write_financial_detail_trio(
                            initializer, conn, code, fetched,
                        )
            except Exception as error:
                logger.warning(
                    "财务明细批量事务失败，退化为逐股提交: %s", error,
                )
                for code, fetched in items:
                    try:
                        with self.duck.transaction() as conn:
                            self._write_financial_detail_trio(
                                initializer, conn, code, fetched,
                            )
                    except Exception as single_error:
                        failed_codes.append(code)
                        logger.warning(
                            "财务明细回填 %s 写入失败: %s", code, single_error,
                        )
                        continue
                    succeeded_codes.append(code)
                    self._record_financial_detail_sector_missing(
                        initializer, code, fetched,
                    )
                    self._resolve_financial_detail_missing(code)
                return
            for code, fetched in items:
                succeeded_codes.append(code)
                self._record_financial_detail_sector_missing(
                    initializer, code, fetched,
                )
                self._resolve_financial_detail_missing(code)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(self._fetch_financial_detail_trio_fast, code): code
                for code in targets
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                code = futures[future]
                try:
                    fetched, errors = future.result()
                except Exception as error:
                    fetched, errors = {}, {
                        data_type: str(error)
                        for data_type in self._FINANCIAL_DETAIL_DATA_TYPES
                    }
                if errors and tdx_attempted < tdx_budget:
                    tdx_attempted += 1
                    for data_type in list(errors):
                        tdx_result = self._fetch_financial_detail_tdx(
                            code, data_type,
                        )
                        if tdx_result is not None:
                            fetched[data_type] = tdx_result
                            errors.pop(data_type, None)
                if errors:
                    missing_codes.append(code)
                    self._record_financial_detail_missing(
                        code, "; ".join(
                            f"{data_type}: {reason}"
                            for data_type, reason in errors.items()
                        )[:400],
                    )
                else:
                    buffer.append((code, fetched))
                    if len(buffer) >= batch_size:
                        persist_buffer(buffer)
                        buffer.clear()
                if detail_cb is not None:
                    detail_cb("financial_detail_backfill", {
                        "step": "financial_detail_backfill",
                        "label": "财务明细回填",
                        "done": completed,
                        "total": len(targets),
                        "current": code,
                        "succeeded": len(succeeded_codes),
                        "failed": len(failed_codes),
                        "source_missing": len(missing_codes),
                    })
            if buffer:
                persist_buffer(buffer)

        return {
            "status": "partial" if failed_codes else "success",
            "targeted": len(targets),
            "succeeded": len(succeeded_codes),
            "failed": len(failed_codes),
            "source_missing": len(missing_codes),
            "succeeded_codes": succeeded_codes,
            "failed_codes": failed_codes[:20],
            "missing_codes": missing_codes[:20],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rate_per_minute": round(
                len(targets) * 60 / max(time.monotonic() - started, 0.001), 2
            ),
        }

    def _fetch_financial_detail_trio_fast(
        self, stock_code: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """并发抓取三表，只走快速源；失败原因返回给调用方决定 TDX 兜底。"""
        fetched: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for data_type in self._FINANCIAL_DETAIL_DATA_TYPES:
            result = self.adapter_mgr.fetch_with_sources(
                FetchRequest(data_type=data_type, stock_codes=[stock_code]),
                list(self._FINANCIAL_DETAIL_FAST_SOURCES),
            )
            if result.metadata.error or not result.data:
                errors[data_type] = result.metadata.error or "source_empty"
            else:
                fetched[data_type] = result
        return fetched, errors

    def _fetch_financial_detail_tdx(
        self, stock_code: str, data_type: str,
    ) -> Any | None:
        """TDX 兜底只由调用方有预算地触发，避免每个源缺口股都等 40s+。"""
        try:
            result = self.adapter_mgr.fetch_with_sources(
                FetchRequest(data_type=data_type, stock_codes=[stock_code]),
                ["tdx"],
            )
        except Exception as error:
            logger.warning("TDX 财务明细兜底失败 %s %s: %s", stock_code, data_type, error)
            return None
        if result.metadata.error or not result.data:
            return None
        return result

    @staticmethod
    def _write_sina_financial_table(
        initializer: Any, conn: Any, data_type: str, stock_code: str,
        result: Any,
    ) -> None:
        """以 TEMP TABLE + UPDATE/INSERT 合并一批 Sina 标准化明细行。

        与逐行 legacy merge 等价：源返回的非 NULL 字段覆盖旧值，源未返回
        的字段保留旧值，新报告期直接插入。每表只执行一次 UPDATE 和一次
        INSERT，避免逐行 DELETE+INSERT 在宽表上反复重写 row group。
        """
        available = initializer._financial_cols_cache.get(data_type)
        if available is None:
            available = {
                row[0]
                for row in conn.execute(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{data_type}'"
                ).fetchall()
            }
            initializer._financial_cols_cache[data_type] = available
        rows: list[dict[str, Any]] = []
        for row in result.data:
            mapped = {
                key: value
                for key, value in row.items()
                if key in available and value is not None
            }
            mapped.setdefault("stock_code", stock_code)
            if "raw_data" in available:
                mapped["raw_data"] = json.dumps(
                    row, ensure_ascii=False, default=str,
                )
            rows.append(mapped)
        if not rows:
            return
        fields = sorted({key for mapped in rows for key in mapped if key in available})
        if "stock_code" not in fields:
            fields.insert(0, "stock_code")
        if "report_date" not in fields:
            fields.insert(1, "report_date")
        temp = f"tmp_{data_type}_{uuid.uuid4().hex[:10]}"
        conn.execute(
            f"CREATE TEMP TABLE {temp} AS "
            f"SELECT * FROM {data_type} WHERE 1 = 0"
        )
        placeholders = ", ".join("?" for _ in fields)
        conn.executemany(
            f"INSERT INTO {temp} ({', '.join(fields)}) "
            f"VALUES ({placeholders})",
            [[mapped.get(field) for field in fields] for mapped in rows],
        )
        updates = ", ".join(
            f"{field} = s.{field}"
            for field in fields
            if field not in {"stock_code", "report_date"}
        )
        conn.execute(
            f"UPDATE {data_type} t SET {updates} FROM {temp} s "
            f"WHERE t.stock_code = s.stock_code "
            f"AND t.report_date = s.report_date"
        )
        source_fields = ", ".join(f"s.{field}" for field in fields)
        conn.execute(
            f"INSERT INTO {data_type} ({', '.join(fields)}) "
            f"SELECT {source_fields} FROM {temp} s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {data_type} t "
            f"WHERE t.stock_code = s.stock_code "
            f"AND t.report_date = s.report_date)"
        )
        conn.execute(f"DROP TABLE {temp}")

    @staticmethod
    def _write_financial_detail_trio(
        initializer: Any, conn: Any, stock_code: str, fetched: dict[str, Any],
    ) -> None:
        """在调用方事务内写入一只股票的三表 + lineage。

        2026-09-01 原始响应归档已冷热分层，内存峰值不再由 raw_response_archive
        决定；Sina 批量行改用 TEMP TABLE 合并，减少逐行 DELETE+INSERT 的
        row-group 重写。非 Sina 兜底仍走逐行 upsert 的通用映射。
        """
        for data_type in IncrementalUpdater._FINANCIAL_DETAIL_DATA_TYPES:
            result = fetched.get(data_type)
            if result is None or not result.data:
                continue
            if result.metadata.source == "sina":
                IncrementalUpdater._write_sina_financial_table(
                    initializer, conn, data_type, stock_code, result,
                )
            else:
                for row in result.data:
                    initializer._upsert_financial_row(
                        conn, data_type, stock_code, row,
                    )
        for data_type in IncrementalUpdater._FINANCIAL_DETAIL_DATA_TYPES:
            result = fetched.get(data_type)
            if result is None or not result.data:
                continue
            batch_id = initializer._record_batch_in_connection(
                conn, result, data_type, len(result.data),
            )
            latest_rows = [
                max(
                    result.data,
                    key=lambda row: str(row.get("report_date") or ""),
                )
            ]
            initializer._record_field_audit_in_connection(
                conn, result, latest_rows, stock_code, "report_date", batch_id,
                field_whitelist=IncrementalUpdater._FINANCIAL_DETAIL_AUDIT_FIELDS,
            )

    def _record_financial_detail_sector_missing(
        self, initializer: Any, stock_code: str, fetched: dict[str, Any],
    ) -> None:
        balance = fetched.get("balance_sheet")
        if balance is not None and balance.data:
            initializer._record_missing_financial_sector_fields(
                stock_code, balance.data,
            )

    def _record_financial_detail_missing(self, stock_code: str, reason: str) -> None:
        try:
            self.sqlite.execute(
                """INSERT INTO missing_list (stock_code, field_name, reason_code)
                   VALUES (?, 'financial_detail_backfill', ?)
                   ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                   DO UPDATE SET reason_code = excluded.reason_code,
                                 detected_at = CURRENT_TIMESTAMP""",
                [stock_code, (reason or "source_unavailable")[:400]],
            )
        except Exception as error:
            logger.warning("记录财务明细源缺口失败 %s: %s", stock_code, error)

    def _resolve_financial_detail_missing(self, stock_code: str) -> None:
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = 'financial_detail_backfill'
                     AND resolved_at IS NULL""",
                [datetime.now(UTC).isoformat(), stock_code],
            )
        except Exception as error:
            logger.warning("解决财务明细源缺口失败 %s: %s", stock_code, error)
    def _refresh_financials(
        self,
        stock_codes: list[str],
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh core statements for stocks with newly registered filings.

        混合批量：并发抓取，每满 financial_refresh_batch_size 只合并一个
        DuckDB 事务；整批失败退化为逐股单事务。
        """
        succeeded = 0
        succeeded_codes: list[str] = []
        pending_codes: list[str] = []
        failed: list[str] = []
        completed = 0
        batch_size = max(1, getattr(self, "financial_refresh_batch_size", 20))
        batch_items: list[tuple[str, list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]] = []

        def refresh_one(code: str) -> tuple[str, list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]:
            try:
                outcomes, fetched = self._fetch_financial_trio(code)
            except Exception as error:
                outcomes = [
                    {"status": "failed", "error": str(error), "data_type": data_type}
                    for data_type in ("balance_sheet", "income_statement", "cash_flow")
                ]
                fetched = {}
            return code, outcomes, fetched

        expected_period = self._current_expected_financial_period()

        def finish_one(code: str, outcomes: list[dict[str, Any]]) -> None:
            nonlocal succeeded, completed
            completed += 1
            if all(outcome["status"] == "success" for outcome in outcomes):
                if all(outcome.get("skipped") for outcome in outcomes):
                    local_dates = [
                        str(outcome.get("latest_local") or "")[:10]
                        for outcome in outcomes
                    ]
                    # 本地三表都已达到当前应发布报告期 → 公告目的已达成，
                    # 应标记成功入册；只有确实落后于期望期才是源延迟 pending。
                    if local_dates and all(d >= expected_period for d in local_dates):
                        succeeded += 1
                        succeeded_codes.append(code)
                    else:
                        pending_codes.append(code)
                else:
                    succeeded += 1
                    succeeded_codes.append(code)
            else:
                failed.append(code)
            if detail_cb is not None:
                detail_cb("financials", {
                    "step": "financials",
                    "label": "财务数据",
                    "done": completed,
                    "total": len(stock_codes),
                    "current": code,
                })

        concurrency = max(1, getattr(self, "financial_refresh_concurrency", 8))
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="vd-financial-refresh",
        ) as executor:
            futures = {executor.submit(refresh_one, code): code for code in stock_codes}
            for future in as_completed(futures):
                code, outcomes, fetched = future.result()
                if fetched:
                    batch_items.append((code, outcomes, fetched))
                    if len(batch_items) >= batch_size:
                        self._persist_financial_batch(batch_items)
                        for item_code, item_outcomes, _ in batch_items:
                            finish_one(item_code, item_outcomes)
                        batch_items.clear()
                else:
                    finish_one(code, outcomes)
        if batch_items:
            self._persist_financial_batch(batch_items)
            for item_code, item_outcomes, _ in batch_items:
                finish_one(item_code, item_outcomes)
            batch_items.clear()
        return {
            "status": "success" if not failed else "partial",
            "total": len(stock_codes), "success": succeeded, "failed": len(failed),
            "failed_codes": failed[:20], "succeeded_codes": succeeded_codes,
            "pending_codes": pending_codes[:20],
        }

    def _refresh_market_actions(
        self,
        stock_codes: list[str],
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh dividend and corporate-action evidence alongside filing data."""
        succeeded = 0
        failed_codes: list[str] = []
        for index, code in enumerate(stock_codes):
            outcomes = [self.refetch_one(code, data_type) for data_type in ("dividends", "xdxr")]
            if all(outcome["status"] == "success" for outcome in outcomes):
                succeeded += 1
            else:
                failed_codes.append(code)
            if detail_cb is not None:
                detail_cb("market_actions", {
                    "step": "market_actions",
                    "label": "分红除权",
                    "done": index + 1,
                    "total": len(stock_codes),
                    "current": code,
                })
        return {
            "status": "success" if not failed_codes else "partial",
            "total": len(stock_codes), "success": succeeded, "failed": len(failed_codes),
            "failed_codes": failed_codes[:20],
        }

    def _check_retry_tasks(self) -> list[dict]:
        """读取待重试任务列表"""
        try:
            rows = self.sqlite.query(
                "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json "
                "FROM retry_list WHERE retry_count < max_retries "
                "AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP) "
                "ORDER BY last_attempt ASC LIMIT 100"
            )
            return rows
        except Exception as e:
            logger.warning(f"读取重试列表失败: {e}")
            return []

    def _update_trading_dates(self) -> dict:
        """更新交易日历"""
        logger.info("[增量] 更新交易日历...")
        result = self.adapter_mgr.fetch(FetchRequest(data_type="trading_dates"))

        if result.metadata.error or not result.data:
            return {"status": "failed", "error": result.metadata.error}

        with self.sqlite.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS trading_dates (trade_date TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT OR REPLACE INTO trading_dates (trade_date) VALUES (?)",
                [(r["trade_date"],) for r in result.data],
            )

        logger.info(f"[增量] 交易日历更新完成: {len(result.data)} 个日期")
        return {"status": "success", "count": len(result.data)}

    def _get_xdxr_codes_since(self, start_date: str) -> set[str]:
        """Get stock codes with corporate-action events on/after start_date.

        前复权（qfq）价格以最新除权为基础重算历史，因此发生除权除息后，
        该股票的全部 qfq 历史都需要重拉（PRD §7.7 复权一致性）。
        """
        try:
            rows = self.duck.read_query(
                "SELECT DISTINCT stock_code FROM xdxr WHERE event_date >= ?",
                [start_date],
            )
            return {str(row["stock_code"]) for row in rows}
        except Exception as e:
            logger.warning(f"查询 xdxr 股票失败: {e}")
            return set()

    def _update_prices_incremental(
        self, max_stocks: int,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict:
        """增量更新价格——只更新有新数据的股票。

        - 普通股票：按 latest_local → today 增量补齐 raw + qfq
        - 发生除权除息（xdxr）的股票：qfq 全历史重拉，保证复权口径一致
        - 本地价格陈旧超过窗口上限：raw 也走全量重拉

        detail_cb: 每只股票处理完后的细粒度回调 (step_name, {done, total,
            current, label})，供前端实时进度条使用。
        """
        logger.info("[增量] 检查需要价格更新的股票...")

        latest_local = self._get_latest_local_price_date()
        today = datetime.now().strftime("%Y-%m-%d")
        target_date = self._latest_expected_trading_date(today)

        # 按股票自己的最新日期选择缺口，不能用全库 MAX 代表全部股票。
        # 每只成功股票立即提交，进程中断后下一轮可从剩余缺口继续。
        try:
            stocks = self.duck.read_query(
                """SELECT stock.stock_code, stock.exchange,
                          raw.latest_raw_date, qfq.latest_qfq_date
                   FROM stock_meta stock
                   LEFT JOIN (
                     SELECT stock_code, MAX(trade_date) AS latest_raw_date
                     FROM price_daily_raw GROUP BY stock_code
                   ) raw ON raw.stock_code = stock.stock_code
                   LEFT JOIN (
                     SELECT stock_code, MAX(trade_date) AS latest_qfq_date
                     FROM price_daily_qfq GROUP BY stock_code
                   ) qfq ON qfq.stock_code = stock.stock_code
                   WHERE stock.is_listed IS TRUE AND COALESCE(stock.is_suspended, FALSE) IS FALSE
                      AND (raw.latest_raw_date IS NULL OR qfq.latest_qfq_date IS NULL
                           OR raw.latest_raw_date < ? OR qfq.latest_qfq_date < ?
                           OR NOT EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND audit.report_date = raw.latest_raw_date
                                 AND batch.data_type = 'price_daily_raw'
                           )
                           OR NOT EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND audit.report_date = qfq.latest_qfq_date
                                 AND batch.data_type = 'price_daily_qfq'
                           ))
                   ORDER BY stock.stock_code""",
                [target_date, target_date],
            )
        except Exception:
            return {"status": "skipped", "reason": "no_stock_meta"}

        if not stocks:
            logger.info("[增量] 所有非停牌股票价格已是最新，无需更新")
            return {"status": "skipped", "reason": "prices_up_to_date"}

        target_stocks = stocks
        try:
            priority_codes = {
                row["stock_code"]
                for row in self.sqlite.query("SELECT DISTINCT stock_code FROM watchlist")
            }
        except Exception as error:
            priority_codes = set()
            logger.warning("读取研究优先名单失败，保持代码顺序: %s", error)
        if priority_codes:
            target_stocks = sorted(
                target_stocks,
                key=lambda stock: (
                    stock["stock_code"] not in priority_codes,
                    stock["stock_code"],
                ),
            )
        if max_stocks > 0:
            # C10修复(报告41): 取前 N 只必须稳定排序（ORDER BY stock_code），
            # 否则每次运行取到的子集随机漂移
            target_stocks = target_stocks[:max_stocks]

        fallback_start = latest_local or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        target_dates = [
            str(value)
            for stock in target_stocks
            for value in (stock.get("latest_raw_date"), stock.get("latest_qfq_date"))
            if value is not None
        ]
        start_date = min(target_dates) if target_dates else fallback_start
        end_date = target_date

        # 增量窗口内发生除权除息的股票：qfq 需要全历史重拉
        xdxr_window_start = start_date
        xdxr_codes = self._get_xdxr_codes_since(xdxr_window_start)
        if xdxr_codes:
            logger.info(f"[增量] {len(xdxr_codes)} 只股票在窗口内发生除权除息，qfq 将全历史重拉")

        # 本地数据过于陈旧（超过窗口上限）：raw 也整段重拉
        raw_full_refetch = False
        if latest_local:
            try:
                latest_date = datetime.strptime(latest_local, "%Y-%m-%d").date()
                gap_days = (datetime.now().date() - latest_date).days
                raw_full_refetch = gap_days > self.incremental_window_days
            except ValueError:
                raw_full_refetch = False
        if raw_full_refetch:
            logger.info(f"[增量] 本地价格陈旧超过 {self.incremental_window_days} 天，raw 整段重拉")

        success_count = 0
        fail_count = 0
        updated_codes: list[str] = []
        completed: list[int] = [0]
        concurrency = max(1, int(self.price_fetch_concurrency))
        max_concurrency = max(concurrency, int(self.price_fetch_max_concurrency))
        concurrency_step = max(1, int(self.price_fetch_concurrency_step))
        scale_up_seconds = max(1, int(self.price_fetch_scale_up_seconds))
        fetch_timeout = max(1, int(self.price_fetch_timeout_seconds))
        batch_size = max(1, int(self.price_fetch_batch_size))
        update_started = time.monotonic()

        request_executor = ThreadPoolExecutor(
            max_workers=max_concurrency * 2,
            thread_name_prefix="vd-price-request",
        )

        def fetch_one(stock: dict) -> tuple[str, Any, Any, str | None, bool]:
            """在 worker 线程网络抓取 raw+qfq；不碰共享写连接。

            增量原则：raw 与 qfq 各自以本地最新日期为起点，仅缺失的一侧
            才全量拉取。绝不用全局最老日期拖低另一侧的起点——历史已完整的
            股票每次只拉缺口（通常几行），避免无意义的全历史重拉。
            """
            code = stock["stock_code"]
            need_qfq_full = code in xdxr_codes
            raw_latest = stock.get("latest_raw_date")
            qfq_latest = stock.get("latest_qfq_date")
            raw_from = None if raw_full_refetch else (str(raw_latest) if raw_latest else None)
            qfq_from = None if (need_qfq_full or raw_full_refetch) else (str(qfq_latest) if qfq_latest else None)
            def fetch_adjust(adjust: str, start: str | None) -> tuple[Any, float]:
                started = time.monotonic()
                result = self.adapter_mgr.fetch(FetchRequest(
                    data_type="price_daily", stock_codes=[code],
                    start_date=start, end_date=end_date, adjust=adjust,
                    extra_params={
                        "deadline_monotonic": started + fetch_timeout,
                    },
                ))
                return result, time.monotonic() - started

            futures = {
                request_executor.submit(fetch_adjust, "raw", raw_from): "raw",
                request_executor.submit(fetch_adjust, "qfq", qfq_from): "qfq",
            }
            done, pending = wait(futures, timeout=fetch_timeout)
            if pending:
                recover = getattr(self.adapter_mgr, "recover_after_timeout", None)
                if callable(recover):
                    recover()
                return code, None, None, f"fetch timeout after {fetch_timeout}s", True
            results: dict[str, Any] = {}
            slow = False
            try:
                for future in done:
                    result, elapsed = future.result()
                    results[futures[future]] = result
                    slow = slow or elapsed > 30
            except Exception as exc:
                return code, None, None, f"fetch exception: {exc}", False
            raw_result = results["raw"]
            qfq_result = results["qfq"]
            if raw_result.metadata.error or not raw_result.data:
                return code, None, None, raw_result.metadata.error or "empty raw", slow
            if qfq_result.metadata.error or not qfq_result.data:
                return code, None, None, (qfq_result.metadata.error or "empty qfq"), slow
            return code, raw_result, qfq_result, None, slow

        batch: list[tuple[str, Any, Any]] = []
        stable_since = time.monotonic()
        observed_concurrency = concurrency
        # 连续流水线：在途任务 = 股票执行器队列深度。慢股票只占用一个槽位，
        # 其余槽位继续完成后面的股票；固定窗口实现会被单只 45s 长尾股票拖住
        # 整窗（2026-08-28 提速计划）。
        pipeline_depth = max(concurrency * 4, int(self.price_fetch_pipeline_depth))
        stock_executor = ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="vd-price-stock",
        )
        outstanding: dict[Future, dict] = {}
        next_index = 0

        def fill_pipeline() -> None:
            nonlocal next_index
            while len(outstanding) < pipeline_depth and next_index < len(target_stocks):
                stock = target_stocks[next_index]
                next_index += 1
                outstanding[stock_executor.submit(fetch_one, stock)] = stock

        def handle_price_result(
            code: str,
            raw_result: Any,
            qfq_result: Any,
            err: str | None,
        ) -> None:
            nonlocal success_count, updated_codes, fail_count
            if err is not None or raw_result is None:
                fail_count += 1
                self._record_failure(code, "price_daily", "manager", err or "empty")
            else:
                batch.append((code, raw_result, qfq_result))
            if len(batch) >= batch_size:
                success_count, updated_codes, fail_count = self._persist_price_batch(
                    batch, success_count, updated_codes, fail_count
                )
                batch.clear()

        try:
            fill_pipeline()
            while outstanding:
                done, _ = wait(
                    tuple(outstanding.keys()),
                    timeout=fetch_timeout + 5,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    # fetch_one 自身有 deadline，这里只是防御 executor 线程异常退出。
                    logger.warning(
                        "价格流水线 %d 秒无任何股票完成（在途 %d），继续等待",
                        fetch_timeout + 5,
                        len(outstanding),
                    )
                    continue
                window_penalty = False
                for future in done:
                    stock = outstanding.pop(future)
                    code = stock["stock_code"]
                    try:
                        code, raw_result, qfq_result, err, slow = future.result()
                        window_penalty = window_penalty or slow or err is not None
                        handle_price_result(code, raw_result, qfq_result, err)
                    except Exception as exc:
                        window_penalty = True
                        handle_price_result(code, None, None, f"price pipeline exception: {exc}")
                    completed[0] += 1
                    if detail_cb is not None:
                        detail_cb("price", {
                            "done": completed[0],
                            "total": len(target_stocks),
                            "current": code,
                            "label": "股票价格",
                        })
                if window_penalty:
                    new_concurrency = max(
                        int(self.price_fetch_concurrency),
                        concurrency - concurrency_step,
                    )
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= scale_up_seconds:
                    new_concurrency = min(max_concurrency, concurrency + concurrency_step)
                    stable_since = time.monotonic()
                else:
                    new_concurrency = concurrency
                if new_concurrency != concurrency:
                    logger.warning(
                        "价格抓取自适应并发 %d -> %d", concurrency, new_concurrency,
                    )
                    concurrency = new_concurrency
                    # ThreadPoolExecutor 支持运行时调整 worker 数；流水线只按
                    # 队列深度提交，不会创建不可控的海量线程。
                    stock_executor._max_workers = concurrency
                    observed_concurrency = max(observed_concurrency, concurrency)
                fill_pipeline()
            if batch:
                success_count, updated_codes, fail_count = self._persist_price_batch(
                    batch, success_count, updated_codes, fail_count
                )
        finally:
            request_executor.shutdown(wait=False, cancel_futures=True)
            stock_executor.shutdown(wait=False, cancel_futures=True)

        logger.info(f"[增量] 价格更新完成: 成功 {success_count}, 失败 {fail_count}")
        elapsed_seconds = max(time.monotonic() - update_started, 0.001)
        return {
            "status": "success" if fail_count == 0 else "partial",
            "total": len(target_stocks),
            "success": success_count,
            "failed": fail_count,
            "xdxr_full_refetch": len(xdxr_codes),
            "raw_full_refetch": raw_full_refetch,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "rate_per_minute": round(completed[0] * 60 / elapsed_seconds, 2),
            "max_concurrency_used": observed_concurrency,
            "priority_count": sum(
                stock["stock_code"] in priority_codes for stock in target_stocks
            ),
            "_updated_codes": updated_codes,
        }

    def _latest_expected_trading_date(self, today: str, *, now: datetime | None = None) -> str:
        """Use the latest closed session, not today's still-open trading date."""
        local_now = now or datetime.now()
        cutoff = today
        if local_now.strftime("%Y-%m-%d") == today and local_now.strftime("%H:%M") < "15:30":
            cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            rows = self.sqlite.query(
                """SELECT MAX(trade_date) AS trade_date FROM trading_dates
                   WHERE trade_date <= ?""",
                [cutoff],
            )
            if rows and rows[0].get("trade_date"):
                return str(rows[0]["trade_date"])
        except Exception as error:
            logger.warning("读取交易日历目标日期失败: %s", error)
        return today

    _PRICE_FULL_REPLACE_THRESHOLD = 200
    _PRICE_FULL_REPLACE_MIN_RATIO = 0.5

    def _persist_price_pair_in_connection(
        self, connection: Any, stock_code: str, raw_result: Any, qfq_result: Any,
        *, full_replace_raw: bool = False, full_replace_qfq: bool = False,
        full_field_audit: bool = False,
    ) -> None:
        """Write one stock's raw/qfq rows + batch lineage inside a caller transaction.

        DuckDB's ON CONFLICT DO UPDATE is linear in the target table size
        (benchmarked ~4-5s per 2000 rows on a 1M-row table), so full-history
        responses on the 17M-row formal table stall the writer for a minute
        per stock. Large responses use an in-transaction delete+insert
        replace instead, guarded against truncated sources by a row-count
        ratio check. Incremental responses keep the conflict-safe upsert.
        Raw and qfq are judged independently: one side may be a small
        incremental while the other is a full-history replacement.
        """
        from app.core.init import DataInitializer

        lineage = DataInitializer.__new__(DataInitializer)
        lineage._batch_id = str(uuid.uuid4())
        for table, data_type, result, full_replace in (
            ("price_daily_raw", "price_daily_raw", raw_result, full_replace_raw),
            ("price_daily_qfq", "price_daily_qfq", qfq_result, full_replace_qfq),
        ):
            if full_replace:
                local_rows = connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE stock_code = ?",
                    [stock_code],
                ).fetchone()
                local_count = int(local_rows[0]) if local_rows else 0
                if len(result.data) < local_count * self._PRICE_FULL_REPLACE_MIN_RATIO:
                    raise ValueError(
                        f"{stock_code} {data_type} full replace truncated "
                        f"({len(result.data)} remote vs {local_count} local); keeping old data"
                    )
                connection.execute(
                    f"DELETE FROM {table} WHERE stock_code = ?", [stock_code]
                )
                connection.executemany(
                    f"""INSERT INTO {table}
                        (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        [stock_code, row.get("trade_date"), row.get("open"), row.get("high"),
                         row.get("low"), row.get("close"), row.get("volume"), row.get("turnover"),
                         row.get("turnover_rate")]
                        for row in result.data
                    ],
                )
            else:
                connection.executemany(
                    f"""INSERT INTO {table}
                        (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                          open=COALESCE(excluded.open, {table}.open),
                          high=COALESCE(excluded.high, {table}.high),
                          low=COALESCE(excluded.low, {table}.low),
                          close=COALESCE(excluded.close, {table}.close),
                          volume=COALESCE(excluded.volume, {table}.volume),
                          turnover=COALESCE(excluded.turnover, {table}.turnover),
                          turnover_rate=COALESCE(excluded.turnover_rate, {table}.turnover_rate)""",
                    [
                        [stock_code, row.get("trade_date"), row.get("open"), row.get("high"),
                         row.get("low"), row.get("close"), row.get("volume"), row.get("turnover"),
                         row.get("turnover_rate")]
                        for row in result.data
                    ],
                )
            batch_id = lineage._record_batch_in_connection(
                connection, result, data_type, len(result.data)
            )
            if full_field_audit:
                lineage._record_field_audit_in_connection(
                    connection, result, result.data, stock_code, "trade_date", batch_id,
                )
            latest_row = max(
                result.data,
                key=lambda row: str(row.get("trade_date") or ""),
            )
            latest_close = latest_row.get("close")
            if isinstance(latest_close, (int, float)):
                connection.execute(
                    """INSERT INTO source_audit
                       (stock_code, field_name, report_date, value, source,
                        fetch_batch_id, fetch_time, raw_response_hash, confidence,
                        reason_code, api_version)
                       VALUES (?, 'latest_close', ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    [
                        stock_code,
                        latest_row.get("trade_date"),
                        latest_close,
                        result.metadata.source,
                        batch_id,
                        result.metadata.fetch_time,
                        result.metadata.raw_response_hash,
                        result.metadata.confidence,
                        result.metadata.api_version,
                    ],
                )
            # 价格行数极大（全历史重拉单只可达数千行），逐值审计会把
            # source_audit 放大成千万行级且非 PRD §14 关键财务字段所需。
            # 价格采用 batch 级溯源，并仅审计每股最新收盘价；财务/股本等
            # 关键字段仍走逐值审计。

    def _persist_incremental_price_pair(
        self, stock_code: str, raw_result: Any, qfq_result: Any, *, full_field_audit: bool = False,
    ) -> None:
        """Commit one stock's raw/qfq rows and lineage atomically for restart safety."""
        with self.duck.transaction() as connection:
            self._persist_price_pair_in_connection(
                connection, stock_code, raw_result, qfq_result, full_field_audit=full_field_audit,
            )

    def _persist_price_batch(
        self,
        batch: list[tuple[str, Any, Any]],
        success_count: int,
        updated_codes: list[str],
        fail_count: int,
    ) -> tuple[int, list[str], int]:
        """Persist many stocks in one DuckDB transaction (few commits, far fewer
        WAL flushes than per-stock transactions). On failure, falls back to
        per-stock atomic commits so one bad row never blocks the whole batch.

        Large full-history responses are split out and committed as single-stock
        full-replace transactions: DuckDB upserts degrade linearly with table
        size, so a 2000-row conflict upsert stalls ~1 minute on the formal
        table, while delete+insert completes in seconds.
        """
        large, small = [], []
        for item in batch:
            _, raw_result, qfq_result = item
            raw_large = (
                raw_result is not None
                and len(raw_result.data) > self._PRICE_FULL_REPLACE_THRESHOLD
            )
            qfq_large = (
                qfq_result is not None
                and len(qfq_result.data) > self._PRICE_FULL_REPLACE_THRESHOLD
            )
            if raw_large or qfq_large:
                large.append((item, raw_large, qfq_large))
            else:
                small.append(item)
        for (code, raw_result, qfq_result), raw_large, qfq_large in large:
            try:
                with self.duck.transaction() as connection:
                    self._persist_price_pair_in_connection(
                        connection, code, raw_result, qfq_result,
                        full_replace_raw=raw_large,
                        full_replace_qfq=qfq_large,
                    )
                success_count += 1
                updated_codes.append(code)
            except Exception as exc:
                fail_count += 1
                self._record_failure(
                    code, "price_daily", "manager", str(exc),
                )
        if not small:
            return success_count, updated_codes, fail_count
        try:
            with self.duck.transaction() as connection:
                for code, raw_result, qfq_result in small:
                    self._persist_price_pair_in_connection(connection, code, raw_result, qfq_result)
            success_count += len(small)
            updated_codes.extend(code for code, _, _ in small)
            return success_count, updated_codes, fail_count
        except Exception:
            for code, raw_result, qfq_result in small:
                try:
                    self._persist_incremental_price_pair(code, raw_result, qfq_result)
                    success_count += 1
                    updated_codes.append(code)
                except Exception as exc:
                    fail_count += 1
                    source = getattr(raw_result, "metadata", None)
                    self._record_failure(
                        code, "price_daily",
                        getattr(source, "source", "manager") if source is not None else "manager",
                        str(exc),
                    )
            return success_count, updated_codes, fail_count

    def _cleanup_redundant_retries(self, target_date: str) -> int:
        """Drop price retry entries whose data is already up to date.

        Historical failures (e.g. the "no available adapter" batch from a
        broken source window) leave entries behind even after the underlying
        price data was recovered by the incremental pass. Rechecking those
        with a full-history refetch would burn minutes per stock for nothing.
        """
        try:
            rows = self.sqlite.query(
                "SELECT id, stock_code FROM retry_list WHERE data_type = 'price_daily'"
            )
        except Exception as error:
            logger.warning("读取 price retry 列表失败: %s", error)
            return 0
        if not rows:
            return 0
        codes = [row["stock_code"] for row in rows]
        slots = ", ".join("?" for _ in codes)
        try:
            coverage = self.duck.read_query(
                f"""WITH raw AS (
                        SELECT stock_code, MAX(trade_date) AS latest FROM price_daily_raw GROUP BY stock_code
                    ), qfq AS (
                        SELECT stock_code, MAX(trade_date) AS latest FROM price_daily_qfq GROUP BY stock_code
                    )
                    SELECT stock.stock_code,
                           raw.latest AS raw_latest,
                           qfq.latest AS qfq_latest,
                           EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND batch.data_type = 'price_daily_raw'
                           ) AS raw_lineaged,
                           EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND batch.data_type = 'price_daily_qfq'
                           ) AS qfq_lineaged
                    FROM stock_meta stock
                    LEFT JOIN raw ON raw.stock_code = stock.stock_code
                    LEFT JOIN qfq ON qfq.stock_code = stock.stock_code
                    WHERE stock.stock_code IN ({slots})""",
                codes,
            )
        except Exception as error:
            logger.warning("查询 price retry 覆盖状态失败: %s", error)
            return 0
        clean_ids = [
            row["id"]
            for row in rows
            for cover in coverage
            if cover["stock_code"] == row["stock_code"]
            and str(cover.get("raw_latest") or "")[:10] >= target_date
            and str(cover.get("qfq_latest") or "")[:10] >= target_date
            and cover.get("raw_lineaged")
            and cover.get("qfq_lineaged")
        ]
        if clean_ids:
            self.sqlite.execute(
                "DELETE FROM retry_list WHERE id IN ({})".format(
                    ", ".join("?" for _ in clean_ids)
                ),
                clean_ids,
            )
            logger.info("[增量] 清理 %d 条已达标的价格重试条目", len(clean_ids))
        return len(clean_ids)

    # 无逐股重试路径、由 universe/行业步骤统一维护的数据域：历史失败条目
    # 是死循环垃圾（如 akshare 被封窗口产生的 listing_info 失败）。
    # announcements 除外：其 retry 条目是公告 pending 的持久化标记（PRD §7.4）。
    _CLEANUP_RETRY_DATA_TYPES = {
        "listing_info", "stock_list", "trading_dates", "sw_industry",
        "csrc_industry",
    }

    def _resolve_complete_missing_records(self) -> int:
        """Mark listing_info missing entries resolved when local fields are complete.

        The universe source outage recorded missing entries for the whole
        universe even though most stocks already had complete listing data.
        Once the local fields are verified complete, the entry is resolved
        (housekeeping purges resolved rows after 30 days).
        """
        rows = self.sqlite.query(
            "SELECT stock_code FROM missing_list "
            "WHERE field_name = 'listing_info' AND resolved_at IS NULL"
        )
        if not rows:
            return 0
        codes = [row["stock_code"] for row in rows]
        slots = ", ".join("?" for _ in codes)
        try:
            complete = self.duck.read_query(
                f"""SELECT stock_code FROM stock_meta
                    WHERE stock_code IN ({slots})
                      AND listing_date IS NOT NULL
                      AND is_st IS NOT NULL
                      AND is_suspended IS NOT NULL""",
                codes,
            )
        except Exception as error:
            logger.warning("查询 listing_info 缺失覆盖状态失败: %s", error)
            return 0
        if not complete:
            return 0
        complete_codes = [row["stock_code"] for row in complete]
        now = datetime.now(UTC).isoformat()
        self.sqlite.execute(
            "UPDATE missing_list SET resolved_at = ? "
            "WHERE field_name = 'listing_info' AND stock_code IN ({})".format(
                ", ".join("?" for _ in complete_codes)
            ),
            [now, *complete_codes],
        )
        logger.info("[增量] 标记 %d 条 listing_info 缺失已补齐", len(complete_codes))
        return len(complete_codes)

    def _cleanup_unretryable_tasks(self) -> int:
        """Drop retry entries whose data domain has no per-stock refetch path.

        listing_info / stock_list / trading_dates etc. are maintained by the
        universe step, not by refetch_one. Historical failures of those types
        (e.g. the akshare outage window) would otherwise fail forever on every
        retry round. Their remaining gaps stay visible in missing_list.
        """
        rows = self.sqlite.query(
            f"""SELECT id FROM retry_list
                WHERE data_type IN ({", ".join("?" for _ in self._CLEANUP_RETRY_DATA_TYPES)})""",
            list(self._CLEANUP_RETRY_DATA_TYPES),
        )
        if not rows:
            return 0
        ids = [row["id"] for row in rows]
        self.sqlite.execute(
            "DELETE FROM retry_list WHERE id IN ({})".format(
                ", ".join("?" for _ in ids)
            ),
            ids,
        )
        logger.info("[增量] 清理 %d 条无重试路径的失败任务", len(ids))
        return len(ids)

    def _resolve_legal_empty_funding_missing(self) -> int:
        """Resolve A-share placement_funding source_empty markers.

        Eastmoney BonusFinancing 页面为空代表该公司没有增发/配股事件，
        属于合法空结果，不应长期挂在 missing_list。北交所（4/8/9 开头）
        没有东财交叉源，继续保留为如实披露缺口。
        """
        try:
            rows = self.sqlite.query(
                """SELECT COUNT(*) AS c FROM missing_list
                   WHERE field_name = 'placement_funding'
                     AND reason_code = 'source_empty'
                     AND resolved_at IS NULL
                     AND stock_code NOT LIKE '4%'
                     AND stock_code NOT LIKE '8%'
                     AND stock_code NOT LIKE '9%'"""
            )
            count = int(rows[0]["c"]) if rows else 0
            if count:
                self.sqlite.execute(
                    """UPDATE missing_list SET resolved_at = CURRENT_TIMESTAMP
                       WHERE field_name = 'placement_funding'
                         AND reason_code = 'source_empty'
                         AND resolved_at IS NULL
                         AND stock_code NOT LIKE '4%'
                         AND stock_code NOT LIKE '8%'
                         AND stock_code NOT LIKE '9%'"""
                )
                logger.info("已解决 %d 条 A 股无增发/配股事件的合法缺失", count)
            return count
        except Exception as error:
            logger.warning("解决合法空融资缺失失败: %s", error)
            return 0

    def _cleanup_completed_announcement_retries(self) -> int:
        """Drop announcement-pending entries only after their filings are registered.

        2026-08-29 修复：旧逻辑只要“三表在 18 个月内有一个共同完整期”就清理，
        导致 Q1 已完整但 Q2 中报仍 pending 的股票被误清，retry 永久丢失。
        现在以 announcement_registry 为准：retry.extra_json 里的公告 ID
        全部已入册，才说明对应财务刷新真正成功过。
        """
        rows = self.sqlite.query(
            "SELECT id, stock_code, extra_json FROM retry_list "
            "WHERE data_type = 'announcements'"
        )
        if not rows:
            return 0
        completed_ids: list[int] = []
        for row in rows:
            try:
                payload = json.loads(row.get("extra_json") or "{}")
                announcement_ids = payload.get("announcement_ids") or []
            except (json.JSONDecodeError, TypeError):
                continue
            if not announcement_ids:
                continue
            slots = ", ".join("?" for _ in announcement_ids)
            registered = self.sqlite.query(
                f"SELECT COUNT(*) AS c FROM announcement_registry "
                f"WHERE announcement_id IN ({slots})",
                announcement_ids,
            )
            if registered and int(registered[0]["c"]) >= len(set(announcement_ids)):
                completed_ids.append(int(row["id"]))
        if completed_ids:
            slots = ", ".join("?" for _ in completed_ids)
            self.sqlite.execute(
                f"DELETE FROM retry_list WHERE id IN ({slots})",
                completed_ids,
            )
            logger.info("[增量] 清理 %d 条已入册的公告待处理", len(completed_ids))
        return len(completed_ids)

    def _retry_failed_tasks(self, tasks: list[dict]) -> dict:
        """重试失败任务"""
        logger.info(f"[增量] 重试 {len(tasks)} 个失败任务...")

        success_count = 0
        still_failing = 0
        recompute_codes: list[str] = []

        def note_snapshot_input(code: str) -> None:
            if code and code not in recompute_codes:
                recompute_codes.append(code)

        def note_treasury_curve_input() -> None:
            try:
                for row in self.duck.read_query(
                    "SELECT stock_code FROM stock_meta "
                    "WHERE is_listed IS TRUE ORDER BY stock_code"
                ):
                    note_snapshot_input(row["stock_code"])
            except Exception as error:
                logger.warning("查询国债重试影响范围失败: %s", error)

        for task in tasks:
            retry_id = task["id"]
            stock_code = task["stock_code"]
            data_type = task["data_type"]
            if data_type != "price_daily":
                if data_type not in {
                    "balance_sheet", "income_statement", "cash_flow",
                    "dividends", "xdxr",
                    "company_profile", "business_breakdown",
                    "share_capital_history", "treasury_yield_curve",
                    "ipo_funding", "placement_funding", "index_valuation",
                }:
                    # 无逐股重试路径的数据域（announcements 等）：retry 条目
                    # 是 pending 标记，由对应维护流程消费；在这里重试只会
                    # 每轮失败并递增 retry_count（死循环），必须跳过。
                    continue
                if data_type in {"company_profile", "business_breakdown"}:
                    from app.core.business import BusinessOverviewUpdater
                    outcome = BusinessOverviewUpdater(
                        duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
                    ).update_stock(stock_code)
                    if outcome["status"] == "success":
                        self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                        success_count += 1
                    else:
                        still_failing += 1
                        self._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                    continue
                if data_type == "share_capital_history":
                    # P4-5 修复（reports/73）：历史股本链 retry 由回填器消费
                    # 2026-08-14 红队 F1：单股异常（如 DuckDB FatalException
                    # "Failed to delete all rows from index"）曾逃逸到 run_once，
                    # 炸掉整轮更新且 retry_count 永不递增（永久死循环）。
                    # 现加 per-task 隔离：异常只标记该股失败，轮次继续；
                    # 累计失败仍受 max_retries 上限约束。
                    try:
                        from app.core.capital import CapitalHistoryUpdater
                        outcome = CapitalHistoryUpdater(
                            duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
                        ).update_stock(stock_code)
                    except Exception as error:
                        still_failing += 1
                        reason = f"retry crashed: {error}"
                        if _is_duckdb_fatal(error):
                            reason = f"duckdb_fatal (建议重建 share_capital_history 索引): {error}"
                        self._mark_retry_failed(retry_id, reason)
                        logger.error(
                            "[增量] 股本链重试 %s 异常: %s（该股已隔离，本轮继续）",
                            stock_code, error,
                        )
                        continue
                    if outcome["status"] == "success":
                        self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                        success_count += 1
                    else:
                        still_failing += 1
                        self._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                    continue
                if data_type == "treasury_yield_curve":
                    # P3-5 修复（reports/73）：国债曲线 retry 按 extra_json
                    # 恢复对应模式（history=按期限回填，daily=按日期日终）
                    from app.core.treasury import TreasuryCurveUpdater
                    try:
                        extra = json.loads(task.get("extra_json") or "{}")
                    except json.JSONDecodeError:
                        extra = {}
                    updater = TreasuryCurveUpdater(
                        duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
                    )
                    if extra.get("mode") == "history" and extra.get("tenor") is not None:
                        outcome = updater._backfill_one(float(extra["tenor"]))
                    else:
                        outcome = updater.update_daily(
                            [str(extra.get("work_date") or "")[:10]]
                            if extra.get("work_date") else None
                        )
                    if outcome["status"] == "success":
                        self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                        success_count += 1
                        note_treasury_curve_input()
                    else:
                        still_failing += 1
                        self._mark_retry_failed(
                            retry_id, outcome.get("error") or outcome.get("reason") or "retry failed"
                        )
                    continue
                if data_type in {"ipo_funding", "placement_funding"}:
                    # 融资事件域（数据补全 2026-08-25）：单股 update_stock 同时
                    # 重抓 IPO + 增发/配股；成功后清理该股全部 funding 相关 retry。
                    from app.core.funding import FundingUpdater
                    outcome = FundingUpdater(
                        duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
                    ).update_stock(stock_code)
                    if outcome["status"] == "success":
                        self.sqlite.execute(
                            "DELETE FROM retry_list WHERE stock_code = ? "
                            "AND data_type IN ('ipo_funding', 'placement_funding')",
                            [stock_code],
                        )
                        success_count += 1
                        note_snapshot_input(stock_code)
                    else:
                        still_failing += 1
                        self._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                    continue
                if data_type == "index_valuation":
                    # 指数估值域（数据补全 2026-08-25）：stock_code 存指数代码，
                    # 重试即重新抓取主源+交叉源全量（低频，无风控风险）。
                    # SW_ALL 为申万一级行业分组重试（2026-09-05 v21）。
                    from app.core.index_valuation import SW_INDUSTRY_GROUP, IndexValuationUpdater
                    updater = IndexValuationUpdater(duck=self.duck, sqlite=self.sqlite)
                    if stock_code == SW_INDUSTRY_GROUP:
                        outcome = updater.update_sw_industries()
                        error_text = outcome.get("error", "retry failed")
                    else:
                        outcome = updater.update_daily([stock_code])
                        error_text = (
                            outcome.get("indexes", {}).get(stock_code, {}).get("primary_error")
                            or "retry failed"
                        )
                    if outcome["status"] == "success":
                        self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                        success_count += 1
                    else:
                        still_failing += 1
                        self._mark_retry_failed(retry_id, error_text)
                    continue
                outcome = self.refetch_one(stock_code, data_type)
                if outcome["status"] == "success":
                    self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
                    if data_type in {
                        "balance_sheet", "income_statement", "cash_flow",
                        "dividends", "xdxr",
                    }:
                        note_snapshot_input(stock_code)
                else:
                    still_failing += 1
                    self._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                continue

            try:
                extra = json.loads(task.get("extra_json") or "{}")
            except json.JSONDecodeError:
                still_failing += 1
                self._mark_retry_failed(retry_id, "invalid retry metadata")
                continue
            adjust = extra.get("adjust", "raw")

            # 增量重试：从本地最新日期起拉缺口，避免对已恢复历史的股票
            # 重复全历史抓取（每条约 3000+ 行，串行处理时拖慢整个重试轮）。
            table = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
            start_from: str | None = None
            try:
                latest_rows = self.duck.read_query(
                    f"SELECT MAX(trade_date) AS latest FROM {table} WHERE stock_code = ?",
                    [stock_code],
                )
                latest_value = latest_rows[0]["latest"] if latest_rows else None
                if latest_value:
                    start_from = str(latest_value)[:10]
            except Exception:
                start_from = None

            # 重新抓取
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type=data_type,
                stock_codes=[stock_code],
                adjust=adjust,
                start_date=start_from,
                end_date=datetime.now().strftime("%Y-%m-%d"),
            ))

            if result.metadata.error or not result.data:
                still_failing += 1
                self._mark_retry_failed(
                    retry_id,
                    result.metadata.error or "empty result",
                )
            else:
                try:
                    self._persist_price_with_lineage(stock_code, adjust, result)
                except (RuntimeError, ValueError, TypeError) as error:
                    still_failing += 1
                    self._mark_retry_failed(retry_id, str(error))
                    continue
                self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                success_count += 1
                note_snapshot_input(stock_code)
                logger.info(f"  重试成功并落库: {stock_code} {data_type} adjust={adjust}")

        logger.info(f"[增量] 重试完成: 成功 {success_count}, 仍失败 {still_failing}")
        return {
            "status": (
                "success"
                if still_failing == 0
                else "failed"
                if success_count == 0
                else "partial"
            ),
            "total": len(tasks),
            "succeeded": success_count,
            "still_failing": still_failing,
            "recompute_codes": recompute_codes,
        }

    def refetch_one(self, stock_code: str, data_type: str, *, incremental: bool = False) -> dict[str, Any]:
        """Refetch one supported dataset through the same durable write paths as init.

        incremental=True 仅对财务三表有效：只拉最新报告期（num=1），
        本地已有该报告期时跳过写入并返回 skipped（数据源未就绪语义）。
        """
        if data_type not in {"price_daily", "balance_sheet", "income_statement", "cash_flow", "dividends", "xdxr"}:
            return {"status": "error", "error": f"unsupported refetch type: {data_type}"}
        if data_type == "price_daily":
            raw = self.adapter_mgr.fetch(FetchRequest(data_type="price_daily", stock_codes=[stock_code], adjust="raw"))
            qfq = self.adapter_mgr.fetch(
                FetchRequest(data_type="price_daily", stock_codes=[stock_code], adjust="qfq")
            )
            if raw.metadata.error or not raw.data or qfq.metadata.error or not qfq.data:
                return {
                    "status": "failed",
                    "error": raw.metadata.error
                    or qfq.metadata.error
                    or "empty result",
                }
            try:
                self._persist_incremental_price_pair(stock_code, raw, qfq, full_field_audit=True)
            except Exception as error:
                return {"status": "failed", "error": str(error)}
            return {"status": "success", "stock_code": stock_code, "data_type": data_type}

        from app.core.init import DataInitializer

        # 财务三表增量模式：只拉最新报告期，本地已有则跳过（PRD §7.7）
        if incremental and data_type in {"balance_sheet", "income_statement", "cash_flow"}:
            latest_local = self._get_latest_financial_report_date(stock_code, data_type)
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type=data_type,
                stock_codes=[stock_code],
                extra_params={"num": "1"},
            ))
            if result.metadata.error or not result.data:
                return {"status": "failed", "error": result.metadata.error or "empty result"}
            new_rows = [
                row for row in result.data
                if not latest_local or str(row.get("report_date") or "") > latest_local
            ]
            if not new_rows:
                logger.info(
                    f"[增量] {stock_code} {data_type} 无新报告期（本地最新 {latest_local}），跳过写入"
                )
                return {
                    "status": "success", "stock_code": stock_code, "data_type": data_type,
                    "skipped": True, "latest_local": latest_local,
                }
            initializer = DataInitializer(duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr)
            try:
                with self.duck.transaction() as conn:
                    for row in new_rows:
                        initializer._upsert_financial_row(conn, data_type, stock_code, row)
                    batch_id = initializer._record_batch_in_connection(
                        conn, result, data_type, len(new_rows)
                    )
                    initializer._record_field_audit_in_connection(
                        conn, result, new_rows, stock_code, "report_date", batch_id
                    )
                if data_type == "balance_sheet":
                    initializer._record_missing_financial_sector_fields(stock_code, new_rows)
            except Exception as error:
                return {"status": "failed", "error": str(error)}
            return {"status": "success", "stock_code": stock_code, "data_type": data_type, "skipped": False}

        result = self.adapter_mgr.fetch(FetchRequest(data_type=data_type, stock_codes=[stock_code]))
        if result.metadata.error or not result.data:
            return {"status": "failed", "error": result.metadata.error or "empty result"}
        initializer = DataInitializer(duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr)
        try:
            with self.duck.transaction() as conn:
                if data_type == "xdxr":
                    initializer._upsert_xdxr_rows(conn, stock_code, result.data)
                elif data_type == "dividends":
                    initializer._upsert_dividend_rows(conn, stock_code, result.data)
                else:
                    for row in result.data:
                        initializer._upsert_financial_row(conn, data_type, stock_code, row)
                batch_id = initializer._record_batch_in_connection(conn, result, data_type, len(result.data))
                initializer._record_field_audit_in_connection(
                    conn, result, result.data, stock_code,
                    "event_date" if data_type == "xdxr" else "report_date", batch_id,
                )
            if data_type == "balance_sheet":
                initializer._record_missing_financial_sector_fields(stock_code, result.data)
        except Exception as error:
            return {"status": "failed", "error": str(error)}
        return {"status": "success", "stock_code": stock_code, "data_type": data_type}

    def replenish_missing_core_data(self, max_stocks: int = 0) -> dict[str, Any]:
        """Fetch only currently listed stocks that lack required snapshot inputs."""
        rows = self.duck.read_query(
            """
            SELECT
                m.stock_code,
                m.exchange,
                EXISTS (
                    SELECT 1
                    FROM balance_sheet bs
                    JOIN income_statement ic
                      ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
                    JOIN cash_flow cf
                      ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
                    WHERE bs.stock_code = m.stock_code
                      AND bs.total_assets IS NOT NULL
                      AND bs.total_liabilities IS NOT NULL
                      AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
                      AND ic.revenue IS NOT NULL
                      AND ic.parent_net_profit IS NOT NULL
                      AND cf.cf_from_operating IS NOT NULL
                ) AS has_core_financials,
                EXISTS (
                    SELECT 1 FROM price_daily_raw raw
                    WHERE raw.stock_code = m.stock_code AND raw.close IS NOT NULL
                ) AS has_raw_price,
                EXISTS (
                    SELECT 1 FROM price_daily_qfq qfq
                    WHERE qfq.stock_code = m.stock_code AND qfq.close IS NOT NULL
                ) AS has_qfq_price
            FROM stock_meta m
            WHERE m.is_listed IS TRUE
            ORDER BY m.stock_code
            """
        )
        targets = [
            row
            for row in rows
            if not (
                row["has_core_financials"]
                and row["has_raw_price"]
                and row["has_qfq_price"]
            )
        ]
        if max_stocks > 0:
            targets = targets[:max_stocks]

        completed = 0
        completed_codes: list[str] = []
        failed_codes: list[str] = []
        for target in targets:
            code = target["stock_code"]
            data_types: list[str] = []
            if not target["has_core_financials"]:
                data_types.extend(["balance_sheet", "income_statement", "cash_flow"])
            if not target["has_raw_price"] or not target["has_qfq_price"]:
                data_types.append("price_daily")
            results = [self.refetch_one(code, data_type) for data_type in data_types]
            if all(result["status"] == "success" for result in results):
                completed += 1
                completed_codes.append(code)
            else:
                failed_codes.append(code)

        return {
            "status": "success" if not failed_codes else "partial",
            "targeted": len(targets),
            "completed": completed,
            "completed_codes": completed_codes,
            "failed": len(failed_codes),
            "failed_codes": failed_codes[:20],
        }

    def _persist_price_retry(
        self,
        stock_code: str,
        adjust: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Persist a successful price retry before its queue item is removed."""
        if adjust not in {"raw", "qfq"}:
            raise ValueError(f"unsupported price adjustment: {adjust}")
        table = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
        with self.duck.transaction() as connection:
            connection.executemany(
                f"""INSERT INTO {table}
                    (stock_code, trade_date, open, high, low, close, volume, turnover,
                     turnover_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                      open=COALESCE(excluded.open, {table}.open),
                      high=COALESCE(excluded.high, {table}.high),
                      low=COALESCE(excluded.low, {table}.low),
                      close=COALESCE(excluded.close, {table}.close),
                      volume=COALESCE(excluded.volume, {table}.volume),
                      turnover=COALESCE(excluded.turnover, {table}.turnover),
                      turnover_rate=COALESCE(excluded.turnover_rate, {table}.turnover_rate)""",
                [
                    (
                        stock_code,
                        row.get("trade_date"),
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("volume"),
                        row.get("turnover"),
                        row.get("turnover_rate"),
                    )
                    for row in rows
                ],
            )

    def _persist_price_with_lineage(self, stock_code: str, adjust: str, result: Any) -> None:
        """Commit a price response and its source evidence as one transaction."""
        if adjust not in {"raw", "qfq"}:
            raise ValueError(f"unsupported price adjustment: {adjust}")
        table = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
        from app.core.init import DataInitializer

        initializer = DataInitializer(duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr)
        with self.duck.transaction() as connection:
            connection.executemany(
                f"""INSERT INTO {table}
                    (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                      open=COALESCE(excluded.open, {table}.open),
                      high=COALESCE(excluded.high, {table}.high),
                      low=COALESCE(excluded.low, {table}.low),
                      close=COALESCE(excluded.close, {table}.close),
                      volume=COALESCE(excluded.volume, {table}.volume),
                      turnover=COALESCE(excluded.turnover, {table}.turnover),
                      turnover_rate=COALESCE(excluded.turnover_rate, {table}.turnover_rate)""",
                [
                    (stock_code, row.get("trade_date"), row.get("open"), row.get("high"),
                     row.get("low"), row.get("close"), row.get("volume"), row.get("turnover"),
                     row.get("turnover_rate"))
                    for row in result.data
                ],
            )
            data_type = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
            batch_id = initializer._record_batch_in_connection(
                connection, result, data_type, len(result.data)
            )
            initializer._record_field_audit_in_connection(
                connection, result, result.data, stock_code, "trade_date", batch_id
            )

    def _record_price_lineage(self, stock_code: str, adjust: str, result: Any) -> None:
        """Record the source metadata only after the matching price rows persisted."""
        from app.core.init import DataInitializer

        data_type = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
        initializer = DataInitializer(duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr)
        initializer._record_batch(result, data_type, len(result.data))
        initializer._record_field_audit(
            result, result.data, stock_code=stock_code, report_date_field="trade_date"
        )

    def _mark_retry_failed(self, retry_id: int, error: str) -> None:
        """Retain a failed retry and update its diagnostic state."""
        self.sqlite.execute(
            """UPDATE retry_list
               SET retry_count = retry_count + 1, error = ?, last_attempt = ?,
                   next_retry_at = datetime('now', '+' || MIN(24, 1 << MIN(retry_count + 1, 5)) || ' hours')
               WHERE id = ?""",
            [error[:500], datetime.now(UTC).isoformat(), retry_id],
        )

    def _record_failure(
        self,
        stock_code: str,
        data_type: str,
        adapter: str,
        error: str,
        extra_json: str | None = None,
    ) -> None:
        """记录失败到 retry_list"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, ?)
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [stock_code, data_type, adapter, error[:500],
                     datetime.now(UTC).isoformat(), extra_json or "{}"],
                )
        except Exception as e:
            logger.warning(f"记录失败信息失败: {e}")
