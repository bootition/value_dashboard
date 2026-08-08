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

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.job_status import aggregate_job_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


# ─── 公告分类（PRD §7.7）───────────────────────────────────────────
# 只有定期报告/业绩预告/业绩快报类公告触发财务刷新；
# 其他公告只登记入册，不进入财务刷新队列。

_ANNOUNCEMENT_FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "年度报告", "半年度报告", "一季报", "第一季度报告", "三季报", "第三季度报告",
    "业绩预告", "业绩快报",
)
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
        # CSRC 行业低频刷新间隔（天）：行业归属变化极低，避免每次更新
        # 逐股查询 CNINFO（约 1.5s/股）占用数小时（默认 30 天）
        self.csrc_refresh_interval_days: int = self._load_update_config(
            "csrc_refresh_interval_days", default=30
        )
        # 价格批量抓取的并发网络请求数（HTTP 源；socket 源内部强制串行）
        self.price_fetch_concurrency: int = self._load_update_config(
            "price_fetch_concurrency", default=8
        )
        # 批量持久化：每批合并到单个 DuckDB 事务，减少 WAL 提交次数
        self.price_fetch_batch_size: int = self._load_update_config(
            "price_fetch_batch_size", default=20
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
    def _load_update_config(key: str, *, default: int) -> int:
        try:
            from app.core.config import Config
            cfg = Config.current()
            update_cfg = cfg["update"] if "update" in cfg else {}
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
            "checked_at": datetime.now(timezone.utc).isoformat(),
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
                    return self._run_incremental_update_locked(max_stocks, progress_cb, detail_cb)
                finally:
                    close = getattr(self.adapter_mgr, "close", None)
                    if callable(close):
                        close()
        except UpdateLockError:
            logger.warning("增量更新被跨进程锁拒绝：另一更新正在运行")
            return {
                "status": "skipped",
                "reason": "another_update_running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }

    def _reconcile_crashed_incremental_jobs(self) -> None:
        """Close jobs abandoned by the dead process that owned the update lock."""
        finished_at = datetime.now(timezone.utc).isoformat()
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
        started_at = datetime.now(timezone.utc).isoformat()
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
        """结束增量更新作业（success 之外一律记 failed，供状态页只读展示）。"""
        finished_at = datetime.now(timezone.utc).isoformat()
        self.sqlite.execute(
            """UPDATE job_logs SET status = ?, finished_at = ?, details_json = ?
               WHERE id = ?""",
            [
                "success" if status == "success" else "failed",
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
                        updated_price_codes
                    ),
                )
            report["status"] = aggregate_job_status(report["steps"])
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
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

        announcement_check = self._check_new_announcements(persist=True)
        report["check"]["announcement_check"] = announcement_check
        announcement_codes = announcement_check.get("affected_stock_codes", [])
        financials: dict[str, Any] | None = None
        if announcement_codes:
            financials = self._refresh_financials(announcement_codes)
            report_step("financials", financials)
            actions = self._refresh_market_actions(announcement_codes)
            report_step("market_actions", actions)
            # 只有真正写入新报告期的股票才登记公告；
            # 数据源延迟（pending）或刷新失败（failed）保持公告 pending 并记录重试
            refreshed_codes = set(financials["succeeded_codes"])
            for code, announcements in announcement_check["affected_announcements"].items():
                if code in refreshed_codes:
                    self._mark_announcements_seen(code, announcements)
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

        financial_step = report["steps"].get("financials", {"status": "success"})
        financial_refreshed = bool(financials and financials.get("success", 0) > 0)
        if (financial_refreshed or share_capital_changed) and financial_step["status"] == "success":
            from app.core.indicators.calculator import IndicatorCalculator

            snapshot_step = IndicatorCalculator(duck=self.duck, sqlite=self.sqlite).compute_snapshot_for_all()
            report_step("indicators", snapshot_step)
            if snapshot_step["status"] != "success":
                snapshot_step["reason"] = "price update retained; snapshot publication not ready"
        elif updated_price_codes and financial_step["status"] == "success":
            from app.core.indicators.calculator import IndicatorCalculator

            snapshot_step = IndicatorCalculator(
                duck=self.duck, sqlite=self.sqlite,
            ).compute_snapshot_for_codes(updated_price_codes)
            report_step("indicators", snapshot_step)

        # 3. 重试失败任务（先清理已达标的历史冗余条目，避免全历史重抓）
        if check_report["retry_tasks"]:
            expected_date = self._latest_expected_trading_date(
                datetime.now().strftime("%Y-%m-%d")
            )
            self._cleanup_redundant_retries(expected_date)
            refreshed_tasks = self._check_retry_tasks()
            if refreshed_tasks:
                report_step("retries", self._retry_failed_tasks(refreshed_tasks))

        report["status"] = aggregate_job_status(report["steps"])
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(f"增量更新完成: {report['status']}")
        return report

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

        if self._refresh_due("listing_info_last_refresh", self.universe_refresh_interval_days):
            try:
                steps["listing_info"] = initializer._fetch_listing_info()
                if steps["listing_info"].get("status") == "success":
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

        if self._csrc_refresh_due():
            try:
                steps["csrc_industry"] = initializer._fetch_csrc_industry()
                if steps["csrc_industry"].get("status") in {"success", "partial"}:
                    self._mark_refreshed("csrc_industry_last_refresh")
            except Exception as error:
                logger.warning("CSRC 行业刷新失败: %s", error)
                steps["csrc_industry"] = {"status": "failed", "error": str(error)}
        else:
            steps["csrc_industry"] = {
                "status": "skipped",
                "reason": "refreshed_within_interval",
                "interval_days": self.csrc_refresh_interval_days,
            }

        statuses = [step.get("status") for step in steps.values()]
        return {
            "status": (
                "success"
                if all(status == "success" for status in statuses)
                else "skipped"
                if all(status == "skipped" for status in statuses)
                else "partial"
            ),
            "steps": steps,
        }

    def _csrc_refresh_due(self) -> bool:
        """Return whether the CSRC industry refresh interval has elapsed."""
        return self._refresh_due("csrc_industry_last_refresh", self.csrc_refresh_interval_days)

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
        return (datetime.now(timezone.utc).date() - last_date).days >= interval_days

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
                [key, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()],
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

    def _check_new_announcements(
        self,
        *,
        persist: bool = True,
        lookback_days: int = 3,
    ) -> dict[str, Any]:
        """Compare remote announcement IDs without mutating during a check-only run.

        全市场按日期段批量查询（PRD §7.7），替代逐股轮询：
        - CNINFO 全市场接口一次查询最近 N 天公告（分 sse/szse/bj 三板块）
        - 与本地 announcement_registry 比对，找出未见过的公告
        - 财务类公告进入 affected_stock_codes（触发财务刷新）
        - 非财务类公告也返回，供登记但不触发财务刷新
        """
        end_date = datetime.now().strftime("%Y-%m-%d")
        lookback = lookback_days if lookback_days else self.announcement_lookback_days
        start_date = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")
        try:
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type="announcements",
                start_date=start_date,
                end_date=end_date,
            ))
        except Exception as error:
            return {"status": "unavailable", "checked_remote": False, "error": str(error)}

        if result.metadata.error:
            if persist:
                self._record_failure("", "announcements", result.metadata.source, result.metadata.error)
            return {
                "status": (
                    "unavailable" if "all_boards_failed" in (result.metadata.error or "")
                    else "partial"
                ),
                "checked_remote": True,
                "affected_stock_codes": [],
                "affected_announcements": {},
                "all_new_announcements": {},
                "errors": [result.metadata.error],
            }

        affected: set[str] = set()
        affected_announcements: dict[str, list[dict[str, Any]]] = {}
        for item in result.data:
            announcement_id = item.get("announcement_id")
            stock_code = item.get("stock_code")
            if not announcement_id or not stock_code:
                continue
            seen = self.sqlite.query(
                "SELECT 1 FROM announcement_registry WHERE announcement_id = ?",
                [announcement_id],
            )
            if not seen:
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

        return {
            "status": "available",
            "checked_remote": True,
            "affected_stock_codes": sorted(financial_codes),
            "affected_announcements": financial_announcements,
            "all_new_announcements": {
                code: affected_announcements[code] for code in sorted(affected)
            },
            "errors": [],
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

    def _refresh_financials(self, stock_codes: list[str]) -> dict[str, Any]:
        """Refresh core statements for stocks with newly registered filings.

        增量模式（PRD §7.7）：
        - 只拉最新报告期，本地已有则跳过（skipped）
        - 数据源无新报告期（财报延迟）时返回 pending_codes，
          由调用方保持公告 pending 并在下次启动重试
        """
        succeeded = 0
        succeeded_codes: list[str] = []
        pending_codes: list[str] = []
        failed: list[str] = []
        for code in stock_codes:
            outcomes = [
                self.refetch_one(code, data_type, incremental=True)
                for data_type in ("balance_sheet", "income_statement", "cash_flow")
            ]
            if all(outcome["status"] == "success" for outcome in outcomes):
                if all(outcome.get("skipped") for outcome in outcomes):
                    pending_codes.append(code)
                else:
                    succeeded += 1
                    succeeded_codes.append(code)
            else:
                failed.append(code)
        return {
            "status": "success" if not failed else "partial",
            "total": len(stock_codes), "success": succeeded, "failed": len(failed),
            "failed_codes": failed[:20], "succeeded_codes": succeeded_codes,
            "pending_codes": pending_codes[:20],
        }

    def _refresh_market_actions(self, stock_codes: list[str]) -> dict[str, Any]:
        """Refresh dividend and corporate-action evidence alongside filing data."""
        succeeded = 0
        failed_codes: list[str] = []
        for code in stock_codes:
            outcomes = [self.refetch_one(code, data_type) for data_type in ("dividends", "xdxr")]
            if all(outcome["status"] == "success" for outcome in outcomes):
                succeeded += 1
            else:
                failed_codes.append(code)
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
        index = 0
        observed_concurrency = concurrency
        try:
            while index < len(target_stocks):
                window = target_stocks[index:index + max(concurrency * 4, concurrency)]
                executor = ThreadPoolExecutor(
                    max_workers=concurrency,
                    thread_name_prefix="vd-price-stock",
                )
                futures = [executor.submit(fetch_one, stock) for stock in window]
                window_penalty = False
                for future in futures:
                    code, raw_result, qfq_result, err, slow = future.result()
                    window_penalty = window_penalty or slow or err is not None
                    completed[0] += 1
                    if err is not None or raw_result is None:
                        fail_count += 1
                        self._record_failure(code, "price_daily", "manager", err or "empty")
                    else:
                        batch.append((code, raw_result, qfq_result))
                    if len(batch) >= batch_size:
                        success_count, updated_codes, fail_count = self._persist_price_batch(
                            batch, success_count, updated_codes, fail_count
                        )
                        batch = []
                    if detail_cb is not None:
                        detail_cb("price", {
                            "done": completed[0],
                            "total": len(target_stocks),
                            "current": code,
                            "label": "股票价格",
                        })
                executor.shutdown(wait=True, cancel_futures=True)
                index += len(window)
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
                    observed_concurrency = max(observed_concurrency, concurrency)
            if batch:
                success_count, updated_codes, fail_count = self._persist_price_batch(
                    batch, success_count, updated_codes, fail_count
                )
        finally:
            request_executor.shutdown(wait=False, cancel_futures=True)

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

    def _persist_incremental_price_pair(self, stock_code: str, raw_result: Any, qfq_result: Any) -> None:
        """Commit one stock's raw/qfq rows and lineage atomically for restart safety."""
        with self.duck.transaction() as connection:
            self._persist_price_pair_in_connection(
                connection, stock_code, raw_result, qfq_result,
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

    def _retry_failed_tasks(self, tasks: list[dict]) -> dict:
        """重试失败任务"""
        logger.info(f"[增量] 重试 {len(tasks)} 个失败任务...")

        success_count = 0
        still_failing = 0

        for task in tasks:
            retry_id = task["id"]
            stock_code = task["stock_code"]
            data_type = task["data_type"]
            if data_type != "price_daily":
                outcome = self.refetch_one(stock_code, data_type)
                if outcome["status"] == "success":
                    self.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
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
                self._persist_price_with_lineage(stock_code, "raw", raw)
                self._persist_price_with_lineage(stock_code, "qfq", qfq)
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
            else:
                failed_codes.append(code)

        return {
            "status": "success" if not failed_codes else "partial",
            "targeted": len(targets),
            "completed": completed,
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
            [error[:500], datetime.now(timezone.utc).isoformat(), retry_id],
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
                     datetime.now(timezone.utc).isoformat(), extra_json or "{}"],
                )
        except Exception as e:
            logger.warning(f"记录失败信息失败: {e}")
