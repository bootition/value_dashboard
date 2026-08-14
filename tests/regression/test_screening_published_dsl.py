from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import resolve_rule_indicator_locks, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def _app(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    insert_matching_trading_calendar(duck, sqlite)
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.state.startup_readiness = {"ready": True}
    app.include_router(router)
    return TestClient(app)


def test_screening_executes_a_hash_locked_published_dsl_indicator(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.web.api.screening._require_current_screenability", lambda request: {"lock_active": False, "data_as_of": None})
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'One', 'SZSE', '2020-01-01', false, false),
                  ('000002', 'Two', 'SZSE', '2020-01-01', false, false)"""
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm, pb_mrq)
           VALUES ('000001', '2025-12-31', 10, 2), ('000002', '2025-12-31', 10, 10)"""
    )
    insert_minimum_screenable_data(duckdb_store, "000001")
    insert_minimum_screenable_data(duckdb_store, "000002")
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        ["value_score", 1, "pe_ttm / pb_mrq", "Value score", "higher_is_better", False, "locked-hash"],
    )
    client = _app(duckdb_store, sqlite_store)

    rule = client.post("/api/screening/rules/save", json={
        "name": "DSL rule",
        "rule_json": {
            "conditions": {"logic": "AND", "rules": [{"field": "value_score", "op": ">", "value": 2}]},
            "columns": ["stock_code", "value_score"],
            "sort": [{"field": "value_score", "direction": "desc"}],
        },
    })
    assert rule.status_code == 200
    saved_rule = sqlite_store.query("SELECT locked_indicators FROM screening_rules WHERE id = ?", [rule.json()["rule_id"]])[0]
    assert saved_rule["locked_indicators"] == '{"value_score": {"version": 1, "content_hash": "locked-hash"}}'

    run = client.post("/api/screening/run", json={
        "rule_id": rule.json()["rule_id"], "rule_version": rule.json()["version"], "min_listing_years": 0,
    })
    assert run.status_code == 200
    assert run.json()["results"] == [{
        "stock_code": "000001", "name": "One", "value_score": 5.0,
        "_entry_explanation": "value_score > 2 (实际: 5.0000)",
    }]


def test_screening_rejects_a_stale_published_indicator_lock(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        ["score", 1, "pe_ttm", "Score", "none", False, "current-hash"],
    )
    response = _app(duckdb_store, sqlite_store).post("/api/screening/rules/save", json={
        "name": "stale lock",
        "locked_indicators": {"score": {"version": 1, "content_hash": "old-hash"}},
        "rule_json": {"conditions": {"logic": "AND", "rules": [{"field": "score", "op": ">", "value": 0}]}},
    })

    assert response.status_code == 400
    assert "stale" in response.json()["detail"]


def test_screening_locks_a_published_dsl_indicator_used_as_the_right_field(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        ["right_score", 2, "pe_ttm", "Right score", "none", False, "right-hash"],
    )

    response = _app(duckdb_store, sqlite_store).post("/api/screening/rules/save", json={
        "name": "right field lock",
        "rule_json": {
            "conditions": {
                "logic": "AND",
                "rules": [{"field": "pb_mrq", "op": "<", "right_field": "right_score"}],
            },
        },
    })

    assert response.status_code == 200
    saved = sqlite_store.query("SELECT locked_indicators FROM screening_rules WHERE id = ?", [response.json()["rule_id"]])[0]
    assert saved["locked_indicators"] == '{"right_score": {"version": 2, "content_hash": "right-hash"}}'


def test_cli_and_web_share_published_indicator_lock_resolution(sqlite_store: SQLiteStore) -> None:
    sqlite_store.execute(
           """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES ('cli_score', 3, 'pe_ttm', 'published', 'CLI score', 'none', false, 'cli-hash')"""
    )

    locks = resolve_rule_indicator_locks(
        sqlite_store,
        {"conditions": {"logic": "AND", "rules": [{"field": "cli_score", "op": ">", "value": 0}]}},
        {},
    )

    assert locks == {"cli_score": {"version": 3, "content_hash": "cli-hash"}}


def test_screening_executes_published_cross_section_dsl_function(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.web.api.screening._require_current_screenability", lambda request: {"lock_active": False, "data_as_of": None})
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'One', 'SZSE', '2020-01-01', false, false),
                  ('000002', 'Two', 'SZSE', '2020-01-01', false, false)"""
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm)
           VALUES ('000001', '2025-12-31', 10), ('000002', '2025-12-31', 20)"""
    )
    insert_minimum_screenable_data(duckdb_store, "000001")
    insert_minimum_screenable_data(duckdb_store, "000002")
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        ["pe_rank", 1, "rank(pe_ttm)", "PE rank", "lower_is_better", False, "rank-hash"],
    )
    client = _app(duckdb_store, sqlite_store)
    rule = client.post("/api/screening/rules/save", json={
        "name": "rank rule",
        "rule_json": {
            "conditions": {"logic": "AND", "rules": [{"field": "pe_rank", "op": "=", "value": 1}]},
            "columns": ["stock_code", "pe_rank"],
        },
    }).json()

    response = client.post("/api/screening/run", json={
        "rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0,
    })

    assert response.status_code == 200
    assert response.json()["results"][0]["stock_code"] == "000001"
    assert response.json()["results"][0]["pe_rank"] == 1


def test_published_dsl_and_indicator_catalog_expose_financial_industry_fields(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Bank', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        """UPDATE balance_sheet SET capital_adequacy_ratio = 12
           WHERE stock_code = '000001' AND report_date = '2025-12-31'"""
    )
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        ["capital_score", 1, "balance.capital_adequacy_ratio", "Capital score", "higher_is_better", False, "capital-hash"],
    )
    client = _app(duckdb_store, sqlite_store)
    indicators = client.get("/api/screening/indicators").json()["indicators"]
    assert {"balance.capital_adequacy_ratio", "balance.non_performing_loan_ratio"} <= {
        item["name"] for item in indicators
    }
    rule = client.post("/api/screening/rules/save", json={
        "name": "capital rule",
        "rule_json": {
            "conditions": {"logic": "AND", "rules": [{"field": "capital_score", "op": ">", "value": 10}]},
            "columns": ["stock_code", "capital_score", "balance.capital_adequacy_ratio"],
        },
    }).json()

    response = client.post("/api/screening/run", json={
        "rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0,
    })

    assert response.status_code == 200
    assert response.json()["results"][0]["capital_score"] == 12
    assert response.json()["results"][0]["balance.capital_adequacy_ratio"] == 12
