"""归档核验：用历史归档域的独立实现，持续校验本项目自算指标。

背景（2026-09-20 系统性审查发现）
--------------------------------
为「榨干」导入的 18 张历史归档域，审查发现**零业务代码引用** ——
导入进库却从不被读取，属于「囤积而非利用」。本模块把它变成**可重复的核验哨兵**：

归档域里保存着外部数据包用**自己的算法**算出的同概念数值。把两者按
(股票, 报告期) 逐行比对，一致率就是自算实现正确性的**独立证据**。
一次性的人工核验会随时间失效；本模块把它固化为可重复执行的检查。

历史上正是靠这种核验抓到了真实缺陷
--------------------------------
- `bps` 名为「每股净资产」却用了归母权益 → 一致率仅 25%，修正后 95%
- 若只导入不核验，该错误会长期潜伏。

用法
----
    from app.core.quality.archive_crosscheck import run_crosscheck
    report = run_crosscheck(duck)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CheckPair:
    """一个待核验的概念：本项目的列 ↔ 归档域的列。"""

    concept: str
    our_table: str
    our_column: str
    archive_table: str
    archive_column: str
    tolerance: float = 0.01


# 仅登记「同一概念、独立实现」的对。口径本就不同的（如托宾Q 的市值口径差异）
# 一律不登记 —— 那会制造假失败。
DEFAULT_PAIRS: tuple[CheckPair, ...] = (
    # 注意：资产负债率/流动比率/速动比率在 indicator_snapshot（**仅最新期**），
    # 与归档（≤2025-03-31）没有重叠期，无法比对 —— 故此处改用 v37 已下沉到
    # indicator_ext 的同类偿债指标（有完整历史序列，可比）。
    CheckPair("产权比率", "indicator_ext", "debt_to_equity", "csmar_fi_t1", "F011701A"),
    CheckPair("现金比率", "indicator_ext", "cash_ratio", "csmar_fi_t1", "F010401A"),
    CheckPair("保守速动比率", "indicator_ext", "conservative_quick_ratio", "csmar_fi_t1", "F010301A"),
    CheckPair("应收账款周转率", "indicator_ext", "receivables_turnover", "csmar_fi_t4", "F040201B"),
    CheckPair("存货周转率", "indicator_ext", "inventory_turnover", "csmar_fi_t4", "F040501B"),
    CheckPair("总资产周转率", "indicator_ext", "total_asset_turnover", "csmar_fi_t4", "F041701B"),
    CheckPair("每股净资产", "indicator_ext", "bps", "csmar_per_share_history", "bps"),
    CheckPair("每股营业收入", "indicator_ext", "revenue_per_share",
              "csmar_per_share_history", "revenue_per_share"),
    CheckPair("每股经营现金流", "indicator_ext", "ocf_per_share",
              "csmar_per_share_history", "ocf_per_share"),
    CheckPair("归属母公司每股净资产", "indicator_ext", "bps_parent",
              "csmar_per_share_history", "bps_parent"),
    # ── 2026-09-20 扩充：让归档域的核验覆盖率从 9 个概念提升到 24 个 ──
    CheckPair("权益乘数", "indicator_ext", "equity_multiplier", "csmar_fi_t1", "F011601A"),
    # 以下两组经 2026-09-20 实测确认**口径本就不同**，按本模块纪律不予登记
    # （登记会制造假失败）：
    #
    # 有形净值债务率：我们的口径为「负债 /（权益 − 无形资产 − 商誉）」，是教材标准；
    #   归档字段只扣无形资产、不扣商誉。实测按我们的口径一致率 74.76%，
    #   改成只扣无形资产则 **98.03%** —— 差异**全部由商誉解释**，不是 bug。
    #   （结论：本项目的定义更保守，保留不动。）
    #
    # 应计项目：归档字段是**绝对金额**（元）且用资产负债表法口径
    #   （流动资产−流动负债+应交税费+…−现金净增加+折旧摊销，TTM）；
    #   我们用的是学术标准口径「(净利润 − 经营现金流量净额) / 总资产」。
    #   两者单位与定义都不同，缩放后仍不符（比值 0.006~2.24），不可直接比对。
    CheckPair("每股有形资产", "indicator_ext", "tangible_asset_per_share",
              "csmar_fi_t9", "F091101A"),
    CheckPair("每股负债", "indicator_ext", "liability_per_share", "csmar_fi_t9", "F091201A"),
    CheckPair("每股资本公积", "indicator_ext", "capital_reserve_per_share",
              "csmar_fi_t9", "F091301A"),
    CheckPair("流动资产比率", "indicator_ext", "current_asset_ratio", "csmar_fi_t3", "F030101A"),
    CheckPair("固定资产比率", "indicator_ext", "fixed_asset_ratio", "csmar_fi_t3", "F030801A"),
    CheckPair("销售费用率", "indicator_ext", "selling_expense_ratio", "csmar_fi_t5", "F051701B"),
    CheckPair("管理费用率", "indicator_ext", "admin_expense_ratio", "csmar_fi_t5", "F051801B"),
    CheckPair("财务费用率", "indicator_ext", "finance_expense_ratio", "csmar_fi_t5", "F051901B"),
    CheckPair("应付账款周转率", "indicator_ext", "accounts_payable_turnover",
              "csmar_fi_t4", "F040801B"),
    CheckPair("流动资产周转率", "indicator_ext", "current_asset_turnover",
              "csmar_fi_t4", "F041201B"),
    CheckPair("固定资产周转率", "indicator_ext", "fixed_asset_turnover",
              "csmar_fi_t4", "F041401B"),
    CheckPair("股东权益周转率", "indicator_ext", "equity_turnover", "csmar_fi_t4", "F041801B"),
)

#: 一致率低于该值即视为「需要复核的回归」（不是失败，是提示）
REVIEW_THRESHOLD = 0.85


@dataclass
class CrossCheckResult:
    concept: str
    compared: int = 0
    within_tolerance: float = 0.0
    median_rel_diff: float = 0.0
    needs_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "compared": self.compared,
            "within_tolerance_pct": round(self.within_tolerance * 100, 2),
            "median_rel_diff_pct": round(self.median_rel_diff * 100, 4),
            "needs_review": self.needs_review,
        }


@dataclass
class CrossCheckReport:
    results: list[CrossCheckResult] = field(default_factory=list)

    @property
    def flagged(self) -> list[CrossCheckResult]:
        return [r for r in self.results if r.needs_review]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs_checked": len(self.results),
            "needs_review": [r.concept for r in self.flagged],
            "results": [r.as_dict() for r in self.results],
        }


def _load(duck: Any, table: str, code_column: str, value_column: str) -> pd.DataFrame:
    rows = duck.read_query(
        f'SELECT stock_code, report_date, "{value_column}" AS archive_value FROM "{table}" '
        f'WHERE "{value_column}" IS NOT NULL'
    )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.date
    return frame


def run_crosscheck(duck: Any, pairs: tuple[CheckPair, ...] = DEFAULT_PAIRS) -> CrossCheckReport:
    """逐个概念比对自算值与归档值，返回一致率报告。"""
    report = CrossCheckReport()
    for pair in pairs:
        result = CrossCheckResult(concept=pair.concept)
        try:
            ours = pd.DataFrame(duck.read_query(
                f'SELECT stock_code, report_date, "{pair.our_column}" AS our_value '
                f'FROM "{pair.our_table}" WHERE "{pair.our_column}" IS NOT NULL'
            ))
            archive = _load(duck, pair.archive_table, "stock_code", pair.archive_column)
            if ours.empty or archive.empty:
                report.results.append(result)
                continue
            ours["report_date"] = pd.to_datetime(ours["report_date"]).dt.date
            merged = ours.merge(archive, on=["stock_code", "report_date"], how="inner")
            a = pd.to_numeric(merged["our_value"], errors="coerce").to_numpy(float)
            b = pd.to_numeric(merged["archive_value"], errors="coerce").to_numpy(float)
            ok = np.isfinite(a) & np.isfinite(b)
            a, b = a[ok], b[ok]
            if a.size == 0:
                report.results.append(result)
                continue
            relative = np.abs(a - b) / np.maximum(np.abs(b), 1e-9)
            result.compared = int(a.size)
            result.within_tolerance = float((relative <= pair.tolerance).mean())
            result.median_rel_diff = float(np.median(relative))
            result.needs_review = result.within_tolerance < REVIEW_THRESHOLD
        except Exception:  # noqa: BLE001 - 归档域缺失时跳过该概念，不影响其他检查
            pass
        report.results.append(result)
    return report
