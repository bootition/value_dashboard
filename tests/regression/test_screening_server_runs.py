from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import SCREENING_RUN_TTL_HOURS, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed
from tests.support.seed_sqlite import CURRENT_TIMESTAMP, SeedSqlite

# `stock_meta` / `indicator_snapshot` / `manual_overrides` 三张表走工厂的按域方法：
# `name='Test'`、`exchange='SZSE'` 逐字传（工厂默认值按代码段推断，与本文件夹具
# 不是同一口径），`pe_ttm` 仍是本轮用例自己要钉的业务值。


def test_screening_run_is_server_persisted_and_save_uses_its_output(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    Seed(duckdb_store).stock(
        "000001", name="Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # 本轮 SQL 的 `ON CONFLICT DO UPDATE SET pe_ttm = excluded.pe_ttm` 与
    # `overwrite=True`（DuckDB 的 INSERT OR REPLACE 是 upsert，只更新本次给出的列）
    # 同粒度：`insert_minimum_screenable_data()` 刚写进去的
    # latest_close / latest_price_date / calculated_at 必须原样留着，不能整行重来。
    Seed(duckdb_store).snapshot("000001", "2025-12-31", pe_ttm=10, overwrite=True)
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)
    rule = client.post(
        "/api/screening/rules/save",
        json={
            "name": "run-value",
            "rule_json": {
                "conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
                "columns": ["stock_code", "name", "pe_ttm"],
            },
        },
    ).json()

    run = client.post(
        "/api/screening/run",
        json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
    )
    assert run.status_code == 200
    # P1-C: API 响应必须包含截断契约键（小池子 → truncated=False）
    assert run.json()["truncated"] is False
    assert run.json()["total"] == 1
    assert run.json()["results"] == [{"stock_code": "000001", "name": "Test", "pe_ttm": 10.0}]

    saved = client.post("/api/screening/save", json={
        "title": "server computed", "run_id": run.json()["run_id"], "columns": ["stock_code", "pe_ttm"],
    })
    assert saved.status_code == 200
    persisted = sqlite_store.query("SELECT result_json FROM screening_results WHERE id = ?", [saved.json()["result_id"]])[0]
    persisted_rows = __import__("json").loads(persisted["result_json"])
    assert persisted_rows[0]["_report_date"] == "2025-12-31"
    assert persisted_rows[0]["stock_code"] == "000001"
    saved_metadata = sqlite_store.query(
        "SELECT columns_json, base_pool_config FROM screening_results WHERE id = ?", [saved.json()["result_id"]]
    )[0]
    assert saved_metadata["columns_json"] == '["stock_code", "pe_ttm"]'
    assert saved_metadata["base_pool_config"] == '{"include_st": false, "include_suspended": false, "min_listing_years": 0}'
    assert sqlite_store.query("SELECT * FROM screening_runs WHERE run_id = ?", [run.json()["run_id"]]) == []


def test_screening_run_ttl_keeps_recent_runs_and_lazily_purges_expired_ones(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    Seed(duckdb_store).stock(
        "000001", name="Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # 本轮 SQL 的 `ON CONFLICT DO UPDATE SET pe_ttm = excluded.pe_ttm` 与
    # `overwrite=True`（DuckDB 的 INSERT OR REPLACE 是 upsert，只更新本次给出的列）
    # 同粒度：`insert_minimum_screenable_data()` 刚写进去的
    # latest_close / latest_price_date / calculated_at 必须原样留着，不能整行重来。
    Seed(duckdb_store).snapshot("000001", "2025-12-31", pe_ttm=10, overwrite=True)
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)
    rule = client.post(
        "/api/screening/rules/save",
        json={
            "name": "ttl-value",
            "rule_json": {
                "conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
                "columns": ["stock_code", "name", "pe_ttm"],
            },
        },
    ).json()

    def run() -> str:
        response = client.post(
            "/api/screening/run",
            json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
        )
        assert response.status_code == 200
        return response.json()["run_id"]

    first_run_id = run()
    second_run_id = run()

    # P1-5回归: 新请求不得删除尚在 TTL 有效期内未保存的其他 run
    surviving = {row["run_id"] for row in sqlite_store.query("SELECT run_id FROM screening_runs")}
    assert surviving == {first_run_id, second_run_id}

    # 过期 run 由下一次请求的原子惰性清理回收
    sqlite_store.execute(
        "UPDATE screening_runs SET created_at = datetime('now', ?) WHERE run_id = ?",
        [f"-{SCREENING_RUN_TTL_HOURS + 1} hours", first_run_id],
    )
    third_run_id = run()
    remaining = {row["run_id"] for row in sqlite_store.query("SELECT run_id FROM screening_runs")}
    assert remaining == {second_run_id, third_run_id}


def test_screening_uses_published_statement_overrides(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    Seed(duckdb_store).stock(
        "000001", name="Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    SeedSqlite(sqlite_store).override(
        "000001",
        "total_assets",
        200,
        reason="verified",
        report_date="2025-12-31",
        status="published",
        # 原 SQL 没写 `created_at`（由 schema 的 `DEFAULT CURRENT_TIMESTAMP` 兜底）：
        # 按迁移守则显式交回库侧时钟，免得"刚覆写的"变成固定过去时间。
        created_at=CURRENT_TIMESTAMP,
    )
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)
    rule = client.post(
        "/api/screening/rules/save",
        json={"name": "overridden", "rule_json": {
            "conditions": {"logic": "AND", "rules": [
                {"field": "balance.total_assets", "op": ">", "value": 150},
            ]},
            "columns": ["stock_code", "balance.total_assets"],
        }},
    ).json()

    response = client.post(
        "/api/screening/run",
        json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["balance.total_assets"] == 200.0


def test_screening_run_accepts_freely_selected_result_columns(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    Seed(duckdb_store).stock(
        "000001", name="Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)
    rule = client.post(
        "/api/screening/rules/save",
        json={"name": "free-columns", "rule_json": {
            "conditions": {"logic": "AND", "rules": []},
            "columns": ["stock_code", "pe_ttm"],
        }},
    ).json()
    columns = ["stock_code", "name", "listing_date", "total_shares", "circ_shares", "pe_ttm"]

    response = client.post(
        "/api/screening/run",
        json={
            "rule_id": rule["rule_id"], "rule_version": rule["version"],
            "min_listing_years": 0, "columns": columns,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["stock_code"] == "000001"
    assert body["results"][0]["listing_date"] == "2020-01-01"
    assert body["results"][0]["total_shares"] == 100
    assert body["results"][0]["circ_shares"] == 80
    persisted = sqlite_store.query(
        "SELECT columns_json FROM screening_runs WHERE run_id = ?",
        [body["run_id"]],
    )[0]
    assert persisted["columns_json"] == '["stock_code", "name", "listing_date", "total_shares", "circ_shares", "pe_ttm"]'
