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

import contextlib
import hashlib
import logging
import math
import uuid
from bisect import bisect_right
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.adapters.czb_mof_adapter import CZB_CURVE_YIELD_TENOR_LABELS, KEY_TENORS
from app.core.financial_period import cn_today
from app.core.market_cap import latest_market_cap, market_cap_from
from app.core.storage.duckdb_store import DuckDBStore, archive_raw_response_if_absent
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore
from app.core.treasury import MAX_STALENESS_DAYS

logger = logging.getLogger(__name__)

SNAPSHOT_TABLE = "indicator_snapshot"


@dataclass(frozen=True)
class DerivedSnapshotDomain:
    """一个"不由本计算器生产、但住在 indicator_snapshot 里"的派生域。

    背景（`reports/133` R3 / `reports/131` K5 / D28）
    ------------------------------------------------
    `indicator_snapshot` 的发布是"整表/整批 DELETE + INSERT"。凡是**不由本
    计算器 staging 产出**的列（ROCE 六列、H 股市值几列、任何以后新增的
    外部派生列），INSERT 时都不会被写入 → 静默变 NULL。这不是某一次的 bug，
    而是"派生列没有重建触发、也没有保全契约"这一类事故的固定生产机制：

    * 2026-11-16 `reports/130` ROCE 落地当晚，每次快照重发把全市场 5,575 行
      `roce_*` 清成 NULL（`reports/131` K5）；
    * 同型事故 D28（`capital_history` 域冻结）；
    * 同一个补丁只补了整表发布路径，**增量发布路径 `_publish_snapshot_delta`
      仍在清空**——本守卫上线当天就会把它照出来。

    因此每个派生域必须显式登记：列名（用于保全 + 校验）+ 重建函数（用于
    保持数值新鲜）。登记后由 `_derived_columns_guard()` 在两条发布路径上
    统一执行"先保全、再重建、最后校验没有列被清空"。
    """

    name: str
    columns: tuple[str, ...]
    rebuild: Callable[[Any], None]


def derived_snapshot_domains() -> tuple[DerivedSnapshotDomain, ...]:
    """indicator_snapshot 派生域注册表（新增派生列必须在这里登记）。"""
    from app.core.indicators import roce

    return (
        DerivedSnapshotDomain(
            name="roce",
            columns=roce.SNAPSHOT_DERIVED_COLUMNS,
            rebuild=roce.rebuild_snapshot_columns,
        ),
    )


