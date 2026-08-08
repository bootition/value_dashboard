"""数据状态 summary 写锁降级缓存契约。"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

import app.web.api.data_status as data_status
import app.web.main as web_main
from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema


def _reset_cache(monkeypatch) -> None:
    monkeypatch.setattr(data_status, "_SUMMARY_CACHE", {})


def _make_app(database_paths: DatabasePathSet):
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=duck,
        sqlite=sqlite,
    )
    return TestClient(app), duck


def test_summary_succeeds_without_lock_and_populates_cache(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, _ = _make_app(database_paths)

    first = client.get("/api/data-status/summary")
    assert first.status_code == 200
    assert first.json().get("stale") is not True
    assert data_status._SUMMARY_CACHE


def test_summary_serves_cached_snapshot_while_update_lock_active(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    try:
        # 无缓存时（首次）仍允许构建一次
        first = client.get("/api/data-status/summary")
        assert first.status_code == 200
        # 写锁仍在：后续请求必须命中缓存并标注 stale，绝不再触碰 DuckDB 写锁
        second = client.get("/api/data-status/summary")
        assert second.status_code == 200
        body = second.json()
        assert body.get("stale") is True
        assert body.get("stale_reason") == "auto_update_active"
    finally:
        lock_path.unlink(missing_ok=True)


def test_summary_reuses_cache_until_ttl_expires(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, _ = _make_app(database_paths)

    client.get("/api/data-status/summary")
    assert data_status._cached_summary(client.app) is not None
    # TTL 内直接复用（无锁也命中缓存）
    again = client.get("/api/data-status/summary")
    assert again.status_code == 200
    assert again.json().get("stale") is not True

    # TTL 过期后重新计算
    monkeypatch.setattr(data_status, "_SUMMARY_CACHE", {})
    assert data_status._cached_summary(client.app) is None


def test_summary_uses_expired_cache_while_live_update_lock_exists(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    assert client.get("/api/data-status/summary").status_code == 200
    key = next(iter(data_status._SUMMARY_CACHE))
    data_status._SUMMARY_CACHE[key]["at"] = 0.0
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    monkeypatch.setattr(
        data_status,
        "_build_summary_fresh",
        lambda request: (_ for _ in ()).throw(AssertionError("must not rebuild")),
    )
    try:
        response = client.get("/api/data-status/summary")
    finally:
        lock_path.unlink(missing_ok=True)
    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_dead_update_lock_does_not_mark_summary_stale(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text("pid=99999999\ntime=0\n", encoding="ascii")
    try:
        response = client.get("/api/data-status/summary")
    finally:
        lock_path.unlink(missing_ok=True)
    assert response.status_code == 200
    assert response.json().get("stale") is not True
