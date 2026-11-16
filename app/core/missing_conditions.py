"""可行动缺失（actionable missing）口径的唯一真源。

历史：这套 SQL 片段原先写在 ``app/web/api/data_status.py`` 里，而
``app/core/data_quality.py::actionable_missing_count()`` 需要复用它 ——
core 反向 import ``app.web.api`` 是本仓唯一一处层反转（曾靠"惰性导入"绕成环）。
现在下沉到 core：web 与 core 都从这里导入，依赖方向只剩 web → core。

口径要点：
- ``missing_list`` 里 ``resolved_at IS NULL`` 的行才算"仍未解决"；
- 六个金融监管字段当前免费源拿不到 → 不计入"待处理"（保留在库中留档）；
- 北交所/B 股代码的 ``placement_funding`` 属结构性不适用 → 同样不计入。
"""

from __future__ import annotations

#: 当前免费源确实无法补齐的字段：不在缺失列表/计数中继续展示为"待处理"，
#: 保留在库中留档；将来接入公告 PDF 解析或付费源时再从这里移除。
DISABLED_MISSING_FIELDS: frozenset[str] = frozenset({
    "balance.core_tier1_capital_adequacy_ratio",
    "balance.tier1_capital_adequacy_ratio",
    "balance.capital_adequacy_ratio",
    "balance.non_performing_loan_ratio",
    "balance.provision_coverage_ratio",
    "balance.risk_coverage_ratio",
})

#: ``{disabled_placeholders}`` 由调用方替换成与 ``disabled`` 等长的 ``?`` 串，
#: 参数顺序必须与 ``actionable_missing_condition()`` 返回的清单一致。
ACTIONABLE_MISSING_SQL = """(
    field_name NOT IN ({disabled_placeholders})
    AND NOT (
        field_name = 'placement_funding'
        AND substr(stock_code, 1, 1) IN ('4', '8', '9')
    )
)"""


def actionable_missing_condition() -> tuple[str, list[str]]:
    """返回 ``(SQL 条件片段, 占位符参数)``，供 ``missing_list`` 聚合查询使用。

    SQL 里字段名是**白名单常量**，禁用字段只以参数形式传入，因此不存在
    拼接注入面；参数个数与 ``?`` 占位符由本函数一次性保证一致。
    """
    placeholders = ", ".join("?" for _ in DISABLED_MISSING_FIELDS)
    return (
        ACTIONABLE_MISSING_SQL.replace("{disabled_placeholders}", placeholders),
        sorted(DISABLED_MISSING_FIELDS),
    )
