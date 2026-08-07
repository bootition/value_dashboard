"""自动更新进度契约：逐股 detail_cb → live/log 持久化 → 只读展示。"""

from __future__ import annotations

from typing import Any

from app.core.auto_update import AutoUpdateController
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater


def test_run_incremental_update_accepts_detail_cb() -> None:
    import inspect

    signature = inspect.signature(IncrementalUpdater.run_incremental_update)
    assert "detail_cb" in signature.parameters
    assert signature.parameters["detail_cb"].default is None


class FakeUpdater:
    def __init__(self, *, duck: DuckDBStore, sqlite: SQLiteStore) -> None:
        self.duck = duck
        self.sqlite = sqlite

    def run_incremental_update(
        self,
        max_stocks: int = 0,
        *,
        progress_cb=None,
        detail_cb=None,
    ) -> dict[str, Any]:
        progress_cb("trading_dates", {"status": "success", "success": 1, "failed": 0})
        progress_cb("prices", {"status": "success", "success": 2, "failed": 0})
        for done in (1, 2):
            detail_cb("price", {
                "done": done, "total": 2, "current": "600519",
                "label": "股票价格",
            })
        return {"status": "success", "steps": {
            "trading_dates": {"status": "success"},
            "prices": {"status": "success"},
        }}


def test_auto_update_progress_logs_and_live_are_persisted_and_readable(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.core.update.IncrementalUpdater",
        FakeUpdater,
    )
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)

    report = controller.run_once()

    assert report["status"] == "success"
    persisted = controller.persisted_status()
    assert persisted["current_stage"] == "finished"
    assert persisted["progress"]["phase"] == "done"
    log = persisted["progress"].get("log", [])
    assert any("股票价格 进行中" in entry["msg"] for entry in log)
    assert any("股票价格 完成" in entry["msg"] for entry in log)
    # live 是运行中的瞬时快照，终态必须清除
    assert "live" not in persisted["progress"]
    assert len(log) <= 20


def test_auto_update_live_snapshot_is_visible_while_running(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch,
) -> None:
    holder: dict[str, Any] = {}

    class PausingUpdater:
        def __init__(self, *, duck: DuckDBStore, sqlite: SQLiteStore) -> None:
            self.duck = duck
            self.sqlite = sqlite

        def run_incremental_update(self, max_stocks: int = 0, *, progress_cb=None, detail_cb=None):
            detail_cb("price", {
                "done": 3, "total": 10, "current": "000001", "label": "股票价格",
            })
            holder["live"] = holder["controller"].status()["progress"].get("live")
            return {"status": "success", "steps": {}}

    monkeypatch.setattr("app.core.update.IncrementalUpdater", PausingUpdater)
    controller = AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    holder["controller"] = controller
    controller.run_once()

    assert holder["live"]["done"] == 3
    assert holder["live"]["total"] == 10
    assert holder["live"]["current"] == "000001"
    assert holder["live"]["rate_per_minute"] > 0
    assert holder["live"]["eta_seconds"] is not None


def test_step_labels_cover_update_steps() -> None:
    from app.core.auto_update import STEP_LABELS

    for step in ("check", "trading_dates", "prices", "indicators", "universe", "financials"):
        assert STEP_LABELS[step]
