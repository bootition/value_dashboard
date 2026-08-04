"""最小可用初始化流程 (PRD §6.7)

初始化顺序（PRD §7.2）:
1. 当前上市股票全集 + ST/停牌/上市日期
2. 每只股票最近收盘价 + 价格日期
3. 每只股票近5年 raw + qfq 日线
4. 每只股票最小核心财务集
5. 当前申万一级/二级归属

失败处理（PRD §7.4 L4）:
- 保留旧值不以空值覆盖
- 生成重试列表
- 生成缺失列表
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, assert_never

from app.core.adapters.base import FetchRequest, FetchResult
from app.core.adapters.manager import AdapterManager
from app.core.job_status import aggregate_job_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# CSRC 行业分块抓取大小（P1: 每块独立提交并记录进度，中断可续传）
CSRC_BATCH_SIZE = 50

FinancialStatementType = Literal[
    "balance_sheet",
    "income_statement",
    "cash_flow",
]


class DataInitializer:
    """最小可用初始化执行器

    执行 PRD §6.7 定义的最小可用初始化流程，
    将数据写入 DuckDB 分析库，将任务日志写入 SQLite 操作库。
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
            raise PathIsolationError("DataInitializer requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("DataInitializer stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.adapter_mgr = adapter_mgr or AdapterManager()
        self.duck = duck
        self.sqlite = sqlite
        self._batch_id = str(uuid.uuid4())

    def run_full_init(
        self,
        skip_prices: bool = False,
        skip_financials: bool = False,
        skip_csrc: bool = False,
    ) -> dict[str, Any]:
        """执行完整最小可用初始化

        Args:
            skip_prices: 跳过价格数据（调试用）
            skip_financials: 跳过财务数据（调试用）
            skip_csrc: 跳过 CSRC 行业全量抓取（P2: 首次全量约 2.3h，
                可用 --skip-csrc 先行建立最小可用，后续由自动更新低频补齐）

        Returns:
            初始化摘要报告
        """
        logger.info("=" * 60)
        logger.info("开始最小可用初始化 (PRD §6.7)")
        logger.info("=" * 60)

        # 记录任务开始
        job_id = self._log_job_start("full_init")

        report: dict[str, Any] = {
            "batch_id": self._batch_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "steps": {},
        }

        try:
            # Step 1: 股票全集 + 元数据
            step1 = self._fetch_stock_universe()
            report["steps"]["stock_universe"] = step1

            # Step 1b: listing metadata is separate from the stock-list endpoint.
            # Do not turn unavailable ST/suspension status into a safe-looking false.
            step1b = self._fetch_listing_info()
            report["steps"]["listing_info"] = step1b

            # Step 2: 交易日历
            step2 = self._fetch_trading_dates()
            report["steps"]["trading_dates"] = step2

            # Step 3: CSRC 行业分类
            if skip_csrc:
                report["steps"]["sw_industry"] = {
                    "status": "skipped",
                    "reason": "skipped_by_flag",
                    "note": "CSRC 行业分类由后续自动更新低频补齐（PRD §24）",
                }
            else:
                step3 = self._fetch_csrc_industry()
                report["steps"]["sw_industry"] = step3

            step4: dict[str, Any] | None = None
            step5: dict[str, Any] | None = None
            if not skip_prices:
                # Step 4: 近5年日线价格 (raw + qfq)
                step4 = self._fetch_daily_prices(years=5)
                report["steps"]["daily_prices"] = step4

            if not skip_financials:
                # Step 5: 最小核心财务集
                step5 = self._fetch_financial_statements()
                report["steps"]["financials"] = step5

            # Company actions are independently required for price history and
            # must not be inferred from dividend rows or adjusted prices.
            if not skip_prices and step4 is not None and step4.get("status") != "skipped":
                step5b = self._fetch_xdxr()
                report["steps"]["corporate_actions"] = step5b

            if (
                step4 is not None
                and step5 is not None
                and step4.get("status") == "success"
                and step5.get("status") == "success"
            ):
                # A successful import without a snapshot is not screenable.
                from app.core.indicators.calculator import IndicatorCalculator

                step6 = IndicatorCalculator(duck=self.duck, sqlite=self.sqlite).compute_snapshot_for_all()
                report["steps"]["indicators"] = step6
            elif not skip_prices and not skip_financials:
                report["steps"]["indicators"] = {
                    "status": "skipped",
                    "reason": "prerequisites_not_ready",
                    "daily_prices_status": step4.get("status") if step4 else None,
                    "financials_status": step5.get("status") if step5 else None,
                }

            report["status"] = aggregate_job_status(report["steps"])

        except Exception as e:
            logger.error(f"初始化失败: {e}", exc_info=True)
            report["status"] = "failed"
            report["error"] = str(e)

        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._log_job_finish(job_id, report["status"], report)

        logger.info("=" * 60)
        logger.info(f"初始化完成: {report['status']}")
        logger.info(f"摘要: {report}")
        logger.info("=" * 60)

        return report

    def _fetch_stock_universe(self) -> dict[str, Any]:
        """Step 1: 获取当前上市股票全集 + 元数据"""
        logger.info("[Step 1] 获取上市股票全集...")
        result = self.adapter_mgr.fetch(FetchRequest(data_type="stock_list"))

        if result.metadata.error or not result.data:
            logger.error(f"获取股票全集失败: {result.metadata.error}")
            self._record_failures("stock_list", [], result)
            return {"status": "failed", "error": result.metadata.error, "count": 0}

        # 写入 DuckDB
        from pypinyin import lazy_pinyin

        # 去重（同一代码可能同时出现在 SSE/SZSE 和 BSE 列表中）
        seen_codes: set[str] = set()
        records = []
        for row in result.data:
            code = row.get("stock_code", "")
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            name = row.get("name", "")
            pinyin = "".join(lazy_pinyin(name)) if name else ""
            exchange = row.get("exchange", "")

            records.append({
                "stock_code": code,
                "name": name,
                "pinyin": pinyin,
                "exchange": exchange,
                "listing_date": row.get("listing_date"),
                "is_listed": True,
                "is_st": row.get("is_st"),
                "is_suspended": row.get("is_suspended"),
                "total_shares": row.get("total_shares"),
                "circ_shares": row.get("circ_shares"),
            })

        # PRD §7.4 L1: 保留旧值，不以空值覆盖旧值。
        # A partial response may contain only the exchanges fetched successfully.
        # Never infer delisting for an exchange absent from this response.
        covered_exchanges = {record["exchange"] for record in records if record["exchange"]}
        # P1-B修复: 防御性退市门禁——按交易所比较"本次抓取数量"与"当前上市
        # 数量"，若某交易所抓取数低于当前上市数的 90%（截断/部分响应的
        # 合理下限），拒绝把缺失代码标记为退市，并披露到报告（防静默
        # 剔除数千只有效股票；正常全量退市事件不会单轮超过 10%）。
        listed_by_exchange: dict[str, int] = {}
        if covered_exchanges:
            exchange_placeholders = ", ".join("?" for _ in covered_exchanges)
            for row in self.duck.read_query(
                f"""SELECT exchange, COUNT(*) AS cnt FROM stock_meta
                    WHERE is_listed IS TRUE AND exchange IN ({exchange_placeholders})
                    GROUP BY exchange""",
                sorted(covered_exchanges),
            ):
                listed_by_exchange[row["exchange"]] = row["cnt"]
        fetched_by_exchange: dict[str, int] = {}
        for record in records:
            exchange = record.get("exchange")
            if exchange:
                fetched_by_exchange[exchange] = fetched_by_exchange.get(exchange, 0) + 1
        delist_guarded: dict[str, dict[str, int]] = {}
        for exchange in covered_exchanges:
            listed = listed_by_exchange.get(exchange, 0)
            fetched = fetched_by_exchange.get(exchange, 0)
            if listed > 0 and fetched < max(1, int(listed * 0.9)):
                delist_guarded[exchange] = {"listed": listed, "fetched": fetched}

        with self.duck.transaction() as conn:
            # Retain historical securities but exclude codes absent from this
            # successfully fetched current-listed exchange from active research.
            if covered_exchanges and not delist_guarded:
                exchange_placeholders = ", ".join("?" for _ in covered_exchanges)
                conn.execute(
                    f"UPDATE stock_meta SET is_listed = FALSE WHERE exchange IN ({exchange_placeholders})",
                    sorted(covered_exchanges),
                )
            conn.executemany(
                """INSERT INTO stock_meta
                   (stock_code, name, pinyin, exchange, listing_date, is_listed, is_st, is_suspended, total_shares, circ_shares)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (stock_code) DO UPDATE SET
                        name = excluded.name,
                        pinyin = COALESCE(excluded.pinyin, stock_meta.pinyin),
                        exchange = excluded.exchange,
                        listing_date = COALESCE(excluded.listing_date, stock_meta.listing_date),
                        is_listed = TRUE,
                        is_st = COALESCE(excluded.is_st, stock_meta.is_st),
                       is_suspended = COALESCE(excluded.is_suspended, stock_meta.is_suspended),
                       total_shares = COALESCE(excluded.total_shares, stock_meta.total_shares),
                       circ_shares = COALESCE(excluded.circ_shares, stock_meta.circ_shares),
                       updated_at = now()""",
                [(r["stock_code"], r["name"], r["pinyin"], r["exchange"],
                  r["listing_date"], r["is_listed"], r["is_st"], r["is_suspended"],
                  r["total_shares"], r["circ_shares"]) for r in records],
            )

        # 记录批次溯源
        # C15修复(报告41): 部分/截断响应（退市门禁触发）时批次溯源必须如实
        # 降级为 approximate，不得伪装 strict——与适配器 partial 语义一致。
        self._record_batch(
            result, "stock_list", len(records),
            confidence="approximate" if delist_guarded else None,
        )

        if delist_guarded:
            logger.warning(
                "退市门禁触发（疑似部分响应），跳过退市标记: %s", delist_guarded
            )
            return {
                "status": "partial",
                "count": len(records),
                "delist_guarded_exchanges": delist_guarded,
                "reason": "partial stock list response; delisting skipped for guarded exchanges",
            }
        logger.info(f"[Step 1] 获取 {len(records)} 只股票")
        return {"status": "success", "count": len(records)}

    def _fetch_listing_info(self) -> dict[str, Any]:
        """Fetch and persist listing/ST/suspension/share metadata for the stock universe."""
        logger.info("[Step 1b] 获取上市与状态元数据...")
        stocks = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        stock_codes = [row["stock_code"] for row in stocks]
        if not stock_codes:
            return {"status": "skipped", "reason": "no_stock_universe", "count": 0}

        result = self.adapter_mgr.fetch(
            FetchRequest(data_type="listing_info", stock_codes=stock_codes)
        )
        if result.metadata.error or not result.data:
            self._record_failures("listing_info", stock_codes, result)
            return {
                "status": "failed",
                "error": result.metadata.error or "empty result",
                "count": 0,
            }

        records_by_code = {
            str(row.get("stock_code", "")): row
            for row in result.data
            if row.get("stock_code")
        }
        updated = 0
        missing_codes: list[str] = []
        with self.duck.write_connection() as conn:
            for code in stock_codes:
                row = records_by_code.get(code)
                if row is None:
                    missing_codes.append(code)
                    continue
                conn.execute(
                    """UPDATE stock_meta SET
                           name = COALESCE(NULLIF(?, ''), name),
                           pinyin = COALESCE(NULLIF(?, ''), pinyin),
                           listing_date = COALESCE(?, listing_date),
                           is_st = COALESCE(?, is_st),
                           is_suspended = COALESCE(?, is_suspended),
                           total_shares = COALESCE(?, total_shares),
                           circ_shares = COALESCE(?, circ_shares),
                           updated_at = now()
                       WHERE stock_code = ?""",
                    [
                        row.get("name"),
                        row.get("pinyin"),
                        row.get("listing_date"),
                        row.get("is_st"),
                        row.get("is_suspended"),
                        row.get("total_shares"),
                        row.get("circ_shares"),
                        code,
                    ],
                )
                if (
                    row.get("listing_date") is None
                    or row.get("is_st") is None
                    or row.get("is_suspended") is None
                ):
                    missing_codes.append(code)
                updated += 1

        for code in missing_codes:
            self._record_missing(code, "listing_info", "source_incomplete")
        self._record_batch(result, "listing_info", updated)
        return {
            "status": "success" if not missing_codes else "partial",
            "count": updated,
            "missing": len(missing_codes),
        }

    def _fetch_trading_dates(self) -> dict[str, Any]:
        """Step 2: 获取交易日历"""
        logger.info("[Step 2] 获取交易日历...")
        result = self.adapter_mgr.fetch(FetchRequest(data_type="trading_dates"))

        if result.metadata.error or not result.data:
            logger.warning(f"获取交易日历失败: {result.metadata.error}")
            return {"status": "failed", "error": result.metadata.error, "count": 0}

        # 交易日历暂存 SQLite（用于增量检查判断新交易日）
        with self.sqlite.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS trading_dates (trade_date TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM trading_dates")
            conn.executemany(
                "INSERT OR REPLACE INTO trading_dates (trade_date) VALUES (?)",
                [(r["trade_date"],) for r in result.data],
            )

        logger.info(f"[Step 2] 获取 {len(result.data)} 个交易日")
        return {"status": "success", "count": len(result.data)}

    def _fetch_csrc_industry(self) -> dict[str, Any]:
        """Step 3: CSRC（证监会）行业分类（CNINFO 自动获取，PRD §24）

        P1修复（首启性能/可用性）：
        - 只补抓 csrc_l1 IS NULL 的股票（断点续传语义，已有旧分类保留）
        - 分块抓取（CSRC_BATCH_SIZE），每块独立事务提交并记录进度，
          中断后下次运行从断点继续，不会每轮全市场逐股重扫
        """
        logger.info("[Step 3] 获取 CSRC 行业分类...")

        try:
            stocks = self.duck.read_query(
                """SELECT stock_code FROM stock_meta
                   WHERE is_listed IS TRUE AND csrc_l1 IS NULL
                   ORDER BY stock_code"""
            )
        except Exception as error:
            return {"status": "failed", "source": "cninfo", "count": 0, "error": str(error)}

        if not stocks:
            return {
                "status": "success", "source": "cninfo", "count": 0,
                "note": "全部上市股已有 CSRC 分类（无需重抓）",
            }

        codes = [str(row["stock_code"]) for row in stocks]
        processed = 0
        errors: list[str] = []
        for i in range(0, len(codes), CSRC_BATCH_SIZE):
            chunk = codes[i : i + CSRC_BATCH_SIZE]
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type="csrc_industry",
                stock_codes=chunk,
            ))
            if result.metadata.error and not result.data:
                errors.append(result.metadata.error)
            else:
                # 幂等写入；缺数据的股票保持 NULL 留给下轮断点
                with self.duck.transaction() as conn:
                    conn.executemany(
                        """UPDATE stock_meta SET csrc_l1=?, csrc_l2=? WHERE stock_code=?""",
                        [(row.get("csrc_l1"), row.get("csrc_l2"), row.get("stock_code"))
                         for row in result.data],
                    )
                processed += len(result.data)
            self._mark_csrc_progress(i + len(chunk), len(codes))
            logger.info(
                "  [Step 3] CSRC 进度: %d/%d (本块 %d 条)",
                min(i + len(chunk), len(codes)), len(codes), len(result.data),
            )

        logger.info(f"[Step 3] CSRC 行业分类更新完成: {processed} 条")
        return {
            "status": "success" if not errors else "partial",
            "source": "cninfo",
            "count": processed,
            "total": len(codes),
            "errors": errors[:20],
        }

    def _mark_csrc_progress(self, processed: int, total: int) -> None:
        """持久化 CSRC 抓取进度（断点续传依据）。"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES ('csrc_industry_progress', ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=excluded.updated_at""",
                    [
                        json.dumps({
                            "processed": processed,
                            "total": total,
                            "as_of": datetime.now(timezone.utc).date().isoformat(),
                        }, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ],
                )
        except Exception as error:
            logger.warning("记录 CSRC 进度失败: %s", error)

    def _fetch_daily_prices(self, years: int = 5) -> dict[str, Any]:
        """Step 4: 获取近N年日线价格 (raw + qfq)"""
        logger.info(f"[Step 4] 获取近 {years} 年日线价格 (raw + qfq)...")

        # 获取股票列表
        stocks = self.duck.read_query(
            "SELECT stock_code, exchange FROM stock_meta WHERE is_listed IS TRUE"
        )
        if not stocks:
            return {"status": "skipped", "reason": "无股票数据"}

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=years * 365 + 30)).strftime("%Y-%m-%d")

        total = len(stocks)
        success_count = 0
        fail_count = 0
        qfq_exempt = 0
        failed_codes: list[str] = []

        for i, stock in enumerate(stocks):
            code = stock["stock_code"]

            if (i + 1) % 50 == 0:
                logger.info(f"  价格抓取进度: {i + 1}/{total} (成功 {success_count}, 失败 {fail_count})")

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
                fail_count += 1
                failed_codes.append(code)
                self._record_failure(code, "price_daily", raw_result.metadata.source or "akshare_eastmoney",
                                      raw_result.metadata.error or "empty result")
                continue

            if qfq_result.metadata.error or not qfq_result.data:
                fail_count += 1
                failed_codes.append(code)
                self._record_failure(
                    code,
                    "price_daily",
                    qfq_result.metadata.source or "akshare_eastmoney",
                    qfq_result.metadata.error or "empty result",
                    extra_json='{"adjust": "qfq"}',
                )
                continue

            # 写入 DuckDB
            # P0#2.7修复: 用 INSERT OR REPLACE 替代 DELETE+INSERT
            # PRD §7.4 L1: "保留旧值，不以空值覆盖旧值"
            try:
                with self.duck.transaction() as conn:
                    # Sparse source rows must not replace already verified values with NULL.
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

                    # qfq (如果有)
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
                    raw_batch_id = self._record_batch_in_connection(
                        conn, raw_result, "price_daily_raw", len(raw_result.data)
                    )
                    self._record_field_audit_in_connection(
                        conn, raw_result, raw_result.data, code, "trade_date", raw_batch_id
                    )
                    qfq_batch_id = self._record_batch_in_connection(
                        conn, qfq_result, "price_daily_qfq", len(qfq_result.data)
                    )
                    self._record_field_audit_in_connection(
                        conn, qfq_result, qfq_result.data, code, "trade_date", qfq_batch_id
                    )

                success_count += 1

            except Exception as e:
                fail_count += 1
                failed_codes.append(code)
                self._record_failure(code, "price_daily", raw_result.metadata.source or "akshare_eastmoney", str(e))
                logger.error(f"  写入 {code} 价格失败: {e}")

        logger.info(f"[Step 4] 价格抓取完成: 成功 {success_count}, 失败 {fail_count}")
        return {
            "status": "success" if fail_count == 0 else "partial",
            "total": total,
            "success": success_count,
            "failed": fail_count,
            "qfq_exempt": qfq_exempt,
            "failed_codes": failed_codes[:20],  # 只记录前20个
        }

    @staticmethod
    def _financial_row_is_complete(
        statement_type: FinancialStatementType,
        row: dict[str, Any],
    ) -> bool:
        """Return whether a source row contains the statement's core fields."""
        match statement_type:
            case "balance_sheet":
                required_aliases = (
                    ("TOTAL_ASSETS", "total_assets"),
                    ("TOTAL_LIABILITIES", "total_liabilities"),
                    (
                        "TOTAL_EQUITY_PARENT",
                        "total_equity_parent",
                        "TOTAL_EQUITY",
                        "total_equity",
                    ),
                )
            case "income_statement":
                required_aliases = (
                    (
                        "TOTAL_OPERATE_INCOME",
                        "total_operating_revenue",
                        "OPERATE_INCOME",
                        "revenue",
                    ),
                    ("PARENT_NETPROFIT", "parent_net_profit"),
                )
            case "cash_flow":
                required_aliases = (("NETCASH_OPERATE", "cf_from_operating"),)
            case unreachable:
                assert_never(unreachable)

        return all(
            any(row.get(field_name) is not None for field_name in aliases)
            for aliases in required_aliases
        )

    def _fetch_financial_statements(self) -> dict[str, Any]:
        """Step 5: 获取最小核心财务集（三大报表）"""
        logger.info("[Step 5] 获取最小核心财务集...")

        stocks = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE"
        )
        if not stocks:
            return {"status": "skipped", "reason": "无股票数据"}

        total = len(stocks)
        balance_ok = 0
        income_ok = 0
        cashflow_ok = 0
        for i, stock in enumerate(stocks):
            code = stock["stock_code"]

            if (i + 1) % 50 == 0:
                logger.info(
                    f"  财务抓取进度: {i + 1}/{total} "
                    f"(资产负债表 {balance_ok}, 利润表 {income_ok}, 现金流量表 {cashflow_ok})"
                )

            # 资产负债表
            bs_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="balance_sheet",
                stock_codes=[code],
            ))

            if bs_result.data:
                complete_rows = [
                    row for row in bs_result.data
                    if self._financial_row_is_complete("balance_sheet", row)
                ]
                if len(complete_rows) != len(bs_result.data):
                    self._record_missing(code, "balance_sheet", "shell_row")
                try:
                    with self.duck.transaction() as conn:
                        # P0#2.7修复: 不再 DELETE, 用 INSERT OR REPLACE 保留旧值
                        for row in complete_rows:
                            self._upsert_financial_row(conn, "balance_sheet", code, row)
                        batch_id = self._record_batch_in_connection(
                            conn, bs_result, "balance_sheet", len(complete_rows)
                        )
                        self._record_field_audit_in_connection(
                            conn, bs_result, complete_rows, code, "report_date", batch_id
                        )
                    if complete_rows:
                        balance_ok += 1
                        self._record_missing_financial_sector_fields(code, complete_rows)
                except Exception as e:
                    logger.error(f"  写入 {code} 资产负债表失败: {e}")
                    self._record_failure(code, "balance_sheet", bs_result.metadata.source or "akshare_eastmoney", str(e))
            else:
                self._record_missing(code, "balance_sheet", "source_empty")

            # 利润表
            ic_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="income_statement",
                stock_codes=[code],
            ))

            if ic_result.data:
                complete_rows = [
                    row for row in ic_result.data
                    if self._financial_row_is_complete("income_statement", row)
                ]
                if len(complete_rows) != len(ic_result.data):
                    self._record_missing(code, "income_statement", "shell_row")
                try:
                    with self.duck.transaction() as conn:
                        # P0#2.7修复: 不再 DELETE, 用 INSERT OR REPLACE 保留旧值
                        for row in complete_rows:
                            self._upsert_financial_row(conn, "income_statement", code, row)
                        batch_id = self._record_batch_in_connection(
                            conn, ic_result, "income_statement", len(complete_rows)
                        )
                        self._record_field_audit_in_connection(
                            conn, ic_result, complete_rows, code, "report_date", batch_id
                        )
                    if complete_rows:
                        income_ok += 1
                except Exception as e:
                    logger.error(f"  写入 {code} 利润表失败: {e}")
                    self._record_failure(code, "income_statement", ic_result.metadata.source or "akshare_eastmoney", str(e))
            else:
                self._record_missing(code, "income_statement", "source_empty")

            # 现金流量表
            cf_result = self.adapter_mgr.fetch(FetchRequest(
                data_type="cash_flow",
                stock_codes=[code],
            ))

            if cf_result.data:
                complete_rows = [
                    row for row in cf_result.data
                    if self._financial_row_is_complete("cash_flow", row)
                ]
                if len(complete_rows) != len(cf_result.data):
                    self._record_missing(code, "cash_flow", "shell_row")
                try:
                    with self.duck.transaction() as conn:
                        # P0#2.7修复: 不再 DELETE, 用 INSERT OR REPLACE 保留旧值
                        for row in complete_rows:
                            self._upsert_financial_row(conn, "cash_flow", code, row)
                        batch_id = self._record_batch_in_connection(
                            conn, cf_result, "cash_flow", len(complete_rows)
                        )
                        self._record_field_audit_in_connection(
                            conn, cf_result, complete_rows, code, "report_date", batch_id
                        )
                    if complete_rows:
                        cashflow_ok += 1
                except Exception as e:
                    logger.error(f"  写入 {code} 现金流量表失败: {e}")
                    self._record_failure(code, "cash_flow", cf_result.metadata.source or "akshare_eastmoney", str(e))
            else:
                self._record_missing(code, "cash_flow", "source_empty")

        logger.info(
            f"[Step 5] 财务抓取完成: "
            f"资产负债表 {balance_ok}, 利润表 {income_ok}, 现金流量表 {cashflow_ok}"
        )
        complete_stock_count = min(balance_ok, income_ok, cashflow_ok)
        status = (
            "success"
            if complete_stock_count == total
            else "failed"
            if complete_stock_count == 0
            else "partial"
        )
        return {
            "status": status,
            "total": total,
            "balance_sheet": balance_ok,
            "income_statement": income_ok,
            "cash_flow": cashflow_ok,
            "complete_stocks": complete_stock_count,
        }

    def _fetch_xdxr(self) -> dict[str, Any]:
        """Fetch and persist corporate actions for every current listed stock."""
        stocks = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        if not stocks:
            return {"status": "skipped", "reason": "no_listed_stocks"}

        succeeded = 0
        failed = 0
        rows_written = 0
        for stock in stocks:
            code = stock["stock_code"]
            result = self.adapter_mgr.fetch(FetchRequest(data_type="xdxr", stock_codes=[code]))
            if result.metadata.error or not result.data:
                failed += 1
                self._record_failure(
                    code,
                    "xdxr",
                    result.metadata.source,
                    result.metadata.error or "empty result",
                )
                self._record_missing(code, "xdxr", "source_unavailable")
                continue
            try:
                with self.duck.transaction() as conn:
                    self._upsert_xdxr_rows(conn, code, result.data)
                    batch_id = self._record_batch_in_connection(conn, result, "xdxr", len(result.data))
                    self._record_field_audit_in_connection(
                        conn, result, result.data, code, "event_date", batch_id
                    )
                succeeded += 1
                rows_written += len(result.data)
            except Exception as error:
                failed += 1
                self._record_failure(code, "xdxr", "duckdb", str(error))

        return {
            "status": "success" if failed == 0 else "partial",
            "total": len(stocks),
            "success": succeeded,
            "failed": failed,
            "rows_written": rows_written,
        }

    @staticmethod
    def _upsert_xdxr_rows(conn: Any, stock_code: str, rows: list[dict[str, Any]]) -> None:
        """Upsert only complete TDX corporate-action keys without deleting history."""
        valid_rows = [
            row for row in rows
            if row.get("event_date") is not None and row.get("category") is not None
        ]
        if not valid_rows:
            raise ValueError("xdxr result has no event_date/category rows")
        conn.executemany(
            """INSERT INTO xdxr
                   (stock_code, event_date, category, fenhong, songzhuangu, peigu, peigujia)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stock_code, event_date, category) DO UPDATE SET
                   fenhong=COALESCE(excluded.fenhong, xdxr.fenhong),
                   songzhuangu=COALESCE(excluded.songzhuangu, xdxr.songzhuangu),
                   peigu=COALESCE(excluded.peigu, xdxr.peigu),
                   peigujia=COALESCE(excluded.peigujia, xdxr.peigujia)""",
            [
                (
                    stock_code,
                    row["event_date"],
                    row["category"],
                    row.get("fenhong"),
                    row.get("songzhuangu"),
                    row.get("peigu"),
                    row.get("peigujia"),
                )
                for row in valid_rows
            ],
        )

    @staticmethod
    def _upsert_dividend_rows(conn: Any, stock_code: str, rows: list[dict[str, Any]]) -> None:
        """Persist dividend rows without replacing previously verified values with NULL."""
        valid_rows = [row for row in rows if row.get("ex_date")]
        if not valid_rows:
            raise ValueError("dividend result has no ex_date rows")
        conn.executemany(
            """INSERT INTO dividends
                   (stock_code, ex_date, announcement_date, dividend_per_share, stock_dividend,
                    transfer_share, rights_issue, rights_issue_price)
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
                    stock_code, row["ex_date"], row.get("announcement_date"),
                    row.get("dividend_per_share"), row.get("stock_dividend"),
                    row.get("transfer_share"), row.get("rights_issue"), row.get("rights_issue_price"),
                )
                for row in valid_rows
            ],
        )

    def _upsert_financial_row(self, conn, table: str, stock_code: str, row: dict) -> None:
        """动态插入财务报表行

        将 Eastmoney 返回的字段映射到 schema 中已定义的列，
        同时将完整原始数据存入 raw_data JSON 列（审查问题6修订）。
        """
        import json

        # 获取表的列名
        cols_info = conn.execute(
            f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
        ).fetchall()
        available_cols = {c[0] for c in cols_info}

        # AKShare/Eastmoney 字段名 → 标准化列名映射
        # Eastmoney 返回大写英文字段名（如 TOTAL_ASSETS），映射到小写标准列名
        field_mapping = {
            # 资产负债表
            "MONETARY_FUNDS": "monetary_funds",
            "ACCOUNTS_RECE": "accounts_receivable",
            "INVENTORY": "inventory",
            "TOTAL_CURRENT_ASSETS": "total_current_assets",
            "TOTAL_ASSETS": "total_assets",
            "TOTAL_CURRENT_LIAB": "total_current_liabilities",
            "TOTAL_LIABILITIES": "total_liabilities",
            "TOTAL_EQUITY": "total_equity",
            "TOTAL_EQUITY_PARENT": "total_equity_parent",
            "PAID_IN_CAPITAL": "paid_in_capital",
            "CAPITAL_RESERVE": "capital_reserve",
            "SURPLUS_RESERVE": "surplus_reserve",
            "UNDIST_PROFIT": "undistributed_profit",
            "MINORITY_INTEREST": "minority_interest",
            "GOODWILL": "goodwill",
            "SHORT_TERM_LOANS": "short_term_loans",
            "LONG_TERM_LOANS": "long_term_loans",
            "BONDS_PAYABLE": "bonds_payable",
            "ACCOUNTS_PAYABLE": "accounts_payable",
            "NOTES_RECEIVABLE": "notes_receivable",
            "NOTES_PAYABLE": "notes_payable",
            "PREPAYMENTS": "prepayments",
            "OTHER_RECEIVABLES": "other_receivables",
            "CONTRACT_ASSETS": "contract_assets",
            "CONTRACT_LIABILITIES": "contract_liabilities",
            "FIXED_ASSETS": "fixed_assets",
            "INTANGIBLE_ASSETS": "intangible_assets",
            "CONSTRUCTION_IN_PROGRESS": "construction_in_progress",
            "LONG_TERM_EQUITY_INVEST": "long_term_equity_investment",
            "TOTAL_NON_CURRENT_ASSETS": "total_non_current_assets",
            "TOTAL_NON_CURRENT_LIAB": "total_non_current_liabilities",
            "RIGHT_OF_USE_ASSETS": "right_of_use_assets",
            "LEASE_LIABILITIES": "lease_liabilities",
            "DEFERRED_TAX_ASSETS": "deferred_tax_assets",
            "EMPLOYEE_BENEFITS_PAYABLE": "employee_benefits_payable",
            "TAXES_PAYABLE": "taxes_payable",
            "PREPAYMENTS_RECEIVED": "prepayments_received",
            "TRADING_FIN_ASSETS": "trading_financial_assets",
            # 金融行业监管指标。供应商未提供时保持 NULL，不能用通用财务字段替代。
            "CORE_TIER1_CAPITAL_ADEQUACY_RATIO": "core_tier1_capital_adequacy_ratio",
            "CORE_TIER1_CAPITAL_RATIO": "core_tier1_capital_adequacy_ratio",
            "核心一级资本充足率": "core_tier1_capital_adequacy_ratio",
            "TIER1_CAPITAL_ADEQUACY_RATIO": "tier1_capital_adequacy_ratio",
            "TIER1_CAPITAL_RATIO": "tier1_capital_adequacy_ratio",
            "一级资本充足率": "tier1_capital_adequacy_ratio",
            "CAPITAL_ADEQUACY_RATIO": "capital_adequacy_ratio",
            "CAPITAL_RATIO": "capital_adequacy_ratio",
            "资本充足率": "capital_adequacy_ratio",
            "NON_PERFORMING_LOAN_RATIO": "non_performing_loan_ratio",
            "NPL_RATIO": "non_performing_loan_ratio",
            "不良贷款率": "non_performing_loan_ratio",
            "PROVISION_COVERAGE_RATIO": "provision_coverage_ratio",
            "拨备覆盖率": "provision_coverage_ratio",
            "RISK_COVERAGE_RATIO": "risk_coverage_ratio",
            "风险覆盖率": "risk_coverage_ratio",
            # 利润表
            "TOTAL_OPERATE_INCOME": "total_operating_revenue",
            "OPERATE_INCOME": "revenue",
            "TOTAL_OPERATE_COST": "total_operating_cost",
            "OPERATE_COST": "cost_of_revenue",
            "TAXES_SURCHARGES": "taxes_and_surcharges",
            "SELLING_EXPENSES": "selling_expenses",
            "ADMINISTRATIVE_EXPENSES": "administrative_expenses",
            "RD_EXPENSES": "rd_expenses",
            "FINANCIAL_EXPENSES": "financial_expenses",
            "INTEREST_EXPENSE": "interest_expense",
            "INTEREST_INCOME": "interest_income",
            "ASSET_IMPAIRMENT_LOSS": "asset_impairment_loss",
            "CREDIT_IMPAIRMENT_LOSS": "credit_impairment_loss",
            "EXCHANGE_GAIN": "exchange_gain",
            "INVESTMENT_INCOME": "investment_income",
            "OPERATE_PROFIT": "operating_profit",
            "NON_OPERATE_INCOME": "non_operating_income",
            "NON_OPERATE_EXPENSES": "non_operating_expenses",
            "TOTAL_PROFIT": "total_profit",
            "INCOME_TAX": "income_tax",
            "NETPROFIT": "net_profit",
            "PARENT_NETPROFIT": "parent_net_profit",
            "MINORITY_SHAREHOLDER_PROFIT": "minority_shareholder_profit",
            "DEDUCTED_NET_PROFIT": "deducted_net_profit",
            "BASIC_EPS": "basic_eps",
            "DILUTED_EPS": "diluted_eps",
            # 现金流量表
            "CASH_RECEIVED_SALES": "cash_received_sales",
            "TAXES_REFUNDED": "taxes_refunded",
            "OTHER_OPERATING_CF_IN": "other_operating_cf_in",
            "TOTAL_OPERATING_CF_IN": "total_operating_cf_in",
            "CASH_PAID_GOODS": "cash_paid_goods",
            "CASH_PAID_EMPLOYEES": "cash_paid_employees",
            "CASH_PAID_TAXES": "cash_paid_taxes",
            "OTHER_OPERATING_CF_OUT": "other_operating_cf_out",
            "TOTAL_OPERATING_CF_OUT": "total_operating_cf_out",
            "NETCASH_OPERATE": "cf_from_operating",
            "NETCASH_INVEST": "cf_from_investing",
            "NETCASH_FINANCE": "cf_from_financing",
            "EXCHANGE_RATE_EFFECT": "exchange_rate_effect",
            "CASH_NET_INCREASE": "cf_net",
            "CASH_BEGINNING": "cash_beginning",
            "CASH_ENDING": "cash_ending",
            # 公共字段
            "SECURITY_CODE": "stock_code",
            "REPORT_DATE": "report_date",
            "REPORT_TYPE": "report_type",
            # 标准化字段名（sina/tdx 等适配器直接输出标准列名）→ 保持标准名
            "stock_code": "stock_code",
            "report_date": "report_date",
            "total_assets": "total_assets",
            "total_liabilities": "total_liabilities",
            "total_equity": "total_equity",
            "total_equity_parent": "total_equity_parent",
            "revenue": "revenue",
            "parent_net_profit": "parent_net_profit",
            "cf_from_operating": "cf_from_operating",
        }

        # 构建标准化记录
        mapped: dict[str, Any] = {}
        unmapped: dict[str, Any] = {}

        for k, v in row.items():
            if v is None:
                continue
            # 清洗值
            if hasattr(v, 'isoformat'):
                v = str(v)[:10]
            try:
                import math
                if isinstance(v, float) and math.isnan(v):
                    continue
            except Exception:
                pass

            mapped_name = field_mapping.get(k)
            if mapped_name and mapped_name in available_cols:
                mapped[mapped_name] = v
            else:
                unmapped[k] = v

        # 确保 stock_code 存在
        if "stock_code" not in mapped:
            mapped["stock_code"] = stock_code

        # 添加 raw_data JSON（存储完整原始数据）
        if "raw_data" in available_cols:
            mapped["raw_data"] = json.dumps(row, ensure_ascii=False, default=str)

        if not mapped:
            return

        constraint_rows = conn.execute(
            """SELECT 1 FROM duckdb_constraints()
               WHERE table_name = ?
                 AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                 AND constraint_column_names = ['stock_code', 'report_date']
               LIMIT 1""",
            [table],
        ).fetchall()
        if not constraint_rows:
            # Older production databases have an income_statement table without
            # the declared composite key. Merge first so replacing one row does
            # not erase columns omitted by a sparse provider response.
            previous = conn.execute(
                f"SELECT * FROM {table} WHERE stock_code = ? AND report_date = ?",
                [mapped["stock_code"], mapped.get("report_date")],
            ).fetchone()
            if previous is not None:
                previous_columns = [column[0] for column in conn.description]
                merged = dict(zip(previous_columns, previous))
                merged.update(mapped)
                mapped = {column: value for column, value in merged.items() if column in available_cols}
            conn.execute(
                f"DELETE FROM {table} WHERE stock_code = ? AND report_date = ?",
                [mapped["stock_code"], mapped.get("report_date")],
            )
            fields = list(mapped.keys())
            placeholders = ", ".join(["?"] * len(fields))
            conn.execute(
                f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})",
                list(mapped.values()),
            )
            return

        # 构建 INSERT
        fields = list(mapped.keys())
        values = list(mapped.values())
        placeholders = ", ".join(["?"] * len(fields))
        field_str = ", ".join(fields)
        update_fields = [field for field in fields if field not in {"stock_code", "report_date"}]
        updates = ", ".join(
            f"{field} = COALESCE(excluded.{field}, {table}.{field})" for field in update_fields
        )
        conn.execute(
            f"""INSERT INTO {table} ({field_str}) VALUES ({placeholders})
                ON CONFLICT(stock_code, report_date) DO UPDATE SET {updates}""",
            values,
        )

    def _record_batch(
        self, result: FetchResult, data_type: str, row_count: int,
        confidence: str | None = None,
    ) -> None:
        """记录批次级溯源到 fetch_batch 表"""
        try:
            with self.duck.write_connection() as conn:
                self._record_batch_in_connection(conn, result, data_type, row_count, confidence)
        except Exception as e:
            logger.warning(f"记录批次溯源失败: {e}")
            raise

    def _record_batch_in_connection(
        self, conn: Any, result: FetchResult, data_type: str, row_count: int,
        confidence: str | None = None,
    ) -> str:
        """Validate and retain source material in the caller's data transaction."""
        import hashlib

        if row_count > 0 and not result.raw_response:
            raise ValueError(f"{data_type} has rows but no source material")
        if row_count > 0 and hashlib.sha256(result.raw_response).hexdigest() != result.metadata.raw_response_hash:
            raise ValueError(f"{data_type} source material hash mismatch")
        # C15: 调用方可显式降级置信度（如部分/截断响应），否则沿用源元数据
        effective_confidence = confidence or result.metadata.confidence
        fetch_batch_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version,
                fetch_time, raw_response_hash, row_count, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [fetch_batch_id, data_type, result.metadata.source,
             result.metadata.api_version or "unknown", result.metadata.fetch_time,
             result.metadata.raw_response_hash, row_count, effective_confidence],
        )
        conn.execute(
            """INSERT INTO raw_response_archive
               (raw_response_hash, source, fetch_time, payload, api_version, integrity_verified)
               VALUES (?, ?, ?, ?, ?, TRUE)
               ON CONFLICT(raw_response_hash) DO NOTHING""",
            [result.metadata.raw_response_hash, result.metadata.source,
             result.metadata.fetch_time, result.raw_response, result.metadata.api_version],
        )
        self._last_fetch_batch_id = fetch_batch_id
        return fetch_batch_id

    def _record_field_audit(
        self,
        result: FetchResult,
        rows: list[dict[str, Any]],
        *,
        stock_code: str,
        report_date_field: str = "report_date",
    ) -> None:
        """Persist normalized per-value provenance after the corresponding data write."""
        with self.duck.write_connection() as conn:
            self._record_field_audit_in_connection(
                conn, result, rows, stock_code, report_date_field,
                getattr(self, "_last_fetch_batch_id", self._batch_id),
            )

    def _record_field_audit_in_connection(
        self, conn: Any, result: FetchResult, rows: list[dict[str, Any]],
        stock_code: str, report_date_field: str, fetch_batch_id: str,
    ) -> None:
        audit_rows: list[tuple[Any, ...]] = []
        for row in rows:
            report_date = row.get(report_date_field) or row.get(report_date_field.upper())
            for source_field, value in row.items():
                field_name = self._standard_audit_field(source_field)
                if field_name is None or not isinstance(value, (int, float)):
                    continue
                audit_rows.append((
                    stock_code, field_name, report_date, value, result.metadata.source,
                    fetch_batch_id,
                    result.metadata.fetch_time, result.metadata.raw_response_hash,
                    result.metadata.confidence, None, result.metadata.api_version,
                ))
        if not audit_rows:
            return
        conn.executemany(
            """INSERT INTO source_audit
               (stock_code, field_name, report_date, value, source, fetch_batch_id,
                fetch_time, raw_response_hash, confidence, reason_code, api_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            audit_rows,
        )

    @staticmethod
    def _standard_audit_field(source_field: str) -> str | None:
        """Map provider aliases to the standardized names consumed by screening."""
        aliases = {
            "OPERATE_INCOME": "revenue", "TOTAL_OPERATE_INCOME": "total_operating_revenue",
            "OPERATE_COST": "cost_of_revenue", "NETPROFIT": "net_profit",
            "PARENT_NETPROFIT": "parent_net_profit", "DEDUCTED_NET_PROFIT": "deducted_net_profit",
            "NETCASH_OPERATE": "cf_from_operating", "NETCASH_INVEST": "cf_from_investing",
            "NETCASH_FINANCE": "cf_from_financing", "TOTAL_ASSETS": "total_assets",
            "TOTAL_LIABILITIES": "total_liabilities", "TOTAL_EQUITY": "total_equity",
            "TOTAL_EQUITY_PARENT": "total_equity_parent", "CLOSE": "latest_close",
            "OPEN": "open", "HIGH": "high", "LOW": "low", "VOLUME": "volume",
            "TURNOVER": "turnover", "TURNOVER_RATE": "turnover_rate",
        }
        normalized = source_field.upper()
        if normalized in {"STOCK_CODE", "SECURITY_CODE", "REPORT_DATE", "TRADE_DATE", "RAW_DATA"}:
            return None
        return aliases.get(normalized, source_field.lower())

    def _record_missing_financial_sector_fields(
        self,
        stock_code: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Record unavailable mandatory sector fields only when classification is explicit."""
        sector_rows = self.duck.read_query(
            "SELECT csrc_l1, csrc_l2 FROM stock_meta WHERE stock_code = ?", [stock_code]
        )
        if not sector_rows:
            return
        sector_text = " ".join(
            str(sector_rows[0].get(field) or "") for field in ("csrc_l1", "csrc_l2")
        )
        if "银行" in sector_text:
            required = (
                "core_tier1_capital_adequacy_ratio",
                "tier1_capital_adequacy_ratio",
                "capital_adequacy_ratio",
                "non_performing_loan_ratio",
                "provision_coverage_ratio",
            )
        elif "证券" in sector_text:
            required = ("risk_coverage_ratio",)
        else:
            required = ()
        if not required:
            return
        aliases = {
            "core_tier1_capital_adequacy_ratio": {"CORE_TIER1_CAPITAL_ADEQUACY_RATIO", "CORE_TIER1_CAPITAL_RATIO", "核心一级资本充足率"},
            "tier1_capital_adequacy_ratio": {"TIER1_CAPITAL_ADEQUACY_RATIO", "TIER1_CAPITAL_RATIO", "一级资本充足率"},
            "capital_adequacy_ratio": {"CAPITAL_ADEQUACY_RATIO", "CAPITAL_RATIO", "资本充足率"},
            "non_performing_loan_ratio": {"NON_PERFORMING_LOAN_RATIO", "NPL_RATIO", "不良贷款率"},
            "provision_coverage_ratio": {"PROVISION_COVERAGE_RATIO", "拨备覆盖率"},
            "risk_coverage_ratio": {"RISK_COVERAGE_RATIO", "风险覆盖率"},
        }
        source_values = {
            str(key).upper(): value
            for row in rows
            for key, value in row.items()
        }
        for field in required:
            present_values = [
                source_values[alias.upper()]
                for alias in aliases[field]
                if alias.upper() in source_values
            ]
            if not present_values:
                self._record_missing(stock_code, f"balance.{field}", "source_field_unavailable")
            elif all(value is None for value in present_values):
                self._record_missing(stock_code, f"balance.{field}", "source_value_missing")

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
                     datetime.now(timezone.utc).isoformat(), extra_json or "{}"],
                )
        except Exception as e:
            logger.warning(f"记录失败信息失败: {e}")

    def _record_missing(self, stock_code: str, field_name: str, reason_code: str) -> None:
        """记录缺失到 missing_list"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)""",
                    [stock_code, field_name, reason_code],
                )
        except Exception as e:
            logger.warning(f"记录缺失信息失败: {e}")

    def _record_failures(
        self, data_type: str, stock_codes: list[str], result: FetchResult
    ) -> None:
        """批量记录失败"""
        if not stock_codes:
            self._record_failure("ALL", data_type, result.metadata.source,
                                 result.metadata.error or "unknown")
        else:
            for code in stock_codes:
                self._record_failure(code, data_type, result.metadata.source,
                                     result.metadata.error or "unknown")

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
                [status, datetime.now(timezone.utc).isoformat(),
                 json.dumps(details, ensure_ascii=False, default=str)[:5000], job_id],
            )
