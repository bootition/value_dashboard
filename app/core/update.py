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
from typing import Any

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

    def run_incremental_update(self, max_stocks: int = 0) -> dict[str, Any]:
        """执行增量更新

        Args:
            max_stocks: 最多更新的股票数量（0=全部需要更新的股票）

        Returns:
            更新报告
        """
        logger.info("=" * 60)
        logger.info("开始增量更新 (PRD §7.3)")
        logger.info("=" * 60)

        # 先检查
        check_report = self.run_incremental_check()

        report: dict[str, Any] = {
            "check": check_report,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "steps": {},
        }
        if check_report["blocked"]:
            report["steps"]["announcements"] = {
                "status": "partial",
                "reason": "authoritative announcement and financial freshness check is unavailable",
            }

        # PRD §7.7 第 4 项: 股本与上市名单（新股/退市/股本变化），
        # 以及 PRD §24 CSRC 行业分类（低频刷新）。
        report["steps"]["universe"] = self._refresh_universe_metadata()

        # 1. 更新交易日历（如果有新的）
        if check_report["new_trading_days"]:
            step = self._update_trading_dates()
            report["steps"]["trading_dates"] = step

        announcement_check = self._check_new_announcements(persist=True)
        report["check"]["announcement_check"] = announcement_check
        announcement_codes = announcement_check.get("affected_stock_codes", [])
        financials: dict[str, Any] | None = None
        if announcement_codes:
            financials = self._refresh_financials(announcement_codes)
            report["steps"]["financials"] = financials
            actions = self._refresh_market_actions(announcement_codes)
            report["steps"]["market_actions"] = actions
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

        # 2. 增量更新价格（只更新有新交易日的股票）
        step = self._update_prices_incremental(max_stocks)
        report["steps"]["prices"] = step
        financial_step = report["steps"].get("financials", {"status": "success"})
        financial_refreshed = bool(financials and financials.get("success", 0) > 0)
        if (
            (step["status"] == "success" and step.get("success", 0) > 0) or financial_refreshed
        ) and financial_step["status"] == "success":
            from app.core.indicators.calculator import IndicatorCalculator

            snapshot_step = IndicatorCalculator(duck=self.duck, sqlite=self.sqlite).compute_snapshot_for_all()
            report["steps"]["indicators"] = snapshot_step
            if snapshot_step["status"] != "success":
                snapshot_step["reason"] = "price update retained; snapshot publication not ready"

        # 3. 重试失败任务
        if check_report["retry_tasks"]:
            step = self._retry_failed_tasks(check_report["retry_tasks"])
            report["steps"]["retries"] = step

        report["status"] = aggregate_job_status(report["steps"])
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(f"增量更新完成: {report['status']}")
        return report

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

        try:
            steps["stock_list"] = initializer._fetch_stock_universe()
        except Exception as error:
            logger.warning("股票池刷新失败: %s", error)
            steps["stock_list"] = {"status": "failed", "error": str(error)}

        try:
            steps["listing_info"] = initializer._fetch_listing_info()
        except Exception as error:
            logger.warning("上市状态刷新失败: %s", error)
            steps["listing_info"] = {"status": "failed", "error": str(error)}

        if self._csrc_refresh_due():
            try:
                steps["csrc_industry"] = initializer._fetch_csrc_industry()
                if steps["csrc_industry"].get("status") == "success":
                    self._mark_csrc_refreshed()
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
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = 'csrc_industry_last_refresh'"
        )
        if not rows:
            return True
        raw = rows[0].get("value")
        try:
            last_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return True
        return (datetime.now(timezone.utc).date() - last_date).days >= self.csrc_refresh_interval_days

    def _mark_csrc_refreshed(self) -> None:
        """Persist the CSRC refresh date so later runs skip the full scan."""
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES ('csrc_industry_last_refresh', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [datetime.now(timezone.utc).date().isoformat(), datetime.now(timezone.utc).isoformat()],
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

    def _update_prices_incremental(self, max_stocks: int) -> dict:
        """增量更新价格——只更新有新数据的股票。

        - 普通股票：按 latest_local → today 增量补齐 raw + qfq
        - 发生除权除息（xdxr）的股票：qfq 全历史重拉，保证复权口径一致
        - 本地价格陈旧超过窗口上限：raw 也走全量重拉
        """
        logger.info("[增量] 检查需要价格更新的股票...")

        latest_local = self._get_latest_local_price_date()
        today = datetime.now().strftime("%Y-%m-%d")

        if latest_local and latest_local >= today:
            logger.info("[增量] 价格已是最新，无需更新")
            return {"status": "skipped", "reason": "prices_up_to_date"}

        # 获取所有有新交易日需要更新的股票
        try:
            stocks = self.duck.read_query(
                "SELECT stock_code, exchange FROM stock_meta WHERE is_listed IS TRUE"
            )
        except Exception:
            return {"status": "skipped", "reason": "no_stock_meta"}

        if not stocks:
            return {"status": "skipped", "reason": "no_stocks"}

        target_stocks = stocks
        if max_stocks > 0:
            target_stocks = target_stocks[:max_stocks]

        start_date = latest_local or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        end_date = today

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
        all_rows = []
        all_qfq_rows = []
        successful_fetches: list[tuple[str, str, Any]] = []

        for i, stock in enumerate(target_stocks):
            code = stock["stock_code"]
            if (i + 1) % 100 == 0:
                logger.info(f"  增量价格进度: {i+1}/{len(target_stocks)}")

            need_qfq_full = code in xdxr_codes
            # qfq 全量重拉的股票：从适配器支持的起点拉全历史；
            # raw 在数据陈旧时同样全量。
            fetch_start = None if (need_qfq_full or raw_full_refetch) else start_date

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

            for row in raw_result.data:
                all_rows.append([
                    code, row.get("trade_date"), row.get("open"),
                    row.get("high"), row.get("low"), row.get("close"),
                    row.get("volume"), row.get("turnover"), row.get("turnover_rate"),
                ])
            successful_fetches.append((code, "price_daily_raw", raw_result))
            for row in qfq_result.data:
                all_qfq_rows.append([
                    code, row.get("trade_date"), row.get("open"), row.get("high"),
                    row.get("low"), row.get("close"), row.get("volume"), row.get("turnover"),
                    row.get("turnover_rate"),
                ])
            successful_fetches.append((code, "price_daily_qfq", qfq_result))
            success_count += 1

        # 批量写入数据库（单次连接，单次事务）
        if all_rows:
            try:
                from app.core.init import DataInitializer

                lineage = DataInitializer.__new__(DataInitializer)
                lineage._batch_id = str(uuid.uuid4())
                with self.duck.transaction() as conn:
                    conn.executemany(
                        """INSERT INTO price_daily_raw
                           (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                             open=COALESCE(excluded.open, price_daily_raw.open),
                             high=COALESCE(excluded.high, price_daily_raw.high),
                             low=COALESCE(excluded.low, price_daily_raw.low),
                             close=COALESCE(excluded.close, price_daily_raw.close),
                             volume=COALESCE(excluded.volume, price_daily_raw.volume),
                             turnover=COALESCE(excluded.turnover, price_daily_raw.turnover),
                             turnover_rate=COALESCE(excluded.turnover_rate, price_daily_raw.turnover_rate)""",
                        all_rows,
                    )
                    if all_qfq_rows:
                        conn.executemany(
                            """INSERT INTO price_daily_qfq
                               (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                                 open=COALESCE(excluded.open, price_daily_qfq.open),
                                 high=COALESCE(excluded.high, price_daily_qfq.high),
                                 low=COALESCE(excluded.low, price_daily_qfq.low),
                                 close=COALESCE(excluded.close, price_daily_qfq.close),
                                 volume=COALESCE(excluded.volume, price_daily_qfq.volume),
                                 turnover=COALESCE(excluded.turnover, price_daily_qfq.turnover),
                                 turnover_rate=COALESCE(excluded.turnover_rate, price_daily_qfq.turnover_rate)""",
                            all_qfq_rows,
                        )
                    for code, data_type, result in successful_fetches:
                        batch_id = lineage._record_batch_in_connection(
                            conn, result, data_type, len(result.data)
                        )
                        lineage._record_field_audit_in_connection(
                            conn, result, result.data, code, "trade_date", batch_id
                        )
            except Exception as e:
                logger.error(f"批量写入价格数据失败: {e}")
                return {"status": "failed", "error": str(e)}

        logger.info(f"[增量] 价格更新完成: 成功 {success_count}, 失败 {fail_count}")
        return {
            "status": "success" if fail_count == 0 else "partial",
            "total": len(target_stocks),
            "success": success_count,
            "failed": fail_count,
            "xdxr_full_refetch": len(xdxr_codes),
            "raw_full_refetch": raw_full_refetch,
        }

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
