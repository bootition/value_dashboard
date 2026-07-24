"""内建指标计算引擎

实现 PRD §10 全部6类内建指标：
1. 估值 (valuation): PE-TTM, PB-MRQ, PS-TTM, PCF-TTM, 股息率, 总市值, 流通市值
2. 盈利能力 (profitability): ROE, ROA, 毛利率, 净利率, ROIC, 经营现金流/净利润
3. 成长 (growth): YoY, CAGR(3年/5年)
4. 安全性 (safety): 资产负债率, 流动比率, 速动比率, 有息负债, 利息保障倍数, 商誉占比
5. 股东回报 (shareholder): 分红率, 每股股息, 连续分红年数
6. 行情 (technical): MA5-250, 区间收益率, 年化波动率, 最大回撤, 平均成交量, 换手率

所有指标基于 latest_restated 口径 (PRD §8.1)。
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """内建指标计算器

    从 DuckDB 读取财务报表和价格数据，计算全部内建指标，
    物化到 indicator_snapshot 表。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
    ) -> None:
        self.duck = duck or DuckDBStore()
        self.sqlite = sqlite or SQLiteStore()

    def compute_all_for_stock(self, stock_code: str) -> dict[str, Any]:
        """计算单只股票的全部内建指标

        Returns:
            包含所有指标值的字典，值可能为 None (缺失)
        """
        result: dict[str, Any] = {
            "stock_code": stock_code,
            "calculated_at": datetime.now(timezone.utc),
            "data_version": "audit-safe-v1",
        }

        # 获取最新报告期的三大报表数据
        financials = self._get_latest_financials(stock_code)
        result["report_date"] = financials.get("report_date")

        # 获取最新收盘价
        price_info = self._get_latest_price(stock_code)
        result["latest_close"] = price_info.get("close")
        result["latest_price_date"] = price_info.get("trade_date")

        # 获取总股本和流通股本
        shares = self._get_shares(stock_code, financials)
        total_shares = shares.get("total_shares")
        circ_shares = shares.get("circ_shares")

        # 获取TTM数据
        ttm = self._get_ttm_data(stock_code)

        # 获取分红数据
        dividends = self._get_dividend_summary(stock_code)

        # ─── 1. 估值指标 ──────────────────────────────────────────
        result.update(self._calc_valuation(
            stock_code, price_info, total_shares, circ_shares, ttm, financials, dividends
        ))

        # ─── 2. 盈利能力 ──────────────────────────────────────────
        result.update(self._calc_profitability(ttm, financials, stock_code))

        # ─── 3. 成长指标 ──────────────────────────────────────────
        result.update(self._calc_growth(stock_code))

        # ─── 4. 安全性 ────────────────────────────────────────────
        result.update(self._calc_safety(financials, ttm))

        # ─── 5. 股东回报 ──────────────────────────────────────────
        result.update(self._calc_shareholder_return(stock_code, financials, dividends, ttm))

        # ─── 6. 行情指标 ──────────────────────────────────────────
        result.update(self._calc_technical(stock_code))

        return result

    def compute_snapshot_for_all(self, batch_size: int = 100) -> dict[str, Any]:
        """为所有有财务数据的股票计算指标快照并写入 indicator_snapshot 表

        Args:
            batch_size: 每批处理的股票数量

        Returns:
            计算报告
        """
        # 只计算有财务数据的股票（避免对空数据股票做无意义查询）
        stocks = self.duck.read_query("""
            SELECT DISTINCT bs.stock_code
            FROM balance_sheet bs
            WHERE EXISTS (
                SELECT 1 FROM income_statement ic
                WHERE ic.stock_code = bs.stock_code
            )
            ORDER BY bs.stock_code
        """)
        total = len(stocks)
        if total == 0:
            return {"status": "skipped", "reason": "no stocks in stock_meta"}

        logger.info(f"开始计算指标快照: {total} 只股票")

        success = 0
        failed = 0
        failed_codes: list[str] = []
        staging_table = f"indicator_snapshot_staging_{uuid.uuid4().hex}"
        self._cleanup_snapshot_staging_tables()
        self.duck.write_query(
            f'CREATE TABLE "{staging_table}" AS '
            "SELECT * FROM indicator_snapshot WHERE FALSE"
        )

        try:
            for i in range(0, total, batch_size):
                batch = stocks[i:i + batch_size]
                batch_records: list[dict] = []

                for stock in batch:
                    code = stock["stock_code"]
                    try:
                        indicators = self.compute_all_for_stock(code)
                        if indicators.get("report_date") is None:
                            failed += 1
                            failed_codes.append(code)
                            continue
                        batch_records.append(indicators)
                        success += 1
                    except Exception as error:
                        failed += 1
                        failed_codes.append(code)
                        logger.debug("计算 %s 指标失败: %s", code, error)

                if batch_records:
                    self._write_batch(batch_records, staging_table)

                logger.info(
                    "  指标计算进度: %s/%s (成功 %s, 失败 %s)",
                    min(i + batch_size, total),
                    total,
                    success,
                    failed,
                )

            if failed == 0:
                self._publish_snapshot(staging_table, expected_count=success)

            logger.info("指标快照计算完成: 成功 %s, 失败 %s", success, failed)
            return {
                "status": "success" if failed == 0 else "partial",
                "total": total,
                "success": success,
                "failed": failed,
                "failed_codes": failed_codes[:20],
            }
        finally:
            self.duck.write_query(f'DROP TABLE IF EXISTS "{staging_table}"')

    # ─── 数据获取 ──────────────────────────────────────────────────

    def _get_latest_financials(self, stock_code: str) -> dict[str, Any]:
        """获取最新报告期的三大报表数据（合并为一行）

        M8-问题1修复: 应用人工覆写值 (PRD §9.5 R7: 覆写与原始值分离, 但计算时使用覆写值)
        """
        rows = self.duck.read_query("""
            SELECT
                bs.report_date,
                bs.monetary_funds, bs.accounts_receivable, bs.inventory,
                bs.total_current_assets, bs.total_assets,
                bs.total_current_liabilities, bs.total_liabilities,
                bs.total_equity, bs.total_equity_parent,
                bs.paid_in_capital, bs.goodwill,
                bs.short_term_loans, bs.long_term_loans, bs.bonds_payable,
                bs.notes_receivable, bs.prepayments, bs.other_receivables,
                bs.fixed_assets, bs.intangible_assets,
                ic.revenue, ic.cost_of_revenue,
                (ic.revenue - ic.cost_of_revenue) AS gross_profit,
                ic.operating_profit, ic.net_profit,
                ic.parent_net_profit, ic.deducted_net_profit,
                ic.total_operating_revenue, ic.total_operating_cost,
                ic.selling_expenses, ic.administrative_expenses,
                ic.financial_expenses, ic.rd_expenses,
                ic.interest_expense, ic.investment_income,
                ic.total_profit, ic.income_tax, ic.basic_eps,
                cf.cf_from_operating, cf.cf_from_investing,
                cf.cf_from_financing, cf.cf_net
            FROM balance_sheet bs
            LEFT JOIN income_statement ic
                ON bs.stock_code = ic.stock_code AND bs.report_date = ic.report_date
            LEFT JOIN cash_flow cf
                ON bs.stock_code = cf.stock_code AND bs.report_date = cf.report_date
            WHERE bs.stock_code = ?
              AND bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
            ORDER BY bs.report_date DESC
            LIMIT 1
        """, [stock_code])

        if not rows:
            return {}

        financials = rows[0]

        # M8-问题1: 应用人工覆写 (PRD §9.5: 覆写值用于计算, 原始值保留在 source_audit)
        try:
            overrides = self.sqlite.query(
                """SELECT field_name, override_value FROM manual_overrides
                   WHERE stock_code = ? AND status = 'published'
                   AND rolled_back_at IS NULL
                   AND (report_date = ? OR report_date IS NULL)""",
                [stock_code, str(financials.get("report_date", ""))],
            )
            for ov in overrides:
                field = ov["field_name"]
                if field in financials:
                    financials[field] = ov["override_value"]
                    logger.debug(f"应用人工覆写: {stock_code}.{field} = {ov['override_value']}")
        except Exception as e:
            logger.debug(f"读取人工覆写失败 (非致命): {e}")

        return financials

    def _get_latest_price(self, stock_code: str) -> dict[str, Any]:
        """获取最新收盘价"""
        rows = self.duck.read_query("""
            SELECT trade_date, close, volume, turnover
            FROM price_daily_raw
            WHERE stock_code = ? AND close IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT 1
        """, [stock_code])

        return rows[0] if rows else {}

    def _get_shares(self, stock_code: str, financials: dict) -> dict[str, Any]:
        """获取总股本和流通股本

        总股本从资产负债表的 paid_in_capital (实收资本/股本) 获取。
        流通股本暂用总股本（精确的流通股本需要额外数据源）。
        """
        total_shares = financials.get("paid_in_capital")
        return {
            "total_shares": total_shares,
            "circ_shares": total_shares,  # 简化：流通股本=总股本
        }

    def _get_ttm_data(self, stock_code: str) -> dict[str, Any]:
        """计算TTM（滚动十二个月）数据

        标准 TTM = 最近4个季度的单季度值之和
        实现：TTM = 最新累计值 - 去年同期累计值（如果去年同期有数据）
              或 TTM = 最新年报值（如果最新报告期是年报）

        PRD §8.1: 默认口径为 latest_restated。
        PRD §9.1: 允许 approximate 值，需标记 confidence。
        """
        # 获取最近8个季度的累计值（足够计算TTM和YoY）
        rows = self.duck.read_query("""
            SELECT
                ic.report_date,
                ic.revenue, ic.cost_of_revenue,
                (ic.revenue - ic.cost_of_revenue) AS gross_profit,
                ic.operating_profit, ic.net_profit,
                ic.parent_net_profit, ic.deducted_net_profit,
                ic.total_operating_revenue, ic.total_operating_cost,
                ic.selling_expenses, ic.administrative_expenses,
                ic.financial_expenses, ic.interest_expense,
                ic.investment_income, ic.total_profit, ic.income_tax,
                cf.cf_from_operating
            FROM income_statement ic
            LEFT JOIN cash_flow cf
                ON ic.stock_code = cf.stock_code AND ic.report_date = cf.report_date
            WHERE ic.stock_code = ?
            ORDER BY ic.report_date DESC
            LIMIT 8
        """, [stock_code])

        if not rows:
            return {}

        latest = rows[0]
        latest_date = str(latest.get("report_date", ""))

        # 情况1: 最新报告期是年报（12月31日），TTM = 年报值
        if "12-31" in latest_date:
            return latest

        # 情况2: 最新报告期是季报，用累计值差分
        # 正确TTM = 最新年报 + 最新累计 - 去年同期累计
        # 例: Q1 2025 TTM = 2024年报 + Q1_2025累计 - Q1_2024累计
        if len(rows) >= 5:
            current_cumulative = rows[0]
            year_ago_cumulative = rows[4]  # 4个季度前的同期累计

            # 找最近的年报（12-31）
            annual = None
            for row in rows:
                if "12-31" in str(row.get("report_date", "")):
                    annual = row
                    break

            ttm: dict[str, Any] = {"report_date": current_cumulative["report_date"]}
            for key in ["revenue", "cost_of_revenue", "gross_profit",
                        "operating_profit", "net_profit", "parent_net_profit",
                        "deducted_net_profit", "total_operating_revenue",
                        "total_operating_cost", "selling_expenses",
                        "administrative_expenses", "financial_expenses",
                        "interest_expense", "investment_income", "total_profit",
                        "income_tax", "cf_from_operating"]:
                curr = current_cumulative.get(key)
                prev = year_ago_cumulative.get(key)
                if annual and curr is not None and prev is not None:
                    ann_val = annual.get(key)
                    if ann_val is not None:
                        ttm[key] = ann_val + curr - prev
                    else:
                        ttm[key] = None
                elif curr is not None and prev is not None:
                    # P0#10修复: 无年报数据时, 累计差分 (curr-prev) 不是 TTM
                    # 例: Q1_2025 - Q1_2024 = 同比增量, 而非12个月滚动总和
                    # 正确做法: 返回 None 而非无意义值
                    ttm[key] = None
                    ttm["_ttm_confidence"] = "missing"
                    ttm["_ttm_reason"] = "no_annual_report"
                else:
                    ttm[key] = None
            if "_ttm_confidence" not in ttm:
                ttm["_ttm_confidence"] = "strict"
            return ttm

        # 情况3: 数据不足（少于5个季度），退化到最新累计值
        # P0#10修复: 如果最新是Q1(3个月), 返回累计值会产出3个月而非12个月的数据
        # 正确做法: 数据不足时返回 None, 标记为 missing, 而非返回无意义的累计值
        logger.warning(f"TTM 数据不足: {stock_code} (仅 {len(rows)} 个季度, 需要至少5个)")
        return {"_ttm_confidence": "missing", "_ttm_reason": "insufficient_history"}

    def _get_dividend_summary(self, stock_code: str) -> dict[str, Any]:
        """获取分红摘要"""
        rows = self.duck.read_query("""
            WITH valid_dividends AS (
                SELECT ex_date, dividend_per_share
                FROM dividends
                WHERE stock_code = ?
                  AND dividend_per_share IS NOT NULL
                  AND dividend_per_share > 0
            ), latest AS (
                SELECT MAX(EXTRACT(YEAR FROM ex_date)) AS latest_year
                FROM valid_dividends
            )
            SELECT
                COUNT(*) as total_records,
                COUNT(DISTINCT EXTRACT(YEAR FROM ex_date)) as years_with_dividend,
                MAX(dividend_per_share) as max_dps,
                AVG(dividend_per_share) as avg_dps,
                SUM(dividend_per_share) as total_dps,
                MAX(ex_date) as latest_ex_date,
                SUM(CASE WHEN EXTRACT(YEAR FROM ex_date) = latest.latest_year
                         THEN dividend_per_share ELSE 0 END) as latest_dps
            FROM valid_dividends
            CROSS JOIN latest
        """, [stock_code])

        return rows[0] if rows else {}

    # ─── 指标计算 ──────────────────────────────────────────────────

    def _calc_valuation(
        self, stock_code: str, price: dict, total_shares: float | None, circ_shares: float | None,
        ttm: dict, financials: dict, dividends: dict
    ) -> dict[str, Any]:
        """1. 估值指标"""
        result: dict[str, Any] = {}
        close = price.get("close")
        shares = total_shares or 0

        # 总市值 = 最新收盘价 × 总股本
        if close and shares:
            result["total_market_cap"] = close * shares
        else:
            result["total_market_cap"] = None

        # 流通市值 = 最新收盘价 × 流通股本
        if close and circ_shares:
            result["circ_market_cap"] = close * circ_shares
        else:
            result["circ_market_cap"] = None

        # PE-TTM = 总市值 / TTM归母净利润
        # V2-3.4修复: PE>1000 时置NULL（净利润接近0导致PE虚高，业务无意义）
        ttm_profit = ttm.get("parent_net_profit")
        if close and shares and ttm_profit and ttm_profit > 0:
            pe = (close * shares) / ttm_profit
            result["pe_ttm"] = pe if pe <= 1000 else None
        else:
            result["pe_ttm"] = None

        # PB-MRQ = 总市值 / 最新报告期归母权益
        # P0#9修复: PB>200 时置NULL (权益接近0导致PB虚高，业务无意义)
        equity = financials.get("total_equity_parent") or financials.get("total_equity")
        if close and shares and equity and equity > 0:
            pb = (close * shares) / equity
            result["pb_mrq"] = pb if pb <= 200 else None
        else:
            result["pb_mrq"] = None

        # PS-TTM = 总市值 / TTM营业收入
        ttm_revenue = ttm.get("revenue") or ttm.get("total_operating_revenue")
        if close and shares and ttm_revenue and ttm_revenue > 0:
            result["ps_ttm"] = (close * shares) / ttm_revenue
        else:
            result["ps_ttm"] = None

        # PCF-TTM = 总市值 / TTM经营现金流净额
        ttm_cf = ttm.get("cf_from_operating")
        if close and shares and ttm_cf and ttm_cf > 0:
            result["pcf_ttm"] = (close * shares) / ttm_cf
        else:
            result["pcf_ttm"] = None

        # 股息率 = 最近12个月每股股息 / 最新收盘价
        total_dps = dividends.get("total_dps")
        if close and close > 0 and total_dps and total_dps > 0:
            # 简化：用历史总DPS近似最近12个月DPS
            # 精确实现需要筛选最近一年的分红记录
            result["dividend_yield"] = self._calc_dividend_yield(stock_code, close)
        else:
            result["dividend_yield"] = None

        return result

    def _calc_dividend_yield(self, stock_code: str, close: float) -> float | None:
        """计算最近12个月股息率

        注: CSMAR学术数据集有滞后(截止2024-12-31), 使用2年窗口确保捕获最新分红.
        生产环境 vd data update 补充最新数据后可收紧到1年.
        """
        rows = self.duck.read_query("""
            SELECT SUM(dividend_per_share) as dps
            FROM dividends
            WHERE stock_code = ?
              AND dividend_per_share IS NOT NULL
              AND dividend_per_share > 0
              AND ex_date >= CURRENT_DATE - INTERVAL '2 years'
        """, [stock_code])

        if rows and rows[0].get("dps"):
            dps = rows[0]["dps"]
            return dps / close if close > 0 else None
        return None

    def _calc_profitability(self, ttm: dict, financials: dict, stock_code: str = "") -> dict[str, Any]:
        """2. 盈利能力指标"""
        result: dict[str, Any] = {}

        revenue = ttm.get("revenue") or ttm.get("total_operating_revenue")
        net_profit = ttm.get("net_profit")
        parent_profit = ttm.get("parent_net_profit")
        gross_profit = ttm.get("gross_profit")
        op_cf = ttm.get("cf_from_operating")

        # 如果TTM没有，退化到最新报告期
        if not revenue:
            revenue = financials.get("revenue") or financials.get("total_operating_revenue")
        if not net_profit:
            net_profit = financials.get("net_profit")
        if not parent_profit:
            parent_profit = financials.get("parent_net_profit")
        if not gross_profit:
            gross_profit = financials.get("gross_profit")

        # P1-24修复: ROE/ROA使用平均值(期初+期末)/2而非期末值
        # 获取上一期权益和总资产
        prev_equity = None
        prev_assets = None
        if stock_code:
            report_date = financials.get("report_date")
            if report_date:
                prev_rows = self.duck.read_query(
                    """SELECT total_equity_parent, total_equity, total_assets
                       FROM balance_sheet
                       WHERE stock_code = ? AND report_date < ?
                       ORDER BY report_date DESC LIMIT 1""",
                    [stock_code, str(report_date)],
                )
                if prev_rows:
                    prev_equity = prev_rows[0].get("total_equity_parent") or prev_rows[0].get("total_equity")
                    prev_assets = prev_rows[0].get("total_assets")

        # ROE = TTM归母净利润 / 平均归母权益
        # V2-3.3修复: |ROE|>1.0 时置NULL
        # P1-24修复: 用(期初+期末)/2而非期末值
        equity = financials.get("total_equity_parent") or financials.get("total_equity")
        avg_equity = equity
        if prev_equity and equity:
            avg_equity = (prev_equity + equity) / 2
        if parent_profit is not None and avg_equity and avg_equity > 0:
            roe = parent_profit / avg_equity
            result["roe"] = roe if abs(roe) <= 1.0 else None
        else:
            result["roe"] = None

        # ROA = TTM净利润 / 平均总资产
        # P1-24修复: 用(期初+期末)/2而非期末值
        total_assets = financials.get("total_assets")
        avg_assets = total_assets
        if prev_assets and total_assets:
            avg_assets = (prev_assets + total_assets) / 2
        if net_profit is not None and avg_assets and avg_assets > 0:
            result["roa"] = net_profit / avg_assets
        else:
            result["roa"] = None

        # 毛利率 = (营收 - 营业成本) / 营收
        # P0#9修复: 毛利率范围限制在 [-1, 1], 超出置 NULL
        cost = ttm.get("cost_of_revenue") or financials.get("cost_of_revenue")
        if revenue and revenue > 0 and cost is not None:
            gm = (revenue - cost) / revenue
            result["gross_margin"] = gm if -1.0 <= gm <= 1.0 else None
        elif gross_profit and revenue and revenue > 0:
            gm = gross_profit / revenue
            result["gross_margin"] = gm if -1.0 <= gm <= 1.0 else None
        else:
            result["gross_margin"] = None

        # 净利率 = 净利润 / 营收
        if net_profit is not None and revenue and revenue > 0:
            result["net_margin"] = net_profit / revenue
        else:
            result["net_margin"] = None

        # ROIC = TTM营业利润 / 投入资本
        # 投入资本 = 总权益 + 有息负债
        op_profit = ttm.get("operating_profit") or financials.get("operating_profit")
        interest_debt = self._calc_interest_bearing_debt(financials)
        invested_capital = None
        if equity and interest_debt is not None:
            invested_capital = equity + interest_debt
        if op_profit is not None and invested_capital and invested_capital > 0:
            result["roic"] = op_profit / invested_capital
        else:
            result["roic"] = None

        # 经营现金流 / 净利润
        if op_cf is not None and net_profit and net_profit > 0:
            result["cf_to_net_profit"] = op_cf / net_profit
        else:
            result["cf_to_net_profit"] = None

        return result

    def _calc_growth(self, stock_code: str) -> dict[str, Any]:
        """3. 成长指标"""
        result: dict[str, Any] = {}

        # 获取历史年度数据用于 YoY 和 CAGR
        rows = self.duck.read_query("""
            SELECT report_date, revenue, parent_net_profit, deducted_net_profit
            FROM income_statement
            WHERE stock_code = ?
              AND EXTRACT(MONTH FROM report_date) = 12
            ORDER BY report_date DESC
            LIMIT 7
        """, [stock_code])

        if not rows:
            result.update({
                "revenue_yoy": None, "net_profit_yoy": None,
                "deducted_profit_yoy": None,
                "revenue_cagr3": None, "revenue_cagr5": None,
                "net_profit_cagr3": None, "net_profit_cagr5": None,
                "deducted_profit_cagr3": None, "deducted_profit_cagr5": None,
            })
            return result

        # YoY = (今年 - 去年) / |去年|
        if len(rows) >= 2:
            curr, prev = rows[0], rows[1]
            result["revenue_yoy"] = self._yoy(curr.get("revenue"), prev.get("revenue"))
            result["net_profit_yoy"] = self._yoy(
                curr.get("parent_net_profit"), prev.get("parent_net_profit"))
            result["deducted_profit_yoy"] = self._yoy(
                curr.get("deducted_net_profit"), prev.get("deducted_net_profit"))
        else:
            result["revenue_yoy"] = None
            result["net_profit_yoy"] = None
            result["deducted_profit_yoy"] = None

        # CAGR = (末值/初值)^(1/n) - 1
        result["revenue_cagr3"] = self._cagr([r.get("revenue") for r in rows], 3)
        result["revenue_cagr5"] = self._cagr([r.get("revenue") for r in rows], 5)
        result["net_profit_cagr3"] = self._cagr(
            [r.get("parent_net_profit") for r in rows], 3)
        result["net_profit_cagr5"] = self._cagr(
            [r.get("parent_net_profit") for r in rows], 5)
        result["deducted_profit_cagr3"] = self._cagr(
            [r.get("deducted_net_profit") for r in rows], 3)
        result["deducted_profit_cagr5"] = self._cagr(
            [r.get("deducted_net_profit") for r in rows], 5)

        return result

    def _calc_safety(self, financials: dict, ttm: dict) -> dict[str, Any]:
        """4. 安全性指标"""
        result: dict[str, Any] = {}

        total_assets = financials.get("total_assets")
        total_liabilities = financials.get("total_liabilities")
        total_current_assets = financials.get("total_current_assets")
        total_current_liabilities = financials.get("total_current_liabilities")
        inventory = financials.get("inventory")

        # 资产负债率 = 总负债 / 总资产
        # P0#9修复: debt_ratio 范围限制在 [0, 1], 超出置 NULL
        if total_liabilities is not None and total_assets and total_assets > 0:
            dr = total_liabilities / total_assets
            result["debt_ratio"] = dr if 0.0 <= dr <= 1.0 else None
        else:
            result["debt_ratio"] = None

        # 流动比率 = 流动资产 / 流动负债
        if total_current_assets is not None and total_current_liabilities and total_current_liabilities > 0:
            result["current_ratio"] = total_current_assets / total_current_liabilities
        else:
            result["current_ratio"] = None

        # 速动比率 = (流动资产 - 存货) / 流动负债
        if (total_current_assets is not None and inventory is not None
                and total_current_liabilities and total_current_liabilities > 0):
            result["quick_ratio"] = (total_current_assets - inventory) / total_current_liabilities
        else:
            result["quick_ratio"] = None

        # 有息负债 = 短期借款 + 长期借款 + 应付债券
        result["interest_bearing_debt"] = self._calc_interest_bearing_debt(financials)

        # 利息保障倍数 = TTM营业利润 / TTM利息费用
        # V2-3.1修复: interest_expense 缺失时用 financial_expenses 近似（覆盖率从1.6%提升到~95%）
        op_profit = ttm.get("operating_profit") or financials.get("operating_profit")
        interest_expense = ttm.get("interest_expense") or financials.get("interest_expense")
        if interest_expense is None:
            # 多数企业只报告 financial_expenses 不单独报告 interest_expense
            interest_expense = ttm.get("financial_expenses") or financials.get("financial_expenses")
        if (op_profit is not None and interest_expense is not None
                and abs(interest_expense) > 0):
            result["interest_coverage"] = op_profit / abs(interest_expense)
        else:
            result["interest_coverage"] = None

        # 商誉占比 = 商誉 / 总资产
        # P2修复: goodwill为NULL时保持NULL（未披露≠无商誉）
        goodwill = financials.get("goodwill")
        if goodwill is not None and total_assets and total_assets > 0:
            result["goodwill_ratio"] = goodwill / total_assets
        else:
            result["goodwill_ratio"] = None

        return result

    def _calc_shareholder_return(
        self, stock_code: str, financials: dict, dividends: dict, ttm: dict
    ) -> dict[str, Any]:
        """5. 股东回报指标"""
        result: dict[str, Any] = {}

        # 分红率 = 每股股息 / 每股收益
        # P1-18修复: 使用最新一年DPS而非历史最大DPS
        dps = dividends.get("latest_dps")  # 最新一年DPS
        if dps is None:
            dps = dividends.get("max_dps")  # 回退到max（兼容旧数据）
        eps = ttm.get("parent_net_profit")
        shares = financials.get("paid_in_capital")
        if dps and eps and shares and shares > 0:
            ttm_eps = eps / shares
            if ttm_eps and ttm_eps > 0:
                result["payout_ratio"] = dps / ttm_eps
            else:
                result["payout_ratio"] = None
        else:
            result["payout_ratio"] = None

        # 每股股息 (DPS)
        result["dps"] = dps

        # 连续分红年数（从最近年份往前数连续分红的年数）
        result["consecutive_div_years"] = self._calc_consecutive_div_years(stock_code)

        return result

    def _calc_technical(self, stock_code: str) -> dict[str, Any]:
        """6. 行情与技术统计指标"""
        result: dict[str, Any] = {}

        # 获取最近250个交易日的价格数据（含换手率）
        rows = self.duck.read_query("""
            SELECT trade_date, close, volume, turnover, turnover_rate
            FROM price_daily_raw
            WHERE stock_code = ?
            ORDER BY trade_date DESC
            LIMIT 250
        """, [stock_code])

        if not rows:
            for key in ["ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
                        "turnover_rate", "avg_volume", "period_return",
                        "annualized_volatility", "max_drawdown"]:
                result[key] = None
            return result

        # 按时间正序排列（旧→新）
        rows.reverse()
        closes = [r["close"] for r in rows if r["close"] is not None]
        volumes = [r["volume"] for r in rows if r["volume"] is not None]
        turn_rates = [r["turnover_rate"] for r in rows if r["turnover_rate"] is not None]

        if not closes:
            for key in ["ma5", "ma10", "ma20", "ma60", "ma120", "ma250"]:
                result[key] = None
            return result

        # 移动平均线
        result["ma5"] = self._sma(closes, 5)
        result["ma10"] = self._sma(closes, 10)
        result["ma20"] = self._sma(closes, 20)
        result["ma60"] = self._sma(closes, 60)
        result["ma120"] = self._sma(closes, 120)
        result["ma250"] = self._sma(closes, 250)

        # 平均成交量 (最近20日)
        if volumes:
            recent_vols = volumes[-20:] if len(volumes) >= 20 else volumes
            result["avg_volume"] = sum(recent_vols) / len(recent_vols)
        else:
            result["avg_volume"] = None

        # 区间收益率 (最近250日)
        if len(closes) >= 2:
            result["period_return"] = (closes[-1] - closes[0]) / closes[0] if closes[0] != 0 else None
        else:
            result["period_return"] = None

        # 年化波动率 (最近60日日收益率标准差 × sqrt(250))
        if len(closes) >= 20:
            returns = []
            for i in range(1, min(len(closes), 60)):
                if closes[i - 1] and closes[i - 1] != 0:
                    returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
            if len(returns) >= 2:
                avg_ret = sum(returns) / len(returns)
                variance = sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1)
                result["annualized_volatility"] = math.sqrt(variance) * math.sqrt(250)
            else:
                result["annualized_volatility"] = None
        else:
            result["annualized_volatility"] = None

        # 最大回撤 (最近250日)
        if len(closes) >= 2:
            max_price = closes[0]
            max_drawdown = 0.0
            for price in closes[1:]:
                if price > max_price:
                    max_price = price
                if max_price > 0:
                    drawdown = (max_price - price) / max_price
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            result["max_drawdown"] = max_drawdown
        else:
            result["max_drawdown"] = None

        # 换手率 (最近20日平均)
        if turn_rates:
            recent_turns = turn_rates[-20:] if len(turn_rates) >= 20 else turn_rates
            result["turnover_rate"] = sum(recent_turns) / len(recent_turns)
        else:
            result["turnover_rate"] = None

        return result

    # ─── 辅助函数 ──────────────────────────────────────────────────

    @staticmethod
    def _yoy(curr: float | None, prev: float | None) -> float | None:
        """同比增长率"""
        if curr is None or prev is None or prev == 0:
            return None
        return (curr - prev) / abs(prev)

    @staticmethod
    def _cagr(values: list[float | None], n: int) -> float | None:
        """复合年增长率"""
        if len(values) < n + 1:
            return None
        end = values[0]
        start = values[n]
        if end is None or start is None or start <= 0 or end <= 0:
            return None
        return (end / start) ** (1.0 / n) - 1.0

    @staticmethod
    def _sma(values: list[float], period: int) -> float | None:
        """简单移动平均"""
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    @staticmethod
    def _calc_interest_bearing_debt(financials: dict) -> float | None:
        """有息负债 = 短期借款 + 长期借款 + 应付债券"""
        st = financials.get("short_term_loans") or 0
        lt = financials.get("long_term_loans") or 0
        bp = financials.get("bonds_payable") or 0
        total = st + lt + bp
        return total if total > 0 else None

    def _calc_consecutive_div_years(self, stock_code: str) -> int:
        """计算连续分红年数（从最近年份往前数连续分红的年数）

        例如：公司2018/2019/2021/2022年分红 → 返回2（2021-2022连续）
        如果2023也分红 → 返回3（2021-2023连续）
        """
        rows = self.duck.read_query("""
            SELECT DISTINCT EXTRACT(YEAR FROM ex_date) as yr
            FROM dividends
            WHERE stock_code = ?
              AND dividend_per_share IS NOT NULL
              AND dividend_per_share > 0
            ORDER BY yr DESC
        """, [stock_code])

        if not rows:
            return 0

        years = sorted([r["yr"] for r in rows], reverse=True)
        current_year = datetime.now().year

        # 允许最近1年没分红（可能是当年还未到分红日）
        # 从最近的分红年份开始往前数
        if not years:
            return 0

        latest_year = years[0]
        # 如果最新分红年份是近三年内，开始计数
        # (CSMAR学术数据集有滞后，放宽到 current_year-2; 生产环境 vd data update 会补充最新)
        if latest_year < current_year - 2:
            return 0  # 最近三年都没分红

        consecutive = 0
        expected_year = latest_year
        for year in years:
            if year == expected_year:
                consecutive += 1
                expected_year -= 1
            else:
                break

        return consecutive

    # ─── 写入 ──────────────────────────────────────────────────────

    def _cleanup_snapshot_staging_tables(self) -> None:
        tables = self.duck.read_query(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name LIKE 'indicator_snapshot_staging_%'
            """
        )
        if not tables:
            return
        with self.duck.write_connection() as connection:
            for table in tables:
                connection.execute(f'DROP TABLE IF EXISTS "{table["table_name"]}"')

    def _publish_snapshot(self, staging_table: str, expected_count: int) -> None:
        with self.duck.transaction() as connection:
            row_count = connection.execute(
                f'SELECT COUNT(*) FROM "{staging_table}"'
            ).fetchone()[0]
            duplicate_count = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT stock_code, report_date
                    FROM "{staging_table}"
                    GROUP BY stock_code, report_date
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            missing_key_count = connection.execute(
                f"""
                SELECT COUNT(*) FROM "{staging_table}"
                WHERE stock_code IS NULL OR report_date IS NULL
                """
            ).fetchone()[0]
            if row_count != expected_count or duplicate_count or missing_key_count:
                raise RuntimeError(
                    "snapshot validation failed: "
                    f"rows={row_count}, expected={expected_count}, "
                    f"duplicates={duplicate_count}, missing_keys={missing_key_count}"
                )
            connection.execute("DELETE FROM indicator_snapshot")
            connection.execute(
                f'INSERT INTO indicator_snapshot BY NAME SELECT * FROM "{staging_table}"'
            )

    def _write_batch(self, records: list[dict], table_name: str = "indicator_snapshot") -> None:
        """批量写入指标快照

        P2修复: 使用executemany批量INSERT（比逐条快10倍+）
        """
        if not records:
            return

        cols_info = self.duck.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table_name],
        )
        available_cols = {c["column_name"] for c in cols_info}

        with self.duck.write_connection() as conn:
            for rec in records:
                fields = [k for k in rec if k in available_cols]
                values = [rec[k] for k in fields]
                placeholders = ", ".join(["?"] * len(fields))
                field_str = ", ".join(fields)
                conn.execute(
                    f'INSERT INTO "{table_name}" ({field_str}) VALUES ({placeholders})',
                    values,
                )
