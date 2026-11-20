"""更新窗口内的只读解耦回归（2026-11-20，用户实测「更新中几乎全员不可用」）。

## 事故

自动更新是**独立子进程**（`app/web/main.py` 的 `subprocess.run`），它独占 DuckDB
文件句柄整个更新期间；Web 进程 `read_query()` 是 open-per-query → 每次新开只读连接
都 `IOException` → `DuckDBReadLockedError` → 搜索/详情/筛选全部 503。
用户截图：`搜索失败，请稍后重试`；原话：*「就算是慢了几天的数据，那我就不能打开看了吗？」*

## 守住的契约

1. **个股搜索**：`stock_meta` 只有 6,285 行 → 锁住时用进程内快照照常搜，
   且 `served_from="cache"` + `as_of` 让前端能说"这是更新前的数据"；
2. **详情入口**：`/info` 锁住时仍能打开（有旧值用旧值，冷缓存退到 `stock_meta` 快照）；
3. **详情各块**：锁住时回退上次成功值；没有则降级成**带标记**的空块，绝不整页 503；
4. **筛选**：锁住时返回上次结果 + 标注；从来没有结果 → **409 人话**（不是 503 不是
   `warning_codes`）；
5. **不假绿**：真的读不到且没有旧值 → 必须如实报错（`DuckDBReadLockedError` 上抛）。

## 隔离

`duckdb_store` / `sqlite_store` fixture（`--basetemp` 下的临时库），**绝不碰**
`data/valuedashboard.duckdb`。锁用「让 `read_query` 抛 `DuckDBReadLockedError`」模拟，
与真实写进程持锁后的异常类型逐字一致。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.blacklist import add_stock
from app.core.storage import read_cache
from app.core.storage.duckdb_store import DuckDBReadLockedError, DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed

LIQUOR = "000858"
VANKE = "000002"
MAOTAI = "600519"

#: 与真实写进程持锁时 `duckdb_store` 抛出的消息同形（Windows 实测原文）。
LOCKED_MESSAGE = (
    "IO Error: Cannot open file \"valuedashboard.duckdb\": "
    "另一个程序正在使用此文件，进程无法访问。 File is already open in python.exe (PID 49296)"
)


class LockedDuck:
    """把真表达层包一层：**任何** `read_query` 都抛 `DuckDBReadLockedError`。

    与真实场景一致——锁的是**文件**，不是某张表：所以不可能靠"换一条 SQL"绕开，
    只有"不查库"（用进程内副本）或"如实报错"两条路。
    """

    def __init__(self, inner: DuckDBStore) -> None:
        self._inner = inner
        self.db_path = inner.db_path
        self.read_attempts = 0

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        self.read_attempts += 1
        raise DuckDBReadLockedError(LOCKED_MESSAGE)

    def __getattr__(self, name: str) -> Any:  # 其余属性透传真 store
        return getattr(self._inner, name)


@pytest.fixture(autouse=True)
def _clean_read_cache():
    """每个用例从空缓存开始：缓存是**模块级**进程状态，不清会串租户。"""
    read_cache.clear_cache()
    yield
    read_cache.clear_cache()


def _app(duck: Any, sqlite: SQLiteStore) -> FastAPI:
    from app.web.api.stock_detail import router as stock_detail_router

    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    _install_read_locked_handler(app)
    return app


def _install_read_locked_handler(app: FastAPI) -> None:
    """复刻 `app/web/main.py:576` 的全局 handler（那边是 `create_app` 内的闭包）。

    只复刻**对外契约**（503 + 稳定 `reason_code`）；契约本身另有
    `tests/regression/test_web_composition_root.py` 守着真实 `create_app`。
    """
    from fastapi.responses import JSONResponse

    @app.exception_handler(DuckDBReadLockedError)
    async def _handler(_request, _error: DuckDBReadLockedError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "数据正在自动更新中，请稍后刷新",
                "reason_code": "duckdb_read_locked",
            },
        )


def _seed(duck: DuckDBStore) -> None:
    seed = Seed(duck)
    seed.stock(LIQUOR, name="五 粮 液", exchange="SZSE", csrc_l1="制造业", is_listed=True)
    seed.stock(VANKE, name="万  科Ａ", exchange="SZSE", csrc_l1="房地产业", is_listed=True)
    seed.stock(MAOTAI, name="贵州茅台", exchange="SSE", csrc_l1="制造业", is_listed=True)


# ─── 1. 个股搜索：更新中照常搜 ───────────────────────────────────────


def test_search_works_while_duckdb_is_read_locked(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """核心用例：锁住后搜索**仍然返回正确结果**，并标注来自缓存。"""
    _seed(duckdb_store)
    client = TestClient(_app(duckdb_store, sqlite_store))

    # 先正常搜一次（真实库），让进程内快照建立起来
    first = client.get("/api/stock/search", params={"query": "五粮液"})
    assert first.status_code == 200
    assert [row["stock_code"] for row in first.json()["items"]] == [LIQUOR]
    assert first.json()["served_from"] == "live"

    locked = LockedDuck(duckdb_store)
    client = TestClient(_app(locked, sqlite_store))
    # 快照是**按库路径**存的，换 app 不换库 → 直接可用
    read_cache.refresh_stock_meta_snapshot(duckdb_store)

    gated = client.get("/api/stock/search", params={"query": "五粮液"})

    assert gated.status_code == 200, gated.text
    body = gated.json()
    assert [row["stock_code"] for row in body["items"]] == [LIQUOR]
    assert body["items"][0]["name"] == "五 粮 液"  # 源数据原样，空白归一化仍在
    assert body["served_from"] == "cache"
    assert body["stale"] is True
    assert body["reason_code"] == "duckdb_read_locked"
    assert body["as_of"], "陈旧标记必须带 as_of，否则前端无法说'这是几点的数据'"
    assert locked.read_attempts == 1, "锁窗口内只应打一次库（先试真读），不得重试轰炸"


def test_search_degrades_honestly_without_snapshot(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """**不许假绿**：从来没读到过快照时，如实 503，而不是返回空结果。"""
    _seed(duckdb_store)
    client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))

    response = client.get("/api/stock/search", params={"query": "五粮液"})

    assert response.status_code == 503
    assert response.json()["reason_code"] == "duckdb_read_locked"


def test_snapshot_search_keeps_semantics(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """锁窗口内的搜索必须与 SQL 版逐条等价：黑名单、超 20 条、排序、空白 """

    seed = Seed(duckdb_store)
    _seed(duckdb_store)
    for index in range(25):
        seed.stock(f"601{index:03d}", name=f"银行样本{index:02d}", exchange="SSE", is_listed=True)

    live_client = TestClient(_app(duckdb_store, sqlite_store))
    cases = ["五粮液", "万科", "银行", "6005", "000858", "%", "_", "zzz"]
    # 先正常搜一遍让快照建立（同时覆盖 SQL 路径本身）
    for case in cases:
        assert live_client.get(
            "/api/stock/search", params={"query": case}
        ).status_code == 200

    add_stock(sqlite_store, VANKE)  # 黑名单必须同样生效
    live_after_blacklist = {
        case: live_client.get("/api/stock/search", params={"query": case}).json()["items"]
        for case in cases
    }

    read_cache.clear_cache()
    read_cache.refresh_stock_meta_snapshot(duckdb_store)
    locked_client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))
    for case in cases:
        cached = locked_client.get("/api/stock/search", params={"query": case})
        assert cached.status_code == 200, case
        assert cached.json()["items"] == live_after_blacklist[case], case

    # LIMIT 20 与"万科被拉黑后搜不到"一并核对
    from app.web.api.stock_detail import SEARCH_LIMIT

    assert SEARCH_LIMIT == 20
    assert len(live_after_blacklist["银行"]) == SEARCH_LIMIT
    assert live_after_blacklist["万科"] == []


# ─── 2. 个股详情入口：能搜到就能打开 ─────────────────────────────────


def test_stock_info_served_from_last_success_while_locked(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    _seed(duckdb_store)
    Seed(duckdb_store).prices(LIQUOR, [("2026-11-19", 128.5)])
    live_client = TestClient(_app(duckdb_store, sqlite_store))

    ok = live_client.get(f"/api/stock/{LIQUOR}/info")
    assert ok.status_code == 200
    assert ok.json()["name"] == "五 粮 液"
    assert ok.json()["latest_close"] == 128.5

    locked_client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))
    gated = locked_client.get(f"/api/stock/{LIQUOR}/info")

    assert gated.status_code == 200, gated.text
    body = gated.json()
    assert body["name"] == "五 粮 液"
    assert body["exchange"] == "SZSE"
    assert body["latest_close"] == 128.5
    assert body["served_from"] == "cache"
    assert body["stale"] is True
    assert body["as_of"]


def test_stock_info_falls_back_to_meta_snapshot_cold_cache(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """`/info` 从没被读过也要能打开 —— 退到 `stock_meta` 全表快照，价格置空。"""
    _seed(duckdb_store)
    read_cache.refresh_stock_meta_snapshot(duckdb_store)
    client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))

    response = client.get(f"/api/stock/{LIQUOR}/info")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "五 粮 液"
    assert body["csrc_l1"] == "制造业"
    assert body["latest_close"] is None
    assert body["served_from"] == "cache"
    assert body["stale"] is True


def test_detail_blocks_return_last_success_instead_of_503(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """K 线等大表块：锁住时给上次成功值，而不是整页 503。"""
    _seed(duckdb_store)
    Seed(duckdb_store).prices(
        LIQUOR, [("2026-11-18", 126.0), ("2026-11-19", 128.5)]
    )
    live_client = TestClient(_app(duckdb_store, sqlite_store))
    ok = live_client.get(f"/api/stock/{LIQUOR}/kline", params={"period": "day"})
    assert ok.status_code == 200
    assert len(ok.json()["candles"]) == 2

    locked_client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))
    gated = locked_client.get(f"/api/stock/{LIQUOR}/kline", params={"period": "day"})

    assert gated.status_code == 200, gated.text
    body = gated.json()
    assert len(body["candles"]) == 2
    assert body["data_freshness"]["served_from"] == "cache"
    assert body["data_freshness"]["stale"] is True


def test_detail_block_degrades_to_marked_empty_block(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """没有旧值时降级成空块，但**必须带 degraded 标记**（不是静默假绿）。"""
    _seed(duckdb_store)
    client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))

    response = client.get(f"/api/stock/{LIQUOR}/kline", params={"period": "day"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candles"] == []
    assert body["data_freshness"]["degraded"] is True
    assert body["data_freshness"]["reason_code"] == "no_cached_snapshot"
    assert body["data_freshness"]["notice"]


def test_detail_block_cache_key_distinguishes_query_params(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """`period=week` 的旧值绝不能冒充 `period=day` 的结果。"""
    _seed(duckdb_store)
    Seed(duckdb_store).prices(
        LIQUOR, [("2026-11-18", 126.0), ("2026-11-19", 128.5)]
    )
    live_client = TestClient(_app(duckdb_store, sqlite_store))
    assert live_client.get(
        f"/api/stock/{LIQUOR}/kline", params={"period": "week"}
    ).status_code == 200

    locked_client = TestClient(_app(LockedDuck(duckdb_store), sqlite_store))
    gated = locked_client.get(f"/api/stock/{LIQUOR}/kline", params={"period": "day"})

    assert gated.status_code == 200
    # day 从没成功过 → 空块，不许拿 week 的缓存顶上
    assert gated.json()["candles"] == []
    assert gated.json()["data_freshness"]["degraded"] is True


# ─── 3. 筛选：不要 503，要人话 ───────────────────────────────────────


def test_screening_run_returns_last_result_while_locked(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web.api.screening import ScreeningRequest, _screening_run_key

    read_cache.remember(
        duckdb_store,
        _screening_run_key(ScreeningRequest(rule_id=49, rule_version=1)),
        {"total": 4214, "results": [{"stock_code": LIQUOR}], "data_date": "2026-11-19"},
    )
    app = _screening_app(LockedDuck(duckdb_store), sqlite_store)
    _pass_the_gate(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/screening/run",
        json={"rule_id": 49, "rule_version": 1},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4214
    assert body["served_from"] == "cache"
    assert body["stale"] is True
    assert "更新" in body["notice"]


def test_screening_run_reports_human_409_without_cache(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _screening_app(LockedDuck(duckdb_store), sqlite_store)
    _pass_the_gate(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/screening/run",
        json={"rule_id": 49, "rule_version": 1},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "data_update_in_progress"
    assert "warning_codes" not in str(detail)
    assert "数据状态" not in detail["message"]
    assert "已保存的筛选结果" in detail["message"]


def _pass_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「可筛选性门禁」放行——**本用例不测那道门**（它有自己的回归）。

    门禁判据（交易日历分级/信任快照）是另一条已有的防线；这里要证明的是
    「门禁放行之后、写锁仍在」时 `/run` 不再 503 而是回退旧结果/给人话 409。
    """
    monkeypatch.setattr(
        "app.web.api.screening._require_current_screenability",
        lambda _request: {
            "lock_active": True,
            "data_as_of": "2026-11-19",
            "readiness_source": "lock_window",
            "readiness_cached_at": None,
            "readiness_stale": False,
            "readiness_age_seconds": None,
        },
    )


