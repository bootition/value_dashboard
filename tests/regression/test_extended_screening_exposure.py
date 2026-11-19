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

import pytest

from app.core.screening.engine import (
    EXTENDED_COLUMNS,
    EXTENDED_SCREENING_READY,
    RANKABLE_INDICATORS,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FORMAT_TS = _REPO_ROOT / "frontend" / "src" / "utils" / "screening-format.ts"


def test_ready_subset_of_extended_columns() -> None:
    assert EXTENDED_SCREENING_READY <= EXTENDED_COLUMNS, (
        f"ready 集合含未登记字段: {sorted(EXTENDED_SCREENING_READY - EXTENDED_COLUMNS)}"
    )


def test_ready_set_is_not_empty() -> None:
    """ready 必须非空，否则界面上一个都用不了。

    注意：2026-09-19 起 18 列已全部就绪（ready == EXTENDED_COLUMNS）。
    若将来新增扩展列却未达覆盖率门槛，它应只进 EXTENDED_COLUMNS 而不进 ready，
    此时 `EXTENDED_SCREENING_READY < EXTENDED_COLUMNS` 会自然成立。
    """
    assert EXTENDED_SCREENING_READY
    assert EXTENDED_SCREENING_READY <= EXTENDED_COLUMNS


def test_rankable_extended_fields_are_all_ready() -> None:
    """扩展字段若参与排名，必须已就绪；否则会生成全 NULL 的排名列。"""
    extended_ranked = {f for f in RANKABLE_INDICATORS if f in EXTENDED_COLUMNS}
    assert extended_ranked <= EXTENDED_SCREENING_READY, (
        f"未就绪却参与排名: {sorted(extended_ranked - EXTENDED_SCREENING_READY)}"
    )


def _stub_request(duck, sqlite):
    """端点只用到 `request.app.state.{duck,sqlite}`（不读 header/query）。"""
    from types import SimpleNamespace

    state = SimpleNamespace(duck=duck, sqlite=sqlite)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _indicators_payload(duckdb_store, sqlite_store, monkeypatch) -> dict:
    """真跑 `list_available_indicators`（不读源码文本）。"""
    import app.core.data_quality as data_quality_module
    from app.web.api.screening import list_available_indicators

    # 单测不跑全量数据质量构建：只把"当前告警码"注入成空（与
    # test_quality_grading / test_series_breaks_and_derived_fields 同一手法）。
    monkeypatch.setattr(data_quality_module, "read_warning_codes", lambda *a, **k: [])
    return list_available_indicators(_stub_request(duckdb_store, sqlite_store))


def test_indicators_endpoint_exposes_exactly_the_ready_set(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/indicators` 下发的扩展字段必须等于 **ready 子集**，未就绪列一条都不许出现。

    拦的事故（2026-09-17 真发生过）：端点按 `EXTENDED_COLUMNS` 铺字段，
    把底层全表 NULL 的列也变成可选条件 —— 用户能选中却永远筛不出结果（死条件）。

    行为断言怎么造出"未就绪列"：现实中它就是 `EXTENDED_COLUMNS - ready`
    （新增列还没过覆盖率门槛）。今天两者恰好相等，所以这里把 ready **收窄一列**
    来模拟那一刻：该列仍在 `EXTENDED_COLUMNS` 里、但不在 ready 里。
    端点若改用 `EXTENDED_COLUMNS`，这一列就会出现在返回里 → 断言红。
    """
    from app.core.screening import engine as engine_module

    ready = frozenset(EXTENDED_SCREENING_READY)
    assert ready, "ready 集合不能为空（否则界面上一个扩展字段都用不了）"
    dropped = sorted(ready)[-1]
    assert dropped in EXTENDED_COLUMNS, "收窄出来的列必须真的在 EXTENDED_COLUMNS 里"
    monkeypatch.setattr(engine_module, "EXTENDED_SCREENING_READY", ready - {dropped})

    payload = _indicators_payload(duckdb_store, sqlite_store, monkeypatch)
    exposed = {
        item["name"] for item in payload["indicators"]
        if item.get("domain") == "extended"
    }

    assert exposed == set(ready - {dropped}), (
        "扩展字段暴露范围 ≠ ready 子集：多出来 = 未就绪列被当成可选条件（死条件），"
        f"少了 = 就绪列在界面上选不到；差集 {sorted(exposed ^ (ready - {dropped}))}"
    )
    # 反向断言（这条才真正拦事故）：未就绪的列不得以任何形式出现在返回里。
    assert dropped not in exposed, (
        f"未就绪列 {dropped} 被 /indicators 下发了：接口又按 EXTENDED_COLUMNS 铺字段，"
        "用户会选中一个底层全 NULL 的死条件"
    )
    assert dropped not in {item["name"] for item in payload["unavailable"]}, (
        f"未就绪列 {dropped} 连 unavailable 都不该出现：它压根不该被枚举到"
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
