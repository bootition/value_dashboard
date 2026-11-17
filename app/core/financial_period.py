"""A股定期报告披露季期望 + 北京时间"今天"（共享工具）。

更新链与详情页都必须使用同一个"北京时间披露季"口径：
半年报季（8-10月）期望 06-30；三季报季（11月后）期望 09-30；
一季报季（5-7月）期望 03-31；其余时间期望上一年年报 12-31。

2026-11-17（reports/133 RG1）**时区单源**：本项目所有"今天"的语义一律是
**北京时间**。此前 `app/core/index_valuation.py`、`app/core/treasury.py`、
`app/core/adapters/czb_mof_adapter.py` 各自复制了一份
`timezone(timedelta(hours=8))` + `_cn_today()`，`app/core/adapters/
index_valuation_adapter.py` 更退化成 `date.today()`（跟机器时区走）。
同一语义四处实现 → 已有两次事故：
  * 2026-11-13 `d7bf840` 详情页 `snapshot_age_days=-1`（UTC 与 CN 日期相减）；
  * 2026-11-13 `32a2e9d` 日凌晨重跑全量 universe（日节流标记按 UTC 归零）。

因此：**任何"今天"都必须来自本模块的 `cn_today()` / `cn_now()`**，
禁止在别处再定义 `timezone(timedelta(hours=8))` 或 `def _cn_today()`。
该约束由 `scripts/check_rework_guards.py`（RG1）静态强制。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def cn_now(now: datetime | None = None) -> datetime:
    """北京时间此刻（传入 aware datetime 时按其时刻换算，不做二次猜测）。"""
    if now is None:
        return datetime.now(CN_TZ)
    if now.tzinfo is None:
        raise ValueError("cn_now() requires a timezone-aware datetime")
    return now.astimezone(CN_TZ)


def cn_today(now: datetime | None = None) -> date:
    """北京时间的"今天"。

    所有"距今天数 / 是否到期 / 是否同一交易日"的判断都必须走这里：
    `date.today()` 与 `datetime.now(UTC).date()` 在北京时间 00:00–08:00
    会给出与 A 股交易日不同的答案（reports/117 §7、reports/118 §3 P2）。
    """
    return cn_now(now).date()


def expected_financial_period(now: datetime | None = None) -> str:
    """Return the report period that should already be published today (Beijing time)."""
    current = cn_now(now)
    year = current.year
    if current.month >= 11:
        return f"{year}-09-30"
    if current.month >= 8:
        return f"{year}-06-30"
    if current.month >= 5:
        return f"{year}-03-31"
    return f"{year - 1}-12-31"