def _screening_app(duck: Any, sqlite: SQLiteStore) -> FastAPI:
    from app.web.api.screening import router as screening_router

    sqlite.execute(
        """INSERT INTO screening_rules (id, name, version, rule_json, locked_indicators)
           VALUES (?, ?, ?, ?, ?)""",
        [49, "ROCE遗传好公司", 1, '{"conditions": {"rules": []}}', "{}"],
    )
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.state.startup_readiness = {"ready": True}
    app.include_router(screening_router)
    _install_read_locked_handler(app)
    return app


# ─── 4. 通用回退层自身的契约 ─────────────────────────────────────────


def test_serve_reraises_when_no_previous_value(duckdb_store: DuckDBStore) -> None:
    def _boom() -> dict:
        raise DuckDBReadLockedError(LOCKED_MESSAGE)

    with pytest.raises(DuckDBReadLockedError):
        read_cache.serve(duckdb_store, ("missing",), _boom)


def test_serve_does_not_swallow_non_lock_errors(duckdb_store: DuckDBStore) -> None:
    """非锁类故障不许被缓存层吞成 200：那会把真错误伪装成正常。"""
    read_cache.serve(duckdb_store, ("k",), lambda: {"v": 1})

    def _boom() -> dict:
        raise ValueError("bad sql")

    with pytest.raises(ValueError):
        read_cache.serve(duckdb_store, ("k",), _boom)


