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

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，本块登记全部原文小节号）
# S1  reports/67 —— 业务概览低频域：自动集成开关 + 间隔到期才执行；失败保留旧值并进独立 retry/missing。
# S2  reports/68 P4 —— 历史股本链 + 统计域：有界续传，失败不阻断其他步骤。
# S3  reports/73 —— 三处修复：P4-10 仅全部成功才持久化指纹；P4-5 历史股本链 retry 由回填器消费；
#     P3-5 国债曲线 retry 按 extra_json 消费。
# S4  reports/114 —— 全市场追赶一次要十几小时（2026-10-20 诊断）：低频域与追赶必须分轮有界。
# S5  reports/119 D28 / D29 ①–⑦ / P1-3 —— 手工链自动接入：补采域（D29 ②③④）、扩展指标域重建
#     （D28，P0）、ETF 行情 + 港股分红（D29 ⑤⑥）、PDF 域（⑦）；P1-3 固定资产净额补采
#     （2026Q3 主链只剩 1.8%）。另：`*_last_success_at` 是与体检域约定的水位键名。
# S6  reports/120 §3 事实 4 —— 价格步骤"成功"但该股 qfq 仍停在旧值，需并入技术指标待重算集合。
# S7  reports/122 §2.2 J6 —— qfq<raw 的股票**无条件**并入技术指标重算；只有真正算过才清名单
#     （rejected/skipped 与 failed_codes 都留着）。
# S8  reports/126 —— `all_pending_in_cooldown` **不算追平**（冷却与"真的没得补"必须区分）。
# S9  reports/130 —— ROCE 域重建入口（口径 C，历史序列来自 roce_history）。
# S10 reports/131 K5 / §9.2 P0-5 / P0-9 —— qfq 写入成功即打 technical_pending（技术三列强制重算）；
#     指纹新增 `share_capital_history` 段后必须重算判定。
# S11 reports/139 §2.2 #13 —— 卖方一致预期每日快照（schema v48）。
# S12 reports/140 §2.5 —— 拆成 `update_parts/{prices,retry}.py` 的第 1 块：本文件只留薄兼容壳（委托），
#     不改任何判据；`_PriceReplaceBuffer` 私有名与时钟源语义保持不变。
# S13 reports/140 §2.5 —— 第 4 块：`_update_prices_incremental`(502 行) 切成
#     `update_parts/prices_step.py` 的"选谁 / 怎么抓 / 怎么结算"三段，本文件只留同名委托；
#     SQL、日志文案、超时/并发公式、`finally` 排空顺序与时钟源（`update.time`）全部原样。
# S14 reports/140 §2.5 —— 第 5 块（最后一块）：`_run_incremental_update_flow`(493 行) 整体搬到
#     `update_parts/flow.py`，本文件只留同名委托。该方法是**步骤清单的事实来源**（30 处
#     `report_step` / 26 个步骤名的首次出现顺序），故按逐行平移搬移：调用点顺序、SQL 与日志
#     字符串常量、`*..._pending_codes` 展开形态逐条对拍，四个私有依赖经入参注入同一对象。


from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager

# 契约 §3：股本完整性与 missing 口径的单一真相，禁止在本文件复制第二份 SQL。
from app.core.data_quality import actionable_missing_count, listed_share_capital_gap_codes
from app.core.financial_guards import (
    BALANCE_SHEET_TABLE,
    CONTRACT_LIABILITIES_COLUMN,
    guard_caches,
    guard_contract_liabilities_write,
)
from app.core.financial_period import CN_TZ as _CN_TZ

# 契约 §2：多子步骤状态归并按严重度取最坏值，rejected 不得被 success 覆盖。
# 步骤状态汇总（`aggregate_job_status`）与步骤名登记表（`STEP_NAMES`）随主流程搬到
# `update_parts/flow.py`（S14），本文件不再直接引用，避免两处各持一份"步骤清单入口"。
from app.core.financial_period import cn_now, cn_today, expected_financial_period
from app.core.job_status import merge_step_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update_parts import flow, prices_step
from app.core.update_parts.prices import PriceReplaceBuffer
from app.core.update_parts.retry import (
    ERROR_KIND_REASON_CODE,
    NON_RETRYABLE_ERROR_KINDS,
    check_retry_tasks,
    cleanup_unretryable_tasks,
    mark_retry_failed,
    persist_price_retry,
    settle_non_retryable_error,
    settle_non_retryable_retries,
)
from app.core.update_parts.retry_tasks import retry_failed_tasks

logger = logging.getLogger(__name__)


def _is_duckdb_fatal(error: Exception) -> bool:
    """DuckDB 致命错误（连接失效类）检测，用于 retry 隔离时的原因标注。

    2026-08-14 红队 F1：ART 索引损坏时 DELETE 抛 FatalException 且连接
    随后 invalidated；此类错误重试无意义，应在原因里引导重建索引。
    """
    text = f"{type(error).__name__} {error}".lower()
    return "fatalexception" in text or "invalidated" in text or "must be restarted" in text


# ─── 公告分类（PRD §7.7）───────────────────────────────────────────
# 只有定期报告/业绩预告/业绩快报类公告触发财务刷新；
# 其他公告只登记入册，不进入财务刷新队列。

_ANNOUNCEMENT_FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "年度报告", "半年度报告", "半年报", "一季报", "第一季度报告",
    "三季报", "第三季度报告", "业绩预告", "业绩快报",
)
# 财务公告按 CNINFO 类别精确查询，避免全市场全文列表翻页被噪声淹没。
# 顺序即查询顺序：当前处于中报季，半年报优先。
_ANNOUNCEMENT_FINANCIAL_CATEGORIES: tuple[str, ...] = (
    "semi_annual", "q1", "q3", "annual",
)
_ANNOUNCEMENT_DIVIDEND_CATEGORY = "dividend"
_ANNOUNCEMENT_CHECK_CURSOR_KEY = "announcement_check_cursor"
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


def _as_counter(value: Any) -> int:
    """Coerce a step counter to int without letting bad data break a merge."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_indicator_steps(*steps: dict[str, Any] | None) -> dict[str, Any]:
    """Merge indicator sub-steps by severity (contract §2) keeping counters.

    2026-11-12 P0：历史写法 ``{**后一个子步骤, "full_recompute": 前一个}``
    会让后一个子步骤的 status 覆盖前一个——于是 ``refresh_treasury_spreads``
    的 success 抹掉了价格域/财务域重算的 ``rejected``，状态页假绿。
    状态一律交给 :func:`app.core.job_status.merge_step_status`（rejected 最高
    优先级），本函数只额外把 success/failed 计数汇总到外层便于观测。
    """
    merged = merge_step_status(*steps)
    for counter in ("success", "failed"):
        merged[counter] = sum(
            _as_counter(step.get(counter)) for step in steps if isinstance(step, dict)
        )
    return merged


# 契约 §1/§5 的单一真相现在在 `app/core/update_parts/retry.py`；这里只转出旧私有名，
# 供本文件其余方法（以及既有 `from app.core.update import _NON_RETRYABLE_ERROR_KINDS`
# 的调用点）继续按原名使用。不要在本文件复制第二份内容。
_NON_RETRYABLE_ERROR_KINDS = NON_RETRYABLE_ERROR_KINDS
_ERROR_KIND_REASON_CODE = ERROR_KIND_REASON_CODE


def _source_error_kind(result: Any) -> str:
    """Read a fetch result's ``metadata.error_kind`` (contract §1).

    只有明确的分类才被采用：``none``（"没错误"）出现在失败路径上说明适配器
    漏标，按 ``source_unavailable`` 处理——真故障宁可贵一点也不能被当成合法空。
    用 ``getattr`` 容错，``error_kind`` 字段缺失时同样回落到旧行为。
    """
    metadata = getattr(result, "metadata", None)
    kind = str(getattr(metadata, "error_kind", "") or "").strip()
    if kind and kind != "none":
        return kind
    return "source_unavailable"


def _source_skipped_codes(result: Any) -> dict[str, str]:
    """Read a fetch result's ``metadata.skipped_codes`` (code → reason), if any."""
    metadata = getattr(result, "metadata", None)
    skipped = getattr(metadata, "skipped_codes", None)
    if not isinstance(skipped, dict):
        return {}
    return {str(code): str(reason or "") for code, reason in skipped.items() if code}


def _source_truncated(result: Any) -> bool:
    """Whether a fetch result is a legitimate-but-truncated response (contract §1)."""
    metadata = getattr(result, "metadata", None)
    return bool(getattr(metadata, "truncated", False)) or (
        _source_error_kind(result) == "truncated_ok"
    )


def _announcement_step_result(check: dict[str, Any] | None) -> dict[str, Any]:
    """Turn an announcement check report into a truthful ``steps.announcements``.

    2026-11-12 P1：``report_step("announcements", ...)`` 原先只在
    ``check_report["blocked"]`` 分支调用，于是当天两轮的公告截断/失败在
    job_logs 里完全不可见。现在无论 available/partial/unavailable/truncated
    都入册；截断是 ``partial`` + reason，不升级成 ERROR。
    """
    if not isinstance(check, dict):
        return {"status": "skipped", "reason": "announcements_not_checked"}
    raw_status = str(check.get("status") or "partial")
    truncated = bool(check.get("truncated"))
    errors = [str(error) for error in check.get("errors") or []]
    warnings = [str(warning) for warning in check.get("warnings") or []]
    if raw_status in {"deferred", "skipped"}:
        status = "skipped"
    elif raw_status in {"unavailable", "failed", "error"}:
        status = "failed"
    elif truncated or errors or raw_status == "partial":
        status = "partial"
    elif raw_status in {"available", "success", "ok"}:
        status = "success"
    else:
        status = "partial"
    reason = check.get("reason")
    if not reason and status == "partial":
        reason = "announcements_truncated" if truncated else "announcements_partial"
    if not reason and status == "failed":
        reason = "announcement_source_unavailable"
    return {
        "status": status,
        "reason": reason,
        "checked_remote": bool(check.get("checked_remote")),
        "truncated": truncated,
        "truncated_categories": list(check.get("truncated_categories") or []),
        "affected_count": len(check.get("affected_stock_codes") or []),
        "new_announcement_count": sum(
            len(items) for items in (check.get("all_new_announcements") or {}).values()
        ),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }


