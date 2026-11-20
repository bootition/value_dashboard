"""指标重算**范围裁决**：这一轮到底算哪些股票（增量集合）还是全量。

## 为什么有这个模块

`flow.py` 旧实现里有一条漏网的例外：`share_capital_changed` 为真时直接
`IndicatorCalculator.compute_snapshot_for_all(...)`。股本/上市名单一天里
只要有一只股票变化，就等于对全市场 5,575 只重算 149 列（实测 ~35 分钟），
而真正受影响的通常只有几十到几百只。

现在判定集中到本模块的**单一入口** :func:`plan_indicator_scope`：

* **增量集合** = 所有"输入可能变了"的信号之并集（财务代码集 / 股本+上市状态+
  行业指纹差异集 / 价格代码集 / 待重算名单 / 重试成功 / 分红事件 / 回购融资）。
* **全量**只由四个可解释的原因触发：`computation_version`（口径版本 bump）、
  `first_run`（快照为空）、`input_fingerprint_bootstrap`（首次部署本机制，
  没有上一轮指纹可比）、`incremental_over_threshold`（增量集合超过全市场
  60%，一次全扫更省）、`forced_full`（显式强制）。

## "不会漏算"靠什么证明

1. 逐股**输入指纹**（`stock_meta` 的股本/上市/ST/停牌/行业 + 最新完整报告期
   三表核心列）持久化在 `data_refresh_state`，每轮与当前值做**集合差异**。
   差异集 = 自上轮指标计算以来输入变过的股票，无论变化来自本流程、外部脚本
   还是上一轮中途崩溃——差集不会因为"本轮没看到变化"而丢掉信号。
2. 只有**真正算成功**（step.status == success）才推进指纹水位；失败/隔离
   保留旧值 → 下一轮重新进入集合（宁可多算）。
3. 口径版本号写进 `indicator_snapshot.data_version`（`calc=...`）与
   `data_refresh_state[indicator_computation_version]`，公式一改就必须 bump，
   bump 后第一轮自动全量。

## 输入 → 影响列 → 何时必须重算（摘要，完整表见 reports/145）

| 输入面 | 影响的快照列 | 判据（本模块的代码来源名） |
|---|---|---|
| 三大报表（新报告期/修正值/覆盖写） | report_date、PE/PB/PS/PCF、ROE/ROA/毛利/净利/ROIC、YoY/CAGR、安全性 6 项、股东回报、ROCE | `financial` / `financial_stale` / `financial_backfill` / `financial_fingerprint` |
| 股本 total_shares/circ_shares | total_market_cap、circ_market_cap、每股股息 DPS、换手率 | `universe_fingerprint` |
| **历史股本链** `share_capital_history`（新增/改值/删行） | total_market_cap、circ_market_cap、PE/PB/PS/PCF、cumulative_dividend_amount、dividend_financing_ratio_pct | `share_capital_fingerprint`（逐股哈希 effective_date+total_shares） |
| **行情表被外部直接改写**（不经价格步） | latest_close/latest_price_date、MA5/10/20/60/120/250、turnover_rate、avg_volume、period_return、annualized_volatility、max_drawdown、估值 6 项、ttm_dividend_yield 与利差 | `price_fingerprint`（逐股哈希 raw/qfq 的相关列，进 price 组） |
| 上市状态/ST/停牌/行业 csrc_l1 | 名单成员 + 排名/筛选口径（市值排名分位） | `universe_fingerprint` |
| 行情 price_daily_raw/qfq | latest_close/date、估值价格域、MA/波动率/回撤/区间收益/成交量/换手 | `price` / `technical_pending` |
| 分红 dividends / 除权 xdxr | dividend_yield、连续分红年数、DPS、分红融资比 | `dividend_event` / `corporate_event` |
| 回购/融资事件 | cumulative_*_amount、dividend_financing_ratio_pct | `corporate_event` |
| 计算口径（公式改） | 全部 149 列 | `computation_version`（仅全量） |
| 国债曲线 | ttm_dividend_yield、div_yield_spread_* | 走 `refresh_treasury_spreads` 专用批量，不进本集合 |
"""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "DEFAULT_PERIODIC_FULL_DAYS",
    "INCREMENTAL_FULL_RATIO_THRESHOLD",
    "IndicatorScopePlan",
    "bootstrap_indicator_scope",
    "commit_indicator_scope",
    "current_computation_version",
    "event_fingerprints",
    "financial_fingerprints",
    "force_full_requested",
    "listed_stock_count",
    "manual_override_fingerprints",
    "plan_indicator_scope",
    "price_fingerprints",
    "share_capital_fingerprints",
    "snapshot_fingerprints",
    "snapshot_row_count",
    "snapshot_version_mismatch_codes",
    "universe_fingerprints",
]

logger = logging.getLogger(__name__)

# 增量集合超过全市场这个比例时，一次全扫比"逐股维护"更省：增量路径要跑
# 3 次发布（完整/价格域/事件域）并逐个 DELETE+INSERT，全量是一次批量
# staging 发布；交集越大，前者的固定开销与 SQL 次数劣势越明显。
INCREMENTAL_FULL_RATIO_THRESHOLD = 0.6
# 小样本保护：股票池很小时比例阈值会退化（2/3 只就算 66%），
# 少于这个只数一律按增量算，避免测试/小池反复全量。
INCREMENTAL_FULL_MIN_CODES = 5

