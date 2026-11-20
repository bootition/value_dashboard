from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import _field_provenance, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed
from tests.support.seed_sqlite import CURRENT_TIMESTAMP, SeedSqlite


def _client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    insert_matching_trading_calendar(duck, sqlite)
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def _make_screenable(duck: DuckDBStore) -> None:
    seed = Seed(duck)
    seed.stock("000001", name="One", exchange="SZSE", listing_date="2020-01-01",
               is_st=False, is_suspended=False)
    seed.snapshot("000001", "2025-12-31", pe_ttm=10)
    insert_minimum_screenable_data(duck)


def test_saved_result_and_watchlist_require_rule_version_provenance(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store)
    client = _client(duckdb_store, sqlite_store)
    rule = client.post(
        "/api/screening/rules/save",
        json={"name": "value", "rule_json": {"conditions": {"logic": "AND", "rules": []}}},
    )
    assert rule.status_code == 200
    rule_data = rule.json()

    save_payload = {
        "title": "strict value candidates",
        "run_id": "missing-run",
    }
    saved = client.post("/api/screening/save", json=save_payload)
    assert saved.status_code == 400

    with sqlite_store.transaction() as conn:
        # 留（第 4 类·多站点缺口，本轮不补 API）：`screening_runs` 全仓 3 处播种、
        # 工厂没有方法；本轮租约禁止扩 `seed_sqlite.py`，如实登记为下一轮候选。
        conn.execute(
            """INSERT INTO screening_runs
               (run_id, rule_id, rule_version, result_json, columns_json, sort_json,
                data_date, base_pool_config, strict_only, confidence_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "server-run", rule_data["rule_id"], rule_data["version"],
                '[{"stock_code":"000001","name":"Strict One"}]', '["stock_code","name"]', '[]',
                "2025-12-31", '{}', True, '{"strict_only":true}',
            ],
        )
    saved = client.post("/api/screening/save", json={"title": "strict value candidates", "run_id": "server-run"})
    assert saved.status_code == 200
    result_id = saved.json()["result_id"]

    watchlist = client.post(
        "/api/screening/add_to_watchlist",
        json={"stock_codes": ["000001"], "group": "screening", "result_id": result_id},
    )
    assert watchlist.status_code == 200
    row = sqlite_store.query(
        "SELECT source_rule_id, source_result_id FROM watchlist WHERE stock_code = ?", ["000001"]
    )[0]
    assert row == {"source_rule_id": rule_data["rule_id"], "source_result_id": result_id}

    rejected = client.post(
        "/api/screening/add_to_watchlist",
        json={"stock_codes": ["000002"], "group": "screening", "result_id": result_id},
    )
    assert rejected.status_code == 400


def test_csv_includes_rule_version_and_strict_provenance(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store)
    client = _client(duckdb_store, sqlite_store)
    SeedSqlite(sqlite_store).dsl_expression(
        "quality_score",
        1,
        "pe_ttm",
        status="published",
        description="Quality score",
        direction="none",
        historical_capable=False,
        content_hash="quality-hash",
    )
    rule = client.post(
        "/api/screening/rules/save",
        json={
            "name": "export-value",
            "rule_json": {"conditions": {"logic": "AND", "rules": [
                {"field": "quality_score", "op": ">", "value": 0},
            ]}},
        },
    ).json()
    with sqlite_store.transaction() as conn:
        # 留（第 4 类·多站点缺口，本轮不补 API）：同上，`screening_runs` 无工厂方法。
        conn.execute(
            """INSERT INTO screening_runs
               (run_id, rule_id, rule_version, result_json, columns_json, sort_json,
                data_date, base_pool_config, strict_only, confidence_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "export-run", rule["rule_id"], rule["version"],
                '[{"stock_code":"000001","name":"Strict One"}]', '["stock_code","name"]', '[]',
                "2025-12-31", '{}', True, '{"strict_only":true}',
            ],
        )
    result_id = client.post(
        "/api/screening/save", json={"title": "export candidates", "run_id": "export-run"}
    ).json()["result_id"]
    response = client.post("/api/screening/export_csv", json={"result_id": result_id})

    assert response.status_code == 200
    header, row = response.json()["csv"].splitlines()
    assert "_rule_version" in header
    assert f",{rule['version']}," in row
    assert "True" in row


def test_field_provenance_maps_normalized_fields_and_marks_overrides(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    SeedSqlite(sqlite_store).override(
        "000001",
        "total_assets",
        200,
        reason="verified",
        report_date="2025-12-31",
        status="published",
        # 原 SQL 没写 `created_at`（由 schema 的 `DEFAULT CURRENT_TIMESTAMP` 兜底）：
        # 显式交回库侧时钟，免得迁移守则里那条"刚写的变成固定过去时间"漂移。
        created_at=CURRENT_TIMESTAMP,
    )

    provenance = _field_provenance(
        duckdb_store,
        sqlite_store,
        [{"stock_code": "000001", "_report_date": "2025-12-31"}],
        ["balance.total_assets"],
    )

    assert provenance[0]["total_assets"]["source"] == "published_override"
