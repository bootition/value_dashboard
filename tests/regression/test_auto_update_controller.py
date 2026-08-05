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

        def run_incremental_update(self, max_stocks: int = 0, progress_cb=None) -> dict:
            if progress_cb is not None:
                progress_cb("prices", {"status": "success"})
            return {"status": "success", "steps": {"prices": {"status": "success"}}}

    monkeypatch.setattr("app.core.update.IncrementalUpdater", FakeUpdater)

    report = controller.run_once()

    assert report["status"] == "success"
    status = controller.status()
    assert status["current_stage"] == "finished"
    assert status["last_success_at"] is not None
    assert status["progress"]["status"] == "success"


def test_auto_update_persists_per_step_progress(duckdb_store, sqlite_store, monkeypatch) -> None:
    """P1: 网络抓取阶段按步骤持久化进度（job_id/started_at/steps）。"""
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    class StepwiseUpdater:
        def __init__(self, **kwargs) -> None:
            pass

        def run_incremental_update(self, max_stocks: int = 0, progress_cb=None) -> dict:
            assert progress_cb is not None
            progress_cb("universe", {"status": "success"})
            progress_cb("prices", {"status": "partial"})
            return {"status": "partial", "steps": {"universe": {"status": "success"}, "prices": {"status": "partial"}}}

    monkeypatch.setattr("app.core.update.IncrementalUpdater", StepwiseUpdater)

    report = controller.run_once()
    assert report["status"] == "partial"

    # 每步回调后持久化：读 persisted 应含 step:prices 进度与 job_id
    persisted = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store).persisted_status()
    progress = persisted["progress"]
    assert progress.get("job_id")
    assert progress.get("started_at")
    assert progress.get("steps") == {"universe": "success", "prices": "partial"}
    assert progress.get("phase") in {"done", "step:prices"}


def test_auto_update_run_once_failure_records_error(duckdb_store, sqlite_store, monkeypatch) -> None:
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    class FailingUpdater:
        def __init__(self, **kwargs) -> None:
            pass

        def run_incremental_update(self, max_stocks: int = 0, progress_cb=None) -> dict:
            return {"status": "partial", "steps": {"prices": {"status": "failed"}}}

    monkeypatch.setattr("app.core.update.IncrementalUpdater", FailingUpdater)

    report = controller.run_once()

    assert report["status"] == "partial"
    status = controller.status()
    assert status["current_stage"] == "failed"
    assert status["last_error"] is not None


def test_auto_update_skipped_reports_idle_not_failed(duckdb_store, sqlite_store, monkeypatch) -> None:
    """C9(报告41): 更新被跳过（如跨进程锁被拒/another_update_running）时，
    控制器状态必须保持 idle 且不产生 last_error，不得误记 failed。"""
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    class LockedUpdater:
        def __init__(self, **kwargs) -> None:
            pass

        def run_incremental_update(self, max_stocks: int = 0, progress_cb=None) -> dict:
            return {"status": "skipped", "reason": "another_update_running"}

    monkeypatch.setattr("app.core.update.IncrementalUpdater", LockedUpdater)

    report = controller.run_once()

    assert report["status"] == "skipped"
    status = controller.status()
    assert status["current_stage"] == "idle"
    assert status["last_error"] is None
    assert status["last_result"] == "skipped"
    assert status["last_skip_reason"] == "another_update_running"
    persisted = controller.persisted_status()
    assert persisted["last_skip_reason"] == "another_update_running"


def test_new_controller_adopts_persisted_disabled_state(duckdb_store, sqlite_store) -> None:
    """P0-3: 用户 disable 后，任何新控制器（CLI/Web 各自实例）不得复活自动更新。"""
    first = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    first.disable()

    second = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    assert second.status()["enabled"] is False
    assert second.status()["state"] == "disabled"
    assert second.persisted_status()["enabled"] is False
    assert second.run_once()["status"] == "skipped"
    assert second.run_once()["reason"] == "auto_update_disabled"


def test_new_controller_adopts_persisted_paused_state(duckdb_store, sqlite_store) -> None:
    first = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    first.pause()

    second = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    assert second.status()["paused"] is True
    assert second.run_once()["status"] == "skipped"
    assert second.run_once()["reason"] == "auto_update_paused"


def test_enable_after_restart_reactivates(duckdb_store, sqlite_store) -> None:
    first = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    first.disable()

    second = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    second.enable()

    assert second.status()["enabled"] is True
    persisted = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    assert persisted.status()["enabled"] is True


def test_crashed_running_marker_is_reset_to_idle(duckdb_store, sqlite_store) -> None:
    """崩溃遗留的 running 标记不得让新控制器永久拒绝 run_once。"""
    first = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    first._current_stage = "running"
    first._persist()

    second = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    assert second.status()["current_stage"] == "idle"
