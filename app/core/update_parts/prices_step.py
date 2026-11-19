"""增量价格步骤的管道编排（reports/140 §2.5 拆分的第 4 块）。

## 这个模块是什么

`IncrementalUpdater._update_prices_incremental`（502 行）是全仓最难搬的一块：它同时
做**选股、抓取、攒批、落盘、降级判定、待重算标记**六件事，内嵌 3 个线程池
（request / stock / 主线程写库）与一套自适应并发，SQL 二十余条。搬走后
`update.py` 里只留同名薄委托，签名 / 返回值 / 异常 / 日志通道一字不改。

## 怎么切的

不是把 502 行整体平移到一个函数里（那只是把一堵墙挪个位置），而是按**关注点**
切成三段，每段都只经由显式入参拿状态：

1. :func:`_plan_price_scope` —— **选谁**。读取本地水位、待重拉 qfq 集合、
   研究优先名单，跑"缺口 + 血缘"那条目标集 SQL，算出每只股票自己的增量窗口、
   本轮 xdxr 全历史重拉集合与 `raw_full_refetch` 开关，产出 :class:`PriceScope`。
   两个 `skipped` 早退（`no_stock_meta` / `prices_up_to_date`）原样保留。
2. :func:`_run_price_pipeline` —— **怎么抓、怎么攒、什么时候落盘**。两个
   `ThreadPoolExecutor`、连续流水线（`fill_pipeline`）、自适应并发升降、
   `_PriceReplaceBuffer` 攒批与 `finally` 排空都在这里；结束（或异常）后把
   计数与证据装进 :class:`PricePipelineOutcome`。
3. :func:`_finalize_price_step` —— **怎么结算**。清待重拉集合、qfq 滞后降级
   `partial(qfq_lagged)`、写 `tech_recompute_pending` / `technical_pending`、
   拼 step 报告。
4. :func:`update_prices_incremental` —— 只有十来行的编排，把上面三段串起来。

## 边界（本模块不做什么）

- **不 import `app.core.update`**（会成循环依赖）。状态一律经由 `owner`
  显式读写（鸭子类型，不 import `IncrementalUpdater`）。
- **不自己打日志实例**：`logger` 由调用方注入，仍是 `app.core.update` 那一个
  logger 对象，日志通道（名字 / 级别 / 文案）与搬移前完全一致。
- **不自己看时钟**：`clock` 由调用方注入。搬移前 `time.monotonic()` 全部来自
  `update.time`，而 `tests/regression/test_price_batch_flush.py` 用
  `monkeypatch.setattr(update_module, "time", ...)` 跳表；注入成
  `lambda: time.monotonic()` 后这套 monkeypatch 语义逐字保留
  （`_PriceReplaceBuffer` 的时钟判据同样走这个 `clock`）。
- **`source_error_kind` 也只有一份实现**：契约 §1 的分类在 `update.py`，这里
  以入参接收，不复制第二份。

## 行为零变更怎么保证

搬移是**机械变换**：原文逐行切段，只做 `self.` → `owner.`、`time.monotonic(`
→ `clock(`、`_source_error_kind(` → `source_error_kind(` 这几个替换，并把三个
段间共享的局部量改成经 `PriceScope` / `PricePipelineOutcome` 传递。SQL 字面量、
日志文案、超时/退避/并发度公式、`_persist_price_batch` 的调用形态、`finally`
排空顺序全部原样。回归网：
``tests/regression/test_price_batch_flush.py``（攒批判据）、
``test_price_full_history_batch.py``（全历史替换 + finally 排空）、
``test_incremental_price_lineage.py``（增量血缘 SQL）、
``test_qfq_refetch_pending.py`` / ``test_qfq_recompute_trigger.py``（待重拉与重算
触发）、``test_incremental_update_scope.py``（范围收缩）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.financial_period import cn_today
from app.core.update_parts.prices import PriceReplaceBuffer

__all__ = [
    "PricePipelineOutcome",
    "PriceScope",
    "update_prices_incremental",
]


@dataclass(slots=True)
class PriceScope:
    """本轮价格增量更新的"范围判定"结果（选股 + 窗口 + 重拉开关）。

    这是 :func:`_plan_price_scope` 唯一的出口：后半段（抓取管道与结算）只读
    这些字段，不再自己去查库、猜窗口。字段语义与搬移前的同名局部量一一对应。
    """

    target_stocks: list[dict]
    priority_codes: set[str]
    pending_qfq_codes: list[str]
    pending_set: set[str]
    pending_in_targets: set[str]
    target_date: str
    fallback_start: str
    end_date: str
    xdxr_codes: set[str]
    raw_full_refetch: bool


@dataclass(slots=True)
class PricePipelineOutcome:
    """抓取 + 攒批 + 落盘跑完之后的可观测结果（供结算段拼报告）。"""

    success_count: int
    fail_count: int
    updated_codes: list[str]
    failed_codes: list[str]
    completed: int
    write_batch_sizes: list[int]
    observed_concurrency: int
    started_at: float


def _plan_price_scope(
    owner: Any,
    max_stocks: int,
    *,
    logger: logging.Logger,
) -> PriceScope | dict:
    """选出本轮要更新的股票，并算出窗口 / 除权重拉集合 / 整段重拉开关。

    返回值要么是 :class:`PriceScope`，要么是搬移前就存在的那两个
    `skipped` 早退报告（`no_stock_meta` / `prices_up_to_date`）——早退语义
    一字未改，由调用方原样返回。
    """
    logger.info("[增量] 检查需要价格更新的股票...")

    latest_local = owner._get_latest_local_price_date()
    today = cn_today().strftime("%Y-%m-%d")
    target_date = owner._latest_expected_trading_date(today)

    # 2026-11-13 P0（B）：待重拉 qfq 的股票必须强制进入本轮目标集，
    # 否则它们"价格已最新"就不会被选中，xdxr_codes 也就无从生效。
    pending_qfq_codes = owner._load_qfq_refetch_pending()
    pending_filter = ""
    query_params: list[Any] = [target_date, target_date]
    if pending_qfq_codes:
        pending_slots = ", ".join("?" for _ in pending_qfq_codes)
        pending_filter = f" OR stock.stock_code IN ({pending_slots})"
        query_params.extend(pending_qfq_codes)
        logger.warning(
            "[增量] %d 只股票在 qfq 待重拉集合中（新写入除权事件），本轮强制全历史重拉: %s",
            len(pending_qfq_codes), ", ".join(pending_qfq_codes[:20]),
        )

    # 按股票自己的最新日期选择缺口，不能用全库 MAX 代表全部股票。
    # 每只成功股票立即提交，进程中断后下一轮可从剩余缺口继续。
    try:
        stocks = owner.duck.read_query(
            f"""SELECT stock.stock_code, stock.exchange,
                          raw.latest_raw_date, qfq.latest_qfq_date
                   FROM stock_meta stock
                   LEFT JOIN (
                     SELECT stock_code, MAX(trade_date) AS latest_raw_date
                     FROM price_daily_raw GROUP BY stock_code
                   ) raw ON raw.stock_code = stock.stock_code
                   LEFT JOIN (
                     SELECT stock_code, MAX(trade_date) AS latest_qfq_date
                     FROM price_daily_qfq GROUP BY stock_code
                   ) qfq ON qfq.stock_code = stock.stock_code
                   WHERE stock.is_listed IS TRUE AND COALESCE(stock.is_suspended, FALSE) IS FALSE
                      AND (raw.latest_raw_date IS NULL OR qfq.latest_qfq_date IS NULL
                           OR raw.latest_raw_date < ? OR qfq.latest_qfq_date < ?
                           OR NOT EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND audit.report_date = raw.latest_raw_date
                                 AND batch.data_type = 'price_daily_raw'
                           )
                           OR NOT EXISTS (
                               SELECT 1 FROM source_audit audit
                               JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                               WHERE audit.stock_code = stock.stock_code
                                 AND audit.field_name = 'latest_close'
                                 AND audit.report_date = qfq.latest_qfq_date
                                 AND batch.data_type = 'price_daily_qfq'
                           ){pending_filter})
                   ORDER BY stock.stock_code""",
            query_params,
        )
    except Exception:
        return {"status": "skipped", "reason": "no_stock_meta"}

    if not stocks:
        logger.info("[增量] 所有非停牌股票价格已是最新，无需更新")
        return {"status": "skipped", "reason": "prices_up_to_date"}

    target_stocks = stocks
    try:
        priority_codes = {
            row["stock_code"]
            for row in owner.sqlite.query("SELECT DISTINCT stock_code FROM watchlist")
        }
    except Exception as error:
        priority_codes = set()
        logger.warning("读取研究优先名单失败，保持代码顺序: %s", error)
    pending_set = set(pending_qfq_codes)
    if priority_codes or pending_set:
        # 待重拉 qfq = 复权因子已过期（正确性），必须排在研究名单之前。
        target_stocks = sorted(
            target_stocks,
            key=lambda stock: (
                stock["stock_code"] not in pending_set,
                stock["stock_code"] not in priority_codes,
                stock["stock_code"],
            ),
        )
    if max_stocks > 0:
        # C10修复(报告41): 取前 N 只必须稳定排序（ORDER BY stock_code），
        # 否则每次运行取到的子集随机漂移
        target_stocks = target_stocks[:max_stocks]

    fallback_start = latest_local or (cn_today() - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = target_date

    # 增量窗口内发生除权除息的股票：qfq 需要全历史重拉。
    # 2026-11-12 P0：窗口按**每只股票自己**的本地最新 raw/qfq 计算。
    # 旧实现取全市场 min(target_dates)，一只 qfq 停在旧日期的股票就
    # 会把全体窗口拉回数年前——除权窗口越退越早、xdxr_full_refetch
    # 32 → 293、价格步骤 143.5 分钟的根因之一。
    stock_windows = {
        str(stock["stock_code"]): owner._local_price_window_start(
            stock, fallback_start,
        )
        for stock in target_stocks
    }
    xdxr_codes = owner._get_xdxr_codes_since(
        fallback_start, stock_windows=stock_windows,
    )
    # 2026-11-13 P0（B）：新写入除权事件的股票无条件全历史重拉 qfq，
    # 不参与任何日期比较（覆盖"事件晚于价格推进"的死角）。
    pending_in_targets = pending_set & {str(stock["stock_code"]) for stock in target_stocks}
    if pending_in_targets:
        xdxr_codes |= pending_in_targets
        logger.warning(
            "[增量] %d 只股票因新写入除权事件强制全历史重拉 qfq: %s",
            len(pending_in_targets), ", ".join(sorted(pending_in_targets)[:20]),
        )
    if xdxr_codes:
        logger.info(f"[增量] {len(xdxr_codes)} 只股票在各自窗口内发生除权除息，qfq 将全历史重拉")

    # 本地数据陈旧时默认仍走增量：从本地最新日期补缺口即可覆盖全部缺失
    # 交易日。整段重拉只作为显式数据修复手段（price_full_refetch_on_stale），
    # 因为它会把每股数千行历史 DELETE+INSERT 回 17M 行主表，实测单股 10s+，
    # 全市场追赶一次要十几小时（2026-10-20 诊断，依据 S4）。
    raw_full_refetch = False
    if owner.price_full_refetch_on_stale and latest_local:
        try:
            latest_date = datetime.strptime(latest_local, "%Y-%m-%d").date()
            gap_days = (cn_today() - latest_date).days
            raw_full_refetch = gap_days > owner.incremental_window_days
        except ValueError:
            raw_full_refetch = False
    if raw_full_refetch:
        logger.info(f"[增量] 本地价格陈旧超过 {owner.incremental_window_days} 天，raw 整段重拉")
    elif latest_local:
        logger.info(f"[增量] 本地价格截至 {latest_local}，从该日期增量补缺口（不整段重拉）")

    return PriceScope(
        target_stocks=target_stocks,
        priority_codes=priority_codes,
        pending_qfq_codes=pending_qfq_codes,
        pending_set=pending_set,
        pending_in_targets=pending_in_targets,
        target_date=target_date,
        fallback_start=fallback_start,
        end_date=end_date,
        xdxr_codes=xdxr_codes,
        raw_full_refetch=raw_full_refetch,
    )


def _run_price_pipeline(
    owner: Any,
    scope: PriceScope,
    *,
    logger: logging.Logger,
    source_error_kind: Callable[[Any], str],
    clock: Callable[[], float],
    detail_cb: Callable[[str, dict[str, Any]], None] | None,
) -> PricePipelineOutcome:
    """跑完"抓取 → 攒批 → 落盘"整条流水线，返回计数与验收证据。

    两个线程池、连续流水线深度、自适应并发升降、`_PriceReplaceBuffer` 的
    落盘时机与 `finally` 排空全部保持搬移前原样；异常（例如 `detail_cb`
    主动抛错）在排空并 `shutdown` 之后照原样向上抛。
    """
    success_count = 0
    fail_count = 0
    updated_codes: list[str] = []
    # 2026-11-12 P1：失败代码必须可追溯（旧实现只有汇总行，DB/日志都查不到）。
    failed_codes: list[str] = []
    completed: list[int] = [0]
    concurrency = max(1, int(owner.price_fetch_concurrency))
    max_concurrency = max(concurrency, int(owner.price_fetch_max_concurrency))
    concurrency_step = max(1, int(owner.price_fetch_concurrency_step))
    scale_up_seconds = max(1, int(owner.price_fetch_scale_up_seconds))
    fetch_timeout = max(1, int(owner.price_fetch_timeout_seconds))
    batch_size = max(1, int(owner.price_fetch_batch_size))
    # 全历史替换的攒批缓冲（硬上限：只数 / 行数 / 存活秒数，见 _PriceReplaceBuffer）。
    # ``clock`` 由调用方注入 = ``update.time.monotonic``，与搬移前那个兼容壳
    # （``_PriceReplaceBuffer`` 内 ``lambda: time.monotonic()``）时钟源完全一致。
    write_buffer = PriceReplaceBuffer(
        max_stocks=owner._PRICE_WRITE_BUFFER_STOCKS,
        max_rows=owner._PRICE_WRITE_BUFFER_ROWS,
        max_seconds=owner._PRICE_WRITE_BUFFER_SECONDS,
        clock=clock,
    )
    # 每次落盘的真实批大小（验收证据：复盘要求能证明 N>1 而不是"N=1 也算批量"）。
    write_batch_sizes: list[int] = []
    update_started = clock()

    request_executor = ThreadPoolExecutor(
        max_workers=max_concurrency * 2,
        thread_name_prefix="vd-price-request",
    )

    def fetch_one(
        stock: dict,
    ) -> tuple[str, Any, Any, str | None, bool, bool, bool, str]:
        """在 worker 线程网络抓取 raw+qfq；不碰共享写连接。

        增量原则：raw 与 qfq 各自以本地最新日期为起点，仅缺失的一侧
        才全量拉取。绝不用全局最老日期拖低另一侧的起点——历史已完整的
        股票每次只拉缺口（通常几行），避免无意义的全历史重拉。

        返回最后三个值表示 raw/qfq 本次是否为"全历史请求"以及失败时的
        ``error_kind``（契约 §1：只有 source_unavailable 计入重试消耗）。
        只有全历史请求才允许走 DELETE+INSERT 全量替换（2026-09-08 红队）。
        """
        code = stock["stock_code"]
        need_qfq_full = code in scope.xdxr_codes
        raw_full_requested = bool(scope.raw_full_refetch)
        qfq_full_requested = bool(need_qfq_full or scope.raw_full_refetch)
        raw_latest = stock.get("latest_raw_date")
        qfq_latest = stock.get("latest_qfq_date")
        raw_from = None if raw_full_requested else (str(raw_latest) if raw_latest else None)
        qfq_from = None if qfq_full_requested else (str(qfq_latest) if qfq_latest else None)
        def fetch_adjust(adjust: str, start: str | None) -> tuple[Any, float]:
            started = clock()
            result = owner.adapter_mgr.fetch(FetchRequest(
                data_type="price_daily", stock_codes=[code],
                start_date=start, end_date=scope.end_date, adjust=adjust,
                extra_params={
                    "deadline_monotonic": started + fetch_timeout,
                },
            ))
            return result, clock() - started

        futures = {
            request_executor.submit(fetch_adjust, "raw", raw_from): "raw",
            request_executor.submit(fetch_adjust, "qfq", qfq_from): "qfq",
        }
        done, pending = wait(futures, timeout=fetch_timeout)
        if pending:
            recover = getattr(owner.adapter_mgr, "recover_after_timeout", None)
            if callable(recover):
                recover()
            return (
                code, None, None, f"fetch timeout after {fetch_timeout}s",
                True, raw_full_requested, qfq_full_requested, "source_unavailable",
            )
        results: dict[str, Any] = {}
        slow = False
        try:
            for future in done:
                result, elapsed = future.result()
                results[futures[future]] = result
                slow = slow or elapsed > 30
        except Exception as exc:
            return (
                code, None, None, f"fetch exception: {exc}",
                False, raw_full_requested, qfq_full_requested, "source_unavailable",
            )
        raw_result = results["raw"]
        qfq_result = results["qfq"]
        if raw_result.metadata.error or not raw_result.data:
            return (
                code, None, None, raw_result.metadata.error or "empty raw",
                slow, raw_full_requested, qfq_full_requested,
                source_error_kind(raw_result),
            )
        if qfq_result.metadata.error or not qfq_result.data:
            return (
                code, None, None,
                qfq_result.metadata.error or "empty qfq",
                slow, raw_full_requested, qfq_full_requested,
                source_error_kind(qfq_result),
            )
        return (
            code, raw_result, qfq_result, None,
            slow, raw_full_requested, qfq_full_requested, "none",
        )

    batch: list[tuple[str, Any, Any, bool, bool]] = []
    stable_since = clock()
    observed_concurrency = concurrency
    # 连续流水线：在途任务 = 股票执行器队列深度。慢股票只占用一个槽位，
    # 其余槽位继续完成后面的股票；固定窗口实现会被单只 45s 长尾股票拖住
    # 整窗（2026-08-28 提速计划）。
    pipeline_depth = max(concurrency * 4, int(owner.price_fetch_pipeline_depth))
    stock_executor = ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="vd-price-stock",
    )
    outstanding: dict[Future, dict] = {}
    next_index = 0

    def fill_pipeline() -> None:
        nonlocal next_index
        while len(outstanding) < pipeline_depth and next_index < len(scope.target_stocks):
            stock = scope.target_stocks[next_index]
            next_index += 1
            outstanding[stock_executor.submit(fetch_one, stock)] = stock

    def flush_write_buffer() -> None:
        """Write the pending full-history buffer in one transaction.

        ``before_write`` 先补满流水线：写库期间主线程被占住，工作线程
        必须已经在跑下一批抓取，否则它们会空等（2026-11-12 P0 ③）。
        """
        nonlocal success_count, updated_codes, fail_count
        success_count, updated_codes, fail_count = owner._persist_price_batch(
            [], success_count, updated_codes, fail_count,
            buffer=write_buffer, flush_buffer=True,
            failed_codes=failed_codes, before_write=fill_pipeline,
            batch_sizes=write_batch_sizes,
        )

    def handle_price_result(
        code: str,
        raw_result: Any,
        qfq_result: Any,
        err: str | None,
        raw_full_requested: bool,
        qfq_full_requested: bool,
        error_kind: str = "source_unavailable",
    ) -> None:
        nonlocal success_count, updated_codes, fail_count
        if err is not None or raw_result is None:
            fail_count += 1
            failed_codes.append(code)
            # 2026-11-12 P1：逐只 WARNING，DB 与日志都能回答"今天哪几只失败"。
            logger.warning(
                "[增量] 价格失败 %s: %s (error_kind=%s)",
                code, err or "empty", error_kind,
            )
            owner._record_failure_or_missing(
                code, "price_daily", "manager", err or "empty",
                extra_json=json.dumps({"adjust": "both"}),
                error_kind=error_kind,
            )
        else:
            batch.append((code, raw_result, qfq_result, raw_full_requested, qfq_full_requested))
        if len(batch) >= batch_size:
            success_count, updated_codes, fail_count = owner._persist_price_batch(
                batch, success_count, updated_codes, fail_count,
                buffer=write_buffer, before_write=fill_pipeline,
                failed_codes=failed_codes, batch_sizes=write_batch_sizes,
            )
            batch.clear()
        elif write_buffer.should_flush():
            flush_write_buffer()

    try:
        fill_pipeline()
        while outstanding:
            done, _ = wait(
                tuple(outstanding.keys()),
                timeout=fetch_timeout + 5,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                # fetch_one 自身有 deadline，这里只是防御 executor 线程异常退出。
                logger.warning(
                    "价格流水线 %d 秒无任何股票完成（在途 %d），继续等待",
                    fetch_timeout + 5,
                    len(outstanding),
                )
                # 流水线进入空窗：已攒 ≥2 只且空闲超过阈值时先把缓冲写掉，
                # 不必等到整轮结束（写库前 before_write 会先补满流水线）。
                if write_buffer.should_flush():
                    flush_write_buffer()
                continue
            window_penalty = False
            for future in done:
                stock = outstanding.pop(future)
                code = stock["stock_code"]
                try:
                    (
                        code, raw_result, qfq_result, err, slow,
                        raw_full_requested, qfq_full_requested, error_kind,
                    ) = future.result()
                    window_penalty = window_penalty or slow or err is not None
                    handle_price_result(
                        code, raw_result, qfq_result, err,
                        raw_full_requested, qfq_full_requested, error_kind,
                    )
                except Exception as exc:
                    window_penalty = True
                    handle_price_result(
                        code, None, None, f"price pipeline exception: {exc}",
                        False, False, "source_unavailable",
                    )
                completed[0] += 1
                if detail_cb is not None:
                    detail_cb("price", {
                        "done": completed[0],
                        "total": len(scope.target_stocks),
                        "current": code,
                        "label": "股票价格",
                    })
            if window_penalty:
                new_concurrency = max(
                    int(owner.price_fetch_concurrency),
                    concurrency - concurrency_step,
                )
                stable_since = clock()
            elif clock() - stable_since >= scale_up_seconds:
                new_concurrency = min(max_concurrency, concurrency + concurrency_step)
                stable_since = clock()
            else:
                new_concurrency = concurrency
            if new_concurrency != concurrency:
                logger.warning(
                    "价格抓取自适应并发 %d -> %d", concurrency, new_concurrency,
                )
                concurrency = new_concurrency
                # ThreadPoolExecutor 支持运行时调整 worker 数；流水线只按
                # 队列深度提交，不会创建不可控的海量线程。
                stock_executor._max_workers = concurrency
                observed_concurrency = max(observed_concurrency, concurrency)
            fill_pipeline()
        if batch:
            success_count, updated_codes, fail_count = owner._persist_price_batch(
                batch, success_count, updated_codes, fail_count,
                buffer=write_buffer, before_write=fill_pipeline,
                failed_codes=failed_codes, batch_sizes=write_batch_sizes,
            )
            batch.clear()
    finally:
        # ② 循环正常结束或抛异常都必须把剩余数据落盘：既包括未满一批的
        #    暂存项（batch），也包括攒批缓冲（write_buffer）。已 DELETE 的
        #    股票更不能停在"删了没插"的中间态。
        try:
            if batch:
                success_count, updated_codes, fail_count = owner._persist_price_batch(
                    list(batch), success_count, updated_codes, fail_count,
                    buffer=write_buffer, failed_codes=failed_codes,
                    batch_sizes=write_batch_sizes,
                )
                batch.clear()
            if len(write_buffer):
                flush_write_buffer()
        except Exception as error:  # noqa: BLE001 - 收尾写入失败必须留痕
            logger.error(
                "价格收尾写入失败（暂存 %d 只 / 缓冲 %d 只）: %s",
                len(batch), len(write_buffer), error,
            )
        request_executor.shutdown(wait=False, cancel_futures=True)
        stock_executor.shutdown(wait=False, cancel_futures=True)

    return PricePipelineOutcome(
        success_count=success_count,
        fail_count=fail_count,
        updated_codes=updated_codes,
        failed_codes=failed_codes,
        completed=completed[0],
        write_batch_sizes=write_batch_sizes,
        observed_concurrency=observed_concurrency,
        started_at=update_started,
    )


def _finalize_price_step(
    owner: Any,
    scope: PriceScope,
    outcome: PricePipelineOutcome,
    *,
    logger: logging.Logger,
    clock: Callable[[], float],
) -> dict:
    """结算价格步骤：清待重拉、qfq 滞后降级、写两份待重算名单、拼报告。"""
    success_count = outcome.success_count
    fail_count = outcome.fail_count
    updated_codes = outcome.updated_codes
    failed_codes = outcome.failed_codes
    completed = outcome.completed
    write_batch_sizes = outcome.write_batch_sizes
    observed_concurrency = outcome.observed_concurrency

    # 只有真正落库成功的才移出待重拉集合；失败的继续保留，下一轮再试。
    cleared_qfq_pending = owner._clear_qfq_refetch_pending(
        [code for code in updated_codes if code in scope.pending_in_targets]
    )
    logger.info(f"[增量] 价格更新完成: 成功 {success_count}, 失败 {fail_count}")
    if failed_codes:
        logger.warning(
            "[增量] 价格失败代码 %d 只: %s",
            len(failed_codes), ", ".join(failed_codes[:20]),
        )
    # 2026-11-13 P1（1）：raw 全成功但 qfq 大面积滞后 → 必须降级 partial，
    # 不能让"0 失败"掩盖 4,613 只 qfq 停在昨天的假绿。
    qfq_lag = owner._qfq_lag_summary(
        [str(stock["stock_code"]) for stock in scope.target_stocks], scope.target_date,
    )
    qfq_lag_threshold = max(5, int(len(scope.target_stocks) * 0.01))
    step_status = "success" if fail_count == 0 else "partial"
    step_reason: str | None = None
    if fail_count == 0 and qfq_lag["qfq_lag_count"] > qfq_lag_threshold:
        step_status = "partial"
        step_reason = "qfq_lagged"
        logger.warning(
            "[增量] qfq 滞后 %d/%d 只（阈值 %d）→ 价格步骤降级 partial(qfq_lagged): %s",
            qfq_lag["qfq_lag_count"],
            len(scope.target_stocks),
            qfq_lag_threshold,
            ", ".join(qfq_lag["qfq_lag_codes"]),
        )
    # 2026-11-14 P0 J6（依据 S7 §2.2）：qfq 落后自己 raw 的股票必须写进"技术指标待重算
    # 名单"。价格步骤"成功"只说明 raw 落库成功；价格域指标读的是 qfq，
    # 而下一轮补 qfq 时 raw 已最新 → 重算集合为空 → 指标永久停在旧值。
    tech_lag_watermarks = owner._qfq_lag_watermarks(
        [str(stock["stock_code"]) for stock in scope.target_stocks]
    )
    tech_lag_codes = sorted(tech_lag_watermarks)
    tech_pending_marked = owner._mark_tech_recompute_pending(tech_lag_watermarks)
    # 2026-11-16（依据 S10 §9.2 P0-5）：**qfq 写入成功**的每一只都打上
    # technical_pending 标记。这覆盖 tech_recompute_pending 抓不到的形态：
    # qfq 事后补齐后与 raw 齐平（滞后判据为假），但快照技术三列仍停在
    # 补齐前算出的 NULL。下一阶段（或下一轮）强制重算，成功后移出。
    technical_watermarks = owner._qfq_watermarks(updated_codes)
    technical_pending_marked = owner._mark_technical_pending(technical_watermarks)
    elapsed_seconds = max(clock() - outcome.started_at, 0.001)
    return {
        "status": step_status,
        "reason": step_reason,
        "total": len(scope.target_stocks),
        "success": success_count,
        "failed": fail_count,
        "failed_codes": failed_codes[:20],
        # qfq 滞后可观测性（job_logs 可见）
        "qfq_lag_count": qfq_lag["qfq_lag_count"],
        "qfq_lag_ratio": qfq_lag["qfq_lag_ratio"],
        "qfq_lag_threshold": qfq_lag_threshold,
        "qfq_lag_codes": qfq_lag["qfq_lag_codes"],
        **({"qfq_lag_error": qfq_lag["qfq_lag_error"]} if "qfq_lag_error" in qfq_lag else {}),
        "xdxr_full_refetch": len(scope.xdxr_codes),
        "raw_full_refetch": scope.raw_full_refetch,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "rate_per_minute": round(completed * 60 / elapsed_seconds, 2),
        "max_concurrency_used": observed_concurrency,
        "priority_count": sum(
            stock["stock_code"] in scope.priority_codes for stock in scope.target_stocks
        ),
        # 落盘批次证据：复盘要求能证明"批量"不是 N=1 的措辞。
        "write_batches": len(write_batch_sizes),
        "write_batch_sizes": write_batch_sizes[:50],
        "max_batch_stocks": max(write_batch_sizes) if write_batch_sizes else 0,
        "qfq_refetch_pending_cleared": cleared_qfq_pending,
        # 依据 S7 §2.2 J6：本轮因 qfq<raw 而进入"技术指标待重算名单"的股票
        "tech_recompute_lag_count": len(tech_lag_codes),
        "tech_recompute_pending_marked": tech_pending_marked,
        "tech_recompute_lag_codes": tech_lag_codes[:20],
        # 依据 S10 §9.2 P0-5：qfq 写入成功 → technical_pending（技术三列强制重算）
        "technical_pending_marked": technical_pending_marked,
        "technical_pending_codes": sorted(technical_watermarks)[:20],
        "_updated_codes": updated_codes,
        "_xdxr_codes": sorted(scope.xdxr_codes),
        "_raw_full_refetch": scope.raw_full_refetch,
    }


def update_prices_incremental(
    owner: Any,
    max_stocks: int,
    detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    *,
    logger: logging.Logger,
    source_error_kind: Callable[[Any], str],
    clock: Callable[[], float],
) -> dict:
    """增量更新价格——只更新有新数据的股票。

    - 普通股票：按 latest_local → today 增量补齐 raw + qfq
    - 发生除权除息（xdxr）的股票：qfq 全历史重拉，保证复权口径一致
    - 本地价格陈旧：默认仍从本地最新日期增量补缺口；
      仅 price_full_refetch_on_stale=true 时才整段重拉（数据修复用）

    detail_cb: 每只股票处理完后的细粒度回调 (step_name, {done, total,
        current, label})，供前端实时进度条使用。

    实现按"选谁 / 怎么抓 / 怎么结算"切成三段（见模块文档）：本函数只做编排，
    不含任何 SQL、并发或落盘判据。
    """
    scope = _plan_price_scope(owner, max_stocks, logger=logger)
    if isinstance(scope, dict):
        return scope
    outcome = _run_price_pipeline(
        owner,
        scope,
        logger=logger,
        source_error_kind=source_error_kind,
        clock=clock,
        detail_cb=detail_cb,
    )
    return _finalize_price_step(owner, scope, outcome, logger=logger, clock=clock)