class IndicatorCalculator:
    """内建指标计算器

    从 DuckDB 读取财务报表和价格数据，计算全部内建指标，
    物化到 indicator_snapshot 表。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("IndicatorCalculator requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("IndicatorCalculator stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite

    def compute_all_for_stock(self, stock_code: str) -> dict[str, Any]:
        """计算单只股票的全部内建指标

        Returns:
            包含所有指标值的字典，值可能为 None (缺失)
        """
        result: dict[str, Any] = {
            "stock_code": stock_code,
            "calculated_at": datetime.now(UTC),
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
        ttm = self._get_ttm_data(stock_code, financials.get("report_date"))

        # 获取分红数据
        dividends = self._get_dividend_summary(stock_code, financials.get("report_date"))

        # ─── 0. 分红融资比数据前置（reports/82 后续指标）──────────
        result["cumulative_dividend_amount"] = self._get_cumulative_dividend_amount(stock_code)
        result["cumulative_financing_amount"] = self._get_cumulative_financing_amount(stock_code)
        if (
            result["cumulative_dividend_amount"] is not None
            and result["cumulative_financing_amount"] is not None
            and result["cumulative_financing_amount"] != 0
        ):
            result["dividend_financing_ratio_pct"] = (
                result["cumulative_dividend_amount"]
                / result["cumulative_financing_amount"]
                * 100.0
            )
        else:
            result["dividend_financing_ratio_pct"] = None

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
        result.update(self._calc_shareholder_return(stock_code, financials, dividends, ttm, total_shares))

        # ─── 6. 行情指标 ──────────────────────────────────────────
        result.update(self._calc_technical(stock_code))

        # ─── 7. 国债基准与股息率利差（reports/68 P3） ───────────────
        result.update(self._calc_treasury_spread(stock_code))

        return result

    def compute_price_fields_for_stock(
        self, stock_code: str, report_date: Any,
    ) -> dict[str, Any]:
        """只重算与价格相关的指标（估值/行情/国债利差），复用既有报告期。

        2026-09-08 提速：日常价格更新只改变价格域字段，盈利/成长/安全/
        股东回报/分红融资比等基本面字段无需重算。report_date 必须等于
        当前最新完整报告期，否则调用方应走完整重算。
        """
        financials = self._get_latest_financials(stock_code)
        if str(financials.get("report_date") or "") != str(report_date or ""):
            raise ValueError(
                f"{stock_code} financial period changed to "
                f"{financials.get('report_date')}; full recompute required"
            )
        result: dict[str, Any] = {
            "stock_code": stock_code,
            "report_date": report_date,
            "calculated_at": datetime.now(UTC),
            "data_version": "audit-safe-v1",
        }
        price_info = self._get_latest_price(stock_code)
        result["latest_close"] = price_info.get("close")
        result["latest_price_date"] = price_info.get("trade_date")
        shares = self._get_shares(stock_code, financials)
        ttm = self._get_ttm_data(stock_code, report_date)
        dividends = self._get_dividend_summary(stock_code, report_date)
        result.update(self._calc_valuation(
            stock_code, price_info, shares.get("total_shares"),
            shares.get("circ_shares"), ttm, financials, dividends,
        ))
        result.update(self._calc_technical(stock_code))
        result.update(self._calc_treasury_spread(stock_code))
        return result

    def compute_price_sensitive_for_codes(
        self,
        stock_codes: list[str],
        *,
        force_codes: set[str] | None = None,
        publish_gate: Callable[[DuckDBStore, SQLiteStore], dict[str, Any]] | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """只更新价格域指标；最新价与既有快照相同且非强制的股票直接跳过。

        只有价格真正变化的股票才重算，未变化股票连查询都省掉；既有快照的
        报告期保持不变，基本面字段完全不触碰。
        """
        requested_codes = list(dict.fromkeys(stock_codes))
        if not requested_codes:
            return {"status": "skipped", "reason": "no_changed_stocks", "success": 0, "failed": 0, "skipped": 0}

        # 契约 §4：hard_block 才整批拒绝；否则逐股隔离后发布其余。
        gate_report = self._run_publish_gate(publish_gate)
        if gate_report.get("hard_block"):
            return self._gate_hard_block_result(gate_report, len(requested_codes))
        codes, quarantined_codes = self._quarantine_split(requested_codes, gate_report)
        quarantine = self._quarantine_fields(quarantined_codes)
        if not codes:
            return {
                "status": "partial",
                "reason": "publish_gate_quarantined",
                "total": len(requested_codes), "success": 0, "failed": 0, "skipped": 0,
                **quarantine,
            }

        force = set(force_codes or ())
        placeholders = ", ".join("?" for _ in codes)
        existing_rows = self.duck.read_query(
            f"""SELECT stock_code, report_date, latest_close, latest_price_date
                FROM indicator_snapshot WHERE stock_code IN ({placeholders})""",
            codes,
        )
        existing = {
            row["stock_code"]: {
                "report_date": row["report_date"],
                "latest_close": row["latest_close"],
                "latest_price_date": row["latest_price_date"],
            }
            for row in existing_rows
        }
        raw_rows = self.duck.read_query(
            f"""SELECT stock_code,
                       MAX(trade_date) AS latest_date,
                       MAX_BY(close, trade_date) AS latest_close
                FROM price_daily_raw
                WHERE stock_code IN ({placeholders}) AND close IS NOT NULL
                GROUP BY stock_code""",
            codes,
        )
        raw = {row["stock_code"]: row for row in raw_rows}

        changed: list[str] = []
        full_fallback: list[str] = []
        skipped = 0
        for code in codes:
            old = existing.get(code)
            if old is None or old.get("report_date") is None:
                full_fallback.append(code)
                continue
            new = raw.get(code)
            if code in force or new is None:
                changed.append(code)
                continue
            same_date = str(old.get("latest_price_date") or "")[:10] == str(new["latest_date"])[:10]
            old_close = old.get("latest_close")
            new_close = new["latest_close"]
            same_close = (
                isinstance(old_close, (int, float))
                and isinstance(new_close, (int, float))
                and math.isclose(float(old_close), float(new_close), rel_tol=1e-9, abs_tol=1e-9)
            )
            if same_date and same_close:
                skipped += 1
            else:
                changed.append(code)

        full_report: dict[str, Any] = {"status": "skipped", "success": 0, "failed": 0}
        if full_fallback:
            full_report = self.compute_snapshot_for_codes(full_fallback, progress_cb=progress_cb)

        records: list[dict[str, Any]] = []
        failed_codes: list[str] = []
        with self.duck.read_connection() as connection:
            self._calculation_read_connection = connection
            try:
                for done, code in enumerate(changed, start=1):
                    try:
                        indicators = self.compute_price_fields_for_stock(
                            code, existing[code]["report_date"],
                        )
                        records.append(indicators)
                    except Exception as error:
                        logger.debug("价格域增量计算 %s 失败: %s", code, error)
                        failed_codes.append(code)
                    if progress_cb is not None:
                        progress_cb({
                            "step": "indicators_price",
                            "label": "指标快照·价格域",
                            "done": done,
                            "total": len(changed),
                            "current": code,
                        })
            finally:
                del self._calculation_read_connection

        if records:
            self._update_snapshot_price_fields(records)

        quarantined = bool(quarantine)
        status = (
            "success"
            if not failed_codes and not quarantined and full_report.get("status") != "partial"
            else "partial"
        )
        if failed_codes:
            reason = "changed_stock_not_ready"
        elif quarantined:
            reason = "publish_gate_quarantined"
        else:
            reason = None
        return {
            "status": status,
            "reason": reason,
            "total": len(requested_codes),
            "success": len(records),
            "skipped": skipped,
            "failed": len(failed_codes) + int(full_report.get("failed") or 0),
            "failed_codes": (failed_codes + list(full_report.get("failed_codes") or []))[:20],
            "full_recompute": full_report,
            **quarantine,
        }

    def refresh_funding_dividend_fields(self, stock_codes: list[str]) -> dict[str, Any]:
        """只重算分红融资比三列（回购/融资事件变化专用，2026-09-08 提速）。"""
        codes = list(dict.fromkeys(stock_codes))
        if not codes:
            return {"status": "skipped", "reason": "no_changed_stocks", "total": 0, "success": 0, "failed": 0}
        placeholders = ", ".join("?" for _ in codes)
        rows = self.duck.read_query(
            f"""SELECT stock_code, report_date FROM indicator_snapshot
                WHERE stock_code IN ({placeholders})""",
            codes,
        )
        report_dates = {row["stock_code"]: row["report_date"] for row in rows}
        records: list[dict[str, Any]] = []
        failed_codes: list[str] = []
        with self.duck.read_connection() as connection:
            self._calculation_read_connection = connection
            try:
                for code in codes:
                    report_date = report_dates.get(code)
                    if report_date is None:
                        failed_codes.append(code)
                        continue
                    try:
                        dividend = self._get_cumulative_dividend_amount(code)
                        financing = self._get_cumulative_financing_amount(code)
                        ratio = None
                        if dividend is not None and financing not in (None, 0):
                            ratio = dividend / financing * 100.0
                        records.append({
                            "stock_code": code,
                            "report_date": report_date,
                            "cumulative_dividend_amount": dividend,
                            "cumulative_financing_amount": financing,
                            "dividend_financing_ratio_pct": ratio,
                            "calculated_at": datetime.now(UTC),
                            "data_version": "audit-safe-v1",
                        })
                    except Exception as error:
                        logger.debug("分红融资比增量计算 %s 失败: %s", code, error)
                        failed_codes.append(code)
            finally:
                del self._calculation_read_connection

        if records:
            self._update_snapshot_funding_dividend_fields(records)
        return {
            "status": "success" if not failed_codes else "partial",
            "reason": None if not failed_codes else "changed_stock_not_ready",
            "total": len(codes),
            "success": len(records),
            "failed": len(failed_codes),
            "failed_codes": failed_codes[:20],
        }

    def _snapshot_publish_gate(self) -> dict[str, Any]:
        from app.core.data_quality import snapshot_publish_gate
        return snapshot_publish_gate(self.duck, self.sqlite)

    # ─── 发布门禁（2026-11-12 契约 §4）──────────────────────────────
    def _run_publish_gate(
        self,
        publish_gate: Callable[[DuckDBStore, SQLiteStore], dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Resolve and run the publish gate exactly once per publish call."""
        if publish_gate is None:
            from app.core.data_quality import snapshot_publish_gate

            publish_gate = snapshot_publish_gate
        return publish_gate(self.duck, self.sqlite)

    def _gate_hard_block_result(
        self, gate_report: dict[str, Any], total: int,
    ) -> dict[str, Any]:
        """§4.1：只有 hard_block 才整批拒绝（一条不写，保留上一代快照）。"""
        logger.error(
            "指标快照发布被质量门禁硬拒绝(保留上一代快照): reason=%s violations=%s/%s",
            gate_report.get("reason"),
            gate_report.get("violation_count"),
            gate_report.get("threshold"),
        )
        return {
            "status": "rejected",
            "reason": "publish_gate_failed",
            "gate": gate_report,
            "total": total,
            "success": 0,
            "failed": total,
        }

    @staticmethod
    def _quarantine_split(
        codes: Sequence[str], gate_report: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        """§4.2：把隔离股从本批发布集合里剔除（返回 allowed, quarantined）。"""
        quarantined_codes = {str(code) for code in (gate_report.get("quarantined_codes") or ())}
        if not quarantined_codes:
            return list(codes), []
        allowed = [code for code in codes if code not in quarantined_codes]
        excluded = [code for code in codes if code in quarantined_codes]
        return allowed, excluded

    @staticmethod
    def _quarantine_fields(quarantined_codes: Sequence[str]) -> dict[str, Any]:
        """§4.2 附带的披露字段（最多 20 个代码 + 全量计数）。"""
        if not quarantined_codes:
            return {}
        return {
            "quarantined_codes": [str(code) for code in quarantined_codes][:20],
            "quarantined_count": len(quarantined_codes),
        }

    def compute_snapshot_for_all(
        self,
        batch_size: int = 100,
        *,
        publish_gate: Callable[[DuckDBStore, SQLiteStore], dict[str, Any]] | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """为所有有财务数据的股票计算指标快照并写入 indicator_snapshot 表

        Args:
            batch_size: 每批处理的股票数量
            publish_gate: 发布前质量门禁 (duck, sqlite) -> {"ready": bool, ...}；
                默认使用 data_quality.snapshot_publish_gate（股本关系完整性），
                调用方可注入更严格的门禁（如 screening_readiness）。

        Returns:
            计算报告
        """
        # A direct CLI invocation must obey the same minimum-data contract as
        # initialization.  Do not publish a screenable snapshot for a subset
        # of the current universe while another stock lacks core inputs.
        readiness = self.duck.read_query("""
            SELECT
                m.stock_code,
                m.listing_date,
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
        """)
        # 契约 §4：门禁先跑。只有 hard_block 才整批拒绝（保留上一代快照）；
        # 否则把 quarantined_codes 从本批发布集合剔除后照常发布其余股票。
        gate_report = self._run_publish_gate(publish_gate)
        if gate_report.get("hard_block"):
            return self._gate_hard_block_result(gate_report, len(readiness))
        listed_codes = [row["stock_code"] for row in readiness]
        publishable_codes, quarantined_codes = self._quarantine_split(listed_codes, gate_report)
        publishable = set(publishable_codes)
        quarantine = self._quarantine_fields(quarantined_codes)
        # 先剔除隔离股再判断缺口：隔离股的数据缺口由门禁统一披露，
        # 绝不能再借 minimum_data_not_ready 把整批发布拖死（2026-11-12 P0）。
        missing_codes = [
            row["stock_code"]
            for row in readiness
            if row["stock_code"] in publishable
            and not (
                row["has_core_financials"]
                and row["has_raw_price"]
                and row["has_qfq_price"]
            )
            and not (
                row.get("listing_date") is not None
                and str(row["listing_date"]) >= str(datetime.now().date() - timedelta(days=90))
            )
        ]
        # 2026-11-12 P0 收尾（与门禁同一政策）：输入缺口同样是**逐股排除**，
        # 不是一票否决。此前只要有一只非新股缺核心三表/价格，整批 5,580 只
        # 就不发布——和 share_capital 门禁是同一类"单点拖死全站"缺陷。
        # 只有缺口规模超过同一阈值（max(50, 1%)）才视为系统性缺失、整批拒绝。
        if missing_codes:
            from app.core.data_quality import gate_hard_block_threshold

            gap_threshold = gate_hard_block_threshold(len(readiness))
            if len(missing_codes) > gap_threshold:
                logger.error(
                    "指标输入缺口 %s 只 > 阈值 %s 只，整批拒绝发布",
                    len(missing_codes), gap_threshold,
                )
                return {
                    "status": "partial",
                    "reason": "minimum_data_not_ready",
                    "total": len(readiness),
                    "success": 0,
                    "failed": len(missing_codes),
                    "failed_codes": missing_codes[:20],
                    **quarantine,
                }
            logger.warning(
                "指标输入缺口 %s 只（<= 阈值 %s），逐股排除后继续发布其余股票: %s",
                len(missing_codes), gap_threshold, ", ".join(missing_codes[:20]),
            )
            publishable = publishable - set(missing_codes)
            quarantine = {
                **quarantine,
                "reason": "minimum_data_not_ready",
                "missing_input_codes": missing_codes[:20],
                "missing_input_count": len(missing_codes),
            }
        stocks = [
            {"stock_code": row["stock_code"]}
            for row in readiness
            if row["stock_code"] in publishable
            and row["has_core_financials"] and row["has_raw_price"] and row["has_qfq_price"]
        ]
        total = len(stocks)
        if total == 0:
            quarantined_count = int(quarantine.get("quarantined_count") or 0)
            if quarantined_count:
                # 2026-11-13 P1：整库被门禁隔离 = "永不出新快照"的事故形态，
                # 绝不能用 partial 掩盖（partial 会被上层当"部分成功"放过）。
                # 返回 rejected，让 update.py 的 merge_step_status 冒泡到最坏值。
                logger.error(
                    "指标快照发布集合被门禁隔离光，整批拒绝发布: quarantined=%s",
                    quarantined_count,
                )
                return {
                    "status": "rejected",
                    "reason": "publish_gate_quarantined_all",
                    "total": len(readiness),
                    "success": 0,
                    "failed": 0,
                    **quarantine,
                }
            if missing_codes:
                # 2026-11-13 修复：`quarantine` 也可能只承载"输入缺口"标记
                # （没有 quarantined_count），此前直接取键会 KeyError。
                return {
                    "status": "partial",
                    "reason": "minimum_data_not_ready",
                    "total": len(readiness),
                    "success": 0,
                    "failed": len(missing_codes),
                    "failed_codes": missing_codes[:20],
                    **quarantine,
                }
            if quarantine:
                return {
                    "status": "partial",
                    "reason": quarantine.get("reason") or "no_publishable_stocks",
                    "total": len(readiness),
                    "success": 0,
                    "failed": 0,
                    **quarantine,
                }
            return {"status": "skipped", "reason": "no stocks in stock_meta"}

        logger.info(f"开始计算指标快照: {total} 只股票")

        success = 0
        failed = 0
        failed_codes: list[str] = []
        computed_records: list[dict[str, Any]] = []

        # Reuse one snapshot-consistent read connection. Opening a new DuckDB
        # connection for every metric query makes a full-universe calculation
        # take hours.
        with self.duck.read_connection() as connection:
            self._calculation_read_connection = connection
            try:
                for i in range(0, total, batch_size):
                    batch = stocks[i:i + batch_size]

                    for stock in batch:
                        code = stock["stock_code"]
                        try:
                            indicators = self.compute_all_for_stock(code)
                            if indicators.get("report_date") is None:
                                failed += 1
                                failed_codes.append(code)
                                continue
                            computed_records.append(indicators)
                            success += 1
                        except Exception as error:
                            failed += 1
                            failed_codes.append(code)
                            logger.debug("计算 %s 指标失败: %s", code, error)

                    done = min(i + batch_size, total)
                    logger.info(
                        "  指标计算进度: %s/%s (成功 %s, 失败 %s)",
                        done,
                        total,
                        success,
                        failed,
                    )
                    if progress_cb is not None:
                        progress_cb({
                            "step": "indicators",
                            "label": "指标快照",
                            "done": done,
                            "total": total,
                            "current": batch[-1]["stock_code"] if batch else None,
                        })
            finally:
                del self._calculation_read_connection

        # A partial recalculation must not erase previously usable snapshots.
        # Publish only a complete replacement; callers receive failed codes and
        # can retry without exposing a mixed-generation screening dataset.
        # 2026-08-28 提速计划：关闭读连接后，按 batch 分批提交 staging 写入
        # （默认每 100 只一个 DuckDB 事务），避免 5,500+ 行单次 executemany
        # 造成超大事务；最终仍用单事务发布，原子性不变。
        if success > 0 and failed == 0:
            staging_table = f"indicator_snapshot_staging_{uuid.uuid4().hex}"
            self._cleanup_snapshot_staging_tables()
            self.duck.write_query(
                f'CREATE TABLE "{staging_table}" AS '
                "SELECT * FROM indicator_snapshot WHERE FALSE"
            )
            try:
                for i in range(0, len(computed_records), batch_size):
                    self._write_batch(computed_records[i:i + batch_size], staging_table)
                self._publish_snapshot(
                    staging_table, expected_count=success, records=computed_records,
                    # 隔离股与"输入缺口股"的上一代已发布行必须原样保留
                    # （契约 §4.2）。否则逐股排除会退化成"删掉旧行"，
                    # 反而让这些股票彻底从详情页消失。
                    preserve_codes=[*quarantined_codes, *missing_codes],
                )
            finally:
                self.duck.write_query(f'DROP TABLE IF EXISTS "{staging_table}"')

        logger.info("指标快照计算完成: 成功 %s, 失败 %s, 隔离 %s", success, failed, len(quarantined_codes))
        if failed or quarantine:
            reason = "changed_stock_not_ready" if failed else "publish_gate_quarantined"
        else:
            reason = None
        return {
            "status": "success" if not failed and not quarantine else "partial",
            "reason": reason,
            "total": total,
            "success": success,
            "failed": failed,
            "failed_codes": failed_codes[:20],
            **quarantine,
        }

    def compute_snapshot_for_codes(
        self,
        stock_codes: list[str],
        *,
        publish_gate: Callable[[DuckDBStore, SQLiteStore], dict[str, Any]] | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Atomically replace snapshots only for stocks whose prices changed."""
        requested_codes = list(dict.fromkeys(stock_codes))
        if not requested_codes:
            return {"status": "skipped", "reason": "no_changed_stocks", "success": 0, "failed": 0}

        # 契约 §4：hard_block 才整批拒绝；否则逐股隔离，其余照常发布。
        gate_report = self._run_publish_gate(publish_gate)
        if gate_report.get("hard_block"):
            return self._gate_hard_block_result(gate_report, len(requested_codes))
        codes, quarantined_codes = self._quarantine_split(requested_codes, gate_report)
        quarantine = self._quarantine_fields(quarantined_codes)
        if not codes:
            return {
                "status": "partial", "reason": "publish_gate_quarantined",
                "total": len(requested_codes), "success": 0, "failed": 0,
                **quarantine,
            }

        records: list[dict[str, Any]] = []
        failed_codes: list[str] = []
        with self.duck.read_connection() as connection:
            self._calculation_read_connection = connection
            try:
                for done, code in enumerate(codes, start=1):
                    try:
                        indicators = self.compute_all_for_stock(code)
                        if indicators.get("report_date") is None:
                            failed_codes.append(code)
                        else:
                            records.append(indicators)
                    except Exception as error:
                        logger.debug("增量计算 %s 指标失败: %s", code, error)
                        failed_codes.append(code)
                    if progress_cb is not None:
                        progress_cb({
                            "step": "indicators",
                            "label": "指标快照",
                            "done": done,
                            "total": len(codes),
                            "current": code,
                        })
            finally:
                del self._calculation_read_connection

        if not records and failed_codes:
            return {
                "status": "partial", "reason": "changed_stock_not_ready",
                "total": len(requested_codes), "success": 0, "failed": len(failed_codes),
                "failed_codes": failed_codes[:20],
                **quarantine,
            }

        # 2026-08-27：即使少数股票计算失败，也要发布已成功计算的股票。
        # 之前 any-fail 就整体不发布，导致价格已更新但快照全市场停留在旧日期，
        # readiness 出现 snapshot_price_coherence 大范围误报。
        # 2026-08-28 提速计划：增量路径只 DELETE+INSERT 已成功计算的股票并
        # 原子提交，不再把全市场快照复制到 staging 表后再整表回写。计算失败的
        # 股票保留上一代快照（stale 披露由 readiness 负责），绝不被删除。
        self._publish_snapshot_delta(
            [str(record["stock_code"]) for record in records],
            records,
        )
        if failed_codes:
            reason = "changed_stock_not_ready"
        elif quarantine:
            reason = "publish_gate_quarantined"
        else:
            reason = None
        return {
            "status": "success" if not failed_codes and not quarantine else "partial",
            "reason": reason,
            "total": len(requested_codes), "success": len(records),
            "failed": len(failed_codes), "failed_codes": failed_codes[:20],
            **quarantine,
        }

    def refresh_treasury_spreads(
        self,
        stock_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """仅刷新快照中的 TTM 股息率与国债利差列。

        国债曲线每日变化会让全市场快照的 div_yield_spread_* 过期；此前
        通过 compute_snapshot_for_codes 对 5,500+ 只股票完整重算全部指标，
        耗时约 12 分钟。这里把价格日/分红/曲线对齐放到 Python 内完成，
        DuckDB 只执行一次批量 UPDATE 与衍生 lineage，正式库可缩短到秒级。
        """
        where = ""
        params: list[Any] = []
        if stock_codes:
            placeholders = ", ".join("?" for _ in stock_codes)
            where = f" AND snap.stock_code IN ({placeholders})"
            params = list(stock_codes)
        rows = self.duck.read_query(
            f"""SELECT snap.stock_code, snap.report_date,
                       snap.latest_price_date, snap.latest_close,
                       snap.calculated_at, snap.data_version
                FROM indicator_snapshot snap
                WHERE snap.latest_close IS NOT NULL{where}
                ORDER BY snap.stock_code""",
            params,
        )
        if not rows:
            return {"status": "skipped", "reason": "no_snapshot_rows", "total": 0, "success": 0}

        codes = [row["stock_code"] for row in rows]
        price_by_code = {
            row["stock_code"]: str(row["latest_price_date"])[:10]
            for row in rows
        }
        # 一次 SQL 聚合每只股票截至各自价格日的 TTM 已实施现金分红。
        placeholders = ", ".join("?" for _ in codes)
        div_rows = self.duck.read_query(
            f"""SELECT b.stock_code, SUM(d.dividend_per_share) AS dps
                FROM (
                    SELECT stock_code, CAST(latest_price_date AS DATE) AS price_date
                    FROM indicator_snapshot
                    WHERE stock_code IN ({placeholders})
                ) b
                JOIN dividends d
                  ON d.stock_code = b.stock_code
                 AND d.dividend_per_share IS NOT NULL
                 AND d.dividend_per_share > 0
                 AND d.announcement_date IS NOT NULL
                 AND d.ex_date IS NOT NULL
                 AND d.announcement_date <= b.price_date
                 AND d.ex_date <= b.price_date
                 AND d.ex_date >= b.price_date - INTERVAL '1 year'
                GROUP BY b.stock_code""",
            codes,
        )
        dps_by_code = {row["stock_code"]: float(row["dps"] or 0.0) for row in div_rows}

        curve_rows = self.duck.read_query(
            """SELECT tenor_years, curve_date, yield_pct
               FROM treasury_yield_curve
               ORDER BY tenor_years, curve_date"""
        )
        curves: dict[float, tuple[list[date], list[float]]] = {}
        for row in curve_rows:
            tenor = float(row["tenor_years"])
            dates, values = curves.setdefault(tenor, ([], []))
            dates.append(date.fromisoformat(str(row["curve_date"])[:10]))
            values.append(float(row["yield_pct"]))

        published_at = datetime.now(UTC)
        update_rows: list[list[Any]] = []
        lineage_rows: list[dict[str, Any]] = []
        for row in rows:
            code = row["stock_code"]
            price_date = date.fromisoformat(price_by_code[code])
            close = float(row["latest_close"])
            dps = dps_by_code.get(code, 0.0)
            ttm_yield = (dps / close) * 100.0 if dps > 0 and close > 0 else None
            item: dict[str, Any] = {
                "stock_code": code,
                "report_date": row["report_date"],
                "latest_price_date": row["latest_price_date"],
                "calculated_at": published_at,
                "data_version": row["data_version"],
                "ttm_dividend_yield": ttm_yield,
            }
            values = [ttm_yield]
            for tenor in KEY_TENORS:
                dates, yields = curves.get(tenor, ([], []))
                index = bisect_right(dates, price_date) - 1
                spread = None
                if index >= 0 and ttm_yield is not None:
                    curve_date = dates[index]
                    if (price_date - curve_date).days <= MAX_STALENESS_DAYS:
                        spread = ttm_yield - yields[index]
                item[CZB_CURVE_YIELD_TENOR_LABELS[tenor]] = spread
                values.append(spread)
            update_rows.append((values, code))
            lineage_rows.append(item)

        with self.duck.transaction() as connection:
            set_fields = ["ttm_dividend_yield = ?"]
            set_fields.extend(
                f"{CZB_CURVE_YIELD_TENOR_LABELS[tenor]} = ?"
                for tenor in KEY_TENORS
            )
            connection.executemany(
                f"UPDATE indicator_snapshot SET {', '.join(set_fields)}, "
                f"calculated_at = ? WHERE stock_code = ?",
                [[*values, published_at, code] for values, code in update_rows],
            )
            self._record_derived_lineage_in_connection(connection, lineage_rows)

        return {
            "status": "success",
            "total": len(rows),
            "success": len(rows),
            "failed": 0,
        }

    _PRICE_SENSITIVE_FIELDS: tuple[str, ...] = (
        "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "dividend_yield",
        "total_market_cap", "circ_market_cap",
        "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
        "latest_close", "latest_price_date", "turnover_rate", "avg_volume",
        "period_return", "annualized_volatility", "max_drawdown",
        "ttm_dividend_yield",
        *(CZB_CURVE_YIELD_TENOR_LABELS[tenor] for tenor in KEY_TENORS),
    )

    _FUNDING_DIVIDEND_FIELDS: tuple[str, ...] = (
        "cumulative_dividend_amount",
        "cumulative_financing_amount",
        "dividend_financing_ratio_pct",
    )

    # 一次性补偿脚本写库的字段子集（scripts/backfill_technical_indicators.py /
    # scripts/backfill_yoy_calibers.py）。放在这里是为了让"计算 → 落库"只有一处实现。
    _TECHNICAL_SNAPSHOT_FIELDS: tuple[str, ...] = (
        "period_return",
        "annualized_volatility",
        "max_drawdown",
    )
    _YOY_SNAPSHOT_FIELDS: tuple[str, ...] = (
        "revenue_yoy", "revenue_yoy_fy",
        "net_profit_yoy", "net_profit_yoy_fy",
        "deducted_profit_yoy", "deducted_profit_yoy_fy",
    )

    def update_technical_snapshot_fields(self, records: list[dict[str, Any]]) -> None:
        """只更新快照中的技术三列（`scripts/backfill_technical_indicators.py`）。"""
        self._update_snapshot_field_subset(records, self._TECHNICAL_SNAPSHOT_FIELDS)

    def update_yoy_snapshot_fields(self, records: list[dict[str, Any]]) -> None:
        """只更新快照中的同比六列（`scripts/backfill_yoy_calibers.py`）。"""
        self._update_snapshot_field_subset(records, self._YOY_SNAPSHOT_FIELDS)

    def _update_snapshot_field_subset(
        self,
        records: list[dict[str, Any]],
        fields: Sequence[str],
    ) -> None:
        """按 (stock_code, report_date) 原地更新指定列，并校验行数不变。

        与 `_update_snapshot_price_fields` 同一套原子性/行数校验纪律：matched
        必须恰好 1 行（DuckDB cursor.rowcount 对 UPDATE 返回 -1，不能用）。
        """
        if not records:
            return
        assignments = ", ".join(f"{field} = ?" for field in fields)
        with self.duck.transaction() as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            for row in records:
                values = [row.get(field) for field in fields]
                connection.execute(
                    f"""UPDATE indicator_snapshot
                        SET {assignments}, calculated_at = ?
                        WHERE stock_code = ? AND report_date = ?""",
                    [*values, row["calculated_at"], row["stock_code"], row["report_date"]],
                )
                matched = connection.execute(
                    """SELECT COUNT(*) AS c FROM indicator_snapshot
                       WHERE stock_code = ? AND report_date = ?""",
                    [row["stock_code"], row["report_date"]],
                ).fetchone()[0]
                if matched != 1:
                    raise RuntimeError(
                        f"snapshot field-subset update matched {matched} rows "
                        f"for {row['stock_code']}"
                    )
            after = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            if after != before:
                raise RuntimeError(
                    f"snapshot field-subset update changed row count: {before} -> {after}"
                )
            self._record_derived_lineage_in_connection(connection, records)

    def _update_snapshot_price_fields(self, records: list[dict[str, Any]]) -> None:
        """价格域 UPDATE：既有行原地更新，不动基本面字段与报告期。"""
        if not records:
            return
        assignments = ", ".join(f"{field} = ?" for field in self._PRICE_SENSITIVE_FIELDS)
        with self.duck.transaction() as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            for row in records:
                values = [row.get(field) for field in self._PRICE_SENSITIVE_FIELDS]
                connection.execute(
                    f"""UPDATE indicator_snapshot
                        SET {assignments}, calculated_at = ?
                        WHERE stock_code = ? AND report_date = ?""",
                    [*values, row["calculated_at"], row["stock_code"], row["report_date"]],
                )
                # DuckDB cursor.rowcount 对 UPDATE 返回 -1，必须用 SELECT 校验。
                matched = connection.execute(
                    """SELECT COUNT(*) AS c FROM indicator_snapshot
                       WHERE stock_code = ? AND report_date = ?""",
                    [row["stock_code"], row["report_date"]],
                ).fetchone()[0]
                if matched != 1:
                    raise RuntimeError(
                        f"price-field snapshot update matched {matched} rows "
                        f"for {row['stock_code']}"
                    )
            after = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            if after != before:
                raise RuntimeError(
                    f"price-field snapshot update changed row count: {before} -> {after}"
                )
            self._record_derived_lineage_in_connection(connection, records)

    def _update_snapshot_funding_dividend_fields(self, records: list[dict[str, Any]]) -> None:
        """只更新分红融资比三列（回购/融资事件变化专用）。"""
        if not records:
            return
        with self.duck.transaction() as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            for row in records:
                connection.execute(
                    """UPDATE indicator_snapshot
                       SET cumulative_dividend_amount = ?,
                           cumulative_financing_amount = ?,
                           dividend_financing_ratio_pct = ?,
                           calculated_at = ?
                       WHERE stock_code = ? AND report_date = ?""",
                    [
                        row["cumulative_dividend_amount"],
                        row["cumulative_financing_amount"],
                        row["dividend_financing_ratio_pct"],
                        row["calculated_at"],
                        row["stock_code"],
                        row["report_date"],
                    ],
                )
                # DuckDB cursor.rowcount 对 UPDATE 返回 -1，必须用 SELECT 校验。
                matched = connection.execute(
                    """SELECT COUNT(*) AS c FROM indicator_snapshot
                       WHERE stock_code = ? AND report_date = ?""",
                    [row["stock_code"], row["report_date"]],
                ).fetchone()[0]
                if matched != 1:
                    raise RuntimeError(
                        f"funding-dividend snapshot update matched {matched} rows "
                        f"for {row['stock_code']}"
                    )
            after = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            if after != before:
                raise RuntimeError(
                    f"funding-dividend snapshot update changed row count: {before} -> {after}"
                )
            self._record_derived_lineage_in_connection(connection, records)

    def _read_query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        connection = getattr(self, "_calculation_read_connection", None)
        if connection is None:
            return self.duck.read_query(sql, params)
        cursor = connection.execute(sql, params or [])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """只读查询（market_cap.resolve_shares_asof 等共享域按 duck 接口调用）。

        与 _read_query 同一条路径：批量重算期间复用同一只读连接，不额外开连接。
        """
        return self._read_query(sql, params)

    # ─── 数据获取 ──────────────────────────────────────────────────

    def _get_latest_financials(self, stock_code: str) -> dict[str, Any]:
        """获取最新报告期的三大报表数据（合并为一行）

        M8-问题1修复: 应用人工覆写值 (PRD §9.5 R7: 覆写与原始值分离, 但计算时使用覆写值)
        """
        rows = self._read_query("""
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
              -- P0-4/5修复: 只取"最新完整期"——三表核心字段齐备的报告期。
              -- 晚于完整期但缺核心字段的行是数据源未就绪（PRD §7.7），
              -- 不得混入快照计算，避免部分新财务+旧快照口径不一致。
              AND cf.cf_from_operating IS NOT NULL
            ORDER BY bs.report_date DESC
            LIMIT 1
        """, [stock_code])

        if not rows:
            return {}

        financials = rows[0]

        return self._apply_published_overrides(stock_code, [financials])[0]

    def _get_latest_price(self, stock_code: str) -> dict[str, Any]:
        """获取最新收盘价"""
        rows = self._read_query("""
            SELECT trade_date, close, volume, turnover
            FROM price_daily_raw
            WHERE stock_code = ? AND close IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT 1
        """, [stock_code])

        return rows[0] if rows else {}

    def _get_shares(self, stock_code: str, financials: dict) -> dict[str, Any]:
        """获取总股本和流通股本

        从 stock_meta 表获取真实的总股本和流通股本（单位：股）。
        """
        rows = self._read_query("""
            SELECT total_shares, circ_shares
            FROM stock_meta
            WHERE stock_code = ?
        """, [stock_code])

        if rows:
            total_shares = rows[0].get("total_shares")
            circ_shares = rows[0].get("circ_shares")
            return {
                "total_shares": total_shares,
                "circ_shares": circ_shares,
            }
        return {"total_shares": None, "circ_shares": None}

    def _get_ttm_data(self, stock_code: str, as_of_date: Any | None = None) -> dict[str, Any]:
        """计算TTM（滚动十二个月）数据

        标准 TTM = 最近4个季度的单季度值之和
        实现：TTM = 最新累计值 - 去年同期累计值（如果去年同期有数据）
              或 TTM = 最新年报值（如果最新报告期是年报）

        PRD §8.1: 默认口径为 latest_restated。
        PRD §9.1: 允许 approximate 值，需标记 confidence。
        """
        # 获取最近8个季度的累计值（足够计算TTM和YoY）
        rows = self._read_query("""
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
              AND (? IS NULL OR ic.report_date <= CAST(? AS DATE))
            ORDER BY ic.report_date DESC
            LIMIT 8
        """, [stock_code, str(as_of_date) if as_of_date else None, str(as_of_date) if as_of_date else None])

        if not rows:
            return {}

        rows = self._apply_published_overrides(stock_code, rows)

        latest = rows[0]
        latest_date = str(latest.get("report_date", ""))

        # 情况1: 最新报告期是年报（12月31日），TTM = 年报值
        if "12-31" in latest_date:
            return latest

        # 情况2: 最新报告期是季报，用累计值差分
        # 正确TTM = 最新年报 + 最新累计 - 去年同期累计
        # 例: Q1 2025 TTM = 2024年报 + Q1_2025累计 - Q1_2024累计
        if len(rows) >= 2:
            current_cumulative = rows[0]
            current_date = str(current_cumulative.get("report_date", ""))[:10]
            try:
                year_ago_date = f"{int(current_date[:4]) - 1}{current_date[4:]}"
            except (TypeError, ValueError):
                return {"_ttm_confidence": "missing", "_ttm_reason": "invalid_report_date"}
            year_ago_cumulative = next(
                (row for row in rows if str(row.get("report_date", ""))[:10] == year_ago_date),
                None,
            )

            # TTM requires exactly the previous fiscal year-end, never an older annual report.
            annual_date = f"{int(current_date[:4]) - 1}-12-31"
            annual = next(
                (row for row in rows if str(row.get("report_date", ""))[:10] == annual_date),
                None,
            )

            ttm: dict[str, Any] = {"report_date": current_cumulative["report_date"]}
            for key in ["revenue", "cost_of_revenue", "gross_profit",
                        "operating_profit", "net_profit", "parent_net_profit",
                        "deducted_net_profit", "total_operating_revenue",
                        "total_operating_cost", "selling_expenses",
                        "administrative_expenses", "financial_expenses",
                        "interest_expense", "investment_income", "total_profit",
                        "income_tax", "cf_from_operating"]:
                curr = current_cumulative.get(key)
                prev = year_ago_cumulative.get(key) if year_ago_cumulative else None
                if annual and curr is not None and prev is not None:
                    ann_val = annual.get(key)
                    if ann_val is not None:
                        ttm[key] = ann_val + curr - prev
                    else:
                        ttm[key] = None
                elif year_ago_cumulative is None:
                    ttm[key] = None
                    ttm["_ttm_confidence"] = "missing"
                    ttm["_ttm_reason"] = "prior_year_same_period_missing"
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

    def _get_dividend_summary(self, stock_code: str, as_of_date: Any | None = None) -> dict[str, Any]:
        """Return only dividends known on the snapshot's reporting date."""
        as_of_date = as_of_date or cn_today()
        rows = self._read_query("""
            WITH valid_dividends AS (
                SELECT ex_date, dividend_per_share
                FROM dividends
                WHERE stock_code = ?
                  AND dividend_per_share IS NOT NULL
                  AND dividend_per_share > 0
                  AND announcement_date IS NOT NULL
                  AND ex_date <= CAST(? AS DATE)
                  AND announcement_date <= CAST(? AS DATE)
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
        """, [stock_code, str(as_of_date), str(as_of_date)])

        return rows[0] if rows else {}

    def _get_cumulative_dividend_amount(self, stock_code: str) -> float | None:
        """A股广义分红金额（元）：A股现金分红 + 回购注销金额。

        口径（2026-09-03 修正）：
        - 现金分红：每股股息 × ex_date 当日生效的 A股股本逐笔折算。
          当前 schema 没有逐日 A股总股本，因此优先使用 stock_meta.circ_shares
          （A股流通股本）作为 A股股本近似，并明确按流通口径披露；
          任一笔有效分红无任何可用股本 → fail-closed 返回 None。
        - 港股分红未采集：绝不把 total_shares 中可能包含的 H 股股本计入，
          避免中国移动等 A+H 公司把分给 H 股股东的钱算成 A股派现。
        - 回购注销：东财回购明细已回购金额（buyback_events.buyback_amount），
          用于补足“广义分红（包括回购注销）”。
        - 无有效记录 → 0.0。
        """
        # 2026-09-08 提速：旧实现每股每笔分红用两个相关子查询回查
        # share_capital_history，600519 单次约 1s。窗口函数一次定位
        # ex_date 时点生效股本，600519 降至约 0.04s，结果逐行一致。
        rows = self._read_query(
            """WITH valid_dividends AS (
                   SELECT d.stock_code, d.ex_date, d.dividend_per_share
                   FROM dividends d
                   WHERE d.stock_code = ?
                     AND d.dividend_per_share IS NOT NULL
                     AND d.dividend_per_share > 0
                     AND d.announcement_date IS NOT NULL
               ), capital_windows AS (
                   SELECT stock_code, effective_date, total_shares,
                          LEAD(effective_date, 1, DATE '9999-12-31') OVER (
                              PARTITION BY stock_code
                              ORDER BY effective_date, total_shares
                          ) AS next_date
                   FROM share_capital_history
               ), joined AS (
                   SELECT v.ex_date, v.dividend_per_share,
                          COALESCE(c.total_shares, m.circ_shares, m.total_shares) AS shares
                   FROM valid_dividends v
                   LEFT JOIN stock_meta m ON m.stock_code = v.stock_code
                   LEFT JOIN capital_windows c
                     ON c.stock_code = v.stock_code
                    AND c.effective_date <= v.ex_date
                    AND v.ex_date < c.next_date
               )
               SELECT COUNT(*) AS n,
                      SUM(CASE WHEN shares IS NULL THEN 1 ELSE 0 END) AS missing_shares,
                      SUM(dividend_per_share * shares) AS total_amount
               FROM joined
            """,
            [stock_code],
        )
        if not rows or not rows[0].get("n"):
            dividend_total = 0.0
        else:
            # 若历史股本链缺失，退而使用当前总股本作为近似（2026-08-26
            # 为了覆盖北交所/无股本链股票；二者都缺失才 fail-closed）。
            if rows[0].get("missing_shares"):
                return None
            dividend_total = float(rows[0]["total_amount"]) if rows[0].get("total_amount") is not None else None
            if dividend_total is None:
                return None

        buyback_rows = self._read_query(
            """SELECT SUM(buyback_amount) AS total
               FROM buyback_events
               WHERE stock_code = ?
                 AND buyback_amount IS NOT NULL""",
            [stock_code],
        )
        buyback_total = float(buyback_rows[0]["total"]) if buyback_rows and buyback_rows[0].get("total") is not None else 0.0
        return dividend_total + buyback_total

    def _get_cumulative_financing_amount(self, stock_code: str) -> float | None:
        """历史累计股权融资金额（元）：IPO + 增发 + 配股。

        口径（2026-08-25）：
        - 优先东财募资总额 raise_funds，缺失时采用 CNINFO IPO 募资净额
          raise_funds_net（报告 82 §4.2 的诚实披露口径）；
        - 单条事件两个金额都缺失 → fail-closed 返回 None，绝不低估；
        - 该股尚无融资事件记录（未采集/无源） → None，等融资域
          有界续传覆盖后再产数；已覆盖且全部事件金额齐备才求和。
        """
        rows = self._read_query(
            """SELECT
                   COUNT(*) AS n,
                   SUM(CASE
                       WHEN COALESCE(raise_funds, raise_funds_net, issue_price * issue_shares) IS NULL THEN 1
                       ELSE 0 END) AS missing_amounts,
                   SUM(COALESCE(raise_funds, raise_funds_net, issue_price * issue_shares)) AS total_amount
               FROM funding_events
               WHERE stock_code = ?
                 AND event_type IN ('ipo', 'a_placement', 'rights')
            """,
            [stock_code],
        )
        if not rows or not rows[0].get("n"):
            return None
        if rows[0].get("missing_amounts"):
            return None
        return float(rows[0]["total_amount"]) if rows[0].get("total_amount") is not None else None

    # ─── 指标计算 ──────────────────────────────────────────────────

    def _calc_valuation(
        self, stock_code: str, price: dict, total_shares: float | None, circ_shares: float | None,
        ttm: dict, financials: dict, dividends: dict
    ) -> dict[str, Any]:
        """1. 估值指标

        2026-11-16 市值口径收敛：总市值不再用「当前股本 × 历史价格」，统一走
        app/core/market_cap.py——优先取 latest_price_date 当日的
        share_capital_history 股本，历史缺行才回退 stock_meta（即调用方传入
        的 total_shares）。历史表无数据时结果与旧实现 `close ×
        stock_meta.total_shares` 逐位一致（tests/regression/
        test_market_cap_single_source.py 钉死）。
        PE/PB/PS/PCF 的口径是「总市值 / X」（见 app/web/api/stock_detail.py
        公式表），因此共用同一份已解析股本，避免同一行里市值与估值倍数各用
        一个股本。
        """
        result: dict[str, Any] = {}
        close = price.get("close")
        cap = latest_market_cap(
            self, stock_code, close=close, price_date=price.get("trade_date"),
        )
        # 历史与元数据都取不到股本时，沿用调用方传入的总股本（与旧行为一致）
        if cap["shares"] is None and total_shares:
            cap["total_market_cap"] = market_cap_from(close, total_shares)
        market_cap = cap["total_market_cap"]
        has_cap = market_cap is not None and market_cap > 0

        # 总市值 = 最新收盘价 × 总股本（唯一实现，见 market_cap 模块）
        result["total_market_cap"] = market_cap

        # 流通市值 = 最新收盘价 × 流通股本
        result["circ_market_cap"] = market_cap_from(close, circ_shares)

        # PE-TTM = 总市值 / TTM归母净利润
        # V2-3.4修复: PE>1000 时置NULL（净利润接近0导致PE虚高，业务无意义）
        ttm_profit = ttm.get("parent_net_profit")
        if has_cap and ttm_profit and ttm_profit > 0:
            pe = market_cap / ttm_profit
            result["pe_ttm"] = pe if pe <= 1000 else None
        else:
            result["pe_ttm"] = None

        # PB-MRQ = 总市值 / 最新报告期归母权益
        # P0#9修复: PB>200 时置NULL (权益接近0导致PB虚高，业务无意义)
        equity = financials.get("total_equity_parent") if financials.get("total_equity_parent") is not None else financials.get("total_equity")
        if has_cap and equity is not None and equity > 0:
            pb = market_cap / equity
            result["pb_mrq"] = pb if pb <= 200 else None
        else:
            result["pb_mrq"] = None

        # PS-TTM = 总市值 / TTM营业收入
        ttm_revenue = ttm.get("revenue") or ttm.get("total_operating_revenue")
        if has_cap and ttm_revenue and ttm_revenue > 0:
            result["ps_ttm"] = market_cap / ttm_revenue
        else:
            result["ps_ttm"] = None

        # PCF-TTM = 总市值 / TTM经营现金流净额
        ttm_cf = ttm.get("cf_from_operating")
        if has_cap and ttm_cf and ttm_cf > 0:
            result["pcf_ttm"] = market_cap / ttm_cf
        else:
            result["pcf_ttm"] = None

        # 股息率 = 最近12个月每股股息 / 最新收盘价
        if close and close > 0:
            result["dividend_yield"] = self._calc_dividend_yield(
                stock_code, close, price.get("trade_date")
            )
        else:
            result["dividend_yield"] = None

        return result

    def _calc_dividend_yield(
        self, stock_code: str, close: float, as_of_date: Any | None = None,
    ) -> float | None:
        """计算最近12个月股息率

        Only distributions in the trailing twelve months are relevant. Older
        data is a missing-data condition, not a reason to inflate the yield.
        """
        as_of_date = as_of_date or cn_today()
        rows = self._read_query("""
            SELECT SUM(dividend_per_share) as dps
            FROM dividends
            WHERE stock_code = ?
               AND dividend_per_share IS NOT NULL
               AND dividend_per_share > 0
               AND announcement_date IS NOT NULL
               AND announcement_date <= CAST(? AS DATE)
               AND ex_date <= CAST(? AS DATE)
               AND ex_date >= CAST(? AS DATE) - INTERVAL '1 year'
        """, [stock_code, str(as_of_date), str(as_of_date), str(as_of_date)])

        if rows and rows[0].get("dps"):
            dps = rows[0]["dps"]
            return dps / close if close > 0 else None
        return None

    def _calc_treasury_spread(self, stock_code: str) -> dict[str, Any]:
        """TTM 已实施现金股息率与相对各关键期限国债收益率的利差（P3）。

        口径（reports/68 §5）：
        - TTM 股息率：价格日前 12 个月内已除权（ex_date<=D）且已公告
          （announcement_date<=D）的实际现金分红合计 / D 收盘价。
        - 曲线对齐：取不晚于 D 的最近曲线点，最大陈旧 5 个自然日；
          超限或曲线缺失时该期限利差为 NULL（不得用更旧值替代）。
        """
        result: dict[str, Any] = {
            "ttm_dividend_yield": None,
        }
        for tenor in KEY_TENORS:
            result[CZB_CURVE_YIELD_TENOR_LABELS[tenor]] = None

        price_rows = self._read_query(
            """SELECT trade_date, close FROM price_daily_raw
               WHERE stock_code = ? AND close IS NOT NULL
               ORDER BY trade_date DESC LIMIT 1""",
            [stock_code],
        )
        if not price_rows or not price_rows[0].get("close"):
            return result
        price_date = str(price_rows[0]["trade_date"])[:10]
        close = float(price_rows[0]["close"])

        div_rows = self._read_query(
            """SELECT SUM(dividend_per_share) AS dps
               FROM dividends
               WHERE stock_code = ?
                 AND dividend_per_share IS NOT NULL
                 AND dividend_per_share > 0
                 AND announcement_date IS NOT NULL
                 AND announcement_date <= CAST(? AS DATE)
                 AND ex_date <= CAST(? AS DATE)
                 AND ex_date >= CAST(? AS DATE) - INTERVAL '1 year'""",
            [stock_code, price_date, price_date, price_date],
        )
        ttm_dps = div_rows[0].get("dps") if div_rows else None
        ttm_div_yield = (float(ttm_dps) / close) * 100.0 if ttm_dps and close > 0 else None
        result["ttm_dividend_yield"] = ttm_div_yield

        # 曲线对齐：批量按期限查询 ≤ 价格日最近点
        curve_rows = self._read_query(
            """SELECT tenor_years, curve_date, yield_pct
               FROM treasury_yield_curve
               WHERE curve_date <= CAST(? AS DATE)
                 AND tenor_years IN ({tenors})
                 AND curve_date IN (
                     SELECT MAX(curve_date) FROM treasury_yield_curve
                     WHERE curve_date <= CAST(? AS DATE)
                     GROUP BY tenor_years
                 )
            """.format(tenors=", ".join("?" for _ in KEY_TENORS)),
            [price_date, *list(KEY_TENORS), price_date],
        )
        aligned: dict[float, dict[str, Any]] = {}
        for row in curve_rows:
            aligned[float(row["tenor_years"])] = row

        for tenor in KEY_TENORS:
            column = CZB_CURVE_YIELD_TENOR_LABELS[tenor]
            curve = aligned.get(tenor)
            if curve is None:
                result[column] = None
                continue
            curve_date = str(curve["curve_date"])[:10]
            staleness = (
                date.fromisoformat(price_date) - date.fromisoformat(curve_date)
            ).days
            if staleness > MAX_STALENESS_DAYS or curve.get("yield_pct") is None:
                result[column] = None
                continue
            if ttm_div_yield is None:
                result[column] = None
                continue
            result[column] = ttm_div_yield - float(curve["yield_pct"])
        return result

    def _calc_profitability(self, ttm: dict, financials: dict, stock_code: str = "") -> dict[str, Any]:
        """2. 盈利能力指标"""
        result: dict[str, Any] = {}

        revenue = ttm.get("revenue") or ttm.get("total_operating_revenue")
        net_profit = ttm.get("net_profit")
        parent_profit = ttm.get("parent_net_profit")
        gross_profit = ttm.get("gross_profit")
        op_cf = ttm.get("cf_from_operating")

        # P1-24修复: ROE/ROA使用平均值(期初+期末)/2而非期末值
        # 获取上一期权益和总资产
        prev_equity = None
        prev_assets = None
        if stock_code:
            report_date = financials.get("report_date")
            if report_date:
                prior_year_same_period = (
                    f"{int(str(report_date)[:4]) - 1}{str(report_date)[4:]}"
                    if len(str(report_date)) >= 10 else None
                )
                prev_rows = self._read_query(
                    """SELECT total_equity_parent, total_equity, total_assets
                       FROM balance_sheet
                       WHERE stock_code = ? AND report_date = CAST(? AS DATE)""",
                    [stock_code, prior_year_same_period],
                )
                if prev_rows:
                    prior = self._apply_published_overrides(stock_code, prev_rows)[0]
                    prev_equity_parent = prior.get("total_equity_parent")
                    prev_equity = prev_equity_parent if prev_equity_parent is not None else prior.get("total_equity")
                    prev_assets = prior.get("total_assets")

        # ROE = TTM归母净利润 / 平均归母权益
        # V2-3.3修复: |ROE|>1.0 时置NULL
        # P1-24修复: 用(期初+期末)/2而非期末值
        # 2026-11-15（reports/122 §2.6 J5）结论：上一期权益缺失时 ROE 保持 NULL，不把期初按 0
        # 处理（否则"平均权益"退化成期末的一半，ROE 会被系统性放大）。
        equity = financials.get("total_equity_parent") if financials.get("total_equity_parent") is not None else financials.get("total_equity")
        avg_equity = None
        if prev_equity is not None and equity is not None:
            avg_equity = (prev_equity + equity) / 2
        if parent_profit is not None and avg_equity is not None and avg_equity > 0:
            roe = parent_profit / avg_equity
            result["roe"] = roe if abs(roe) <= 1.0 else None
        else:
            result["roe"] = None

        # ROA = TTM净利润 / 平均总资产
        # P1-24修复: 用(期初+期末)/2而非期末值
        # 2026-11-15（reports/122 §2.6 J5）结论：同 ROE——上一期总资产缺失时保持 NULL。
        total_assets = financials.get("total_assets")
        avg_assets = None
        if prev_assets is not None and total_assets is not None:
            avg_assets = (prev_assets + total_assets) / 2
        if net_profit is not None and avg_assets and avg_assets > 0:
            result["roa"] = net_profit / avg_assets
        else:
            result["roa"] = None

        # 毛利率 = (营收 - 营业成本) / 营收
        # P0#9修复: 毛利率范围限制在 [-1, 1], 超出置 NULL
        cost = ttm.get("cost_of_revenue")
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
        # 2026-11-15（reports/122 §2.6 J5）结论：这里保持 NULL，不把缺项按 0 处理。
        # - 营业利润缺失 → 分子无法计算；
        # - 归母权益/总权益缺失 → 不能拿 0 代替（会算出无穷大 ROIC）；
        # - `_calc_interest_bearing_debt` 三项全空时返回 None（fail-closed），
        #   因为无法区分"没有借款"与"负债明细没采到"；只有任一借款科目有值
        #   时才按"缺项=0"求和（该口径已在 2026-11-14 修复并钉死）。
        op_profit = ttm.get("operating_profit")
        interest_debt = self._calc_interest_bearing_debt(financials)
        invested_capital = None
        if equity and interest_debt is not None:
            invested_capital = equity + interest_debt
        if op_profit is not None and invested_capital and invested_capital > 0:
            result["roic"] = op_profit / invested_capital
        else:
            result["roic"] = None

        # 经营现金流 / 净利润
        # 2026-11-15（reports/122 §2.6 J5）结论：分母必须是正净利润。净利润 <= 0 时该比率没有
        # 经济含义（负分母会让"现金流好"看起来像负值），不能按 0 或绝对值兜底。
        if op_cf is not None and net_profit and net_profit > 0:
            result["cf_to_net_profit"] = op_cf / net_profit
        else:
            result["cf_to_net_profit"] = None

        return result

    # 同比双口径（2026-11-16，reports/131 §9.2 P0-4，schema v43）。
    # key = 快照列名；value = TTM 序列里的净利润字段名。
    _TTM_YOY_METRICS: tuple[tuple[str, str], ...] = (
        ("revenue_yoy", "revenue"),
        ("net_profit_yoy", "net_profit"),
        ("deducted_profit_yoy", "deducted_net_profit"),
    )

    def _calc_growth(self, stock_code: str) -> dict[str, Any]:
        """3. 成长指标（同比双口径）

        口径纪律（reports/131 §9.2 P0-4，**不允许**在别处再写一套）：

        - ``*_yoy``    = **TTM 口径**：最新报告期 TTM ÷ 上年最后一个报告期 TTM − 1。
          必须与详情页 ``stock_detail._metric_history_rows(..., base_period='ttm')``
          逐值同源（两侧公式一致；回归用例
          ``test_yoy_caliber_and_technical_coverage.py`` 直接对比两个来源）。
        - ``*_yoy_fy`` = **年报口径**：最近两个完整年报的同比（旧 ``_calc_growth``
          行为原样保留到新列）。快照 ``report_date`` 是季报期时，这两个口径本来
          就应当不同，绝不可互相回填。

        CAGR 仍按年报序列计算（口径未变）。
        """
        result: dict[str, Any] = {
            "revenue_yoy": None, "revenue_yoy_fy": None,
            "net_profit_yoy": None, "net_profit_yoy_fy": None,
            "deducted_profit_yoy": None, "deducted_profit_yoy_fy": None,
            "revenue_cagr3": None, "revenue_cagr5": None,
            "net_profit_cagr3": None, "net_profit_cagr5": None,
            "deducted_profit_cagr3": None, "deducted_profit_cagr5": None,
        }
        result.update(self._calc_ttm_yoy(stock_code))

        # 年报口径：获取历史年度数据用于 FY YoY 和 CAGR
        rows = self._read_query("""
            SELECT report_date, revenue, parent_net_profit, deducted_net_profit
            FROM income_statement
            WHERE stock_code = ?
               AND EXTRACT(MONTH FROM report_date) = 12
               AND EXTRACT(DAY FROM report_date) = 31
            ORDER BY report_date DESC
            LIMIT 7
        """, [stock_code])

        if not rows:
            return result

        rows = self._apply_published_overrides(stock_code, rows)

        years = {int(str(row["report_date"])[:4]): row for row in rows}
        current_year = max(years)
        current = years[current_year]

        # FY YoY requires an exact prior annual report.
        if current_year - 1 in years:
            curr, prev = current, years[current_year - 1]
            result["revenue_yoy_fy"] = self._yoy(
                curr.get("revenue"), prev.get("revenue"))
            result["net_profit_yoy_fy"] = self._yoy(
                curr.get("parent_net_profit"), prev.get("parent_net_profit"))
            result["deducted_profit_yoy_fy"] = self._yoy(
                curr.get("deducted_net_profit"), prev.get("deducted_net_profit"))

        # CAGR = (末值/初值)^(1/n) - 1
        def cagr(field: str, years_back: int) -> float | None:
            required = set(range(current_year - years_back, current_year + 1))
            if not required.issubset(years):
                return None
            return self._cagr([years[year].get(field) for year in range(current_year, current_year - years_back - 1, -1)], years_back)

        result["revenue_cagr3"] = cagr("revenue", 3)
        result["revenue_cagr5"] = cagr("revenue", 5)
        result["net_profit_cagr3"] = cagr("parent_net_profit", 3)
        result["net_profit_cagr5"] = cagr("parent_net_profit", 5)
        result["deducted_profit_cagr3"] = cagr("deducted_net_profit", 3)
        result["deducted_profit_cagr5"] = cagr("deducted_net_profit", 5)

        return result

    def _calc_ttm_yoy(self, stock_code: str) -> dict[str, Any]:
        """TTM 同比（与详情页 metric-history 的 'ttm' 分支同源）。

        复刻 ``app/web/api/stock_detail.py`` 的两步口径：

        1. ``calculate_ttm_trend``：年报期直接取年报值；季报期
           ``TTM = 上年年报 + 本期累计 − 去年同期累计``，缺任一项则该期不出行；
        2. ``_metric_history_yoy``：每期与**上一年最后一个 TTM 行**相除 − 1，
           取最后一个非空值为"当前值"。

        为什么在 core 里复刻而不是 import：``app/core`` 不得反向依赖
        ``app/web``（tests/regression/test_core_web_layering.py 钉死）。
        两边公式必须同步修改；一致性由
        ``test_yoy_caliber_and_technical_coverage.py`` 直接对比两个来源。
        """
        result: dict[str, Any] = {
            "revenue_yoy": None,
            "net_profit_yoy": None,
            "deducted_profit_yoy": None,
        }
        # 16 个季度覆盖"最新期 + 上年最后一期 + 再上一年"的构造需求；
        # 快照只关心当前一期，更早的 TTM 行不参与当前值判定。
        # 行集必须从 `balance_sheet` 出发（与 stock_detail._metric_history_rows
        # 的 FROM 子句一致）：只有利润表、没有资产负债表的新期不能提前进入 TTM，
        # 否则快照会用"下一期"信息而详情页不会 —— 一致性用例会立刻抓到。
        rows = self._read_query("""
            SELECT bs.report_date,
                   ic.revenue, ic.parent_net_profit, ic.net_profit,
                   ic.deducted_net_profit
            FROM balance_sheet bs
            LEFT JOIN income_statement ic
              ON bs.stock_code = ic.stock_code
             AND bs.report_date = ic.report_date
            WHERE bs.stock_code = ?
            ORDER BY bs.report_date DESC
            LIMIT 16
        """, [stock_code])
        if not rows:
            # 合成夹具/极端残缺库可能只有利润表。生产路径不会走到这里
            # （`_get_latest_financials` 要求三表核心字段齐备），但保留
            # income-only 退化可让单测与历史数据修复脚本仍能算 TTM。
            rows = self._read_query("""
                SELECT report_date, revenue, parent_net_profit, net_profit,
                       deducted_net_profit
                FROM income_statement
                WHERE stock_code = ?
                ORDER BY report_date DESC
                LIMIT 16
            """, [stock_code])
        if not rows:
            return result
        rows = self._apply_published_overrides(stock_code, rows)
        rows.reverse()

        flow_fields = (
            "revenue", "parent_net_profit", "net_profit", "deducted_net_profit",
        )
        # 关键：上年年报/去年同期取的是**原始累计报表行**，不是"上年 TTM 行"。
        # 与 stock_detail.calculate_ttm_trend 的 dated_rows.get(...) 完全一致。
        raw_by_date: dict[date, dict[str, Any]] = {}
        for row in rows:
            try:
                report_date = date.fromisoformat(str(row.get("report_date"))[:10])
            except (TypeError, ValueError):
                continue
            raw_by_date[report_date] = row
        ttm_by_date: dict[date, dict[str, Any]] = {}
        for report_date in sorted(raw_by_date):
            row = raw_by_date[report_date]
            if report_date.month == 12 and report_date.day == 31:
                item = {field: row.get(field) for field in flow_fields}
            else:
                annual = raw_by_date.get(date(report_date.year - 1, 12, 31))
                prior_period = raw_by_date.get(
                    date(report_date.year - 1, report_date.month, report_date.day)
                )
                if annual is None or prior_period is None:
                    continue
                item = {}
                for field in flow_fields:
                    annual_value = annual.get(field)
                    current_value = row.get(field)
                    prior_value = prior_period.get(field)
                    item[field] = (
                        annual_value + current_value - prior_value
                        if annual_value is not None
                        and current_value is not None
                        and prior_value is not None
                        else None
                    )
            # 与详情页一致：net_profit 优先取归母净利润。
            item["net_profit"] = (
                item.get("parent_net_profit")
                if item.get("parent_net_profit") is not None
                else item.get("net_profit")
            )
            ttm_by_date[report_date] = item

        ordered = sorted(ttm_by_date)
        # 上一年"最后一个 TTM 行"——与 _metric_history_yoy 的 by_year 覆盖语义一致。
        by_year = {report_date.year: ttm_by_date[report_date] for report_date in ordered}
        for column, field in self._TTM_YOY_METRICS:
            latest: float | None = None
            for report_date in ordered:
                prior = by_year.get(report_date.year - 1)
                if prior is None:
                    continue
                current_value = ttm_by_date[report_date].get(field)
                base_value = prior.get(field)
                if current_value is None or base_value in (None, 0):
                    continue
                latest = current_value / base_value - 1.0
            result[column] = latest
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
        # 2026-11-15（reports/122 §2.6 J5）结论：流动资产/流动负债缺失时保持 NULL，不按 0 兜底：
        # 缺资产按 0 会把比率压成 0，缺负债则分母为 0；都不能用总资产/总负债替代。
        if total_current_assets is not None and total_current_liabilities and total_current_liabilities > 0:
            result["current_ratio"] = total_current_assets / total_current_liabilities
        else:
            result["current_ratio"] = None

        # 速动比率 = (流动资产 - 存货) / 流动负债
        # 2026-11-15（reports/122 §2.6 J5）修复：存货为 NULL 时按 0 计。
        # 存货是"没有就是 0"的典型科目——银行/保险/互联网/服务业本来就没有
        # 存货，源里留 NULL 不代表缺失。旧写法要求三项全非空，导致这 53 只
        # （2026-09-30 实测）速动比率被整项压成 NULL（覆盖率 97.2% → 98.2%）。
        # 注意：流动资产/流动负债仍必须非空，不能用 0 或其它科目替代。
        inventory_for_quick = inventory if inventory is not None else 0.0
        if (total_current_assets is not None
                and total_current_liabilities and total_current_liabilities > 0):
            result["quick_ratio"] = (
                total_current_assets - inventory_for_quick
            ) / total_current_liabilities
        else:
            result["quick_ratio"] = None

        # 有息负债 = 短期借款 + 长期借款 + 应付债券
        result["interest_bearing_debt"] = self._calc_interest_bearing_debt(financials)

        # 利息保障倍数 = TTM营业利润 / TTM利息费用
        # 2026-11-15（reports/122 §2.6 J5）结论：保持 NULL。利息费用缺失或为 0 时倍数在数学上是
        # 无穷大/无定义，不能把利息费用按 0 处理；也不能用 financial_expenses
        # 兜底（其中混有汇兑损益、手续费，口径不同）。
        op_profit = ttm.get("operating_profit")
        interest_expense = ttm.get("interest_expense")
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
        self, stock_code: str, financials: dict, dividends: dict, ttm: dict, total_shares: float | None
    ) -> dict[str, Any]:
        """5. 股东回报指标"""
        result: dict[str, Any] = {}

        # 分红率 = 已验证的最近年度每股股息 / 每股收益
        # 2026-11-15（reports/122 §2.6 J5）结论：保持 NULL。DPS 无记录、股本缺失、TTM EPS <= 0
        # 时都无法定义有意义的分红率；EPS 为负更不能按 0 或绝对值兜底。
        # `latest_dps` 来自"已公告 + 已除权"的年度合计，0 不会作为有效记录返回。
        dps = dividends.get("latest_dps")  # 最新一年DPS
        eps = ttm.get("parent_net_profit")
        shares = total_shares
        if dps and eps and shares and shares > 0:
            ttm_eps = eps / shares
            if ttm_eps and ttm_eps > 0:
                result["payout_ratio"] = dps / ttm_eps
            else:
                result["payout_ratio"] = None
        else:
            result["payout_ratio"] = None

        # 每股股息 (DPS)：直接取已验证年度合计，没有"多项输入"公式；
        # 无记录时如实为 NULL（不是 0 元/股，避免把"没采到"显示成"不分红"）。
        result["dps"] = dps

        # 连续分红年数（从最近年份往前数连续分红的年数）
        result["consecutive_div_years"] = self._calc_consecutive_div_years(
            stock_code, financials.get("report_date")
        )

        return result

    def _calc_technical(self, stock_code: str) -> dict[str, Any]:
        """6. 行情与技术统计指标"""
        result: dict[str, Any] = {}

        # Raw prices remain appropriate for moving averages and volume. Returns,
        # volatility and drawdown must use QFQ closes to remove corporate-action gaps.
        rows = self._read_query("""
            SELECT raw.trade_date, raw.close AS raw_close, raw.volume, raw.turnover,
                   raw.turnover_rate, qfq.close AS qfq_close
            FROM price_daily_raw raw
            LEFT JOIN price_daily_qfq qfq
              ON qfq.stock_code = raw.stock_code AND qfq.trade_date = raw.trade_date
            WHERE raw.stock_code = ?
            ORDER BY raw.trade_date DESC
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
        expected_trading_dates = self._expected_trading_dates(rows)
        if expected_trading_dates is None:
            for key in ["ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
                        "turnover_rate", "avg_volume", "period_return",
                        "annualized_volatility", "max_drawdown"]:
                result[key] = None
            return result
        # Without a persisted calendar, gaps cannot be distinguished from
        # non-trading days. Fail closed instead of inventing a contiguous run.
        closes = self._trailing_contiguous_closes(rows, "raw_close", expected_trading_dates)
        qfq_closes = self._trailing_contiguous_closes(rows, "qfq_close", expected_trading_dates)
        trailing_rows = self._trailing_contiguous_rows(rows, expected_trading_dates)
        volumes = [row["volume"] for row in trailing_rows if row["volume"] is not None]
        turn_rates = [row["turnover_rate"] for row in trailing_rows if row["turnover_rate"] is not None]

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

        # QFQ return/volatility/drawdown use the trailing uninterrupted QFQ run.
        # Never compact values across a missing QFQ observation into a fake series.
        if len(qfq_closes) >= 2:
            result["period_return"] = (
                (qfq_closes[-1] - qfq_closes[0]) / qfq_closes[0]
                if qfq_closes[0] != 0 else None
            )
        else:
            result["period_return"] = None

        # 年化波动率 (最近60日日收益率标准差 × sqrt(250))
        if len(qfq_closes) >= 21:
            returns = []
            recent_qfq = qfq_closes[-60:]
            for i in range(1, len(recent_qfq)):
                returns.append((recent_qfq[i] - recent_qfq[i - 1]) / recent_qfq[i - 1])
            if len(returns) >= 2:
                avg_ret = sum(returns) / len(returns)
                variance = sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1)
                result["annualized_volatility"] = math.sqrt(variance) * math.sqrt(250)
            else:
                result["annualized_volatility"] = None
        else:
            result["annualized_volatility"] = None

        # 最大回撤 (最近250日)
        if len(qfq_closes) >= 2:
            max_price = qfq_closes[0]
            max_drawdown = 0.0
            for price in qfq_closes[1:]:
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

    def _apply_published_overrides(
        self, stock_code: str, rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Overlay published corrections on every financial period used in a calculation."""
        if not rows:
            return rows
        by_date = {str(row.get("report_date", ""))[:10]: row for row in rows}
        try:
            overrides = self.sqlite.query(
                """SELECT field_name, report_date, override_value FROM manual_overrides
                   WHERE stock_code = ? AND status = 'published' AND rolled_back_at IS NULL""",
                [stock_code],
            )
        except Exception as error:
            logger.debug("读取人工覆写失败 (非致命): %s", error)
            return rows
        for override in overrides:
            report_date = override.get("report_date")
            targets = [by_date.get(str(report_date)[:10])] if report_date else list(rows)
            for target in targets:
                if target is not None and override["field_name"] in target:
                    target[override["field_name"]] = override["override_value"]
        return rows

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
        """有息负债 = 短期借款 + 长期借款 + 应付债券。

        2026-11-14 修复（reports/119 P1-5）：旧实现要求三项**全部非空**才算，
        但 `bonds_payable`（应付债券）全市场只有约 815 只非空 —— 绝大多数公司
        本来就没有发债，该科目为空是**正常态**而非缺失。结果 `interest_bearing_debt`
        与依赖它的 `roic` 覆盖率被压到 11.8%，且是"悄悄少算"（不是全空），
        用户会误以为"全市场只有这么几家能算 ROIC"。

        正确口径：缺失科目按 0 计，只要**至少一项有值**即可计算；
        三项全空才返回 None（该股确实没有可识别的有息负债明细）。

        2026-11-15（reports/122 §2.6 J5）复核：维持该口径。三项全空时无法区分"真无借款"与
        "借款明细整段没采到"，保持 NULL 是 fail-closed；若将来确认源以 NULL
        表示零借款，再单独评估是否改成 0.0（需同步 roic 覆盖率与测试）。
        """
        components = [
            value for value in (
                financials.get("short_term_loans"),
                financials.get("long_term_loans"),
                financials.get("bonds_payable"),
            )
            if value is not None
        ]
        if not components:
            return None
        return float(sum(components))

    def _calc_consecutive_div_years(self, stock_code: str, as_of_date: Any | None = None) -> int | None:
        """计算连续分红年数（从最近年份往前数连续分红的年数）

        例如：公司2018/2019/2021/2022年分红 → 返回2（2021-2022连续）
        如果2023也分红 → 返回3（2021-2023连续）
        """
        as_of_date = as_of_date or cn_today()
        rows = self._read_query("""
            SELECT DISTINCT EXTRACT(YEAR FROM ex_date) as yr
            FROM dividends
            WHERE stock_code = ?
              AND dividend_per_share IS NOT NULL
              AND dividend_per_share > 0
              AND announcement_date IS NOT NULL
              AND ex_date <= CAST(? AS DATE)
              AND announcement_date <= CAST(? AS DATE)
            ORDER BY yr DESC
        """, [stock_code, str(as_of_date), str(as_of_date)])

        if not rows:
            return None

        years = sorted([r["yr"] for r in rows], reverse=True)
        current_year = int(str(as_of_date)[:4])

        # 允许最近1年没分红（可能是当年还未到分红日）
        # 从最近的分红年份开始往前数
        if not years:
            return 0

        latest_year = years[0]
        # 如果最新分红年份是近三年内，开始计数
        # (CSMAR学术数据集有滞后，放宽到 current_year-2; 生产环境 vd data update 会补充最新)
        if latest_year < current_year - 2:
            return None  # 最近三年都没分红

        consecutive = 0
        expected_year = latest_year
        for year in years:
            if year == expected_year:
                consecutive += 1
                expected_year -= 1
            else:
                break

        return consecutive

    def _expected_trading_dates(self, rows: list[dict[str, Any]]) -> set[str] | None:
        """Return persisted expected dates for this price window, when available."""
        if len(rows) < 2:
            return None
        start_date = str(rows[0].get("trade_date", ""))[:10]
        end_date = str(rows[-1].get("trade_date", ""))[:10]
        if not start_date or not end_date:
            return None
        try:
            calendar_rows = self.sqlite.query(
                """SELECT trade_date FROM trading_dates
                   WHERE trade_date >= ? AND trade_date <= ?""",
                [start_date, end_date],
            )
        except Exception as error:
            logger.debug("交易日历不可用，跳过日期连续性校验: %s", error)
            return None
        expected_dates = {str(row["trade_date"])[:10] for row in calendar_rows}
        return expected_dates or None

    @staticmethod
    def _trailing_contiguous_closes(
        rows: list[dict[str, Any]], field: str, expected_trading_dates: set[str] | None = None,
    ) -> list[float]:
        """Return the latest valid run without bridging persisted expected trading days."""
        closes: list[float] = []
        newer_date: str | None = None
        for row in reversed(rows):
            value = row.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                break
            trade_date = str(row.get("trade_date", ""))[:10]
            if not trade_date:
                break
            if (
                newer_date is not None
                and expected_trading_dates is not None
                and any(trade_date < date < newer_date for date in expected_trading_dates)
            ):
                break
            closes.append(float(value))
            newer_date = trade_date
        closes.reverse()
        return closes

    @staticmethod
    def _trailing_contiguous_rows(
        rows: list[dict[str, Any]], expected_trading_dates: set[str],
    ) -> list[dict[str, Any]]:
        """Return the latest calendar-contiguous raw window without compacting gaps."""
        trailing: list[dict[str, Any]] = []
        newer_date: str | None = None
        for row in reversed(rows):
            trade_date = str(row.get("trade_date", ""))[:10]
            if not trade_date:
                break
            if newer_date is not None and any(
                trade_date < expected < newer_date for expected in expected_trading_dates
            ):
                break
            trailing.append(row)
            newer_date = trade_date
        trailing.reverse()
        return trailing

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

    def _publish_snapshot_delta(
        self, stock_codes: list[str], records: list[dict[str, Any]],
    ) -> None:
        """Atomically replace snapshots only for the affected stock codes.

        2026-08-28 提速计划：增量重算不再构建"全市场副本 staging"。
        单事务内完成受影响行 DELETE、新行 INSERT、行数与键校验、衍生 lineage，
        任一步失败都整体回滚，上一代已发布快照保持原样。

        2026-11-17（`reports/133` R3）：补上派生域保全/重建/校验。此前本路径
        不跑 ROCE 聚合，增量重算过的股票（=每天价格变动的全体）ROCE 六列会被
        静默置空——整表路径的补丁没覆盖到这里。
        """
        if not records:
            return
        placeholders = ", ".join("?" for _ in stock_codes)
        scope_where = f" WHERE stock_code IN ({placeholders})"
        with self.duck.transaction() as connection:
            previous_total = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            affected_count = connection.execute(
                f"SELECT COUNT(*) FROM indicator_snapshot WHERE stock_code IN ({placeholders})",
                stock_codes,
            ).fetchone()[0]
            missing_keys = sum(
                1 for row in records
                if row.get("stock_code") is None or row.get("report_date") is None
            )
            duplicate_count = 0
            seen: set[tuple[str, str]] = set()
            for row in records:
                key = (str(row.get("stock_code")), str(row.get("report_date")))
                if key in seen:
                    duplicate_count += 1
                seen.add(key)
            if missing_keys or duplicate_count:
                raise RuntimeError(
                    "snapshot delta validation failed: "
                    f"missing_keys={missing_keys}, duplicates={duplicate_count}"
                )
            owned_columns = self._record_owned_column_names(connection, records)
            with self._derived_columns_guard(
                connection,
                owned_columns=owned_columns,
                scope_where=scope_where,
                scope_params=list(stock_codes),
            ):
                connection.execute(
                    f"DELETE FROM indicator_snapshot WHERE stock_code IN ({placeholders})",
                    stock_codes,
                )
                self._insert_records_in_connection(connection, records, "indicator_snapshot")
                row_count = connection.execute(
                    "SELECT COUNT(*) FROM indicator_snapshot"
                ).fetchone()[0]
                expected_count = previous_total - affected_count + len(records)
                if row_count != expected_count:
                    raise RuntimeError(
                        "snapshot delta validation failed: "
                        f"rows={row_count}, expected={expected_count}"
                    )
            self._record_derived_lineage_in_connection(connection, records)

    def _publish_snapshot(
        self, staging_table: str, expected_count: int, records: list[dict[str, Any]],
        *, preserve_codes: Sequence[str] = (),
    ) -> None:
        """Replace the whole snapshot table, keeping quarantined codes untouched.

        2026-11-12 契约 §4.2：被门禁隔离的股票不得因"整表替换"而丢掉上一代
        已发布行——它们的旧快照继续参与显示与陈旧度披露，直到股本补全。
        因此整表 DELETE 改成 ``DELETE ... WHERE stock_code NOT IN (隔离股)``，
        并在同一事务内校验最终行数。
        """
        preserved = [str(code) for code in preserve_codes]
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
            preserved_count = 0
            placeholders = ""
            if preserved:
                placeholders = ", ".join("?" for _ in preserved)
                preserved_count = connection.execute(
                    f"SELECT COUNT(*) FROM indicator_snapshot "
                    f"WHERE stock_code IN ({placeholders})",
                    preserved,
                ).fetchone()[0]
            staging_columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{staging_table}")').fetchall()
            }
            scope_where = (
                f" WHERE stock_code NOT IN ({placeholders})" if preserved else ""
            )
            with self._derived_columns_guard(
                connection,
                owned_columns=staging_columns,
                scope_where=scope_where,
                scope_params=list(preserved),
            ):
                if preserved:
                    connection.execute(
                        f"DELETE FROM indicator_snapshot WHERE stock_code NOT IN ({placeholders})",
                        preserved,
                    )
                else:
                    connection.execute("DELETE FROM indicator_snapshot")
                connection.execute(
                    f'INSERT INTO indicator_snapshot BY NAME SELECT * FROM "{staging_table}"'
                )
            final_count = connection.execute(
                "SELECT COUNT(*) FROM indicator_snapshot"
            ).fetchone()[0]
            if final_count != expected_count + preserved_count:
                raise RuntimeError(
                    "snapshot validation failed: "
                    f"rows={final_count}, expected={expected_count + preserved_count} "
                    f"(published={expected_count}, preserved={preserved_count})"
                )
            self._record_derived_lineage_in_connection(connection, records)

    def _record_owned_column_names(
        self, connection: Any, records: list[dict[str, Any]],
    ) -> set[str]:
        """本事务"写了"的列 = 记录里出现过的列（与 `_insert_records_in_connection` 同判据）。

        只有**完全没出现在任何记录里**的列才算"派生列"：记录里出现但值为 None
        的列仍由计算器负责（那是"算出来是空"，不是"没写"）。
        """
        column_names = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info('{SNAPSHOT_TABLE}')").fetchall()
        ]
        return {name for name in column_names if any(name in record for record in records)}

    def _non_null_column_counts(
        self,
        connection: Any,
        columns: Sequence[str],
        scope_where: str,
        scope_params: Sequence[Any],
    ) -> dict[str, int]:
        """统计给定列在"本事务将被替换的行"上的非空行数（一次查询拿全部列）。"""
        if not columns:
            return {}
        selects = ", ".join(
            f'SUM(CASE WHEN "{column}" IS NOT NULL THEN 1 ELSE 0 END)' for column in columns
        )
        row = connection.execute(
            f"SELECT {selects} FROM {SNAPSHOT_TABLE}{scope_where}",
            list(scope_params),
        ).fetchone()
        return {
            column: int(value or 0)
            for column, value in zip(columns, row or (), strict=False)
        }

    @contextlib.contextmanager
    def _derived_columns_guard(
        self,
        connection: Any,
        *,
        owned_columns: Collection[str],
        scope_where: str,
        scope_params: Sequence[Any],
    ) -> Iterator[None]:
        """派生列守卫：**先保全、再重建、最后校验没有列被清空**。

        `owned_columns` 之外的一切 `indicator_snapshot` 列都被视为"派生列"
        （ROCE 六列、H 股市值几列、以及以后新增的外部派生列）。三步：

        1. **保全**：删除前把非空派生列按 `(stock_code, report_date)` 备份到临时表，
           插入后 `COALESCE(新值, 旧值)` 回填——这样即便该域没有重建钩子，
           "整表重发把派生列清成 NULL"（K5/D28）也不可能再发生。
        2. **重建**：跑 `derived_snapshot_domains()` 里登记的钩子，让依赖行情/市值
           派生值保持新鲜（ROCE 的 `eligible` 就依赖当日市值与行业）。
        3. **校验**：重建后若某列"删除前有值、插入后全空"→ 抛错回滚整批发布，
           宁可发布失败也不允许静默把已发布数据抹成 NULL。
        """
        snapshot_columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info('{SNAPSHOT_TABLE}')").fetchall()
        ]
        untouched = [column for column in snapshot_columns if column not in owned_columns]
        before = self._non_null_column_counts(
            connection, untouched, scope_where, scope_params
        )
        carried = [column for column in untouched if before.get(column, 0) > 0]
        backup_table = f"_vd_derived_backup_{uuid.uuid4().hex[:12]}"
        if carried:
            column_list = ", ".join(f'"{column}"' for column in carried)
            connection.execute(
                f'CREATE TEMP TABLE "{backup_table}" AS '
                f"SELECT stock_code, report_date, {column_list} "
                f"FROM {SNAPSHOT_TABLE}{scope_where}",
                list(scope_params),
            )
        try:
            yield
            if carried:
                assignments = ", ".join(
                    f'"{column}" = COALESCE(s."{column}", backup."{column}")'
                    for column in carried
                )
                connection.execute(
                    f'UPDATE {SNAPSHOT_TABLE} AS s SET {assignments} '
                    f'FROM "{backup_table}" AS backup '
                    "WHERE s.stock_code = backup.stock_code "
                    "AND s.report_date = backup.report_date"
                )
            for domain in derived_snapshot_domains():
                try:
                    domain.rebuild(connection)
                except Exception as error:  # noqa: BLE001 - 单域失败不连坐其它域
                    logger.warning(
                        "派生域 %s 重建失败（列 %s 将沿用上一代值）：%s",
                        domain.name,
                        ",".join(domain.columns),
                        error,
                        exc_info=True,
                    )
            after = self._non_null_column_counts(
                connection, untouched, scope_where, scope_params
            )
            wiped = [
                column
                for column in carried
                if after.get(column, 0) == 0
            ]
            if wiped:
                raise RuntimeError(
                    "snapshot publish would wipe derived columns: "
                    f"{', '.join(wiped)}（发布前有值、发布后全空）。"
                    "该列不由指标计算器生产，必须登记到 "
                    "calculator.derived_snapshot_domains()，"
                    "或确认它已退役后从 schema/注册表同时删除。"
                )
        finally:
            if carried:
                connection.execute(f'DROP TABLE IF EXISTS "{backup_table}"')

    def _insert_records_in_connection(
        self, connection: Any, records: list[dict[str, Any]], table_name: str,
    ) -> None:
        """Insert computed snapshot rows through an existing connection.

        Kept separate from _write_batch so the delta publication path can use
        the same transaction connection for DELETE + INSERT + validation.
        """
        if not records:
            return
        columns = connection.execute(
            f"PRAGMA table_info('{table_name}')"
        ).fetchall()
        column_names = [row[1] for row in columns]
        # Records may differ by source or data availability. Keep every schema
        # column emitted by any record instead of silently dropping late fields.
        fields = [
            column for column in column_names
            if any(column in record for record in records)
        ]
        if not fields:
            return
        placeholders = ", ".join(["?"] * len(fields))
        field_str = ", ".join(fields)
        sql = f'INSERT INTO "{table_name}" ({field_str}) VALUES ({placeholders})'
        all_values = [[rec.get(key) for key in fields] for rec in records]
        connection.executemany(sql, all_values)

    def _write_batch(self, records: list[dict], table_name: str = "indicator_snapshot") -> None:
        """批量写入指标快照

        使用executemany批量INSERT（比逐条快10倍+）
        """
        if not records:
            return
        with self.duck.write_connection() as conn:
            self._insert_records_in_connection(conn, records, table_name)

    def _record_derived_lineage(self, records: list[dict[str, Any]]) -> None:
        """Write lineage for every published snapshot value from the final table.

        The calculator cannot claim strict confidence unless all upstream inputs
        are verifiably strict; until that propagation is available it records
        derived values as approximate with a formula/data-version marker.
        """
        with self.duck.transaction() as connection:
            self._record_derived_lineage_in_connection(connection, records)

    def _record_derived_lineage_in_connection(
        self, connection: Any, records: list[dict[str, Any]],
    ) -> None:
        formula = "indicator_calculator/latest_restated/v1"
        batch_id = str(uuid.uuid4())
        raw_hash = hashlib.sha256(formula.encode("utf-8")).hexdigest()
        if not records:
            return
        audit_rows: list[tuple[Any, ...]] = []
        excluded = {"stock_code", "report_date", "latest_price_date", "calculated_at", "data_version"}
        for row in records:
            for field_name, value in row.items():
                if field_name in excluded or not isinstance(value, (int, float)):
                    continue
                audit_rows.append((
                    row["stock_code"], field_name, row["report_date"], value,
                    "derived_calculator", batch_id, datetime.now(UTC), raw_hash,
                    "approximate", "derived_input_lineage_pending", "indicator_calculator/v1",
                    row["report_date"], "latest_restated", formula,
                ))
        connection.execute(
            """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash,
                row_count, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "indicator_snapshot", "derived_calculator", "indicator_calculator/v1",
             datetime.now(UTC), raw_hash, len(audit_rows), "approximate"],
        )
        archive_raw_response_if_absent(
            connection,
            raw_response_hash=raw_hash,
            source="derived_calculator",
            fetch_time=datetime.now(UTC),
            payload=formula.encode("utf-8"),
            api_version="indicator_calculator/v1",
        )
        # 2026-09-04 OOM 修复：不要再对 source_audit 直接 executemany。
        # DuckDB 1.5.5 Python 驱动在 executemany 绑定 date/datetime 参数时，
        # 事务本地内存按 ~450KB/行 堆积（1 万行实测峰值 4.4GB），
        # 全量发布 24.5 万行 lineage 直接 OOM（7.4GB 预算耗尽，
        # 2026-09-04 自动更新验证轮实锤，并已在库副本上复现）。
        # 改为 pandas 向量化注册 + 单条 INSERT SELECT：
        # 24.5 万行实测 0.3s / 峰值 <100MB，且仍在同一事务内保持原子性
        # （与 reports/97 Sina 明细 TEMP TABLE 合并写入同思路）。
        import pandas as pd

        lineage_columns = (
            "stock_code", "field_name", "report_date", "value", "source",
            "fetch_batch_id", "fetch_time", "raw_response_hash", "confidence",
            "reason_code", "api_version", "effective_date", "data_version",
            "formula",
        )
        frame = pd.DataFrame(audit_rows, columns=lineage_columns)
        for column in ("report_date", "effective_date"):
            frame[column] = pd.to_datetime(frame[column])
        # fetch_time 为 UTC 时区感知值；统一转成 naive UTC 落 TIMESTAMP 列，
        # 与原 executemany 路径的落库语义一致。
        frame["fetch_time"] = (
            pd.to_datetime(frame["fetch_time"], utc=True).dt.tz_localize(None)
        )
        lineage_view = f"_derived_lineage_stage_{uuid.uuid4().hex[:8]}"
        connection.register(lineage_view, frame)
        try:
            connection.execute(
                f"""INSERT INTO source_audit
                    ({', '.join(lineage_columns)})
                    SELECT stock_code, field_name,
                           CAST(report_date AS DATE), value, source,
                           fetch_batch_id, fetch_time, raw_response_hash,
                           confidence, reason_code, api_version,
                           CAST(effective_date AS DATE), data_version, formula
                    FROM {lineage_view}"""
            )
        finally:
            connection.unregister(lineage_view)
