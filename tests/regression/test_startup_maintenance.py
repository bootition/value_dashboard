"""Startup maintenance runs at most once and exposes its lifecycle state."""

from __future__ import annotations

import threading
import time

import app.web.main as web_main
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore


def _build_app(database_paths: DatabasePathSet) -> FastAPI:
    return web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=DuckDBStore(paths=database_paths),
        sqlite=SQLiteStore(paths=database_paths),
    )


def _wait_for_terminal_status(app: FastAPI, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = app.state.startup_maintenance
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("startup maintenance did not reach a terminal status")


def test_maintenance_status_is_idle_before_start_and_exposed(
    database_paths: DatabasePathSet,
) -> None:
    app = _build_app(database_paths)
    client = TestClient(app)

    assert app.state.startup_maintenance == {"status": "idle", "error": None}
    assert client.get("/api/maintenance/status").json() == {"status": "idle", "error": None}


def test_startup_maintenance_completes_and_reports_done(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    app = _build_app(database_paths)
    readiness = {"ready": True, "stock_count": 1, "missing": {}, "missing_counts": {}}
    monkeypatch.setattr(
        web_main, "_run_startup_maintenance", lambda duck, sqlite, current: readiness
    )

    web_main._start_startup_maintenance(app, app.state.duck, app.state.sqlite, readiness)

    assert _wait_for_terminal_status(app) == {"status": "done", "error": None}
    assert app.state.startup_readiness is readiness
    assert TestClient(app).get("/api/maintenance/status").json() == {
        "status": "done", "error": None,
    }


def test_startup_maintenance_does_not_start_twice(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    app = _build_app(database_paths)
    entered = threading.Event()
    release = threading.Event()
    calls: list[None] = []

    def blocked(duck, sqlite, readiness) -> dict:
        calls.append(None)
        entered.set()
        release.wait(timeout=5)
        return {"ready": True, "stock_count": 1, "missing": {}, "missing_counts": {}}

    monkeypatch.setattr(web_main, "_run_startup_maintenance", blocked)

    web_main._start_startup_maintenance(app, app.state.duck, app.state.sqlite, {})
    assert entered.wait(timeout=5)
    assert app.state.startup_maintenance["status"] == "running"

    web_main._start_startup_maintenance(app, app.state.duck, app.state.sqlite, {})
    release.set()

    assert _wait_for_terminal_status(app) == {"status": "done", "error": None}
    assert len(calls) == 1


def test_startup_maintenance_reports_worker_failure(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    app = _build_app(database_paths)

    def explode(duck, sqlite, readiness) -> dict:
        raise RuntimeError("network down")

    monkeypatch.setattr(web_main, "_run_startup_maintenance", explode)

    web_main._start_startup_maintenance(app, app.state.duck, app.state.sqlite, {})

    state = _wait_for_terminal_status(app)
    assert state["status"] == "failed"
    assert "network down" in state["error"]


def test_startup_maintenance_marks_failed_initialization(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    app = _build_app(database_paths)

    class FailingInitializer:
        def __init__(self, **kwargs) -> None:
            pass

        def run_full_init(self) -> dict:
            raise RuntimeError("init boom")

    class NoopUpdater:
        def __init__(self, **kwargs) -> None:
            pass

        def run_incremental_check(self, **kwargs) -> dict:
            return {"needs_update": False}

    monkeypatch.setattr("app.core.init.DataInitializer", FailingInitializer)
    monkeypatch.setattr("app.core.update.IncrementalUpdater", NoopUpdater)
    startup_readiness = {"ready": False, "stock_count": 0, "missing": {}, "missing_counts": {}}

    web_main._start_startup_maintenance(app, app.state.duck, app.state.sqlite, startup_readiness)

    state = _wait_for_terminal_status(app)
    assert state["status"] == "failed"
    assert "init boom" in state["error"]
    assert app.state.startup_readiness["ready"] is False
