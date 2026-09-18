"""时点可见性（point-in-time，「当时可见」口径）查询层。

背景
----
本项目历史研究此前只能采用「最新重述回看」口径：计算 2015 年的市盈率时，
用的是 2015 年的股价，但配的是**今天才知道的、已被公司事后更正过的**财务数字。
`docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` §8.1 因此只能声明：

    「PE/PB 历史研究序列首期采用『最新重述回看』口径……
      界面、API、导出和规则元数据必须标记『非当时可见、不用于回测』」

CSMAR C17 的 `FAR_Finidx.Annodt` 提供了 **1990-2024 年每份年报的公布日期**
（74,509 条，填充率 97.7%），据此可以还原「在某一天，投资者能看到的最新一份年报
是哪一期」——即「当时可见」口径，从而使**年度频率的回测**成立。

能力与边界（重要，不得夸大）
----------------------------
- ✅ 支持：给定股票与日期，返回**当时可见的最新年度报告期**及其公布日；
  以及「该期财务数字在 as_of 当天是否已公开」的判定。
- ⚠️ **仅年度频率**：CSMAR 只提供年报公布日；季报/中报公布日需要另行采集
  （CNINFO 公告库），当前**不做**。因此 `visible_annual_period` 返回的是
  「最近一期**已公开的年报**」，不等价于「最近一期已公开的财报」。
- ⚠️ 数据截止 2024-12-31（2025 年年报公布日尚无）。2025-01-01 之后的 as_of
  只能看到 2024 年报（若已公布）。

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
    "仅年度频率：CSMAR 只提供年报公布日，季报/中报公布日未采集",
    "数据截止 2024-12-31（2025 年年报公布日尚无）",
    "填充率 97.7%（74,509/76,262）；1,753 条缺失集中在 2014-2021 年，缺失即返回无记录",
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


def latest_visible_annual(
    duck: DuckDBStore, stock_code: str, as_of: date
) -> VisiblePeriod | None:
    """返回 `as_of` 当天「当时可见」的最新年度报告期；无记录返回 None。

    判定条件：`announce_date <= as_of`。缺失公布日的报告期**不参与**判定
    （不能假设未登记公布日就等于当天可见）。
    """
    rows = duck.read_query(
        """SELECT report_date, announce_date
           FROM financial_report_dates
           WHERE stock_code = ? AND announce_date <= CAST(? AS DATE)
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