UNIVERSE_FP_KEY = "indicator_universe_fingerprint:v1"
# v2（2026-11-20，验收报告漏算 #1/#4）：v1 只哈希"最新完整报告期"，
# 上一期修正与报告期回补都在格子外 → 换 key 让部署后的第一轮走
# `input_fingerprint_bootstrap` 全量建基线（可解释，不是悄悄发生）。
FINANCIAL_FP_KEY = "indicator_financial_fingerprint:v2"
# 分红 / 回购 / 融资事件：无公告驱动的手工插入也必须进集合。
EVENT_FP_KEY = "indicator_event_fingerprint:v1"
# 历史股本链 `share_capital_history`（2026-11-20 验收漏算 ⑦）：
# 市值/估值以前只由 `stock_meta` 股本驱动，**历史股本链新增一行就能让
# total_market_cap 与 cumulative_dividend_amount 变一个数量级**，而它不在任何
# 指纹里 → `skipped / planned_count=0`。新 key 让部署后第一轮走
# `input_fingerprint_bootstrap` 全量建基线（可解释，不是悄悄发生）。
SHARE_CAPITAL_FP_KEY = "indicator_share_capital_fingerprint:v1"
# 行情表**逐股内容**指纹（2026-11-20 验收漏算 ⑧）：价格域此前只由价格步返回的
# `updated_codes` 驱动，**外部直接 UPDATE price_daily_raw/qfq 谁都看不见**。
PRICE_FP_KEY = "indicator_price_fingerprint:v1"
# `manual_overrides` 里 status='published' 的行（PRD §9.5 R7 官方纠正路径）。
OVERRIDE_FP_KEY = "indicator_manual_override_fingerprint:v1"
# `indicator_snapshot` 逐股内容摘要：外部改坏/半写靠它当场发现（#3）。
SNAPSHOT_FP_KEY = "indicator_snapshot_fingerprint:v1"
COMPUTATION_VERSION_KEY = "indicator_computation_version"
LAST_SCOPE_KEY = "indicator_last_scope_decision"
# 周期全量兜底的"上次全量时间"。**只在真的全量成功后刷新**（增量轮不刷新），
# 否则常跑的实例永远不会到期。
PERIODIC_FULL_KEY = "indicator_periodic_full_last_at"

DEFAULT_PERIODIC_FULL_DAYS = 7
DEFAULT_FORCE_FULL = False

# 变化来源 → 重算组。full = 整股 149 列；price = 价格域（估值/行情/利差）；
# event = 仅分红融资比三列。判不准的来源一律归 full（宁可多算）。
SOURCE_DOMAIN: dict[str, str] = {
    "financial": "full",
    "financial_stale": "full",
    "financial_backfill": "full",
    "financial_fingerprint": "full",
    "retry": "full",
    "tech_pending": "full",
    "dividend_event": "full",
    "share_capital": "full",
    "universe_fingerprint": "full",
    # 2026-11-20 覆盖补全（验收报告 5 类漏算）：
    "event_fingerprint": "full",              # 分红/回购/融资行（#2）
    "manual_override_fingerprint": "full",    # PRD §9.5 R7 published 覆写（#5）
    "snapshot_state_mismatch": "full",        # 快照内容被外部改坏/半写（#3）
    "snapshot_version_mismatch": "full",      # 快照 data_version 与当前口径不一致
    "price": "price",
    "technical_pending": "price",
    "corporate_event": "event",
    # 2026-11-20 验收实测的最后 2 类漏算（⑦⑧）：
    "share_capital_fingerprint": "full",      # 历史股本链（⑦）→ 估值/分红口径全变
    "price_fingerprint": "price",             # 行情表被外部改写（⑧）→ 只重算价格域
}

# stock_meta 里**不进指纹**的列：`updated_at` 每次 listing_info 刷新都会变，
# name/pinyin/exchange 不参与任何指标。把 updated_at 算进指纹 = 每轮全量。
_UNIVERSE_META_COLUMNS = (
    "total_shares", "circ_shares", "is_listed", "is_st", "is_suspended",
    "listing_date", "csrc_l1", "csrc_l2",
)


