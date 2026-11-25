"""`roce_latest` 必须能作为筛选条件参与「周期位置」判断（2026-11-25，reports/168）。

背景
----
`roce_latest`（最近一年年报 ROCE）一直只在个股详情页当"现况对照"，不在
`SNAPSHOT_COLUMNS` 里 —— 于是 `_is_known_field('roce_latest')` 判 False，
条件 JSON 里既不能筛它，也不能用 `right_field` 拿它和 `roce_10y_median` 比。

后果不是"少一个可选指标"，而是**周期股判据缺一条腿**：
- 只有 PB 分位 → 分不清"周期底部便宜"和"结构性衰退便宜"；
- 拿到 `roce_latest < roce_10y_median`（当期低于自身十年中位）才能确认
  「盈利被周期压住，而不是公司变差」。

本文件锁三件事：
1. `roce_latest` 在条件白名单里（保存期与运行期同源，`_is_known_field_name`）；
2. `roce_latest vs roce_10y_median` 的字段对字段比较能通过量纲闸门并编译成 SQL；
3. `/api/screening/indicators` 会列出它（否则用户在下拉里选不到）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.screening.engine import (
    SNAPSHOT_COLUMNS,
    ScreeningEngine,
    validate_rule_fields,
)
from app.core.screening.field_units import field_unit
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router


def _make_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def test_roce_latest_is_a_screenable_column() -> None:
    """白名单是保存期/运行期共用的唯一判定入口（AGENTS R1）。"""
    assert "roce_latest" in SNAPSHOT_COLUMNS
    assert field_unit("roce_latest") == "pct", "必须与 roce_*_median 同族（小数比例）"


def test_roce_latest_rule_saves() -> None:
    """保存期校验必须接受它（否则规则"能跑不能存"）。"""
    validate_rule_fields(
        {
            "conditions": {
                "logic": "AND",
                "rules": [
                    {"field": "roce_latest", "op": "<", "right_field": "roce_10y_median"},
                ],
            }
        }
    )


def test_cycle_position_condition_compiles_to_sql() -> None:
    """`当期 ROCE < 自身 10 年中位` 必须编译成两个列的比较，而不是被拦下。"""
    engine = ScreeningEngine.__new__(ScreeningEngine)
    engine._custom_fields = set()  # type: ignore[attr-defined]
    sql, params = ScreeningEngine._build_condition(
        engine, {"field": "roce_latest", "op": "<", "right_field": "roce_10y_median"}
    )
    assert sql == "roce_latest < roce_10y_median"
    assert params == []


def test_indicators_endpoint_lists_roce_latest(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """用户要能在筛选页的下拉里看到它。"""
    client = _make_client(duckdb_store, sqlite_store)
    payload = client.get("/api/screening/indicators").json()
    names = {entry["name"] for entry in payload["indicators"]}
    assert "roce_latest" in names
    entry = next(item for item in payload["indicators"] if item["name"] == "roce_latest")
    assert entry["unit"] == "pct"
