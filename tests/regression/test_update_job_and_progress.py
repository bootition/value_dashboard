"""发布级红队 P1 回归：job_logs、跨进程更新互斥、按步进度回调、
股本变化触发快照重算、CSRC 断点续传。

对应 `.planning/2026-08-01-release-red-team/findings.md`：
- P1 增量更新不写 job_logs（状态页"最近更新"失真）
- P1 自动更新计划要求的跨进程互斥未实现
- P1 自动更新状态不是可操作的进度模型
- P1（三轮回测新发现）universe 刷新股本后未触发快照重算
- P1 CSRC 初始化性能（分块 + 断点续传）
"""

from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone

from app.core.init import CSRC_BATCH_SIZE, DataInitializer
from app.core.update import IncrementalUpdater
from app.core.storage.update_lock import exclusive_update
from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.tdx_adapter import TDXAdapter


def _stub_update_network_steps(updater: IncrementalUpdater) -> None:
    updater.run_incremental_check = lambda **kwargs: {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    updater._check_new_announcements = lambda persist=False: {
        "status": "available", "affected_stock_codes": [],
        "affected_announcements": {}, "all_new_announcements": {},
    }
    updater._refresh_financials = lambda codes: {"status": "success", "succeeded_codes": codes}
    updater._refresh_market_actions = lambda codes: {"status": "success"}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}


def test_tdx_declines_adjusted_prices_before_fetch() -> None:
    adapter = TDXAdapter(rate_limit=0)

    assert adapter.can_handle(FetchRequest(
        data_type="price_daily", stock_codes=["600519"], adjust="raw",
    )) is True
    assert adapter.can_handle(FetchRequest(
        data_type="price_daily", stock_codes=["600519"], adjust="qfq",
    )) is False


def test_incremental_update_writes_job_logs_lifecycle(duckdb_store, sqlite_store) -> None:
    """P1: run_incremental_update 必须写 job_logs 的 running→终态。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)

    report = updater.run_incremental_update()

    rows = sqlite_store.query(
        "SELECT job_type, status, started_at, finished_at, details_json "
        "FROM job_logs WHERE job_type = 'incremental_update'"
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["started_at"] is not None
    assert rows[0]["finished_at"] is not None
    details = json.loads(rows[0]["details_json"])
    assert details["job_id"] == report["job_id"]
    assert details["status"] == "success"


def test_incremental_update_always_closes_adapter_sessions(duckdb_store, sqlite_store) -> None:
    class ClosingAdapterManager:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    manager = ClosingAdapterManager()
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=manager)
    _stub_update_network_steps(updater)

    updater.run_incremental_update(max_stocks=1)

    assert manager.closed is True


def test_incremental_update_records_failed_job_on_partial(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "failed", "success": 0}

    report = updater.run_incremental_update()

    rows = sqlite_store.query(
        "SELECT status FROM job_logs WHERE job_type = 'incremental_update'"
    )
    assert rows[0]["status"] == "failed"
    assert report["status"] != "success"


def test_update_lock_rejects_concurrent_live_owner(duckdb_store, sqlite_store) -> None:
    """P1: 另一个活进程持锁时，run_incremental_update 返回 skipped。"""
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)

    report = updater.run_incremental_update()

    assert report["status"] == "skipped"
    assert report["reason"] == "another_update_running"


def test_update_lock_reclaims_dead_owner(duckdb_store, sqlite_store) -> None:
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text("pid=99999999\ntime=0\n", encoding="ascii")

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)

    report = updater.run_incremental_update()

    assert report["status"] != "skipped"
    assert not lock_path.exists()


def test_update_lock_pid_probe_rejects_exited_windows_process(monkeypatch) -> None:
    from app.core.storage import update_lock

    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):
            return 123

        def GetExitCodeProcess(self, handle, output):
            output._obj.value = 0
            return 1

        def CloseHandle(self, handle):
            return 1

    monkeypatch.setattr(update_lock.ctypes.windll, "kernel32", FakeKernel32(), raising=False)

    assert update_lock._pid_exists(12560) is False


def test_update_lock_recovery_closes_crashed_incremental_job(duckdb_store, sqlite_store) -> None:
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text("pid=99999999\ntime=0\n", encoding="ascii")
    sqlite_store.execute(
        """INSERT INTO job_logs (job_type, status, started_at, details_json)
           VALUES ('incremental_update', 'running', '2026-08-04T10:33:17+00:00', '{}')"""
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater.run_incremental_update()

    rows = sqlite_store.query(
        """SELECT status, finished_at, details_json FROM job_logs
           WHERE started_at = '2026-08-04T10:33:17+00:00'"""
    )
    assert rows[0]["status"] == "failed"
    assert rows[0]["finished_at"] is not None
    assert json.loads(rows[0]["details_json"])["reconciliation"]["reason_code"] == "dead_update_lock_owner"


def test_update_lock_context_manager_serializes(duckdb_store) -> None:
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    with exclusive_update(duckdb_store.db_path):
        assert lock_path.exists()
        # 同进程重入直接通过（跨进程互斥由锁文件 + PID 保证）
        with exclusive_update(duckdb_store.db_path):
            assert lock_path.exists()
        assert lock_path.exists()
    assert not lock_path.exists()


def test_progress_callback_receives_each_step(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater._refresh_universe_metadata = lambda: {"status": "success", "steps": {}}

    seen: list[tuple[str, str]] = []

    def progress(step_name: str, step: dict) -> None:
        seen.append((step_name, step.get("status")))

    updater.run_incremental_update(progress_cb=progress)

    assert seen
    assert ("universe", "success") in seen
    assert ("prices", "skipped") in seen


def test_bounded_update_stops_after_price_step(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    universe_called = False

    def universe() -> dict:
        nonlocal universe_called
        universe_called = True
        return {"status": "success", "steps": {}}

    updater._refresh_universe_metadata = universe

    report = updater.run_incremental_update(max_stocks=10)

    assert universe_called is False
    assert set(report["steps"]) == {"prices"}
    assert report["status"] == "success"


def test_share_capital_change_triggers_snapshot_recompute(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """P1: 股本/上市名单变化后必须重算快照（市值类指标口径）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)

    recomputed: list[dict] = []

    class FakeCalculator:
        def __init__(self, **kwargs) -> None:
            pass

        def compute_snapshot_for_all(self) -> dict:
            recomputed.append({"status": "success"})
            return {"status": "success"}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)
    fingerprint_calls = {"n": 0}

    def fingerprint() -> str:
        fingerprint_calls["n"] += 1
        return "before" if fingerprint_calls["n"] == 1 else "after"

    monkeypatch.setattr(updater, "_share_capital_fingerprint", fingerprint)

    report = updater.run_incremental_update()

    assert report["share_capital_changed"] is True
    assert recomputed, "share-capital change must trigger snapshot recompute"