@dataclass
class IndicatorScopePlan:
    """一轮指标重算的**范围裁决**结果（唯一可读索引）。"""

    mode: str
    reason: str | None
    computation_version: str
    listed_count: int
    changed_sources: dict[str, int] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    universe_fingerprint: dict[str, str] = field(default_factory=dict)
    financial_fingerprint: dict[str, str] = field(default_factory=dict)
    event_fingerprint: dict[str, str] = field(default_factory=dict)
    share_capital_fingerprint: dict[str, str] = field(default_factory=dict)
    price_fingerprint: dict[str, str] = field(default_factory=dict)
    manual_override_fingerprint: dict[str, str] = field(default_factory=dict)
    snapshot_fingerprint: dict[str, str] = field(default_factory=dict)
    prev_universe_fingerprint: dict[str, str] | None = None
    prev_financial_fingerprint: dict[str, str] | None = None
    prev_event_fingerprint: dict[str, str] | None = None
    prev_share_capital_fingerprint: dict[str, str] | None = None
    prev_price_fingerprint: dict[str, str] | None = None
    prev_manual_override_fingerprint: dict[str, str] | None = None
    prev_snapshot_fingerprint: dict[str, str] | None = None
    # 由**行情指纹**（而不是价格步 `_updated_codes`）抓到的代码：这些股票的最新
    # 收盘价/日期可能没变（改的是历史行或 qfq/成交量），`compute_price_sensitive`
    # 的"最新价相同就跳过"自检会把它们当成没事 → 必须强制重算。
    price_fingerprint_codes: list[str] = field(default_factory=list)
    fingerprint_error: str | None = None
    # 范围裁决整体失败（异常/读不出/算不出）时的原始异常与栈：必须进报告，
    # 不许静默吞掉（`scope_planning_failed` 只是给用户看的那一句）。
    planning_error: str | None = None
    planning_traceback: str | None = None

    @property
    def full_codes(self) -> list[str]:
        return list(self.groups.get("full", []))

    @property
    def price_codes(self) -> list[str]:
        return list(self.groups.get("price", []))

    @property
    def event_codes(self) -> list[str]:
        return list(self.groups.get("event", []))

    @property
    def planned_codes(self) -> list[str]:
        return list(dict.fromkeys([
            *self.full_codes, *self.price_codes, *self.event_codes,
        ]))

    @property
    def is_full(self) -> bool:
        return self.mode == "full"

    def report_fields(
        self,
        recomputed_count: int,
        *,
        skipped: int | None = None,
        step_reason: str | None = None,
    ) -> dict[str, Any]:
        """`report_step("indicators", ...)` 必须带的可解释字段。

        `reason` **不得覆盖计算器自己的失败/拒因**（`publish_gate_failed` /
        `changed_stock_not_ready` 这类事实比"这轮为什么选了全量"更要紧）：
        步骤自身有 reason 时它继续占 `reason`，本轮范围原因放 `scope_reason`。
        """
        planned = len(self.planned_codes)
        reason = self.reason
        if reason is None and planned == 0:
            # 一只都没算必须给出可解释的 reason，否则"为什么没算"查不出来。
            reason = "no_changed_stocks"
        return {
            "mode": self.mode,
            "reason": step_reason or reason,
            "scope_reason": reason,
            "computation_version": self.computation_version,
            "recomputed_count": int(recomputed_count),
            "skipped_count": int(
                max(0, self.listed_count - int(recomputed_count)) if skipped is None else skipped
            ),
            "changed_sources": dict(sorted(self.changed_sources.items())),
            "planned_count": planned,
            "planned_groups": {name: len(codes) for name, codes in sorted(self.groups.items())},
            "recomputed_codes": self.planned_codes[:200],
            # 判不准/算不出时的**事实**必须进报告（不许只留在日志里）。
            "fingerprint_error": self.fingerprint_error,
            "planning_error": self.planning_error,
        }


# ─── 状态读写（data_refresh_state 的 key/value，带来 owner 的薄封装）─────────


def current_computation_version() -> str:
    """口径版本的**唯一来源**在计算器模块（公式写在哪，版本号就定义在哪）。"""
    from app.core.indicators.calculator import INDICATOR_COMPUTATION_VERSION

    return INDICATOR_COMPUTATION_VERSION


def _read_json_map(owner: Any, key: str) -> dict[str, str] | None:
    raw = owner._read_refresh_state(key)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        logger.warning("指标范围状态 %s 解析失败，按缺失处理: %s", key, error)
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(code): str(fp) for code, fp in parsed.items()}


def _write_json_map(owner: Any, key: str, mapping: Mapping[str, str]) -> None:
    owner._write_refresh_state(key, json.dumps(dict(sorted(mapping.items())), ensure_ascii=False))


# ─── 输入指纹 ────────────────────────────────────────────────────────────


def universe_fingerprints(owner: Any) -> dict[str, str]:
    """逐股股本/上市状态/ST/停牌/CSRC 行业指纹（单次扫 stock_meta）。"""
    columns = ", ".join(_UNIVERSE_META_COLUMNS)
    rows = owner.duck.read_query(
        f"""SELECT stock_code,
                   substr(md5(CAST(({columns}) AS VARCHAR)), 1, 16) AS fp
            FROM stock_meta"""
    )
    return {str(row["stock_code"]): str(row["fp"]) for row in rows}


# ─── 输入指纹：**全历史**而不是"最新一期的 40 个格子"───────────────────
#
# 验收报告（2026-11-20）的根因原话：指纹只哈希"最新完整期 40 列 + stock_meta
# 8 列 = 49 个格子"，格子外的输入（历史期修正 / 报告期回补 / 分红行 / 已落库
# 快照 / manual_overrides）既不进集合，也**说不出"我不知道"** → 永不退回全量。
# 这里按输入面逐个补全，每只股票**每个输入面一个指纹**，差异集仍按股票求并集。
#
# 性能（同一个查询在真库副本 5,575 只 × 36 万行/表上实测，2026-11-20）：
# 旧实现"最新期 JOIN + md5(CAST(... AS VARCHAR))"约 102 ms；
# 新实现"全历史 + hash() 聚合"按表 30–45 ms（三表 ~114 ms）——比旧实现更快，
# 因为 DuckDB 的 `hash()` 是向量化整数哈希，不必把 55 列都转成字符串再 md5。
_FINANCIAL_TABLES = ("balance_sheet", "income_statement", "cash_flow")
# `raw_data` 是整段原始报文（体积最大且不是指标输入的有序数值）；
# `stock_code` 是分组键。其余列**全部进哈希**——新增财务科目自动被覆盖，
# 不需要有人记得回来加列（"未知信号默认进 full"在指纹层面同样成立）。
_FINANCIAL_EXCLUDED_COLUMNS = frozenset({"stock_code", "raw_data"})

# 分红 / 回购 / 融资：取"值列"，剔除抓取簿记列（fetch_time/raw_hash/batch_id/
# source/confidence 每轮刷新都变，算进指纹等于每轮全量）。
_EVENT_TABLES = ("dividends", "buyback_events", "funding_events")
_EVENT_EXCLUDED_COLUMNS = frozenset({
    "stock_code", "fetch_time", "raw_hash", "batch_id", "source", "confidence",
})

