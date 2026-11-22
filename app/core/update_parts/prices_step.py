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

## 2026-11-20 追加：三个 qfq 探针也搬到这里

`update.py` 行数棘轮（``tests/regression/test_update_structure.py``）红了，
按测试文档指示走"搬函数出去 + 原地薄委托"的正路，把**纯价格域**的三个探针
原样搬来（函数体逐字未改，只做 `self.` → `owner.`、`logger` 改入参）：

* :func:`qfq_lag_summary` ← `IncrementalUpdater._qfq_lag_summary`（85 行）
* :func:`qfq_watermarks` ← `IncrementalUpdater._qfq_watermarks`（28 行）
* :func:`qfq_lag_watermarks` ← `IncrementalUpdater._qfq_lag_watermarks`（43 行）

它们与已在这里的 :func:`raw_bars_beyond_qfq` / :func:`qfq_raw_watermarks` /
:func:`load_qfq_catchup_attempts` / :func:`defer_qfq_source_lag` 是同一套
"qfq 相对 raw 水位"判据，放在一起才看得出全貌。`IncrementalUpdater` 上的
同名方法仍是逐行薄委托，方法数不变（`test_update_structure.py` /
`test_update_step_inventory.py` 与既有 qfq 回归共同守这一层）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    # 本轮因"源侧 qfq 还没发"（gap < price_qfq_lag_bars 且本交易日已尝试满）
    # 被**延迟**的股票：既不抓取、也不算失败、不降级 partial、不写待重算名单。
    deferred_codes: list[str] = field(default_factory=list)
    # 候选裁决的逐条计数（为什么延迟 / 为什么没延迟），供报告与日志自证。
    defer_reasons: dict[str, int] = field(default_factory=dict)
    # 2026-11-22：被"源侧空数据负缓存"挡下的股票（源侧确认无数据、24h 内已试过）。
    # 它们**仍是硬缺口**，只是本轮不打网络；报告里单列，缺口不会被隐藏。
    source_empty_cached_codes: list[str] = field(default_factory=list)


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
    #: 本轮的追平尝试记账器（随批次落盘）。结算段只用它做兜底 flush 与计数；
    #: ``None`` 表示调用方没走流水线（测试/意外路径）→ 退回单次记账。
    catchup_tracker: QfqCatchupAttemptTracker | None = None


# ─── 源侧 qfq 发布滞后：候选 + 裁决（reports/151 §1）──────────────────────
#
# 事故：选股 SQL 里的 ``qfq.latest_qfq_date < ?target`` 对"最新交易日"在
# **当晚 raw 已发、qfq 未发**的那几个小时里对全市场恒真 → 每轮重选 5400+ 只，
# 单轮从 3 分 40 秒变成 36–90 分钟（限速锁把单轮钉死在 ~150 股/分钟，只能
# 减少"扫的次数"，提不快单轮）。
#
# 修法：``qfq < raw``（复权序列没跟上自己的日线）**只作候选**，再由两条判据裁决：
#   1. gap = 该股自己 raw K 线里排在 qfq 最新日之后的根数。
#      gap ≥ ``price_qfq_lag_bars``（默认 2）= 真滞后 → **无条件重选**；
#   2. gap 更小（默认 1 = 源侧正常发布节奏）时，看同一 ``(raw, qfq)`` 水位在
#      **同一交易日**里已成功尝试的次数：≥ ``price_qfq_catchup_max_attempts``
#      （默认 2）→ 本轮跳过。
#
# 硬缺口（raw/qfq 为 NULL、raw < target、两个血缘缺失、在 qfq 待重拉集合里）
# 一律在上面两步之前就归入"必须抓"，延迟逻辑碰不到它们；判不出原因
# （gap 查不到 / 记录对不上 / 状态读失败）时默认**不延迟**——方向是宁可多扫
# 一轮，绝不漏更新。
#
# ## 2026-11-21：记账必须**跟着批次落盘**，否则整条裁决是死循环
#
# 旧实现把 :func:`mark_qfq_catchup_attempts` 只放在 :func:`_finalize_price_step`
# （整步跑完）。而延迟判据要读的正是这条记录 —— 于是：
#
#    价格步跑不到终点（超时被杀 / 进程重启 / 收尾异常）
#      → 记录永远不落盘 → 下轮没有"已尝试满"的证据
#      → :func:`defer_qfq_source_lag` 永远判不出延迟 → 又重选全市场
#      → 又跑不完 → 记录仍不落盘（自锁）
#
# 现场证据：``data_refresh_state`` 里**根本不存在** ``price_qfq_catchup_attempts``
# 这个 key；76MB 日志里「源侧 qfq 滞后」0 次命中 —— 裁决一次都没生效过。
#
# 修法：记账由 :class:`QfqCatchupAttemptTracker` 承担，跟着
# ``_persist_price_batch`` 的**每一个批次**走（内存累积 + 每批一次幂等写），
# 步末只做兜底 flush。只对**成功落库**的股票记账（失败不记 → 下轮重试）。
_QFQ_CATCHUP_ATTEMPTS_KEY = "price_qfq_catchup_attempts"
#: 尝试记录的条目上限（正常只存"今天且仍落后"的股票，极端源故障时防无限膨胀）。
_QFQ_CATCHUP_ATTEMPTS_LIMIT = 8000
#: 记账落盘的合并阈值：攒够这么多**新记账**才写一次 ``data_refresh_state``。
#: 为什么不能每只股票写一次：整张表是一个 JSON 值（事故形态下 5571 条 ≈ 500KB），
#: 实测一次写 ≈13ms；每只写一次 = 5571×13ms ≈ 72s。按 100 只合并 ≈ 56 次 ≈ 0.7s。
_QFQ_CATCHUP_FLUSH_STOCKS = 100
#: 记账落盘的时间兜底：即使没攒够 ``_QFQ_CATCHUP_FLUSH_STOCKS``，超过这么多秒也写一次。
#: 它兜的是"进程被硬杀（SIGKILL/重启，`finally` 跑不到）"——最多丢最后 N 秒的记账，
#: 而不是像旧实现那样一次都写不下去。
_QFQ_CATCHUP_FLUSH_SECONDS = 15.0
_QFQ_LAG_BARS_DEFAULT = 2
_QFQ_CATCHUP_MAX_ATTEMPTS_DEFAULT = 2


def _day_text(value: Any) -> str | None:
    """把 DATE / 字符串水位统一成 ``YYYY-MM-DD``；判不出就返回 ``None``。"""
    if value is None:
        return None
    text = str(value).strip()
    return text[:10] or None


