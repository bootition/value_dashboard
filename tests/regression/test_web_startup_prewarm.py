"""启动预热筛选体检（2026-11-21，用户点名的第 2 件事）回归。

事故形态（用户实测）：服务重启后**第一次**点「运行筛选」墙钟 58.9 秒
（引擎只占 734ms），第二三次 1.46–1.6 秒。58 秒全在请求线程里冷算
「全库数据体检」——`_require_current_screenability` → `screening_readiness`
→ `build_data_quality_status`（副本实测分段：minimum_data_readiness 8.0s /
_archive_hash_mismatch_rows 7.9s / _missing_lineage_coverage 6.5s /
_asset_health_summary 3.8s / 其余扫描 ~4s，合计 31.4s；再叠上
`read_warning_codes()` 后台重建线程并发重算同一份 → 40.5s 墙钟）。

启动维护里本来就有一次「后台数据就绪核对」，但它算的是
`minimum_data_readiness()` 这个**子集**、存自己的键；筛选门禁要的是
`screening_readiness()` 的完整决策（带指纹）。旧代码确实也预热了筛选决策，
却排在「启动自动更新子进程」之后（可能几小时后）→ 对第一次筛选无效。

本文件锁死修复后的四条行为：
  ① 启动维护**先**预热（且在自动更新之前），并把同一份计算复用为启动核对
    结果——同一份全库体检只算一遍（不是两遍/三遍）；
  ② 预热失败（模拟 `DuckDBReadLockedError`）不影响启动，退回同步核对；
  ③ 预热成功后**第一次筛选命中缓存**（`readiness_source == "cache"`）；
  ④ 数据变了（`content_epoch` +1 → 指纹变）后必须重算，不得复用旧决策；
  ⑤ 预热**还在跑**时点筛选 → 等它算完再命中缓存（不重复冷算），预热失败则冷算。
"""

from __future__ import annotations

import types

import pytest

import app.core.data_quality as dq
import app.web.main as web_main
from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBReadLockedError, DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import _require_current_screenability
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed

_IDLE_PREWARM = {
    "status": "idle",
    "reason": None,
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "cache_source": None,
    "decision_ready": None,
    "blocking_warnings": None,
    "fingerprint": None,
    "error": None,
    "attempts": 0,
}


class _FakeRequest:
    def __init__(self, duck: DuckDBStore, sqlite: SQLiteStore) -> None:
        state = types.SimpleNamespace(duck=duck, sqlite=sqlite, startup_readiness={})
        self.app = types.SimpleNamespace(state=state)


@pytest.fixture(autouse=True)
def _reset_prewarm_state():
    """模块级预热状态在测试间复位（否则上一条的 done/cache_hit 会串味）。"""
    with dq._screening_prewarm_lock:
        dq._screening_prewarm_state.clear()
        dq._screening_prewarm_state.update(_IDLE_PREWARM)
    yield
    with dq._screening_prewarm_lock:
        dq._screening_prewarm_state.clear()
        dq._screening_prewarm_state.update(_IDLE_PREWARM)


@pytest.fixture
def _quiet_startup(monkeypatch):
    """把启动维护里与本次修复无关的慢/联网副作用换成 no-op。

    ETF/指数只读预热、收盘后调度器、自动更新控制器、运维 GC 都不是本用例的
    被测对象；留着它们只会把"启动维护"变成联网 + 起线程的不确定测试。
    """

    class _DisabledAutoUpdate:
        def __init__(self, **_kwargs) -> None:
            pass

        def status(self) -> dict:
            return {"enabled": False, "paused": False}

    monkeypatch.setattr(web_main, "_start_auto_update_scheduler", lambda *a, **k: None)
    monkeypatch.setattr("app.core.auto_update.AutoUpdateController", _DisabledAutoUpdate)
    monkeypatch.setattr(
        "app.web.api.etf_strategy.warm_etf_read_cache", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.web.api.index_dashboard.warm_index_read_cache", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.core.housekeeping.gc_operational_tables", lambda *a, **k: None,
    )


def _seed_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    """Copy of tests/regression/test_screening_gate.py::_seed_screenable."""
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


def _build_app(database_paths: DatabasePathSet):
    return web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=DuckDBStore(paths=database_paths),
        sqlite=SQLiteStore(paths=database_paths),
    )


