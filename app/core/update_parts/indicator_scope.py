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
| 上市状态/ST/停牌/行业 csrc_l1 | 名单成员 + 排名/筛选口径（市值排名分位） | `universe_fingerprint` |
| 行情 price_daily_raw/qfq | latest_close/date、估值价格域、MA/波动率/回撤/区间收益/成交量/换手 | `price` / `technical_pending` |
| 分红 dividends / 除权 xdxr | dividend_yield、连续分红年数、DPS、分红融资比 | `dividend_event` / `corporate_event` |
| 回购/融资事件 | cumulative_*_amount、dividend_financing_ratio_pct | `corporate_event` |
| 计算口径（公式改） | 全部 149 列 | `computation_version`（仅全量） |
| 国债曲线 | ttm_dividend_yield、div_yield_spread_* | 走 `refresh_treasury_spreads` 专用批量，不进本集合 |
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "INCREMENTAL_FULL_RATIO_THRESHOLD",
    "IndicatorScopePlan",
    "bootstrap_indicator_scope",
    "commit_indicator_scope",
    "current_computation_version",
    "financial_fingerprints",
    "listed_stock_count",
    "plan_indicator_scope",
    "snapshot_row_count",
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
FINANCIAL_FP_KEY = "indicator_financial_fingerprint:v1"
COMPUTATION_VERSION_KEY = "indicator_computation_version"
LAST_SCOPE_KEY = "indicator_last_scope_decision"

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
    "price": "price",
    "technical_pending": "price",
    "corporate_event": "event",
}

# 最新完整报告期三表核心列（与 calculator._get_latest_financials 读取的列同源）。
_BS_COLUMNS = (
    "total_assets", "total_liabilities", "total_equity", "total_equity_parent",
    "monetary_funds", "accounts_receivable", "inventory",
    "total_current_assets", "total_current_liabilities", "paid_in_capital",
    "goodwill", "short_term_loans", "long_term_loans", "bonds_payable",
    "notes_receivable", "prepayments", "other_receivables",
    "fixed_assets", "intangible_assets",
)
_IC_COLUMNS = (
    "revenue", "cost_of_revenue", "operating_profit", "net_profit",
    "parent_net_profit", "deducted_net_profit", "total_operating_revenue",
    "total_operating_cost", "selling_expenses", "administrative_expenses",
    "financial_expenses", "rd_expenses", "interest_expense",
    "investment_income", "total_profit", "income_tax", "basic_eps",
)
_CF_COLUMNS = ("cf_from_operating", "cf_from_investing", "cf_from_financing", "cf_net")

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
    prev_universe_fingerprint: dict[str, str] | None = None
    prev_financial_fingerprint: dict[str, str] | None = None
    fingerprint_error: str | None = None

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


def financial_fingerprints(owner: Any) -> dict[str, str]:
    """逐股"最新完整报告期"三表核心列指纹（新报告期/修正值/覆盖写都会变）。"""
    bs_cols = ", ".join(f"bs.{name}" for name in _BS_COLUMNS)
    ic_cols = ", ".join(f"ic.{name}" for name in _IC_COLUMNS)
    cf_cols = ", ".join(f"cf.{name}" for name in _CF_COLUMNS)
    rows = owner.duck.read_query(
        f"""WITH complete AS (
                SELECT bs.stock_code, bs.report_date
                FROM balance_sheet bs
                JOIN income_statement ic
                  ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
                JOIN cash_flow cf
                  ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
                WHERE bs.total_assets IS NOT NULL
                  AND bs.total_liabilities IS NOT NULL
                  AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
                  AND ic.revenue IS NOT NULL
                  AND ic.parent_net_profit IS NOT NULL
                  AND cf.cf_from_operating IS NOT NULL
            ), latest AS (
                SELECT stock_code, MAX(report_date) AS report_date
                FROM complete GROUP BY stock_code
            )
            SELECT latest.stock_code AS stock_code,
                   substr(md5(CAST((latest.report_date, {bs_cols}, {ic_cols}, {cf_cols})
                                  AS VARCHAR)), 1, 16) AS fp
            FROM latest
            JOIN balance_sheet bs
              ON bs.stock_code = latest.stock_code AND bs.report_date = latest.report_date
            JOIN income_statement ic
              ON ic.stock_code = latest.stock_code AND ic.report_date = latest.report_date
            JOIN cash_flow cf
              ON cf.stock_code = latest.stock_code AND cf.report_date = latest.report_date"""
    )
    return {str(row["stock_code"]): str(row["fp"]) for row in rows}


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


def _full_reason(owner: Any, *, force_full: bool) -> tuple[str | None, dict[str, Any]]:
    """四个可解释的全量原因（按"最需要解释"的优先级）。"""
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
    return None, {"computation_version": version}


