"""L0-6（报告42）: 自选添加的股票代码校验（6 位数字 + 存在于股票列表）。

前端与后端双重校验，防止垃圾代码进入自选；筛选路径来源的代码天然合法。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.watchlist import router as watchlist_router


def _build_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(watchlist_router)
    return TestClient(app)


def _seed_stock(duckdb_store: DuckDBStore, code: str) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES (?, 'Test', 'SZSE', '2020-01-01', false, false)""",
        [code],
    )


def test_watchlist_add_rejects_malformed_codes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    client = _build_client(duckdb_store, sqlite_store)
    for bad in ["123", "ABC123", "12345", "1234567", "", "60051 9"]:
        response = client.post("/api/watchlist/add", json={"stock_code": bad, "group_name": "default"})
        assert response.status_code == 400, bad
        assert "invalid stock code" in response.json()["detail"], bad


def test_watchlist_add_rejects_code_not_in_universe(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "000001")
    client = _build_client(duckdb_store, sqlite_store)
    response = client.post("/api/watchlist/add", json={"stock_code": "999999", "group_name": "default"})
    assert response.status_code == 400
    assert "not found in universe" in response.json()["detail"]
    count = sqlite_store.query("SELECT COUNT(*) AS cnt FROM watchlist")[0]["cnt"]
    assert count == 0


def test_watchlist_add_accepts_valid_code_and_trims_whitespace(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "000001")
    client = _build_client(duckdb_store, sqlite_store)
    response = client.post("/api/watchlist/add", json={"stock_code": " 000001 ", "group_name": "default"})
    assert response.status_code == 200, response.text
    assert response.json()["stock_code"] == "000001"
    row = sqlite_store.query(
        "SELECT stock_code, group_name FROM watchlist WHERE stock_code = ?",
        ["000001"],
    )[0]
    assert row["stock_code"] == "000001"
    assert row["group_name"] == "default"