def test_share_capital_unchanged_skips_recompute(duckdb_store, sqlite_store, monkeypatch) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    monkeypatch.setattr(updater, "_share_capital_fingerprint", lambda: "same")

    recomputed: list[dict] = []

    class FakeCalculator:
        def __init__(self, **kwargs) -> None:
            pass

        def compute_snapshot_for_all(self) -> dict:
            recomputed.append({"status": "success"})
            return {"status": "success"}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)

    updater.run_incremental_update()

    assert recomputed == []


def test_csrc_fetch_only_targets_missing_classifications(duckdb_store, sqlite_store, monkeypatch) -> None:
    """P1: CSRC 只补抓缺失分类的股票（断点续传）。"""
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed, csrc_l1)
           VALUES ('000001', 'a', 'SZSE', true, '制造业'),
                  ('000002', 'b', 'SZSE', true, NULL),
                  ('000003', 'c', 'SZSE', true, NULL)"""
    )
    requested: list[list[str]] = []

    class FakeAdapter:
        def fetch(self, request):
            requested.append(list(request.stock_codes))
            rows = [{"stock_code": code, "csrc_l1": "制造业", "csrc_l2": "大类"} for code in request.stock_codes]
            import hashlib
            from app.core.adapters.base import FetchResult, SourceMetadata
            raw = json.dumps(rows).encode("utf-8")
            return FetchResult(
                data=rows,
                metadata=SourceMetadata(
                    source="cninfo_csrc", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw).hexdigest(), confidence="strict",
                ),
                raw_response=raw,
            )

    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FakeAdapter())
    result = initializer._fetch_csrc_industry()

    assert result["status"] == "success"
    assert result["count"] == 2
    assert requested == [["000002", "000003"]]
    assert all(len(chunk) <= CSRC_BATCH_SIZE for chunk in requested)

    rows = duckdb_store.read_query(
        "SELECT stock_code, csrc_l1 FROM stock_meta WHERE stock_code IN ('000001','000002','000003') ORDER BY stock_code"
    )
    assert rows == [
        {"stock_code": "000001", "csrc_l1": "制造业"},
        {"stock_code": "000002", "csrc_l1": "制造业"},
        {"stock_code": "000003", "csrc_l1": "制造业"},
    ]


def test_csrc_fetch_skips_when_all_classified(duckdb_store, sqlite_store) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed, csrc_l1)
           VALUES ('000001', 'a', 'SZSE', true, '制造业')"""
    )

    class FailingAdapter:
        def fetch(self, request):
            raise AssertionError("no fetch expected when every stock is classified")

    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FailingAdapter())
    result = initializer._fetch_csrc_industry()

    assert result["status"] == "success"
    assert result["count"] == 0