def test_startup_maintenance_prewarms_before_anything_else(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch, _quiet_startup,
) -> None:
    """① 启动维护必须先预热筛选体检，且整份体检只算一遍。

    旧实现把 `warm_screening_readiness_cache()` 放在「启动自动更新子进程」之后，
    并把 `minimum_data_readiness()` 与 `screening_readiness()` 各算一遍 —— 后者
    内部**又**算一次 `minimum_data_readiness()`（副本实测 7.2s + 36.5s，且与
    warning-codes 后台重建线程重复 37.3s）。这里用调用计数把"只算一遍"锁死，
    用事件序列锁死"预热在自动更新之前"。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    calls = {"n": 0}
    events: list[tuple[str, str | None]] = []
    original_readiness = dq.minimum_data_readiness
    original_warm = dq.warm_screening_readiness_cache

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original_readiness(*args, **kwargs)

    def recording_warm(*args, **kwargs):
        events.append(("prewarm", kwargs.get("reason")))
        return original_warm(*args, **kwargs)

    class _RecordingAutoUpdate:
        def __init__(self, **_kwargs) -> None:
            pass

        def status(self) -> dict:
            events.append(("auto_update_status", None))
            return {"enabled": False, "paused": False}

    monkeypatch.setattr(dq, "minimum_data_readiness", counting)
    monkeypatch.setattr(dq, "warm_screening_readiness_cache", recording_warm)
    monkeypatch.setattr("app.core.auto_update.AutoUpdateController", _RecordingAutoUpdate)

    current = web_main._run_startup_maintenance(
        duckdb_store,
        sqlite_store,
        {"ready": False, "checking": True},
    )

    status = dq.screening_readiness_prewarm_status(sqlite_store)
    assert status["prewarm"]["status"] in {"done", "cache_hit"}, status
    assert status["prewarm"]["duration_seconds"] is not None
    assert status["prewarmed"] is True
    # 预热必须发生在"碰自动更新"之前（旧实现排在 run_auto_update_child 之后）。
    assert events[0] == ("prewarm", "startup"), events
    assert ("auto_update_status", None) in events
    assert [name for name, _reason in events].index("prewarm") < events.index(
        ("auto_update_status", None)
    )
    # 合并：启动核对结果就是预热那次计算里的 readiness，没有第二次全库扫。
    assert current["screening_readiness_prewarmed"] is True
    assert current["ready"] is True
    assert calls["n"] == 1, f"全库体检被算了 {calls['n']} 遍"


def test_startup_maintenance_survives_prewarm_failure(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch, _quiet_startup,
) -> None:
    """② 预热撞写锁（DuckDBReadLockedError）→ 只 log，启动照旧，退回同步核对。"""
    _seed_screenable(duckdb_store, sqlite_store)

    def locked(*_args, **_kwargs):
        raise DuckDBReadLockedError("io error: Could not set lock on file")

    monkeypatch.setattr(dq, "screening_readiness", locked)
    fallback = {
        "ready": True,
        "stock_count": 1,
        "missing": {},
        "missing_counts": {},
        "schema_compatibility": {"compatible": True, "missing": []},
    }
    monkeypatch.setattr(dq, "minimum_data_readiness", lambda *a, **k: dict(fallback))

    current = web_main._run_startup_maintenance(
        duckdb_store,
        sqlite_store,
        {"ready": False, "checking": True},
    )

    status = dq.screening_readiness_prewarm_status(sqlite_store)
    assert status["prewarm"]["status"] == "failed"
    assert "DuckDBReadLockedError" in (status["prewarm"]["error"] or "")
    assert status["prewarmed"] is False
    # 启动没有失败：核对结果照旧落盘，且明确标出来自同步冷算。
    assert current["ready"] is True
    assert "screening_readiness_prewarmed" not in current
    assert dq.read_cached_data_readiness(sqlite_store) is not None
    # 失败绝不落盘伪决策（否则第一次筛选会拿到过期/假的"已就绪"）。
    assert dq.load_screening_readiness_cache(
        sqlite_store,
        dq.screening_readiness_cache_key(duckdb_store, sqlite_store) or "",
    ) is None


def test_first_screening_hits_prewarmed_cache(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, _quiet_startup,
) -> None:
    """③ 预热成功后第一次筛选（门禁）直接命中缓存，不再冷算全库体检。"""
    _seed_screenable(duckdb_store, sqlite_store)

    web_main._run_startup_maintenance(
        duckdb_store,
        sqlite_store,
        {"ready": False, "checking": True},
    )

    gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    assert gate["readiness_source"] == "cache"
    assert gate["readiness_stale"] is False
    assert gate["lock_active"] is False


def test_prewarm_recomputes_after_content_epoch_changes(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    """④ 数据变了（content_epoch +1）→ 指纹变 → 必须重算，不许复用旧决策。

    失效机制**沿用既有的** `content_epoch`（每个成功写事务 +1）而不是新造一套：
    它正是"就地 UPDATE 也看得见"的那一半信号（见 content_epoch 模块 docstring）。
    """
    from app.core.storage import content_epoch

    _seed_screenable(duckdb_store, sqlite_store)
    calls = {"n": 0}
    original = dq.screening_readiness

    def counting(duck, sqlite):
        calls["n"] += 1
        return original(duck, sqlite)

    monkeypatch.setattr(dq, "screening_readiness", counting)

    first = dq.warm_screening_readiness_cache(duckdb_store, sqlite_store, reason="test")
    assert first is not None and first["ready"] is True
    assert calls["n"] == 1
    first_fingerprint = dq.screening_readiness_cache_key(duckdb_store, sqlite_store)

    # 同一输入 → 命中缓存，不重算。
    again = dq.warm_screening_readiness_cache(duckdb_store, sqlite_store, reason="test")
    assert again is not None
    assert calls["n"] == 1
    assert dq.screening_readiness_prewarm_status()["prewarm"]["status"] == "cache_hit"

    # 模拟"数据被写过一次"：纪元 +1（真实写事务由 DuckDBStore 自己 bump）。
    content_epoch.bump(sqlite_store.paths.sqlite_path, reason="test-write")
    second_fingerprint = dq.screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert second_fingerprint != first_fingerprint

    third = dq.warm_screening_readiness_cache(duckdb_store, sqlite_store, reason="test")
    assert third is not None
    assert calls["n"] == 2
    assert dq.screening_readiness_prewarm_status()["prewarm"]["status"] == "done"
    assert dq.screening_readiness_prewarm_status()["prewarm"]["cache_source"] == "computed"


def test_gate_does_not_serve_stale_decision_after_data_change(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """④ 续：纪元变了以后，请求路径**不能**拿旧决策冒充当前（不得假绿）。"""
    from app.core.storage import content_epoch

    _seed_screenable(duckdb_store, sqlite_store)
    dq.warm_screening_readiness_cache(duckdb_store, sqlite_store, reason="test")
    first_gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    content_epoch.bump(sqlite_store.paths.sqlite_path, reason="test-write")
    second_gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    # 同指纹命中；纪元变了指纹就变 → 必须重算（computed），不是 cache/stale_cache。
    assert first_gate["readiness_source"] == "cache"
    assert second_gate["readiness_source"] == "computed"
    assert second_gate["readiness_stale"] is False


def test_gate_waits_for_inflight_prewarm_instead_of_recomputing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    """预热**未完成**时立刻筛选 → 等它算完再命中缓存，不重复冷算。

    副本实测：请求线程与预热线程各算一遍全库体检 → 59.5s（互相拖慢）；
    改成等预热落盘 → 32s 且只算一次。预热失败/跳过时事件同样会被 set，
    门禁自然退回冷算（见下一条用例）。
    """
    import threading

    _seed_screenable(duckdb_store, sqlite_store)
    calls = {"n": 0}
    original = dq.screening_readiness

    def counting(duck, sqlite):
        calls["n"] += 1
        return original(duck, sqlite)

    monkeypatch.setattr(dq, "screening_readiness", counting)
    with dq._screening_prewarm_lock:
        dq._screening_prewarm_state.update({"status": "running", "reason": "startup"})
        dq._screening_prewarm_done.clear()

    def finish_prewarm() -> None:
        # 模拟预热线程的尾部：算完 → 落盘 → 置终态（唤醒等待者）。
        decision = counting(duckdb_store, sqlite_store)
        fingerprint = dq.screening_readiness_cache_key(duckdb_store, sqlite_store)
        dq.store_screening_readiness_cache(sqlite_store, fingerprint, decision)
        dq._screening_prewarm_update(status="cache_hit", fingerprint=fingerprint)

    worker = threading.Thread(target=finish_prewarm, name="test-prewarm")
    worker.start()
    gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))
    worker.join(timeout=30)

    assert gate["readiness_source"] == "cache"
    assert calls["n"] == 1, f"全库体检被算了 {calls['n']} 遍（请求线程不该重算）"


def test_gate_falls_back_to_cold_compute_when_prewarm_fails(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """预热失败（终态 failed）→ 门禁自己冷算，不报错、不返回旧结论。"""
    _seed_screenable(duckdb_store, sqlite_store)
    with dq._screening_prewarm_lock:
        dq._screening_prewarm_state.update({"status": "running", "reason": "startup"})
        dq._screening_prewarm_done.clear()
    dq._screening_prewarm_update(status="failed", error="DuckDBReadLockedError: locked")

    gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    assert gate["readiness_source"] == "computed"
    assert gate["readiness_stale"] is False


def test_status_endpoint_exposes_prewarm_state(
    database_paths: DatabasePathSet, _quiet_startup,
) -> None:
    """可观测：status 接口能查到"体检是否已就绪"。"""
    from fastapi.testclient import TestClient

    app = _build_app(database_paths)
    body = TestClient(app).get("/api/maintenance/screening-readiness").json()

    assert body["prewarmed"] is False
    assert body["prewarm"]["status"] == "idle"
    assert body["cache"]["present"] is False