def plan_indicator_scope(
    owner: Any,
    *,
    sources: Mapping[str, Iterable[str]],
    force_full: bool = False,
) -> IndicatorScopePlan:
    """决定这一轮"算谁"：增量集合（默认）或全量（仅四个可解释原因）。

    Args:
        owner: `IncrementalUpdater`（鸭子类型：duck/sqlite/_read_refresh_state/
            _write_refresh_state）。
        sources: 变化来源 → 代码集合。键必须是 :data:`SOURCE_DOMAIN` 里的名字。
        force_full: 显式强制全量（运维开关/CLI flag）。
    """
    reason, meta = _full_reason(owner, force_full=force_full)
    version = str(meta["computation_version"])
    listed_count = listed_stock_count(owner)

    current_universe: dict[str, str] = {}
    current_financial: dict[str, str] = {}
    fingerprint_error: str | None = None
    try:
        current_universe = universe_fingerprints(owner)
        current_financial = financial_fingerprints(owner)
    except Exception as error:  # noqa: BLE001 - 指纹算不出来就必须退回全量（宁可多算）
        logger.warning("指标输入指纹计算失败，本轮退回全量: %s", error)
        fingerprint_error = str(error)

    prev_universe = _read_json_map(owner, UNIVERSE_FP_KEY)
    prev_financial = _read_json_map(owner, FINANCIAL_FP_KEY)

    merged_sources: dict[str, Iterable[str]] = dict(sources)
    if fingerprint_error is not None and reason is None:
        reason = "scope_planning_failed"
    if reason is None:
        if prev_universe is None or prev_financial is None:
            # 首次部署本机制：没有上轮水位，无法证明谁没变 → 一次全量建立基线。
            reason = "input_fingerprint_bootstrap"
        else:
            merged_sources["universe_fingerprint"] = _diff_codes(prev_universe, current_universe)
            merged_sources["financial_fingerprint"] = _diff_codes(
                prev_financial, current_financial,
            )

    groups, changed_sources = _group_codes(merged_sources)
    plan = IndicatorScopePlan(
        mode="incremental",
        reason=None,
        computation_version=version,
        listed_count=listed_count,
        changed_sources=changed_sources,
        groups=groups,
        universe_fingerprint=current_universe,
        financial_fingerprint=current_financial,
        prev_universe_fingerprint=prev_universe,
        prev_financial_fingerprint=prev_financial,
        fingerprint_error=fingerprint_error,
    )

    # 阈值只看**完整重算组**（整股 149 列），不看价格域组：
    # "今天全市场都成交了"是日常形态，价格域增量本身就会跳过没变的收盘价，
    # 拿它判全量等于每天退回 35 分钟。只有"一大半股票要整股重算"时，
    # 一次全扫（单事务 staging 发布）才真的比逐股维护更省。
    full_group_size = len(groups["full"])
    threshold = max(INCREMENTAL_FULL_MIN_CODES, int(listed_count * INCREMENTAL_FULL_RATIO_THRESHOLD))
    if reason is None and plan.fingerprint_error is None and full_group_size > threshold:
        reason = "incremental_over_threshold"

    if reason is not None:
        plan.mode = "full"
        plan.reason = reason
    return plan


def bootstrap_indicator_scope(owner: Any) -> dict[str, Any]:
    """把"当前输入指纹 + 当前口径版本"直接写成基线（**不重算任何股票**）。

    两个正当用途：
    1. 运维：明确知道现网快照就是当前口径、只想立刻切到增量模式时，用它建立
       基线，避免升级后的第一轮全量；
    2. 回归测试：钉住"上一轮水位"，让用例只验证本轮的变化来源。

    常规路径**不需要**调用它：没有基线时 `plan_indicator_scope` 会判
    `input_fingerprint_bootstrap` 并跑一次全量建立基线。
    """
    universe = universe_fingerprints(owner)
    financial = financial_fingerprints(owner)
    _write_json_map(owner, UNIVERSE_FP_KEY, universe)
    _write_json_map(owner, FINANCIAL_FP_KEY, financial)
    owner._write_refresh_state(COMPUTATION_VERSION_KEY, current_computation_version())
    return {"universe_codes": len(universe), "financial_codes": len(financial)}


# ─── 收尾：只有算成功才推进指纹水位 ──────────────────────────────────────


def _step_succeeded(step: Mapping[str, Any] | None) -> bool:
    if not step:
        return False
    return str(step.get("status")) == "success" and not step.get("failed")


def commit_indicator_scope(owner: Any, plan: IndicatorScopePlan, step: Any) -> dict[str, Any]:
    """把本轮裁决结果落盘：口径版本 + 逐股指纹水位 + 最后的决策记录。

    规则（"不会漏算"的第二条）：**只有 step 成功**才推进水位；partial/failed/
    rejected 一律保留旧指纹 —— 下一轮这些股票会重新进入增量集合（宁可多算）。
    """
    succeeded = _step_succeeded(step)
    committed_codes = 0
    if succeeded:
        if plan.is_full:
            _write_json_map(owner, UNIVERSE_FP_KEY, plan.universe_fingerprint)
            _write_json_map(owner, FINANCIAL_FP_KEY, plan.financial_fingerprint)
            owner._write_refresh_state(COMPUTATION_VERSION_KEY, plan.computation_version)
            committed_codes = len(plan.universe_fingerprint)
        else:
            universe = dict(plan.prev_universe_fingerprint or {})
            financial = dict(plan.prev_financial_fingerprint or {})
            for code in plan.full_codes:
                if code in plan.universe_fingerprint:
                    universe[code] = plan.universe_fingerprint[code]
                else:
                    universe.pop(code, None)
                if code in plan.financial_fingerprint:
                    financial[code] = plan.financial_fingerprint[code]
                else:
                    financial.pop(code, None)
            _write_json_map(owner, UNIVERSE_FP_KEY, universe)
            _write_json_map(owner, FINANCIAL_FP_KEY, financial)
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
            "succeeded": succeeded,
        }, ensure_ascii=False),
    )
    return {"committed_codes": committed_codes, "succeeded": succeeded}