# 快照自检：`calculated_at` 每次重算必然不同（不是内容），`data_version` 单独走
# `snapshot_version_mismatch`（口径版本有它自己的可解释 reason）。
_SNAPSHOT_EXCLUDED_COLUMNS = frozenset({"calculated_at", "data_version"})


def _table_columns(owner: Any, table: str, excluded: frozenset[str]) -> list[str]:
    """表列清单（按 ordinal_position，稳定顺序）减掉永不进指纹的列。"""
    rows = owner.duck.read_query(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = ? ORDER BY ordinal_position""",
        [table],
    )
    columns = [str(row["column_name"]) for row in rows]
    if not columns:
        raise RuntimeError(f"指纹无法计算：表 {table} 不存在或没有列")
    return [name for name in columns if name not in excluded]


def _digest_by_stock(owner: Any, table: str, columns: list[str]) -> dict[str, tuple[int, int, int]]:
    """按股票哈希**该表全部行**：`(行数, Σhash, XOR hash)` 三个聚合。

    为什么是聚合摘要而不是 `string_agg + md5`：后者要把每行的所有列转成字符串，
    36 万行 × 55 列实测 ~1.1 s/表；`hash()` 是向量化整数哈希，同表 ~45 ms。
    行数 + 求和 + 异或三条同时撞的概率可以忽略，而且撞了也只会"少算"这一只，
    另有 `periodic_full_refresh` 兜底。
    """
    if not columns:
        return {}
    args = ", ".join(f'"{name}"' for name in columns)
    rows = owner.duck.read_query(
        f"""SELECT stock_code,
                   count(*) AS n,
                   sum(hash({args})) AS s,
                   bit_xor(hash({args})) AS x
            FROM {table}
            GROUP BY stock_code"""
    )
    return {
        str(row["stock_code"]): (
            int(row["n"] or 0),
            int(row["s"] or 0),
            int(row["x"] or 0),
        )
        for row in rows
    }


def _combine_digest(aggregates: list[tuple[int, int, int]]) -> str:
    payload = "|".join(f"{n}:{s}:{x}" for n, s, x in aggregates)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def _multisurface_fingerprints(
    owner: Any, tables: Iterable[str], excluded: frozenset[str],
) -> dict[str, str]:
    """多个输入表合并成"每只股票一个指纹"（表顺序固定，保证可复现）。"""
    collected: dict[str, list[tuple[int, int, int]]] = {}
    for table in tables:
        columns = _table_columns(owner, table, excluded)
        for code, aggregate in _digest_by_stock(owner, table, columns).items():
            collected.setdefault(code, []).append(aggregate)
    return {code: _combine_digest(aggs) for code, aggs in collected.items()}


def financial_fingerprints(owner: Any) -> dict[str, str]:
    """逐股**全部报告期**的三表指纹（覆盖同期修正 + 报告期回补）。

    v1 只看"最新完整期"，所以"上一期数值被修正"（YoY/TTM 的输入）和
    "补一个过去某期"都在指纹之外 —— 验收报告漏算 #1/#4 的根因。
    """
    return _multisurface_fingerprints(owner, _FINANCIAL_TABLES, _FINANCIAL_EXCLUDED_COLUMNS)


def event_fingerprints(owner: Any) -> dict[str, str]:
    """逐股分红 / 回购 / 融资事件行指纹（覆盖**无公告驱动**的手工插入）。"""
    return _multisurface_fingerprints(owner, _EVENT_TABLES, _EVENT_EXCLUDED_COLUMNS)


# 历史股本链里**真正进指标计算**的列（按 calculator.py / market_cap.py 逐行核对，
# 不是按表名猜）：
#   * `_calc_valuation` → `market_cap.latest_market_cap` → `resolve_shares_asof`
#     = `effective_date <= price_date` 的最近一条 `total_shares`；
#   * `_get_cumulative_dividend_amount` 的 `capital_windows` 窗口函数
#     = `ORDER BY effective_date, total_shares` 的 LEAD 区间。
# 其余列不参与任何快照列：`change_reason`/`is_anchor`/`verified` 只影响统计披露，
# 而 `source`/`raw_hash`/`batch_id` 每次重抓都会换（`capital.py` 是**全删全插**）
# —— 把它们算进指纹会让"内容没变的重抓"每轮触发整股重算，稳态直接退化。
_SHARE_CAPITAL_COLUMNS = ("effective_date", "total_shares")


def share_capital_fingerprints(owner: Any) -> dict[str, str]:
    """逐股**历史股本链**指纹（覆盖新增 / 改值 / 删行三种变化，验收漏算 ⑦）。

    修前 `total_market_cap` / `cumulative_dividend_amount` 只由 `stock_meta`
    的股本指纹驱动：给一只股票补一条 `share_capital_history`（或改/删一条），
    增量轮直接 `skipped / planned_count=0`，而市值与分红会差一个数量级。

    粒度：`(count(*), Σhash, XOR hash)` 三元聚合按 `(effective_date,
    total_shares)` 求——**全表全行**都进哈希（不做时间窗截断），因为
    `_get_cumulative_dividend_amount` 会逐笔回看**每一条**历史分红日的股本。
    实测真库 24.4 万行 / 5,551 只：**17–27 ms**（threads=2、只读连接）。
    """
    aggregates = _digest_by_stock(
        owner, "share_capital_history", list(_SHARE_CAPITAL_COLUMNS),
    )
    return {code: _combine_digest([agg]) for code, agg in aggregates.items()}


# 行情表里**真正被指标读到的列**（同样按 calculator.py 核对）：
#   * `_get_latest_price` / `compute_price_fields_for_stock` 取 raw 的 close；
#   * `_calc_technical` 读 raw 的 close/volume/turnover/turnover_rate
#     + qfq 的 close（按 trade_date 左连接）。
# `open/high/low` 不进任何快照列 → 不哈希（少一条"每轮多算"的噪声通道）。
_PRICE_FINGERPRINT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("price_daily_raw", ("trade_date", "close", "volume", "turnover", "turnover_rate")),
    ("price_daily_qfq", ("trade_date", "close")),
)


def price_fingerprints(owner: Any) -> dict[str, str]:
    """逐股**行情内容**指纹（覆盖"外部直接 UPDATE 行情表"，验收漏算 ⑧）。

    修前价格域只由价格步返回的 `_updated_codes` 驱动：有人绕过更新流程直接
    `UPDATE price_daily_raw/qfq` 时，增量轮 `skipped / planned_count=0`，
    15 列（pe_ttm/total_market_cap/ma5 …）永久停在旧值。

    粒度：**全表全行** `(count(*), Σhash, XOR hash)`，不是"最近 N 日"摘要——
    实测 1,759 万 + 1,757 万行上，全行哈希与"MAX+COUNT"同量级（扫描/分组是
    瓶颈，哈希不是），所以没必要为了省几十毫秒去留"改历史行看不见"的盲区。
    实测（threads=2，真库只读）：raw 5 列 0.36 s、qfq 2 列 0.25 s，合计 ~0.61 s。

    覆盖不到：整行 `DELETE` 到 0 行后再按**原样**插回（内容不变＝结果不变，
    所以不算漏）；以及 `price_daily_*` 之外的行情副本（ETF/港股行情表另域）。
    """
    collected: dict[str, list[tuple[int, int, int]]] = {}
    for table, columns in _PRICE_FINGERPRINT_TABLES:
        for code, aggregate in _digest_by_stock(owner, table, list(columns)).items():
            collected.setdefault(code, []).append(aggregate)
    return {code: _combine_digest(aggs) for code, aggs in collected.items()}


def manual_override_fingerprints(owner: Any) -> dict[str, str]:
    """逐股 `manual_overrides` 里 published 覆写的指纹（PRD §9.5 R7 官方纠正路径）。

    行数是个位到百位量级，直接在 SQLite 侧拼内容再摘要。含值、期次、状态与
    `rolled_back_at`：撤销一条覆写（状态变了）与改一个值都必须被看见。
    """
    rows = owner.sqlite.query(
        """SELECT stock_code, field_name, report_date, override_value,
                  COALESCE(status, '') AS status,
                  COALESCE(CAST(rolled_back_at AS TEXT), '') AS rolled_back_at
           FROM manual_overrides
           WHERE status = 'published'
           ORDER BY stock_code, field_name, report_date, id"""
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        code = str(row["stock_code"])
        grouped.setdefault(code, []).append(
            "|".join((
                str(row["field_name"]),
                str(row["report_date"]),
                repr(row["override_value"]),
                str(row["status"]),
                str(row["rolled_back_at"]),
            ))
        )
    return {
        code: hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()[:16]
        for code, lines in grouped.items()
    }


def snapshot_fingerprints(owner: Any) -> dict[str, str]:
    """逐股 `indicator_snapshot` **内容**指纹：外部改坏/半写的当场发现通道。

    `data_version` 只能证明"这行是这版公式算的"，证明不了"没被人改过"——
    验收报告 #3（`roe -999` / `latest_close 999`）就是靠它自愈的。
    """
    columns = _table_columns(owner, "indicator_snapshot", _SNAPSHOT_EXCLUDED_COLUMNS)
    aggregates = _digest_by_stock(owner, "indicator_snapshot", columns)
    return {code: _combine_digest([agg]) for code, agg in aggregates.items()}


def snapshot_version_mismatch_codes(owner: Any) -> list[str]:
    """`data_version` 与当前口径不一致的股票（口径 bump 后遗留行 / 半写入行）。"""
    from app.core.indicators.calculator import indicator_data_version

    rows = owner.duck.read_query(
        """SELECT DISTINCT stock_code FROM indicator_snapshot
           WHERE data_version IS DISTINCT FROM ?""",
        [indicator_data_version()],
    )
    return sorted(str(row["stock_code"]) for row in rows)


def snapshot_row_count(owner: Any) -> int:
    rows = owner.duck.read_query("SELECT COUNT(*) AS c FROM indicator_snapshot")
    return int(rows[0]["c"]) if rows else 0


def listed_stock_count(owner: Any) -> int:
    rows = owner.duck.read_query(
        "SELECT COUNT(*) AS c FROM stock_meta WHERE is_listed IS TRUE"
    )
    return int(rows[0]["c"]) if rows else 0


# ─── 裁决 ────────────────────────────────────────────────────────────────


def _diff_codes(previous: Mapping[str, str] | None, current: Mapping[str, str]) -> list[str]:
    """指纹集合差异：新增 / 删除 / 值变化都算"输入变了"。"""
    if not previous:
        return sorted(current)
    changed = {
        code for code, fp in current.items() if previous.get(code) != fp
    }
    changed.update(code for code in previous if code not in current)
    return sorted(changed)


def _group_codes(sources: Mapping[str, Iterable[str]]) -> tuple[dict[str, list[str]], dict[str, int]]:
    grouped: dict[str, dict[str, None]] = {"full": {}, "price": {}, "event": {}}
    counts: dict[str, int] = {}
    for source, codes in sources.items():
        unique = {str(code) for code in codes if code}
        counts[source] = len(unique)
        target = grouped[SOURCE_DOMAIN.get(source, "full")]
        for code in unique:
            target.setdefault(code, None)
    ordered = {name: sorted(codes) for name, codes in grouped.items()}
    return ordered, counts


def _update_config() -> dict[str, Any]:
    """`update` 段配置（读不到就用空字典：配置问题不得让范围裁决崩掉）。"""
    try:
        from app.core.config import Config

        section = Config.current().get_value("update", {})
        return section if isinstance(section, dict) else {}
    except Exception as error:  # noqa: BLE001 - 配置读不出来按默认值走
        logger.warning("读取 update 配置失败，按默认值处理: %s", error)
        return {}


def force_full_requested(owner: Any) -> bool:
    """`forced_full` 的**唯一设置点**（此前是没有任何写入方的空开关）。

    优先级：owner 属性（测试 / 嵌入方显式传）> 配置项 `update.indicator_force_full`。
    配置项在两个 `default.yaml` 里都有登记，运维改一行即可让下一轮全量。
    """
    attr = getattr(owner, "force_full_indicator_recompute", None)
    if attr is not None:
        return bool(attr)
    return bool(_update_config().get("indicator_force_full", DEFAULT_FORCE_FULL))


def periodic_full_days() -> int:
    """周期全量兜底的天数（默认 7 天，`update.indicator_periodic_full_days` 可配）。"""
    try:
        days = int(_update_config().get("indicator_periodic_full_days", DEFAULT_PERIODIC_FULL_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_PERIODIC_FULL_DAYS
    return max(1, days)


def _periodic_full_due(owner: Any, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """距上次**真的全量成功**是否已超过兜底周期（返回 (是否到期, 上次时间)）。"""
    days = periodic_full_days()
    stored = owner._read_refresh_state(PERIODIC_FULL_KEY)
    if not stored:
        return True, None
    try:
        last = datetime.fromisoformat(str(stored))
    except ValueError:
        logger.warning("周期全量水位 %s 无法解析，按到期处理", stored)
        return True, str(stored)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return reference - last >= timedelta(days=days), str(stored)


def _full_reason(
    owner: Any, *, force_full: bool,
) -> tuple[str | None, dict[str, Any]]:
    """可解释的全量原因（按"最需要解释"的优先级）。

    `forced_full` → `first_run` → `computation_version` → `periodic_full_refresh`。
    周期兜底排最后：它是**兜底**，有更具体的理由时优先报更具体的那个。
    """
    version = current_computation_version()
    if force_full:
        return "forced_full", {"computation_version": version}
    if snapshot_row_count(owner) == 0:
        return "first_run", {"computation_version": version}
    stored = owner._read_refresh_state(COMPUTATION_VERSION_KEY)
    if stored != version:
        return "computation_version", {
            "computation_version": version, "stored_computation_version": stored,
        }
    due, last_run = _periodic_full_due(owner)
    if due:
        return "periodic_full_refresh", {
            "computation_version": version,
            "periodic_full_days": periodic_full_days(),
            "periodic_full_last_at": last_run,
        }
    return None, {"computation_version": version}


# 输入面 → (当前指纹、上一轮水位 key、变化来源名)。新增输入面只能加在这里，
# 漏加 = 这个面永远不进增量集合（验收报告 5 类漏算就是这么产生的）。
_FINGERPRINT_SURFACES: tuple[tuple[str, str, str], ...] = (
    ("universe", UNIVERSE_FP_KEY, "universe_fingerprint"),
    ("financial", FINANCIAL_FP_KEY, "financial_fingerprint"),
    ("event", EVENT_FP_KEY, "event_fingerprint"),
    ("share_capital", SHARE_CAPITAL_FP_KEY, "share_capital_fingerprint"),
    ("price", PRICE_FP_KEY, "price_fingerprint"),
    ("manual_override", OVERRIDE_FP_KEY, "manual_override_fingerprint"),
    ("snapshot", SNAPSHOT_FP_KEY, "snapshot_state_mismatch"),
)


def _collect_fingerprints(owner: Any) -> dict[str, dict[str, str]]:
    """一次性算出七个输入面的逐股指纹。任一失败 → 整轮退回全量（宁可多算）。"""
    return {
        "universe": universe_fingerprints(owner),
        "financial": financial_fingerprints(owner),
        "event": event_fingerprints(owner),
        "share_capital": share_capital_fingerprints(owner),
        "price": price_fingerprints(owner),
        "manual_override": manual_override_fingerprints(owner),
        "snapshot": snapshot_fingerprints(owner),
    }


def _failed_plan(owner: Any, error: BaseException) -> IndicatorScopePlan:
    """范围裁决整体失败 → **真的**退回全量，并把异常写进报告。"""
    detail = f"{type(error).__name__}: {error}"
    logger.error("指标范围裁决失败，本轮退回全量: %s", detail, exc_info=error)
    try:
        listed_count = listed_stock_count(owner)
    except Exception:  # noqa: BLE001 - 连只数都读不出来也不能再抛
        listed_count = 0
    try:
        version = current_computation_version()
    except Exception:  # noqa: BLE001
        version = "unknown"
    return IndicatorScopePlan(
        mode="full",
        reason="scope_planning_failed",
        computation_version=version,
        listed_count=listed_count,
        fingerprint_error=detail,
        planning_error=detail,
        planning_traceback=traceback.format_exc(limit=12),
    )


def _plan_indicator_scope(
    owner: Any,
    *,
    sources: Mapping[str, Iterable[str]],
    force_full: bool,
) -> IndicatorScopePlan:
    reason, meta = _full_reason(owner, force_full=force_full)
    version = str(meta["computation_version"])
    listed_count = listed_stock_count(owner)

    unknown = sorted(name for name in sources if name not in SOURCE_DOMAIN)
    if unknown:
        # 新增/未知信号来源**默认进 full 组**（`SOURCE_DOMAIN.get` 已如此），
        # 这里再显式记一笔，避免"以为它被忽略"或"以为它被当价格域"。
        logger.info("指标范围裁决收到未知来源，按 full 处理: %s", unknown)

    current = _collect_fingerprints(owner)
    previous = {
        surface: _read_json_map(owner, key) for surface, key, _ in _FINGERPRINT_SURFACES
    }

    merged_sources: dict[str, Iterable[str]] = dict(sources)
    if reason is None:
        # 任何一个输入面没有上轮水位 → 无法证明"谁没变" → 一次全量建基线。
        missing = [
            surface for surface, _key, _src in _FINGERPRINT_SURFACES
            if previous[surface] is None
        ]
        if missing:
            logger.info("指标指纹基线缺失(%s)，本轮全量建立基线", ",".join(missing))
            reason = "input_fingerprint_bootstrap"
        else:
            for surface, _key, source_name in _FINGERPRINT_SURFACES:
                merged_sources[source_name] = _diff_codes(
                    previous[surface], current[surface],
                )
            # 口径版本的**行级**证据：快照里有行的 data_version 与当前口径不一致，
            # 说明上一轮只写了一部分（半写）或公式 bump 后残留 → 这些股票整股重算。
            merged_sources["snapshot_version_mismatch"] = snapshot_version_mismatch_codes(owner)

    groups, changed_sources = _group_codes(merged_sources)
    plan = IndicatorScopePlan(
        mode="incremental",
        reason=None,
        computation_version=version,
        listed_count=listed_count,
        changed_sources=changed_sources,
        groups=groups,
        universe_fingerprint=current["universe"],
        financial_fingerprint=current["financial"],
        event_fingerprint=current["event"],
        share_capital_fingerprint=current["share_capital"],
        price_fingerprint=current["price"],
        manual_override_fingerprint=current["manual_override"],
        snapshot_fingerprint=current["snapshot"],
        prev_universe_fingerprint=previous["universe"],
        prev_financial_fingerprint=previous["financial"],
        prev_event_fingerprint=previous["event"],
        prev_share_capital_fingerprint=previous["share_capital"],
        prev_price_fingerprint=previous["price"],
        prev_manual_override_fingerprint=previous["manual_override"],
        prev_snapshot_fingerprint=previous["snapshot"],
        price_fingerprint_codes=sorted({
            str(code) for code in (merged_sources.get("price_fingerprint") or []) if code
        }),
    )

    # 阈值只看**完整重算组**（整股 149 列），不看价格域组：
    # "今天全市场都成交了"是日常形态，价格域增量本身就会跳过没变的收盘价，
    # 拿它判全量等于每天退回 35 分钟。只有"一大半股票要整股重算"时，
    # 一次全扫（单事务 staging 发布）才真的比逐股维护更省。
    full_group_size = len(groups["full"])
    threshold = max(INCREMENTAL_FULL_MIN_CODES, int(listed_count * INCREMENTAL_FULL_RATIO_THRESHOLD))
    if reason is None and full_group_size > threshold:
        reason = "incremental_over_threshold"

    if reason is not None:
        plan.mode = "full"
        plan.reason = reason
    return plan


def plan_indicator_scope(
    owner: Any,
    *,
    sources: Mapping[str, Iterable[str]],
    force_full: bool | None = None,
) -> IndicatorScopePlan:
    """决定这一轮"算谁"：增量集合（默认）或全量（仅可解释原因）。

    **整体包 try/except**：任何异常 / 数据读不出 / 指纹算不出 → 返回 full，
    `reason="scope_planning_failed"`，异常与栈写进 `planning_error` 随报告出去。
    "判不准就归 full"以前只是文档里的一句话：代码里只要有一处抛异常，整轮更新
    就垮掉，既没全量也没增量。

    Args:
        owner: `IncrementalUpdater`（鸭子类型：duck/sqlite/_read_refresh_state/
            _write_refresh_state）。
        sources: 变化来源 → 代码集合。键不在 :data:`SOURCE_DOMAIN` 里时按
            **full** 处理（未知信号不得被默认忽略）。
        force_full: 显式强制全量；``None`` = 由 :func:`force_full_requested` 决定
            （owner 属性 / 配置项 `update.indicator_force_full`）。
    """
    try:
        requested = force_full_requested(owner) if force_full is None else bool(force_full)
        return _plan_indicator_scope(owner, sources=sources, force_full=requested)
    except Exception as error:  # noqa: BLE001 - 判不准必须真的退回全量
        return _failed_plan(owner, error)


def bootstrap_indicator_scope(owner: Any) -> dict[str, Any]:
    """把"当前输入指纹 + 当前口径版本"直接写成基线（**不重算任何股票**）。

    三个正当用途：
    1. 运维：明确知道现网快照就是当前口径、只想立刻切到增量模式时，用它建立
       基线，避免升级后的第一轮全量；
    2. 回归测试：钉住"上一轮水位"，让用例只验证本轮的变化来源；
    3. 周期兜底的"今天已经全量过了"声明（同时重置 `periodic_full` 计时）。

    常规路径**不需要**调用它：没有基线时 `plan_indicator_scope` 会判
    `input_fingerprint_bootstrap` 并跑一次全量建立基线。
    """
    current = _collect_fingerprints(owner)
    for surface, key, _source in _FINGERPRINT_SURFACES:
        _write_json_map(owner, key, current[surface])
    owner._write_refresh_state(COMPUTATION_VERSION_KEY, current_computation_version())
    owner._write_refresh_state(PERIODIC_FULL_KEY, datetime.now(UTC).isoformat())
    return {
        "universe_codes": len(current["universe"]),
        "financial_codes": len(current["financial"]),
        "event_codes": len(current["event"]),
        "share_capital_codes": len(current["share_capital"]),
        "price_codes": len(current["price"]),
        "manual_override_codes": len(current["manual_override"]),
        "snapshot_codes": len(current["snapshot"]),
    }


# ─── 收尾：只有算成功才推进指纹水位 ──────────────────────────────────────


def _step_succeeded(step: Mapping[str, Any] | None) -> bool:
    if not step:
        return False
    return str(step.get("status")) == "success" and not step.get("failed")


def _current_maps(plan: IndicatorScopePlan) -> dict[str, dict[str, str]]:
    return {
        "universe": plan.universe_fingerprint,
        "financial": plan.financial_fingerprint,
        "event": plan.event_fingerprint,
        "share_capital": plan.share_capital_fingerprint,
        "price": plan.price_fingerprint,
        "manual_override": plan.manual_override_fingerprint,
        "snapshot": plan.snapshot_fingerprint,
    }


def _previous_maps(plan: IndicatorScopePlan) -> dict[str, dict[str, str] | None]:
    return {
        "universe": plan.prev_universe_fingerprint,
        "financial": plan.prev_financial_fingerprint,
        "event": plan.prev_event_fingerprint,
        "share_capital": plan.prev_share_capital_fingerprint,
        "price": plan.prev_price_fingerprint,
        "manual_override": plan.prev_manual_override_fingerprint,
        "snapshot": plan.prev_snapshot_fingerprint,
    }


def commit_indicator_scope(owner: Any, plan: IndicatorScopePlan, step: Any) -> dict[str, Any]:
    """把本轮裁决结果落盘：口径版本 + 逐股指纹水位 + 最后的决策记录。

    三条规则：
    1. **只有 step 成功**才推进水位；partial/failed/rejected 一律保留旧指纹 ——
       下一轮这些股票会重新进入增量集合（宁可多算）。
    2. 快照面的指纹必须**重算后**再取（计划时取的是算之前的内容）：任何一组
       （full/price/event）真的算过，就用当前库里的内容重新摘一遍，否则下一轮会
       把自己刚写的结果当成"被外部改坏"。
    3. `periodic_full` 水位**只在真的全量成功后**刷新；增量轮不刷新，否则常跑
       的实例永远等不到周期兜底。
    """
    succeeded = _step_succeeded(step)
    committed_codes = 0
    snapshot_refreshed = False
    if succeeded:
        current = _current_maps(plan)
        if plan.planned_codes or plan.is_full:
            # 算过东西 → 快照内容变了，必须按**算完之后**的库重新摘要。
            current["snapshot"] = snapshot_fingerprints(owner)
            snapshot_refreshed = True
        if plan.is_full:
            for surface, key, _source in _FINGERPRINT_SURFACES:
                _write_json_map(owner, key, current[surface])
            owner._write_refresh_state(COMPUTATION_VERSION_KEY, plan.computation_version)
            owner._write_refresh_state(PERIODIC_FULL_KEY, datetime.now(UTC).isoformat())
            committed_codes = len(current["universe"])
        else:
            previous = _previous_maps(plan)
            for surface, key, _source in _FINGERPRINT_SURFACES:
                stored = dict(previous[surface] or {})
                latest = current[surface]
                # 哪些代码的**输入水位**可以由本轮算过之后推进：
                # * 默认：只有整股重算组（full）——它的输入被完整消费；
                # * snapshot：价格域/事件域重算也会改快照行，按"本轮算过的全部股票"；
                # * price：行情指纹驱动的股票落在**价格域**（不是 full），不推进
                #   就等于每轮重复检测同一处改动（⑦⑧ 补完之后的新面必须在这里
                #   登记，否则稳态从"算一次"退化成"每轮都算"）。
                if surface in ("snapshot", "price"):
                    advance = plan.planned_codes
                else:
                    advance = plan.full_codes
                for code in advance:
                    if code in latest:
                        stored[code] = latest[code]
                    else:
                        stored.pop(code, None)
                _write_json_map(owner, key, stored)
            if plan.prev_universe_fingerprint is not None:
                # 水位已建立，口径版本也必须是"当前版本"，否则下一轮又判
                # computation_version。增量路径只有在版本一致时才会走到这里。
                owner._write_refresh_state(COMPUTATION_VERSION_KEY, plan.computation_version)
            committed_codes = len(plan.full_codes)
    owner._write_refresh_state(
        LAST_SCOPE_KEY,
        json.dumps({
            "mode": plan.mode,
            "reason": plan.reason,
            "computation_version": plan.computation_version,
            "changed_sources": dict(sorted(plan.changed_sources.items())),
            "planned_count": len(plan.planned_codes),
            "committed_codes": committed_codes,
            "snapshot_refreshed": snapshot_refreshed,
            "fingerprint_error": plan.fingerprint_error,
            "planning_error": plan.planning_error,
            "succeeded": succeeded,
        }, ensure_ascii=False),
    )
    return {"committed_codes": committed_codes, "succeeded": succeeded}
