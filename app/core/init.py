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

    def run_full_init(self, skip_prices: bool = False, skip_financials: bool = False) -> dict[str, Any]:
        """执行完整最小可用初始化

        Args:
            skip_prices: 跳过价格数据（调试用）
            skip_financials: 跳过财务数据（调试用）

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

            # Step 2: 交易日历
            step2 = self._fetch_trading_dates()
            report["steps"]["trading_dates"] = step2

            # Step 3: 申万行业分类
            step3 = self._fetch_sw_industry()
            report["steps"]["sw_industry"] = step3

            if not skip_prices:
                # Step 4: 近5年日线价格 (raw + qfq)
                step4 = self._fetch_daily_prices(years=5)
                report["steps"]["daily_prices"] = step4

            if not skip_financials:
                # Step 5: 最小核心财务集
                step5 = self._fetch_financial_statements()
                report["steps"]["financials"] = step5

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
                "is_st": row.get("is_st"),
                "is_suspended": row.get("is_suspended"),
                "total_shares": row.get("total_shares"),
                "circ_shares": row.get("circ_shares"),
            })

        # PRD §7.4 L1: 保留旧值，不以空值覆盖旧值。
        with self.duck.write_connection() as conn:
            conn.executemany(
                """INSERT INTO stock_meta
                   (stock_code, name, pinyin, exchange, listing_date, is_st, is_suspended, total_shares, circ_shares)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (stock_code) DO UPDATE SET
                       name = excluded.name,
                       pinyin = COALESCE(excluded.pinyin, stock_meta.pinyin),
                       exchange = excluded.exchange,
                       listing_date = COALESCE(excluded.listing_date, stock_meta.listing_date),
                       is_st = COALESCE(excluded.is_st, stock_meta.is_st),
                       is_suspended = COALESCE(excluded.is_suspended, stock_meta.is_suspended),
                       total_shares = COALESCE(excluded.total_shares, stock_meta.total_shares),
                       circ_shares = COALESCE(excluded.circ_shares, stock_meta.circ_shares),
                       updated_at = now()""",
                [(r["stock_code"], r["name"], r["pinyin"], r["exchange"],
                  r["listing_date"], r["is_st"], r["is_suspended"],
                  r["total_shares"], r["circ_shares"]) for r in records],
            )

        # 记录批次溯源
        self._record_batch(result, "stock_list", len(records))

        logger.info(f"[Step 1] 获取 {len(records)} 只股票")
        return {"status": "success", "count": len(records)}

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

    def _fetch_sw_industry(self) -> dict[str, Any]:
        """Step 3: 申万行业分类（本地缓存或缺失）"""
        logger.info("[Step 3] 获取申万行业分类...")

        # PRD 审查问题2: SW industry 无备用适配器
        # 从本地缓存加载（用户手动下载的 SWS 分类文件）
        from app.core.config import Config
        import json

        cfg = Config.current()
        sw_json = cfg.project_root / "config" / "sw_industry_cache.json"
        sw_csv = cfg.project_root / "config" / "sw_industry_cache.csv"

        records = []
        if sw_json.exists():
            with open(sw_json, encoding="utf-8") as f:
                data = json.load(f)
                records = data if isinstance(data, list) else data.get("data", [])
        elif sw_csv.exists():
            import csv
            with open(sw_csv, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    records.append(row)

        if records:
            # 更新 stock_meta 的行业字段
            with self.duck.write_connection() as conn:
                for r in records:
                    conn.execute(
                        """UPDATE stock_meta SET sw_level1=?, sw_level2=?,
                           sw_level1_code=?, sw_level2_code=? WHERE stock_code=?""",
                        [r.get("sw_level1"), r.get("sw_level2"),
                         r.get("sw_level1_code"), r.get("sw_level2_code"),
                         r.get("stock_code")],
                    )

            logger.info(f"[Step 3] 从本地缓存加载 {len(records)} 条行业分类")
            return {"status": "success", "source": "local_cache", "count": len(records)}
        else:
            # 无缓存文件，所有股票行业为 NULL（PRD §12.4: 不阻止全市场排名）
            logger.warning(
                "[Step 3] 申万行业分类缓存文件不存在 (config/sw_industry_cache.json 或 .csv)。"
                "行业排名将为 NULL，全市场排名仍可用。"
                "请手动从 swsresearch.com 下载分类文件并保存为 config/sw_industry_cache.json。"
            )
            return {
                "status": "missing",
                "source": "local_cache",
                "count": 0,
                "note": "无缓存文件，行业排名将为 NULL（PRD §12.4 允许）",
            }

    def _fetch_daily_prices(self, years: int = 5) -> dict[str, Any]:
        """Step 4: 获取近N年日线价格 (raw + qfq)"""
        logger.info(f"[Step 4] 获取近 {years} 年日线价格 (raw + qfq)...")

        # 获取股票列表
        stocks = self.duck.read_query("SELECT stock_code, exchange FROM stock_meta")
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
                if stock.get("exchange") == "BSE":
                    qfq_exempt += 1
                else:
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
                    # raw — INSERT OR REPLACE 保留未抓取的旧日期
                    conn.executemany(
                        """INSERT OR REPLACE INTO price_daily_raw
                           (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                            """INSERT OR REPLACE INTO price_daily_qfq
                               (stock_code, trade_date, open, high, low, close,
                                volume, turnover, turnover_rate)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

                success_count += 1

                # 记录批次溯源
                self._record_batch(raw_result, "price_daily_raw", len(raw_result.data))

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

        stocks = self.duck.read_query("SELECT stock_code FROM stock_meta")
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
                    if complete_rows:
                        balance_ok += 1
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
        return {
            "status": "success",
            "total": total,
            "balance_sheet": balance_ok,
            "income_statement": income_ok,
            "cash_flow": cashflow_ok,
        }

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

        # 构建 INSERT
        fields = list(mapped.keys())
        values = list(mapped.values())
        placeholders = ", ".join(["?"] * len(fields))
        field_str = ", ".join(fields)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({field_str}) VALUES ({placeholders})",
            values,
        )

    def _record_batch(self, result: FetchResult, data_type: str, row_count: int) -> None:
        """记录批次级溯源到 fetch_batch 表"""
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
        except Exception as e:
            logger.warning(f"记录批次溯源失败: {e}")

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
