"""报告期日期合法性（reports/146）。

为什么需要单独一个模块
----------------------
`cash_flow_indirect` 里有 **5,481 行 `report_date = 2024-01-01`**，
且不是孤例：2013–2024 **每一年**都有一个 `YYYY-01-01` 批次，与 `(YYYY-1)-12-31`
在 90%+ 的股票上**数值完全相同**（实测 2024-01-01 vs 2023-12-31：5,382 只两期都有值，
其中 4,885 只逐位相同）。这是 CSMAR 历史批次把「上一年的年报」重复记成了
「次年 1 月 1 日」——**同一条数据落成了两个报告期**。

危害不是"多几行"，而是：

1. 任何 `ORDER BY report_date` 的"最近一期"逻辑会把 `2024-01-01` 当成新年度的期次；
2. 财务序列（LAG/环比/TTM）里多出一个伪期次，会把区间算错；
3. 覆盖率/期次统计被重复计数。

所以「报告期必须是标准期末」这条约束需要**一处实现、多处复用**：
写库层在落盘前挡掉，修复脚本按同一份规则清理存量。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final

#: 合法报告期（月, 日）。A 股只有这四个：一季报 / 半年报 / 三季报 / 年报。
STANDARD_REPORT_PERIODS: Final[tuple[tuple[int, int], ...]] = (
    (3, 31),
    (6, 30),
    (9, 30),
    (12, 31),
)

#: 报告期中文口径名（月 → 名称）
_PERIOD_NAMES: Final[dict[int, str]] = {
    3: "一季报",
    6: "半年报",
    9: "三季报",
    12: "年报",
}


def is_standard_report_date(value: object) -> bool:
    """`value` 是否为合法报告期（含类型校验，None/字符串/非法日期一律 False）。"""
    parsed = _as_date(value)
    if parsed is None:
        return False
    return (parsed.month, parsed.day) in STANDARD_REPORT_PERIODS


def report_period_name(value: object) -> str:
    """报告期 → 中文口径名（如「2026 半年报」）；非法日期返回「未知报告期」。"""
    parsed = _as_date(value)
    if parsed is None:
        return "未知报告期"
    name = _PERIOD_NAMES.get(parsed.month)
    if name is None or not is_standard_report_date(parsed):
        return f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}（非标准报告期）"
    return f"{parsed.year} {name}"


def canonical_year_start_target(value: object) -> date | None:
    """`YYYY-01-01` 形态的错记日期 → 它**本该属于**的报告期 `(YYYY-1)-12-31`。

    这是 CSMAR 历史批次的固定错法（把上年年报记成次年 1 月 1 日）。
    非该形态返回 None，交由调用方按其它规则处理。
    """
    parsed = _as_date(value)
    if parsed is None:
        return None
    if (parsed.month, parsed.day) != (1, 1):
        return None
    return date(parsed.year - 1, 12, 31)


def annual_period_for(value: object) -> date | None:
    """任意日期 → 它**当时能看到的最新年度报告期**（`YYYY-12-31`）。

    用途（reports/146 §6）：`company_employee_history` 的东财批次把
    `report_date` 写成了**抓取当天**（2026-08-25 ~ 2026-09-17），
    导致 `2025-12-31` 这一期整期缺失 —— 而抓取时能看到的最新年度就是 FY2025。

    规则：取 `<= 该日期` 的最近一个 12-31。
    """
    parsed = _as_date(value)
    if parsed is None:
        return None
    if parsed.month == 12 and parsed.day == 31:
        return parsed
    return date(parsed.year - 1, 12, 31)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                continue
    return None
