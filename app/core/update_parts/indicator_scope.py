"""指标重算**范围裁决**：这一轮到底算哪些股票（增量集合）还是全量。

## 为什么有这个模块

`flow.py` 旧实现里有一条漏网的例外：`share_capital_changed` 为真时直接
`IndicatorCalculator.compute_snapshot_for_all(...)`。股本/上市名单一天里
只要有一只股票变化，就等于对全市场 5,575 只重算 149 列（实测 ~35 分钟），
而真正受影响的通常只有几十到几百只。

现在判定集中到本模块的**单一入口** :func:`plan_indicator_scope`：

* **增量集合** = 所有"输入可能变了"的信号之并集（财务代码集 / 股本+上市状态+
  行业指纹差异集 / 价格代码集 / 待重算名单 / 重试成功 / 分红事件 / 回购融资）。
* **全量**只由可解释的原因触发：`computation_version`（口径版本 bump）、
  `first_run`（快照为空）、`input_fingerprint_bootstrap`（首次部署本机制，
  没有上一轮指纹可比）、`incremental_over_threshold`（增量集合超过全市场
  60%，一次全扫更省）、`trading_calendar_changed` / `treasury_curve_changed`
  （两个**全市场共用的标量输入面**变了，受影响集合说不出"只这几只"）、
  `periodic_full_refresh`（周期兜底）、`forced_full`（显式强制）。

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
| **交易日历** `trading_dates`（SQLite，被源端订正/外部脚本改写） | MA5/10/20/60/120/250、avg_volume、turnover_rate、period_return、annualized_volatility、max_drawdown（`_calc_technical` 用它判定"连续窗口"） | `trading_calendar_fingerprint`（每轮）+ `trading_calendar_gap_days`（日历变了**或行情行变了**才算，覆盖度判据）→ 命中走 `trading_calendar_changed`（仅全量） |
| 计算口径（公式改） | 全部 149 列 | `computation_version`（仅全量） |
| **国债曲线** `treasury_yield_curve`（被源端订正/外部脚本改写） | ttm_dividend_yield、div_yield_spread_0p25y…30y（全市场共用一条曲线） | `treasury_curve_fingerprint`（全表内容哈希）→ 命中走 `treasury_curve_changed`（仅全量） |

## 故意**不**哈希的列 / 只靠周期兜底的盲区（2026-11-21 复核，别再重复怀疑）

这一节是给"以后又有人怀疑指纹漏了某个格子"准备的清单：每条都写了
**为什么不算错**或**为什么只能兜底**，以及判据出处。新增指纹面前先读这里。

| 格子 | 会不会算错 | 处理 | 为什么 |
|---|---|---|---|
| `price_daily_raw.open/high/low` | **不会** | 不哈希（见 `_PRICE_FINGERPRINT_TABLES`） | `calculator.py` 通篇只读 raw 的 `trade_date/close/volume/turnover/turnover_rate` 与 qfq 的 `close`（`_calc_technical` / `_get_latest_price` / `compute_price_fields_for_stock` 三处 SQL 逐字核对）；开高低不参与任何快照列 |
| `price_daily_qfq.volume/turnover/turnover_rate/open/high/low` | **不会** | 不哈希 | 同上：qfq 只提供 `close`（区间收益/波动率/回撤的复权序列），其余列没有任何读取方 |
| `share_capital_history.change_reason/is_anchor/verified/source/raw_hash/batch_id` | **不会** | 不哈希（见 `_SHARE_CAPITAL_COLUMNS`） | 计算只读 `effective_date`/`total_shares`（`_get_cumulative_dividend_amount` 的窗口函数 + `market_cap.resolve_shares_asof`）；`verified` 属统计披露域（`extended.py` / capital 报表），`source/raw_hash/batch_id` 每次重抓都换（`capital.py` 全删全插）——哈希它们等于每轮全量 |
| `stock_meta.updated_at/name/pinyin/exchange` | **不会** | 不哈希（见 `_UNIVERSE_META_COLUMNS`） | 每轮 listing_info 刷新都会变；不进任何指标列 |
| `dividends/buyback/funding` 的 fetch_time/raw_hash/batch_id/source/confidence | **不会** | 不哈希（见 `_EVENT_EXCLUDED_COLUMNS`） | 抓取簿记列，不是值 |
| 三元摘要 `(count, Σhash, XORhash)` **同时**碰撞 | 会（理论上） | 只靠 `periodic_full` | 三个聚合同时撞才漏一只，概率可忽略；撞了也只影响那一只，15 天（半个月）兜底一轮 |
| plan 之后、commit 之前的并发改写 | 会（本轮） | 只靠下一轮 | 水位只在 step 成功后推进，且推进的是**plan 时刻**的指纹 → 并发改写留下的新值下一轮必然进差异集，不丢信号 |
| 公式/口径变更 | 会 | `INDICATOR_COMPUTATION_VERSION` bump | 指纹管"输入变没变"，不管"同样输入算出什么"；口径版本一改就全量 |
| `trading_dates` 增删"**只有很少股票**有行情"的日期 | **不会**（2026-11-21 修复 #13） | 覆盖度判据（见 `trading_calendar_gap_fingerprint`） | 缺口集合现在按"日历日 → 当天有 bar 的股票数"算，覆盖面低于 `update.indicator_calendar_coverage_ratio`（默认 0.5）× 上市只数就算缺口 → 增删这类日期必然被看见 |
| `trading_dates` 增删"**绝大多数股票**有行情、只差几只（停牌/新股）"的日期 | 会（那几只） | 只靠 `periodic_full` | 真实市场每天都有几十只停牌，"缺一只就算缺口"会让日历的每日例行刷新变成每天一轮 35 分钟全量；阈值 0.5 相对实测覆盖率（近 3 年 ≥99.8%）留了 500 倍余量，代价是"只差几只"这类日期不触发。15 天（半个月）兜底 |
| 两次日历内容变化**之间**某天的覆盖度掉了（行情行被删/回补），随后又增删该日期 | **不会**（2026-11-21 修复 #13-b） | 缺口判据的**第二个触发条件**：逐股行情指纹差异非空（行情行增删 ⟹ 覆盖度可能变）也重算缺口集合 | 修前缺口水位只是"上次日历内容变化时"算的快照：覆盖度在两次变化之间掉下去时那份快照还记着旧覆盖，下一次日历变化拿旧覆盖去比 → 判成"缺口没变" → 0 重算。现在缺口状态落盘的是**日期集合**（有序差值编码），判定做集合差；缺口张开/合上时若日历没动只刷新水位（受影响股票已在价格域），日历动了才考虑全量 |
| `price_daily_*` 之外的行情副本（ETF/港股/`index_valuation`） | 不会（对本表） | 另域另刷新 | 那些表喂的是别的派生域，不进 `indicator_snapshot` |
"""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

