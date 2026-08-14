"""数据状态 summary stale-while-revalidate 缓存契约。

构建（build_data_quality_status 在正式库需 20s+）绝不能阻塞请求线程；
任何情况下立即返回最近缓存（含过期值）或轻量占位，刷新由后台线程完成。
"""

from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

import app.web.api.data_status as data_status
import app.web.main as web_main
from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore


def _reset_cache(monkeypatch) -> None:
    monkeypatch.setattr(data_status, "_SUMMARY_CACHE", {})
    monkeypatch.setattr(data_status, "_SUMMARY_REFRESHING", set())


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


def _wait_for_real_summary(client, timeout: float = 5.0) -> dict | None:
    """Wait until the background refresh publishes a non-placeholder summary."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/data-status/summary")
        body = response.json()
        if not body.get("checking"):
            return body
        time.sleep(0.05)
    return None


def _unlink_with_retry(lock_path, attempts: int = 40, delay: float = 0.05) -> None:
    """Unlink a lock file tolerating transient Windows sharing races.

    后台 summary 刷新线程可能在 finally 的 unlink 瞬间正在读取锁文件
    （_update_write_lock_active → read_text），Windows 上偶发
    PermissionError(WinError 32)（reports/79 构建门禁两度撞见）。重试
    2 秒覆盖线程完成读写的窗口。
    """
    for _ in range(attempts):
        try:
            lock_path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(delay)
    lock_path.unlink(missing_ok=True)


def test_summary_first_request_returns_placeholder_and_background_refreshes(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    """无缓存首次请求：立即返回占位（不阻塞），后台构建完成后缓存为真值。"""
    _reset_cache(monkeypatch)
    client, _ = _make_app(database_paths)

    started = time.monotonic()
    first = client.get("/api/data-status/summary")
    elapsed = time.monotonic() - started
    assert first.status_code == 200
    assert first.json().get("checking") is True
    assert elapsed < 2.0, "首次请求必须立即返回，不得同步构建"

    refreshed = _wait_for_real_summary(client)
    assert refreshed is not None, "后台刷新应发布真实 summary"
    assert refreshed.get("checking") is not True
    assert data_status._SUMMARY_CACHE


def test_summary_serves_stale_while_update_lock_active(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    try:
        first = client.get("/api/data-status/summary")
        assert first.status_code == 200
        second = client.get("/api/data-status/summary")
        assert second.status_code == 200
        assert second.json().get("stale") is True
    finally:
        _unlink_with_retry(lock_path)


def test_summary_reuses_cache_until_ttl_expires(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, _ = _make_app(database_paths)

    client.get("/api/data-status/summary")
    cached, _ = data_status._cached_summary(client.app, allow_stale=True)
    assert cached is not None
    again = client.get("/api/data-status/summary")
    assert again.status_code == 200
    assert again.json().get("checking") is True

    refreshed = _wait_for_real_summary(client)
    assert refreshed is not None


def test_summary_expired_cache_served_while_lock_active_without_rebuild(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    first = client.get("/api/data-status/summary")
    assert first.json().get("checking") is True
    key = next(iter(data_status._SUMMARY_CACHE))
    data_status._SUMMARY_CACHE[key]["at"] = 0.0
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    built = {"n": 0}

    def fake_build(state) -> dict:
        built["n"] += 1
        return {"data_quality": {"minimum_data_readiness": {"ready": True}, "warning_codes": []}}

    monkeypatch.setattr(data_status, "_build_summary_from_state", fake_build)
    try:
        response = client.get("/api/data-status/summary")
    finally:
        _unlink_with_retry(lock_path)
    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert built["n"] <= 1, "后台刷新 single-flight，最多构建一次"


def test_dead_update_lock_does_not_mark_summary_stale(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text("pid=99999999\ntime=0\n", encoding="ascii")
    try:
        first = client.get("/api/data-status/summary")
        assert first.status_code == 200
    finally:
        _unlink_with_retry(lock_path)
    # 死锁不视为写锁：后台刷新完成后返回真实 summary，不标 stale
    refreshed = _wait_for_real_summary(client)
    assert refreshed is not None
    assert refreshed.get("stale") is not True


def test_live_update_lock_without_cache_returns_placeholder_not_full_build(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    """写锁活跃且进程内无缓存（服务刚重启）时不得同步全量构建（曾致前端 15s 超时）。"""
    _reset_cache(monkeypatch)
    client, duck = _make_app(database_paths)
    lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    built = {"n": 0}

    def fake_build(state) -> dict:
        built["n"] += 1
        return {"data_quality": {"minimum_data_readiness": {"ready": True}, "warning_codes": []}}

    monkeypatch.setattr(data_status, "_build_summary_from_state", fake_build)
    try:
        response = client.get("/api/data-status/summary")
    finally:
        _unlink_with_retry(lock_path)
    assert response.status_code == 200
    body = response.json()
    assert body.get("stale") is True
    assert body.get("checking") is True


def test_background_refresh_single_flight(
    database_paths: DatabasePathSet, monkeypatch,
) -> None:
    """并发请求只触发一次后台构建。"""
    _reset_cache(monkeypatch)
    client, _ = _make_app(database_paths)
    built = {"n": 0}

    def fake_build(state) -> dict:
        built["n"] += 1
        time.sleep(0.1)
        return {"data_quality": {"minimum_data_readiness": {"ready": True}, "warning_codes": []}}

    monkeypatch.setattr(data_status, "_build_summary_from_state", fake_build)
    for _ in range(5):
        client.get("/api/data-status/summary")
    time.sleep(0.5)
    assert built["n"] <= 1, "single-flight：多个并发请求只允许一次后台构建"