class _PriceReplaceBuffer(PriceReplaceBuffer):
    """全历史价格替换的攒批缓冲（类本体已搬到 ``app/core/update_parts/prices.py``）。

    依据 S12 §2.5 拆分的第 1 块。这里只留一个**薄兼容壳**，不改任何判据：

    * 保留 ``_PriceReplaceBuffer`` 这个历史私有名，既有
      ``from app.core.update import _PriceReplaceBuffer`` 调用点与测试一行都不用改；
    * 时钟源仍是**本模块**的 ``time.monotonic()``。``tests/regression/test_price_batch_flush.py``
      用 ``monkeypatch.setattr(update_module, "time", _FakeClock())`` 跳时间，若直接复用
      ``prices`` 模块里的 ``time``，那套 monkeypatch 会静默失效（缓冲改看真实时钟）。
      因此这里把时钟注入为对 ``self`` 所在模块全局 ``time`` 的**运行期**查找，
      搬移前后时钟语义完全一致。
    """

    __slots__ = ()

    def __init__(self, *, max_stocks: int, max_rows: int, max_seconds: float) -> None:
        super().__init__(
            max_stocks=max_stocks,
            max_rows=max_rows,
            max_seconds=max_seconds,
            # 必须是 lambda：``update.time`` 可能在构造之后被 monkeypatch 替换。
            clock=lambda: time.monotonic(),
        )


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
        # 本地价格陈旧阈值：仅当显式开启 price_full_refetch_on_stale 时，
        # 距今超过该天数的 raw 才整段重拉（数据修复场景）。
        # 默认关闭：常规追赶一律从本地最新日期增量拉取。否则长时间未启动
        # 会把全市场个股全历史（每股数千行）逐股 DELETE+INSERT 回主表，
        # 实测吞吐从约 150 只/分跌到约 6 只/分（2026-10-20 诊断）。
        self.incremental_window_days: int = self._load_update_config(
            "incremental_window_days", default=30
        )
        self.price_full_refetch_on_stale: bool = self._load_update_config(
            "price_full_refetch_on_stale", default=False
        )
        # 全市场公告检查的日期回看窗口（默认 3 天）
        self.announcement_lookback_days: int = self._load_update_config(
            "announcement_lookback_days", default=3
        )
        # 公告检查游标缺失时的首次追补窗口（天）。2026-08-28 之前
        # CNINFO 分页 bug 导致中报大量漏检，首次修复需回看整个披露季。
        self.announcement_catchup_days: int = self._load_update_config(
            "announcement_catchup_days", default=45
        )
        # CNINFO 每页实际固定返回 30 条；财务类别在 45 天窗口约 11,000 条，
        # 需要 370+ 页。非财务公告只做最近 3 天注册，页数设上限即可。
        self.announcement_financial_max_pages: int = self._load_update_config(
            "announcement_financial_max_pages", default=450
        )
        self.announcement_general_max_pages: int = self._load_update_config(
            "announcement_general_max_pages", default=40
        )
        # CSRC 行业低频刷新间隔（天）：行业归属变化极低，避免每次更新
        # 逐股查询 CNINFO（约 1.5s/股）占用数小时（默认 30 天）
        self.csrc_refresh_interval_days: int = self._load_update_config(
            "csrc_refresh_interval_days", default=30
        )
        self.financial_detail_backfill_max_stocks_per_run: int = self._load_update_config(
            "financial_detail_backfill_max_stocks_per_run", default=10000
        )
        self.financial_detail_backfill_concurrency: int = self._load_update_config(
            "financial_detail_backfill_concurrency", default=16
        )
        self.financial_detail_backfill_persist_batch_size: int = self._load_update_config(
            "financial_detail_backfill_persist_batch_size", default=50
        )
        self.financial_detail_backfill_tdx_max_stocks_per_run: int = self._load_update_config(
            "financial_detail_backfill_tdx_max_stocks_per_run", default=10
        )
        self.financial_detail_backfill_missing_retry_days: int = self._load_update_config(
            "financial_detail_backfill_missing_retry_days", default=7
        )
        self.buyback_refresh_interval_days: int = self._load_update_config(
            "buyback_refresh_interval_days", default=7
        )
        # 价格批量抓取的并发网络请求数（HTTP 源；socket 源内部强制串行）
        self.price_fetch_concurrency: int = self._load_update_config(
            "price_fetch_concurrency", default=8
        )
        # 连续流水线中的在途股票任务数：慢股票只占用自己的槽位，
        # 不再像固定窗口那样拖住同窗口所有已完成股票。
        self.price_fetch_pipeline_depth: int = self._load_update_config(
            "price_fetch_pipeline_depth", default=64
        )
        # 批量持久化：每批合并到单个 DuckDB 事务，减少 WAL 提交次数
        self.price_fetch_batch_size: int = self._load_update_config(
            "price_fetch_batch_size", default=20
        )
        # 历史统计域全量重建的进程池并行数（只读分析；发布仍在主进程）。
        # 2026-08-28 提速计划：串行重建约需数十分钟，多进程可显著缩短。
        self.research_statistics_parallel_workers: int = self._load_update_config(
            "research_statistics_parallel_workers", default=12
        )
        # 财务三表刷新的并发股票数：网络请求并行，DuckDB 写事务仍由
        # 单写者锁串行，因此并发只缩短网络等待、不会双写。
        self.financial_refresh_concurrency: int = self._load_update_config(
            "financial_refresh_concurrency", default=8
        )
        # 混合批量写：并发抓取后每满 N 只合并为一个 DuckDB 事务。
        self.financial_refresh_batch_size: int = self._load_update_config(
            "financial_refresh_batch_size", default=20
        )
        self.price_fetch_timeout_seconds: int = self._load_update_config(
            "price_fetch_timeout_seconds", default=45
        )
        # 分红/除权批量抓取批大小：TDX 建连约 5s/次，逐股调用会让整步耗时
        # 数十分钟；同批共用一次 TDX 会话 + 一次事务落库（2026-10-20 提速）。
        self.market_actions_batch_size: int = self._load_update_config(
            "market_actions_batch_size", default=50
        )
        self.price_fetch_max_concurrency: int = self._load_update_config(
            "price_fetch_max_concurrency", default=16
        )
        self.price_fetch_concurrency_step: int = self._load_update_config(
            "price_fetch_concurrency_step", default=4
        )
        self.price_fetch_scale_up_seconds: int = self._load_update_config(
            "price_fetch_scale_up_seconds", default=300
        )
        # 源侧 qfq 发布滞后（reports/151 §1，语义与判据见 prices_step）：qfq 落后
        # 自己 raw ≥ lag_bars 根 = 真滞后（无条件重选）；只差 1 根且同一 (raw,qfq)
        # 水位本交易日已成功尝试 ≥ max_attempts 次 → 本轮跳过。硬缺口不受影响。
        self.price_qfq_lag_bars: int = self._load_update_config(
            "price_qfq_lag_bars", default=2
        )
        self.price_qfq_catchup_max_attempts: int = self._load_update_config(
            "price_qfq_catchup_max_attempts", default=2
        )
        # universe 步骤（股票池/上市状态）按日节流：全市场 stock_list 约
        # 51s + listing_info 约 53s，后台线程可接受但无需每轮重拉（默认 1 天）
        self.universe_refresh_interval_days: int = self._load_update_config(
            "universe_refresh_interval_days", default=1
        )

        # ─── 手工链自动接入（2026-11-14，依据 S5 D28/D29）───────────
        # 背景：7 条数据链路"功能完整、跑通过、有数据"，但唯一触发方式是人
        # 工敲命令，于是停在最后一次有人跑的那天（indicator_ext 停在
        # 2026-06-30 → 界面 55 个扩展筛选字段全部失效）。这里给每条链补上
        # 「开关 + 到期间隔 + 每轮上限」，默认值一律保守（宁可少跑不可拖长
        # 更新周期），失败只让本步骤 failed/partial，绝不阻断主链。
        self.cashflow_supplement_auto_enabled: bool = self._load_update_flag(
            "cashflow_supplement_auto_enabled", default=True
        )
        self.cashflow_supplement_refresh_interval_days: int = self._load_update_config(
            "cashflow_supplement_refresh_interval_days", default=7
        )
        self.cashflow_supplement_max_stocks_per_run: int = self._load_update_config(
            "cashflow_supplement_max_stocks_per_run", default=200
        )
        self.cashflow_supplement_concurrency: int = self._load_update_config(
            "cashflow_supplement_concurrency", default=4
        )
        self.cashflow_supplement_deadline_seconds: int = self._load_update_config(
            "cashflow_supplement_deadline_seconds", default=240
        )
        self.cashflow_supplement_missing_retry_days: int = self._load_update_config(
            "cashflow_supplement_missing_retry_days", default=7
        )
        self.balance_sheet_ext_auto_enabled: bool = self._load_update_flag(
            "balance_sheet_ext_auto_enabled", default=True
        )
        self.balance_sheet_ext_refresh_interval_days: int = self._load_update_config(
            "balance_sheet_ext_refresh_interval_days", default=7
        )
        self.balance_sheet_ext_max_stocks_per_run: int = self._load_update_config(
            "balance_sheet_ext_max_stocks_per_run", default=200
        )
        self.balance_sheet_ext_concurrency: int = self._load_update_config(
            "balance_sheet_ext_concurrency", default=4
        )
        self.balance_sheet_ext_deadline_seconds: int = self._load_update_config(
            "balance_sheet_ext_deadline_seconds", default=240
        )
        self.balance_sheet_ext_missing_retry_days: int = self._load_update_config(
            "balance_sheet_ext_missing_retry_days", default=7
        )
        self.deducted_profit_auto_enabled: bool = self._load_update_flag(
            "deducted_profit_auto_enabled", default=True
        )
        self.deducted_profit_refresh_interval_days: int = self._load_update_config(
            "deducted_profit_refresh_interval_days", default=7
        )
        self.deducted_profit_max_stocks_per_run: int = self._load_update_config(
            "deducted_profit_max_stocks_per_run", default=600
        )
        self.deducted_profit_concurrency: int = self._load_update_config(
            "deducted_profit_concurrency", default=6
        )
        self.deducted_profit_deadline_seconds: int = self._load_update_config(
            "deducted_profit_deadline_seconds", default=240
        )
        self.deducted_profit_missing_retry_days: int = self._load_update_config(
            "deducted_profit_missing_retry_days", default=7
        )
        # 固定资产净额补采（依据 S5 P1-3，2026Q3 主链只剩 1.8%）
        self.fixed_assets_auto_enabled: bool = self._load_update_flag(
            "fixed_assets_auto_enabled", default=True
        )
        self.fixed_assets_refresh_interval_days: int = self._load_update_config(
            "fixed_assets_refresh_interval_days", default=7
        )
        self.fixed_assets_max_stocks_per_run: int = self._load_update_config(
            "fixed_assets_max_stocks_per_run", default=1000
        )
        self.fixed_assets_concurrency: int = self._load_update_config(
            "fixed_assets_concurrency", default=8
        )
        self.fixed_assets_deadline_seconds: int = self._load_update_config(
            "fixed_assets_deadline_seconds", default=240
        )
        self.fixed_assets_missing_retry_days: int = self._load_update_config(
            "fixed_assets_missing_retry_days", default=7
        )
        self.regulatory_metrics_auto_enabled: bool = self._load_update_flag(
            "regulatory_metrics_auto_enabled", default=True
        )
        self.regulatory_metrics_refresh_interval_days: int = self._load_update_config(
            "regulatory_metrics_refresh_interval_days", default=7
        )
        self.regulatory_metrics_max_stocks_per_run: int = self._load_update_config(
            "regulatory_metrics_max_stocks_per_run", default=200
        )
        self.regulatory_metrics_deadline_seconds: int = self._load_update_config(
            "regulatory_metrics_deadline_seconds", default=180
        )
        self.indicator_ext_auto_enabled: bool = self._load_update_flag(
            "indicator_ext_auto_enabled", default=True
        )
        # 报告期推进/指纹变化是主判据；该间隔只是"输入未变也不要永久停更"的
        # 兜底（事件类列如送转/回购只能靠周期重建才更新）。
        self.indicator_ext_max_age_days: int = self._load_update_config(
            "indicator_ext_max_age_days", default=7
        )
        self.etf_prices_auto_enabled: bool = self._load_update_flag(
            "etf_prices_auto_enabled", default=True
        )
        self.etf_prices_refresh_interval_days: int = self._load_update_config(
            "etf_prices_refresh_interval_days", default=1
        )
        self.etf_prices_max_etfs_per_run: int = self._load_update_config(
            "etf_prices_max_etfs_per_run", default=20
        )
        self.etf_prices_deadline_seconds: int = self._load_update_config(
            "etf_prices_deadline_seconds", default=300
        )
        self.hk_dividends_auto_enabled: bool = self._load_update_flag(
            "hk_dividends_auto_enabled", default=True
        )
        self.hk_dividends_refresh_interval_days: int = self._load_update_config(
            "hk_dividends_refresh_interval_days", default=30
        )
        self.hk_dividends_max_stocks_per_run: int = self._load_update_config(
            "hk_dividends_max_stocks_per_run", default=60
        )
        self.hk_dividends_deadline_seconds: int = self._load_update_config(
            "hk_dividends_deadline_seconds", default=300
        )

    @staticmethod
    def _update_duckdb_budget() -> tuple[str, int]:
        """更新专用的 DuckDB 低内存预算（4GB/2线程，可配置）。"""
        try:
            from app.core.config import Config
            cfg = Config.current().get_value("update", {})
            budget = str(cfg.get("duckdb_memory_limit") or "4GB") if isinstance(cfg, dict) else "4GB"
            threads = int(cfg.get("duckdb_threads") or 2) if isinstance(cfg, dict) else 2
            return budget, max(1, threads)
        except Exception:
            return "4GB", 2

    @staticmethod
    def _load_update_config(key: str, *, default: int) -> int:
        try:
            from app.core.config import Config
            cfg = Config.current()
            update_cfg = cfg.get_value("update", {})
            value = update_cfg.get(key) if isinstance(update_cfg, dict) else None
            return int(value) if value else default
        except Exception:
            return default

    @staticmethod
    def _load_update_flag(key: str, *, default: bool) -> bool:
        """读取布尔型 update.* 开关（YAML true/false 或 "true"/"false" 均可）。"""
        try:
            from app.core.config import Config
            cfg = Config.current()
            update_cfg = cfg.get_value("update", {})
            value = update_cfg.get(key) if isinstance(update_cfg, dict) else None
        except Exception:
            return default
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def run_incremental_check(self, *, include_announcements: bool = True) -> dict[str, Any]:
        """执行增量检查，返回检查报告

        不自动执行更新——只检查并报告需要更新的内容。
        实际更新由 run_incremental_update() 执行。
        """
        report: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
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

        # 4. 缺失列表（契约 §3：可行动口径的单一真相，禁止用全表 COUNT
        #    把已解决的历史行也算进缺口——旧口径报 11,823，而状态页报 162）
        try:
            report["missing_count"] = actionable_missing_count(self.sqlite)
        except Exception as error:
            logger.warning("统计可行动缺失数量失败: %s", error)
            report["missing_count"] = None

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
                    # 自动更新是全库重计算，但只在本线程/子进程生命周期内
                    # 使用 update.duckdb_memory_limit 与 update.duckdb_threads。
                    # 更新进程退出后内存完整归还 OS。该覆盖只允许在专用子进程
                    # 使用，Web 进程内的连接必须保持 database.* 统一配置。
                    budget, threads = self._update_duckdb_budget()
                    with self.duck.memory_limit(
                        budget, threads=threads, preserve_insertion_order=False,
                    ):
                        return self._run_incremental_update_locked(max_stocks, progress_cb, detail_cb)
                finally:
                    close = getattr(self.adapter_mgr, "close", None)
                    if callable(close):
                        close()
                    import gc
                    gc.collect()
        except UpdateLockError:
            logger.warning("增量更新被跨进程锁拒绝：另一更新正在运行")
            return {
                "status": "skipped",
                "reason": "another_update_running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    def _reconcile_crashed_incremental_jobs(self) -> None:
        """Close jobs abandoned by the dead process that owned the update lock."""
        finished_at = datetime.now(UTC).isoformat()
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
        started_at = datetime.now(UTC).isoformat()
        job_row_id = self._start_job(job_id, max_stocks, started_at)
        try:
            report = self._run_incremental_update_flow(
                max_stocks, progress_cb, job_id, started_at, detail_cb
            )
        except Exception as error:
            self._finish_job(job_row_id, "failed", {"error": str(error)})
            raise
        self._finish_job(job_row_id, report["status"], report)
        # 2026-11-20（reports/151）：更新成功后重作物化「筛选快照」。
        # 写锁窗口里的筛选靠它做第二数据源，否则用户换个条件就必须 409。
        # 快照是**兜底手段**，失败只能记日志，绝不反过来阻断更新收尾。
        if report.get("status") in {"success", "partial"}:
            from app.core.storage import screening_snapshot

            screening_snapshot.build_screening_snapshot(
                self.duck, reason="update_finished",
            )
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
        """结束增量更新作业，保留 partial/skipped 语义供状态页展示。

        2026-08-28：partial 表示部分步骤降级但数据实际已推进，不能记成
        failed，否则状态页"最近一次更新执行"显示 failed、误导用户以为
        本轮更新无效。
        """
        finished_at = datetime.now(UTC).isoformat()
        persisted_status = (
            status if status in {"success", "partial", "skipped"} else "failed"
        )
        self.sqlite.execute(
            """UPDATE job_logs SET status = ?, finished_at = ?, details_json = ?
               WHERE id = ?""",
            [
                persisted_status,
                finished_at,
                json.dumps(details, ensure_ascii=False, default=str),
                job_row_id,
            ],
        )
        price_step = details.get("steps", {}).get("prices", {})
        if price_step.get("total"):
            value = {
                "rate_per_minute": price_step.get("rate_per_minute"),
                "elapsed_seconds": price_step.get("elapsed_seconds"),
                "total": price_step.get("total"),
                "success": price_step.get("success"),
                "failed": price_step.get("failed"),
                "finished_at": finished_at,
            }
            self.sqlite.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                  updated_at=excluded.updated_at""",
                ["price_update_last_rate", json.dumps(value, ensure_ascii=False), finished_at],
            )

    def _run_incremental_update_flow(
        self,
        max_stocks: int,
        progress_cb: Callable[[str, dict[str, Any]], None] | None,
        job_id: str,
        started_at: str,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """更新主流程（调用方已持有跨进程更新锁）——实现已搬到 ``update_parts/flow.py``。

        依据 S14（reports/140 §2.5 拆分的第 5 块，也是最后一块）：493 行的顺序编排
        整体搬到 ``app.core.update_parts.flow``，本方法只留同名委托——签名、返回值、
        异常、日志通道与**步骤顺序**一字不改。四个"只有一份实现"的私有依赖仍从本
        模块注入（同一个对象，不复制第二份实现）：

        * ``logger``：仍是本模块的 logger（日志通道 ``app.core.update`` 不变）；
        * ``announcement_step_result``：仍是本模块的 ``_announcement_step_result``；
        * ``merge_indicator_steps``：仍是本模块的 ``_merge_indicator_steps``
          （契约 §2 多子步骤状态归并的唯一实现）；
        * ``classify_announcement`` / ``announcement_dividend_category``：仍是本模块
          公告分类关键词的唯一实现。

        搬移是逐行平移：30 处 ``report_step(...)`` 的调用点与首次出现顺序、每个 SQL /
        日志文案字符串常量、每个 ``*..._pending_codes`` 展开形态全部对拍一致。
        ``tests/support/source_guard.py::resolve_body`` 会顺着这个委托壳追到
        ``flow.run_incremental_update_flow``，结构守卫仍指向真正的实现。
        """
        return flow.run_incremental_update_flow(
            self,
            max_stocks,
            progress_cb,
            job_id,
            started_at,
            detail_cb,
            logger=logger,
            announcement_step_result=_announcement_step_result,
            merge_indicator_steps=_merge_indicator_steps,
            classify_announcement=classify_announcement,
            announcement_dividend_category=_ANNOUNCEMENT_DIVIDEND_CATEGORY,
        )

    def _refresh_business_overview(
        self,
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """低频业务概览自动集成入口（最小安全）。

        仅当 update.business_overview_auto_enabled=true 且刷新间隔到期时
        才执行；否则返回 skipped，不产生任何网络请求。任何异常都被捕获，
        绝不让业务概览源失败阻断价格/财务增量更新。
        """
        try:
            from app.core.business import BusinessOverviewUpdater

            def progress(code: str, outcome: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                detail_cb("business_overview", {
                    "step": "business_overview",
                    "label": "业务概览",
                    "done": int(outcome.get("done") or 0),
                    "total": int(outcome.get("total") or 0),
                    "current": code,
                })

            return BusinessOverviewUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_if_due(progress_cb=progress)
        except Exception as error:
            logger.warning("业务概览刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_treasury_curve(self) -> dict[str, Any]:
        """低频国债曲线自动集成入口（最小安全）。

        每日检查当日曲线并补齐近 30 天缺失的关键期限；任何异常都被捕获，
        绝不让国债源失败阻断价格/财务/readiness 增量更新。

        返回 curve_changed，调用方据此重算所有含 div_yield_spread_* 的快照。
        """
        try:
            before = self._treasury_curve_fingerprint()
            from app.core.treasury import TreasuryCurveUpdater

            report = TreasuryCurveUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_if_due()
            after = self._treasury_curve_fingerprint()
            curve_changed = before != after
            return {**report, "curve_changed": curve_changed}
        except Exception as error:
            logger.warning("国债曲线刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _treasury_curve_fingerprint(self) -> str:
        """Cheap content fingerprint of the whole treasury curve table.

        收益率利差快照可能引用任意历史日期的曲线点（停牌/价格滞后股票尤其
        明显），所以不能只比较行数与最大日期；任意已有日期的收益率被
        upsert 修正也必须触发 div_yield_spread_* 重算。
        """
        try:
            rows = self.duck.read_query(
                """SELECT COALESCE(md5(string_agg(
                       CAST(curve_date AS VARCHAR) || ':' ||
                       CAST(tenor_years AS VARCHAR) || ':' ||
                       CAST(yield_pct AS VARCHAR),
                       '|' ORDER BY curve_date, tenor_years
                   )), '') AS fp
                   FROM treasury_yield_curve"""
            )
            return str(rows[0]["fp"]) if rows else ""
        except Exception as error:
            logger.warning("计算国债曲线指纹失败: %s", error)
            return ""

    def _refresh_capital_history(
        self,
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """历史股本链有界续传（P4）：每轮最多 20 只上市股票，按代码序。

        自选优先；失败保留旧值 + retry/missing；绝不阻断价格/财务/readiness。
        """
        try:
            from app.core.capital import CapitalHistoryUpdater

            def progress(code: str, outcome: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                detail_cb("capital_history", {
                    "step": "capital_history",
                    "label": "历史股本",
                    "done": int(outcome.get("done") or 0),
                    "total": int(outcome.get("total") or 0),
                    "current": code,
                })

            return CapitalHistoryUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).update_all(max_stocks=20, progress_cb=progress)
        except Exception as error:
            logger.warning("历史股本链刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_research_statistics(
        self,
        *,
        parallel_workers: int = 1,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """历史统计域重建（P4）：仅当股本链或价格/财务/曲线输入变化时执行。

        以输入指纹为判据避免每轮全量重建；任何异常均不阻断其他步骤。
        """
        try:
            from app.core.statistics import StatisticsBuilder
            builder = StatisticsBuilder(duck=self.duck, sqlite=self.sqlite)
            fingerprint = builder._input_fingerprint()
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = 'research_statistics_fingerprint'"
            )
            if rows and rows[0].get("value") == fingerprint:
                return {"status": "skipped", "reason": "fingerprint_unchanged"}

            def stats_progress(code: str, outcome: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                detail_cb("research_statistics", {
                    "step": "research_statistics",
                    "label": "历史统计",
                    "done": int(outcome.get("done") or 0),
                    "total": int(outcome.get("total") or 0),
                    "current": code,
                })

            try:
                report = builder.rebuild_incremental(
                    parallel=parallel_workers,
                    progress_cb=stats_progress,
                )
            except (OSError, PermissionError) as error:
                # 受限启动环境（工具型进程树/临时目录 ACL）里 multiprocessing
                # spawn 会以 WinError 5 失败；串行重建不创建子进程，作为
                # 同语义降级重试一次，避免统计域每轮 failed。
                logger.warning(
                    "统计域并行重建失败，降级串行重试: %s", error,
                )
                report = builder.rebuild_incremental(
                    parallel=0,
                    progress_cb=stats_progress,
                )
            # P4-10 修复（依据 S3）：仅全部成功才持久化指纹；
            # partial（部分股票失败）不落指纹，下一轮自动重试失败股。
            if report["status"] == "success":
                with self.sqlite.transaction() as conn:
                    conn.execute(
                        """INSERT INTO data_refresh_state (key, value, updated_at)
                           VALUES ('research_statistics_fingerprint', ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                             value=excluded.value, updated_at=excluded.updated_at""",
                        [fingerprint, datetime.now(UTC).isoformat()],
                    )
            return report
        except Exception as error:
            logger.warning("历史统计域重建失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_funding(self) -> dict[str, Any]:
        """融资事件低频域有界续传（数据补全 2026-08-25）。

        每轮最多 update.funding_max_stocks_per_run（默认 100）只未覆盖上市股票，
        批 50 + 冷却 30s（东财 F10 安全组合）；失败保留旧值 + retry/missing，
        绝不阻断价格/财务/readiness。历史事件一次补齐后日常仅新股增量。

        返回 changed_codes 供调用方重算 cumulative_financing_amount /
        dividend_financing_ratio_pct 快照。
        """
        try:
            # 与 IndicatorCalculator._get_cumulative_financing_amount 完全同口径：
            # COALESCE(raise_funds, raise_funds_net, issue_price * issue_shares)。
            # 旧实现只看 SUM(raise_funds)，会把 IPO（raise_funds 几乎恒为 NULL、
            # 金额只存在 raise_funds_net）的融资变化全部漏检。
            def funding_amounts() -> dict[str, float]:
                rows = self.duck.read_query(
                    """SELECT stock_code,
                              COALESCE(SUM(COALESCE(
                                  raise_funds, raise_funds_net,
                                  issue_price * issue_shares
                              )), 0) AS amount
                       FROM funding_events
                       WHERE event_type IN ('ipo', 'a_placement', 'rights')
                       GROUP BY stock_code"""
                )
                return {
                    str(row["stock_code"]): float(row["amount"] or 0.0)
                    for row in rows
                }

            before_by_code = funding_amounts()
            from app.core.funding import FundingUpdater

            report = FundingUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_if_due()
            after_by_code = funding_amounts()
            changed_codes = sorted({
                code for code in set(before_by_code) | set(after_by_code)
                if before_by_code.get(code) != after_by_code.get(code)
            })
            return {**report, "changed_codes": changed_codes}
        except Exception as error:
            logger.warning("融资事件刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_index_valuation(self) -> dict[str, Any]:
        """指数估值低频域每日更新（数据补全 2026-08-25）。

        每日最多 1 次（UTC+8 节流，避免频繁重启重复请求第三方源）；
        主源失败保留旧值并登记 retry，绝不阻断价格/财务/readiness。
        """
        try:
            from app.core.index_valuation import IndexValuationUpdater
            return IndexValuationUpdater(
                duck=self.duck, sqlite=self.sqlite,
            ).refresh_if_due()
        except Exception as error:
            logger.warning("指数估值刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    def _refresh_analyst_forecast(self) -> dict[str, Any]:
        """卖方一致预期每日快照（schema v48，依据 S11 §2.2 #13）。

        唯一实现是 `app/core/analyst_forecast.py::refresh_daily()`：本方法只负责
        「拿水位读写函数 → 调用 → 异常降级」，不重复任何抓取/写库口径。
        水位 = `data_refresh_state[analyst_forecast_last_refresh]` 的北京日期，
        因此一天最多抓 1 次（重启/重跑都不会重复请求）；失败只让本步骤 failed，
        不影响价格/财务/快照/readiness。
        """
        try:
            from app.core.analyst_forecast import STATE_KEY, refresh_daily

            return refresh_daily(
                duck=self.duck,
                state_key=STATE_KEY,
                read_state=self._read_refresh_state,
                write_state=self._write_refresh_state,
            )
        except Exception as error:  # noqa: BLE001 - 本步骤失败不得炸整轮更新
            logger.warning("一致预期快照失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    # ─── 手工链自动接入（2026-11-14，依据 S5 D28/D29）────────────────────
    #
    # 下面 7 个步骤对应审计报告 119 里"功能完整、跑通过、有数据，但没接进每天
    # 自动跑的主流程"的链路（PDF 域不在本次范围）。统一约定：
    #   1) 开关 update.<domain>_auto_enabled=false 时一律 skipped，无网络请求；
    #   2) 用 data_refresh_state 记水位，`_refresh_due(key, interval_days)` 判到期；
    #   3) 每步都有 max_stocks/截止时间上限，日志输出 counts + elapsed_seconds；
    #   4) 任何异常只让本步骤 failed/partial，绝不让整轮更新失败，也不影响
    #      readiness（readiness 只看数据日期，不看 job 步骤状态）。

    _CASHFLOW_SUPPLEMENT_STATE_KEY = "cashflow_supplement_last_refresh"
    _BALANCE_SHEET_EXT_STATE_KEY = "balance_sheet_ext_last_refresh"
    _DEDUCTED_PROFIT_STATE_KEY = "deducted_profit_last_refresh"
    _FIXED_ASSETS_STATE_KEY = "fixed_assets_supplement_last_refresh"
    _REGULATORY_METRICS_STATE_KEY = "regulatory_metrics_last_refresh"
    _INDICATOR_EXT_STATE_KEY = "indicator_ext_last_refresh"
    _ETF_PRICES_STATE_KEY = "etf_prices_last_refresh"
    _HK_DIVIDENDS_STATE_KEY = "hk_dividends_last_refresh"

    #: 同一水位的第二个键名：`*_last_refresh` 是本文件历史命名
    #: （buyback/csrc 同款）；`*_last_success_at` 是 依据 S5 D29 与
    #: `app/core/quality/asset_health.py::MANUAL_CHAIN_STALE` 约定的键名。
    #: 两个都写，保证"水位/停摆探针"两套读取方都能看到同一事实。
    _DOMAIN_SUCCESS_ALIASES: dict[str, str] = {
        _CASHFLOW_SUPPLEMENT_STATE_KEY: "cashflow_supplement_last_success_at",
        _BALANCE_SHEET_EXT_STATE_KEY: "balance_sheet_ext_last_success_at",
        _DEDUCTED_PROFIT_STATE_KEY: "deducted_profit_last_success_at",
        _FIXED_ASSETS_STATE_KEY: "fixed_assets_supplement_last_success_at",
        _REGULATORY_METRICS_STATE_KEY: "regulatory_metrics_last_success_at",
        _INDICATOR_EXT_STATE_KEY: "indicator_ext_last_success_at",
        _ETF_PRICES_STATE_KEY: "etf_prices_last_success_at",
        _HK_DIVIDENDS_STATE_KEY: "hk_dividends_last_success_at",
    }

    def _mark_domain_refreshed(self, state_key: str) -> None:
        """写一个低频域的成功水位（同时写 `_last_refresh` 与 `_last_success_at`）。

        两键同事务写入，避免出现"一个键写了、另一个没写"的半状态让
        健康探针与到期判断各说各话。
        """
        now = datetime.now(UTC).isoformat()
        keys = [state_key]
        alias = self._DOMAIN_SUCCESS_ALIASES.get(state_key)
        if alias:
            keys.append(alias)
        try:
            with self.sqlite.transaction() as conn:
                for key in keys:
                    conn.execute(
                        """INSERT INTO data_refresh_state (key, value, updated_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                             value = excluded.value, updated_at = excluded.updated_at""",
                        [key, now, now],
                    )
        except Exception as error:  # noqa: BLE001 - 水位写失败不得炸掉步骤
            logger.warning("写入域水位失败(%s): %s", state_key, error)

    def _read_refresh_state(self, key: str) -> str | None:
        """读取 data_refresh_state 的原始值（指纹等非时间戳用途）。"""
        try:
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = ?", [key],
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("读取刷新状态失败(%s): %s", key, error)
            return None
        return str(rows[0].get("value")) if rows else None

    def _write_refresh_state(self, key: str, value: str) -> None:
        """写入 data_refresh_state（与 `_mark_refreshed` 同表，用途不同）。"""
        now = datetime.now(UTC).isoformat()
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value = excluded.value, updated_at = excluded.updated_at""",
                    [key, value, now],
                )
        except Exception as error:  # noqa: BLE001 - 状态写入失败不得炸掉步骤
            logger.warning("写入刷新状态失败(%s): %s", key, error)

    @staticmethod
    def _new_domain_step(interval_days: int, *, max_stocks: int = 0) -> dict[str, Any]:
        """低频域步骤的骨架结果（字段与既有域步骤对齐）。"""
        return {
            "status": "skipped",
            "reason": None,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "elapsed_seconds": 0.0,
            "interval_days": interval_days,
            "max_stocks_per_run": max_stocks,
        }

    def _refresh_supplement_dataset(
        self,
        dataset: str,
        *,
        state_key: str,
        interval_days: int,
        enabled: bool,
        max_stocks: int,
        concurrency: int,
        deadline_seconds: int,
        retry_days: int,
    ) -> dict[str, Any]:
        """补采域通用步骤（折旧摊销/资本支出、资产负债表补充科目、扣非净利润）。

        到期才跑；每轮上限 = `max_stocks_per_run` + `deadline_seconds`；
        只有成功且无残留失败才推进水位，失败/部分失败下一轮继续。
        """
        started = time.time()
        step = self._new_domain_step(interval_days, max_stocks=max_stocks)
        step["dataset"] = dataset
        if not enabled:
            step["reason"] = "disabled_by_config"
            return step
        if not self._refresh_due(state_key, interval_days):
            step["reason"] = "refreshed_within_interval"
            return step
        try:
            from app.core.supplement import SupplementFetcher

            report = SupplementFetcher(
                duck=self.duck, sqlite=self.sqlite,
            ).refresh(
                dataset,
                max_stocks=max_stocks,
                concurrency=concurrency,
                deadline_seconds=deadline_seconds,
                retry_days=retry_days,
            )
            step.update({
                "status": report.get("status", "failed"),
                "reason": report.get("reason"),
                "targeted": report.get("targeted", 0),
                "updated": report.get("updated", 0),
                "skipped": report.get("skipped", 0),
                "failed": report.get("failed", 0),
                "deferred": report.get("deferred", 0),
                "rows_written": report.get("rows_written", {}),
                "periods": report.get("periods", []),
                "failed_codes": report.get("failed_codes", []),
                "remaining_pending": report.get("remaining_pending", 0),
            })
            # 只有"这一域已经追平"才推进水位：
            #   · success + remaining_pending=0 → 全部目标期都已补齐；
            #   · no_pending_codes → 本来就没得补（**不含**"都在失败冷却里"）。
            # 若还有残留（首次 Q3 追补就是这种：全市场 5,575 只一次跑不完），
            # 不推进会 7 天白等，续传进度就会卡死在第一轮。
            #
            # 2026-11-16（依据 S8）：`all_pending_in_cooldown` **不算追平**。
            # 旧实现把"待补全在冷却"也报成 `no_pending_codes` 并推进水位，
            # 于是"水位前进、待补数不降"（balance_sheet_ext 105 只）永远不收敛，
            # 体检的 CHAIN_PROGRESS_STALLED 只能一直红着。
            caught_up = (
                step["status"] == "success" and not step.get("remaining_pending")
            )
            if caught_up or step.get("reason") == "no_pending_codes":
                self._mark_domain_refreshed(state_key)
        except Exception as error:  # noqa: BLE001 - 补采失败绝不阻断主链
            logger.warning("补采步骤失败(非致命) dataset=%s: %s", dataset, error)
            step["status"] = "failed"
            step["reason"] = f"exception: {type(error).__name__}: {error}"
        finally:
            step["elapsed_seconds"] = round(time.time() - started, 2)
        logger.info(
            "补采步骤 %s: status=%s targeted=%s updated=%s failed=%s deferred=%s "
            "remaining=%s elapsed_seconds=%s",
            dataset, step["status"], step.get("targeted", 0), step["updated"],
            step["failed"], step.get("deferred", 0),
            step.get("remaining_pending", 0), step["elapsed_seconds"],
        )
        return step

    def _refresh_cashflow_supplement(self) -> dict[str, Any]:
        """折旧摊销/资本支出补采（依据 S5 D29 ②，停在 2026-06-30）。

        根因**不是没人跑，而是续传判据写错**：旧脚本用"本表有任意
        eastmoney_f10 行就跳过"，第一次跑完后再也补不到新报告期。判据已下沉
        到 `app/core/supplement.py`（本源最新报告期 < 目标最新期即待补）。
        """
        return self._refresh_supplement_dataset(
            "cash_flow_supplement",
            state_key=self._CASHFLOW_SUPPLEMENT_STATE_KEY,
            interval_days=self.cashflow_supplement_refresh_interval_days,
            enabled=self.cashflow_supplement_auto_enabled,
            max_stocks=self.cashflow_supplement_max_stocks_per_run,
            concurrency=self.cashflow_supplement_concurrency,
            deadline_seconds=self.cashflow_supplement_deadline_seconds,
            retry_days=self.cashflow_supplement_missing_retry_days,
        )

    def _refresh_balance_sheet_ext(self) -> dict[str, Any]:
        """资产负债表补充科目（其他应付款 / 一年内到期非流动负债 / 其他应收款）。

        三个科目各自解锁 indicator_ext 里无法自算的一项能力（大股东占款、
        有息负债口径、应收应付同源配对）；与折旧摊销同一模式、同一个续传 bug。
        """
        return self._refresh_supplement_dataset(
            "balance_sheet_ext",
            state_key=self._BALANCE_SHEET_EXT_STATE_KEY,
            interval_days=self.balance_sheet_ext_refresh_interval_days,
            enabled=self.balance_sheet_ext_auto_enabled,
            max_stocks=self.balance_sheet_ext_max_stocks_per_run,
            concurrency=self.balance_sheet_ext_concurrency,
            deadline_seconds=self.balance_sheet_ext_deadline_seconds,
            retry_days=self.balance_sheet_ext_missing_retry_days,
        )

    def _refresh_fixed_assets(self) -> dict[str, Any]:
        """固定资产净额补采（依据 S5 P1-3）。

        2026Q3 主链 `balance_sheet.fixed_assets` 只剩 1.8%（上期 99.9%）：
        新浪 Q3 响应里没有「固定资产净额」这一项。东财 F10 资产负债表有该字段，
        此处**只回填 NULL**，既有值一律不动。
        """
        return self._refresh_supplement_dataset(
            "fixed_assets_supplement",
            state_key=self._FIXED_ASSETS_STATE_KEY,
            interval_days=self.fixed_assets_refresh_interval_days,
            enabled=self.fixed_assets_auto_enabled,
            max_stocks=self.fixed_assets_max_stocks_per_run,
            concurrency=self.fixed_assets_concurrency,
            deadline_seconds=self.fixed_assets_deadline_seconds,
            retry_days=self.fixed_assets_missing_retry_days,
        )

    def _refresh_deducted_profit(self) -> dict[str, Any]:
        """扣非净利润补采（依据 S5 D29 ③，2026-06 起基本没数）。

        只 UPDATE `income_statement.deducted_net_profit` 的 NULL 值：既有
        CSMAR/主链值保持不动；源字段缺失时结果是"没更新"而不会写坏三表。
        """
        return self._refresh_supplement_dataset(
            "deducted_profit",
            state_key=self._DEDUCTED_PROFIT_STATE_KEY,
            interval_days=self.deducted_profit_refresh_interval_days,
            enabled=self.deducted_profit_auto_enabled,
            max_stocks=self.deducted_profit_max_stocks_per_run,
            concurrency=self.deducted_profit_concurrency,
            deadline_seconds=self.deducted_profit_deadline_seconds,
            retry_days=self.deducted_profit_missing_retry_days,
        )

    def _refresh_regulatory_metrics(self) -> dict[str, Any]:
        """6 个金融监管指标补采（依据 S5 D29 ④，只到 2026-06-30）。

        该域**每次到期全量重抓**（每只金融股 1 次请求、只取最新一期），没有
        "有没有本源行"的续传判据，所以不存在上面那个判据 bug；接入调度只需
        补上到期判断 + 每轮上限（默认 200 只，覆盖银行+券商全量）。
        """
        started = time.time()
        step = self._new_domain_step(
            self.regulatory_metrics_refresh_interval_days,
            max_stocks=self.regulatory_metrics_max_stocks_per_run,
        )
        if not self.regulatory_metrics_auto_enabled:
            step["reason"] = "disabled_by_config"
            return step
        if not self._refresh_due(
            self._REGULATORY_METRICS_STATE_KEY,
            self.regulatory_metrics_refresh_interval_days,
        ):
            step["reason"] = "refreshed_within_interval"
            return step
        try:
            from app.core.supplement import RegulatoryMetricsUpdater

            report = RegulatoryMetricsUpdater(duck=self.duck).refresh(
                max_stocks=self.regulatory_metrics_max_stocks_per_run,
                deadline_seconds=self.regulatory_metrics_deadline_seconds,
            )
            step.update({
                "status": report.get("status", "failed"),
                "reason": report.get("reason"),
                "targeted": report.get("targeted", 0),
                "updated": report.get("updated", 0),
                "skipped": report.get("skipped", 0),
                "failed": report.get("failed", 0),
                "deferred": report.get("deferred", 0),
                "rows_updated": report.get("rows_updated", 0),
                "coverage_rows": report.get("coverage_rows", 0),
                "failed_codes": report.get("failed_codes", []),
            })
            if step["status"] in {"success", "skipped"}:
                self._mark_domain_refreshed(self._REGULATORY_METRICS_STATE_KEY)
        except Exception as error:  # noqa: BLE001
            logger.warning("监管指标补采失败(非致命): %s", error)
            step["status"] = "failed"
            step["reason"] = f"exception: {type(error).__name__}: {error}"
        finally:
            step["elapsed_seconds"] = round(time.time() - started, 2)
        logger.info(
            "监管指标步骤: status=%s targeted=%s updated=%s failed=%s elapsed_seconds=%s",
            step["status"], step.get("targeted", 0), step["updated"],
            step["failed"], step["elapsed_seconds"],
        )
        return step

    def _refresh_roce(self) -> dict[str, Any]:
        """ROCE 域重建（依据 S9/S10）。

        门控：`roce_history` 的最新年报期 < 三表最新年报期，或 `roce_history` 为空
        （从未构建）时重建。ROCE 只吃年报，季报更新不触发，一年最多重建一次。

        为什么必须有这一步：`indicator_snapshot.roce_*` 五列由 `roce_history`
        聚合而来（聚合发生在 `calculator._publish_snapshot` 里），
        若 `roce_history` 冻结在旧年份，聚合出来的就是旧值——D28 同型。

        失败只让本步骤 failed，`roce_history` 保留旧值，主链与 readiness 不受影响。
        """
        started = time.time()
        step = self._new_domain_step(400)
        try:
            from app.core.indicators.roce import RoceBuilder

            latest_annual = self.duck.read_query(
                "SELECT MAX(report_date) AS d FROM balance_sheet "
                "WHERE strftime(report_date, '%m-%d') = '12-31'"
            )
            latest_annual = latest_annual[0]["d"] if latest_annual else None
            built = None
            try:
                rows = self.duck.read_query(
                    "SELECT MAX(report_date) AS d FROM roce_history"
                )
                built = rows[0]["d"] if rows else None
            except Exception:  # noqa: BLE001 - 表不存在等价于「从未构建」
                built = None
            if built is not None and latest_annual is not None and built >= latest_annual:
                step["reason"] = "up_to_date"
                step["roce_history_max"] = str(built)
                return step
            report = RoceBuilder(duck=self.duck).build()
            step["status"] = "success"
            step["roce_history_rows"] = report.get("history_rows")
            step["roce_5y_median"] = report.get("snapshot", {}).get("roce_5y_median")
            step["roce_history_max"] = str(latest_annual)
        except Exception as error:  # noqa: BLE001
            step["status"] = "failed"
            step["reason"] = f"exception: {type(error).__name__}: {error}"
            logger.warning("ROCE 域重建失败", exc_info=True)
        finally:
            step["elapsed_seconds"] = round(time.time() - started, 2)
        logger.info(
            "ROCE 步骤: status=%s reason=%s rows=%s elapsed=%ss",
            step["status"], step.get("reason"), step.get("roce_history_rows"),
            step["elapsed_seconds"],
        )
        return step

    def _refresh_indicator_ext(self) -> dict[str, Any]:
        """扩展指标域 indicator_ext 重建（依据 S5 D28，P0）。

        门控（两条硬规则 + 一条兜底）：
        1. `max(indicator_ext.report_date) < 最新完整三表报告期` → **必须**重建；
        2. 否则只在输入指纹变化时重建（三表/补采表最新报告期或行数变化）；
        3. 输入长期未变时按 `indicator_ext_max_age_days` 兜底重建一次
           （送转/回购等事件类列不参与指纹，只能靠周期刷新）。
        失败只让本步骤 failed，`indicator_ext` 保留旧值，主链快照与
        readiness 完全不受影响。
        """
        started = time.time()
        step = self._new_domain_step(self.indicator_ext_max_age_days)
        if not self.indicator_ext_auto_enabled:
            step["reason"] = "disabled_by_config"
            return step
        try:
            from app.core.indicators.extended import (
                FINGERPRINT_STATE_KEY,
                ExtendedIndicatorBuilder,
                rebuild_decision,
            )

            decision = rebuild_decision(
                self.duck,
                stored_fingerprint=self._read_refresh_state(FINGERPRINT_STATE_KEY),
            )
            step.update({
                "ext_max_period": decision.get("ext_max_period"),
                "complete_trio_period": decision.get("complete_period"),
                "ext_rows": decision.get("ext_rows", 0),
                # 2026-11-17（依据 S10 P0-9）：指纹新增 `share_capital_history` 段后，
                # 归因能分清"全量重建"与"只有股本变了"。后者只重建受影响的少数股票
                # （689009 的 unit_rescale:10 就是这一类），避免 36 万行整表重写。
                "ext_scope": decision.get("scope"),
                "ext_changed_inputs": decision.get("changed_inputs", []),
                "ext_force_codes": decision.get("force_codes", []),
            })
            if not decision["required"] and not self._refresh_due(
                self._INDICATOR_EXT_STATE_KEY, self.indicator_ext_max_age_days,
            ):
                step["reason"] = "up_to_date"
                step["skipped"] = 1
                return step
            build_reason = (
                decision["reason"] if decision["required"] else "max_age_exceeded"
            )
            force_codes = decision.get("force_codes") or []
            report = ExtendedIndicatorBuilder(duck=self.duck, paths=None).build(
                force_codes=force_codes if decision.get("scope") == "codes" else None,
            )
            step.update({
                "status": "success" if report.get("status") == "success" else "failed",
                "reason": build_reason,
                "updated": int(report.get("rows_after", 0)),
                "rows_written": report.get("rows_written", 0),
                "filled": report.get("filled", {}),
                "report_date_range": report.get("report_date_range", []),
            })
            if report.get("status") == "success":
                self._mark_domain_refreshed(self._INDICATOR_EXT_STATE_KEY)
                # 指纹按"重建后"的输入重新采集并落库，否则输入在重建期间又
                # 变化时下一轮会重复全量重建。
                refreshed = rebuild_decision(self.duck)
                self._write_refresh_state(
                    FINGERPRINT_STATE_KEY, str(refreshed.get("fingerprint") or ""),
                )
        except Exception as error:  # noqa: BLE001 - 派生域失败绝不阻断主链
            logger.warning("扩展指标重建失败(非致命): %s", error)
            step["status"] = "failed"
            step["reason"] = f"exception: {type(error).__name__}: {error}"
        finally:
            step["elapsed_seconds"] = round(time.time() - started, 2)
        logger.info(
            "indicator_ext 步骤: status=%s reason=%s rows=%s elapsed_seconds=%s",
            step["status"], step.get("reason"), step.get("updated", 0),
            step["elapsed_seconds"],
        )
        return step

    def _refresh_etf_prices(self) -> dict[str, Any]:
        """ETF 行情（依据 S5 D29 ⑥，停在 2026-09-18）。

        此前 ETF 只在 `retry` 分支被碰到：没有 retry 记录就永远不刷新。
        现在按日到期 + 每轮上限 + 截止时间，只刷新 `etf_meta` 里登记的代码
        （不做全市场扫描），并按"本地行情最旧优先"推进，避免永远只刷前几只。
        """
        started = time.time()
        step = self._new_domain_step(
            self.etf_prices_refresh_interval_days,
            max_stocks=self.etf_prices_max_etfs_per_run,
        )
        if not self.etf_prices_auto_enabled:
            step["reason"] = "disabled_by_config"
            return step
        if not self._refresh_due(
            self._ETF_PRICES_STATE_KEY, self.etf_prices_refresh_interval_days,
        ):
            step["reason"] = "refreshed_within_interval"
            return step
        try:
            from app.core.etf_prices import EtfPriceUpdater

            report = EtfPriceUpdater(
                duck=self.duck, sqlite=self.sqlite,
            ).update_all(
                max_etfs=self.etf_prices_max_etfs_per_run,
                deadline_seconds=self.etf_prices_deadline_seconds,
            )
            summary = report.get("summary", {})
            step.update({
                "status": report.get("status", "failed"),
                "reason": report.get("reason"),
                "targeted": report.get("targeted", 0),
                "updated": int(summary.get("success", 0)),
                "skipped": int(summary.get("missing", 0)),
                "failed": int(summary.get("failed", 0)),
                "deferred": report.get("deferred", 0),
            })
            if step["status"] == "success":
                self._mark_domain_refreshed(self._ETF_PRICES_STATE_KEY)
        except Exception as error:  # noqa: BLE001
            logger.warning("ETF 行情刷新失败(非致命): %s", error)
            step["status"] = "failed"
            step["reason"] = f"exception: {type(error).__name__}: {error}"
        finally:
            step["elapsed_seconds"] = round(time.time() - started, 2)
        logger.info(
            "ETF 行情步骤: status=%s targeted=%s updated=%s failed=%s deferred=%s "
            "elapsed_seconds=%s",
            step["status"], step.get("targeted", 0), step["updated"],
            step["failed"], step.get("deferred", 0), step["elapsed_seconds"],
        )
        return step

    def _refresh_hk_dividends(self) -> dict[str, Any]:
        """港股分红（依据 S5 D29 ⑤，2026-09-04 导入后没再跑）。

        只刷新映射表里的 A+H 股票（约 179 只），按"最久未刷新优先"分批，
        每批 20 只 + 总截止时间有界；源侧限速 ≤2 req/s，失败保留旧值并进入
        retry/missing，不影响 A 股 readiness。
        """
        started = time.time()
        step = self._new_domain_step(
            self.hk_dividends_refresh_interval_days,
            max_stocks=self.hk_dividends_max_stocks_per_run,
        )
        if not self.hk_dividends_auto_enabled:
            step["reason"] = "disabled_by_config"
            return step
        if not self._refresh_due(
            self._HK_DIVIDENDS_STATE_KEY, self.hk_dividends_refresh_interval_days,
        ):
            step["reason"] = "refreshed_within_interval"
            return step
        try:
            from app.core.hk_dividends import HKDividendUpdater

            updater = HKDividendUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            )
            mapping = updater.resolve_mapping().get("mapping", {})
            targets = self._order_hk_dividend_targets(mapping)
            if not targets:
                step["status"] = "skipped"
                step["reason"] = "no_ah_hk_mapping"
                return step
            targets = targets[: self.hk_dividends_max_stocks_per_run]
            step["targeted"] = len(targets)
            succeeded = failed = unmapped = deferred = 0
            for start in range(0, len(targets), 20):
                if (time.time() - started) >= self.hk_dividends_deadline_seconds:
                    deferred = len(targets) - start
                    break
                report = updater.update_many(targets[start:start + 20], batch_size=20)
                succeeded += int(report.get("succeeded", 0))
                failed += int(report.get("failed", 0))
                unmapped += int(report.get("unmapped", 0))
            step.update({
                "status": (
                    "success" if failed == 0 and unmapped == 0 and deferred == 0
                    else "partial"
                ),
                "updated": succeeded,
                "failed": failed,
                "skipped": unmapped,
                "deferred": deferred,
                "reason": None if failed == 0 else "partial_source_failures",
            })
            if step["status"] == "success":
                self._mark_domain_refreshed(self._HK_DIVIDENDS_STATE_KEY)
        except Exception as error:  # noqa: BLE001
            logger.warning("港股分红刷新失败(非致命): %s", error)
            step["status"] = "failed"
            step["reason"] = f"exception: {type(error).__name__}: {error}"
        finally:
            step["elapsed_seconds"] = round(time.time() - started, 2)
        logger.info(
            "港股分红步骤: status=%s targeted=%s updated=%s failed=%s deferred=%s "
            "elapsed_seconds=%s",
            step["status"], step.get("targeted", 0), step["updated"],
            step["failed"], step.get("deferred", 0), step["elapsed_seconds"],
        )
        return step

    def _order_hk_dividend_targets(self, mapping: dict[str, Any]) -> list[str]:
        """映射内 A 股代码按"港股分红最久未刷新优先"排序。"""
        codes = sorted(str(code) for code in mapping)
        if not codes:
            return []
        try:
            rows = self.duck.read_query(
                """SELECT stock_code, MAX(fetch_time) AS last_fetch
                   FROM hk_dividends GROUP BY stock_code"""
            )
            last_fetch = {
                str(row["stock_code"]).zfill(5): str(row["last_fetch"])
                for row in rows if row.get("last_fetch") is not None
            }
        except Exception as error:  # noqa: BLE001 - 排序失败退化为代码序
            logger.debug("港股分红陈旧度排序失败，退化为代码序: %s", error)
            return codes

        def _sort_key(code: str) -> tuple[bool, str, str]:
            # hk_dividends.stock_code 存 5 位港股代码，映射里才是 A 股代码。
            hk_code = str(mapping[code].get("hk_code", "")).zfill(5)
            return (hk_code in last_fetch, last_fetch.get(hk_code, ""), code)

        return sorted(codes, key=_sort_key)

    def _stale_price_snapshot_codes(self) -> list[str]:
        """Listed stocks whose snapshot price date is behind raw price."""
        try:
            rows = self.duck.read_query(
                """SELECT snap.stock_code
                   FROM indicator_snapshot snap
                   JOIN stock_meta m ON m.stock_code = snap.stock_code
                   JOIN (
                       SELECT stock_code, MAX(trade_date) AS latest
                       FROM price_daily_raw GROUP BY stock_code
                   ) raw ON raw.stock_code = snap.stock_code
                   WHERE m.is_listed IS TRUE
                     AND snap.latest_price_date != raw.latest
                   ORDER BY snap.stock_code"""
            )
            return [row["stock_code"] for row in rows]
        except Exception as error:
            logger.warning("查询价格过期指标快照失败: %s", error)
            return []

    def _stale_financial_snapshot_codes(self) -> list[str]:
        """Listed stocks whose snapshot report_date is behind complete financials.

        财报中报季 catch-up 可能 partial：已成功写入最新三表的股票即使
        价格日期没变，也必须重算快照，否则市值/TTM 仍停留在旧报告期。
        """
        try:
            rows = self.duck.read_query(
                """WITH complete_financials AS (
                       SELECT bs.stock_code, MAX(bs.report_date) AS latest
                       FROM balance_sheet bs
                       JOIN income_statement ic
                         ON ic.stock_code = bs.stock_code
                         AND ic.report_date = bs.report_date
                       JOIN cash_flow cf
                         ON cf.stock_code = bs.stock_code
                         AND cf.report_date = bs.report_date
                       WHERE bs.total_assets IS NOT NULL
                         AND bs.total_liabilities IS NOT NULL
                         AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
                         AND ic.revenue IS NOT NULL
                         AND ic.parent_net_profit IS NOT NULL
                         AND cf.cf_from_operating IS NOT NULL
                       GROUP BY bs.stock_code
                   )
                   SELECT snap.stock_code
                   FROM indicator_snapshot snap
                   JOIN complete_financials fin
                     ON fin.stock_code = snap.stock_code
                   JOIN stock_meta m ON m.stock_code = snap.stock_code
                   WHERE m.is_listed IS TRUE
                     AND fin.latest > snap.report_date
                   ORDER BY snap.stock_code"""
            )
            return [row["stock_code"] for row in rows]
        except Exception as error:
            logger.warning("查询财务过期指标快照失败: %s", error)
            return []

    def _stale_snapshot_codes(self) -> list[str]:
        """Listed stocks whose snapshot is behind raw price OR complete financials."""
        return list(dict.fromkeys([
            *self._stale_price_snapshot_codes(),
            *self._stale_financial_snapshot_codes(),
        ]))

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

    def _listing_info_gap_codes(self) -> list[str]:
        """Return listed stocks missing metadata needed by the publish gate.

        契约 §5：缺口 = 既有三列（listing_date/is_st/is_suspended）缺失
        ∪ 股本完整性违例（:func:`data_quality.listed_share_capital_gap_codes`
        的单一真相，含 90 天新股豁免）。旧实现只看三列，26 只股本缺失股
        既不在 missing_list 也不在 retry_list——5 只新股豁免到期后直接锁死
        全市场发布（2026-11-12 P0）。
        """
        rows = self.duck.read_query(
            """SELECT stock_code FROM stock_meta
               WHERE is_listed IS TRUE
                 AND (listing_date IS NULL OR is_st IS NULL OR is_suspended IS NULL)
               ORDER BY stock_code"""
        )
        metadata_gaps = [str(row["stock_code"]) for row in rows]
        share_capital_gaps = listed_share_capital_gap_codes(self.duck)
        return sorted(dict.fromkeys([*metadata_gaps, *share_capital_gaps]))

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

        listing_due = self._refresh_due(
            "listing_info_last_refresh", self.universe_refresh_interval_days
        )
        # 新股今天刚进入 stock_list 时，listing_info 可能还在日内节流窗口；
        # 不能跳过，否则 listing_date/ST/停牌/股本缺失会卡住快照发布。
        gap_codes = self._listing_info_gap_codes()
        if listing_due or gap_codes:
            try:
                steps["listing_info"] = initializer._fetch_listing_info(
                    stock_codes=gap_codes if not listing_due and gap_codes else None
                )
                if listing_due and steps["listing_info"].get("status") == "success":
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

        csrc_full_due = self._csrc_refresh_due()
        csrc_null_gap = self._csrc_null_gap_exists()
        if csrc_full_due or csrc_null_gap:
            try:
                steps["csrc_industry"] = initializer._fetch_csrc_industry(
                    full_refresh=csrc_full_due,
                )
                if steps["csrc_industry"].get("status") in {"success", "partial"}:
                    self._mark_refreshed("csrc_industry_last_refresh")
            except Exception as error:
                logger.warning("CSRC 行业刷新失败: %s", error)
                steps["csrc_industry"] = {"status": "failed", "error": str(error)}
        else:
            steps["csrc_industry"] = {
                "status": "skipped",
                "reason": "refreshed_within_interval_and_no_unrecorded_null_gap",
                "interval_days": self.csrc_refresh_interval_days,
            }

        statuses = [step.get("status") for step in steps.values()]
        # skipped 是节流命中，不是失败。只要没有 failed/partial 子步骤，
        # 本轮 universe 就是正常完成，避免页面长期显示误导性的 partial。
        if all(status == "skipped" for status in statuses):
            status = "skipped"
        elif all(status in {"success", "skipped"} for status in statuses):
            status = "success"
        elif any(status == "failed" for status in statuses):
            status = "partial"
        else:
            status = "partial"
        return {
            "status": status,
            "steps": steps,
        }

    def _csrc_refresh_due(self) -> bool:
        """Return whether the full CSRC refresh interval has elapsed."""
        return self._refresh_due("csrc_industry_last_refresh", self.csrc_refresh_interval_days)

    def _csrc_null_gap_exists(self) -> bool:
        """Return true when listed stocks still lack CSRC and are not recorded missing.

        新股上市后 csrc_l1/csrc_l2 为空时，不能等 30 天全量刷新才补；
        已由 CNINFO 确认“源无分类”的股票记入 missing_list，不再每轮重试。
        """
        try:
            null_rows = self.duck.read_query(
                """SELECT COUNT(*) AS c FROM stock_meta
                   WHERE is_listed IS TRUE AND csrc_l1 IS NULL"""
            )
            missing_rows = self.sqlite.query(
                """SELECT COUNT(*) AS c FROM missing_list
                   WHERE field_name = 'csrc_industry' AND resolved_at IS NULL"""
            )
        except Exception as error:
            logger.warning("检查 CSRC 未记录缺口失败: %s", error)
            return False
        null_count = int(null_rows[0]["c"]) if null_rows else 0
        missing_count = int(missing_rows[0]["c"]) if missing_rows else 0
        return null_count > missing_count

    @staticmethod
    def _marker_cn_date(raw: Any) -> datetime | None:
        """Normalize a refresh marker to its **China trading date**.

        2026-11-12 修复：标记由 ``_mark_refreshed`` 以 **UTC ISO** 写入，而旧实现
        直接截取字符串前 10 位当年月日，再与 ``datetime.now(_CN_TZ).date()`` 相减。
        在北京时间 00:00–08:00 区间，UTC 日期比本地日期早一天，于是"今天刚刷新过"
        的标记会被判成"已过期"——每次凌晨的更新都会重跑一次约 104s 的
        universe 全量刷新，回归测试亦在跨零点后假失败。

        现在统一按 CN 时区归一：带时区的 ISO 时间戳先 ``astimezone(_CN_TZ)``，
        纯日期字符串按日期解释，其它格式视为无效（返回 ``None`` → 视为到期）。
        """
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d")
            except (ValueError, TypeError):
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(_CN_TZ)

    def _refresh_due(self, key: str, interval_days: int) -> bool:
        """Return whether a data-domain refresh marker is older than the interval."""
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [key],
        )
        if not rows:
            return True
        last_moment = self._marker_cn_date(rows[0].get("value"))
        if last_moment is None:
            return True
        last_date = last_moment.date()
        return (cn_today() - last_date).days >= interval_days

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
                [key, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()],
            )

    # ─── qfq 待重拉集合（2026-11-13 P0：关闭"事件晚于价格推进"死角）──────────
    #
    # 背景：按股票的除权窗口是 `min(本地 raw 最新日, 本地 qfq 最新日)` 与
    # `xdxr.event_date >=` 的比较。若除权事件 D 是在该股价格已经推进到 D
    # 之后才写进 xdxr（上一轮 market_actions 失败/公告截断/事件晚一轮），
    # 这个条件恒假 → 该股 qfq 永久保留旧复权因子（D 处跳空，MA/RSI/回撤全错），
    # 且任何 retry 都不会触发全历史重拉。
    #
    # 解法：不再依赖日期比较。**只要某股新写入了除权事件**（含数值修正），
    # 就把代码放进 SQLite 的持久化待重拉集合；价格步骤开头并入强制全历史
    # 重拉集合，重拉成功才移除，失败保留。覆盖"事件晚于价格推进"是因为触发
    # 条件变成"写入事实"，与两边日期先后无关。
    _QFQ_REFETCH_PENDING_KEY = "qfq_refetch_pending"
    _QFQ_REFETCH_PENDING_LIMIT = 500

    def _load_qfq_refetch_pending(self) -> list[str]:
        """Read the persisted codes whose qfq history must be fully refetched.

        只读诊断 SQL（运维事后核对，DuckDB 只读 + SQLite 只读）::

            -- 1) 待重拉集合（SQLite）
            SELECT value FROM data_refresh_state WHERE key = 'qfq_refetch_pending';
            -- 2) 这些股票本地 qfq 是否仍落后于 xdxr 事件（DuckDB）
            SELECT m.stock_code,
                   MAX(q.trade_date) AS latest_qfq_date,
                   MAX(x.event_date) AS latest_event_date
            FROM stock_meta m
            LEFT JOIN price_daily_qfq q ON q.stock_code = m.stock_code
            LEFT JOIN xdxr x ON x.stock_code = m.stock_code
            WHERE m.is_listed IS TRUE
            GROUP BY m.stock_code
            HAVING MAX(x.event_date) IS NOT NULL
               AND (MAX(q.trade_date) IS NULL OR MAX(q.trade_date) < MAX(x.event_date))
            ORDER BY m.stock_code;
        """
        try:
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = ?",
                [self._QFQ_REFETCH_PENDING_KEY],
            )
        except Exception as error:
            logger.warning("读取 qfq 待重拉集合失败: %s", error)
            return []
        if not rows:
            return []
        raw = rows[0].get("value")
        codes: list[str] = []
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                codes = [str(code).strip() for code in parsed]
            else:  # 兼容手工写入的逗号分隔值
                codes = [part.strip() for part in raw.replace(chr(10), ",").split(",")]
        return list(dict.fromkeys(code for code in codes if code))

    def _prune_unknown_stock_codes(self, codes: list[str]) -> list[str]:
        """Drop codes absent from stock_meta so the pending set cannot accumulate junk."""
        if not codes:
            return codes
        try:
            slots = ", ".join("?" for _ in codes)
            rows = self.duck.read_query(
                f"SELECT stock_code FROM stock_meta WHERE stock_code IN ({slots})",
                codes,
            )
        except Exception as error:
            logger.warning("裁剪 qfq 待重拉集合失败（保留原集合）: %s", error)
            return codes
        known = {str(row["stock_code"]) for row in rows}
        return [code for code in codes if code in known]

    def _mark_qfq_refetch_pending(self, codes: Iterable[str]) -> int:
        """Persist codes that must get a full qfq refetch in the next price step."""
        fresh = [str(code).strip() for code in codes if str(code or "").strip()]
        if not fresh:
            return 0
        pending = self._load_qfq_refetch_pending()
        merged = self._prune_unknown_stock_codes(
            list(dict.fromkeys([*pending, *fresh]))
        )
        if len(merged) > self._QFQ_REFETCH_PENDING_LIMIT:
            overflow = len(merged) - self._QFQ_REFETCH_PENDING_LIMIT
            logger.warning(
                "qfq 待重拉集合超过上限 %d，丢弃最早的 %d 只（请检查 market_actions 连续性）",
                self._QFQ_REFETCH_PENDING_LIMIT, overflow,
            )
            merged = merged[overflow:]
        try:
            now = datetime.now(UTC).isoformat()
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=excluded.updated_at""",
                    [self._QFQ_REFETCH_PENDING_KEY, json.dumps(merged), now],
                )
        except Exception as error:
            logger.warning("写入 qfq 待重拉集合失败: %s", error)
            return 0
        logger.info(
            "[增量] 新增 %d 只待重拉 qfq（累计 %d 只）: %s",
            len(fresh), len(merged), ", ".join(fresh[:20]),
        )
        return len(fresh)

    def _clear_qfq_refetch_pending(self, codes: Iterable[str]) -> int:
        """Drop codes whose full qfq refetch has been persisted."""
        done = {str(code) for code in codes if code}
        if not done:
            return 0
        pending = self._load_qfq_refetch_pending()
        remaining = [code for code in pending if code not in done]
        if len(remaining) == len(pending):
            return 0
        try:
            now = datetime.now(UTC).isoformat()
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=excluded.updated_at""",
                    [self._QFQ_REFETCH_PENDING_KEY, json.dumps(remaining), now],
                )
        except Exception as error:
            logger.warning("清理 qfq 待重拉集合失败: %s", error)
            return 0
        cleared = len(pending) - len(remaining)
        logger.info("[增量] %d 只 qfq 已完成全历史重拉，移出待重拉集合", cleared)
        return cleared

    # ─── 技术指标待重算集合（2026-11-14 P0 J6，依据 S7 §2.2：qfq 补齐后到不触发重算）──────
    #
    # 背景（依据 S6 §3 事实 4）：价格步骤"成功"但该股 qfq 仍停在旧的
    # 交易日（源侧 qfq 索引滞后 / 复权序列补齐晚一天）。此时 raw 已经推进，
    # 而价格域指标（annualized_volatility / period_return / max_drawdown /
    # MA 族）全都读 **qfq**：
    #   - `_stale_price_snapshot_codes()` 按 `latest_price_date != raw.latest`
    #     判断，读的是 raw 日期 → 判"没有过期"，不会触发重算；
    #   - 下一次价格步骤又会因为 `qfq.latest < target_date` 重新选中该股，
    #     但重算集合只认"本轮真正写入了价格"的股票，qfq 补齐那一天如果 raw
    #     已经是最新（无需再抓），重算集合里就没有它 → 指标永远停在旧值。
    # 实测：波动率覆盖率 17.8%，手动 `compute_indicators` 一次就到 99.6%。
    #
    # 解法：把"本轮出现 qfq 滞后（qfq 最新日 < raw 最新日）"的股票写进
    # `data_refresh_state['tech_recompute_pending']`；下一轮指标计算把它们
    # **强制**并入完整重算集合（与指纹/日期是否变化无关），重算成功后移除。
    # 名单有上限（2000 只）与 TTL（3 天）双保险，防止源长期滞后时无限堆积。
    _TECH_RECOMPUTE_PENDING_KEY = "tech_recompute_pending"
    _TECH_RECOMPUTE_PENDING_LIMIT = 2000
    _TECH_RECOMPUTE_PENDING_TTL_DAYS = 3

    # ─── 技术指标强制重算集合（2026-11-16，依据 S10 §9.2 P0-5）──────────────
    #
    # `tech_recompute_pending` 只覆盖"qfq 最新日 < raw 最新日"的**滞后**形态；
    # 还有一种形态它永远抓不到：qfq 在历史上是事后补齐的（例如某轮只补到旧日期，
    # 或手工补采/重放），补齐后 qfq 最新日已经和 raw 齐平 → 滞后判据为假，但
    # 快照里的技术三列还停在补齐前写死的 NULL（正式库实测 2,931/5,575 = 52.6%）。
    #
    # 因此本标记的语义是"**qfq 写入成功**"而不是"qfq 落后"：价格步骤每成功落盘
    # 一只股票，就把它连同 qfq 水位写进 `technical_pending`；指标步骤把这些股票
    # **强制**并入价格域重算（force_codes 绕过"最新价没变就跳过"），成功后移出。
    # 结构复用 tech_recompute_pending 的 {"code","status","marked_at","qfq_latest"}，
    # 上限 6,000（覆盖全市场一轮写入）/ TTL 3 天，防止异常轮次无限堆积。
    _TECHNICAL_PENDING_KEY = "technical_pending"
    _TECHNICAL_PENDING_LIMIT = 6000

    @staticmethod
    def _parse_state_timestamp(value: Any) -> datetime | None:
        """解析 data_refresh_state 里的 ISO 时间戳（naive 按 UTC 处理）。"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _read_tech_recompute_state(
        self, key: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取待重算名单原始记录（只读解析，兼容三种历史形态）。

        `key` 缺省为 `tech_recompute_pending`；`technical_pending` 复用同一个
        JSON 结构（{"code","status","marked_at","qfq_latest"}）。

        记录形态（同一 key 下的 JSON 数组）::

            {"code": "600519", "status": "pending",      # 待重算
             "marked_at": "...", "qfq_latest": "2026-11-12"}
            {"code": "600519", "status": "done",         # 已在某个 qfq 水位上算过
             "cleared_at": "...", "qfq_latest": "2026-11-12"}

        为什么还要留 done 记录：源侧 qfq 可能连着一周补不上来。若"清掉就允许
        立刻重新标记"，每轮都会把同一批股票（上限 2000 只）整股重算一遍 ——
        实测全量重算约 12 分钟，这会变成每天几小时的纯浪费。记住"这只股票在
        qfq=X 水位上已经重算过"，只有 qfq 真的前进了才再算一次。
        """
        try:
            rows = self.sqlite.query(
                "SELECT value, updated_at FROM data_refresh_state WHERE key = ?",
                [key or self._TECH_RECOMPUTE_PENDING_KEY],
            )
        except Exception as error:  # noqa: BLE001 - 读失败不得炸掉价格步骤
            logger.warning("读取技术指标待重算名单失败: %s", error)
            return []
        if not rows:
            return []
        raw = rows[0].get("value")
        fallback_stamp = str(rows[0].get("updated_at") or "").strip()
        parsed: Any = None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
        records: list[dict[str, Any]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    code = str(item.get("code") or "").strip()
                    status = str(item.get("status") or "pending").strip() or "pending"
                    stamp = str(
                        item.get("marked_at")
                        or item.get("cleared_at")
                        or fallback_stamp
                    )
                    watermark = item.get("qfq_latest")
                else:  # 兼容只写代码字符串的旧格式
                    code = str(item or "").strip()
                    status, stamp, watermark = "pending", fallback_stamp, None
                if not code:
                    continue
                records.append({
                    "code": code,
                    "status": status if status in {"pending", "done"} else "pending",
                    "timestamp": stamp,
                    "qfq_latest": None if watermark is None else str(watermark),
                })
        elif isinstance(raw, str) and raw.strip():  # 兼容手工写入的逗号分隔值
            for part in raw.replace(chr(10), ",").split(","):
                code = part.strip()
                if code:
                    records.append({
                        "code": code,
                        "status": "pending",
                        "timestamp": fallback_stamp,
                        "qfq_latest": None,
                    })
        # 同 code 只保留最后一条（后写入的状态更新）
        deduped: dict[str, dict[str, Any]] = {}
        for record in records:
            deduped.pop(record["code"], None)
            deduped[record["code"]] = record
        return list(deduped.values())

    @staticmethod
    def _tech_record_payload(record: dict[str, Any]) -> dict[str, Any]:
        """把内部记录还原成落库形态（时间戳字段名按状态区分）。"""
        payload: dict[str, Any] = {"code": record["code"], "status": record["status"]}
        if record["status"] == "done":
            payload["cleared_at"] = record["timestamp"]
        else:
            payload["marked_at"] = record["timestamp"]
        payload["qfq_latest"] = record.get("qfq_latest")
        return payload

    def _prune_expired_tech_entries(
        self, records: list[dict[str, Any]], *, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """丢掉超过 TTL 的记录（pending 与 done 一视同仁）。"""
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=self._TECH_RECOMPUTE_PENDING_TTL_DAYS)
        kept: list[dict[str, Any]] = []
        for record in records:
            stamp = self._parse_state_timestamp(record.get("timestamp"))
            # 解析不了的时间戳按"刚刚标记"处理（宁多重算一次，不漏重算）
            if stamp is not None and stamp < cutoff:
                continue
            kept.append(record)
        return kept

    def _write_tech_recompute_entries(
        self, records: list[dict[str, Any]], *, key: str | None = None,
    ) -> bool:
        try:
            now = datetime.now(UTC).isoformat()
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=excluded.updated_at""",
                    [
                        key or self._TECH_RECOMPUTE_PENDING_KEY,
                        json.dumps(
                            [self._tech_record_payload(record) for record in records],
                            ensure_ascii=False,
                        ),
                        now,
                    ],
                )
        except Exception as error:  # noqa: BLE001 - 状态写失败不得炸掉更新
            logger.warning("写入技术指标待重算名单失败: %s", error)
            return False
        return True

    def _load_tech_recompute_pending(
        self, *, now: datetime | None = None, key: str | None = None,
    ) -> list[str]:
        """读取**待重算**股票代码（自动丢弃过期记录，按标记顺序返回）。

        只读诊断 SQL（运维事后核对）::

            -- 1) 待重算名单（SQLite）
            SELECT value FROM data_refresh_state WHERE key = 'tech_recompute_pending';
            -- 2) 本地 qfq 是否仍落后 raw（DuckDB）
            SELECT r.stock_code, r.latest_raw, q.latest_qfq
            FROM (SELECT stock_code, MAX(trade_date) AS latest_raw
                  FROM price_daily_raw GROUP BY stock_code) r
            LEFT JOIN (SELECT stock_code, MAX(trade_date) AS latest_qfq
                       FROM price_daily_qfq GROUP BY stock_code) q
                   ON q.stock_code = r.stock_code
            WHERE q.latest_qfq IS NULL OR q.latest_qfq < r.latest_raw
            ORDER BY r.stock_code;
        """
        records = self._prune_expired_tech_entries(
            self._read_tech_recompute_state(key), now=now,
        )
        return [record["code"] for record in records if record["status"] == "pending"]

    def _mark_tech_recompute_pending(
        self,
        watermarks: dict[str, str | None] | Iterable[str],
        *,
        now: datetime | None = None,
        key: str | None = None,
        limit: int | None = None,
    ) -> int:
        """把 qfq 滞后的股票写进待重算名单（幂等；同水位算过的不重复入列）。

        `watermarks` 支持 ``{code: qfq 最新日}`` 或纯代码集合。传水位时，若该
        股票**已经在同一个 qfq 水位上重算过**（done 记录），就跳过 —— 否则源侧
        连续滞后数天会变成"每轮重算 2000 只"的纯浪费。
        """
        if isinstance(watermarks, dict):
            fresh: dict[str, str | None] = {
                str(code).strip(): (None if value is None else str(value))
                for code, value in watermarks.items()
                if str(code or "").strip()
            }
        else:
            fresh = {
                str(code).strip(): None
                for code in watermarks
                if str(code or "").strip()
            }
        if not fresh:
            return 0
        moment = now or datetime.now(UTC)
        stamp = moment.isoformat()
        records = self._prune_expired_tech_entries(
            self._read_tech_recompute_state(key), now=moment,
        )
        by_code = {record["code"]: record for record in records}
        added = 0
        skipped_same_watermark = 0
        for code, watermark in fresh.items():
            record = by_code.get(code)
            if record is not None and record["status"] == "pending":
                # 仍滞后 → 续期，避免 TTL 在长期滞后时把名单清空
                record["timestamp"] = stamp
                if watermark is not None:
                    record["qfq_latest"] = watermark
                continue
            if (
                record is not None
                and record["status"] == "done"
                and watermark is not None
                and record.get("qfq_latest") == watermark
            ):
                # 这个 qfq 水位已经重算过，没有新数据 → 不重复计算
                skipped_same_watermark += 1
                continue
            by_code[code] = {
                "code": code,
                "status": "pending",
                "timestamp": stamp,
                "qfq_latest": watermark,
            }
            added += 1
        ordered = list(by_code.values())
        pending = [record for record in ordered if record["status"] == "pending"]
        entry_limit = limit if limit is not None else self._TECH_RECOMPUTE_PENDING_LIMIT
        if len(pending) > entry_limit:
            overflow = len(pending) - entry_limit
            logger.warning(
                "技术指标待重算名单超过上限 %d，丢弃最早的 %d 只（请检查 qfq 源滞后）",
                entry_limit, overflow,
            )
            dropped = {record["code"] for record in pending[:overflow]}
            ordered = [record for record in ordered if record["code"] not in dropped]
        if not self._write_tech_recompute_entries(ordered, key=key):
            return 0
        logger.info(
            "[增量] %s %d 只 → 待重算名单新增 %d（同水位已算过 %d，累计待重算 %d）: %s",
            "qfq 滞后" if key is None else "qfq 已写入",
            len(fresh), added, skipped_same_watermark,
            min(len(pending), entry_limit),
            ", ".join(list(fresh)[:20]),
        )
        return added

    def _clear_tech_recompute_pending(
        self, codes: Iterable[str], *, key: str | None = None,
    ) -> int:
        """把已完成重算的股票标记为 done（记下 qfq 水位，失败/被拒的保留）。"""
        done_codes = {str(code) for code in codes if code}
        if not done_codes:
            return 0
        stamp = datetime.now(UTC).isoformat()
        records = self._read_tech_recompute_state(key)
        cleared = 0
        for record in records:
            if record["code"] in done_codes and record["status"] == "pending":
                record["status"] = "done"
                record["timestamp"] = stamp
                cleared += 1
        if not cleared:
            return 0
        if not self._write_tech_recompute_entries(records, key=key):
            return 0
        logger.info("[增量] %d 只技术指标已完成重算，移出待重算名单", cleared)
        return cleared

    def _qfq_lag_watermarks(self, target_codes: Iterable[str]) -> dict[str, str | None]:
        """列出 **qfq 最新日 < raw 最新日** 的股票及其 qfq 水位（实现见 `prices_step`）。"""
        return prices_step.qfq_lag_watermarks(self, target_codes, logger=logger)

    def _qfq_watermarks(self, codes: Iterable[str]) -> dict[str, str | None]:
        """读取一批股票**落库后**的 qfq 最新日（实现见 `prices_step`）。"""
        return prices_step.qfq_watermarks(self, codes, logger=logger)

    def _qfq_lag_codes_vs_raw(self, target_codes: Iterable[str]) -> list[str]:
        """`_qfq_lag_watermarks` 的代码清单形态（只关心"有没有滞后"时用）。"""
        return sorted(self._qfq_lag_watermarks(target_codes))

    # ─── technical_pending（qfq 写入成功 → 强制重算技术三列）───────────────
    def _load_technical_pending(self, *, now: datetime | None = None) -> list[str]:
        """读取"qfq 已写入、待强制重算技术指标"的股票代码。

        运维事后核对::

            SELECT value FROM data_refresh_state WHERE key = 'technical_pending';
        """
        return self._load_tech_recompute_pending(
            now=now, key=self._TECHNICAL_PENDING_KEY,
        )

    def _mark_technical_pending(
        self,
        watermarks: dict[str, str | None] | Iterable[str],
        *,
        now: datetime | None = None,
    ) -> int:
        """把 qfq 写入成功的股票写进 `technical_pending`（幂等、同水位去重）。"""
        return self._mark_tech_recompute_pending(
            watermarks,
            now=now,
            key=self._TECHNICAL_PENDING_KEY,
            limit=self._TECHNICAL_PENDING_LIMIT,
        )

    def _clear_technical_pending(self, codes: Iterable[str]) -> int:
        """把技术指标已重算成功的股票移出 `technical_pending`。"""
        return self._clear_tech_recompute_pending(
            codes, key=self._TECHNICAL_PENDING_KEY,
        )

    def _finalize_technical_pending(
        self, step: dict[str, Any] | None, pending: Iterable[str],
    ) -> int:
        """`technical_pending` 的重算收尾（失败/隔离保留给下一轮）。"""
        return self._finalize_pending(step, pending, key=self._TECHNICAL_PENDING_KEY)

    def _finalize_tech_recompute(
        self, step: dict[str, Any] | None, pending: Iterable[str],
    ) -> int:
        """重算收尾：成功的移出名单，失败/被拒的保留给下一轮。

        判定口径：`failed_codes` 里的保留；`rejected` 整批拒绝时一只都不清；
        `skipped`（无重算需求）不清 —— 名单里的股票本来就"必须被重算"，
        skipped 意味着这一轮根本没算，清掉就等于把 P0 触发信号吃了。
        """
        return self._finalize_pending(step, pending)

    def _finalize_pending(
        self,
        step: dict[str, Any] | None,
        pending: Iterable[str],
        *,
        key: str | None = None,
    ) -> int:
        """通用收尾：成功重算的移出待重算名单，失败/被拒的保留给下一轮。"""
        codes = [str(code) for code in pending if code]
        if not codes:
            return 0
        report = step if isinstance(step, dict) else {}
        if report.get("status") in {"rejected", "skipped", None}:
            return 0
        # failed_codes 是失败；quarantined_codes 是发布门禁隔离（都没重算成功），
        # 两类都必须留在名单里等下一轮。合并后的步骤（`_merge_indicator_steps`）
        # 会把逐股结果放在 sub_steps 里，这里一并收上来。
        keep: set[str] = set()
        sources = [report, *(report.get("sub_steps") or [])]
        for source in sources:
            if not isinstance(source, dict):
                continue
            keep.update(str(code) for code in (source.get("failed_codes") or []))
            keep.update(str(code) for code in (source.get("quarantined_codes") or []))
        known_failure = any(
            _as_counter(source.get("failed"))
            for source in sources
            if isinstance(source, dict)
        )
        if known_failure and not keep:
            # 有失败但没有可定位的代码 → 保守不清（宁可下轮多重算一次）
            logger.warning("指标重算有失败但无可定位代码，保留整份待重算名单")
            return 0
        return self._clear_tech_recompute_pending(
            [code for code in codes if code not in keep], key=key,
        )

    def _qfq_lag_summary(
        self, target_codes: list[str], target_date: str,
    ) -> dict[str, Any]:
        """统计 qfq 落后自己 raw 的目标股（细节见 `prices_step.qfq_lag_summary`）。"""
        return prices_step.qfq_lag_summary(
            self, target_codes, target_date, logger=logger,
        )

    def _xdxr_event_fingerprints(self, codes: list[str]) -> set[tuple[str, str, str, str]]:
        """Fingerprint existing xdxr events so new/changed ones can be detected.

        用 (代码, 除权日, 类别, 分红/配股/送转) 做指纹：同一天公告被修正
        （分红金额变化）同样会改变前复权因子，必须算作"新写入"。
        """
        codes = [str(code) for code in codes if code]
        if not codes:
            return set()
        slots = ", ".join("?" for _ in codes)
        try:
            rows = self.duck.read_query(
                f"""SELECT stock_code,
                           CAST(event_date AS VARCHAR) AS event_date,
                           CAST(category AS VARCHAR) AS category,
                           CAST(fenhong AS VARCHAR) AS fenhong,
                           CAST(peigu AS VARCHAR) AS peigu,
                           CAST(songzhuangu AS VARCHAR) AS songzhuangu
                    FROM xdxr
                    WHERE stock_code IN ({slots})""",
                codes,
            )
        except Exception as error:
            logger.warning("读取既有除权事件指纹失败: %s", error)
            return set()
        return {
            (
                str(row["stock_code"]),
                str(row.get("event_date") or "")[:10],
                str(row.get("category")),
                f"{row.get('fenhong')}|{row.get('peigu')}|{row.get('songzhuangu')}",
            )
            for row in rows
        }

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

    def _is_b_share_stock(self, stock_code: str) -> bool:
        """B 股没有免费财务主源，按代码段或名称后缀识别。

        2026-08-30：日志发现 201872 这类深市 B 股不在 200/900 前缀规则内，
        改为代码段 + stock_meta 名称后缀双重识别。
        """
        if stock_code.startswith(("200", "201", "900")):
            return True
        try:
            rows = self.duck.read_query(
                "SELECT name FROM stock_meta WHERE stock_code = ? "
                "AND (name LIKE '%B' OR name LIKE '%B股')",
                [stock_code],
            )
            return bool(rows)
        except Exception:
            return False

    @staticmethod
    def _current_expected_financial_period(now: datetime | None = None) -> str:
        """Return the report period that should already be published today.

        半年报季（8-10月）期望 06-30；三季报季（11月后）期望 09-30；
        一季报季（5-7月）期望 03-31；其余时间期望上一年年报 12-31。
        披露季以北京时间为准（2026-09-08 红队 C 修复）。
        """
        return expected_financial_period(now)

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

    def _announcement_cursor(self) -> datetime | None:
        """Last successfully persisted announcement-check cursor (UTC)."""
        try:
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = ?",
                [_ANNOUNCEMENT_CHECK_CURSOR_KEY],
            )
            if not rows:
                return None
            value = rows[0].get("value")
            if not value:
                return None
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (ValueError, TypeError):
            return None

    def _save_announcement_cursor(self, at: datetime | None = None) -> None:
        """Advance the durable announcement-check cursor after a clean remote check."""
        cursor = (at or datetime.now(UTC)).isoformat()
        self.sqlite.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                              updated_at=excluded.updated_at""",
            [_ANNOUNCEMENT_CHECK_CURSOR_KEY, cursor, datetime.now(UTC).isoformat()],
        )

    def _check_new_announcements(
        self,
        *,
        persist: bool = True,
        lookback_days: int = 3,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Compare remote announcement IDs without mutating during a check-only run.

        全市场按日期段批量查询（PRD §7.7），替代逐股轮询。2026-08-28 修复：
        - CNINFO 每页固定 30 条；财务公告按半年报/一季报/三季报/年报类别
          分别精确查询，不再依赖噪声极大的全市场全文列表。
        - 持久化检查游标后，日常只查游标之后的窗口；游标缺失时回看
          `announcement_catchup_days`（默认 45 天，覆盖整个中报季）。
        - 非财务公告只查最近 `announcement_lookback_days` 天用于登记。
        """
        now = datetime.now(UTC)
        end_date = now.strftime("%Y-%m-%d")
        general_lookback = (
            lookback_days if lookback_days else self.announcement_lookback_days
        )
        general_start = (now - timedelta(days=general_lookback)).strftime("%Y-%m-%d")
        cursor = self._announcement_cursor() if persist else None
        if cursor is not None:
            financial_start = (cursor - timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            financial_window = (
                getattr(self, "announcement_catchup_days", 45)
                if persist
                else general_lookback
            )
            financial_start = (now - timedelta(days=financial_window)).strftime("%Y-%m-%d")
        financial_max_pages = getattr(self, "announcement_financial_max_pages", 450)
        general_max_pages = getattr(self, "announcement_general_max_pages", 40)

        category_labels = {
            "semi_annual": "半年报", "q1": "一季报", "q3": "三季报",
            "annual": "年报",
        }

        def fetch_items(
            category: str | None, start_date: str, max_pages: int,
        ) -> tuple[list[dict[str, Any]], str | None, bool]:
            label = category_labels.get(category or "", "近期公告")

            def page_progress(info: dict[str, Any]) -> None:
                if detail_cb is None:
                    return
                total_pages = int(info.get("total_pages") or 0)
                detail_cb("announcements", {
                    "step": "announcements",
                    "label": f"公告检查·{label}",
                    "done": int(info.get("page") or 0),
                    "total": total_pages,
                    "current": f"第{int(info.get('page') or 0)}页/{total_pages or '?'}",
                })

            try:
                result = self.adapter_mgr.fetch(FetchRequest(
                    data_type="announcements",
                    start_date=start_date,
                    end_date=end_date,
                    extra_params={
                        "category": category,
                        "page_size": 30,
                        "max_pages": max_pages,
                        "progress_cb": page_progress,
                    },
                ))
            except Exception as error:
                return [], str(error), False
            items = list(result.data or [])
            # 类别接口已经按"半年报/一季报/三季报/年报/分红"精确过滤；
            # 携带来源类别，后面不再依赖标题措辞判断是否财务公告
            # （CNINFO 存在"2026年半年报"这类标题，旧关键词会漏报）。
            if category in _ANNOUNCEMENT_FINANCIAL_CATEGORIES or category == _ANNOUNCEMENT_DIVIDEND_CATEGORY:
                for item in items:
                    item["_announcement_category"] = category
            return items, result.metadata.error or None, _source_truncated(result)

        all_items: list[dict[str, Any]] = []
        errors: list[str] = []
        truncated_categories: list[str] = []
        last_error_source = "cninfo"
        for category in _ANNOUNCEMENT_FINANCIAL_CATEGORIES:
            items, error, truncated = fetch_items(
                category, financial_start, financial_max_pages,
            )
            all_items.extend(items)
            if error:
                errors.append(f"{category}: {error}")
            if truncated:
                truncated_categories.append(category)
        dividend_items, dividend_error, dividend_truncated = fetch_items(
            _ANNOUNCEMENT_DIVIDEND_CATEGORY, financial_start, financial_max_pages,
        )
        all_items.extend(dividend_items)
        if dividend_error:
            errors.append(f"dividend: {dividend_error}")
        if dividend_truncated:
            truncated_categories.append(_ANNOUNCEMENT_DIVIDEND_CATEGORY)
        # general 全文列表只用于"非财务公告登记"（可选簿记）；分红/除权公告
        # 已由上面的 dividend 类别精确覆盖，general 截断不再阻断游标推进，
        # 否则每个公告量大日都会把更新打成 partial 并反复重查同一窗口。
        general_items, general_error, general_truncated = fetch_items(
            None, general_start, general_max_pages,
        )
        all_items.extend(general_items)
        # 2026-11-13 P1（C）：general 全文列表只用于"非财务公告登记"（可选簿记），
        # 它截断时**不得**把整轮 job 打成 partial —— 否则每轮都 partial，
        # last_full_success_at 永久冻结（生产已从 10-30 冻结至今）。只进 warnings
        # （带条数与窗口），财务/分红类别的截断仍按 partial 处理。
        general_warnings: list[str] = []
        if general_error:
            general_warnings.append(f"general: {general_error}")
        if general_truncated:
            general_warnings.append(
                f"general 窗口截断（{general_start}~{end_date}，已取 {len(general_items)} 条；"
                "只影响非财务公告登记，不影响财务/分红类别）"
            )
        general_warning = "; ".join(general_warnings) or None

        # 类别查询与全文查询可能返回同一公告；按公告 ID 去重。
        unique_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in all_items:
            announcement_id = item.get("announcement_id")
            stock_code = item.get("stock_code")
            if not announcement_id or not stock_code:
                continue
            if announcement_id in seen_ids:
                continue
            seen_ids.add(announcement_id)
            unique_items.append(item)

        if errors and persist:
            with contextlib.suppress(Exception):
                self._record_failure(
                    "", "announcements", last_error_source, "; ".join(errors),
                )

        if not unique_items and errors:
            return {
                "status": "unavailable",
                "checked_remote": True,
                "reason": "announcement_source_unavailable",
                "affected_stock_codes": [],
                "affected_announcements": {},
                "all_new_announcements": {},
                "errors": errors,
                "warnings": [general_warning] if general_warning else [],
                "truncated": bool(truncated_categories),
                "truncated_categories": truncated_categories,
            }

        # 批量查询本地登记表，避免每一条公告一次 SQL。
        existing_ids: set[str] = set()
        all_ids = [item["announcement_id"] for item in unique_items]
        for offset in range(0, len(all_ids), 400):
            chunk = all_ids[offset:offset + 400]
            placeholders = ", ".join("?" for _ in chunk)
            try:
                rows = self.sqlite.query(
                    f"SELECT announcement_id FROM announcement_registry "
                    f"WHERE announcement_id IN ({placeholders})",
                    chunk,
                )
                existing_ids.update(str(row["announcement_id"]) for row in rows)
            except Exception:
                # 老库缺少该列时退回逐条查询，保证发现功能仍可用。
                existing_ids.update(
                    str(item["announcement_id"]) for item in unique_items
                    if self.sqlite.query(
                        "SELECT 1 FROM announcement_registry WHERE announcement_id = ?",
                        [item["announcement_id"]],
                    )
                )

        affected: set[str] = set()
        affected_announcements: dict[str, list[dict[str, Any]]] = {}
        for item in unique_items:
            announcement_id = str(item["announcement_id"])
            stock_code = str(item["stock_code"])
            if announcement_id in existing_ids:
                continue
            affected.add(stock_code)
            affected_announcements.setdefault(stock_code, []).append(item)

        # 财务类别接口直接命中的公告无条件视为财务公告；general 列表中的
        # 公告按标题分类，只保留财务类公告触发刷新。
        financial_codes: set[str] = set()
        financial_announcements: dict[str, list[dict[str, Any]]] = {}
        for code in sorted(affected):
            financial_items = [
                item for item in affected_announcements[code]
                if item.get("_announcement_category") in _ANNOUNCEMENT_FINANCIAL_CATEGORIES
                or classify_announcement(item.get("title")) == "financial"
            ]
            if financial_items:
                financial_codes.add(code)
                financial_announcements[code] = financial_items

        # 游标不在这里推进：只有当本轮发现的财务公告全部成功刷新/入册后，
        # 才允许把“已检查到”的边界前移（见 _run_incremental_update_flow 末尾）。
        # 若中途失败，下一轮会重新发现未入册公告，断点不丢失。
        # 截断 = 拿到部分数据但源标记不完整（契约 §1 truncated_ok）：如实的
        # ``partial`` + reason，绝不升级成 ERROR，也不阻断游标推进。
        if truncated_categories:
            status = "partial"
            reason = "announcements_truncated"
        elif errors:
            status = "partial"
            reason = "announcements_partial"
        else:
            status = "available"
            reason = None
        return {
            "status": status,
            "reason": reason,
            "checked_remote": True,
            "affected_stock_codes": sorted(financial_codes),
            "affected_announcements": financial_announcements,
            "all_new_announcements": {
                code: affected_announcements[code] for code in sorted(affected)
            },
            "errors": errors,
            "warnings": [general_warning] if general_warning else [],
            "truncated": bool(truncated_categories),
            "truncated_categories": truncated_categories,
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

    def _pending_announcement_items(self) -> list[dict[str, Any]]:
        """Return durable pending financial-announcement markers from retry_list."""
        try:
            rows = self.sqlite.query(
                "SELECT stock_code, extra_json FROM retry_list "
                "WHERE data_type = 'announcements'"
            )
        except Exception:
            return []
        items: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("stock_code") or "").strip()
            if not code:
                continue
            ids: list[str] = []
            try:
                ids = json.loads(row.get("extra_json") or "{}").get("announcement_ids", [])
            except (json.JSONDecodeError, TypeError):
                ids = []
            if not ids:
                ids = [f"pending-{code}"]
            for announcement_id in ids:
                items.append({
                    "stock_code": code,
                    "announcement_id": str(announcement_id),
                    "title": "pending financial announcement",
                    "announcement_time": "",
                })
        return items

    def _resolve_announcement_retries(self, stock_code: str) -> None:
        """Remove announcement pending markers once the stock's refresh succeeded."""
        try:
            self.sqlite.execute(
                "DELETE FROM retry_list WHERE stock_code = ? AND data_type = 'announcements'",
                [stock_code],
            )
        except Exception as error:
            logger.warning("清理公告 retry %s 失败: %s", stock_code, error)

    def _resolve_announcement_source_retries(self) -> None:
        """Remove the global CNINFO outage marker once a later check succeeds.

        Source-level failures are recorded with stock_code='' so per-stock
        pending markers are never conflated; without this cleanup the global
        marker would keep diagnose 永久显示 retry_count=1 even after CNINFO
        recovers.
        """
        try:
            self.sqlite.execute(
                "DELETE FROM retry_list WHERE stock_code = '' AND data_type = 'announcements'"
            )
        except Exception as error:
            logger.warning("清理公告源故障 retry 失败: %s", error)

    def _fetch_financial_trio(
        self, stock_code: str,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]:
        """Fetch latest rows for one stock's three statements without writing."""
        data_types = ("balance_sheet", "income_statement", "cash_flow")
        fetched: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
        outcomes: list[dict[str, Any]] = []
        if self._is_b_share_stock(stock_code):
            return [
                {
                    "status": "failed", "data_type": data_type,
                    "error": "B-share financial source unsupported",
                }
                for data_type in data_types
            ], fetched
        for data_type in data_types:
            try:
                latest_local = self._get_latest_financial_report_date(
                    stock_code, data_type,
                )
                result = self.adapter_mgr.fetch(FetchRequest(
                    data_type=data_type,
                    stock_codes=[stock_code],
                    extra_params={"num": "1"},
                ))
                if result.metadata.error or not result.data:
                    outcomes.append({
                        "status": "failed", "data_type": data_type,
                        "error": result.metadata.error or "empty result",
                    })
                    continue
                new_rows = [
                    row for row in result.data
                    if not latest_local or str(row.get("report_date") or "") > latest_local
                ]
                if not new_rows:
                    outcomes.append({
                        "status": "success", "data_type": data_type,
                        "skipped": True, "latest_local": latest_local,
                    })
                    continue
                fetched[data_type] = (result, new_rows)
                outcomes.append({
                    "status": "success", "data_type": data_type, "skipped": False,
                    "latest_fetched": str(max(
                        (str(row.get("report_date") or "") for row in new_rows),
                        default=latest_local or "",
                    )),
                })
            except Exception as error:
                outcomes.append({
                    "status": "failed", "data_type": data_type, "error": str(error),
                })
        return outcomes, fetched

    @staticmethod
    def _persist_fetched_trio_in_connection(
        initializer: Any,
        conn: Any,
        stock_code: str,
        fetched: dict[str, tuple[Any, list[dict[str, Any]]]],
    ) -> None:
        """Write fetched financial rows + lineage for one stock into conn."""
        for data_type, (_, new_rows) in fetched.items():
            for row in new_rows:
                initializer._upsert_financial_row(conn, data_type, stock_code, row)
        for data_type, (result, new_rows) in fetched.items():
            batch_id = initializer._record_batch_in_connection(
                conn, result, data_type, len(new_rows),
            )
            initializer._record_field_audit_in_connection(
                conn, result, new_rows, stock_code, "report_date", batch_id,
            )

    def _persist_financial_trio(
        self, stock_code: str, outcomes: list[dict[str, Any]],
        fetched: dict[str, tuple[Any, list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """Persist one stock's fetched trio in its own transaction."""
        if not fetched:
            return outcomes
        from app.core.init import DataInitializer

        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        try:
            with self.duck.transaction() as conn:
                self._persist_fetched_trio_in_connection(
                    initializer, conn, stock_code, fetched,
                )
        except Exception as error:
            for outcome in outcomes:
                if outcome.get("status") == "success" and not outcome.get("skipped"):
                    outcome["status"] = "failed"
                    outcome["error"] = str(error)
            return outcomes
        balance = fetched.get("balance_sheet")
        if balance is not None:
            try:
                initializer._record_missing_financial_sector_fields(
                    stock_code, balance[1],
                )
            except Exception:
                logger.debug("记录 %s 金融业缺失字段失败（非致命）", stock_code)
        return outcomes

    def _persist_financial_batch(
        self, items: list[tuple[str, list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]],
    ) -> None:
        """Persist up to batch_size stocks in one transaction; fallback per stock."""
        from app.core.init import DataInitializer

        if not items:
            return
        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        try:
            with self.duck.transaction() as conn:
                for code, _, fetched in items:
                    self._persist_fetched_trio_in_connection(
                        initializer, conn, code, fetched,
                    )
        except Exception as error:
            logger.warning(
                "财务批量写入失败，退化为逐股单事务: %s", error,
            )
            for code, outcomes, fetched in items:
                self._persist_financial_trio(code, outcomes, fetched)
            return
        for code, _, fetched in items:
            balance = fetched.get("balance_sheet")
            if balance is None:
                continue
            try:
                initializer._record_missing_financial_sector_fields(
                    code, balance[1],
                )
            except Exception:
                logger.debug("记录 %s 金融业缺失字段失败（非致命）", code)

    def refetch_financial_trio(self, stock_code: str) -> list[dict[str, Any]]:
        """Refresh one stock's three statements (fetch + single transaction)."""
        outcomes, fetched = self._fetch_financial_trio(stock_code)
        return self._persist_financial_trio(stock_code, outcomes, fetched)

    def _financial_detail_gap_codes(self) -> list[str]:
        """Return stocks whose latest financial rows still miss detail line items.

        核心字段（total_assets/revenue/cf_from_operating）齐备但明细字段为空，
        说明该报告期来自旧的“最小核心集”抓取或旧版解析器。这些股票不阻断
        readiness，但详情页会显示缺口；本方法为有界续传提供队列。
        """
        try:
            rows = self.duck.read_query(
                """
                WITH latest_bs AS (
                    SELECT stock_code, report_date, total_assets,
                           monetary_funds, paid_in_capital, undistributed_profit
                    FROM balance_sheet
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code ORDER BY report_date DESC
                    ) = 1
                ), latest_ic AS (
                    SELECT stock_code, report_date, revenue,
                           cost_of_revenue, interest_income, interest_expense,
                           selling_expenses, administrative_expenses,
                           taxes_and_surcharges, investment_income
                    FROM income_statement
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code ORDER BY report_date DESC
                    ) = 1
                ), latest_cf AS (
                    SELECT stock_code, report_date, cf_from_operating,
                           cash_received_sales, taxes_refunded, cash_paid_goods,
                           cash_paid_employees, cash_paid_taxes,
                           total_operating_cf_in, total_operating_cf_out
                    FROM cash_flow
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code ORDER BY report_date DESC
                    ) = 1
                )
                SELECT m.stock_code
                FROM stock_meta m
                LEFT JOIN latest_bs bs ON bs.stock_code = m.stock_code
                LEFT JOIN latest_ic ic ON ic.stock_code = m.stock_code
                LEFT JOIN latest_cf cf ON cf.stock_code = m.stock_code
                WHERE m.is_listed IS TRUE
                  AND NOT (
                      m.stock_code LIKE '200%'
                      OR m.stock_code LIKE '201%'
                      OR m.stock_code LIKE '900%'
                      OR m.name LIKE '%B'
                      OR m.name LIKE '%B股'
                  )
                  AND (
                      -- 不能用单一字段（如 cost_of_revenue）探测“旧最小核心集”：
                      -- 银行/券商/保险的利润表本来就没有营业成本，单一探测会
                      -- 让这些股票回填成功后仍然留在缺口队列头部，挤占每轮
                      -- 100 个名额，后面的股票永远排不到。改为“行业通用候选
                      -- 字段组全部为空才算旧行”：正常回填成功的股票至少会写入
                      -- paid_in_capital/undistributed_profit、利息或费用类利润
                      -- 字段、职工/税费现金字段之一。
                      (
                          bs.total_assets IS NOT NULL
                          AND COALESCE(
                              bs.monetary_funds, bs.paid_in_capital,
                              bs.undistributed_profit,
                          ) IS NULL
                      )
                      OR (
                          ic.revenue IS NOT NULL
                          AND COALESCE(
                              ic.cost_of_revenue, ic.interest_income,
                              ic.interest_expense, ic.selling_expenses,
                              ic.administrative_expenses,
                              ic.taxes_and_surcharges, ic.investment_income,
                          ) IS NULL
                      )
                      OR (
                          cf.cf_from_operating IS NOT NULL
                          AND COALESCE(
                              cf.cash_received_sales, cf.taxes_refunded,
                              cf.cash_paid_goods, cf.cash_paid_employees,
                              cf.cash_paid_taxes, cf.total_operating_cf_in,
                              cf.total_operating_cf_out,
                          ) IS NULL
                      )
                  )
                ORDER BY m.stock_code
                """
            )
        except Exception as error:
            logger.warning("查询财务明细缺口失败: %s", error)
            return []
        codes = [row["stock_code"] for row in rows]
        # 快速源（sina/akshare）已确认无数据的股票登记
        # financial_detail_backfill missing；在重试窗口内不得再次占住队头，
        # 否则每轮都会落到 40s+ 的 TDX 慢回退上，队列永远推不动。
        try:
            cutoff = datetime.now(UTC) - timedelta(
                days=max(0, self.financial_detail_backfill_missing_retry_days),
            )
            miss_rows = self.sqlite.query(
                """SELECT stock_code FROM missing_list
                   WHERE field_name = 'financial_detail_backfill'
                     AND resolved_at IS NULL
                     AND detected_at >= ?""",
                [cutoff.isoformat()],
            )
            blocked = {row["stock_code"] for row in miss_rows}
            return [code for code in codes if code not in blocked]
        except Exception as error:
            logger.warning("查询财务明细 missing 缓存失败: %s", error)
            return codes

    def _refresh_buyback(self) -> dict[str, Any]:
        """低频全市场回购明细刷新，并返回本次发生变化的股票代码。

        东财回购接口一次返回全市场，刷新成本低；写入后需要重算受影响股票
        的 dividend_financing_ratio_pct 快照。
        """
        if not self._refresh_due("buyback_last_refresh", self.buyback_refresh_interval_days):
            return {
                "status": "skipped",
                "reason": "refreshed_within_interval",
                "interval_days": self.buyback_refresh_interval_days,
            }
        try:
            before = self.duck.read_query(
                """SELECT stock_code, COALESCE(SUM(buyback_amount), 0) AS amount
                   FROM buyback_events GROUP BY stock_code"""
            )
            before_by_code = {row["stock_code"]: row["amount"] for row in before}
            from app.core.buyback import BuybackUpdater

            # 2026-11-13 P1（3）：必须注入更新进程共享的 adapter_mgr，否则
            # BuybackUpdater 内部会自建 AdapterManager() —— 每次调用都是全新
            # 熔断状态，东财回购源即使持续失败也永远不会熔断（其余域早已注入）。
            report = BuybackUpdater(
                duck=self.duck, sqlite=self.sqlite, adapter=self.adapter_mgr,
            ).refresh_all()
            if report.get("status") != "success":
                return report
            after = self.duck.read_query(
                """SELECT stock_code, COALESCE(SUM(buyback_amount), 0) AS amount
                   FROM buyback_events GROUP BY stock_code"""
            )
            after_by_code = {row["stock_code"]: row["amount"] for row in after}
            changed_codes = sorted({
                code for code in set(before_by_code) | set(after_by_code)
                if before_by_code.get(code) != after_by_code.get(code)
            })
            self._mark_refreshed("buyback_last_refresh")
            return {**report, "changed_codes": changed_codes}
        except Exception as error:
            logger.warning("回购明细刷新失败(非致命): %s", error)
            return {"status": "failed", "error": str(error)}

    _FINANCIAL_DETAIL_DATA_TYPES = ("balance_sheet", "income_statement", "cash_flow")
    _FINANCIAL_DETAIL_FAST_SOURCES = ("sina", "akshare_eastmoney")
    # 明细回填的字段级溯源只保留 screening/readiness 使用的核心口径；
    # 全字段逐行审计会在 source_audit（约 4000 万行）上产生数百万次
    # 索引插入，把回填速度压到 2 只/分钟。
    _FINANCIAL_DETAIL_AUDIT_FIELDS = {
        "total_assets", "total_liabilities", "total_equity",
        "total_equity_parent", "revenue", "parent_net_profit",
        "cf_from_operating", "net_profit", "deducted_net_profit",
        "total_operating_revenue", "cost_of_revenue",
    }

    def _refresh_financial_detail_backfill(
        self,
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """有界并发回填三大表明细（2026-08-31 提速版）。

        旧实现逐股串行 refetch_one：每股三次独立 DuckDB 事务提交，
        并且源缺口股会逐次落到 40s+ 的 TDX 慢回退；正式库实测约
        2 只/分钟。新实现：
        - 网络抓取并发（sina -> akshare 快速失败，不再默认走 TDX）；
        - 每 N 只合并为一个 DuckDB 事务，摊销每次约 7s 的提交开销；
        - 快速源确认无数据的股票登记 missing 并在 7 天内出队；
        - TDX 仅对每轮少量快速源缺口做补齐，不再阻塞大队列。
        """
        started = time.monotonic()
        limit = max(0, int(self.financial_detail_backfill_max_stocks_per_run))
        if limit <= 0:
            return {"status": "skipped", "reason": "backfill_disabled"}
        codes = self._financial_detail_gap_codes()
        if not codes:
            return {
                "status": "success", "targeted": 0, "succeeded": 0,
                "failed": 0, "source_missing": 0,
            }
        targets = codes[:limit]

        from app.core.init import DataInitializer

        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        succeeded_codes: list[str] = []
        failed_codes: list[str] = []
        missing_codes: list[str] = []
        buffer: list[tuple[str, dict[str, Any]]] = []
        tdx_budget = max(0, int(self.financial_detail_backfill_tdx_max_stocks_per_run))
        tdx_attempted = 0
        concurrency = max(1, min(int(self.financial_detail_backfill_concurrency), 16))
        batch_size = max(1, int(self.financial_detail_backfill_persist_batch_size))

        def persist_buffer(items: list[tuple[str, dict[str, Any]]]) -> None:
            nonlocal succeeded_codes, failed_codes
            if not items:
                return
            try:
                with self.duck.transaction() as conn:
                    for code, fetched in items:
                        self._write_financial_detail_trio(
                            initializer, conn, code, fetched,
                        )
            except Exception as error:
                logger.warning(
                    "财务明细批量事务失败，退化为逐股提交: %s", error,
                )
                for code, fetched in items:
                    try:
                        with self.duck.transaction() as conn:
                            self._write_financial_detail_trio(
                                initializer, conn, code, fetched,
                            )
                    except Exception as single_error:
                        failed_codes.append(code)
                        logger.warning(
                            "财务明细回填 %s 写入失败: %s", code, single_error,
                        )
                        continue
                    succeeded_codes.append(code)
                    self._record_financial_detail_sector_missing(
                        initializer, code, fetched,
                    )
                    self._resolve_financial_detail_missing(code)
                return
            for code, fetched in items:
                succeeded_codes.append(code)
                self._record_financial_detail_sector_missing(
                    initializer, code, fetched,
                )
                self._resolve_financial_detail_missing(code)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(self._fetch_financial_detail_trio_fast, code): code
                for code in targets
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                code = futures[future]
                try:
                    fetched, errors = future.result()
                except Exception as error:
                    fetched, errors = {}, {
                        data_type: str(error)
                        for data_type in self._FINANCIAL_DETAIL_DATA_TYPES
                    }
                if errors and tdx_attempted < tdx_budget:
                    tdx_attempted += 1
                    for data_type in list(errors):
                        tdx_result = self._fetch_financial_detail_tdx(
                            code, data_type,
                        )
                        if tdx_result is not None:
                            fetched[data_type] = tdx_result
                            errors.pop(data_type, None)
                if errors:
                    missing_codes.append(code)
                    self._record_financial_detail_missing(
                        code, "; ".join(
                            f"{data_type}: {reason}"
                            for data_type, reason in errors.items()
                        )[:400],
                    )
                else:
                    buffer.append((code, fetched))
                    if len(buffer) >= batch_size:
                        persist_buffer(buffer)
                        buffer.clear()
                if detail_cb is not None:
                    detail_cb("financial_detail_backfill", {
                        "step": "financial_detail_backfill",
                        "label": "财务明细回填",
                        "done": completed,
                        "total": len(targets),
                        "current": code,
                        "succeeded": len(succeeded_codes),
                        "failed": len(failed_codes),
                        "source_missing": len(missing_codes),
                    })
            if buffer:
                persist_buffer(buffer)

        return {
            "status": "partial" if failed_codes else "success",
            "targeted": len(targets),
            "succeeded": len(succeeded_codes),
            "failed": len(failed_codes),
            "source_missing": len(missing_codes),
            "succeeded_codes": succeeded_codes,
            "failed_codes": failed_codes[:20],
            "missing_codes": missing_codes[:20],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rate_per_minute": round(
                len(targets) * 60 / max(time.monotonic() - started, 0.001), 2
            ),
        }

    def _fetch_financial_detail_trio_fast(
        self, stock_code: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """并发抓取三表，只走快速源；失败原因返回给调用方决定 TDX 兜底。"""
        fetched: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for data_type in self._FINANCIAL_DETAIL_DATA_TYPES:
            result = self.adapter_mgr.fetch_with_sources(
                FetchRequest(data_type=data_type, stock_codes=[stock_code]),
                list(self._FINANCIAL_DETAIL_FAST_SOURCES),
            )
            if result.metadata.error or not result.data:
                errors[data_type] = result.metadata.error or "source_empty"
            else:
                fetched[data_type] = result
        return fetched, errors

    def _fetch_financial_detail_tdx(
        self, stock_code: str, data_type: str,
    ) -> Any | None:
        """TDX 兜底只由调用方有预算地触发，避免每个源缺口股都等 40s+。"""
        try:
            result = self.adapter_mgr.fetch_with_sources(
                FetchRequest(data_type=data_type, stock_codes=[stock_code]),
                ["tdx"],
            )
        except Exception as error:
            logger.warning("TDX 财务明细兜底失败 %s %s: %s", stock_code, data_type, error)
            return None
        if result.metadata.error or not result.data:
            return None
        return result

    @staticmethod
    def _write_sina_financial_table(
        initializer: Any, conn: Any, data_type: str, stock_code: str,
        result: Any,
    ) -> None:
        """以 TEMP TABLE + UPDATE/INSERT 合并一批 Sina 标准化明细行。

        与逐行 legacy merge 等价：源返回的非 NULL 字段覆盖旧值，源未返回
        的字段保留旧值，新报告期直接插入。每表只执行一次 UPDATE 和一次
        INSERT，避免逐行 DELETE+INSERT 在宽表上反复重写 row group。

        **P0-6 守卫（2026-11-17）**：这条路径是保险股
        `contract_liabilities` 的**实际来源**（`source_audit` 记
        `sina-getFinanceReport2022-1`）。合并前逐行过
        `financial_guards.guard_contract_liabilities_write()`：保险股的
        「保险合同负债」被摘出本批、显式置 NULL 并改道
        `financial_sector_items.insurance_contract_liability`。旧实现只有
        `supplement.eastmoney_f10` 那条管线有守卫，Sina 这条路完全裸奔。
        """
        column_cache, industry_cache = guard_caches(initializer)
        available = initializer._financial_cols_cache.get(data_type)
        if available is None:
            available = {
                row[0]
                for row in conn.execute(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{data_type}'"
                ).fetchall()
            }
            initializer._financial_cols_cache[data_type] = available
        rows: list[dict[str, Any]] = []
        for row in result.data:
            mapped = {
                key: value
                for key, value in row.items()
                if key in available and value is not None
            }
            mapped.setdefault("stock_code", stock_code)
            # 守卫必须作用在"即将进入 TEMP TABLE 的映射"上：被拒的行整列
            # 不出现在本批，插入/更新时该列即 NULL（脏值另由守卫显式清空）。
            if data_type == BALANCE_SHEET_TABLE and CONTRACT_LIABILITIES_COLUMN in mapped:
                guard_contract_liabilities_write(
                    conn,
                    stock_code=stock_code,
                    values=mapped,
                    column_cache=column_cache,
                    industry_cache=industry_cache,
                )
            if "raw_data" in available:
                mapped["raw_data"] = json.dumps(
                    row, ensure_ascii=False, default=str,
                )
            rows.append(mapped)
        if not rows:
            return
        fields = sorted({key for mapped in rows for key in mapped if key in available})
        if "stock_code" not in fields:
            fields.insert(0, "stock_code")
        if "report_date" not in fields:
            fields.insert(1, "report_date")
        temp = f"tmp_{data_type}_{uuid.uuid4().hex[:10]}"
        conn.execute(
            f"CREATE TEMP TABLE {temp} AS "
            f"SELECT * FROM {data_type} WHERE 1 = 0"
        )
        placeholders = ", ".join("?" for _ in fields)
        conn.executemany(
            f"INSERT INTO {temp} ({', '.join(fields)}) "
            f"VALUES ({placeholders})",
            [[mapped.get(field) for field in fields] for mapped in rows],
        )
        updates = ", ".join(
            f"{field} = s.{field}"
            for field in fields
            if field not in {"stock_code", "report_date"}
        )
        conn.execute(
            f"UPDATE {data_type} t SET {updates} FROM {temp} s "
            f"WHERE t.stock_code = s.stock_code "
            f"AND t.report_date = s.report_date"
        )
        source_fields = ", ".join(f"s.{field}" for field in fields)
        conn.execute(
            f"INSERT INTO {data_type} ({', '.join(fields)}) "
            f"SELECT {source_fields} FROM {temp} s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {data_type} t "
            f"WHERE t.stock_code = s.stock_code "
            f"AND t.report_date = s.report_date)"
        )
        conn.execute(f"DROP TABLE {temp}")

    @staticmethod
    def _write_financial_detail_trio(
        initializer: Any, conn: Any, stock_code: str, fetched: dict[str, Any],
    ) -> None:
        """在调用方事务内写入一只股票的三表 + lineage。

        2026-09-01 原始响应归档已冷热分层，内存峰值不再由 raw_response_archive
        决定；Sina 批量行改用 TEMP TABLE 合并，减少逐行 DELETE+INSERT 的
        row-group 重写。非 Sina 兜底仍走逐行 upsert 的通用映射。
        """
        for data_type in IncrementalUpdater._FINANCIAL_DETAIL_DATA_TYPES:
            result = fetched.get(data_type)
            if result is None or not result.data:
                continue
            if result.metadata.source == "sina":
                IncrementalUpdater._write_sina_financial_table(
                    initializer, conn, data_type, stock_code, result,
                )
            else:
                for row in result.data:
                    initializer._upsert_financial_row(
                        conn, data_type, stock_code, row,
                    )
        for data_type in IncrementalUpdater._FINANCIAL_DETAIL_DATA_TYPES:
            result = fetched.get(data_type)
            if result is None or not result.data:
                continue
            batch_id = initializer._record_batch_in_connection(
                conn, result, data_type, len(result.data),
            )
            latest_rows = [
                max(
                    result.data,
                    key=lambda row: str(row.get("report_date") or ""),
                )
            ]
            initializer._record_field_audit_in_connection(
                conn, result, latest_rows, stock_code, "report_date", batch_id,
                field_whitelist=IncrementalUpdater._FINANCIAL_DETAIL_AUDIT_FIELDS,
            )

    def _record_financial_detail_sector_missing(
        self, initializer: Any, stock_code: str, fetched: dict[str, Any],
    ) -> None:
        balance = fetched.get("balance_sheet")
        if balance is not None and balance.data:
            initializer._record_missing_financial_sector_fields(
                stock_code, balance.data,
            )

    def _record_financial_detail_missing(self, stock_code: str, reason: str) -> None:
        try:
            self.sqlite.execute(
                """INSERT INTO missing_list (stock_code, field_name, reason_code)
                   VALUES (?, 'financial_detail_backfill', ?)
                   ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                   DO UPDATE SET reason_code = excluded.reason_code,
                                 detected_at = CURRENT_TIMESTAMP""",
                [stock_code, (reason or "source_unavailable")[:400]],
            )
        except Exception as error:
            logger.warning("记录财务明细源缺口失败 %s: %s", stock_code, error)

    def _resolve_financial_detail_missing(self, stock_code: str) -> None:
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = 'financial_detail_backfill'
                     AND resolved_at IS NULL""",
                [datetime.now(UTC).isoformat(), stock_code],
            )
        except Exception as error:
            logger.warning("解决财务明细源缺口失败 %s: %s", stock_code, error)
    def _refresh_financials(
        self,
        stock_codes: list[str],
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh core statements for stocks with newly registered filings.

        混合批量：并发抓取，每满 financial_refresh_batch_size 只合并一个
        DuckDB 事务；整批失败退化为逐股单事务。
        """
        succeeded = 0
        succeeded_codes: list[str] = []
        pending_codes: list[str] = []
        failed: list[str] = []
        completed = 0
        batch_size = max(1, getattr(self, "financial_refresh_batch_size", 20))
        batch_items: list[tuple[str, list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]] = []

        def refresh_one(code: str) -> tuple[str, list[dict[str, Any]], dict[str, tuple[Any, list[dict[str, Any]]]]]:
            try:
                outcomes, fetched = self._fetch_financial_trio(code)
            except Exception as error:
                outcomes = [
                    {"status": "failed", "error": str(error), "data_type": data_type}
                    for data_type in ("balance_sheet", "income_statement", "cash_flow")
                ]
                fetched = {}
            return code, outcomes, fetched

        expected_period = self._current_expected_financial_period()

        def _latest_period(outcome: dict[str, Any]) -> str:
            # 非 skipped：取本轮实际写入的最新报告期；skipped：取本地已有最新期。
            return str(
                outcome.get("latest_fetched") or outcome.get("latest_local") or ""
            )[:10]

        def finish_one(code: str, outcomes: list[dict[str, Any]]) -> None:
            nonlocal succeeded, completed
            completed += 1
            if all(outcome["status"] == "success" for outcome in outcomes):
                local_dates = [_latest_period(outcome) for outcome in outcomes]
                # 三张报表都达到当前应发布报告期 → 公告目的已达成，标记成功
                # 入册。任何一张表仍停留在旧报告期（源侧延迟/部分成功）都
                # 保持 pending，下一轮继续重试；绝不能把"只有资产负债表
                # 更新了半年报"误判为成功并把公告永久标记 seen。
                if local_dates and all(d >= expected_period for d in local_dates):
                    succeeded += 1
                    succeeded_codes.append(code)
                else:
                    pending_codes.append(code)
            else:
                failed.append(code)
            if detail_cb is not None:
                detail_cb("financials", {
                    "step": "financials",
                    "label": "财务数据",
                    "done": completed,
                    "total": len(stock_codes),
                    "current": code,
                })

        concurrency = max(1, getattr(self, "financial_refresh_concurrency", 8))
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="vd-financial-refresh",
        ) as executor:
            futures = {executor.submit(refresh_one, code): code for code in stock_codes}
            for future in as_completed(futures):
                code, outcomes, fetched = future.result()
                if fetched:
                    batch_items.append((code, outcomes, fetched))
                    if len(batch_items) >= batch_size:
                        self._persist_financial_batch(batch_items)
                        for item_code, item_outcomes, _ in batch_items:
                            finish_one(item_code, item_outcomes)
                        batch_items.clear()
                else:
                    finish_one(code, outcomes)
        if batch_items:
            self._persist_financial_batch(batch_items)
            for item_code, item_outcomes, _ in batch_items:
                finish_one(item_code, item_outcomes)
            batch_items.clear()
        return {
            "status": "success" if not failed else "partial",
            "total": len(stock_codes), "success": succeeded, "failed": len(failed),
            "failed_codes": failed[:20], "succeeded_codes": succeeded_codes,
            "pending_codes": pending_codes[:20],
        }

    _MARKET_ACTION_DATA_TYPES = ("dividends", "xdxr")

    def _refresh_market_actions(
        self,
        stock_codes: list[str],
        *,
        detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh dividend and corporate-action evidence alongside filing data.

        2026-10-20 提速：原先逐股 ``refetch_one`` 会为每只股票新建一次 TDX
        连接（``from_best_host`` 实测约 5s）并单独提交 fetch_batch/审计，
        535 只股票耗时约 76 分钟。现在按批在同一会话内抓取、每批只做一次
        事务与一次批量审计写入；批内缺席或整批失败的股票才回退逐股，
        失败仍精确归因并进入 retry_list。
        """
        codes = list(dict.fromkeys(stock_codes))
        total_codes = len(codes)
        batch_size = max(1, int(getattr(self, "market_actions_batch_size", 50)))
        succeeded = 0
        failed_codes: list[str] = []
        skipped_codes: list[str] = []
        failures: dict[str, list[str]] = {}
        processed = 0  # 进度计数（含被跳过的 B 股，见下）
        # 2026-11-13 P1（E）：B 股没有该源，直接如实登记 missing 并撤销历史 retry，
        # 不抓取、不计失败、不占重试次数。否则 900948/xdxr 会以 retry_exhausted
        # 的假名义永久进 missing_list（公告侧早就有同样的跳过通道）。
        b_share_codes = [code for code in codes if self._is_b_share_stock(code)]
        if b_share_codes:
            for code in b_share_codes:
                for data_type in self._MARKET_ACTION_DATA_TYPES:
                    self._record_missing(code, data_type, "source_unsupported")
                with contextlib.suppress(Exception):
                    self.sqlite.execute(
                        "DELETE FROM retry_list WHERE stock_code = ? "
                        "AND data_type IN (?, ?)",
                        [code, *self._MARKET_ACTION_DATA_TYPES],
                    )
            logger.info(
                "[增量] %d 只 B 股无分红/除权源，已登记 missing(source_unsupported): %s",
                len(b_share_codes), ", ".join(b_share_codes[:20]),
            )
            skipped_codes.extend(b_share_codes)
            codes = [code for code in codes if code not in set(b_share_codes)]
        processed = len(b_share_codes)
        new_event_codes: set[str] = set()
        for offset in range(0, len(codes), batch_size):
            chunk = codes[offset:offset + batch_size]
            chunk_failures, chunk_skipped, chunk_events = self._refresh_market_actions_chunk(chunk)
            new_event_codes.update(chunk_events)
            for code in chunk:
                processed += 1
                code_errors = chunk_failures.get(code) or []
                if code in chunk_skipped:
                    # 源明确不支持：已登记 missing(source_unsupported)，不算成功
                    # 也不算失败，更不写 retry（契约 §1）。
                    skipped_codes.append(code)
                elif code_errors:
                    failed_codes.append(code)
                    failures[code] = code_errors
                    for message in code_errors:
                        data_type, _, detail = message.partition(": ")
                        self._record_failure(
                            code, data_type or "dividends", "manager",
                            detail or message,
                        )
                else:
                    succeeded += 1
                if detail_cb is not None:
                    detail_cb("market_actions", {
                        "step": "market_actions",
                        "label": "分红除权",
                        "done": processed,
                        "total": total_codes,
                        "current": code,
                    })
        # 2026-11-13 P0（B）：本次新写入除权事件的股票进入"待重拉 qfq"集合，
        # 与日期比较无关——这样即使事件晚于价格推进写入也能强制全历史重拉。
        if new_event_codes:
            self._mark_qfq_refetch_pending(sorted(new_event_codes))
        return {
            "status": "success" if not failed_codes else "partial",
            "total": total_codes, "success": succeeded, "failed": len(failed_codes),
            "skipped": len(skipped_codes),
            "skipped_codes": skipped_codes[:20],
            "failed_codes": failed_codes[:20],
            "errors": {code: failures[code] for code in failed_codes[:20]},
            "new_xdxr_event_codes": sorted(new_event_codes)[:20],
            "qfq_refetch_pending": len(self._load_qfq_refetch_pending()),
            "batch_size": batch_size,
        }

    def _settle_skipped_codes(
        self, data_type: str, skipped: dict[str, str],
    ) -> None:
        """Record codes the source explicitly cannot serve (contract §1).

        ``SourceMetadata.skipped_codes`` 是源"明确不支持"的股票（如 BSE 无东财
        分红/除权交叉源）。它们必须直接进 missing_list，**不得**写进 error
        字符串、不得进 retry——601123/603435 的历史 ``无可用适配器 (tried: [])``
        就是这样零尝试假失败、5 轮后以 retry_exhausted 永久消失的。
        """
        for code, reason in skipped.items():
            self._record_missing(str(code), data_type, "source_unsupported")
            logger.info(
                "[增量] %s %s 源明确不支持，登记为缺失: %s",
                code, data_type, reason or "source_unsupported",
            )
            # 已被误记为 retry 的同一请求顺带撤销（不占重试次数）。
            with contextlib.suppress(Exception):
                self.sqlite.execute(
                    "DELETE FROM retry_list WHERE stock_code = ? AND data_type = ?",
                    [str(code), data_type],
                )

    def _refresh_market_actions_chunk(
        self, codes: list[str],
    ) -> tuple[dict[str, list[str]], set[str], set[str]]:
        """Fetch+persist dividends/xdxr for one batch.

        Returns ``(per_stock_errors, source_skipped_codes, new_xdxr_event_codes)``.
        第二个是源明确不支持的股票（契约 §1），已直接登记 missing，不得再走
        逐股回退/重试；第三个是**本次真正写入了除权事件**（新增或数值修正）的
        股票，供 qfq 强制全历史重拉（2026-11-13 P0 B）。
        """
        errors: dict[str, list[str]] = {}
        skipped_codes: set[str] = set()
        new_event_codes: set[str] = set()
        for data_type in self._MARKET_ACTION_DATA_TYPES:
            before_events = (
                self._xdxr_event_fingerprints(codes) if data_type == "xdxr" else None
            )
            grouped, result = self._fetch_market_action_batch(data_type, codes)
            if result is None:
                # 整批无结果 → 逐股回退（可能仍会写入部分除权事件，所以下面
                # 的 before/after 指纹比对不能因为这里 break 而跳过）。
                self._fallback_market_actions(codes, data_type, errors)
            else:
                skipped = _source_skipped_codes(result)
                if skipped:
                    self._settle_skipped_codes(data_type, skipped)
                    skipped_codes.update(skipped)
                persist_errors = self._persist_market_action_batch(
                    data_type, result, grouped,
                )
                for code in codes:
                    if code in skipped:
                        # 源明确不支持：零尝试假失败比真故障更糟，直接跳过。
                        continue
                    if code in persist_errors or not grouped.get(code):
                        # 批内没有该股记录、或该股行在批量事务里落库失败：逐股
                        # 复核以保留精确归因（正常应极少，只作为兜底路径）。
                        self._fallback_market_actions([code], data_type, errors)
            if before_events is not None:
                after_events = self._xdxr_event_fingerprints(codes)
                new_event_codes.update(
                    {key[0] for key in after_events - before_events}
                )
        return errors, skipped_codes, new_event_codes

    def _fetch_market_action_batch(
        self, data_type: str, codes: list[str],
    ) -> tuple[dict[str, list[dict[str, Any]]], Any | None]:
        """One adapter call for the whole batch; rows grouped by stock_code."""
        try:
            result = self.adapter_mgr.fetch(FetchRequest(
                data_type=data_type, stock_codes=list(codes),
            ))
        except Exception as error:
            logger.warning("批量抓取 %s 失败，回退逐股: %s", data_type, error)
            return {}, None
        if result.metadata.error and not result.data:
            logger.warning(
                "批量抓取 %s 无数据，回退逐股: %s", data_type, result.metadata.error,
            )
            return {}, None
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in result.data or []:
            code = str(row.get("stock_code") or "")
            if not code:
                continue
            grouped.setdefault(code, []).append(row)
        return grouped, result

    def _persist_market_action_batch(
        self, data_type: str, result: Any,
        grouped: dict[str, list[dict[str, Any]]],
    ) -> dict[str, str]:
        """Persist one batch in a single transaction; return per-stock errors."""
        from app.core.init import DataInitializer

        errors: dict[str, str] = {}
        pairs: list[tuple[str, list[dict[str, Any]]]] = []
        for code, rows in grouped.items():
            if data_type == "xdxr":
                valid = [
                    row for row in rows
                    if row.get("event_date") is not None
                    and row.get("category") is not None
                ]
            else:
                valid = [row for row in rows if row.get("ex_date")]
            if not valid:
                # 单只股票行不可用时不拖垮整批：交给逐股路径精确报错。
                errors[code] = "no valid rows in batch response"
                continue
            pairs.append((code, valid))
        if not pairs:
            return errors
        initializer = DataInitializer(
            duck=self.duck, sqlite=self.sqlite, adapter_mgr=self.adapter_mgr,
        )
        report_field = "event_date" if data_type == "xdxr" else "report_date"
        try:
            with self.duck.transaction() as connection:
                batch_id = initializer._record_batch_in_connection(
                    connection, result, data_type,
                    sum(len(rows) for _, rows in pairs),
                )
                for code, rows in pairs:
                    if data_type == "xdxr":
                        initializer._upsert_xdxr_rows(connection, code, rows)
                    else:
                        initializer._upsert_dividend_rows(connection, code, rows)
                initializer._record_field_audit_bulk_in_connection(
                    connection, result, pairs, report_field, batch_id,
                )
        except Exception as error:
            logger.warning("批量落库 %s 失败，回退逐股: %s", data_type, error)
            for code, _ in pairs:
                errors[code] = str(error)
        return errors

    def _fallback_market_actions(
        self, codes: list[str], data_type: str,
        errors: dict[str, list[str]],
    ) -> None:
        """Per-stock fallback so failures keep precise attribution."""
        for code in codes:
            outcome = self.refetch_one(code, data_type)
            if outcome.get("status") != "success":
                errors.setdefault(code, []).append(
                    f"{data_type}: {outcome.get('error') or 'failed'}"
                )

    def _settle_non_retryable_retries(self) -> int:
        """批量结算 legal_empty / source_unsupported（实现已搬到 ``app/core/update_parts/retry.py``）。"""
        return settle_non_retryable_retries(self.sqlite, logger=logger)

    def _check_retry_tasks(self) -> list[dict]:
        """读取待重试任务列表（实现已搬到 ``app/core/update_parts/retry.py``）。"""
        return check_retry_tasks(self.sqlite, self._settle_non_retryable_retries, logger=logger)

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

    @staticmethod
    def _local_price_window_start(stock: dict[str, Any], fallback_start: str) -> str:
        """Per-stock lower bound for the xdxr lookback window.

        取该股**自己**的 ``min(latest_raw_date, latest_qfq_date)``：只要有一侧
        停在旧日期（典型：qfq 陈旧），更晚的除权事件就必须被看见，否则 qfq
        永远不会全历史重拉。两侧都没有本地日期时回退 ``fallback_start``。
        """
        raw = str(stock.get("latest_raw_date") or "")[:10]
        qfq = str(stock.get("latest_qfq_date") or "")[:10]
        if raw and qfq:
            return min(raw, qfq)
        return raw or qfq or fallback_start

    def _get_xdxr_codes_since(
        self,
        start_date: str,
        *,
        stock_codes: list[str] | None = None,
        stock_windows: dict[str, str | None] | None = None,
    ) -> set[str]:
        """Get stock codes with corporate-action events inside their own window.

        前复权（qfq）价格以最新除权为基础重算历史，因此发生除权除息后，
        该股票的全部 qfq 历史都需要重拉（PRD §7.7 复权一致性）。

        2026-11-12 P0：窗口必须**按股票**计算。旧实现把全市场
        ``min(latest_raw_date/latest_qfq_date)`` 当唯一起点——一只 qfq 停在
        2015 年的陈旧股就把全体拖进全历史重拉，``xdxr_full_refetch`` 从 32
        涨到 293，价格步骤 143.5 分钟。现在一条 JOIN SQL 完成：
        ``xdxr.event_date >= 该股自己的窗口起点``，无本地日期的股票回退
        ``start_date``（``stock_codes`` 模式下由 SQL 自行求本地日期）。

        Args:
            start_date: 兜底窗口起点（YYYY-MM-DD），用于无任何本地价格的股票。
            stock_codes: 只在这些股票范围内判定（应当是被本轮抓取的目标集）。
            stock_windows: 预先算好的 {code: 窗口起点}；给出时避免再扫一遍
                17M 行价格表（``_update_prices_incremental`` 已在手上）。
        """
        try:
            if stock_windows:
                codes = [str(code) for code in stock_windows]
                windows = [
                    str(stock_windows[code] or start_date)[:10] for code in codes
                ]
                rows = self.duck.read_query(
                    """WITH target AS (
                           SELECT unnest(?::VARCHAR[]) AS stock_code,
                                  unnest(?::VARCHAR[]) AS window_start
                       )
                       SELECT DISTINCT action.stock_code AS stock_code
                       FROM xdxr action
                       JOIN target ON target.stock_code = action.stock_code
                       WHERE action.event_date >= CAST(target.window_start AS DATE)
                       ORDER BY action.stock_code""",
                    [codes, windows],
                )
            elif stock_codes:
                rows = self.duck.read_query(
                    """WITH target AS (
                           SELECT unnest(?::VARCHAR[]) AS stock_code
                       ), local AS (
                           SELECT target.stock_code,
                                  CASE
                                      WHEN raw.latest_raw_date IS NOT NULL
                                       AND qfq.latest_qfq_date IS NOT NULL
                                      THEN LEAST(raw.latest_raw_date, qfq.latest_qfq_date)
                                      ELSE COALESCE(
                                          raw.latest_raw_date, qfq.latest_qfq_date,
                                          CAST(? AS DATE)
                                      )
                                  END AS window_start
                           FROM target
                           LEFT JOIN (
                               SELECT stock_code, MAX(trade_date) AS latest_raw_date
                               FROM price_daily_raw GROUP BY stock_code
                           ) raw ON raw.stock_code = target.stock_code
                           LEFT JOIN (
                               SELECT stock_code, MAX(trade_date) AS latest_qfq_date
                               FROM price_daily_qfq GROUP BY stock_code
                           ) qfq ON qfq.stock_code = target.stock_code
                       )
                       SELECT DISTINCT action.stock_code AS stock_code
                       FROM xdxr action
                       JOIN local ON local.stock_code = action.stock_code
                       WHERE action.event_date >= local.window_start
                       ORDER BY action.stock_code""",
                    [list(stock_codes), start_date],
                )
            else:
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
        """增量更新价格——只更新有新数据的股票（实现已搬到 ``update_parts/prices_step.py``）。

        依据 S12/S13（reports/140 §2.5 拆分的第 4 块）：选股 / 抓取管道 / 结算已切成
        三段放进 ``app.core.update_parts.prices_step``，本方法只留同名委托——签名、
        返回值、异常与日志通道一字不改。三个"只有一份实现"的依赖仍从本模块注入：

        * ``logger``：仍是本模块的 logger（日志通道不变）；
        * ``source_error_kind``：仍是本模块的 ``_source_error_kind``（契约 §1 唯一实现）；
        * ``clock``：仍是**本模块**的 ``time.monotonic()``，运行期查找，
          ``test_price_batch_flush.py`` 对 ``update.time`` 的 monkeypatch 语义逐字保留
          （攒批缓冲的时间判据也走这个时钟）。
        """
        return prices_step.update_prices_incremental(
            self,
            max_stocks,
            detail_cb,
            logger=logger,
            source_error_kind=_source_error_kind,
            clock=lambda: time.monotonic(),
        )

    def _load_qfq_catchup_attempts(self) -> dict[str, dict[str, Any]]:
        """读 `data_refresh_state['price_qfq_catchup_attempts']`（实现见 `prices_step`）。"""
        return prices_step.load_qfq_catchup_attempts(self)

    def _mark_qfq_catchup_attempts(
        self,
        watermarks: dict[str, tuple[str | None, str | None]],
        *,
        on_date: str,
    ) -> int:
        """记"本交易日以该 `(raw, qfq)` 水位成功落库"（实现见 `prices_step`）。"""
        return prices_step.mark_qfq_catchup_attempts(self, watermarks, on_date=on_date)

    def _raw_bars_beyond_qfq(self, codes: Iterable[str]) -> dict[str, int]:
        """每只股票自己的 raw K 线里排在 qfq 最新日之后的根数（实现见 `prices_step`）。"""
        return prices_step.raw_bars_beyond_qfq(self, codes, logger=logger)

    def _latest_expected_trading_date(self, today: str, *, now: datetime | None = None) -> str:
        """Use the latest closed session, not today's still-open trading date."""
        local_now = now or cn_now()
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

    _PRICE_FULL_REPLACE_THRESHOLD = 200
    _PRICE_FULL_REPLACE_MIN_RATIO = 0.5
    # 全历史替换攒批上限（2026-11-12 P0 ②）：只数 / 行数 / 存活秒数任一超限
    # 即刻落盘。8 只 ≈ 最坏 16 份全历史响应（约 4 万行）；5 秒保证慢网络下
    # 缓冲不会长期滞留；20 万行是 17M 行表的 ~1%，绝不会把整表读进内存。
    _PRICE_WRITE_BUFFER_STOCKS = 8
    _PRICE_WRITE_BUFFER_ROWS = 200_000
    _PRICE_WRITE_BUFFER_SECONDS = 5.0
    _PRICE_INSERT_COLUMNS = (
        "stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate"
    )
    _PRICE_INSERT_PLACEHOLDERS = "(?, ?, ?, ?, ?, ?, ?, ?, ?)"

    @staticmethod
    def _price_row_values(stock_code: str, result: Any) -> list[list[Any]]:
        """Row payload for one price table — single source of truth for the shape."""
        return [
            [stock_code, row.get("trade_date"), row.get("open"), row.get("high"),
             row.get("low"), row.get("close"), row.get("volume"), row.get("turnover"),
             row.get("turnover_rate")]
            for row in result.data
        ]

    def _assert_price_full_replace_safe(
        self, connection: Any, table: str, data_type: str, stock_code: str, result: Any,
    ) -> None:
        """Refuse a full-history replace that would silently drop local history."""
        local_rows = connection.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE stock_code = ?",
            [stock_code],
        ).fetchone()
        local_count = int(local_rows[0]) if local_rows else 0
        if len(result.data) < local_count * self._PRICE_FULL_REPLACE_MIN_RATIO:
            raise ValueError(
                f"{stock_code} {data_type} full replace truncated "
                f"({len(result.data)} remote vs {local_count} local); keeping old data"
            )
        # 全历史替换还必须覆盖本地最老日期；源侧截断为"最近 N 年"
        # 时不得把更早的历史静默删掉（2026-09-08 红队 P0-1 防线）。
        remote_dates = [
            str(row.get("trade_date") or "")[:10]
            for row in result.data
            if str(row.get("trade_date") or "")[:10]
        ]
        if not remote_dates:
            raise ValueError(
                f"{stock_code} {data_type} full replace has no trade_date rows"
            )
        local_min_row = connection.execute(
            f"SELECT MIN(trade_date) AS earliest FROM {table} WHERE stock_code = ?",
            [stock_code],
        ).fetchone()
        local_min = str(local_min_row[0])[:10] if local_min_row and local_min_row[0] else ""
        if local_min and min(remote_dates) > local_min:
            raise ValueError(
                f"{stock_code} {data_type} full replace would drop history "
                f"before {min(remote_dates)} (local earliest {local_min}); keeping old data"
            )

    def _insert_price_rows(
        self, connection: Any, table: str, stock_code: str, result: Any, *,
        full_replace: bool,
    ) -> None:
        """Insert one stock's rows; full-history replies replace, incremental upsert."""
        rows = self._price_row_values(stock_code, result)
        if full_replace:
            connection.executemany(
                f"INSERT INTO {table} ({self._PRICE_INSERT_COLUMNS}) "
                f"VALUES {self._PRICE_INSERT_PLACEHOLDERS}",
                rows,
            )
            return
        connection.executemany(
            f"INSERT INTO {table} ({self._PRICE_INSERT_COLUMNS}) "
            f"VALUES {self._PRICE_INSERT_PLACEHOLDERS} "
            "ON CONFLICT(stock_code, trade_date) DO UPDATE SET "
            f"open=COALESCE(excluded.open, {table}.open), "
            f"high=COALESCE(excluded.high, {table}.high), "
            f"low=COALESCE(excluded.low, {table}.low), "
            f"close=COALESCE(excluded.close, {table}.close), "
            f"volume=COALESCE(excluded.volume, {table}.volume), "
            f"turnover=COALESCE(excluded.turnover, {table}.turnover), "
            f"turnover_rate=COALESCE(excluded.turnover_rate, {table}.turnover_rate)",
            rows,
        )

    def _record_price_lineage_in_connection(
        self, connection: Any, lineage: Any, stock_code: str, table: str,
        data_type: str, result: Any, *, full_field_audit: bool = False,
    ) -> None:
        """Persist batch lineage plus the per-stock ``latest_close`` audit row."""
        batch_id = lineage._record_batch_in_connection(
            connection, result, data_type, len(result.data)
        )
        if full_field_audit:
            lineage._record_field_audit_in_connection(
                connection, result, result.data, stock_code, "trade_date", batch_id,
            )
        latest_row = max(
            result.data,
            key=lambda row: str(row.get("trade_date") or ""),
        )
        latest_close = latest_row.get("close")
        if isinstance(latest_close, (int, float)):
            connection.execute(
                """INSERT INTO source_audit
                   (stock_code, field_name, report_date, value, source,
                    fetch_batch_id, fetch_time, raw_response_hash, confidence,
                    reason_code, api_version)
                   VALUES (?, 'latest_close', ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                [
                    stock_code,
                    latest_row.get("trade_date"),
                    latest_close,
                    result.metadata.source,
                    batch_id,
                    result.metadata.fetch_time,
                    result.metadata.raw_response_hash,
                    result.metadata.confidence,
                    result.metadata.api_version,
                ],
            )
        # 价格行数极大（全历史重拉单只可达数千行），逐值审计会把
        # source_audit 放大成千万行级且非 PRD §14 关键财务字段所需。
        # 价格采用 batch 级溯源，并仅审计每股最新收盘价；财务/股本等
        # 关键字段仍走逐值审计。

    def _persist_price_pair_in_connection(
        self, connection: Any, stock_code: str, raw_result: Any, qfq_result: Any,
        *, full_replace_raw: bool = False, full_replace_qfq: bool = False,
        full_field_audit: bool = False,
    ) -> None:
        """Write one stock's raw/qfq rows + batch lineage inside a caller transaction.

        DuckDB's ON CONFLICT DO UPDATE is linear in the target table size
        (benchmarked ~4-5s per 2000 rows on a 1M-row table), so full-history
        responses on the 17M-row formal table stall the writer for a minute
        per stock. Large responses use an in-transaction delete+insert
        replace instead, guarded against truncated sources by a row-count
        ratio check. Incremental responses keep the conflict-safe upsert.
        Raw and qfq are judged independently: one side may be a small
        incremental while the other is a full-history replacement.
        """
        from app.core.init import DataInitializer

        lineage = DataInitializer.__new__(DataInitializer)
        lineage._batch_id = str(uuid.uuid4())
        for table, data_type, result, full_replace in (
            ("price_daily_raw", "price_daily_raw", raw_result, full_replace_raw),
            ("price_daily_qfq", "price_daily_qfq", qfq_result, full_replace_qfq),
        ):
            if result is None:
                continue
            if full_replace:
                self._assert_price_full_replace_safe(
                    connection, table, data_type, stock_code, result,
                )
                connection.execute(
                    f"DELETE FROM {table} WHERE stock_code = ?", [stock_code]
                )
            self._insert_price_rows(
                connection, table, stock_code, result, full_replace=full_replace,
            )
            self._record_price_lineage_in_connection(
                connection, lineage, stock_code, table, data_type, result,
                full_field_audit=full_field_audit,
            )

    def _persist_price_replace_group_in_connection(
        self, connection: Any, items: list[tuple[str, Any, Any, bool, bool]],
    ) -> None:
        """Replace N stocks' full history inside ONE transaction (grouped writes).

        2026-11-12 P0（性能）：全历史响应原先逐股独立事务，每只一次 WAL 提交，
        正式库 17M 行表上单股可达 10~159s，日志 144 处 ≥10s 全静默、共
        96.5 分钟，是价格步骤 143.5 分钟的主因（剔除停顿后纯抓取 118 只/分）。
        这里把 N 只并进一个事务：每张表只发一条 ``DELETE ... IN (...)``，
        再逐股 ``executemany`` 插入（仍是整段替换语义，不会退化成 upsert 追加）。

        不变量（契约要求）：
        ① 批内 DELETE+INSERT 同事务，任一股票校验/写入失败 → 整批回滚，
           旧行一条不丢；调用方随后回退逐股事务，单股原子性依旧成立。
        ② 每只股票仍然"先删后插"的整段替换，绝不留下重复/新旧混合行。
        """
        from app.core.init import DataInitializer

        lineage = DataInitializer.__new__(DataInitializer)
        lineage._batch_id = str(uuid.uuid4())
        sides = (
            ("price_daily_raw", "price_daily_raw", 3),
            ("price_daily_qfq", "price_daily_qfq", 4),
        )
        # 1) 先跑完全部截断防线：任意一只不合格就一行都不写。
        for item in items:
            stock_code, raw_result, qfq_result, raw_full, qfq_full = item
            for table, data_type, index in sides:
                full_replace = raw_full if index == 3 else qfq_full
                result = raw_result if index == 3 else qfq_result
                if full_replace and result is not None:
                    self._assert_price_full_replace_safe(
                        connection, table, data_type, stock_code, result,
                    )
        # 2) 每张表一条 DELETE ... IN (...)，把 N 次删除压成 1 次。
        for table, _data_type, index in sides:
            codes = [item[0] for item in items if item[index]]
            if not codes:
                continue
            placeholders = ", ".join("?" for _ in codes)
            connection.execute(
                f"DELETE FROM {table} WHERE stock_code IN ({placeholders})", codes,
            )
        # 3) 逐股 INSERT（整段替换）+ 溯源。
        for item in items:
            stock_code, raw_result, qfq_result, raw_full, qfq_full = item
            for table, data_type, index in sides:
                full_replace = raw_full if index == 3 else qfq_full
                result = raw_result if index == 3 else qfq_result
                if result is None:
                    continue
                self._insert_price_rows(
                    connection, table, stock_code, result, full_replace=full_replace,
                )
                self._record_price_lineage_in_connection(
                    connection, lineage, stock_code, table, data_type, result,
                )

    def _flush_price_replace_buffer(
        self,
        buffer: _PriceReplaceBuffer,
        success_count: int,
        updated_codes: list[str],
        fail_count: int,
        *,
        failed_codes: list[str] | None = None,
        before_write: Callable[[], None] | None = None,
        batch_sizes: list[int] | None = None,
    ) -> tuple[int, list[str], int]:
        """Write all buffered full-history responses in one transaction.

        ``before_write`` 在拿到写锁前调用（生产上传 ``fill_pipeline``），
        保证主线程阻塞在写库期间工作线程仍在抓取，不会把并发饿死。
        整批失败时回退逐股事务：一只坏数据不能拖累同批其它股票。
        """
        items = buffer.drain()
        if not items:
            return success_count, updated_codes, fail_count
        if batch_sizes is not None:
            batch_sizes.append(len(items))
        if before_write is not None:
            with contextlib.suppress(Exception):
                before_write()
        rows = sum(
            len(result.data or ())
            for _code, raw_result, qfq_result, raw_full, qfq_full in items
            for result, full in ((raw_result, raw_full), (qfq_result, qfq_full))
            if full and result is not None
        )
        try:
            with self.duck.transaction() as connection:
                self._persist_price_replace_group_in_connection(connection, items)
            success_count += len(items)
            updated_codes.extend(item[0] for item in items)
            # 单只落盘不得写"批量"字样：验收证据必须与真实批大小一致
            # （2026-11-13 生产日志 13 次落盘 12 次 N=1 的事故复盘要求）。
            if len(items) >= 2:
                logger.info(
                    "[增量] 价格全历史批量写入 %d 只 / %d 行（单事务）", len(items), rows,
                )
            else:
                logger.info(
                    "[增量] 价格全历史写入 1 只 / %d 行（单事务）", rows,
                )
            return success_count, updated_codes, fail_count
        except Exception as exc:
            logger.warning(
                "价格全历史批量事务失败（%d 只），已整批回滚，回退逐股事务: %s",
                len(items), exc,
            )
        for code, raw_result, qfq_result, raw_large, qfq_large in items:
            try:
                with self.duck.transaction() as connection:
                    self._persist_price_pair_in_connection(
                        connection, code, raw_result, qfq_result,
                        full_replace_raw=raw_large, full_replace_qfq=qfq_large,
                    )
                success_count += 1
                updated_codes.append(code)
            except Exception as exc:
                fail_count += 1
                if failed_codes is not None:
                    failed_codes.append(code)
                logger.warning("[增量] 价格落库失败 %s: %s", code, exc)
                self._record_failure(code, "price_daily", "manager", str(exc))
        return success_count, updated_codes, fail_count

    def _persist_incremental_price_pair(
        self, stock_code: str, raw_result: Any, qfq_result: Any, *, full_field_audit: bool = False,
    ) -> None:
        """Commit one stock's raw/qfq rows and lineage atomically for restart safety."""
        with self.duck.transaction() as connection:
            self._persist_price_pair_in_connection(
                connection, stock_code, raw_result, qfq_result, full_field_audit=full_field_audit,
            )

    def _persist_price_batch(
        self,
        batch: list[tuple[str, Any, Any, bool, bool]],
        success_count: int,
        updated_codes: list[str],
        fail_count: int,
        *,
        buffer: _PriceReplaceBuffer | None = None,
        flush_buffer: bool = False,
        failed_codes: list[str] | None = None,
        before_write: Callable[[], None] | None = None,
        batch_sizes: list[int] | None = None,
    ) -> tuple[int, list[str], int]:
        """Persist many stocks in few DuckDB transactions (ingest pipeline).

        Two write paths:

        * incremental responses → one conflict-safe upsert transaction per call
          (falls back to per-stock commits on failure);
        * full-history responses (``*_full_requested`` and rows above
          ``_PRICE_FULL_REPLACE_THRESHOLD``) → appended to ``buffer`` and, once
          the buffer hits its stock/row/time budget, replaced by
          :meth:`_flush_price_replace_buffer` in a **single** transaction with a
          grouped ``DELETE ... IN (...)``. 2026-11-12 P0: per-stock transactions
          were 93% of the incremental price step's wall clock (144 silent stalls
          ≥10s, 96.5 min total) — not the network.

        ``flush_buffer=True`` drains the buffer even when ``batch`` is empty, so
        callers can flush the remainder in a ``finally`` block.

        2026-09-08 红队修复：只有 fetch_one 明确标记为"全历史请求"
        （raw_full_requested/qfq_full_requested）且行数超阈值，才允许
        DELETE+INSERT 替换。增量窗口返回的数据再长也只走冲突 upsert，
        避免把窗口外的历史K线静默删除。
        """
        # 2026-11-20（reports/151 §吞吐）：**增量路径同样要在阻塞写库前补满流水线**。
        # 生产日志实证：10 分钟内请求数呈 32/64/128 的整齐倍数（= 流水线被写库
        # 掐断后整窗排空再突发），而同一台机上 tencent 单请求 RTT 只有 0.09~0.36s。
        # 原先只有全历史替换缓冲那条路传了 ``before_write``，增量 upsert 这条
        # （正常交易日占 99%）主线程一进事务就把 64 深度队列抽干。这里补上。
        if before_write is not None and batch:
            with contextlib.suppress(Exception):
                before_write()
        large, small = [], []
        for item in batch:
            _, raw_result, qfq_result, raw_full_requested, qfq_full_requested = item
            raw_large = (
                bool(raw_full_requested)
                and raw_result is not None
                and len(raw_result.data) > self._PRICE_FULL_REPLACE_THRESHOLD
            )
            qfq_large = (
                bool(qfq_full_requested)
                and qfq_result is not None
                and len(qfq_result.data) > self._PRICE_FULL_REPLACE_THRESHOLD
            )
            if raw_large or qfq_large:
                large.append((item, raw_large, qfq_large))
            else:
                small.append(item)
        if large and buffer is None:
            # 兼容无缓冲调用点：逐股独立事务（有缓冲时走批量路径）。
            for (code, raw_result, qfq_result, _, _), raw_large, qfq_large in large:
                try:
                    with self.duck.transaction() as connection:
                        self._persist_price_pair_in_connection(
                            connection, code, raw_result, qfq_result,
                            full_replace_raw=raw_large,
                            full_replace_qfq=qfq_large,
                        )
                    success_count += 1
                    updated_codes.append(code)
                except Exception as exc:
                    fail_count += 1
                    if failed_codes is not None:
                        failed_codes.append(code)
                    logger.warning("[增量] 价格落库失败 %s: %s", code, exc)
                    self._record_failure(
                        code, "price_daily", "manager", str(exc),
                    )
        elif large and buffer is not None:
            for item, raw_large, qfq_large in large:
                buffer.add((item[0], item[1], item[2], raw_large, qfq_large))
                if buffer.should_flush():
                    success_count, updated_codes, fail_count = self._flush_price_replace_buffer(
                        buffer, success_count, updated_codes, fail_count,
                        failed_codes=failed_codes, before_write=before_write,
                        batch_sizes=batch_sizes,
                    )
        if small:
            try:
                with self.duck.transaction() as connection:
                    for code, raw_result, qfq_result, _, _ in small:
                        self._persist_price_pair_in_connection(
                            connection, code, raw_result, qfq_result,
                        )
                success_count += len(small)
                updated_codes.extend(code for code, _, _, _, _ in small)
            except Exception:
                for code, raw_result, qfq_result, _, _ in small:
                    try:
                        self._persist_incremental_price_pair(code, raw_result, qfq_result)
                        success_count += 1
                        updated_codes.append(code)
                    except Exception as exc:
                        fail_count += 1
                        if failed_codes is not None:
                            failed_codes.append(code)
                        logger.warning("[增量] 价格落库失败 %s: %s", code, exc)
                        source = getattr(raw_result, "metadata", None)
                        self._record_failure(
                            code, "price_daily",
                            getattr(source, "source", "manager") if source is not None else "manager",
                            str(exc),
                        )
        if flush_buffer and buffer is not None and len(buffer):
            success_count, updated_codes, fail_count = self._flush_price_replace_buffer(
                buffer, success_count, updated_codes, fail_count,
                failed_codes=failed_codes, before_write=before_write,
                batch_sizes=batch_sizes,
            )
        return success_count, updated_codes, fail_count

    def _cleanup_redundant_retries(self, target_date: str) -> int:
        """Drop price retry entries whose data is already up to date.

        Historical failures (e.g. the "no available adapter" batch from a
        broken source window) leave entries behind even after the underlying
        price data was recovered by the incremental pass. Rechecking those
        with a full-history refetch would burn minutes per stock for nothing.
        """
        try:
            rows = self.sqlite.query(
                "SELECT id, stock_code FROM retry_list WHERE data_type = 'price_daily'"
            )
        except Exception as error:
            logger.warning("读取 price retry 列表失败: %s", error)
            return 0
        if not rows:
            return 0
        codes = [row["stock_code"] for row in rows]
        slots = ", ".join("?" for _ in codes)
        try:
            coverage = self.duck.read_query(
                f"""WITH raw AS (
                        SELECT stock_code, MAX(trade_date) AS latest FROM price_daily_raw GROUP BY stock_code
                    ), qfq AS (
                        SELECT stock_code, MAX(trade_date) AS latest FROM price_daily_qfq GROUP BY stock_code
                    )
                    SELECT stock.stock_code,
                           raw.latest AS raw_latest,
                           qfq.latest AS qfq_latest,
                           EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND batch.data_type = 'price_daily_raw'
                           ) AS raw_lineaged,
                           EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND batch.data_type = 'price_daily_qfq'
                           ) AS qfq_lineaged
                    FROM stock_meta stock
                    LEFT JOIN raw ON raw.stock_code = stock.stock_code
                    LEFT JOIN qfq ON qfq.stock_code = stock.stock_code
                    WHERE stock.stock_code IN ({slots})""",
                codes,
            )
        except Exception as error:
            logger.warning("查询 price retry 覆盖状态失败: %s", error)
            return 0
        clean_ids = [
            row["id"]
            for row in rows
            for cover in coverage
            if cover["stock_code"] == row["stock_code"]
            and str(cover.get("raw_latest") or "")[:10] >= target_date
            and str(cover.get("qfq_latest") or "")[:10] >= target_date
            and cover.get("raw_lineaged")
            and cover.get("qfq_lineaged")
        ]
        if clean_ids:
            self.sqlite.execute(
                "DELETE FROM retry_list WHERE id IN ({})".format(
                    ", ".join("?" for _ in clean_ids)
                ),
                clean_ids,
            )
            logger.info("[增量] 清理 %d 条已达标的价格重试条目", len(clean_ids))
        return len(clean_ids)

    def _cleanup_redundant_index_valuation_retries(self) -> int:
        """删除本地数据已覆盖且足够新鲜的指数估值 retry。

        乐咕为月末序列（2026-09 起月度）；某次刷新被风控拦截但本地最新点
        仍在 35 天内时，重试条目只是噪声，继续留在队列会让状态页永久显示
        "待重试"。源本身不可用时改为由下次每日刷新自然覆盖。
        """
        try:
            rows = self.sqlite.query(
                "SELECT id, stock_code FROM retry_list WHERE data_type = 'index_valuation'"
            )
            if not rows:
                return 0
            codes = [row["stock_code"] for row in rows]
            slots = ", ".join("?" for _ in codes)
            coverage = self.duck.read_query(
                f"""SELECT index_code, MAX(trade_date) AS latest
                    FROM index_valuation
                    WHERE source = 'legulegu' AND index_code IN ({slots})
                    GROUP BY index_code""",
                codes,
            )
            cutoff = cn_today() - timedelta(days=35)
            fresh = {
                row["index_code"]
                for row in coverage
                if row["latest"] is not None and row["latest"] >= cutoff
            }
            clean_ids = [row["id"] for row in rows if row["stock_code"] in fresh]
            if clean_ids:
                clean_slots = ", ".join("?" for _ in clean_ids)
                self.sqlite.execute(
                    f"DELETE FROM retry_list WHERE id IN ({clean_slots})",
                    clean_ids,
                )
                logger.info(
                    "[增量] 清理 %d 条本地数据已覆盖的指数估值重试条目",
                    len(clean_ids),
                )
            return len(clean_ids)
        except Exception as error:
            logger.warning("清理指数估值冗余重试失败: %s", error)
            return 0

    # 无逐股重试路径、由 universe/行业步骤统一维护的数据域：历史失败条目
    # 是死循环垃圾（如 akshare 被封窗口产生的 listing_info 失败）。
    # announcements 除外：其 retry 条目是公告 pending 的持久化标记（PRD §7.4）。
    _CLEANUP_RETRY_DATA_TYPES = {
        "listing_info", "stock_list", "trading_dates", "sw_industry",
        "csrc_industry",
    }

    def _resolve_complete_missing_records(self) -> int:
        """Mark listing_info missing entries resolved when local fields are complete.

        The universe source outage recorded missing entries for the whole
        universe even though most stocks already had complete listing data.
        Once the local fields are verified complete, the entry is resolved
        (housekeeping purges resolved rows after 30 days).
        """
        rows = self.sqlite.query(
            "SELECT stock_code FROM missing_list "
            "WHERE field_name = 'listing_info' AND resolved_at IS NULL"
        )
        if not rows:
            return 0
        codes = [row["stock_code"] for row in rows]
        slots = ", ".join("?" for _ in codes)
        try:
            complete = self.duck.read_query(
                f"""SELECT stock_code FROM stock_meta
                    WHERE stock_code IN ({slots})
                      AND listing_date IS NOT NULL
                      AND is_st IS NOT NULL
                      AND is_suspended IS NOT NULL""",
                codes,
            )
        except Exception as error:
            logger.warning("查询 listing_info 缺失覆盖状态失败: %s", error)
            return 0
        if not complete:
            return 0
        complete_codes = [row["stock_code"] for row in complete]
        now = datetime.now(UTC).isoformat()
        self.sqlite.execute(
            "UPDATE missing_list SET resolved_at = ? "
            "WHERE field_name = 'listing_info' AND stock_code IN ({})".format(
                ", ".join("?" for _ in complete_codes)
            ),
            [now, *complete_codes],
        )
        logger.info("[增量] 标记 %d 条 listing_info 缺失已补齐", len(complete_codes))
        return len(complete_codes)

    # 一次性重查标记（见 _recheck_zero_attempt_retries）。带此前缀的行不再重置，
    # 所以每个历史行最多获得一次干净重试，不会变成每轮重置的死循环。
    _ZERO_ATTEMPT_RECHECK_MARKER = "[zero_attempt_recheck]"
    # 历史"零尝试假失败"的错误串：601123/603435 曾一次适配器都没真正尝试就被
    # 记成 ``无可用适配器 (tried: [])``。
    _ZERO_ATTEMPT_ERROR_PATTERNS = ("无可用适配器", "tried: []")

    def _recheck_zero_attempt_retries(self) -> int:
        """Give historical zero-attempt false failures one clean retry.

        2026-11-12 P1：这些行其实从未真正请求过源，继续按"重试 5 次"计数只会
        以 retry_exhausted 永久进 missing_list。重置 retry_count 让它们走一次
        真实请求；成功后自动出队，仍失败则按错误种类正确归因。
        """
        try:
            rows = self.sqlite.query(
                "SELECT id, error FROM retry_list WHERE error IS NOT NULL"
            )
        except Exception as error:
            logger.warning("读取零尝试失败任务失败: %s", error)
            return 0
        ids = [
            row["id"]
            for row in rows
            if not str(row.get("error") or "").startswith(self._ZERO_ATTEMPT_RECHECK_MARKER)
            and any(
                pattern in str(row.get("error") or "")
                for pattern in self._ZERO_ATTEMPT_ERROR_PATTERNS
            )
        ]
        if not ids:
            return 0
        slots = ", ".join("?" for _ in ids)
        try:
            self.sqlite.execute(
                f"""UPDATE retry_list
                    SET retry_count = 0, next_retry_at = NULL,
                        last_attempt = CURRENT_TIMESTAMP,
                        error = ? || error
                    WHERE id IN ({slots})""",
                [f"{self._ZERO_ATTEMPT_RECHECK_MARKER} ", *ids],
            )
        except Exception as error:
            logger.warning("重置零尝试失败任务失败: %s", error)
            return 0
        logger.info(
            "[增量] %d 条零尝试假失败任务已重置为一次干净重试", len(ids),
        )
        return len(ids)

    def _cleanup_unretryable_tasks(self) -> int:
        """清理无逐股重试路径域的死循环条目（实现已搬到 ``app/core/update_parts/retry.py``）。"""
        return cleanup_unretryable_tasks(
            self.sqlite,
            self._CLEANUP_RETRY_DATA_TYPES,
            self._recheck_zero_attempt_retries,
            logger=logger,
        )

    def _resolve_legal_empty_funding_missing(self) -> int:
        """Resolve A-share placement_funding source_empty markers.

        Eastmoney BonusFinancing 页面为空代表该公司没有增发/配股事件，
        属于合法空结果，不应长期挂在 missing_list。北交所（4/8/9 开头）
        没有东财交叉源，继续保留为如实披露缺口。
        """
        try:
            rows = self.sqlite.query(
                """SELECT COUNT(*) AS c FROM missing_list
                   WHERE field_name = 'placement_funding'
                     AND reason_code = 'source_empty'
                     AND resolved_at IS NULL
                     AND stock_code NOT LIKE '4%'
                     AND stock_code NOT LIKE '8%'
                     AND stock_code NOT LIKE '9%'"""
            )
            count = int(rows[0]["c"]) if rows else 0
            if count:
                self.sqlite.execute(
                    """UPDATE missing_list SET resolved_at = CURRENT_TIMESTAMP
                       WHERE field_name = 'placement_funding'
                         AND reason_code = 'source_empty'
                         AND resolved_at IS NULL
                         AND stock_code NOT LIKE '4%'
                         AND stock_code NOT LIKE '8%'
                         AND stock_code NOT LIKE '9%'"""
                )
                logger.info("已解决 %d 条 A 股无增发/配股事件的合法缺失", count)
            return count
        except Exception as error:
            logger.warning("解决合法空融资缺失失败: %s", error)
            return 0

    def _cleanup_completed_announcement_retries(self) -> int:
        """Drop announcement-pending entries only after their filings are registered.

        2026-08-29 修复：旧逻辑只要“三表在 18 个月内有一个共同完整期”就清理，
        导致 Q1 已完整但 Q2 中报仍 pending 的股票被误清，retry 永久丢失。
        现在以 announcement_registry 为准：retry.extra_json 里的公告 ID
        全部已入册，才说明对应财务刷新真正成功过。
        """
        rows = self.sqlite.query(
            "SELECT id, stock_code, extra_json FROM retry_list "
            "WHERE data_type = 'announcements'"
        )
        if not rows:
            return 0
        completed_ids: list[int] = []
        for row in rows:
            try:
                payload = json.loads(row.get("extra_json") or "{}")
                announcement_ids = payload.get("announcement_ids") or []
            except (json.JSONDecodeError, TypeError):
                continue
            if not announcement_ids:
                continue
            slots = ", ".join("?" for _ in announcement_ids)
            registered = self.sqlite.query(
                f"SELECT COUNT(*) AS c FROM announcement_registry "
                f"WHERE announcement_id IN ({slots})",
                announcement_ids,
            )
            if registered and int(registered[0]["c"]) >= len(set(announcement_ids)):
                completed_ids.append(int(row["id"]))
        if completed_ids:
            slots = ", ".join("?" for _ in completed_ids)
            self.sqlite.execute(
                f"DELETE FROM retry_list WHERE id IN ({slots})",
                completed_ids,
            )
            logger.info("[增量] 清理 %d 条已入册的公告待处理", len(completed_ids))
        return len(completed_ids)

    def _retry_failed_tasks(self, tasks: list[dict]) -> dict:
        """重试失败任务（实现已搬到 ``app/core/update_parts/retry_tasks.py``）。"""
        return retry_failed_tasks(
            self, tasks, logger=logger, is_duckdb_fatal=_is_duckdb_fatal,
        )
    def _settle_non_retryable_error(
        self,
        retry_id: int,
        stock_code: str,
        data_type: str,
        outcome: dict[str, Any],
    ) -> bool:
        """单条"非故障失败"结算（实现已搬到 ``app/core/update_parts/retry.py``）。"""
        return settle_non_retryable_error(
            self.sqlite,
            self._record_missing,
            retry_id,
            stock_code,
            data_type,
            outcome,
            logger=logger,
        )

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
                    "error_kind": _source_error_kind(
                        raw if (raw.metadata.error or not raw.data) else qfq
                    ),
                }
            try:
                self._persist_incremental_price_pair(stock_code, raw, qfq, full_field_audit=True)
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
                return {
                    "status": "failed",
                    "error": result.metadata.error or "empty result",
                    "error_kind": _source_error_kind(result),
                }
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
            return {
                "status": "failed",
                "error": result.metadata.error or "empty result",
                "error_kind": _source_error_kind(result),
            }
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
        completed_codes: list[str] = []
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
                completed_codes.append(code)
            else:
                failed_codes.append(code)

        return {
            "status": "success" if not failed_codes else "partial",
            "targeted": len(targets),
            "completed": completed,
            "completed_codes": completed_codes,
            "failed": len(failed_codes),
            "failed_codes": failed_codes[:20],
        }

    def _persist_price_retry(
        self,
        stock_code: str,
        adjust: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """价格重试成功后的先落值（实现已搬到 ``app/core/update_parts/retry.py``）。"""
        persist_price_retry(self.duck, stock_code, adjust, rows)

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
        """重试失败条目的落地与退避排期（实现已搬到 ``app/core/update_parts/retry.py``）。

        搬走的是 46 行 SQL 与退避公式（``MIN(24, 1 << MIN(retry_count + 1, 5))`` 小时）；
        本方法只做参数转发，签名、返回、异常与日志通道（``app.core.update``）都不变。
        """
        mark_retry_failed(self.sqlite, retry_id, error, logger=logger)

    def _record_failure(
        self,
        stock_code: str,
        data_type: str,
        adapter: str,
        error: str,
        extra_json: str | None = None,
        *,
        error_kind: str = "unknown",
    ) -> None:
        """记录失败到 retry_list（带 error_kind，供重试分类与追溯）"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt,
                        extra_json, error_kind)
                       VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt,
                         error_kind=excluded.error_kind""",
                    [stock_code, data_type, adapter, error[:500],
                     datetime.now(UTC).isoformat(), extra_json or "{}",
                     str(error_kind or "unknown")],
                )
        except Exception as e:
            logger.warning(f"记录失败信息失败: {e}")

    def _record_missing(self, stock_code: str, field_name: str, reason_code: str) -> None:
        """记录缺失到 missing_list（与 init.DataInitializer 同语义）"""
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [stock_code, field_name, reason_code],
                )
        except Exception as e:
            logger.warning(f"记录缺失信息失败: {e}")

    def _record_failure_or_missing(
        self,
        stock_code: str,
        data_type: str,
        adapter: str,
        error: str,
        *,
        extra_json: str | None = None,
        error_kind: str = "unknown",
    ) -> None:
        """如实归因一次抓取失败（契约 §1/§5）。

        ``legal_empty``（源正常但没有数据）与 ``source_unsupported``（源不支持
        该票）不是故障：直接写 missing_list，**不写 retry**、不占重试次数、
        不会在 5 轮后以 ``retry_exhausted`` 的假名义永久消失。
        """
        kind = str(error_kind or "unknown")
        reason_code = _ERROR_KIND_REASON_CODE.get(kind)
        if reason_code is not None:
            self._record_missing(stock_code, data_type, reason_code)
            logger.info(
                "[增量] %s %s 非故障失败已登记为缺失(%s): %s",
                stock_code, data_type, reason_code, error,
            )
            return
        self._record_failure(
            stock_code, data_type, adapter, error,
            extra_json=extra_json, error_kind=kind,
        )
