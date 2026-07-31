"""历史数据回填 (PRD §6.1 D4: 上市以来全部可得数据)

PRD §6.1 D4 硬约束: "沪深日线历史目标范围: 上市以来全部可得数据"
PRD §7.2 分阶段: 最小可用初始化(近5年) → 其余历史回填

本模块实现"其余历史回填"阶段:
- 读取每只股票的上市日期 (listing_date)
- 从上市日期起抓取全部 price_daily (raw + qfq)
- 同步回填 dividends (baostock, 含送股/转增)
- 记录任务日志到 job_logs

设计原则:
- 幂等: 重复运行不会产生重复数据 (DELETE + INSERT per stock)
- 可中断: 进程中断后重新运行会跳过已完成的股票
- 可追踪: 进度写入 job_logs, 失败写入 retry_list
"""

from __future__ import annotations

import logging
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.job_status import aggregate_job_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# A股市场最早数据年份 (沪市 1990-12-19 开业)
_A_SHARE_EPOCH_DATE = "1990-01-01"


class PriceBackfiller:
    """历史价格回填执行器

    将 price_daily_raw / price_daily_qfq 从"近5年"扩展到"上市以来全部可得数据"。
    同时回填 dividends 表的送股/转增字段 (baostock 源)。
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
            raise PathIsolationError("PriceBackfiller requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("PriceBackfiller stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.adapter_mgr = adapter_mgr or AdapterManager()
        self.duck = duck
        self.sqlite = sqlite
        self._batch_id = str(uuid.uuid4())

    def run_full_backfill(
        self,
        skip_if_complete: bool = True,
        max_stocks: int = 0,
        fetch_dividends: bool = True,
        fetch_xdxr: bool = True,
    ) -> dict[str, Any]:
        """执行全市场价格历史回填

        Args:
            skip_if_complete: 跳过已有充足历史的股票 (earliest_price <= listing_date + 30天)
            max_stocks: 最多处理N只股票 (0=全部)
            fetch_dividends: 是否同时回填 dividends 送股/转增

        Returns:
            回填摘要报告
        """
        logger.info("=" * 60)
        logger.info("开始历史价格回填 (PRD §6.1 D4: 上市以来全部可得数据)")
        logger.info("=" * 60)

        job_id = self._log_job_start("price_backfill")

        report: dict[str, Any] = {
            "batch_id": self._batch_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "steps": {},
        }

        try:
            report["steps"]["prices"] = self._backfill_prices(
                skip_if_complete=skip_if_complete,
                max_stocks=max_stocks,
            )

            if fetch_dividends:
                report["steps"]["dividends"] = self._backfill_dividends(
                    max_stocks=max_stocks,
                )
            if fetch_xdxr:
                report["steps"]["corporate_actions"] = self._backfill_xdxr(
                    max_stocks=max_stocks,
                )

            report["status"] = aggregate_job_status(report["steps"])

        except Exception as e:
            logger.error(f"回填失败: {e}", exc_info=True)
            report["status"] = "failed"
            report["error"] = str(e)

        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._log_job_finish(job_id, report["status"], report)

        logger.info("=" * 60)
        logger.info(f"回填完成: {report['status']}")
        logger.info(f"摘要: {report}")
        logger.info("=" * 60)

        return report

    def _backfill_prices(
        self,
        skip_if_complete: bool = True,
        max_stocks: int = 0,
    ) -> dict[str, Any]:
        """回填 price_daily_raw + price_daily_qfq"""
        logger.info("[价格回填] 开始...")

        stocks = self.duck.read_query(
            """SELECT stock_code, listing_date, exchange FROM stock_meta
               WHERE is_listed IS TRUE ORDER BY stock_code"""
        )
        if not stocks:
            return {"status": "skipped", "reason": "无股票数据"}

        total = len(stocks)
        if max_stocks > 0:
            stocks = stocks[:max_stocks]
            total = len(stocks)

        end_date = datetime.now().strftime("%Y-%m-%d")

        success = 0
        skipped = 0
        failed = 0
        failed_codes: list[str] = []

        for i, stock in enumerate(stocks):
            code = stock["stock_code"]
            listing_date = stock.get("listing_date")
            listing_str = str(listing_date) if listing_date else None

            if (i + 1) % 50 == 0:
                logger.info(
                    f"  价格回填进度: {i + 1}/{total} "
                    f"(成功 {success}, 跳过 {skipped}, 失败 {failed})"
                )

            # Only an exchange-sourced listing date may constrain the request.
            # Unknown metadata uses the market epoch without creating a date.
            start_date = listing_str or _A_SHARE_EPOCH_DATE

            # 跳过逻辑: 如果已有数据且最早日期 <= listing_date + 30天, 说明已完整
            if skip_if_complete and listing_str:
                existing = self.duck.read_query(
                    "SELECT MIN(trade_date) as earliest FROM price_daily_raw WHERE stock_code = ?",
                    [code],
                )
                earliest = (
                    existing[0]["earliest"] if existing and existing[0]["earliest"] else None
                )
                if earliest and str(earliest) <= listing_str:
                    skipped += 1
                    continue

            # 抓取 raw
            raw_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=start_date,
                end_date=end_date,
                adjust="raw",
            ))

            # 抓取 qfq
            qfq_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily",
                stock_codes=[code],
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ))

            if raw_result.metadata.error or not raw_result.data:
                failed += 1
                failed_codes.append(code)
                self._record_failure(
                    code, "price_daily",
                    raw_result.metadata.source,
                    raw_result.metadata.error or "empty result",
                )
                continue

            if qfq_result.metadata.error or not qfq_result.data:
                failed += 1
                failed_codes.append(code)
                self._record_failure(
                    code,
                    "price_daily",
                    qfq_result.metadata.source,
                    qfq_result.metadata.error or "empty result",
                    extra_json='{"adjust": "qfq"}',
                )
                continue

            # 写入 DuckDB (DELETE + INSERT)
            try:
                with self.duck.transaction() as conn:
                    # Raw and qfq data are upserted so a partial adapter response
                    # cannot erase dates or fields already verified locally.
                    conn.executemany(
                        """INSERT INTO price_daily_raw
                           (stock_code, trade_date, open, high, low, close,
                            volume, turnover, turnover_rate)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                             open=COALESCE(excluded.open, price_daily_raw.open),
                             high=COALESCE(excluded.high, price_daily_raw.high),
                             low=COALESCE(excluded.low, price_daily_raw.low),
                             close=COALESCE(excluded.close, price_daily_raw.close),
                             volume=COALESCE(excluded.volume, price_daily_raw.volume),
                             turnover=COALESCE(excluded.turnover, price_daily_raw.turnover),
                             turnover_rate=COALESCE(excluded.turnover_rate, price_daily_raw.turnover_rate)""",
                        [(
                            code,
                            r.get("trade_date"),
                            r.get("open"),
                            r.get("high"),
                            r.get("low"),
                            r.get("close"),
                            r.get("volume"),
                            r.get("turnover"),
                            r.get("turnover_rate"),
                        ) for r in raw_result.data],
                    )

                    # qfq
                    if qfq_result.data:
                        conn.executemany(
                            """INSERT INTO price_daily_qfq
                               (stock_code, trade_date, open, high, low, close,
                                volume, turnover, turnover_rate)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                                 open=COALESCE(excluded.open, price_daily_qfq.open),
                                 high=COALESCE(excluded.high, price_daily_qfq.high),
                                 low=COALESCE(excluded.low, price_daily_qfq.low),
                                 close=COALESCE(excluded.close, price_daily_qfq.close),
                                 volume=COALESCE(excluded.volume, price_daily_qfq.volume),
                                 turnover=COALESCE(excluded.turnover, price_daily_qfq.turnover),
                                 turnover_rate=COALESCE(excluded.turnover_rate, price_daily_qfq.turnover_rate)""",
                            [(
                                code,
                                r.get("trade_date"),
                                r.get("open"),
                                r.get("high"),
                                r.get("low"),
                                r.get("close"),
                                r.get("volume"),
                                r.get("turnover"),
                                r.get("turnover_rate"),
                            ) for r in qfq_result.data],
                        )
                    from app.core.init import DataInitializer

                    lineage = DataInitializer.__new__(DataInitializer)
                    lineage._batch_id = self._batch_id
                    raw_batch_id = lineage._record_batch_in_connection(
                        conn, raw_result, "price_daily_raw", len(raw_result.data)
                    )
                    lineage._record_field_audit_in_connection(
                        conn, raw_result, raw_result.data, code, "trade_date", raw_batch_id
                    )
                    qfq_batch_id = lineage._record_batch_in_connection(
                        conn, qfq_result, "price_daily_qfq", len(qfq_result.data)
                    )
                    lineage._record_field_audit_in_connection(
                        conn, qfq_result, qfq_result.data, code, "trade_date", qfq_batch_id
                    )

                success += 1

            except Exception as e:
                failed += 1
                failed_codes.append(code)
                self._record_failure(code, "price_daily", "duckdb", str(e))
                logger.error(f"  写入 {code} 价格失败: {e}")

        logger.info(
            f"[价格回填] 完成: 成功 {success}, 跳过 {skipped}, 失败 {failed}"
        )
        return {
            "status": "success" if failed == 0 else "partial",
            "total": total,
            "success": success,
            "skipped": skipped,
            "failed": failed,
            "failed_codes": failed_codes[:50],
        }

    def _backfill_dividends(self, max_stocks: int = 0) -> dict[str, Any]:
        """回填 dividends 表 (baostock 源, 补送股/转增)

        策略: 对每只股票, 用 baostock 重新抓取分红记录,
        INSERT OR REPLACE 覆盖已有 CSMAR 行 (以 ex_date 为 PK)。
        baostock 的 ex_date 是真实除权除息日, 会修正 CSMAR 的年末占位。
        """
        logger.info("[分红回填] 开始 (baostock 源, 补送股/转增)...")

        stocks = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        if not stocks:
            return {"status": "skipped", "reason": "无股票数据"}

        total = len(stocks)
        if max_stocks > 0:
            stocks = stocks[:max_stocks]
            total = len(stocks)

        success = 0
        failed = 0
        missing = 0
        total_rows = 0

        for i, stock in enumerate(stocks):
            code = stock["stock_code"]

            if (i + 1) % 100 == 0:
                logger.info(
                    f"  分红回填进度: {i + 1}/{total} (成功 {success}, 失败 {failed})"
                )

            result = self.adapter_mgr.fetch(FetchRequest(
                data_type="dividends",
                stock_codes=[code],
                start_date=_A_SHARE_EPOCH_DATE,
            ))

            if result.metadata.error or not result.data:
                failed += 1
                self._record_failure(
                    code,
                    "dividends",
                    result.metadata.source,
                    result.metadata.error or "empty result",
                )
                continue

            valid_rows = [row for row in result.data if row.get("ex_date") is not None]
            missing_rows = len(result.data) - len(valid_rows)
            if missing_rows:
                missing += 1
                self._record_missing(code, "dividends", "missing_ex_date")

            if not valid_rows:
                continue

            try:
                with self.duck.transaction() as conn:
                    conn.executemany(
                        """INSERT INTO dividends
                           (stock_code, ex_date, announcement_date,
                            dividend_per_share, stock_dividend, transfer_share,
                            rights_issue, rights_issue_price)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(stock_code, ex_date) DO UPDATE SET
                             announcement_date=COALESCE(excluded.announcement_date, dividends.announcement_date),
                             dividend_per_share=COALESCE(excluded.dividend_per_share, dividends.dividend_per_share),
                             stock_dividend=COALESCE(excluded.stock_dividend, dividends.stock_dividend),
                             transfer_share=COALESCE(excluded.transfer_share, dividends.transfer_share),
                             rights_issue=COALESCE(excluded.rights_issue, dividends.rights_issue),
                             rights_issue_price=COALESCE(excluded.rights_issue_price, dividends.rights_issue_price)""",
                        [
                            (
                                code,
                                row["ex_date"],
                                row.get("announcement_date"),
                                row.get("dividend_per_share"),
                                row.get("stock_dividend"),
                                row.get("transfer_share"),
                                row.get("rights_issue"),
                                row.get("rights_issue_price"),
                            )
                            for row in valid_rows
                        ],
                    )
                    from app.core.init import DataInitializer

                    lineage = DataInitializer.__new__(DataInitializer)
                    lineage._batch_id = self._batch_id
                    batch_id = lineage._record_batch_in_connection(
                        conn, result, "dividends", len(valid_rows)
                    )
                    lineage._record_field_audit_in_connection(
                        conn, result, valid_rows, code, "ex_date", batch_id
                    )
                success += 1
                total_rows += len(valid_rows)

            except duckdb.Error as e:
                failed += 1
                self._record_failure(code, "dividends", "duckdb", str(e))
                logger.error(f"  写入 {code} 分红失败: {e}")

        logger.info(
            f"[分红回填] 完成: 成功 {success}, 失败 {failed}, 总行数 {total_rows}"
        )
        return {
            "status": (
                "failed"
                if failed == total and success == 0
                else "partial"
                if failed > 0 or missing > 0
                else "success"
            ),
            "total": total,
            "success": success,
            "failed": failed,
            "missing": missing,
            "total_rows": total_rows,
        }

    def _backfill_xdxr(self, max_stocks: int = 0) -> dict[str, Any]:
        """Backfill TDX corporate actions without replacing verified local history."""
        from app.core.init import DataInitializer

        stocks = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        if max_stocks > 0:
            stocks = stocks[:max_stocks]
        if not stocks:
            return {"status": "skipped", "reason": "no_listed_stocks"}

        success = failed = rows_written = 0
        for stock in stocks:
            code = stock["stock_code"]
            result = self.adapter_mgr.fetch(FetchRequest(data_type="xdxr", stock_codes=[code]))
            if result.metadata.error or not result.data:
                failed += 1
                self._record_failure(code, "xdxr", result.metadata.source, result.metadata.error or "empty result")
                self._record_missing(code, "xdxr", "source_unavailable")
                continue
            try:
                with self.duck.transaction() as conn:
                    DataInitializer._upsert_xdxr_rows(conn, code, result.data)
                    lineage = DataInitializer.__new__(DataInitializer)
                    lineage._batch_id = self._batch_id
                    batch_id = lineage._record_batch_in_connection(conn, result, "xdxr", len(result.data))
                    lineage._record_field_audit_in_connection(
                        conn, result, result.data, code, "event_date", batch_id
                    )
                success += 1
                rows_written += len(result.data)
            except Exception as error:
                failed += 1
                self._record_failure(code, "xdxr", "duckdb", str(error))
        return {
            "status": "success" if failed == 0 else "partial",
            "total": len(stocks),
            "success": success,
            "failed": failed,
            "rows_written": rows_written,
        }

    def _record_missing(
        self,
        stock_code: str,
        field_name: str,
        reason_code: str,
    ) -> None:
        """Record source rows that cannot be safely promoted into formal data."""
        with self.sqlite.transaction() as connection:
            connection.execute(
                """
                INSERT INTO missing_list (stock_code, field_name, reason_code)
                VALUES (?, ?, ?)
                """,
                [stock_code, field_name, reason_code],
            )

    def _record_batch(self, result: Any, data_type: str, row_count: int) -> None:
        """记录批次溯源"""
        if row_count > 0:
            if not result.raw_response:
                raise ValueError(f"{data_type} has rows but no source material")
            if hashlib.sha256(result.raw_response).hexdigest() != result.metadata.raw_response_hash:
                raise ValueError(f"{data_type} source material hash mismatch")
        try:
            with self.duck.write_connection() as conn:
                conn.execute(
                    """INSERT INTO fetch_batch
                       (batch_id, data_type, source, adapter_version,
                        fetch_time, raw_response_hash, row_count, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        self._batch_id,
                        data_type,
                        result.metadata.source,
                        result.metadata.api_version or "unknown",
                        result.metadata.fetch_time,
                        result.metadata.raw_response_hash,
                        row_count,
                        result.metadata.confidence,
                    ],
                )
                conn.execute(
                    """INSERT INTO raw_response_archive
                       (raw_response_hash, source, fetch_time, payload, api_version, integrity_verified)
                       VALUES (?, ?, ?, ?, ?, TRUE)
                       ON CONFLICT(raw_response_hash) DO NOTHING""",
                    [
                        result.metadata.raw_response_hash,
                        result.metadata.source,
                        result.metadata.fetch_time,
                        result.raw_response,
                        result.metadata.api_version,
                    ],
                )
        except Exception as e:
            logger.warning(f"记录批次溯源失败: {e}")
            raise

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
                    [
                        stock_code,
                        data_type,
                        adapter,
                        error[:500],
                        datetime.now(timezone.utc).isoformat(),
                        extra_json,
                    ],
                )
        except Exception as e:
            logger.warning(f"记录失败信息失败: {e}")

    def _log_job_start(self, job_type: str) -> int:
        """记录任务开始"""
        with self.sqlite.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO job_logs (job_type, status) VALUES (?, 'running')",
                [job_type],
            )
            return cursor.lastrowid or 0

    def _log_job_finish(self, job_id: int, status: str, details: dict) -> None:
        """记录任务完成"""
        import json

        with self.sqlite.transaction() as conn:
            conn.execute(
                """UPDATE job_logs SET status=?, finished_at=?, details_json=? WHERE id=?""",
                [
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(details, ensure_ascii=False, default=str)[:5000],
                    job_id,
                ],
            )