def test_csrc_fetch_records_resume_progress(duckdb_store, sqlite_store) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'a', 'SZSE', true), ('000002', 'b', 'SZSE', true)"""
    )

    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_ChunkAdapter())
    result = initializer._fetch_csrc_industry()

    assert result["status"] == "success"
    rows = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = 'csrc_industry_progress'"
    )
    assert rows
    progress = json.loads(rows[0]["value"])
    assert progress["processed"] == 2
    assert progress["total"] == 2


def test_csrc_fetch_handles_empty_success_without_executemany(duckdb_store, sqlite_store) -> None:
    from types import SimpleNamespace

    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed, csrc_l1)
           VALUES ('600519', 'TEST', 'SSE', TRUE, NULL)"""
    )

    class EmptyAdapterManager:
        def fetch(self, request):
            return SimpleNamespace(data=[], metadata=SimpleNamespace(error=None, source="cninfo_csrc"))

    initializer = DataInitializer(
        duck=duckdb_store,
        sqlite=sqlite_store,
        adapter_mgr=EmptyAdapterManager(),
    )

    report = initializer._fetch_csrc_industry()

    assert report["status"] == "partial"
    assert report["count"] == 0
    assert report["errors"] == ["CSRC source returned no classifications"]


class _ChunkAdapter:
    def fetch(self, request):
        from app.core.adapters.base import FetchResult, SourceMetadata
        import hashlib

        rows = [{"stock_code": code, "csrc_l1": "制造业", "csrc_l2": "大类"} for code in request.stock_codes]
        raw = json.dumps(rows).encode("utf-8")
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="cninfo_csrc", fetch_time=datetime.now(timezone.utc),
                raw_response_hash=hashlib.sha256(raw).hexdigest(), confidence="strict",
            ),
            raw_response=raw,
        )


class _SimulatedPriceManager:
    """Fake adapter manager: 按股票返回标准价格行，fail_codes 命中则抛错模拟中断。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_codes: set[str] = set()

    def fetch(self, request: FetchRequest) -> FetchResult:
        code = request.stock_codes[0]
        self.calls.append((code, request.adjust))
        if code in self.fail_codes:
            raise RuntimeError(f"source down for {code}")
        rows = [
            {"trade_date": "2026-08-05", "open": 1.0, "high": 1.5, "low": 0.9,
             "close": 1.2, "volume": 100, "turnover": 1200.0, "turnover_rate": 0.01},
            {"trade_date": "2026-08-06", "open": 1.2, "high": 1.6, "low": 1.1,
             "close": 1.4, "volume": 120, "turnover": 1300.0, "turnover_rate": 0.012},
        ]
        return FetchResult(
            data=rows,
            raw_response=b"fake-price-payload",
            metadata=SourceMetadata(
                source="local_cache",
                fetch_time=datetime.now(timezone.utc),
                raw_response_hash=hashlib.sha256(b"fake-price-payload").hexdigest(),
                confidence="strict",
                row_count=len(rows),
                error=None,
            ),
        )


def test_price_update_resumes_from_committed_progress(duckdb_store, sqlite_store) -> None:
    """价格逐股原子提交：图 B 抓取失败（等价于中断）后，下次只补缺失股票，不重抓已提交的图 A。"""
    for code, listing in (("000001", "2020-01-01"), ("600519", "2020-01-01")):
        with duckdb_store.write_connection() as conn:
            conn.execute(
                """INSERT INTO stock_meta (stock_code, exchange, name, is_listed, is_suspended, listing_date)
                   VALUES (?, ?, ?, TRUE, FALSE, ?) ON CONFLICT DO NOTHING""",
                [code, "SZ" if code == "000001" else "SH", code, listing],
            )
    with sqlite_store.transaction() as conn:
        conn.execute("INSERT INTO trading_dates (trade_date) VALUES ('2026-08-06')")

    manager = _SimulatedPriceManager()
    manager.fail_codes = {"600519"}  # 模拟 600519 抓取失败，其余正常提交

    first = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=manager)
    first_report = first._update_prices_incremental(max_stocks=0)
    # 并发路径失败不中断整体；失败股计入 report 与重试列表
    assert first_report["status"] == "partial"
    assert first_report["success"] == 1
    assert first_report["failed"] == 1

    committed = duckdb_store.read_query(
        "SELECT stock_code FROM price_daily_raw WHERE trade_date = '2026-08-06'"
    )
    retries = sqlite_store.query("SELECT stock_code FROM retry_list")
    assert {row["stock_code"] for row in committed} == {"000001"}, "失败时已提交的股票必须留在库中"
    assert {row["stock_code"] for row in retries} == {"600519"}, "失败股票须进入重试列表"

    manager.fail_codes = set()  # 模拟下次启动续传
    second = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=manager)
    report = second._update_prices_incremental(max_stocks=0)

    assert report["status"] == "success"
    assert report["success"] == 1
    assert manager.calls.count(("000001", "raw")) == 1, "已达标的股票不得被再次抓取"
    after = duckdb_store.read_query(
        "SELECT stock_code FROM price_daily_raw WHERE trade_date = '2026-08-06'"
    )
    assert {row["stock_code"] for row in after} == {"000001", "600519"}
