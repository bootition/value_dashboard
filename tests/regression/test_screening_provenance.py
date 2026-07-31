from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import _field_provenance, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def _client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    insert_matching_trading_calendar(duck, sqlite)
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def _make_screenable(duck: DuckDBStore) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'One', 'SZSE', '2020-01-01', false, false)"""
    )
    duck.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm)
           VALUES ('000001', '2025-12-31', 10)"""
    )
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
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        ["quality_score", 1, "pe_ttm", "Quality score", "none", False, "quality-hash"],
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
    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status)
           VALUES ('000001', 'total_assets', '2025-12-31', 200, 'verified', 'published')"""
    )

    provenance = _field_provenance(
        duckdb_store,
        sqlite_store,
        [{"stock_code": "000001", "_report_date": "2025-12-31"}],
        ["balance.total_assets"],
    )

    assert provenance[0]["total_assets"]["source"] == "published_override"
