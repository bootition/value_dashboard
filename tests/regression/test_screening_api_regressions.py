from __future__ import annotations

import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import resolve_rule_indicator_locks, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def _make_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def _make_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'One', 'SZSE', '2020-01-01', false, false)"""
    )
    duck.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm)
           VALUES ('000001', '2025-12-31', 10)"""
    )
    insert_minimum_screenable_data(duck)
    insert_matching_trading_calendar(duck, sqlite)


# ─── 3a: 草稿 revision 乐观锁原子条件更新 ──────────────────────────────────

def test_draft_stale_revision_conflicts(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    client = _make_client(duckdb_store, sqlite_store)
    draft = {"conditions": {"logic": "AND", "rules": []}}

    first = client.put("/api/screening/draft", json={"draft": draft, "revision": 0})
    assert first.status_code == 200
    assert first.json()["revision"] == 1

    stale = client.put("/api/screening/draft", json={"draft": draft, "revision": 0})
    assert stale.status_code == 409

    current = client.put(
        "/api/screening/draft",
        json={"draft": draft, "revision": first.json()["revision"]},
    )
    assert current.status_code == 200
    assert current.json()["revision"] == 2


def test_draft_concurrent_puts_with_same_revision_only_one_succeeds(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    client = _make_client(duckdb_store, sqlite_store)
    first = client.put(
        "/api/screening/draft",
        json={"draft": {"conditions": {"logic": "AND", "rules": []}}, "revision": 0},
    )
    assert first.status_code == 200
    revision = first.json()["revision"]

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def put_once() -> None:
        barrier.wait()
        response = client.put(
            "/api/screening/draft",
            json={"draft": {"conditions": {"logic": "OR", "rules": []}}, "revision": revision},
        )
        statuses.append(response.status_code)

    workers = [threading.Thread(target=put_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(statuses) == [200, 409]
    assert client.get("/api/screening/draft").json()["revision"] == revision + 1


# ─── 3b + 1d: /run 对未知 columns / min_listing_years / sort 返回 400 ───────

def _saved_empty_rule(client: TestClient) -> dict:
    response = client.post(
        "/api/screening/rules/save",
        json={"name": "api-regression", "rule_json": {"conditions": {"logic": "AND", "rules": []}}},
    )
    assert response.status_code == 200
    return response.json()


def test_run_unknown_request_columns_returns_400(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _saved_empty_rule(client)

    response = client.post(
        "/api/screening/run",
        json={
            "rule_id": rule["rule_id"],
            "rule_version": rule["version"],
            "min_listing_years": 0,
            "columns": ["stock_code", "does_not_exist"],
        },
    )

    assert response.status_code == 400
    assert "未知结果字段" in response.json()["detail"]
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM screening_runs")[0]["c"] == 0


def test_run_out_of_range_min_listing_years_returns_400(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _saved_empty_rule(client)

    for bad_years in (-1, 101):
        response = client.post(
            "/api/screening/run",
            json={
                "rule_id": rule["rule_id"],
                "rule_version": rule["version"],
                "min_listing_years": bad_years,
            },
        )

        assert response.status_code == 400
        assert "0-100" in response.json()["detail"]


def test_run_non_dict_sort_returns_400_not_500(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule_json = {"conditions": {"logic": "AND", "rules": []}, "sort": ["pe_ttm"]}
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO screening_rules (name, version, rule_json, locked_indicators, status)
               VALUES ('legacy-bad-sort', 1, ?, '{}', 'saved')""",
            [json.dumps(rule_json)],
        )

    response = client.post(
        "/api/screening/run",
        json={"rule_id": 1, "rule_version": 1, "min_listing_years": 0},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "排序项必须是对象"


# ─── 3c: watchlist executemany + 去重 ──────────────────────────────────────

def test_add_to_watchlist_deduplicates_requested_codes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = client.post(
        "/api/screening/rules/save",
        json={"name": "watchlist-value", "rule_json": {"conditions": {"logic": "AND", "rules": []}}},
    ).json()
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO screening_runs
               (run_id, rule_id, rule_version, result_json, columns_json, sort_json,
                data_date, base_pool_config, strict_only, confidence_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "watchlist-run", rule["rule_id"], rule["version"],
                '[{"stock_code":"000001","name":"One"}]', '["stock_code","name"]', '[]',
                "2025-12-31", '{}', False, '{}',
            ],
        )
    result_id = client.post(
        "/api/screening/save", json={"title": "watchlist result", "run_id": "watchlist-run"}
    ).json()["result_id"]

    response = client.post(
        "/api/screening/add_to_watchlist",
        json={"stock_codes": ["000001", "000001"], "group": "screening", "result_id": result_id},
    )

    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert sqlite_store.query(
        "SELECT stock_code, group_name FROM watchlist"
    ) == [{"stock_code": "000001", "group_name": "screening"}]


# ─── 3d: 规则锁只认最新 published 版本 ─────────────────────────────────────

def test_resolve_rule_indicator_locks_uses_max_published_version(
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as conn:
        conn.executemany(
            """INSERT INTO dsl_expressions
               (name, version, expression_text, status, content_hash)
               VALUES (?, ?, 'pe_ttm', ?, ?)""",
            [
                ("locked_ind", 1, "published", "hash-v1"),
                ("locked_ind", 2, "published", "hash-v2"),
                ("locked_ind", 3, "draft", "hash-v3"),
            ],
        )

    locks = resolve_rule_indicator_locks(
        sqlite_store,
        {"conditions": {"logic": "AND", "rules": [{"field": "locked_ind", "op": ">", "value": 0}]}},
        {},
    )

    assert locks == {"locked_ind": {"version": 2, "content_hash": "hash-v2"}}


# ─── 3e + 1h: /indicators DSL 单位语义与 sw1 不暴露 ────────────────────────

def test_indicators_derive_dsl_units_and_explain_plain_semantics(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as conn:
        conn.executemany(
            """INSERT INTO dsl_expressions
               (name, version, expression_text, ast_json, status, content_hash)
               VALUES (?, 1, ?, ?, 'published', ?)""",
            [
                ("asset_ratio", "balance.total_liabilities / balance.total_assets",
                 json.dumps({"unit": "ratio"}), "ratio-hash"),
                ("pct_ind", "balance.capital_adequacy_ratio",
                 json.dumps({"unit": "percent"}), "percent-hash"),
            ],
        )
    client = _make_client(duckdb_store, sqlite_store)

    body = client.get("/api/screening/indicators").json()

    by_name = {item["name"]: item for item in body["indicators"]}
    assert by_name["asset_ratio"]["dsl"] is True
    assert by_name["asset_ratio"]["unit"] == "ratio"
    assert by_name["pct_ind"]["unit"] == "percent"
    assert "不进行百分比换算" in body["unit_semantics"]["plain"]
    # sw1 后缀不暴露（历史别名只保留在引擎侧兼容旧规则）；sw2 仍暴露。
    assert not any(name.endswith("_sw1_rank") or name.endswith("_sw1_percentile") for name in by_name)
    assert any(name.endswith("_sw2_rank") for name in by_name)
