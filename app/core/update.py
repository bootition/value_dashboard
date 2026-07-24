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
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.job_status import aggregate_job_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class IncrementalUpdater:
    """增量检查与更新执行器

    PRD §7.3: 每次启动只进行简单增量检查。
    PRD §7.4: 失败时保留旧值，生成重试列表与缺失列表。
    """

    def __init__(self) -> None:
        self.adapter_mgr = AdapterManager()
        self.duck = DuckDBStore()
        self.sqlite = SQLiteStore()

    def run_incremental_check(self) -> dict[str, Any]:
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
        report["announcement_check"] = self._check_new_announcements()

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
        )

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

        # 1. 更新交易日历（如果有新的）
        if check_report["new_trading_days"]:
            step = self._update_trading_dates()
            report["steps"]["trading_dates"] = step

        # 2. 增量更新价格（只更新有新交易日的股票）
        step = self._update_prices_incremental(max_stocks)
        report["steps"]["prices"] = step

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

    def _check_new_announcements(self) -> dict[str, Any]:
        """Report that remote announcement comparison is not implemented."""
        return {
            "status": "not_implemented",
            "checked_remote": False,
            "note": (
                "公告或财报差异检测尚未接入权威披露源检查点；"
                "当前结果不能证明本地财务数据已是最新。"
            ),
        }

    def _check_retry_tasks(self) -> list[dict]:
        """读取待重试任务列表"""
        try:
            rows = self.sqlite.query(
                "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json "
                "FROM retry_list ORDER BY last_attempt ASC LIMIT 100"
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
            stocks = self.duck.read_query("SELECT stock_code FROM stock_meta")
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

        for i, stock in enumerate(target_stocks):
            code = stock["stock_code"]
            if (i + 1) % 100 == 0:
                logger.info(f"  增量价格进度: {i+1}/{len(target_stocks)}")

            # 只抓取raw（增量更新时qfq可以后续补）
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=start_date,
                end_date=end_date,
                adjust="raw",
            ))

            if result.metadata.error or not result.data:
                fail_count += 1
                self._record_failure(code, "price_daily", result.metadata.source,
                                      result.metadata.error or "empty")
                continue

            try:
                with self.duck.write_connection() as conn:
                    # 增量插入（不删除旧数据，只添加新日期）
                    for row in result.data:
                        conn.execute(
                            """INSERT OR REPLACE INTO price_daily_raw
                               (stock_code, trade_date, open, high, low, close, volume, turnover)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            [code, row.get("trade_date"), row.get("open"),
                             row.get("high"), row.get("low"), row.get("close"),
                             row.get("volume"), row.get("turnover")],
                        )
                success_count += 1
            except Exception as e:
                fail_count += 1
                self._record_failure(code, "price_daily", "duckdb", str(e))

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
                still_failing += 1
                self._mark_retry_failed(retry_id, "unsupported retry persistence target")
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
                    self._persist_price_retry(stock_code, adjust, result.data)
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
                f"""INSERT OR REPLACE INTO {table}
                    (stock_code, trade_date, open, high, low, close, volume, turnover,
                     turnover_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    def _mark_retry_failed(self, retry_id: int, error: str) -> None:
        """Retain a failed retry and update its diagnostic state."""
        self.sqlite.execute(
            """UPDATE retry_list
               SET retry_count = retry_count + 1, error = ?, last_attempt = ?
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
                    """INSERT OR REPLACE INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt,
                        extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, ?)""",
                    [stock_code, data_type, adapter, error[:500],
                     datetime.now(timezone.utc).isoformat(), extra_json],
                )
        except Exception as e:
            logger.warning(f"记录失败信息失败: {e}")
