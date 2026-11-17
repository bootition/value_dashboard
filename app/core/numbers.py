"""数值归一化工具（单一实现，2026-11-17 `reports/136` §6.3）。

为什么存在
----------
`_to_float()` 此前在适配器里各有副本，**同名不同义**：

- `czb_mof`：拒绝 NaN/±Infinity（reports/73 P3-10，收益率必须有限）；
- `eastmoney_f10`：裸 `float()`，NaN/±Inf 原样返回；
- `cninfo_funding` / `eastmoney_repurchase` / `hk_dividend` / `index_valuation`：
  NaN→None，但保留 ±Inf（配 `value != value` 判空）。

同一份上游数据在不同适配器里因此得到不同结果。本模块只提供**一个**实现加一个
显式开关，各适配器按自己源的口径调用，语义写在各自的调用点（不再各自抄一份）。

用法
----
    to_float(value)                  # 严格：非有限值（NaN / ±Inf）→ None
    to_float(value, allow_inf=True)  # 宽松：等价于裸 float()

注意
----
- `allow_inf=True` 是逐字等价于 `float(value)`，所以 **NaN 也会被保留**。需要
  "NaN→None 但 ±Inf 保留" 的调用点必须自己做 NaN 判定（见四个 akshare 型适配器）。
- 与原有各份实现一致：只吞 `TypeError` / `ValueError`，不吞 `OverflowError`。
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "to_float",
    "to_float_strict",
    "to_float_keep_inf",
    "to_float_keep_inf_reject_nan",
]


def to_float(value: Any, *, allow_inf: bool = False, reject_nan: bool = False) -> float | None:
    """把上游原始值归一为 float。

    Parameters
    ----------
    value:
        上游原始值（int/float/str/Decimal/...）。`None` 与空串 `""` 视为缺失。
    allow_inf:
        - False（默认，严格）：非有限值（NaN / ±Inf）一律 → None；
        - True（宽松）：只做 `float(value)`，±Inf 与 NaN 原样保留。
    reject_nan:
        仅在 `allow_inf=True` 时有意义：NaN → None，但 ±Inf 仍保留。
        （`cninfo_funding` 等四个源用 NaN 表示缺失，同时允许 ±Inf 通过。）

    Returns
    -------
    float | None
        缺失或不可转换时返回 None。

    Raises
    ------
    与原有各份实现完全一致，不做额外兜底：`OverflowError`（如 `float(10**400)`）
    照常抛出；`pd.NA` 这类「`== ""` 布尔歧义」对象也会在判空时就抛 `TypeError`。
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if reject_nan and math.isnan(number):
        return None
    if allow_inf:
        return number
    return number if math.isfinite(number) else None


def to_float_strict(value: Any) -> float | None:
    """严格口径：NaN / ±Inf → None（`czb_mof` 用它，收益率必须有限）。"""
    return to_float(value)


def to_float_keep_inf(value: Any) -> float | None:
    """宽松口径：等价于裸 `float()`（`eastmoney_f10` 用它）。"""
    return to_float(value, allow_inf=True)


def to_float_keep_inf_reject_nan(value: Any) -> float | None:
    """NaN → None，但 ±Inf 保留（四个 akshare 型源用它，NaN 表示缺失）。"""
    return to_float(value, allow_inf=True, reject_nan=True)
