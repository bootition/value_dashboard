"""自动更新控制器测试（PRD §7.3, §16.1）"""

from __future__ import annotations

from app.core.auto_update import AutoUpdateController


def test_auto_update_controller_defaults_to_enabled(duckdb_store, sqlite_store) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    status = controller.status()

    assert status["enabled"] is True
    assert status["paused"] is False
    assert status["current_stage"] in {"idle", "finished", "failed"}


def test_auto_update_controller_disable_and_enable(duckdb_store, sqlite_store) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    disabled = controller.disable()
    assert disabled["enabled"] is False

    enabled = controller.enable()
    assert enabled["enabled"] is True


def test_auto_update_controller_pause_and_resume(duckdb_store, sqlite_store) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    paused = controller.pause()
    assert paused["paused"] is True

    resumed = controller.resume()
    assert resumed["paused"] is False


def test_auto_update_status_persists_to_sqlite(duckdb_store, sqlite_store) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    controller.pause()

    persisted = controller.persisted_status()

    assert persisted["paused"] is True
    assert persisted["state"] == "paused"


def test_auto_update_run_once_disabled_skips(duckdb_store, sqlite_store) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    controller.disable()

    report = controller.run_once()

    assert report["status"] == "skipped"
    assert report["reason"] == "auto_update_disabled"


def test_auto_update_run_once_records_result(duckdb_store, sqlite_store, monkeypatch) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    class FakeUpdater:
        def __init__(self, **kwargs) -> None:
            pass

        def run_incremental_update(self, max_stocks: int = 0) -> dict:
            return {"status": "success", "steps": {"prices": {"status": "success"}}}

    monkeypatch.setattr("app.core.auto_update.IncrementalUpdater", FakeUpdater)

    report = controller.run_once()

    assert report["status"] == "success"
    status = controller.status()
    assert status["current_stage"] == "finished"
    assert status["last_success_at"] is not None
    assert status["progress"]["status"] == "success"


def test_auto_update_run_once_failure_records_error(duckdb_store, sqlite_store, monkeypatch) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    class FailingUpdater:
        def __init__(self, **kwargs) -> None:
            pass

        def run_incremental_update(self, max_stocks: int = 0) -> dict:
            return {"status": "partial", "steps": {"prices": {"status": "failed"}}}

    monkeypatch.setattr("app.core.auto_update.IncrementalUpdater", FailingUpdater)

    report = controller.run_once()

    assert report["status"] == "partial"
    status = controller.status()
    assert status["current_stage"] == "failed"
    assert status["last_error"] is not None