def test_freshness_meta_marks_live_and_cache_differently() -> None:
    live = read_cache.freshness_meta(None, live=True, stale=False)
    assert live == {
        "served_from": "live", "as_of": None, "stale": False,
        "reason_code": None, "notice": None,
    }
    entry = read_cache.entry_with_as_of("2026-11-20T08:00:00+00:00")
    stale = read_cache.freshness_meta(
        entry, live=False, stale=True, reason_code=read_cache.REASON_READ_LOCKED,
        notice=read_cache.STALE_NOTICE,
    )
    assert stale["served_from"] == "cache"
    assert stale["as_of"] == "2026-11-20T08:00:00+00:00"
    assert stale["stale"] is True


def test_clear_cache_drops_previous_value(duckdb_store: DuckDBStore) -> None:
    read_cache.serve(duckdb_store, ("k",), lambda: {"v": 1})
    read_cache.clear_cache()

    def _boom() -> dict:
        raise DuckDBReadLockedError(LOCKED_MESSAGE)

    with pytest.raises(DuckDBReadLockedError):
        read_cache.serve(duckdb_store, ("k",), _boom)


def test_http_exception_still_propagates_through_cache_layer(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """404（代码不存在）不能被缓存层改写成 503，也不能被当成"陈旧"。"""
    _seed(duckdb_store)
    client = TestClient(_app(duckdb_store, sqlite_store))

    response = client.get("/api/stock/999999/info")

    assert response.status_code == 404


# ─── 5. 黑名单页：代码在 SQLite，名字走快照 ──────────────────────────


def test_blacklist_list_works_while_locked(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """黑名单里那几行**代码**是 SQLite 事实；锁住时只有"取名字"该降级。"""
    from app.web.api.blacklist import router as blacklist_router

    _seed(duckdb_store)
    add_stock(sqlite_store, LIQUOR)
    read_cache.refresh_stock_meta_snapshot(duckdb_store)

    app = FastAPI()
    app.state.duck = LockedDuck(duckdb_store)
    app.state.sqlite = sqlite_store
    app.include_router(blacklist_router)
    _install_read_locked_handler(app)

    response = TestClient(app).get("/api/blacklist/list")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["stock_code"] for item in body["items"]] == [LIQUOR]
    assert body["items"][0]["name"] == "五 粮 液"
    assert body["served_from"] == "cache"
    assert body["stale"] is True
    assert body["as_of"]