__all__ = [
    "DEFAULT_PERIODIC_FULL_DAYS",
    "INCREMENTAL_FULL_RATIO_THRESHOLD",
    "TRADING_CALENDAR_FP_KEY",
    "TRADING_CALENDAR_GAP_FP_KEY",
    "TREASURY_CURVE_FP_KEY",
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
    "settle_trading_calendar_fingerprint",
    "share_capital_fingerprints",
    "snapshot_fingerprints",
    "snapshot_row_count",
    "snapshot_version_mismatch_codes",
    "trading_calendar_fingerprint",
    "trading_calendar_gap_fingerprint",
    "treasury_curve_fingerprint",
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
# 交易日历（SQLite `trading_dates`）**内容**指纹（2026-11-21 盲区复核 #1）：
# `_calc_technical` 的 `_expected_trading_dates` 拿它判定"连续窗口"，日历一变
# MA/量能整列可能变 NULL，而**行情行一个都没变** → 行情指纹看不见。
# 两级：v1 = 全表内容（实测 ~7 ms/轮，天天算得起）；语义见下面 _GAP 那个 key。
TRADING_CALENDAR_FP_KEY = "indicator_trading_calendar_fingerprint:v1"
# 日历的**语义**状态：缺口日期集合（= "日历里有、当天有 bar 的股票数不够"的日期，
# 会让某个连续窗口断开的那些）。值不再是纯摘要，而是
# `{"fp": 摘要, "listed": 上市只数, "gaps": 有序差值编码的日期集合}`：
#   * 摘要给报告/日志用（同构三元摘要）；
#   * **日期集合本身**必须落盘 —— 判定"这个缺口是刚张开的吗"要做集合差，
#     两个摘要相减做不出来（2026-11-21 #13-b）；
#   * `listed` 是覆盖度判据的分母，分母变了缺口集合就会变，必须能看出来。
# 编码后只有"每多一个缺口日多 1–2 个字符"的量级（缺口集合通常个位数），
# 相比"逐日覆盖度（约 90 KB/轮）"省两个数量级。
# v3（2026-11-21，#13-b）：v2 只存一个不透明摘要、且只在日历内容变化时才重算，
# 覆盖度在两次日历变化之间掉了就失真；v3 换格式并让**行情变化**也能触发重算。
TRADING_CALENDAR_GAP_FP_KEY = "indicator_trading_calendar_gap_fingerprint:v3"
# 国债曲线（`treasury_yield_curve`）**全表内容**指纹（2026-11-21 独立审查 #14）：
# `div_yield_spread_*` 与 `ttm_dividend_yield` 是"全市场共用一条曲线"的派生列，
# 而修前曲线变化**只在** `_refresh_treasury_curve` 的 before/after 里被发现：
# 外部脚本直接 UPDATE 曲线（或那一步"写完后抛异常"）时本轮 0 重算，
# 利差永久停在旧曲线上。表很小（个位数十万行内），全表哈希 <5 ms，每轮都算得起。
TREASURY_CURVE_FP_KEY = "indicator_treasury_curve_fingerprint:v1"
# 日历缺口的**覆盖度阈值**：某日历日有 bar 的股票数 < 阈值 × 上市股票数 就算缺口。
# 为什么不是 1.0（"缺一只就算缺口"）：真实市场每天都有几十只停牌股票，
# 1.0 会把日历每天例行的 INSERT OR REPLACE 变成一轮 35 分钟全量。
# 为什么是 0.5 而不是更贴近实测的值（近 3 年每日覆盖率 ≥99.8%）：阈值越低越安全
# （越不会把"正常的停牌日"判成缺口），代价只是"只差几只股票"的日期不触发。
# 0.5 相对实测覆盖率留了约 500 倍余量，相对"只有 1/5 只股票有行情"这种真事故
# 留了 2.5 倍余量。
DEFAULT_CALENDAR_COVERAGE_RATIO = 0.5
# `indicator_snapshot` 逐股内容摘要：外部改坏/半写靠它当场发现（#3）。
SNAPSHOT_FP_KEY = "indicator_snapshot_fingerprint:v1"
COMPUTATION_VERSION_KEY = "indicator_computation_version"
LAST_SCOPE_KEY = "indicator_last_scope_decision"
# 周期全量兜底的"上次全量时间"。**只在真的全量成功后刷新**（增量轮不刷新，
# `bootstrap_indicator_scope` 也不刷新——它不重算任何股票），否则常跑的实例
# 永远不会到期。
PERIODIC_FULL_KEY = "indicator_periodic_full_last_at"

# 周期全量兜底：15 天（半个月，2026-11-21 用户裁决，7 → 15）。
# 它只兜"指纹看不见的外部改写"（绕过更新流程直接改库 / 半写 / 崩溃残留），
# 这类残留只可能来自人肉脚本；周期越短越安全，但**每次全量都很贵**
# （5,575 只 × 149 列 ≈ 35 分钟），7 天等于每周白烧一轮 → 半个月兜底。
DEFAULT_PERIODIC_FULL_DAYS = 15
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
    # 2026-11-21 盲区复核 #1：日历是全市场共用的输入，一个日期就能让**全市场**的
    # 连续窗口断开（MA/量能整列变 NULL），受影响集合说不出"只这几只" → 归 full 组。
    # 实际触发点不在 sources 里（见 `_plan_indicator_scope` 的日历判定），这里登记
    # 是为了"任何来源键都必须有明确归属"这条自证。
    "trading_calendar_changed": "full",
    # 2026-11-21 独立审查 #14：国债曲线是全市场共用的标量输入面，变了之后
    # `div_yield_spread_*` / `ttm_dividend_yield` 全市场都要重算（说不出"只这几只"）。
    # 触发点在 `_plan_indicator_scope` 的曲线判定里，这里登记同样是自证。
    "treasury_curve_changed": "full",
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
    # 交易日历（全市场共用的**标量**输入面，不是逐股映射）：
    #   * `trading_calendar_fingerprint`：本轮日历内容摘要（每轮都算，~5 ms）；
    #   * `trading_calendar_gap_fingerprint`：**日历内容变了或行情行变了**才去算的
    #     缺口摘要（日历里有、当天 bar 覆盖度不够的日期），没重算时为 None；
    #   * `trading_calendar_gap_days`：同一时刻的缺口日期集合（落盘用，#13-b）；
    #   * `trading_calendar_gap_refreshed`：只刷新了缺口水位（覆盖度/上市只数驱动），
    #     不需要重算 —— 走 `settle_trading_calendar_fingerprint` 提前推进；
    #   * `trading_calendar_fingerprint_changed`：内容与上轮水位不同；
    #   * `trading_calendar_semantics_changed`：语义不同 = **真的**会让技术列变。
    trading_calendar_fingerprint: str | None = None
    trading_calendar_gap_fingerprint: str | None = None
    trading_calendar_gap_days: list[str] = field(default_factory=list)
    trading_calendar_gap_refreshed: bool = False
    trading_calendar_fingerprint_changed: bool = False
    trading_calendar_semantics_changed: bool = False
    # 国债曲线同样是**标量**输入面（全市场共用一条曲线，不是逐股映射）：
    # 内容指纹每轮都算（<5 ms），变了 → `treasury_curve_changed` 全量。
    treasury_curve_fingerprint: str | None = None
    treasury_curve_changed: bool = False
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


def _read_scalar(owner: Any, key: str) -> str | None:
    """全市场共用的**标量**输入指纹水位（逐股映射走 `_read_json_map`）。"""
    raw = owner._read_refresh_state(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _write_scalar(owner: Any, key: str, value: str) -> None:
    owner._write_refresh_state(key, str(value))


# ─── 日历缺口状态（**日期集合**，不是摘要）─────────────────────────────────


def _day_ordinal(day: str) -> int:
    return date.fromisoformat(str(day)[:10]).toordinal()


def _day_from_ordinal(ordinal: int) -> str:
    return date.fromordinal(int(ordinal)).isoformat()


def _encode_days(days: Iterable[str]) -> str:
    """日期集合 → 紧凑字符串（有序：首日用序数，其后只存相邻差值，十六进制）。

    为什么要落盘**集合**而不只是摘要：`#13-b` 的判定要做集合差
    （"这个缺口是刚张开的吗 / 刚合上的吗"），两个摘要相减做不出来。
    为什么不存"逐日覆盖度"（上一版评估约 90 KB/轮）：缺口集合通常是个位数，
    差值编码后"每多一天只多 1–2 个字符"，两三个数量级的差距。
    实测 fixture 5 只 × 5 个日历日 → 空串或几十字节；真库最坏（8,797 个日历日
    全是缺口）也只有 ~18 KB，且只在重算缺口时才写一次。
    """
    ordered = sorted({_day_ordinal(day) for day in days})
    if not ordered:
        return ""
    parts = [format(ordered[0], "x")]
    parts.extend(format(b - a, "x") for a, b in zip(ordered, ordered[1:], strict=False))
    return ",".join(parts)


def _decode_days(encoded: str) -> set[str]:
    text = str(encoded or "").strip()
    if not text:
        return set()
    parts = text.split(",")
    ordinal = int(parts[0], 16)
    days = {_day_from_ordinal(ordinal)}
    for part in parts[1:]:
        ordinal += int(part, 16)
        days.add(_day_from_ordinal(ordinal))
    return days


@dataclass
class CalendarGapState:
    """上一轮落盘的缺口语义状态：摘要 + 覆盖度分母 + **缺口日期集合**。"""

    fingerprint: str
    listed_count: int
    days: set[str] = field(default_factory=set)


def _read_calendar_gap_state(owner: Any) -> CalendarGapState | None:
    raw = _read_scalar(owner, TRADING_CALENDAR_GAP_FP_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("不是 JSON 对象")
        return CalendarGapState(
            fingerprint=str(parsed.get("fp") or ""),
            listed_count=int(parsed.get("listed") or 0),
            days=_decode_days(str(parsed.get("gaps") or "")),
        )
    except (TypeError, ValueError) as error:
        # 旧格式（v2 的不透明摘要）/ 损坏 → 按"没有基线"处理：本轮重建一次
        # （`input_fingerprint_bootstrap` 全量），绝不把读不懂的状态当成"没变化"。
        logger.warning("日历缺口状态解析失败，按缺失处理（本轮重建基线）: %s", error)
        return None


def _write_calendar_gap_state(
    owner: Any, fingerprint: str, listed_count: int, days: Iterable[str],
) -> None:
    _write_scalar(
        owner,
        TRADING_CALENDAR_GAP_FP_KEY,
        json.dumps({
            "v": 3,
            "fp": str(fingerprint),
            "listed": int(listed_count),
            "gaps": _encode_days(days),
        }, ensure_ascii=False, sort_keys=True),
    )


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


def _digest_dates(dates: Iterable[str], *, salt: str = "") -> str:
    """日期集合 → `(count, max, Σcrc32, XOR crc32)` 摘要。

    与逐股指纹的三元聚合**同构**（行数 + 求和 + 异或）：同样的"三重同撞才漏"
    概率论，但日历是全局标量，撞了也只是少一次全量，另有 `periodic_full` 兜底。
    用 `zlib.crc32`（C 实现）而不是 `hash()`：SQLite 侧没有向量化 `hash()`。
    """
    ordered = sorted({str(value)[:10] for value in dates if str(value)[:10]})
    total = 0
    xor = 0
    for day in ordered:
        value = zlib.crc32(day.encode("utf-8"))
        total += value
        xor ^= value
    payload = f"{salt}|{len(ordered)}:{ordered[-1] if ordered else ''}:{total}:{xor}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def trading_calendar_dates(owner: Any) -> list[str]:
    """SQLite `trading_dates` 的全部日期（`YYYY-MM-DD`，已排序）。

    真库 8,797 行实测（只读连接、同进程连测 7 次）：读取 + 摘要
    **7.2–7.5 ms**（中位 7.29 ms；其中 SQLite 侧读取约 4.2 ms，其余是摘要素）。
    """
    rows = owner.sqlite.query("SELECT trade_date FROM trading_dates ORDER BY trade_date")
    return [str(row["trade_date"])[:10] for row in rows]


def trading_calendar_fingerprint(owner: Any) -> str:
    """交易日历的**内容**指纹（每轮都算，成本 ~5 ms）。

    只用来回答"这一轮日历被动过吗"。内容变了**不等于**要重算（见
    :func:`trading_calendar_gap_fingerprint`）：`_update_trading_dates` 每天
    `INSERT OR REPLACE` 整份列表，Sina 源给的是**整年**日期表 —— 把"日历长得
    和昨天不一样"直接判成全量，等于把每天例行的日历刷新变成一轮 35 分钟全扫。
    """
    return _digest_dates(trading_calendar_dates(owner))


def calendar_coverage_ratio() -> float:
    """日历缺口判据的覆盖度阈值（`update.indicator_calendar_coverage_ratio`）。

    配置项只允许在 `[0.05, 0.95]` 里取值：1.0 等于"缺一只股票就算缺口"，
    会把日历每天例行的整份重写变成每天一轮全量；0 等于"永远没有缺口"。
    """
    raw = _update_config().get(
        "indicator_calendar_coverage_ratio", DEFAULT_CALENDAR_COVERAGE_RATIO,
    )
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CALENDAR_COVERAGE_RATIO
    return min(0.95, max(0.05, ratio))


def trading_calendar_coverage(owner: Any) -> dict[str, int]:
    """每个交易日有多少只股票有 bar（覆盖度，2026-11-21 缺陷 #13 的判据来源）。

    修前用的是 `SELECT DISTINCT trade_date`（**全市场并集**）：任意一只股票那天
    有 bar，该日就被从缺口集合里剔掉 —— 于是"日历新增/删除一个只有 1/5 只股票
    有行情的日期"完全不触发重算，另外 4 只的 ma10…ma250 与波动率永久停在旧
    日历算出来的值（独立审查实测 24 格错 / 4 只）。

    修后按 `GROUP BY trade_date` 拿**每天的行数**（= 当天有 bar 的股票数；主键
    `(stock_code, trade_date)` 保证一行一股），再与上市只数比阈值。

    成本（真库 1,758.9 万行、threads=2、只读实测）：`GROUP BY trade_date` +
    `COUNT(*)` **63–67 ms**，比它替换掉的 `DISTINCT CAST(trade_date AS VARCHAR)`
    （**313 ms**，字符串转换是全表的开销）快 **4.7 倍**，而且信息严格更多
    （并集只是"行数 ≥ 1"的特例）。与原来一样**只在日历内容变了才调用**。
    """
    rows = owner.duck.read_query(
        """SELECT CAST(trade_date AS VARCHAR) AS trade_date, COUNT(*) AS n
           FROM price_daily_raw
           GROUP BY trade_date"""
    )
    return {str(row["trade_date"])[:10]: int(row["n"]) for row in rows}


def _calendar_gap_dates(
    calendar: Iterable[str],
    coverage: Mapping[str, int],
    listed_count: int,
) -> set[str]:
    """缺口日期集合 = 日历里**当天有 bar 的股票数不够**的那些日期。

    判据：`coverage(D) < max(1, ratio × 上市只数)`。

    为什么用"覆盖率"而不是"有没有"：`calculator._trailing_contiguous_closes` 是
    **逐股**判定的 —— 日期 D 要改变某只股票的结果，必须 D 是预期交易日、而**该股
    自己**没有 D 这一行。所以"全市场并集非空"完全不能证明"每只股票都有 D"。

    为什么不是"缺一只就算缺口"：真实市场每天都有几十只停牌股票，`ratio=1` 会把
    日历每天例行的整份 `INSERT OR REPLACE` 变成每天一轮 35 分钟全量。默认
    `ratio=0.5` 相对实测覆盖率（近 3 年每日 ≥99.8%，见
    `docs/runbooks/ops-knowledge-base.md`）留了极大余量，同时"只有 1/5 只股票
    有行情"这种真事故仍然 2.5 倍超阈值。
    """
    days = {str(day)[:10] for day in calendar}
    denominator = int(listed_count or 0)
    if denominator <= 0:
        # 上市名单读不出来（或全库空表）→ 用"历史上单日最多有多少只股票有 bar"
        # 当分母；两者都没有时 floor=1，任何"有日历没行情"的日期都算缺口。
        denominator = max(coverage.values(), default=0)
    floor = max(1, int(denominator * calendar_coverage_ratio()))
    return {day for day in days if int(coverage.get(day, 0)) < floor}


def trading_calendar_gap_days(
    owner: Any,
    calendar_dates: Iterable[str] | None = None,
    *,
    listed_count: int | None = None,
) -> set[str]:
    """当前的**缺口日期集合**（日历里有、当天 bar 覆盖度不够的日期）。

    与 :func:`trading_calendar_gap_fingerprint` 的关系：那个是同一份东西的摘要
    （给报告/日志用），这个返回**集合本身** —— `#13-b` 的判定做集合差，
    摘要相减做不出来（见 :func:`_encode_days`）。

    成本：`GROUP BY trade_date, COUNT(*)` 走一遍 `price_daily_raw`
    （1,758.9 万行实测 **63–67 ms**）。调用点只有两个：
      * 日历内容变了（内容指纹每轮 ~7 ms，正常轮根本走不到这里）；
      * **行情行变了**（逐股行情指纹差异非空，见 `_plan_trading_calendar`）
        —— 这是 `#13-b` 的修复点：覆盖度只能在行情行变化时变，**不能**指望
        "下次日历变化时再算"，否则两次日历变化之间的覆盖度下滑会被吞掉。
    """
    calendar = (
        {str(day)[:10] for day in calendar_dates}
        if calendar_dates is not None
        else set(trading_calendar_dates(owner))
    )
    denominator = listed_stock_count(owner) if listed_count is None else int(listed_count)
    return _calendar_gap_dates(
        calendar, trading_calendar_coverage(owner), denominator,
    )


def calendar_gap_digest(gaps: Iterable[str], calendar: Iterable[str]) -> str:
    """缺口集合 + "日历是否为空" → 摘要（:func:`trading_calendar_gap_fingerprint` 的内核）。

    `cal` 位：日历**整表为空**时 `_expected_trading_dates` 返回 None，
    `_calc_technical` 会 fail-closed 把 MA/量能**整列写 NULL**。所以
    "空日历 → 有日历"（哪怕新增的日期覆盖度都够）也是真的语义变化，
    必须能和"日期集合没动"区分开 —— 一个 bit 就够，成本 0。
    """
    calendar_days = {str(day)[:10] for day in calendar}
    return _digest_dates(gaps, salt=f"cal={int(bool(calendar_days))}")


def trading_calendar_gap_fingerprint(
    owner: Any, calendar_dates: Iterable[str] | None = None,
) -> str:
    """日历的**语义**指纹：哪些日历日"当天的覆盖度不够"，会让技术列改变。

    为什么是这个集合（2026-11-21 实测，见 `reports/`「盲区复核」与
    `tests/regression/test_indicator_incremental_scope.py` 的 ⑨⑩ 组）：

    * `calculator._trailing_contiguous_closes` 只在"**两个相邻行情行之间**夹着
      一个预期交易日"时截断序列。一个日历日期 D 要改变结果，必须 D 是预期交易日
      而**该股没有 D 这一行**；
    * 行情行本身变了 → `price_fingerprints` 逐股摘要必然变 → 价格域重算，
      与本指纹无关；
    * 于是"日历"这一侧唯一需要盯的是**缺口日期集合**：新增这样的 D 会让缺口张开
      （MA/量能整列变 NULL），删除这样的 D 会让缺口合上（序列变长）——两个方向
      都必须重算；
    * **"该股没有 D 这一行"是逐股的事实，不能用"全市场有没有 D 这一行"近似**
      （2026-11-21 独立审查缺陷 #13 的原话：任意一只股票那天有 bar，该日就被从
      缺口集合里剔掉）。所以判据是**覆盖度**：D 当天有 bar 的股票数少于
      `ratio × 上市只数` 就把 D 算作缺口 → 见 :func:`_calendar_gap_dates`。
    * 反过来，D 的覆盖度足够（近满员）时，增删 D 不会改变任何"两行之间有预期日
      缺失"的判定 → 不重算（这条是性能关键：正常日子里日历每天都会新增当天日期，
      而当天行情行基本都在）。

    残留盲区（**故意**，由 `periodic_full` 兜底）：覆盖度在阈值**之上**、只差几只
    股票（当天停牌/新股）的日期增删时，那几只不触发重算。要精确到"逐股 × 8,797
    天"就得按股 join 窗口（8,797 × N 行），成本远超收益；阈值 0.5 相对实测每日
    覆盖率（≥99.8%）留了 500 倍余量，15 天（半个月）兜底一轮。

    成本：见 :func:`trading_calendar_gap_days`（走一遍 `price_daily_raw`
    的 `GROUP BY trade_date`，1,758.9 万行实测 **63–67 ms**，只在日历内容变了
    或行情行变了才调用；修前那句 `SELECT DISTINCT CAST(trade_date AS VARCHAR)`
    实测 313 ms）。
    """
    calendar = (
        {str(day)[:10] for day in calendar_dates}
        if calendar_dates is not None
        else set(trading_calendar_dates(owner))
    )
    gaps = trading_calendar_gap_days(owner, calendar, listed_count=listed_stock_count(owner))
    return calendar_gap_digest(gaps, calendar)


def treasury_curve_fingerprint(owner: Any) -> str:
    """国债曲线的**全表内容**指纹（2026-11-21 独立审查缺陷 #14）。

    修前曲线变化**只在** `update.py::_refresh_treasury_curve` 的
    before/after 里被发现，而且那一步"写完之后抛异常"的分支连 `curve_changed`
    都不返回 —— 外部脚本直接 `UPDATE treasury_yield_curve` 时本轮
    `incremental / no_changed_stocks / 0 重算`，全市场的 `div_yield_spread_*`
    永久停在旧曲线（实测停在 3.5/2.0，全量应为 1.5/0.0）。

    为什么是**全表**而不是"最新曲线日 + 行数"：利差对齐的是"不晚于该股价格日的
    最近曲线点"，停牌/价格滞后股票可以引用任意历史日期；订正某个历史日期的收益率
    同样必须被看见。表很小（真库 51,849 行 = 曲线日 × 9 个关键期限），每轮都算得起。

    摘要形状与逐股指纹**同构**：`(行数, Σhash, XOR hash)` 三元聚合（不是
    `string_agg + md5`）—— 后者要把 5 万行拼成一个大字符串，真库只读实测
    **24.6 ms**；`hash()` 是向量化整数哈希，同一张表 **1.79 ms**（14 倍）。
    碰撞概率同本模块其它输入面（三元同时撞才漏），另有 `periodic_full` 兜底。

    与 `update.py::_treasury_curve_fingerprint` 的关系：那一个是**单个步骤内**的
    before/after 对比（回答"这一轮这一步改了没有"），本函数是**跨轮水位**
    （回答"自上轮指标计算以来曲线变了没有"）。两者的判定入口不同、不能互相替代
    （缺陷 #14 正是"只有前者"造成的），但语义相同：都只认
    `curve_date/tenor_years/yield_pct` 三列，来源/批次等簿记列不进指纹。
    """
    rows = owner.duck.read_query(
        """SELECT count(*) AS n,
                  sum(hash(curve_date, tenor_years, yield_pct)) AS s,
                  bit_xor(hash(curve_date, tenor_years, yield_pct)) AS x
           FROM treasury_yield_curve"""
    )
    row = rows[0] if rows else {}
    payload = "{}:{}:{}".format(
        int(row.get("n") or 0), int(row.get("s") or 0), int(row.get("x") or 0),
    )
    # 空表也是稳定值（"空 → 有 / 有 → 空"都必须算变化）。
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


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


#: bootstrap **不许**吸收成基线的百分比列（2026-11-21 独立审查 N5）。
#: 只登记"单位是百分数、且真实值不可能到三位数"的格子：`roe=-999` 这类哨兵值
#: 一把就能抓到，而 48 只真实 ROE > 100% 的老股东权益公司不会被误伤（阈值 900）。
#: 这里的判据只用来**决定要不要重算这一只**，绝不改任何值（R4：不许用护栏
#: 掩盖看不懂的数字）。
_BOOTSTRAP_SENTINEL_ABS = 900.0
_BOOTSTRAP_SENTINEL_COLUMNS = (
    "roe", "roa", "gross_margin", "net_margin", "roic",
    "debt_ratio", "goodwill_ratio", "payout_ratio", "ttm_dividend_yield",
)


def bootstrap_rejected_snapshot_codes(owner: Any) -> list[str]:
    """bootstrap 时**不许当基线吸收**的快照行（缺陷 N5）。

    两类：
    1. `data_version` 与当前口径不符（NULL / 旧 `calc=...`）—— 该行不是这版公式
       算的，把它当基线等于"承认它是当前的"；
    2. 明显异常值（`abs(roe) >= 900`、`latest_close <= 0`）—— 独立审查实测的
       `roe=-999` 就是这样被吸收成基线、然后**永久错**的。

    修前 `bootstrap_indicator_scope` 把"当前库里的内容"无条件写成基线：它既不
    重算也不核对，于是损坏行从此再也不会被任何指纹差异带进重算集合。
    """
    from app.core.indicators.calculator import indicator_data_version

    columns = set(_table_columns(owner, "indicator_snapshot", _SNAPSHOT_EXCLUDED_COLUMNS))
    guards = [
        f'abs("{name}") >= {_BOOTSTRAP_SENTINEL_ABS}'
        for name in _BOOTSTRAP_SENTINEL_COLUMNS
        if name in columns
    ]
    if "latest_close" in columns:
        guards.append('"latest_close" <= 0')
    where = "data_version IS DISTINCT FROM ?"
    if guards:
        where = f"{where} OR " + " OR ".join(guards)
    rows = owner.duck.read_query(
        f"SELECT DISTINCT stock_code FROM indicator_snapshot WHERE {where}",
        [indicator_data_version()],
    )
    return sorted(str(row["stock_code"]) for row in rows)


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
    """周期全量兜底的天数（默认 15 天 = 半个月，`update.indicator_periodic_full_days` 可配）。"""
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
    owner: Any, *, force_full: bool, trading_calendar_changed: bool = False,
    treasury_curve_changed: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    """可解释的全量原因（按"最需要解释"的优先级）。

    `forced_full` → `first_run` → `computation_version` → `treasury_curve_changed`
    → `trading_calendar_changed` → `periodic_full_refresh`。周期兜底排最后：
    它是**兜底**，有更具体的理由时优先报更具体的那个。

    `treasury_curve_changed`（2026-11-21 独立审查 #14）：国债曲线的**全表内容**
    变了（外部脚本改写 / 源端订正历史日期 / 那一步写完抛异常）。曲线是
    **全市场共用**的输入，一条收益率就能挪动 5,575 只股票的
    `div_yield_spread_*` 与 `ttm_dividend_yield` → 走全量。

    `trading_calendar_changed`（2026-11-21）：交易日历的语义指纹变了（多出/少了
    "覆盖度不够"的日期）。日历是**全市场共用**的输入，一个日期就能让每只股票的
    连续窗口断开（MA/量能整列变 NULL），受影响集合说不出"只这几只"
    → 走全量（与 `incremental_over_threshold` 同一条经济账：与其逐股维护，
    不如一次批量 staging 发布），但 reason 必须是这个具体原因，不许糊成阈值。
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
    if treasury_curve_changed:
        return "treasury_curve_changed", {"computation_version": version}
    if trading_calendar_changed:
        return "trading_calendar_changed", {"computation_version": version}
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


@dataclass
class CalendarPlan:
    """交易日历这一轮的判定结果（`_plan_trading_calendar` 的返回，替掉 5 元组）。"""

    content: str
    gap_fingerprint: str | None = None
    gap_days: set[str] = field(default_factory=set)
    content_changed: bool = False
    semantics_changed: bool = False
    baseline_missing: bool = False
    #: 只刷新了缺口水位（覆盖度/上市只数驱动，日历没动）→ 不需要重算，
    #: 但水位必须推进，否则下一轮还要再判一次同样的覆盖度变化。
    gap_refreshed: bool = False


def _plan_trading_calendar(
    owner: Any,
    *,
    price_rows_changed: bool,
    listed_count: int,
) -> CalendarPlan:
    """交易日历的两级判定：内容指纹 →（内容变了**或行情行变了**才）缺口集合。

    两级的理由（性能硬约束：稳态 ≤2 s，当前 1.05–1.4 s）：
    * 一级 `trading_calendar_fingerprint` **每轮都算**，只读 SQLite 8,797 行
      实测 **7.2–7.5 ms**（真库只读连接，同进程 7 连测中位 7.29 ms）；它回答
      "日历被碰过吗"；
    * 二级 `trading_calendar_gap_days` 要 `GROUP BY trade_date` 扫一遍
      `price_daily_raw`（1,759 万行实测 **63–67 ms**），**只在下面两条任一成立时跑**：
      1. 日历内容变了（原来的唯一触发条件）；
      2. **行情行变了**（`price_fingerprints` 逐股差异非空）。
         这是 `#13-b` 的修复点：缺口集合 = f(日历, 逐日覆盖度, 上市只数)，
         后两者只能由行情行/上市名单变化引起；如果只在日历变化时算，
         "两次日历变化之间覆盖度掉了"就会被漏掉 —— 到下一次日历变化时那张
         旧快照还记着旧覆盖，判成"语义没变"→ 该日永远不重算。
         门控用**已有的**逐股行情指纹差异，不额外加一次全表扫描：
         覆盖度变化 ⟺ 行情行增删 ⟹ 该股行情指纹必变（三元聚合含 `count(*)`）。
         稳态轮（一行都没变）仍然一分钱不花。

    三种"变了"必须分开：
    * 日历内容变了 + 缺口集合也变了 → `semantics_changed`（真的会让技术列变）；
    * 日历内容变了 + 缺口集合没变 → 只推进水位（新增/删除的日期覆盖度够）；
    * 日历没动 + 缺口集合变了（`#13-b`）→ **不重算**：缺口张开/合上的那些股票
      正是行情行被删/加的股票，逐股行情指纹已经把它们带进价格域（价格域重算
      本来就重算 MA/量能/波动率）。这里只把缺口水位刷新成当前覆盖度。
      残留（**故意**、极罕见）：日历内容变化与"覆盖度跨阈值"发生在**同一轮**时，
      "缺口张开"分不清是"日历新增了该日期"还是"覆盖度掉了" → 按前者（全量）
      保守处理，代价是那一轮多一次全量，方向安全。
    """
    content = trading_calendar_fingerprint(owner)
    stored_content = _read_scalar(owner, TRADING_CALENDAR_FP_KEY)
    stored_gap = _read_calendar_gap_state(owner)
    if stored_content is None or stored_gap is None:
        # 首次部署本输入面：交给 `input_fingerprint_bootstrap` 建基线，
        # 这里不额外判定（否则会和"没有任何上一轮水位"叠成两个 full 理由）。
        # 基线必须**把缺口集合也建出来**交给 commit 落盘，否则 `_GAP` 这个 key
        # 永远缺失 → 下一次日历内容变化时无谓地判成"语义变了"→ 全量。
        calendar = trading_calendar_dates(owner)
        gaps = trading_calendar_gap_days(
            owner, calendar, listed_count=listed_count,
        )
        return CalendarPlan(
            content=content,
            gap_fingerprint=calendar_gap_digest(gaps, calendar),
            gap_days=gaps,
            baseline_missing=True,
        )
    content_changed = stored_content != content
    if not content_changed and not price_rows_changed and stored_gap.listed_count == listed_count:
        return CalendarPlan(content=content)  # 稳态：一分钱不花
    calendar = trading_calendar_dates(owner)
    gaps = trading_calendar_gap_days(owner, calendar, listed_count=listed_count)
    digest = calendar_gap_digest(gaps, calendar)
    if gaps == stored_gap.days:
        # 缺口集合一模一样 → 语义没变（日历增删的都是"覆盖度够"的日期，
        # 或行情变化没跨过覆盖度阈值）。水位推进即可（`listed` 变了要重写状态，
        # 其余情况连写都不用写）。
        return CalendarPlan(
            content=content,
            gap_fingerprint=digest,
            gap_days=gaps,
            content_changed=content_changed,
            gap_refreshed=stored_gap.listed_count != listed_count,
        )
    if not content_changed:
        # `#13-b`：日历没动，缺口集合跟着覆盖度/上市只数移动。
        # 受影响股票全在**价格域**（行情行变化 ⟹ 逐股行情指纹变化），不重算全集。
        logger.info(
            "日历缺口集合随行情覆盖度变化（不触发全量）：%d → %d 个缺口日",
            len(stored_gap.days), len(gaps),
        )
        return CalendarPlan(
            content=content,
            gap_fingerprint=digest,
            gap_days=gaps,
            gap_refreshed=True,
        )
    # 日历变了 + 缺口集合也变了 → 分清"谁让它变的"：
    # * 缺口**合上**且该日期已不在日历里 = 日历删了这一天 → 那几只的连续序列
    #   变长（行情行没变，价格域看不见）→ 必须全量；
    # * 缺口**合上**但该日期还在日历里 = 覆盖度涨上去了（行情行增加）→ 价格域
    #   已经覆盖 → 不必全量；
    # * 缺口**张开** → 分不清"日历新增该日期"（必须全量）与"覆盖度掉了"
    #   （价格域够）→ 保守按全量（见上面 docstring 的残留说明）。
    opened = gaps - stored_gap.days
    closed = stored_gap.days - gaps
    calendar_set = {str(day)[:10] for day in calendar}
    closed_by_calendar = {day for day in closed if day not in calendar_set}
    changed = bool(opened) or bool(closed_by_calendar)
    logger.info(
        "交易日历内容变化（语义%s）：content=%s gap=%s 张开=%d 日历删掉的缺口日=%d",
        "也变了 → 需要重算技术指标" if changed else "没变 → 只推进水位",
        content, digest, len(opened), len(closed_by_calendar),
    )
    return CalendarPlan(
        content=content,
        gap_fingerprint=digest,
        gap_days=gaps,
        content_changed=True,
        semantics_changed=changed,
        gap_refreshed=not changed,
    )


def _plan_treasury_curve(owner: Any) -> tuple[str, bool, bool]:
    """国债曲线的**一级**判定：全表内容指纹 vs 上轮水位（缺陷 #14）。

    返回 `(当前指纹, 是否变化, 基线是否缺失)`。

    只有一级（不像日历有"内容/语义"两级）：曲线表很小，全表哈希 1–3 ms，
    而且"内容变了"在这里**就等于**"利差列会变"——没有"改了但不影响任何列"的
    情形（任何一天的任何一个期限都会改变某只股票的对齐结果）。
    """
    current = treasury_curve_fingerprint(owner)
    stored = _read_scalar(owner, TREASURY_CURVE_FP_KEY)
    if stored is None:
        # 首次部署本输入面 → 交给 `input_fingerprint_bootstrap` 建基线。
        return current, False, True
    if stored == current:
        return current, False, False
    logger.info(
        "国债曲线内容变化 → 全市场 div_yield_spread_* 必须重算（%s → %s）",
        stored, current,
    )
    return current, True, False


def _plan_indicator_scope(
    owner: Any,
    *,
    sources: Mapping[str, Iterable[str]],
    force_full: bool,
) -> IndicatorScopePlan:
    listed_count = listed_stock_count(owner)
    # 七个逐股输入面在任何判定之前先算出来：**行情面差异**是"逐日覆盖度可能变了"
    # 的唯一免费门控（覆盖度变化 ⟺ 行情行增删 ⟹ 该股行情指纹必变），
    # 见 `_plan_trading_calendar` 的 `#13-b` 说明。
    current = _collect_fingerprints(owner)
    previous = {
        surface: _read_json_map(owner, key) for surface, key, _ in _FINGERPRINT_SURFACES
    }
    price_rows_changed = bool(_diff_codes(previous["price"], current["price"]))
    calendar = _plan_trading_calendar(
        owner,
        price_rows_changed=price_rows_changed,
        listed_count=listed_count,
    )
    treasury_fp, treasury_changed, treasury_baseline_missing = _plan_treasury_curve(owner)
    reason, meta = _full_reason(
        owner, force_full=force_full,
        trading_calendar_changed=calendar.semantics_changed,
        treasury_curve_changed=treasury_changed,
    )
    version = str(meta["computation_version"])

    unknown = sorted(name for name in sources if name not in SOURCE_DOMAIN)
    if unknown:
        # 新增/未知信号来源**默认进 full 组**（`SOURCE_DOMAIN.get` 已如此），
        # 这里再显式记一笔，避免"以为它被忽略"或"以为它被当价格域"。
        logger.info("指标范围裁决收到未知来源，按 full 处理: %s", unknown)

    merged_sources: dict[str, Iterable[str]] = dict(sources)
    if reason is None:
        # 任何一个输入面没有上轮水位 → 无法证明"谁没变" → 一次全量建基线。
        missing = [
            surface for surface, _key, _src in _FINGERPRINT_SURFACES
            if previous[surface] is None
        ]
        if calendar.baseline_missing:
            # 交易日历是**标量**面，不在 `_FINGERPRINT_SURFACES` 里；没有上轮
            # 水位同样"无法证明它没变"→ 一次全量建基线（与 ⑦⑧ 两次补面同一条规矩）。
            missing.append("trading_calendar")
        if treasury_baseline_missing:
            # 国债曲线同理（缺陷 #14 新增的标量面）：没有上轮水位就证明不了
            # "曲线没变"，而利差是全市场共用输出 → 一次全量建基线。
            missing.append("treasury_curve")
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
        trading_calendar_fingerprint=calendar.content,
        # 缺口状态只有"真的重算过缺口集合"那一轮才有：稳态轮不写它，避免把
        # 作废的语义水位当成"我核对过"。
        trading_calendar_gap_fingerprint=calendar.gap_fingerprint,
        trading_calendar_gap_days=sorted(calendar.gap_days),
        trading_calendar_gap_refreshed=calendar.gap_refreshed,
        trading_calendar_fingerprint_changed=calendar.content_changed,
        trading_calendar_semantics_changed=calendar.semantics_changed,
        treasury_curve_fingerprint=treasury_fp,
        treasury_curve_changed=treasury_changed,
    )
    if calendar.semantics_changed:
        # 日历变了必须留痕：`changed_sources` 是运维唯一能读到的"谁把谁带进集合"。
        # 受影响面是全市场（每只股票的连续窗口都可能断在同一个日期），
        # 所以计数用上市只数，而不是编一个假的代码列表。
        plan.changed_sources["trading_calendar_changed"] = listed_count
    if treasury_changed:
        # 同上：曲线是全市场共用的标量输入，计数用上市只数。
        plan.changed_sources["treasury_curve_changed"] = listed_count

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

    它到底做了什么、**没做什么**（2026-11-21 独立审查 N5 修正）：

    * ✅ 建的是**输入基线**：七个逐股输入面 + 交易日历 + 国债曲线两个标量面 +
      口径版本号。下一轮按这些水位做集合差异，只算变化的部分。
    * ❌ **不代表做过全量**：`indicator_periodic_full_last_at`（周期兜底水位）
      **不写**。修前它写 `now()`，于是一次 bootstrap 就把**已经到期**的周期全量
      吞掉了 —— "periodic_full 水位只在真全量成功后刷新"这条保证当场不成立。
      现在 bootstrap 之后该到期还是到期（下一轮 `reason=periodic_full_refresh`）。
    * ❌ **不吸收损坏快照当基线**：`data_version` 与当前口径不符、或明显异常值
      （`roe=-999` 这类）的行会被**排除在快照基线之外**并 log 警告 —— 它们下一轮
      自动进 `snapshot_state_mismatch`（full 组）重算。修前这类行一进基线就再也
      没人看，那一格**永久错**。

    正当用途：
    1. 运维：明确知道现网快照就是当前口径、只想立刻切到增量模式时，用它建立
       基线，避免升级后的第一轮全量；
    2. 回归测试：钉住"上一轮水位"，让用例只验证本轮的变化来源。

    常规路径**不需要**调用它：没有基线时 `plan_indicator_scope` 会判
    `input_fingerprint_bootstrap` 并跑一次全量建立基线。
    """
    current = _collect_fingerprints(owner)
    rejected = bootstrap_rejected_snapshot_codes(owner)
    if rejected:
        dropped = set(rejected)
        current["snapshot"] = {
            code: fingerprint
            for code, fingerprint in current["snapshot"].items()
            if code not in dropped
        }
        logger.warning(
            "指标指纹 bootstrap 拒绝吸收 %d 只股票的损坏/旧口径快照"
            "（口径版本不符或明显异常值）：%s —— 它们不写进快照基线，"
            "下一轮自动进 full 组重算",
            len(rejected), rejected[:20],
        )
    for surface, key, _source in _FINGERPRINT_SURFACES:
        _write_json_map(owner, key, current[surface])
    # 交易历是**标量**输入面（不是逐股映射），水位单独写：
    #   * 内容指纹：每轮都算的那个；
    #   * 缺口状态（摘要 + 覆盖度分母 + **缺口日期集合**）：日历里"覆盖度不够"
    #     的日期。基线必须两个都建，否则下一轮会把"日历语义变了"误判成真的变化
    #     （或反过来漏掉）。集合本身也要建 —— `#13-b` 的判定靠它做集合差。
    _write_scalar(owner, TRADING_CALENDAR_FP_KEY, trading_calendar_fingerprint(owner))
    calendar_days = trading_calendar_dates(owner)
    gap_days = trading_calendar_gap_days(owner, calendar_days)
    _write_calendar_gap_state(
        owner,
        calendar_gap_digest(gap_days, calendar_days),
        listed_stock_count(owner),
        gap_days,
    )
    # 国债曲线：同样是标量面，同样必须建（否则下一轮判 input_fingerprint_bootstrap）。
    _write_scalar(owner, TREASURY_CURVE_FP_KEY, treasury_curve_fingerprint(owner))
    owner._write_refresh_state(COMPUTATION_VERSION_KEY, current_computation_version())
    return {
        "universe_codes": len(current["universe"]),
        "financial_codes": len(current["financial"]),
        "event_codes": len(current["event"]),
        "share_capital_codes": len(current["share_capital"]),
        "price_codes": len(current["price"]),
        "manual_override_codes": len(current["manual_override"]),
        "snapshot_codes": len(current["snapshot"]),
        # 被拒绝吸收成基线的股票：下一轮进 full 组（可解释、可断言）。
        "rejected_snapshot_codes": rejected,
        # 明示"bootstrap 不代表全量"：周期兜底水位没有被刷新。
        "periodic_full_watermark_written": False,
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
        # 交易日历的**标量**水位：算过（step 成功）就推进。
        # 语义变了但没有推进代价——这一轮本来就是全量，技术列已经按新日历重算完；
        # 语义没变（新增/删除的日期在行情表里有行）时由
        # `settle_trading_calendar_fingerprint` 提前推进，走不到这里有代价。
        if plan.trading_calendar_fingerprint:
            _write_scalar(
                owner, TRADING_CALENDAR_FP_KEY, plan.trading_calendar_fingerprint,
            )
        if plan.trading_calendar_gap_fingerprint:
            _write_calendar_gap_state(
                owner,
                plan.trading_calendar_gap_fingerprint,
                plan.listed_count,
                plan.trading_calendar_gap_days,
            )
        # 国债曲线（缺陷 #14）：曲线变了必然走全量，本轮的利差列已经按新曲线重算
        # → step 成功就推进水位。失败轮不写，下一轮重新判 `treasury_curve_changed`。
        if plan.treasury_curve_fingerprint:
            _write_scalar(
                owner, TREASURY_CURVE_FP_KEY, plan.treasury_curve_fingerprint,
            )
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


def settle_trading_calendar_fingerprint(
    owner: Any, plan: IndicatorScopePlan,
) -> bool:
    """把"日历变了/覆盖度变了、但语义没变"的那一次变更水位推进掉（返回是否推进）。

    为什么需要一条**绕过 step 成功门禁**的推进路径：逐股水位必须"算成功才推进"
    （否则下次会把自己没算过的东西当已消费）；但日历的语义结论是**纯判定结论**——
    "新增/删除的日期在行情表里本来就有行，任何股票的时间序列都不会改变"。
    这种情况下本轮 0 重算，`flow` 根本不会调 `commit_indicator_scope`（没算东西
    就没有 step），水位就会永远停在旧值 → 每轮重新探测同一处变更（常态轮多付
    一段扫描，还会让报告里常年挂着一次"日历变了"）。

    两条独立的推进理由（`#13-b` 的第二条）：
    * `trading_calendar_fingerprint_changed`：日历内容变了，缺口集合没变；
    * `trading_calendar_gap_refreshed`：日历没动，但**逐日覆盖度**变了（行情行
      增删）导致缺口集合移动 —— 受影响的股票已在价格域，本轮 0 重算，但这张
      缺口快照必须刷新成当前覆盖度，否则下一次日历变化时会拿旧覆盖去比
      （那正是 `#13-b` 的漏算来源）。

    反过来，**语义真的变了**时这里拒绝推进：那一轮必须走全量，推进留给
    `commit_indicator_scope` 的"step 成功"分支 —— 全量失败就下一轮再来。
    """
    if plan.trading_calendar_semantics_changed:
        return False
    advance_content = bool(
        plan.trading_calendar_fingerprint_changed and plan.trading_calendar_fingerprint
    )
    advance_gap = bool(
        plan.trading_calendar_gap_refreshed and plan.trading_calendar_gap_fingerprint
    )
    if not advance_content and not advance_gap:
        return False
    if advance_content:
        _write_scalar(owner, TRADING_CALENDAR_FP_KEY, plan.trading_calendar_fingerprint)
    if plan.trading_calendar_gap_fingerprint:
        _write_calendar_gap_state(
            owner,
            plan.trading_calendar_gap_fingerprint,
            plan.listed_count,
            plan.trading_calendar_gap_days,
        )
    return True
