"""股票黑名单可见性回归（2026-11-22，用户需求）。

守住的底线
----------
1. **落库**：`init_sqlite_schema()` 建出 `stock_blacklist` 且 `schema_migrations`
   有 v20 —— 只建表不记账会让 `skip_if_current` 快路径漏建表；
2. **单一真源**：排除谓词只由 `app/core/blacklist.py::exclusion_condition()`
   生成；空集合返回空串（拼进 SQL 会语法错误）、超上限**拒绝**而不是静默截断；
3. **单股出口全屏蔽**：`/{stock_code}/...` 全部 15 个端点共用
   `require_visible_stock` 依赖，黑名单后一律 404 + `detail.code ==
   "stock_blacklisted"`（前端据此区分「代码不存在」与「被屏蔽」）；
4. **多股出口**：搜索 / 自选列表 / 已保存筛选结果 / CSV 导出 都不含黑名单股票；
5. **筛选基础池**：黑名单进的是**基础池条件**而不是前端过滤——排名/分位是跑在
   `base_pool` 上的窗口函数，池里剔掉才能让排名分母与可见行数自洽；
6. **可逆**：移出黑名单后数据立刻重新可见（含「自选列表里的原有行自动回来」——
   屏蔽期不删除 watchlist 行）；
7. **不误伤**：黑名单只影响读侧，`stock_meta`/`indicator_snapshot` 等原始表一行不动。

隔离：全部用 `duckdb_store` / `sqlite_store` fixture（`--basetemp` 下的临时库），
**绝不碰** `data/valuedashboard.duckdb`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import blacklist as blacklist_core
from app.core.blacklist import (
    InvalidStockCodeError,
    add_stock,
    blacklisted_code_set,
    blacklisted_codes,
    exclusion_condition,
    filter_rows,
    is_blacklisted,
    remove_stock,
)
from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

BLOCKED = "000001"
VISIBLE = "600519"
BLOCKED_NAME = "平安银行"
VISIBLE_NAME = "贵州茅台"


# ─── 造数 ──────────────────────────────────────────────────────────


def _seed_meta(duck: DuckDBStore, code: str, name: str) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES (?, ?, 'SZSE', TRUE)
           ON CONFLICT (stock_code) DO UPDATE SET name = excluded.name, is_listed = TRUE""",
        [code, name],
    )


