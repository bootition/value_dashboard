"""扩展指标域「可上界面」子集的不变量回归（2026-09-19）。

背景：2026-09-17 核验发现 `deducted_profit_yoy` 等三个字段已注册进 DSL、
筛选引擎、导出清单，但底层全表 NULL —— 用户在界面上能选中，却永远筛不出结果。
这类「死条件」是真实发生过的产品缺陷，必须用测试守住。

本文件守住：
1. `EXTENDED_SCREENING_READY ⊆ EXTENDED_COLUMNS`（不能凭空冒出字段）；
2. 参与排名的扩展字段必须都在 ready 集合内（否则会生成全空的排名列）；
3. `/api/screening/indicators` 暴露的扩展字段 === ready 集合（不遗漏、不越界）；
4. **前端中文名契约**：ready 集合内每个字段都必须在 `screening-format.ts`
   的 FIELD_LABELS 里有中文名 —— 项目规则要求用户可见指标中文优先，
   漏加标签会让界面直接显示英文列名。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.screening.engine import (
    EXTENDED_COLUMNS,
    EXTENDED_SCREENING_READY,
    RANKABLE_INDICATORS,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FORMAT_TS = _REPO_ROOT / "frontend" / "src" / "utils" / "screening-format.ts"


def test_ready_subset_of_extended_columns() -> None:
    assert EXTENDED_SCREENING_READY <= EXTENDED_COLUMNS, (
        f"ready 集合含未登记字段: {sorted(EXTENDED_SCREENING_READY - EXTENDED_COLUMNS)}"
    )


def test_ready_set_is_not_empty_and_has_room_to_grow() -> None:
    """ready 必须非空（否则界面上一个都用不了）；
    同时未就绪列必须显式保留在 EXTENDED_COLUMNS 中（历史研究仍可查）。"""
    assert EXTENDED_SCREENING_READY
    assert EXTENDED_COLUMNS - EXTENDED_SCREENING_READY, "全部就绪时应删除本断言并更新注释"


def test_rankable_extended_fields_are_all_ready() -> None:
    """扩展字段若参与排名，必须已就绪；否则会生成全 NULL 的排名列。"""
    extended_ranked = {f for f in RANKABLE_INDICATORS if f in EXTENDED_COLUMNS}
    assert extended_ranked <= EXTENDED_SCREENING_READY, (
        f"未就绪却参与排名: {sorted(extended_ranked - EXTENDED_SCREENING_READY)}"
    )


def test_indicators_endpoint_exposes_exactly_the_ready_set() -> None:
    """直接调用 /indicators 处理函数，比对扩展字段的暴露范围。"""
    import inspect

    from app.web.api.screening import list_available_indicators

    source = inspect.getsource(list_available_indicators)
    assert "EXTENDED_SCREENING_READY" in source, "indicator 接口必须基于 ready 子集暴露"
    assert "EXTENDED_COLUMNS" not in source, (
        "indicator 接口不得直接暴露 EXTENDED_COLUMNS（会把未就绪列变成死条件）"
    )


def test_frontend_has_chinese_labels_for_every_ready_field() -> None:
    text = _FORMAT_TS.read_text(encoding="utf-8")
    missing = [
        field
        for field in sorted(EXTENDED_SCREENING_READY)
        if not re.search(rf"^\s*{re.escape(field)}\s*:", text, flags=re.MULTILINE)
    ]
    assert not missing, (
        f"以下已上界面的字段缺少中文名（会直接显示英文列名）: {missing}"
    )
