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
from tests.support.seed import Seed


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
    Seed(duck).stock(
        "000001",
        name="Test",
        exchange="SZSE",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
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

    # 缺陷 B（2026-11-18）：门禁返回体新增了就绪决策的新鲜度字段
    # (readiness_source/cached_at/stale/age_seconds)，调用方才分得清"刚算的
    # 就绪"和"过期决策"。故这里只断言请求链依赖的两个键 + 新鲜度语义，
    # 不再做整字典相等断言（那不是契约，加兼容字段不该让它变红）。
    assert gate["lock_active"] is False
    assert gate["data_as_of"] is None
    assert gate["readiness_source"] in {"cache", "stale_cache", "computed"}
    assert gate["readiness_stale"] is False


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
    assert gate["lock_active"] is False
    assert gate["data_as_of"] is None
    # 缺陷 B：命中未过期缓存时必须能看出"这条决策是什么时候算的"。
    assert gate["readiness_source"] == "cache"
    assert gate["readiness_stale"] is False
    assert gate["readiness_cached_at"] is not None


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
    """TTL 过期后的 stale 读取：**硬上限内允许，超过即拒绝**。

    ⚠️ 本用例原先断言"updated_at=2000-01-01 的决策在 allow_stale=True 时照样
    返回"，等于把这个真缺陷写成了期望行为（缺陷 B，2026-11-18 实测确认）：
    TTL 沦为"刷新触发器"而不是正确性边界，叠加后台 worker 遇写锁直接 return、
    异常只 logger.warning 不落盘，过期决策可被无限期当作"已就绪"返回。
    修复后 stale 有硬上限 ``SCREENING_READINESS_STALE_MAX_AGE_SECONDS``（2×TTL），
    因此这里改为分别断言"硬上限内允许 stale / 超过硬上限必须拒绝"，并顺带
    锁定 ``load_screening_readiness_entry`` 暴露的 ``stale``/``cached_at``。
    行为变更的完整背景见 ``test_screening_cache_freshness.py`` 的模块 docstring。
    """
    import json as _json
    from datetime import UTC, datetime, timedelta

    from app.core.data_quality import (
        SCREENING_READINESS_CACHE_KEY,
        SCREENING_READINESS_CACHE_TTL_SECONDS,
        SCREENING_READINESS_STALE_MAX_AGE_SECONDS,
        load_screening_readiness_cache,
        load_screening_readiness_entry,
        screening_readiness_cache_key,
    )

    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store) or "test-fp"
    decision = {"ready": True, "readiness": {"ready": True}, "warning_codes": []}

    def _store(age_seconds: int) -> None:
        updated = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        # 留（第 6 类·库侧相对时间 / 论点构造）：`data_refresh_state.updated_at` 必须是
        sqlite_store.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                             updated_at=excluded.updated_at""",
            [SCREENING_READINESS_CACHE_KEY, _json.dumps({
                "fingerprint": fingerprint,
                "decision": decision,
                "updated_at": updated,
            }), updated],
        )

    # 1) 过期但在硬上限内：禁止 fresh 读取，允许 stale 读取（保持原设计的
    #    stale-while-revalidate），并且必须能自证"这是旧的"。
    _store(SCREENING_READINESS_CACHE_TTL_SECONDS + 60)
    assert load_screening_readiness_cache(sqlite_store, fingerprint) is None
    assert load_screening_readiness_cache(
        sqlite_store, fingerprint, allow_stale=True,
    ) == decision
    entry = load_screening_readiness_entry(sqlite_store, fingerprint, allow_stale=True)
    assert entry is not None
    assert entry["decision"] == decision
    assert entry["stale"] is True
    assert entry["cached_at"] is not None

    # 2) 超过硬上限：fresh 与 stale 都必须拒绝（改为同步重算/不可用）。
    _store(SCREENING_READINESS_STALE_MAX_AGE_SECONDS + 60)
    assert load_screening_readiness_cache(sqlite_store, fingerprint) is None
    assert load_screening_readiness_cache(
        sqlite_store, fingerprint, allow_stale=True,
    ) is None
    assert load_screening_readiness_entry(
        sqlite_store, fingerprint, allow_stale=True,
    ) is None