def _seed_price(duck: DuckDBStore, code: str) -> None:
    duck.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
           VALUES (?, DATE '2026-01-05', 10, 1000) ON CONFLICT DO NOTHING""",
        [code],
    )


@pytest.fixture
def seeded(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> dict[str, Any]:
    for code, name in ((BLOCKED, BLOCKED_NAME), (VISIBLE, VISIBLE_NAME)):
        _seed_meta(duckdb_store, code, name)
        _seed_price(duckdb_store, code)
    return {"duck": duckdb_store, "sqlite": sqlite_store}


def _api(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    """装配与 `app/web/main.py` 相同的路由集合（守卫 + 无守卫两套）。"""
    from app.web.api.blacklist import router as blacklist_router
    from app.web.api.stock_detail import router as stock_detail_router
    from app.web.api.watchlist import router as watchlist_router

    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    # 外部入口与 `app/web/main.py` 完全一致：只 include 一次
    app.include_router(stock_detail_router)
    app.include_router(watchlist_router)
    app.include_router(blacklist_router)
    return TestClient(app)


# ─── 1. schema + 迁移记账 ───────────────────────────────────────────


def test_sqlite_schema_creates_stock_blacklist_and_records_migration_20(
    sqlite_store: SQLiteStore,
) -> None:
    tables = {
        row["name"] for row in sqlite_store.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "stock_blacklist" in tables

    versions = {row["version"] for row in sqlite_store.query("SELECT version FROM schema_migrations")}
    assert 20 in versions, "建表必须同时记账，否则 skip_if_current 快路径会漏建表"

    columns = {
        row["name"] for row in sqlite_store.query("PRAGMA table_info(stock_blacklist)")
    }
    assert {"stock_code", "reason", "created_at"} <= columns


# ─── 2. 单一真源：exclusion_condition ──────────────────────────────


def test_exclusion_condition_returns_empty_for_empty_set() -> None:
    """空集合必须返回空串：把空谓词拼进 SQL 会直接语法错误。"""
    assert exclusion_condition("stock_code", []) == ("", [])


def test_exclusion_condition_is_parameterised_not_interpolated() -> None:
    predicate, params = exclusion_condition("m.stock_code", ["000001", "600519"])
    assert predicate == "m.stock_code NOT IN (?, ?)"
    assert params == ["000001", "600519"]
    # 代码绝不能出现在 SQL 文本里（参数化，不是字符串拼接）
    assert "000001" not in predicate


def test_exclusion_condition_rejects_oversized_set() -> None:
    """超过上限必须抛错——静默截断会让被截掉的那部分股票误显示。"""
    codes = [f"{index:06d}" for index in range(blacklist_core.MAX_EXCLUSION_CODES + 1)]
    with pytest.raises(ValueError, match="上限"):
        exclusion_condition("stock_code", codes)


# ─── 3. 增删查 + 代码校验 ──────────────────────────────────────────


def test_add_is_idempotent_and_remove_is_exact(sqlite_store: SQLiteStore) -> None:
    add_stock(sqlite_store, BLOCKED)
    add_stock(sqlite_store, BLOCKED)  # 重复加入不报错
    assert blacklisted_codes(sqlite_store) == [BLOCKED]
    assert is_blacklisted(sqlite_store, BLOCKED) is True
    assert is_blacklisted(sqlite_store, VISIBLE) is False

    assert remove_stock(sqlite_store, BLOCKED) == 1
    assert remove_stock(sqlite_store, BLOCKED) == 0
    assert blacklisted_codes(sqlite_store) == []


def test_invalid_stock_code_is_rejected_not_silently_corrected(
    sqlite_store: SQLiteStore,
) -> None:
    for bad in ("", "12345", "1234567", "abcdef", "60051a"):
        with pytest.raises(InvalidStockCodeError):
            add_stock(sqlite_store, bad)


def test_filter_rows_drops_only_blacklisted_codes() -> None:
    rows = [{"stock_code": BLOCKED}, {"stock_code": VISIBLE}]
    assert filter_rows(rows, {BLOCKED}) == [{"stock_code": VISIBLE}]
    assert filter_rows(rows, set()) == rows


# ─── 4. 单股出口：守卫依赖覆盖全部端点 ────────────────────────────


def test_every_stock_path_endpoint_is_behind_the_guard(seeded: dict[str, Any]) -> None:
    """链式断言：**每一个** `{stock_code}` 端点都必须 404 + 结构化 detail。

    为什么不写成「列举 15 个路径逐个测」：那样新增端点仍会漏。
    这里从 OpenAPI 反向枚举全部带 `{stock_code}` 的路径，将来加一个就自动被覆盖。
    """
    from app.web.api.stock_detail import router as stock_detail_router

    app = FastAPI()
    app.state.duck = seeded["duck"]
    app.state.sqlite = seeded["sqlite"]
    app.include_router(stock_detail_router)

    paths = [
        path
        for path, operations in app.openapi()["paths"].items()
        if "{stock_code}" in path and "get" in operations
    ]
    assert len(paths) >= 14, paths
    assert "/api/stock/search" not in paths, "搜索端点没有路径参数，走 SQL 层过滤"

    client = TestClient(app)
    add_stock(seeded["sqlite"], BLOCKED)
    for path in paths:
        url = path.replace("{stock_code}", BLOCKED).replace("{filename}", "x.pdf")
        response = client.get(url)
        assert response.status_code == 404, f"{path} 未受守卫"
        assert response.json()["detail"]["code"] == "stock_blacklisted", f"{path} 漏出"


def test_info_returns_structured_404_when_blacklisted(seeded: dict[str, Any]) -> None:
    client = _api(seeded["duck"], seeded["sqlite"])
    add_stock(seeded["sqlite"], BLOCKED)

    response = client.get(f"/api/stock/{BLOCKED}/info")
    assert response.status_code == 404
    detail = response.json()["detail"]
    # 结构体而不是字符串：前端要能区分「代码不存在」与「被黑名单屏蔽」
    assert detail["code"] == "stock_blacklisted"

    # 未拉黑的股票照常可见
    ok = client.get(f"/api/stock/{VISIBLE}/info")
    assert ok.status_code == 200
    assert ok.json()["name"] == VISIBLE_NAME


def test_removing_from_blacklist_restores_visibility(seeded: dict[str, Any]) -> None:
    client = _api(seeded["duck"], seeded["sqlite"])
    add_stock(seeded["sqlite"], BLOCKED)
    assert client.get(f"/api/stock/{BLOCKED}/info").status_code == 404

    assert client.request(
        "DELETE", "/api/blacklist/remove", json={"stock_code": BLOCKED}
    ).status_code == 200
    assert client.get(f"/api/stock/{BLOCKED}/info").status_code == 200


def test_raw_tables_are_untouched_by_blacklisting(seeded: dict[str, Any]) -> None:
    """黑名单只影响读侧：原始行情/元数据一行不动（用户要求"数据可以留在库里"）。"""
    duck = seeded["duck"]
    before = duck.read_query(
        "SELECT COUNT(*) AS n FROM price_daily_raw WHERE stock_code = ?", [BLOCKED]
    )[0]["n"]
    add_stock(seeded["sqlite"], BLOCKED)
    _api(seeded["duck"], seeded["sqlite"]).get(f"/api/stock/{BLOCKED}/info")
    after = duck.read_query(
        "SELECT COUNT(*) AS n FROM price_daily_raw WHERE stock_code = ?", [BLOCKED]
    )[0]["n"]
    assert before == after == 1
    meta = duck.read_query(
        "SELECT is_listed FROM stock_meta WHERE stock_code = ?", [BLOCKED]
    )[0]
    assert bool(meta["is_listed"]) is True, "不能拿 is_listed=FALSE 冒充黑名单"


# ─── 5. 多股出口 ───────────────────────────────────────────────────


def test_search_excludes_blacklisted_stock(seeded: dict[str, Any]) -> None:
    client = _api(seeded["duck"], seeded["sqlite"])

    before = client.get("/api/stock/search", params={"query": "银行"}).json()["items"]
    assert [row["stock_code"] for row in before] == [BLOCKED]

    add_stock(seeded["sqlite"], BLOCKED)
    after = client.get("/api/stock/search", params={"query": "银行"}).json()["items"]
    assert after == []

    # 按代码搜也一样搜不到
    by_code = client.get("/api/stock/search", params={"query": BLOCKED}).json()["items"]
    assert by_code == []


def test_watchlist_hides_blacklisted_but_keeps_the_row(seeded: dict[str, Any]) -> None:
    client = _api(seeded["duck"], seeded["sqlite"])
    sqlite = seeded["sqlite"]
    sqlite.execute(
        "INSERT INTO watchlist (stock_code, group_name) VALUES (?, '核心持仓')",
        [BLOCKED],
    )

    before = client.get("/api/watchlist/list").json()
    assert [item["stock_code"] for item in before["items"]] == [BLOCKED]
    assert before["groups"][0]["cnt"] == 1

    add_stock(sqlite, BLOCKED)
    hidden = client.get("/api/watchlist/list").json()
    assert hidden["items"] == []
    # 侧栏分组计数必须与可见行同口径，否则显示「1」而表里 0 行
    assert all(group["cnt"] == 0 for group in hidden["groups"])

    # 屏蔽期**不删除** watchlist 行：移出黑名单后自动回来
    assert sqlite.query("SELECT COUNT(*) AS n FROM watchlist")[0]["n"] == 1
    remove_stock(sqlite, BLOCKED)
    restored = client.get("/api/watchlist/list").json()
    assert [item["stock_code"] for item in restored["items"]] == [BLOCKED]


def test_adding_blacklisted_stock_to_watchlist_is_refused(seeded: dict[str, Any]) -> None:
    client = _api(seeded["duck"], seeded["sqlite"])
    add_stock(seeded["sqlite"], BLOCKED)

    response = client.post("/api/watchlist/add", json={"stock_code": BLOCKED})
    assert response.status_code == 400
    assert "blacklisted" in response.json()["detail"]


def test_saved_screening_result_and_csv_export_drop_blacklisted_rows(
    seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """已保存结果是冻结 JSON，无法在 SQL 层过滤 —— 必须读出来后在内存剔除。"""
    from app.web.api import screening as screening_api

    sqlite = seeded["sqlite"]
    frozen_rows = [
        {"stock_code": BLOCKED, "name": BLOCKED_NAME},
        {"stock_code": VISIBLE, "name": VISIBLE_NAME},
    ]
    # screening_results.rule_id 有外键 → 先造一条来源规则
    sqlite.execute(
        """INSERT INTO screening_rules (id, name, version, rule_json, locked_indicators)
           VALUES (1, 'blacklist-probe', 1, '{}', '{}')"""
    )
    sqlite.execute(
        """INSERT INTO screening_results
               (id, title, note, rule_id, rule_version, data_date,
                result_json, columns_json, sort_json, confidence_summary, base_pool_config)
           VALUES (1, 't', 'n', 1, 1, '2026-01-05', ?, ?, '[]', '{}', '{}')""",
        [json.dumps(frozen_rows, ensure_ascii=False), '["stock_code", "name"]'],
    )

    app = FastAPI()
    app.state.duck = seeded["duck"]
    app.state.sqlite = sqlite
    app.include_router(screening_api.router)
    # 导出端点带就绪门禁；本用例只验证「导出内容不含黑名单股票」，把门禁短路。
    monkeypatch.setattr(screening_api, "_require_current_screenability", lambda request: {})
    client = TestClient(app)

    add_stock(sqlite, BLOCKED)

    saved = client.get("/api/screening/results/1").json()
    assert [row["stock_code"] for row in saved["result_json"]] == [VISIBLE]

    exported = client.post("/api/screening/export_csv", json={"result_id": 1})
    assert exported.status_code == 200
    csv_text = exported.json()["csv"]
    assert VISIBLE in csv_text
    assert BLOCKED not in csv_text


# ─── 6. 筛选基础池（排名分母口径）─────────────────────────────────


def test_screening_base_pool_excludes_blacklisted_codes(seeded: dict[str, Any]) -> None:
    """黑名单必须进基础池条件：排名窗口函数跑在 base_pool 之上，
    只在前端过滤会让百分位与可见行数对不上。"""
    duck, sqlite = seeded["duck"], seeded["sqlite"]
    engine = ScreeningEngine(duck=duck, sqlite=sqlite)

    engine._blacklist_codes = []
    assert engine._get_base_pool_size(True, True, 0) == 2

    add_stock(sqlite, BLOCKED)
    engine._blacklist_codes = blacklisted_codes(sqlite)
    assert engine._get_base_pool_size(True, True, 0) == 1

    sql, params = engine._build_sql(
        conditions={},
        sort_spec=[],
        columns_spec=[],
        include_st=True,
        include_suspended=True,
        min_listing_years=0,
        rank_fields=set(),
        strict_fields=set(),
        compiled_indicators={},
    )
    predicate, expected_params = exclusion_condition("m.stock_code", [BLOCKED])
    assert predicate in sql
    assert expected_params[0] in params

    # 真正执行一遍：池里只剩未拉黑的那只
    rows = duck.read_query(sql, params)
    returned = {row["stock_code"] for row in rows}
    assert returned == {VISIBLE}


def test_blacklist_does_not_touch_unrelated_codes(seeded: dict[str, Any]) -> None:
    sqlite = seeded["sqlite"]
    add_stock(sqlite, BLOCKED, reason="不想看")
    assert blacklisted_code_set(sqlite) == frozenset({BLOCKED})
    assert not is_blacklisted(sqlite, VISIBLE)


# ─── 7. 写入面：/api/blacklist/* 端点本身 ──────────────────────────


def test_blacklist_api_round_trip(seeded: dict[str, Any]) -> None:
    """详情页按钮实际走的路径：check → add → list → remove → check。"""
    client = _api(seeded["duck"], seeded["sqlite"])

    assert client.get(f"/api/blacklist/check/{BLOCKED}").json()["blacklisted"] is False
    assert client.get("/api/blacklist/list").json() == {"items": [], "count": 0}

    added = client.post("/api/blacklist/add", json={"stock_code": BLOCKED})
    assert added.status_code == 200
    assert added.json()["stock_code"] == BLOCKED

    # 幂等：再加一次不报错
    assert client.post("/api/blacklist/add", json={"stock_code": BLOCKED}).status_code == 200
    assert client.get(f"/api/blacklist/check/{BLOCKED}").json()["blacklisted"] is True

    listed = client.get("/api/blacklist/list").json()
    assert listed["count"] == 1
    assert listed["items"][0]["stock_code"] == BLOCKED
    # 管理页要能辨认是哪只股票，所以名称/交易所来自 stock_meta
    assert listed["items"][0]["name"] == BLOCKED_NAME
    assert listed["items"][0]["exchange"] == "SZSE"

    assert client.request(
        "DELETE", "/api/blacklist/remove", json={"stock_code": BLOCKED}
    ).status_code == 200
    assert client.get(f"/api/blacklist/check/{BLOCKED}").json()["blacklisted"] is False
    assert client.get("/api/blacklist/list").json()["count"] == 0


def test_blacklist_api_rejects_unknown_and_malformed_codes(
    seeded: dict[str, Any],
) -> None:
    client = _api(seeded["duck"], seeded["sqlite"])

    # 格式不合法 → 400（不静默纠正成别的代码）
    for bad in ("", "12345", "abcdef", "6005199"):
        assert client.post("/api/blacklist/add", json={"stock_code": bad}).status_code == 400

    # 格式合法但不在股票池 → 400（防止垃圾代码进黑名单把管理页塞满）
    missing = client.post("/api/blacklist/add", json={"stock_code": "999999"})
    assert missing.status_code == 400
    assert "not found" in missing.json()["detail"]

    # 重复移出 → 404，而不是假装成功
    assert client.request(
        "DELETE", "/api/blacklist/remove", json={"stock_code": BLOCKED}
    ).status_code == 404
