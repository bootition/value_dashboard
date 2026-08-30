"""方案 A 门禁直接测试（红队 80 P2-8）+ 写窗口判定统一（P2-1）回归。

方案 A（用户决策，reports/79）：写锁活跃时筛选不再 409 禁用，而以快照
口径运行并把 auto_update_in_progress/data_as_of 随结果持久化；快照完全
缺失仍 409 兜底。P2-1：update 锁与 DuckDB 写锁任一活跃即判定写窗口。
"""

from __future__ import annotations

import os
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.update_lock import _pid_creation_time
from app.web.api.screening import _require_current_screenability, router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


class _FakeRequest:
    def __init__(self, duck: DuckDBStore, sqlite: SQLiteStore) -> None:
        state = types.SimpleNamespace()
        state.duck = duck
        state.sqlite = sqlite
        state.startup_readiness = {}
        self.app = types.SimpleNamespace(state=state)


def _live_lock_content() -> str:
    created = _pid_creation_time(os.getpid()) or 0.0
    return f"pid={os.getpid()}\ncreated={created}\ntime=0\n"


def _write_live_lock(path) -> None:
    path.write_text(_live_lock_content(), encoding="ascii")


def _seed_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duck)
    insert_matching_trading_calendar(duck, sqlite)


def _snapshot_as_of(duck: DuckDBStore) -> str | None:
    rows = duck.read_query("SELECT MAX(latest_price_date) AS d FROM indicator_snapshot")
    return str(rows[0]["d"])[:10] if rows and rows[0].get("d") is not None else None


def _make_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def _save_rule(client: TestClient) -> dict:
    response = client.post(
        "/api/screening/rules/save",
        json={
            "name": "gate-value",
            "rule_json": {
                "conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
                "columns": ["stock_code", "name", "pe_ttm"],
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def test_gate_no_lock_returns_pass_through(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    request = _FakeRequest(duckdb_store, sqlite_store)

    gate = _require_current_screenability(request)

    assert gate == {"lock_active": False, "data_as_of": None}


def test_gate_update_lock_returns_snapshot_data_as_of(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    _write_live_lock(lock_path)
    try:
        gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))
    finally:
        lock_path.unlink(missing_ok=True)

    assert gate["lock_active"] is True
    assert gate["data_as_of"] == _snapshot_as_of(duckdb_store)
    assert gate["data_as_of"] is not None


def test_gate_duckdb_write_lock_also_marks_lock_active(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    lock_path = duckdb_store.db_path.parent / ".duckdb.write.lock"
    _write_live_lock(lock_path)
    try:
        gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))
    finally:
        lock_path.unlink(missing_ok=True)

    assert gate["lock_active"] is True


def test_gate_lock_active_with_empty_snapshot_409(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    duckdb_store.write_query("DELETE FROM indicator_snapshot")
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    _write_live_lock(lock_path)
    try:
        with pytest.raises(HTTPException) as exc:
            _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))
    finally:
        lock_path.unlink(missing_ok=True)

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "minimum_data_not_ready"


def test_run_under_update_lock_persists_auto_update_markers(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _save_rule(client)
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    _write_live_lock(lock_path)
    try:
        response = client.post(
            "/api/screening/run",
            json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
        )
    finally:
        lock_path.unlink(missing_ok=True)

    assert response.status_code == 200
    body = response.json()
    assert body["auto_update_in_progress"] is True
    assert body["data_as_of"] == _snapshot_as_of(duckdb_store)
    persisted = sqlite_store.query(
        "SELECT confidence_summary FROM screening_runs WHERE run_id = ?",
        [body["run_id"]],
    )[0]
    summary = __import__("json").loads(persisted["confidence_summary"])
    assert summary["auto_update_in_progress"] is True
    assert summary["data_as_of"] == body["data_as_of"]


def test_run_under_duckdb_write_lock_marks_auto_update(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _save_rule(client)
    lock_path = duckdb_store.db_path.parent / ".duckdb.write.lock"
    _write_live_lock(lock_path)
    try:
        response = client.post(
            "/api/screening/run",
            json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
        )
    finally:
        lock_path.unlink(missing_ok=True)

    assert response.status_code == 200
    assert response.json()["auto_update_in_progress"] is True


def test_gate_uses_fresh_persisted_readiness_cache_with_gate_shape(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    from app.core.data_quality import (
        screening_readiness_cache_key,
        store_screening_readiness_cache,
    )

    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert fingerprint is not None
    store_screening_readiness_cache(sqlite_store, fingerprint, {
        "ready": True,
        "readiness": {"ready": True},
        "warning_codes": [],
    })

    gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    # 缓存的是 screening_readiness 决策；请求链需要 gate 形状。
    assert gate == {"lock_active": False, "data_as_of": None}


def test_gate_raises_409_from_fresh_persisted_not_ready_cache(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    from app.core.data_quality import (
        screening_readiness_cache_key,
        store_screening_readiness_cache,
    )

    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert fingerprint is not None
    store_screening_readiness_cache(sqlite_store, fingerprint, {
        "ready": False,
        "readiness": {"ready": False},
        "warning_codes": ["LINEAGE_INVALID"],
    })

    with pytest.raises(HTTPException) as exc:
        _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "minimum_data_not_ready"


def test_warm_screening_readiness_cache_round_trip(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    from app.core.data_quality import (
        load_screening_readiness_cache,
        screening_readiness_cache_key,
        warm_screening_readiness_cache,
    )

    decision = warm_screening_readiness_cache(duckdb_store, sqlite_store)
    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)

    assert decision is not None
    assert decision["ready"] is True
    assert fingerprint is not None
    cached = load_screening_readiness_cache(sqlite_store, fingerprint)
    assert cached is not None
    assert cached["ready"] is True
    assert cached["warning_codes"] == []


def test_screening_readiness_cache_stale_while_revalidate_read(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    import json as _json

    from app.core.data_quality import (
        SCREENING_READINESS_CACHE_KEY,
        load_screening_readiness_cache,
        screening_readiness_cache_key,
    )

    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store) or "test-fp"
    decision = {"ready": True, "readiness": {"ready": True}, "warning_codes": []}
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                         updated_at=excluded.updated_at""",
        [SCREENING_READINESS_CACHE_KEY, _json.dumps({
            "fingerprint": fingerprint,
            "decision": decision,
            "updated_at": "2000-01-01T00:00:00+00:00",
        }), "2000-01-01T00:00:00+00:00"],
    )

    assert load_screening_readiness_cache(sqlite_store, fingerprint) is None
    assert load_screening_readiness_cache(
        sqlite_store, fingerprint, allow_stale=True,
    ) == decision
