"""A股定期报告披露季期望（共享工具）。

更新链与详情页都必须使用同一个"北京时间披露季"口径：
半年报季（8-10月）期望 06-30；三季报季（11月后）期望 09-30；
一季报季（5-7月）期望 03-31；其余时间期望上一年年报 12-31。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def expected_financial_period(now: datetime | None = None) -> str:
    """Return the report period that should already be published today (Beijing time)."""
    current = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    year = current.year
    if current.month >= 11:
        return f"{year}-09-30"
    if current.month >= 8:
        return f"{year}-06-30"
    if current.month >= 5:
        return f"{year}-03-31"
    return f"{year - 1}-12-31"
