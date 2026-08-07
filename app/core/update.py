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
import uuid
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
        self.sqlite.execute(
            """UPDATE job_logs SET status = ?, finished_at = ?, details_json = ?
               WHERE id = ?""",
            [
                "success" if status == "success" else "failed",
                datetime.now(timezone.utc).isoformat(),
                json.dumps(details, ensure_ascii=False, default=str),
                job_row_id,
            ],
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

        # 3. 重试失败任务
        if check_report["retry_tasks"]:
            report_step("retries", self._retry_failed_tasks(check_report["retry_tasks"]))

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
                          OR raw.latest_raw_date < ? OR qfq.latest_qfq_date < ?)
                   ORDER BY stock.stock_code""",
                [target_date, target_date],
            )
        except Exception:
            return {"status": "skipped", "reason": "no_stock_meta"}

        if not stocks:
            logger.info("[增量] 所有非停牌股票价格已是最新，无需更新")
            return {"status": "skipped", "reason": "prices_up_to_date"}

        target_stocks = stocks
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
        for i, stock in enumerate(target_stocks):
            code = stock["stock_code"]
            if (i + 1) % 100 == 0:
                logger.info(f"  增量价格进度: {i+1}/{len(target_stocks)}")
            if detail_cb is not None:
                detail_cb("price", {
                    "done": i + 1,
                    "total": len(target_stocks),
                    "current": code,
                    "label": "股票价格",
                })

            need_qfq_full = code in xdxr_codes
            # qfq 全量重拉的股票：从适配器支持的起点拉全历史；
            # raw 在数据陈旧时同样全量。
            raw_latest = str(stock.get("latest_raw_date") or start_date)
            qfq_latest = str(stock.get("latest_qfq_date") or start_date)
            fetch_start = None if (need_qfq_full or raw_full_refetch) else min(raw_latest, qfq_latest)

            raw_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=fetch_start,
                end_date=end_date,
                adjust="raw",
            ))
            qfq_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=fetch_start,
                end_date=end_date,
                adjust="qfq",
            ))

            if raw_result.metadata.error or not raw_result.data:
                fail_count += 1
                self._record_failure(code, "price_daily", raw_result.metadata.source,
                                      raw_result.metadata.error or "empty")
                continue
            if qfq_result.metadata.error or not qfq_result.data:
                fail_count += 1
                self._record_failure(
                    code, "price_daily", qfq_result.metadata.source,
                    qfq_result.metadata.error or "empty", extra_json='{"adjust":"qfq"}',
                )
                continue

            try:
                self._persist_incremental_price_pair(code, raw_result, qfq_result)
                success_count += 1
                updated_codes.append(code)
            except Exception as e:
                fail_count += 1
                self._record_failure(code, "price_daily", raw_result.metadata.source, str(e))

        logger.info(f"[增量] 价格更新完成: 成功 {success_count}, 失败 {fail_count}")
        return {
            "status": "success" if fail_count == 0 else "partial",
            "total": len(target_stocks),
            "success": success_count,
            "failed": fail_count,
            "xdxr_full_refetch": len(xdxr_codes),
            "raw_full_refetch": raw_full_refetch,
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

    def _persist_incremental_price_pair(self, stock_code: str, raw_result: Any, qfq_result: Any) -> None:
        """Commit one stock's raw/qfq rows and lineage atomically for restart safety."""
        from app.core.init import DataInitializer

        lineage = DataInitializer.__new__(DataInitializer)
        lineage._batch_id = str(uuid.uuid4())
        with self.duck.transaction() as connection:
            for table, data_type, result in (
                ("price_daily_raw", "price_daily_raw", raw_result),
                ("price_daily_qfq", "price_daily_qfq", qfq_result),
            ):
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
                lineage._record_field_audit_in_connection(
                    connection, result, result.data, stock_code, "trade_date", batch_id
                )

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

            # 重新抓取
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type=data_type,
                stock_codes=[stock_code],
                adjust=adjust,
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