def _positive_int(value: Any, default: int) -> int:
    """读旋钮：非正整数（含缺省/手写坏值）一律回退到给定默认值。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _flag(row: dict, key: str) -> bool:
    """把 SQL 的布尔列读成 Python bool；列缺失/不可判定 → ``False``（保守=硬缺口）。"""
    value = row.get(key)
    return value is not None and bool(value)


def load_qfq_catchup_attempts(owner: Any) -> dict[str, dict[str, Any]]:
    """读取 ``data_refresh_state['price_qfq_catchup_attempts']``。

    形如 ``{code: {"raw": ..., "qfq": ..., "date": ..., "n": ...}}``：记录"这只
    股票在 ``date`` 那天以 ``(raw, qfq)`` 这个水位被抓过、且成功落库了 ``n`` 次"。
    读不出 / 格式非法一律返回空字典 —— 没有"已尝试"证据就不许延迟。
    """
    state_key = _QFQ_CATCHUP_ATTEMPTS_KEY
    try:
        raw = owner._read_refresh_state(state_key)
    except Exception:  # noqa: BLE001 - 状态读失败不改变选股语义
        return {}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    attempts: dict[str, dict[str, Any]] = {}
    for code, record in parsed.items():
        if not isinstance(record, dict):
            continue
        raw_day = _day_text(record.get("raw"))
        qfq_day = _day_text(record.get("qfq"))
        try:
            count = int(record.get("n"))
        except (TypeError, ValueError):
            continue
        if not code or raw_day is None or qfq_day is None or count < 1:
            continue
        attempts[str(code)] = {
            "raw": raw_day,
            "qfq": qfq_day,
            "date": _day_text(record.get("date")),
            "n": count,
        }
    return attempts


def _prune_attempts(attempts: dict[str, dict[str, Any]]) -> None:
    """条目超上限时按代码序丢掉超出部分（极端源故障时防状态无限膨胀）。"""
    if len(attempts) <= _QFQ_CATCHUP_ATTEMPTS_LIMIT:
        return
    overflow = len(attempts) - _QFQ_CATCHUP_ATTEMPTS_LIMIT
    for code in sorted(attempts)[:overflow]:
        attempts.pop(code, None)


def _apply_catchup_attempts(
    attempts: dict[str, dict[str, Any]],
    watermarks: dict[str, tuple[str | None, str | None]],
    day: str,
) -> list[str]:
    """把一批**落库后**的水位记账进 ``attempts``（原地改），返回本次记上的代码。

    语义（与 :func:`mark_qfq_catchup_attempts` 的文档一字不差）：
    同一 ``(raw, qfq)`` + 同一交易日 → ``n+1``；任一水位变化 → 重新从 1 记；
    没落后（qfq 已跟上自己的 raw）或水位判不出 → **不记**。

    记账判据只在这里实现一次：批量落盘的
    :class:`QfqCatchupAttemptTracker` 与单次写库的
    :func:`mark_qfq_catchup_attempts` 都调它，不许出现第二份口径。
    """
    marked: list[str] = []
    for code, (raw_value, qfq_value) in watermarks.items():
        raw_day = _day_text(raw_value)
        qfq_day = _day_text(qfq_value)
        # 没落后（qfq 已跟上自己的 raw）或判不出水位 → 不需要尝试记录。
        if raw_day is None or qfq_day is None or qfq_day >= raw_day:
            continue
        code = str(code)
        record = attempts.get(code)
        if (
            isinstance(record, dict)
            and record.get("raw") == raw_day
            and record.get("qfq") == qfq_day
        ):
            record["n"] = int(record.get("n") or 0) + 1
        else:
            attempts[code] = {"raw": raw_day, "qfq": qfq_day, "date": day, "n": 1}
        marked.append(code)
    _prune_attempts(attempts)
    return marked


def _write_catchup_attempts(
    owner: Any, attempts: dict[str, dict[str, Any]], *, logger: logging.Logger | None,
) -> bool:
    """把整张尝试记录写成 ``data_refresh_state`` 的一行（一次 SQLite 事务）。

    写失败**不抛**：记账只影响"下一轮能不能延迟"，绝不能炸掉价格步骤本身
    （下轮抓完再记一次即可）。返回是否写成功。
    """
    try:
        owner._write_refresh_state(
            _QFQ_CATCHUP_ATTEMPTS_KEY,
            json.dumps(attempts, ensure_ascii=False, sort_keys=True),
        )
    except Exception as error:  # noqa: BLE001 - 记账失败只影响"能不能延迟"
        if logger is not None:
            logger.warning("写入 qfq 追平尝试记录失败: %s", error)
        return False
    return True


def mark_qfq_catchup_attempts(
    owner: Any,
    watermarks: dict[str, tuple[str | None, str | None]],
    *,
    on_date: str,
) -> int:
    """记一次"本交易日以该 ``(raw, qfq)`` 水位成功落库"的尝试，返回记账只数。

    这是**单次/兜底**入口；流水线里每批落盘走
    :class:`QfqCatchupAttemptTracker`（同一套 :func:`_apply_catchup_attempts`
    口径，只是把"每批一次读+写"降成"内存累积 + 每批一次写"）。

    ``watermarks`` 必须是**落库之后**的水位（抓取失败不写 → 下轮照旧重试）。
    同一 ``(raw, qfq)`` + 同一交易日 → ``n+1``；任一水位变化 → 重新从 1 记
    （水位前进说明源侧已经动过，旧记录的"已尝试满"不再成立）。
    换交易日整表作废 —— 这就是"最迟下个交易日必重试"的落地机制。
    """
    day = _day_text(on_date)
    if day is None or not watermarks:
        return 0
    loaded = load_qfq_catchup_attempts(owner)
    attempts = {
        code: record for code, record in loaded.items() if record.get("date") == day
    }
    marked = len(_apply_catchup_attempts(attempts, watermarks, day))
    if not marked and attempts == loaded:
        return 0
    if not _write_catchup_attempts(owner, attempts, logger=getattr(owner, "logger", None)):
        return 0
    return marked


class QfqCatchupAttemptTracker:
    """把"本交易日以该 ``(raw, qfq)`` 水位成功落库"的记账**跟着批次落盘**。

    为什么不能只在步末写（本模块顶部「记账必须跟着批次落盘」一节）：
    延迟判据读的就是这条记录，而旧实现只在 :func:`_finalize_price_step`
    （整步跑完）写 —— 价格步跑不到终点 → 记录永不落盘 → 永不延迟 →
    下轮又跑满全市场 → 又跑不完，自锁。

    设计约束（本类就是这三条的实现）：

    * **只记成功落库的**：调用方只把 ``_persist_price_batch`` 返回的
      ``updated_codes`` 增量交进来；抓取/落库失败的股票不记账 → 下轮重试。
    * **低频**：状态在内存里累积，**攒够 100 只或过了 15 秒**才写一次
      ``_write_refresh_state``（整张表一个 JSON、一个 SQLite 事务），
      步末/异常收尾再兜底一次 —— 不是每只股票一次，也不是每批必写。
    * **幂等**：同一 (code, 水位, 交易日) 重复落盘只会把 ``n`` 再加一次 ——
      与"抓了一次就记一次"一致；flush 失败不清账，下次 flush 会补写。
    """

    __slots__ = (
        "_attempts", "_clock", "_day", "_dirty", "_flush_seconds", "_flushes",
        "_last_flush_at", "_loaded", "_pending_since_flush", "_recorded_codes",
        "_total_marked",
    )

    def __init__(
        self, *, clock: Callable[[], float], on_date: str | None = None,
    ) -> None:
        # 时钟由调用方注入（与模块内其余计时代码同一口径：不给测试留下
        # "偷偷读真实时钟"的第二条路径）。
        self._clock = clock
        self._attempts: dict[str, dict[str, Any]] = {}
        self._day = _day_text(on_date) or cn_today().strftime("%Y-%m-%d")
        self._loaded = False
        self._dirty = False
        self._total_marked = 0
        self._flushes = 0
        self._flush_seconds = 0.0
        self._pending_since_flush = 0
        self._last_flush_at = clock()
        self._recorded_codes: set[str] = set()

    # ── 只读属性（供报告拼装）────────────────────────────────────────
    @property
    def total_marked(self) -> int:
        """本轮累计记账只数（= 报告里的 ``qfq_catchup_attempts_marked``）。"""
        return self._total_marked

    @property
    def flushes(self) -> int:
        """本轮真正落盘的次数（每批一次 → 用来证明"低频"）。"""
        return self._flushes

    @property
    def flush_seconds(self) -> float:
        """本轮记账落盘的累计耗时（验收"不能让价格步变慢"的实测口径）。"""
        return self._flush_seconds

    @property
    def recorded_codes(self) -> set[str]:
        """本轮已记账的代码（步末兜底只补差集，避免同一只重复记）。"""
        return self._recorded_codes

    def _ensure_loaded(self, owner: Any) -> None:
        if self._loaded:
            return
        loaded = load_qfq_catchup_attempts(owner)
        # 换交易日整表作废：只保留今天的，其余不带进本轮写回。
        self._attempts = {
            code: record for code, record in loaded.items()
            if record.get("date") == self._day
        }
        self._loaded = True

    def record(
        self,
        owner: Any,
        watermarks: dict[str, tuple[str | None, str | None]],
        *,
        logger: logging.Logger | None = None,
    ) -> int:
        """把一批落库后水位记入内存（**不写盘**），返回本批记上的只数。"""
        if not watermarks:
            return 0
        self._ensure_loaded(owner)
        marked = _apply_catchup_attempts(self._attempts, watermarks, self._day)
        if not marked:
            return 0
        self._recorded_codes.update(marked)
        self._total_marked += len(marked)
        self._pending_since_flush += len(marked)
        self._dirty = True
        if logger is not None:
            logger.debug("qfq 追平尝试已记账 %d 只（累计 %d）", len(marked), self._total_marked)
        return len(marked)

    def flush(self, owner: Any, *, logger: logging.Logger | None = None) -> bool:
        """把内存里的整理结果落一次盘（无变化则**不写**，天然幂等）。"""
        if not self._dirty:
            return False
        started = self._clock()
        written = _write_catchup_attempts(owner, self._attempts, logger=logger)
        self._flush_seconds += self._clock() - started
        if not written:
            return False
        self._dirty = False
        self._flushes += 1
        self._pending_since_flush = 0
        self._last_flush_at = self._clock()
        return True

    def flush_if_due(self, owner: Any, *, logger: logging.Logger | None = None) -> bool:
        """攒够 ``_QFQ_CATCHUP_FLUSH_STOCKS`` 只、或距上次落盘超过
        ``_QFQ_CATCHUP_FLUSH_SECONDS`` 秒，才真的写一次（低频但不丢太多）。"""
        if not self._dirty:
            return False
        if self._pending_since_flush < _QFQ_CATCHUP_FLUSH_STOCKS and (
            self._clock() - self._last_flush_at < _QFQ_CATCHUP_FLUSH_SECONDS
        ):
            return False
        return self.flush(owner, logger=logger)

    def mark_and_flush(
        self,
        owner: Any,
        watermarks: dict[str, tuple[str | None, str | None]],
        *,
        logger: logging.Logger | None = None,
    ) -> int:
        """记账 + 按阈值/时间兜底落盘（每个落盘批次调用一次）。"""
        marked = self.record(owner, watermarks, logger=logger)
        if marked:
            self.flush_if_due(owner, logger=logger)
        return marked


def raw_bars_beyond_qfq(
    owner: Any, codes: Any, *, logger: logging.Logger,
) -> dict[str, int]:
    """每只股票**自己的** raw K 线里排在它 qfq 最新日之后的根数（= gap）。

    用该股自己的 raw 序列计数，天然跳过停牌与节假日（不按日历天算）。
    没有 qfq 行 / 查不出结果的股票**不在**返回字典里：调用方按"判不出 → 不延迟"。
    """
    requested = [str(code) for code in dict.fromkeys(codes) if code]
    if not requested:
        return {}
    try:
        rows = owner.duck.read_query(
            """WITH target AS (
                   SELECT unnest(?::VARCHAR[]) AS stock_code
               ), qfq AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_qfq
                   FROM price_daily_qfq price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               )
               SELECT price.stock_code AS stock_code,
                      COUNT(*) AS bars_beyond
               FROM price_daily_raw price
               JOIN qfq ON qfq.stock_code = price.stock_code
               WHERE price.trade_date > qfq.latest_qfq
               GROUP BY price.stock_code""",
            [requested],
        )
    except Exception as error:  # noqa: BLE001 - 判不出 gap 时调用方按"不延迟"处理
        logger.warning("统计 qfq 落后自己 raw 的 K 线根数失败（本轮不延迟）: %s", error)
        return {}
    gaps: dict[str, int] = {}
    for row in rows:
        try:
            gaps[str(row["stock_code"])] = int(row["bars_beyond"])
        except (KeyError, TypeError, ValueError):
            continue
    return gaps


def qfq_raw_watermarks(
    owner: Any, codes: Any, *, logger: logging.Logger,
) -> dict[str, tuple[str | None, str | None]]:
    """读取一批股票**落库后**的 ``(raw 最新日, qfq 最新日)``（尝试记录的入参）。"""
    requested = [str(code) for code in dict.fromkeys(codes) if code]
    if not requested:
        return {}
    try:
        rows = owner.duck.read_query(
            """WITH target AS (
                   SELECT unnest(?::VARCHAR[]) AS stock_code
               ), raw AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_raw
                   FROM price_daily_raw price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               ), qfq AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_qfq
                   FROM price_daily_qfq price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               )
               SELECT target.stock_code AS stock_code,
                      CAST(raw.latest_raw AS VARCHAR) AS latest_raw,
                      CAST(qfq.latest_qfq AS VARCHAR) AS latest_qfq
               FROM target
               LEFT JOIN raw ON raw.stock_code = target.stock_code
               LEFT JOIN qfq ON qfq.stock_code = target.stock_code""",
            [requested],
        )
    except Exception as error:  # noqa: BLE001 - 读不出水位 = 不记尝试（下轮照旧重试）
        logger.warning("读取落库后 raw/qfq 水位失败（本轮不记追平尝试）: %s", error)
        return {}
    return {
        str(row["stock_code"]): (
            _day_text(row.get("latest_raw")), _day_text(row.get("latest_qfq")),
        )
        for row in rows
    }


def qfq_lag_summary(
    owner: Any, target_codes: list[str], target_date: str, *, logger: logging.Logger,
) -> dict[str, Any]:
    """Count target stocks whose local qfq history still ends before **own raw**.

    2026-11-13 P1（1）：生产 job 207 收尾 0 失败、却只有 959/5,572 只 qfq
    到达当日（raw 已到 11-13）。因为 raw/qfq 是成对抓取的，两侧都可能
    "成功但只到前一天"（源侧 qfq 滞后），步骤只按 fail_count 判定就会
    报 success，下一轮又把 4,613 只重新选中，形成"每轮补 qfq"的稳态。

    2026-11-20 P0（速度退化复盘，reports/151）：判据原先比的是**日历目标日**
    （``MAX(qfq.trade_date) < target_date``），把「源侧今天整市场都还没发」
    与「这只股票的复权序列没跟上自己的日线」混成一件事。两者必须分开：

    * ``qfq_lag_*``（本函数的降级判据）= **qfq 落后自己的 raw**。这才是
      "复权序列没跟上"的定义，也是 job 207 事故的真正形态（raw 已到
      11-13、qfq 停在 11-12）。raw 尚未到达 target 时它不会误报。
    * ``raw_lag_count`` / ``qfq_below_target_count`` = 相对日历目标日的
      **源侧**水位，只做可观测性，不参与降级（否则"源侧还没发"整市场
      97% 命中 → 每轮 partial(qfq_lagged) 假红）。

    NULL qfq 与 NULL raw（连一行都没有）同样计入滞后，保持"缺数据必须
    可见"的保守方向：本改动只去掉"raw 也没到却怪 qfq"的误判，不放宽
    任何真实缺口。
    """
    codes = [str(code) for code in dict.fromkeys(target_codes) if code]
    if not codes:
        return {
            "qfq_lag_count": 0, "qfq_lag_ratio": 0.0, "qfq_lag_codes": [],
            "raw_lag_count": 0, "qfq_below_target_count": 0,
        }
    try:
        rows = owner.duck.read_query(
            """WITH target AS (
                   SELECT unnest(?::VARCHAR[]) AS stock_code
               ), raw AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_raw
                   FROM price_daily_raw price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               ), qfq AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_qfq
                   FROM price_daily_qfq price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               )
               SELECT target.stock_code AS stock_code,
                      CAST(raw.latest_raw AS VARCHAR) AS latest_raw,
                      CAST(qfq.latest_qfq AS VARCHAR) AS latest_qfq
               FROM target
               LEFT JOIN raw ON raw.stock_code = target.stock_code
               LEFT JOIN qfq ON qfq.stock_code = target.stock_code
               ORDER BY target.stock_code""",
            [codes],
        )
    except Exception as error:
        logger.warning("统计 qfq 滞后失败（不改变步骤状态）: %s", error)
        return {
            "qfq_lag_count": 0,
            "qfq_lag_ratio": 0.0,
            "qfq_lag_codes": [],
            "qfq_lag_error": str(error),
        }
    lag_codes: list[str] = []
    raw_lag = 0
    qfq_below_target = 0
    for row in rows:
        code = str(row["stock_code"])
        latest_raw = None if row.get("latest_raw") is None else str(row["latest_raw"])[:10]
        latest_qfq = None if row.get("latest_qfq") is None else str(row["latest_qfq"])[:10]
        # 真滞后：qfq 缺行，或与自己的 raw 相比没跟上。
        if latest_qfq is None or latest_raw is None or latest_qfq < latest_raw:
            lag_codes.append(code)
        if latest_raw is None or latest_raw < target_date:
            raw_lag += 1
        if latest_qfq is None or latest_qfq < target_date:
            qfq_below_target += 1
    return {
        "qfq_lag_count": len(lag_codes),
        "qfq_lag_ratio": round(len(lag_codes) / len(codes), 4),
        "qfq_lag_codes": lag_codes[:20],
        # 源侧水位（可观测性，不参与降级）
        "raw_lag_count": raw_lag,
        "qfq_below_target_count": qfq_below_target,
    }


def qfq_watermarks(
    owner: Any, codes: Iterable[str], *, logger: logging.Logger,
) -> dict[str, str | None]:
    """读取一批股票**落库后**的 qfq 最新日（technical_pending 的水位）。

    与 `_qfq_lag_watermarks` 的区别：这里不比较 raw，只回答"这只股票
    当前 qfq 写到哪一天"，供 `_mark_technical_pending` 做同水位去重。
    """
    requested = [str(code) for code in dict.fromkeys(codes) if code]
    if not requested:
        return {}
    try:
        rows = owner.duck.read_query(
            """SELECT stock_code, CAST(MAX(trade_date) AS VARCHAR) AS latest_qfq
               FROM price_daily_qfq
               WHERE stock_code IN (SELECT unnest(?::VARCHAR[]))
               GROUP BY stock_code""",
            [requested],
        )
    except Exception as error:  # noqa: BLE001 - 水位探针失败不改变价格步骤状态
        logger.warning("读取 qfq 水位失败（technical_pending 不写入）: %s", error)
        return {}
    found = {
        str(row["stock_code"]): (
            None if row.get("latest_qfq") is None else str(row["latest_qfq"])[:10]
        )
        for row in rows
    }
    # 没有 qfq 行的股票也入列（水位 None），确保下一轮会尝试重算。
    return {code: found.get(code) for code in requested}


def qfq_lag_watermarks(
    owner: Any, target_codes: Iterable[str], *, logger: logging.Logger,
) -> dict[str, str | None]:
    """列出 **qfq 最新日 < raw 最新日** 的股票及其 qfq 水位（NULL 也算滞后）。

    与 `_qfq_lag_summary` 的区别：那个比的是"目标交易日"，这里比的是
    "同一只股票自己的 raw 最新日"。前者用于把价格步骤降级 partial（源侧
    整体滞后），后者用于判定"这只股票的复权序列没跟上自己的日线"，
    因为技术指标只读 qfq —— raw 到没到齐与指标可信与否是两件事。
    """
    codes = [str(code) for code in dict.fromkeys(target_codes) if code]
    if not codes:
        return {}
    try:
        rows = owner.duck.read_query(
            """WITH target AS (
                   SELECT unnest(?::VARCHAR[]) AS stock_code
               ), raw AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_raw
                   FROM price_daily_raw price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               ), qfq AS (
                   SELECT price.stock_code, MAX(price.trade_date) AS latest_qfq
                   FROM price_daily_qfq price
                   JOIN target ON target.stock_code = price.stock_code
                   GROUP BY price.stock_code
               )
               SELECT raw.stock_code AS stock_code,
                      CAST(qfq.latest_qfq AS VARCHAR) AS latest_qfq
               FROM raw
               LEFT JOIN qfq ON qfq.stock_code = raw.stock_code
               WHERE qfq.latest_qfq IS NULL OR qfq.latest_qfq < raw.latest_raw
               ORDER BY raw.stock_code""",
            [codes],
        )
    except Exception as error:  # noqa: BLE001 - 探针失败不改变价格步骤状态
        logger.warning("统计 qfq 相对 raw 滞后失败（不写待重算名单）: %s", error)
        return {}
    return {
        str(row["stock_code"]): (
            None if row.get("latest_qfq") is None else str(row["latest_qfq"])[:10]
        )
        for row in rows
    }


def defer_qfq_source_lag(
    owner: Any,
    candidates: dict[str, tuple[str, str]],
    *,
    logger: logging.Logger,
    stats: dict[str, int] | None = None,
) -> set[str]:
    """裁决："``qfq < raw`` 的候选里，本轮哪些可以**跳过**"。

    只返回**同时**满足下列三条的代码（缺一条就照旧重选）：

    * ``1 ≤ gap < price_qfq_lag_bars`` —— 只差源侧发布节奏那一根；
    * 尝试记录的水位 ``(raw, qfq)`` 与**当前**水位逐字相等 —— 水位一动记录作废；
    * 记录的 ``date`` 就是**今天**且 ``n ≥ price_qfq_catchup_max_attempts``。

    gap ≥ ``price_qfq_lag_bars``（真滞后）在这里直接跳过判断 → 无条件重选；
    gap 查不到（``None``）同样不延迟。

    ``stats``（可选，原地累加）：把"**为什么没延迟**"逐条计数写进去 ——
    ``gap_unknown`` / ``gap_real_lag`` / ``record_missing`` /
    ``watermark_mismatch`` / ``date_mismatch`` / ``attempts_below_cap`` /
    ``deferred``。裁决失效时，光有 ``deferred=0`` 看不出卡在哪一环。
    """
    if not candidates:
        return set()
    reason_counts = stats if stats is not None else {}

    def bump(reason: str) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    lag_bars = _positive_int(
        getattr(owner, "price_qfq_lag_bars", None), _QFQ_LAG_BARS_DEFAULT,
    )
    max_attempts = _positive_int(
        getattr(owner, "price_qfq_catchup_max_attempts", None),
        _QFQ_CATCHUP_MAX_ATTEMPTS_DEFAULT,
    )
    gaps = raw_bars_beyond_qfq(owner, candidates, logger=logger)
    attempts = load_qfq_catchup_attempts(owner)
    today = cn_today().strftime("%Y-%m-%d")
    deferred: set[str] = set()
    for code, (raw_day, qfq_day) in candidates.items():
        gap = gaps.get(code)
        if gap is None:
            bump("gap_unknown")
            continue
        if gap < 1 or gap >= lag_bars:
            # gap ≥ lag_bars = 真滞后（不是源侧发布节奏）；gap < 1 = 判不出落后。
            bump("gap_real_lag")
            continue
        record = attempts.get(code)
        if not isinstance(record, dict):
            bump("record_missing")
            continue
        if record.get("raw") != raw_day or record.get("qfq") != qfq_day:
            bump("watermark_mismatch")
            continue
        if record.get("date") != today:
            bump("date_mismatch")
            continue
        try:
            count = int(record.get("n") or 0)
        except (TypeError, ValueError):
            bump("attempts_below_cap")
            continue
        if count < max_attempts:
            bump("attempts_below_cap")
            continue
        deferred.add(code)
        bump("deferred")
    return deferred


# ─── 源侧"永远没有这只股票的数据"的负缓存（2026-11-22） ──────────────────
# 实测事故：`002122`（ST汇洲）在库里**一根 K 线都没有**，源侧对目标交易日
# 始终不返回数据；而选股 SQL 把"raw 缺失"判为**硬缺口**（这是对的：缺口必须
# 可见，不许延迟）。于是它**每一轮都被重抓一次**，白花约 10 秒/轮（占整轮 5%），
# 且永远不可能成功。
#
# 处理方式刻意保守，只解决"永远填不上的空账户"：
#   * **只缓存"库里一行都没有"的股票**（`raw` 行数 = 0）。有历史行、只是落后
#     一两天的股票**不缓存** —— 源侧随时可能补发，缓存会拖慢正常更新。
#   * 缓存按 `(code, target_date)` 记，**TTL 24 小时**到期自动重试一次。
#   * 缺口**不会被隐藏**：报告里单列 `source_empty_cached_count/codes`，
#     日志一行说明，用户照样看得到"这几只源侧没数据"。
SOURCE_EMPTY_CACHE_KEY = "price_source_empty_cache"
SOURCE_EMPTY_TTL_SECONDS = 24 * 3600


def _load_source_empty_cache(owner: Any) -> dict[str, dict[str, Any]]:
    raw = owner._read_refresh_state(SOURCE_EMPTY_CACHE_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_source_empty_cache(
    owner: Any, cache: dict[str, dict[str, Any]], *, logger: logging.Logger,
) -> None:
    """写回负缓存，顺手丢掉过期条目（别让它无限长）。"""
    now = datetime.now(UTC)
    pruned: dict[str, dict[str, Any]] = {}
    for code, entry in cache.items():
        try:
            last = datetime.fromisoformat(str(entry.get("last_attempt") or ""))
        except ValueError:
            continue
        if (now - last).total_seconds() <= SOURCE_EMPTY_TTL_SECONDS:
            pruned[code] = entry
    try:
        owner._write_refresh_state(
            SOURCE_EMPTY_CACHE_KEY, json.dumps(pruned, ensure_ascii=False),
        )
    except Exception as error:  # noqa: BLE001 - 缓存写失败绝不影响更新
        logger.debug("价格源侧空数据负缓存写入失败: %s", error)


def _apply_source_empty_cache(
    owner: Any, target_stocks: list[dict], target_date: str, *,
    logger: logging.Logger,
) -> tuple[list[dict], list[str]]:
    """把"源侧确认无数据且 TTL 未到"的股票从本轮抓取集合里摘掉。

    返回 `(保留的股票, 被缓存挡下的代码)`。缓存读不出来/格式坏 → **原样放行**
    （宁可多扫一轮，也不能因为缓存问题漏更新）。
    """
    cache = _load_source_empty_cache(owner)
    if not cache:
        return list(target_stocks), []
    now = datetime.now(UTC)
    kept: list[dict] = []
    cached: list[str] = []
    for stock in target_stocks:
        code = str(stock["stock_code"])
        entry = cache.get(code)
        if (
            entry
            and str(entry.get("target_date") or "") == str(target_date)
            and entry.get("had_bars") is False
        ):
            try:
                last = datetime.fromisoformat(str(entry.get("last_attempt") or ""))
            except ValueError:
                kept.append(stock)
                continue
            if (now - last).total_seconds() < SOURCE_EMPTY_TTL_SECONDS:
                cached.append(code)
                continue
        kept.append(stock)
    if cached:
        logger.info(
            "[增量] 源侧空数据负缓存命中 %d 只（目标日 %s 内已确认源侧无数据，"
            "24 小时内不重复抓取；缺口仍然如实上报）: %s",
            len(cached), target_date, ", ".join(sorted(cached)[:20]),
        )
    return kept, sorted(cached)


def _refresh_source_empty_cache(
    owner: Any, target_codes: list[str], target_date: str, *,
    logger: logging.Logger,
) -> None:
    """按"本轮抓完后的真实水位"更新负缓存。

    只有**库里一行都没有**（`had_bars=False`）的股票才进缓存；一只股票一旦
    有了行情行，就把它的缓存条目删掉（说明源侧其实有数据，问题已消失）。
    """
    codes = [str(code) for code in dict.fromkeys(target_codes) if code]
    if not codes:
        return
    try:
        rows = owner.duck.read_query(
            """SELECT price.stock_code AS stock_code,
                      COUNT(*) AS bar_count,
                      MAX(price.trade_date) AS latest_raw
               FROM price_daily_raw price
               WHERE price.stock_code IN (SELECT unnest(?::VARCHAR[]))
               GROUP BY price.stock_code""",
            [codes],
        )
    except Exception as error:  # noqa: BLE001
        logger.debug("价格源侧空数据负缓存：读取行情水位失败: %s", error)
        return
    stats = {
        str(row["stock_code"]): (
            int(row.get("bar_count") or 0),
            _day_text(row.get("latest_raw")),
        )
        for row in rows
    }
    cache = _load_source_empty_cache(owner)
    now_iso = datetime.now(UTC).isoformat()
    changed = False
    for code in codes:
        bar_count, latest_raw = stats.get(code, (0, None))
        if bar_count == 0:
            # 源侧确实没有这只股票的行情：记下来，24 小时内别再抓。
            cache[code] = {
                "target_date": str(target_date),
                "last_attempt": now_iso,
                "had_bars": False,
            }
            changed = True
        else:
            # **只要有行情行就一律移出缓存** —— 不管有没有追平。
            # 反例（2026-11-22 自查抓到的自查 bug）：一只股票先被缓存（那时它一行
            # 都没有），后来源侧有了历史行但还没追平；旧实现在这个分支什么都不做，
            # 于是**过期条目继续压制它最多 24 小时**。有历史行说明源侧是有数据的，
            # 就该恢复正常每轮重试。
            if cache.pop(code, None) is not None:
                changed = True
    if changed:
        _save_source_empty_cache(owner, cache, logger=logger)


def _split_qfq_scope_rows(
    owner: Any,
    rows: list[dict],
    *,
    pending_set: set[str],
    target_date: str,
    logger: logging.Logger,
    reasons: dict[str, int] | None = None,
) -> tuple[list[dict], list[str]]:
    """把选股结果切成"本轮要抓"与"源侧滞后、本轮延迟"两半（顺序保持不变）。

    硬缺口（``raw``/``qfq`` 为 NULL、``raw < target``、两个血缘缺失、在
    ``qfq_refetch_pending`` 里）**一个都不许进延迟集合**：这里的判据与选股 SQL
    的 WHERE 逐条对应（血缘标志由同一条 SQL 的 SELECT 列给出），任何一项判不出
    都按硬缺口处理（方向 = 多扫一轮，不是漏更新）。

    ``reasons``（可选，原地累加）：入选原因/未延迟原因的逐条计数，供报告回答
    "这轮为什么延迟了 N 只（或为什么一只都没延迟）"。
    """
    counts = reasons if reasons is not None else {}
    if not rows:
        return list(rows), []
    hard_gap_count = 0
    candidates: dict[str, tuple[str, str]] = {}
    for row in rows:
        code = str(row["stock_code"])
        raw_day = _day_text(row.get("latest_raw_date"))
        qfq_day = _day_text(row.get("latest_qfq_date"))
        hard_gap = (
            raw_day is None
            or qfq_day is None
            or raw_day < target_date
            or not _flag(row, "raw_lineaged")
            or not _flag(row, "qfq_lineaged")
            or code in pending_set
        )
        if hard_gap:
            hard_gap_count += 1
        else:
            candidates[code] = (raw_day, qfq_day)
    if hard_gap_count:
        counts["hard_gap"] = counts.get("hard_gap", 0) + hard_gap_count
    deferred = defer_qfq_source_lag(owner, candidates, logger=logger, stats=counts)
    if not deferred:
        return list(rows), []
    logger.warning(
        "[增量] 源侧 qfq 滞后：%d/%d 只与自己的 raw 只差不到 %s 根且本交易日已尝试满"
        " → 本轮跳过（不算失败、不降级 partial）: %s",
        len(deferred),
        len(rows),
        getattr(owner, "price_qfq_lag_bars", _QFQ_LAG_BARS_DEFAULT),
        ", ".join(sorted(deferred)[:20]),
    )
    kept = [row for row in rows if str(row["stock_code"]) not in deferred]
    return kept, sorted(deferred)


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
    pending_set = set(pending_qfq_codes)
    pending_filter = ""
    query_params: list[Any] = [target_date]
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
    #
    # 2026-11-20（reports/151 §1）：``qfq < target`` 改成 ``qfq < raw``（候选）。
    # 前者在"当晚 raw 已发、qfq 未发"的几小时里对全市场恒真 → 每轮重选 5400+ 只。
    # 硬缺口的四个子句（raw/qfq 为 NULL、raw < target、两个血缘 NOT EXISTS）
    # 一字未改；候选的最终去留由 `_split_qfq_scope_rows` 裁决，判定"硬缺口"所需的
    # 血缘标志由同一条 SQL 的 SELECT 列（raw_lineaged / qfq_lineaged）给出。
    try:
        stocks = owner.duck.read_query(
            f"""SELECT stock.stock_code, stock.exchange,
                          raw.latest_raw_date, qfq.latest_qfq_date,
                          EXISTS (
                              SELECT 1 FROM source_audit audit
                              JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                              WHERE audit.stock_code = stock.stock_code
                                AND audit.field_name = 'latest_close'
                                AND audit.report_date = raw.latest_raw_date
                                AND batch.data_type = 'price_daily_raw'
                          ) AS raw_lineaged,
                          EXISTS (
                              SELECT 1 FROM source_audit audit
                              JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
                              WHERE audit.stock_code = stock.stock_code
                                AND audit.field_name = 'latest_close'
                                AND audit.report_date = qfq.latest_qfq_date
                                AND batch.data_type = 'price_daily_qfq'
                          ) AS qfq_lineaged
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
                           OR raw.latest_raw_date < ?
                           OR qfq.latest_qfq_date < raw.latest_raw_date
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

    # 候选 + 裁决：硬缺口全部留下，``qfq < raw`` 里"只差源侧那一根且已尝试满"的
    # 才本轮跳过（不算失败、不降级 partial、不写待重算名单）。
    defer_reasons: dict[str, int] = {}
    target_stocks, deferred_codes = _split_qfq_scope_rows(
        owner, stocks,
        pending_set=pending_set,
        target_date=target_date,
        logger=logger,
        reasons=defer_reasons,
    )
    if not target_stocks:
        logger.info(
            "[增量] 所有非停牌股票价格已是最新，无需更新（其中 %d 只源侧 qfq 滞后，"
            "本交易日已尝试满，本轮不重扫）",
            len(deferred_codes),
        )
        # 全部被延迟时这是**唯一**一条能看见裁决结果的路径：早退报告必须带上
        # 延迟只数与逐条原因，否则"源侧 qfq 滞后"在生产里永远是隐形的
        # （reports/151 §1 的死循环就是这么藏了三天）。
        return {
            "status": "skipped",
            "reason": "prices_up_to_date",
            "deferred_qfq_source_lag_count": len(deferred_codes),
            "deferred_qfq_source_lag_codes": deferred_codes[:20],
            "qfq_catchup_defer_reasons": defer_reasons,
        }

    try:
        priority_codes = {
            row["stock_code"]
            for row in owner.sqlite.query("SELECT DISTINCT stock_code FROM watchlist")
        }
    except Exception as error:
        priority_codes = set()
        logger.warning("读取研究优先名单失败，保持代码顺序: %s", error)
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

    # 2026-11-22：摘掉"源侧确认无数据且 24h 内已试过"的股票（只针对库里一行
    # 都没有的；有历史行只是落后的不动）。缺口照样进报告，只是本轮不打网络。
    target_stocks, source_empty_cached_codes = _apply_source_empty_cache(
        owner, target_stocks, target_date, logger=logger,
    )
    if not target_stocks:
        logger.info(
            "[增量] 全部 %d 只候选都被'源侧空数据负缓存'挡下（源侧确认无数据，"
            "24 小时内不重复抓取）: %s",
            len(source_empty_cached_codes), ", ".join(source_empty_cached_codes[:20]),
        )
    return PriceScope(
        source_empty_cached_codes=source_empty_cached_codes,
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
        deferred_codes=deferred_codes,
        defer_reasons=defer_reasons,
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

    # 追平尝试记账：跟着**每一个落盘批次**走，而不是等整步跑完（死循环的修法，
    # 见模块顶部「记账必须跟着批次落盘」）。状态在内存里累积，一批一次写。
    catchup_tracker = QfqCatchupAttemptTracker(clock=clock)

    def persist_batch(
        pending: list[tuple[str, Any, Any, bool, bool]],
        *,
        flush_buffer: bool = False,
    ) -> None:
        """落一批价格，并**立刻**把这一批"成功落库且 QFQ 仍落后"的记账落盘。

        记账点必须紧贴 ``_persist_price_batch`` 的返回值：``updated_codes`` 的
        增量 = 本批**真的落库成功**的股票（失败的不进 → 下轮照旧重试）。
        水位走 ``qfq_raw_watermarks``（落库后的库内真值，不是抓取结果的猜测）。
        """
        nonlocal success_count, updated_codes, fail_count
        before = len(updated_codes)
        success_count, updated_codes, fail_count = owner._persist_price_batch(
            pending, success_count, updated_codes, fail_count,
            buffer=write_buffer, flush_buffer=flush_buffer,
            failed_codes=failed_codes, before_write=fill_pipeline,
            batch_sizes=write_batch_sizes,
        )
        landed = updated_codes[before:]
        if landed:
            catchup_tracker.mark_and_flush(
                owner,
                qfq_raw_watermarks(owner, landed, logger=logger),
                logger=logger,
            )

    def flush_write_buffer() -> None:
        """Write the pending full-history buffer in one transaction.

        ``before_write`` 先补满流水线：写库期间主线程被占住，工作线程
        必须已经在跑下一批抓取，否则它们会空等（2026-11-12 P0 ③）。
        """
        persist_batch([], flush_buffer=True)

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
            persist_batch(batch)
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
            persist_batch(batch)
            batch.clear()
    finally:
        # ② 循环正常结束或抛异常都必须把剩余数据落盘：既包括未满一批的
        #    暂存项（batch），也包括攒批缓冲（write_buffer）。已 DELETE 的
        #    股票更不能停在"删了没插"的中间态。
        try:
            if batch:
                persist_batch(list(batch))
                batch.clear()
            if len(write_buffer):
                flush_write_buffer()
        except Exception as error:  # noqa: BLE001 - 收尾写入失败必须留痕
            logger.error(
                "价格收尾写入失败（暂存 %d 只 / 缓冲 %d 只）: %s",
                len(batch), len(write_buffer), error,
            )
        # 最后一道兜底：万一收尾写入/中途异常留下没落盘的记账，这里补一次。
        try:
            catchup_tracker.flush(owner, logger=logger)
        except Exception as error:  # noqa: BLE001 - 记账失败绝不能吞掉原异常
            logger.warning("qfq 追平尝试兜底落盘失败: %s", error)
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
        catchup_tracker=catchup_tracker,
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
    # 2026-11-20（reports/151 §1）：记"本交易日以这个 (raw, qfq) 水位成功抓过"。
    # 水位取**落库之后**的值（`updated_codes` 只含成功落库的股票 → 失败不写 →
    # 下轮照旧重试）。同一水位当日记满 `price_qfq_catchup_max_attempts` 次后，
    # 下一轮"只差源侧那一根"的候选才会被延迟；水位/交易日一变记录立刻作废。
    #
    # 2026-11-21：主记账已经搬进 `_run_price_pipeline`（每批一次落盘，见模块
    # 顶部「记账必须跟着批次落盘」）。这里只做**兜底**：补记流水线里没记到的
    # 成功代码（例如调用方没走流水线、或批次落盘后异常）。
    tracker = outcome.catchup_tracker
    attempts_marked = 0
    attempts_flushes = 0
    attempts_flush_seconds = 0.0
    if tracker is None:
        attempts_marked = mark_qfq_catchup_attempts(
            owner,
            qfq_raw_watermarks(owner, updated_codes, logger=logger),
            on_date=cn_today().strftime("%Y-%m-%d"),
        )
    else:
        remaining = [code for code in updated_codes if code not in tracker.recorded_codes]
        if remaining:
            tracker.mark_and_flush(
                owner,
                qfq_raw_watermarks(owner, remaining, logger=logger),
                logger=logger,
            )
        attempts_marked = tracker.total_marked
        attempts_flushes = tracker.flushes
        attempts_flush_seconds = tracker.flush_seconds
    logger.info(f"[增量] 价格更新完成: 成功 {success_count}, 失败 {fail_count}")
    # 可观测性（reports/151 §1.1）：延迟裁决与记账必须能在**日志**里看见 ——
    # 上一轮的死循环正是因为这两个数字从来没出现过。
    logger.info(
        "[增量] qfq 追平记账: qfq_catchup_attempts_marked=%d"
        "（落盘 %d 次 / %.3fs）, deferred_qfq_source_lag_count=%d, 裁决明细=%s",
        attempts_marked,
        attempts_flushes,
        attempts_flush_seconds,
        len(scope.deferred_codes),
        dict(scope.defer_reasons),
    )
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
        # 2026-11-20（reports/151）：判据是"qfq 落后自己的 raw"（真滞后）；
        # 源侧整体还没发到 target_date 只记在 raw_lag_count，不再降级，
        # 否则收盘后那一轮会把 97% 的正常波动报成 partial(qfq_lagged)。
        logger.warning(
            "[增量] qfq 落后自己 raw %d/%d 只（阈值 %d，源侧未到目标日 raw=%d/qfq=%d）"
            " → 价格步骤降级 partial(qfq_lagged): %s",
            qfq_lag["qfq_lag_count"],
            len(scope.target_stocks),
            qfq_lag_threshold,
            qfq_lag.get("raw_lag_count", 0),
            qfq_lag.get("qfq_below_target_count", 0),
            ", ".join(qfq_lag["qfq_lag_codes"]),
        )
    elif qfq_lag.get("raw_lag_count"):
        logger.info(
            "[增量] 源侧尚未发布到目标日 %s：raw 未到 %d 只 / qfq 未到 %d 只"
            "（qfq 与自己的 raw 齐平 → 不算滞后，不降级）",
            scope.target_date,
            qfq_lag.get("raw_lag_count", 0),
            qfq_lag.get("qfq_below_target_count", 0),
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
        # 源侧水位（reports/151）：raw/qfq 相对日历目标日的覆盖，不参与降级
        "raw_lag_count": qfq_lag.get("raw_lag_count", 0),
        "qfq_below_target_count": qfq_lag.get("qfq_below_target_count", 0),
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
        # 源侧 qfq 滞后（reports/151 §1）的可观测性：本轮延迟了哪些、记了几只尝试。
        # 被延迟的股票**不在** target_stocks 里 → 既不算失败、也不参与
        # `partial(qfq_lagged)` 降级判定、不进 tech_recompute_pending。
        "deferred_qfq_source_lag_count": len(scope.deferred_codes),
        "deferred_qfq_source_lag_codes": scope.deferred_codes[:20],
        # 2026-11-22：被"源侧空数据负缓存"挡下的股票（源侧确认无数据、24h 内不重抓）。
        # **缺口照样如实上报**：它仍是硬缺口，只是本轮没打网络。看这两项就知道
        # "为什么这轮没抓它、但它依然缺"。
        "source_empty_cached_count": len(scope.source_empty_cached_codes),
        "source_empty_cached_codes": scope.source_empty_cached_codes[:20],
        # 记账只看**成功落库**的股票 → 抓取/落库失败的下一轮照旧重试。
        "qfq_catchup_attempts_marked": attempts_marked,
        # 记账落盘证据（"批次走一次写"而不是"每只一次写"的实测口径）：
        # flushes = 本轮写了几次 data_refresh_state；flush_seconds = 这些写的累计耗时。
        "qfq_catchup_attempt_flushes": attempts_flushes,
        "qfq_catchup_attempt_flush_seconds": round(attempts_flush_seconds, 4),
        # 为什么延迟了这些、为什么另一些没延迟（gap 判不出 / 真滞后 / 记录为空 /
        # 水位不符 / 日期不符 / 次数不够）。延迟失效时先看这里。
        "qfq_catchup_defer_reasons": dict(scope.defer_reasons),
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
    # 2026-11-22：按"抓完之后的真实水位"更新源侧空数据负缓存。
    # 只有库里一行都没有的股票才进缓存（有历史行只是落后的不进），
    # 追平的股票会被自动移出。失败只记 debug，绝不影响更新结果。
    _refresh_source_empty_cache(
        owner,
        [str(stock["stock_code"]) for stock in scope.target_stocks],
        scope.target_date,
        logger=logger,
    )
    return _finalize_price_step(owner, scope, outcome, logger=logger, clock=clock)
