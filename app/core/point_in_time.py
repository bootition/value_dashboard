"""时点可见性（point-in-time，「当时可见」口径）查询层。

背景
----
本项目历史研究此前只能采用「最新重述回看」口径：计算 2015 年的市盈率时，
用的是 2015 年的股价，但配的是**今天才知道的、已被公司事后更正过的**财务数字。
`docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` §8.1 因此只能声明：

    「PE/PB 历史研究序列首期采用『最新重述回看』口径……
      界面、API、导出和规则元数据必须标记『非当时可见、不用于回测』」

公布日来自两段拼接：
1. 历史归档域的年度公布日（1990-2024，74,509 条）
2. 东方财富 datacenter 的 `NOTICE_DATE`（2026-09-20 补入 295,139 条，
   覆盖**全部报告期**：一季报 / 中报 / 三季报 / 年报，1988-2026）

据此可还原「在某一天，投资者能看到的最新一期财报是哪一期」——
即「当时可见」口径，**季度频率**的回测成立。

能力与边界（重要，不得夸大）
----------------------------
- ✅ 支持：给定股票与日期，返回**当时可见的最新年度报告期**及其公布日；
  以及「该期财务数字在 as_of 当天是否已公开」的判定。
- ✅ **季度频率**：`latest_visible_period` 返回最近一期已公开的财报（任意频率）；
  需要年报时用 `frequency="annual"`。
- ⚠️ 覆盖 6,264 只 / 369,648 条 / 1988-12-31 ~ 2026-06-30。
  极少数股票（新上市或暂停披露）无记录时返回 None，**不做推断**。

纪律
----
本模块**只回答「何时公开」**，不改变任何财务数值口径；调用方决定如何使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.core.storage.duckdb_store import DuckDBStore

# 如实披露的边界，供 UI/导出引用
PIT_LIMITATIONS: tuple[str, ...] = (
    "季度频率：覆盖一季报/中报/三季报/年报（2026-09-20 起，此前仅年报）",
    "覆盖 6,264 只 / 369,648 条 / 1988-12-31 ~ 2026-06-30",
    "无公布日记录的报告期不参与判定（不假设未登记即当天可见）",
    "本能力只描述「何时公开」，不改变财务数值口径",
)


@dataclass(frozen=True)
class VisiblePeriod:
    """某股票在某一时点「当时可见」的年报期。"""

    stock_code: str
    report_date: date          # 可见的最新报告期
    announce_date: date        # 该报告期的公布日（<= as_of）
    days_since_announce: int   # 距公布日天数（用于「新鲜度」判断）

    @property
    def is_stale(self) -> bool:
        """超过一年未更新年报（用于提示数据陈旧，非错误）。"""
        return self.days_since_announce > 400


_FREQUENCY_FILTER = {
    "any": "",
    "annual": " AND EXTRACT(month FROM report_date) = 12",
    "interim": " AND EXTRACT(month FROM report_date) IN (6, 9)",
    "quarterly": " AND EXTRACT(month FROM report_date) IN (3, 9)",
}


def latest_visible_period(
    duck: DuckDBStore, stock_code: str, as_of: date, frequency: str = "any"
) -> VisiblePeriod | None:
    """返回 `as_of` 当天「当时可见」的最新报告期；无记录返回 None。

    `frequency`：`any`（默认，任意频率）/ `annual`（仅年报）/ `interim`（中报+三季报）
    / `quarterly`（一季报+三季报）。

    判定条件：`announce_date <= as_of`。缺失公布日的报告期**不参与**判定
    （不能假设未登记公布日就等于当天可见）。
    """
    if frequency not in _FREQUENCY_FILTER:
        raise ValueError(f"未知 frequency: {frequency}")
    rows = duck.read_query(
        """SELECT report_date, announce_date
           FROM financial_report_dates
           WHERE stock_code = ? AND announce_date <= CAST(? AS DATE)"""
        + _FREQUENCY_FILTER[frequency]
        + """
           ORDER BY report_date DESC
           LIMIT 1""",
        [stock_code, str(as_of)],
    )
    if not rows:
        return None
    report_date = rows[0]["report_date"]
    announce_date = rows[0]["announce_date"]
    if isinstance(report_date, str):
        report_date = date.fromisoformat(report_date)
    if isinstance(announce_date, str):
        announce_date = date.fromisoformat(announce_date)
    return VisiblePeriod(
        stock_code=stock_code,
        report_date=report_date,
        announce_date=announce_date,
        days_since_announce=(as_of - announce_date).days,
    )


def latest_visible_annual(
    duck: DuckDBStore, stock_code: str, as_of: date
) -> VisiblePeriod | None:
    """`latest_visible_period(frequency="annual")` 的兼容别名（仅年报）。"""
    return latest_visible_period(duck, stock_code, as_of, frequency="annual")


def was_visible(duck: DuckDBStore, stock_code: str, report_date: date, as_of: date) -> bool | None:
    """判断某报告期的财务数字在 `as_of` 当天是否已公开。

    返回 True/False；该报告期无公布日记录时返回 **None**（未知，不得当作 False）。
    """
    rows = duck.read_query(
        """SELECT announce_date FROM financial_report_dates
           WHERE stock_code = ? AND report_date = CAST(? AS DATE)""",
        [stock_code, str(report_date)],
    )
    if not rows:
        return None
    announce_date = rows[0]["announce_date"]
    if isinstance(announce_date, str):
        announce_date = date.fromisoformat(announce_date)
    return announce_date <= as_of


def coverage(duck: DuckDBStore) -> dict:
    """公布日域覆盖概况（供状态页/文档引用）。"""
    rows = duck.read_query(
        """SELECT COUNT(*) AS rows,
                  COUNT(DISTINCT stock_code) AS stocks,
                  MIN(report_date) AS first_period,
                  MAX(report_date) AS last_period,
                  MIN(announce_date) AS first_announce,
                  MAX(announce_date) AS last_announce
           FROM financial_report_dates"""
    )
    out = dict(rows[0]) if rows else {}
    out["limitations"] = list(PIT_LIMITATIONS)
    return out
