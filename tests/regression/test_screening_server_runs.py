from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import SCREENING_RUN_TTL_HOURS, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def test_screening_run_is_server_persisted_and_save_uses_its_output(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm)
           VALUES ('000001', '2025-12-31', 10)
           ON CONFLICT (stock_code, report_date) DO UPDATE SET pe_ttm = excluded.pe_ttm"""
    )
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
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm)
           VALUES ('000001', '2025-12-31', 10)
           ON CONFLICT (stock_code, report_date) DO UPDATE SET pe_ttm = excluded.pe_ttm"""
    )
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
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status)
           VALUES ('000001', 'total_assets', '2025-12-31', 200, 'verified', 'published')"""
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
