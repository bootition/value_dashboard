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
            refreshed_codes = set(financials["succeeded_codes"])
            for code, announcements in announcement_check["affected_announcements"].items():
                if code in refreshed_codes:
                    self._mark_announcements_seen(code, announcements)
                else:
                    self._record_failure(
                        code,
                        "announcements",
                        "cninfo",
                        "financial refresh failed; announcement remains pending",
                        extra_json=json.dumps({"announcement_ids": [item["announcement_id"] for item in announcements]}),
                    )

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

    def _check_new_announcements(self, *, persist: bool = True) -> dict[str, Any]:
        """Compare remote announcement IDs without mutating during a check-only run."""
        try:
            stocks = self.duck.read_query(
                "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
            )
        except Exception as error:
            return {"status": "unavailable", "checked_remote": False, "error": str(error)}
        if not stocks:
            return {"status": "available", "checked_remote": True, "affected_stock_codes": []}
        affected: set[str] = set()
        errors: list[str] = []
        affected_announcements: dict[str, list[dict[str, Any]]] = {}
        for stock in stocks:
            code = stock["stock_code"]
            result = self.adapter_mgr.fetch(FetchRequest(data_type="announcements", stock_codes=[code]))
            if result.metadata.error:
                errors.append(f"{code}: {result.metadata.error}")
                if persist:
                    self._record_failure(code, "announcements", result.metadata.source, result.metadata.error)
                continue
            announcements = [item for item in result.data if item.get("announcement_id")]
            if not announcements:
                continue
            for item in announcements:
                seen = self.sqlite.query(
                    "SELECT 1 FROM announcement_registry WHERE announcement_id = ?",
                    [item["announcement_id"]],
                )
                if not seen:
                    affected.add(code)
                    affected_announcements.setdefault(code, []).append(item)
        return {
            "status": "available" if not errors else "partial",
            "checked_remote": True,
            "affected_stock_codes": sorted(affected),
            "affected_announcements": affected_announcements,
            "errors": errors[:20],
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
        """Refetch all three core statements for stocks with newly registered filings."""
        succeeded = 0
        succeeded_codes: list[str] = []
        failed: list[str] = []
        for code in stock_codes:
            outcomes = [self.refetch_one(code, data_type) for data_type in ("balance_sheet", "income_statement", "cash_flow")]
            if all(outcome["status"] == "success" for outcome in outcomes):
                succeeded += 1
                succeeded_codes.append(code)
            else:
                failed.append(code)
        return {
            "status": "success" if not failed else "partial",
            "total": len(stock_codes), "success": succeeded, "failed": len(failed),
            "failed_codes": failed[:20], "succeeded_codes": succeeded_codes,
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

    def _update_prices_incremental(self, max_stocks: int) -> dict:
        """增量更新价格——只更新有新数据的股票"""
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

        success_count = 0
        fail_count = 0
        all_rows = []
        all_qfq_rows = []
        successful_fetches: list[tuple[str, str, Any]] = []

        for i, stock in enumerate(target_stocks):
            code = stock["stock_code"]
            if (i + 1) % 100 == 0:
                logger.info(f"  增量价格进度: {i+1}/{len(target_stocks)}")

            raw_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=start_date,
                end_date=end_date,
                adjust="raw",
            ))
            qfq_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=start_date,
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

    def refetch_one(self, stock_code: str, data_type: str) -> dict[str, Any]:
        """Refetch one supported dataset through the same durable write paths as init."""
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
