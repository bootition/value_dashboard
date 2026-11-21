"""`_retry_failed_tasks` 的重试编排（reports/140 §2.5 / §13.7 拆分的第 3 块）。

## 这个模块是什么

`IncrementalUpdater._retry_failed_tasks`（约 306 行）原本是 `update.py` 里最长的
一个方法：它按 `data_type` 逐个分支把 retry_list 里的条目重新派发给对应的域
updater（业务概览 / 股本链 / 国债曲线 / 融资事件 / 指数估值 / ETF / 行情动作），
负责"成功就删队列项、失败就记一次退避"，最后汇总成一步报告。

这些分支彼此独立、又不碰价格管道，属于"内聚、低耦合"的搬迁对象；搬走后
`update.py` 里只留同名的薄委托，外部调用点与既有测试断言零改动。

## 边界

- **不 import `app.core.update`**（会成循环依赖）。所有状态通过 `updater`
  显式读写，`logger` 与 `is_duckdb_fatal` 由调用方注入——日志通道仍是
  `app.core.update` 那一个 logger，异常判定也只有 `update.py` 一份实现。
- **行为零变更**：分支顺序、SQL 字面量、日志文案、异常类型、`_mark_retry_failed`
  的退避参数全部原样；本模块只是把 `self.` 换成 `owner.`、把两个模块级
  依赖改成入参。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.financial_period import cn_today

__all__ = ["retry_failed_tasks"]


def retry_failed_tasks(
    owner: Any,
    tasks: list[dict],
    *,
    logger: logging.Logger,
    is_duckdb_fatal: Callable[[Exception], bool],
) -> dict:
    """重试失败任务"""
    logger.info(f"[增量] 重试 {len(tasks)} 个失败任务...")

    success_count = 0
    still_failing = 0
    resolved_unconfigured = 0
    resolved_non_retryable = 0
    recompute_codes: list[str] = []

    def note_snapshot_input(code: str) -> None:
        if code and code not in recompute_codes:
            recompute_codes.append(code)

    def note_treasury_curve_input() -> None:
        try:
            for row in owner.duck.read_query(
                "SELECT stock_code FROM stock_meta "
                "WHERE is_listed IS TRUE ORDER BY stock_code"
            ):
                note_snapshot_input(row["stock_code"])
        except Exception as error:
            logger.warning("查询国债重试影响范围失败: %s", error)

    for task in tasks:
        retry_id = task["id"]
        stock_code = task["stock_code"]
        data_type = task["data_type"]
        if data_type != "price_daily":
            if data_type not in {
                "balance_sheet", "income_statement", "cash_flow",
                "dividends", "xdxr",
                "company_profile", "business_breakdown",
                "share_capital_history", "treasury_yield_curve",
                "ipo_funding", "placement_funding", "index_valuation",
                "etf_daily",
            }:
                # 无逐股重试路径的数据域（announcements 等）：retry 条目
                # 是 pending 标记，由对应维护流程消费；在这里重试只会
                # 每轮失败并递增 retry_count（死循环），必须跳过。
                continue
            if data_type in {"company_profile", "business_breakdown"}:
                from app.core.business import BusinessOverviewUpdater
                outcome = BusinessOverviewUpdater(
                    duck=owner.duck, sqlite=owner.sqlite, adapter=owner.adapter_mgr,
                ).update_stock(stock_code)
                if outcome["status"] == "success":
                    owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
                else:
                    still_failing += 1
                    owner._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                continue
            if data_type == "share_capital_history":
                # P4-5 修复（依据 S3）：历史股本链 retry 由回填器消费
                # 2026-08-14 红队 F1：单股异常（如 DuckDB FatalException
                # "Failed to delete all rows from index"）曾逃逸到 run_once，
                # 炸掉整轮更新且 retry_count 永不递增（永久死循环）。
                # 现加 per-task 隔离：异常只标记该股失败，轮次继续；
                # 累计失败仍受 max_retries 上限约束。
                try:
                    from app.core.capital import CapitalHistoryUpdater
                    outcome = CapitalHistoryUpdater(
                        duck=owner.duck, sqlite=owner.sqlite, adapter=owner.adapter_mgr,
                    ).update_stock(stock_code)
                except Exception as error:
                    still_failing += 1
                    reason = f"retry crashed: {error}"
                    if is_duckdb_fatal(error):
                        reason = f"duckdb_fatal (建议重建 share_capital_history 索引): {error}"
                    owner._mark_retry_failed(retry_id, reason)
                    logger.error(
                        "[增量] 股本链重试 %s 异常: %s（该股已隔离，本轮继续）",
                        stock_code, error,
                    )
                    continue
                if outcome["status"] == "success":
                    owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
                else:
                    still_failing += 1
                    owner._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                continue
            if data_type == "treasury_yield_curve":
                # P3-5 修复（依据 S3）：国债曲线 retry 按 extra_json
                # 恢复对应模式（history=按期限回填，daily=按日期日终）
                from app.core.treasury import TreasuryCurveUpdater
                try:
                    extra = json.loads(task.get("extra_json") or "{}")
                except json.JSONDecodeError:
                    extra = {}
                updater = TreasuryCurveUpdater(
                    duck=owner.duck, sqlite=owner.sqlite, adapter=owner.adapter_mgr,
                )
                if extra.get("mode") == "history" and extra.get("tenor") is not None:
                    outcome = updater._backfill_one(float(extra["tenor"]))
                else:
                    outcome = updater.update_daily(
                        [str(extra.get("work_date") or "")[:10]]
                        if extra.get("work_date") else None
                    )
                if outcome["status"] == "success":
                    owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
                    note_treasury_curve_input()
                else:
                    still_failing += 1
                    owner._mark_retry_failed(
                        retry_id, outcome.get("error") or outcome.get("reason") or "retry failed"
                    )
                continue
            if data_type in {"ipo_funding", "placement_funding"}:
                # 融资事件域（数据补全 2026-08-25）：单股 update_stock 同时
                # 重抓 IPO + 增发/配股；成功后清理该股全部 funding 相关 retry。
                from app.core.funding import FundingUpdater
                outcome = FundingUpdater(
                    duck=owner.duck, sqlite=owner.sqlite, adapter=owner.adapter_mgr,
                ).update_stock(stock_code)
                if outcome["status"] == "success":
                    owner.sqlite.execute(
                        "DELETE FROM retry_list WHERE stock_code = ? "
                        "AND data_type IN ('ipo_funding', 'placement_funding')",
                        [stock_code],
                    )
                    success_count += 1
                    note_snapshot_input(stock_code)
                else:
                    still_failing += 1
                    owner._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
                continue
            if data_type == "index_valuation":
                # 指数估值域（数据补全 2026-08-25）：stock_code 存指数代码，
                # 重试即重新抓取主源+交叉源全量（低频，无风控风险）。
                # SW_ALL 为申万一级行业分组重试（2026-09-05 v21）。
                from app.core.index_valuation import SW_INDUSTRY_GROUP, IndexValuationUpdater
                updater = IndexValuationUpdater(duck=owner.duck, sqlite=owner.sqlite)
                if stock_code == SW_INDUSTRY_GROUP:
                    outcome = updater.update_sw_industries()
                    error_text = outcome.get("error", "retry failed")
                else:
                    outcome = updater.update_daily([stock_code])
                    error_text = (
                        outcome.get("indexes", {}).get(stock_code, {}).get("primary_error")
                        or "retry failed"
                    )
                if outcome["status"] == "success":
                    owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
                else:
                    still_failing += 1
                    owner._mark_retry_failed(retry_id, error_text)
                continue
            if data_type == "etf_daily":
                # ETF 行情域（2026-09-05）：stock_code 存 ETF 代码。
                # 缺少同花顺 API Key 是配置项而非可重试故障——如实转
                # missing 并从活跃重试队列移出，避免状态页永久"待重试"。
                from app.core.etf_prices import EtfPriceUpdater
                try:
                    outcome = EtfPriceUpdater(
                        duck=owner.duck, sqlite=owner.sqlite,
                    ).update_etf(stock_code)
                except Exception as error:
                    outcome = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
                error_text = outcome.get("error") or ""
                # 2026-11-21 事故：`etf_meta` 里的合成伪代码 `ALL_A`（全A指数）被当基金抓，
                # 同花顺回 `code=3001: Fund not found`（正确），却被反复重试 →
                # 每轮 `retries` 都 failed，整轮永远 partial。伪代码/确定性 not-found
                # 一律结算成 missing 并移出重试队列。
                from app.core.etf_prices import (
                    _is_definitive_not_found,
                    is_price_fetchable,
                )

                if not is_price_fetchable(stock_code) or _is_definitive_not_found(error_text):
                    owner._record_missing(stock_code, data_type, "source_unsupported")
                    owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    resolved_non_retryable += 1
                    logger.info(
                        "[增量] %s %s 不是可抓行情的交易所代码或源侧确定性 not-found，"
                        "登记 missing(source_unsupported) 并移出重试队列（错误=%s）",
                        stock_code, data_type, error_text[:80],
                    )
                    continue
                if outcome["status"] == "success":
                    owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                    success_count += 1
                elif "HITHINK_FINANCE_API_KEY" in error_text:
                    with owner.sqlite.transaction() as conn:
                        conn.execute(
                            """INSERT INTO missing_list (stock_code, field_name, reason_code)
                               VALUES (?, 'etf_daily', 'source_unconfigured')
                               ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                               DO UPDATE SET reason_code = excluded.reason_code,
                                             detected_at = CURRENT_TIMESTAMP""",
                            [stock_code],
                        )
                        conn.execute(
                            "DELETE FROM retry_list WHERE id = ?", [retry_id],
                        )
                    resolved_unconfigured += 1
                else:
                    still_failing += 1
                    owner._mark_retry_failed(retry_id, error_text)
                continue
            if data_type in owner._MARKET_ACTION_DATA_TYPES and owner._is_b_share_stock(
                stock_code
            ):
                # 2026-11-13 P1（E）：B 股没有分红/除权源。直接如实登记
                # missing(source_unsupported) 并撤销 retry，不消耗重试次数
                # ——否则 900948/xdxr(retry_count=4/5) 下一轮就会以
                # retry_exhausted 的假名义永久消失。
                owner._record_missing(stock_code, data_type, "source_unsupported")
                owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                resolved_non_retryable += 1
                logger.info(
                    "[增量] %s %s 为 B 股（无该源），登记 missing(source_unsupported) 并移出重试",
                    stock_code, data_type,
                )
                continue
            outcome = owner.refetch_one(stock_code, data_type)
            if outcome["status"] == "success":
                owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
                success_count += 1
                if data_type in {
                    "balance_sheet", "income_statement", "cash_flow",
                    "dividends", "xdxr",
                }:
                    note_snapshot_input(stock_code)
                if data_type == "xdxr":
                    # 重试成功说明本轮真的写入了除权事件 → 下一轮强制全历史
                    # 重拉 qfq（2026-11-13 P0 B：不依赖日期比较）。
                    owner._mark_qfq_refetch_pending([stock_code])
            elif owner._settle_non_retryable_error(
                retry_id, stock_code, data_type, outcome,
            ):
                # 2026-11-12：本次抓取已明确是"合法空/源不支持"（新股无实施
                # 分红、BSE 不支持），不得再消耗重试次数，也不得以
                # retry_exhausted 的假名义进 missing_list。
                resolved_non_retryable += 1
            else:
                still_failing += 1
                owner._mark_retry_failed(retry_id, outcome.get("error", "retry failed"))
            continue

        try:
            extra = json.loads(task.get("extra_json") or "{}")
        except json.JSONDecodeError:
            still_failing += 1
            owner._mark_retry_failed(retry_id, "invalid retry metadata")
            continue
        # 2026-10-30 修复：价格管道 raw/qfq 是成对失败的，旧 retry
        # 默认只重抓 raw，成功后删掉任务，导致 qfq 永久停在旧日期，
        # readiness 的 price_freshness 持续失败。无显式 adjust 时
        # 默认同时增量刷新 raw + qfq；显式单边任务保持原语义。
        adjust = extra.get("adjust") or "both"
        adjusts = ("raw", "qfq") if adjust == "both" else (adjust,)
        end_date = cn_today().strftime("%Y-%m-%d")
        results: dict[str, Any] = {}
        failure: str | None = None
        for side in adjusts:
            table = "price_daily_raw" if side == "raw" else "price_daily_qfq"
            start_from: str | None = None
            try:
                latest_rows = owner.duck.read_query(
                    f"SELECT MAX(trade_date) AS latest FROM {table} WHERE stock_code = ?",
                    [stock_code],
                )
                latest_value = latest_rows[0]["latest"] if latest_rows else None
                if latest_value:
                    start_from = str(latest_value)[:10]
            except Exception:
                start_from = None
            result = owner.adapter_mgr.fetch(FetchRequest(
                data_type=data_type,
                stock_codes=[stock_code],
                adjust=side,
                start_date=start_from,
                end_date=end_date,
            ))
            if result.metadata.error or not result.data:
                failure = result.metadata.error or f"empty {side} result"
                break
            results[side] = result

        if failure is not None:
            still_failing += 1
            owner._mark_retry_failed(retry_id, failure)
            continue
        try:
            if adjust == "both":
                owner._persist_incremental_price_pair(
                    stock_code, results["raw"], results["qfq"],
                    full_field_audit=True,
                )
            else:
                owner._persist_price_with_lineage(
                    stock_code, adjust, results[adjust],
                )
        except (RuntimeError, ValueError, TypeError) as error:
            still_failing += 1
            owner._mark_retry_failed(retry_id, str(error))
            continue
        owner.sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
        success_count += 1
        note_snapshot_input(stock_code)
        logger.info(
            "  重试成功并落库: %s %s adjust=%s", stock_code, data_type, adjust,
        )

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
        "resolved_unconfigured": resolved_unconfigured,
        "resolved_non_retryable": resolved_non_retryable,
        "still_failing": still_failing,
        "recompute_codes": recompute_codes,
    }
