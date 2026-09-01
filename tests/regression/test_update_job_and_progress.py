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

import hashlib
import json
import os
from datetime import UTC, datetime

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.tdx_adapter import TDXAdapter
from app.core.init import CSRC_BATCH_SIZE, DataInitializer
from app.core.storage.update_lock import exclusive_update
from app.core.update import IncrementalUpdater


def _stub_update_network_steps(updater: IncrementalUpdater) -> None:
    updater.run_incremental_check = lambda **kwargs: {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    updater._check_new_announcements = lambda persist=False, **kwargs: {
        "status": "available", "affected_stock_codes": [],
        "affected_announcements": {}, "all_new_announcements": {},
    }
    updater._refresh_financials = lambda codes, **kwargs: {"status": "success", "succeeded_codes": codes}
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success"}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}
    # P2/P3 独立低频域：测试环境不得发起真实网络请求
    # 指标输入域（回购/融资/财务明细回填）：测试环境不得发起真实网络请求。
    # 全部 skipped 且无 changed_codes，可确保"无输入变化则不再重算快照"。
    updater._refresh_financial_detail_backfill = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub", "succeeded_codes": [],
    }
    updater._refresh_buyback = lambda: {
        "status": "skipped", "reason": "test_stub", "changed_codes": [],
    }
    updater._refresh_funding = lambda: {
        "status": "skipped", "reason": "test_stub", "changed_codes": [],
    }
    updater._refresh_business_overview = lambda **kwargs: {"status": "skipped", "reason": "test_stub"}
    updater._refresh_treasury_curve = lambda: {"status": "skipped", "reason": "test_stub"}
    # P4 历史股本链与统计域：测试环境不得触发网络或全量重建
    updater._refresh_capital_history = lambda **kwargs: {"status": "skipped", "reason": "test_stub"}
    updater._refresh_research_statistics = lambda **kwargs: {"status": "skipped", "reason": "test_stub"}


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


def test_incremental_update_preserves_partial_job_when_steps_mixed(duckdb_store, sqlite_store) -> None:
    """价格 failed 但其他降级步骤形成 partial 时，job_logs 如实保留 partial。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "failed", "success": 0}

    report = updater.run_incremental_update()

    rows = sqlite_store.query(
        "SELECT status FROM job_logs WHERE job_type = 'incremental_update'"
    )
    assert report["status"] == "partial"
    assert rows[0]["status"] == "partial"


def test_incremental_update_persists_partial_job_status(duckdb_store, sqlite_store) -> None:
    """partial 更新实际推进了数据，job_logs 不得降级成 failed。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {
        "status": "partial", "success": 5, "failed": 1,
    }

    report = updater.run_incremental_update()

    rows = sqlite_store.query(
        "SELECT status FROM job_logs WHERE job_type = 'incremental_update'"
    )
    assert report["status"] == "partial"
    assert rows[0]["status"] == "partial"


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

        def compute_snapshot_for_all(self, *, progress_cb=None) -> dict:
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

        def compute_snapshot_for_all(self, *, progress_cb=None) -> dict:
            recomputed.append({"status": "success"})
            return {"status": "success"}

        def compute_snapshot_for_codes(self, codes, *, progress_cb=None) -> dict:
            recomputed.append({"status": "success"})
            return {"status": "success"}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)

    updater.run_incremental_update()

    assert recomputed == []


def test_share_capital_change_recomputes_even_when_financials_partial(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """财报步骤 partial 时股本变化仍必须触发全量快照重算。

    2026-08-31 全局数据路径审计：旧条件要求 financials 必须 success，
    partial 时会继续使用旧股本口径的 market_cap/per_share 指标。
    """
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater._check_new_announcements = lambda persist=False, **kwargs: {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "a1", "title": "年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "a1", "title": "年度报告"}]},
    }
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "partial", "succeeded_codes": [],
    }
    recomputed: list[str] = []

    class FakeCalculator:
        def __init__(self, **kwargs) -> None:
            pass

        def compute_snapshot_for_all(self, *, progress_cb=None) -> dict:
            recomputed.append("full")
            return {"status": "success"}

        def compute_snapshot_for_codes(self, codes, *, progress_cb=None) -> dict:
            recomputed.append("codes")
            return {"status": "success"}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)
    fingerprint_calls = {"n": 0}

    def fingerprint() -> str:
        fingerprint_calls["n"] += 1
        return "before" if fingerprint_calls["n"] == 1 else "after"

    monkeypatch.setattr(updater, "_share_capital_fingerprint", fingerprint)

    report = updater.run_incremental_update()

    assert report["share_capital_changed"] is True
    assert recomputed == ["full"]


def test_buyback_funding_treasury_refresh_before_indicators(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """指标输入域必须在 snapshot 重算之前刷新。

    回购/融资/国债曲线任一域变化都会改变快照字段；旧流程中融资与国债
    位于指标重算之后，会整轮陈旧。
    """
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES ('000001', 'a', 'SZSE', true)"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    order: list[str] = []

    def buyback() -> dict:
        order.append("buyback")
        return {"status": "success", "changed_codes": ["000001"]}

    def funding() -> dict:
        order.append("funding")
        return {"status": "success", "changed_codes": ["000001"]}

    def treasury() -> dict:
        order.append("treasury_curve")
        return {"status": "success", "curve_changed": True}

    updater._refresh_buyback = buyback
    updater._refresh_funding = funding
    updater._refresh_treasury_curve = treasury
    computed: list[list[str]] = []

    class FakeCalculator:
        def __init__(self, **kwargs) -> None:
            pass

        def compute_snapshot_for_all(self, *, progress_cb=None) -> dict:
            order.append("indicators_full")
            return {"status": "success"}

        def compute_snapshot_for_codes(self, codes, *, progress_cb=None) -> dict:
            order.append("indicators")
            computed.append(list(codes))
            return {"status": "success"}

        def refresh_treasury_spreads(self, codes) -> dict:
            order.append("indicators_treasury")
            computed.append(list(codes))
            return {"status": "success"}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)
    updater._share_capital_fingerprint = lambda: "same"

    updater.run_incremental_update()

    assert order.index("buyback") < order.index("indicators")
    assert order.index("funding") < order.index("indicators")
    assert order.index("treasury_curve") < order.index("indicators")
    assert order.index("treasury_curve") < order.index("indicators_treasury")
    assert computed and "000001" in computed[-1]


def test_retry_success_runs_before_indicators_and_triggers_recompute(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """重试成功的快照输入必须在同一轮进入指标重算。

    旧流程把 retries 放在统计域之后，价格/财务/分红/融资/曲线的重试成果
    要再等一整轮才反映到 indicator_snapshot。
    """
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES ('000001', 'a', 'SZSE', true)"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_update_network_steps(updater)
    updater.run_incremental_check = lambda **kwargs: {
        "new_trading_days": [], "retry_tasks": [{"id": 1}],
        "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"},
        "needs_update": True, "blocked": False,
    }
    updater._check_retry_tasks = lambda: [{
        "id": 1, "stock_code": "000001", "data_type": "price_daily",
        "adapter": "fixture", "error": "fixture", "retry_count": 0,
        "extra_json": "{}",
    }]
    order: list[str] = []

    def retry(tasks: list[dict]) -> dict:
        order.append("retries")
        return {"status": "success", "total": len(tasks),
                "succeeded": 1, "still_failing": 0,
                "recompute_codes": ["000001"]}

    updater._retry_failed_tasks = retry
    computed: list[list[str]] = []

    class FakeCalculator:
        def __init__(self, **kwargs) -> None:
            pass

        def compute_snapshot_for_all(self, *, progress_cb=None) -> dict:
            order.append("indicators_full")
            return {"status": "success"}

        def compute_snapshot_for_codes(self, codes, *, progress_cb=None) -> dict:
            order.append("indicators")
            computed.append(list(codes))
            return {"status": "success"}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)
    updater._share_capital_fingerprint = lambda: "same"

    report = updater.run_incremental_update()

    assert report["steps"]["retries"]["recompute_codes"] == ["000001"]
    assert "retries" in order and "indicators" in order
    assert order.index("retries") < order.index("indicators")
    assert computed and "000001" in computed[-1]


def test_treasury_curve_fingerprint_detects_value_updates(
    duckdb_store, sqlite_store,
) -> None:
    """行数与最大日期不变、收益率被 upsert 修正时也必须触发利差重算。"""
    duckdb_store.write_query(
        """INSERT INTO treasury_yield_curve
           (curve_date, tenor_years, yield_pct, source, fetch_time,
            raw_hash, confidence, batch_id)
           VALUES ('2026-08-28', 10.0, 2.10, 'czb_mof', CURRENT_TIMESTAMP,
                   '0', 'strict', 'b1')"""
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    before = updater._treasury_curve_fingerprint()
    duckdb_store.write_query(
        """UPDATE treasury_yield_curve SET yield_pct = 2.25
           WHERE curve_date = '2026-08-28' AND tenor_years = 10.0"""
    )
    after = updater._treasury_curve_fingerprint()
    assert before != ""
    assert after != before


def test_financial_detail_gap_excludes_b_shares_in_sql(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """B 股过滤必须在 SQL 内完成；旧实现逐代码查库，5000 缺口会放大到分钟级。"""
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'sparse', 'SZSE', true),
                  ('200001', 'sparseB', 'SZSE', true),
                  ('900001', 'sparseB股', 'SSE', true)"""
    )
    for code in ("000001", "200001", "900001"):
        duckdb_store.write_query(
            f"""INSERT INTO balance_sheet (stock_code, report_date, total_assets)
               VALUES ('{code}', '2026-06-30', 100)"""
        )
        duckdb_store.write_query(
            f"""INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES ('{code}', '2026-06-30', 10)"""
        )
        duckdb_store.write_query(
            f"""INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
               VALUES ('{code}', '2026-06-30', 3)"""
        )

    def fail_if_called(stock_code: str) -> bool:
        raise AssertionError("B 股判断不得在缺口查询中对每只股票逐次查库")

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(updater, "_is_b_share_stock", fail_if_called)
    assert updater._financial_detail_gap_codes() == ["000001"]


def test_financial_detail_source_missing_blocks_recent_retry(
    duckdb_store, sqlite_store,
) -> None:
    """快速源确认无数据的股票 7 天内出队，之后允许重新尝试。"""
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'sparse', 'SZSE', true)"""
    )
    duckdb_store.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets)
           VALUES ('000001', '2026-06-30', 100)"""
    )
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue)
           VALUES ('000001', '2026-06-30', 10)"""
    )
    duckdb_store.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES ('000001', '2026-06-30', 3)"""
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._record_financial_detail_missing("000001", "sina: source_empty")
    assert updater._financial_detail_gap_codes() == []

    from datetime import UTC, datetime, timedelta

    sqlite_store.execute(
        """UPDATE missing_list SET detected_at = ?
           WHERE stock_code = '000001' AND field_name = 'financial_detail_backfill'""",
        [(datetime.now(UTC) - timedelta(days=8)).isoformat()],
    )
    assert updater._financial_detail_gap_codes() == ["000001"]


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
                    source="cninfo_csrc", fetch_time=datetime.now(UTC),
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

    assert report["status"] == "success"
    assert report["count"] == 0
    assert report["missing"] == 1
    assert report["errors"] == []
    missing = sqlite_store.query(
        "SELECT stock_code, field_name, reason_code FROM missing_list WHERE field_name = 'csrc_industry'"
    )
    assert missing == [{
        "stock_code": "600519",
        "field_name": "csrc_industry",
        "reason_code": "source_no_classification",
    }]


class _ChunkAdapter:
    def fetch(self, request):
        import hashlib

        from app.core.adapters.base import FetchResult, SourceMetadata

        rows = [{"stock_code": code, "csrc_l1": "制造业", "csrc_l2": "大类"} for code in request.stock_codes]
        raw = json.dumps(rows).encode("utf-8")
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="cninfo_csrc", fetch_time=datetime.now(UTC),
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
                fetch_time=datetime.now(UTC),
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


def test_csrc_full_refresh_replaces_legacy_non_csrc_values(
    duckdb_store, sqlite_store,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed, csrc_l1, csrc_l2)
           VALUES ('000001', 'a', 'SZSE', true, '金融', '银行'),
                  ('000002', 'b', 'SZSE', true, '主要消费', '饮料')"""
    )

    class FullAdapter:
        def fetch(self, request):
            rows = [
                {"stock_code": code, "csrc_l1": "制造业", "csrc_l2": "专用设备制造业"}
                for code in request.stock_codes
                if code == "000001"
            ]
            import hashlib

            from app.core.adapters.base import FetchResult, SourceMetadata
            raw = json.dumps(rows).encode("utf-8")
            return FetchResult(
                data=rows,
                metadata=SourceMetadata(
                    source="cninfo_csrc", fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw).hexdigest(), confidence="strict",
                ),
                raw_response=raw,
            )

    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FullAdapter())
    report = initializer._fetch_csrc_industry(full_refresh=True)

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT stock_code, csrc_l1, csrc_l2 FROM stock_meta ORDER BY stock_code"
    )
    assert rows == [
        {"stock_code": "000001", "csrc_l1": "制造业", "csrc_l2": "专用设备制造业"},
        {"stock_code": "000002", "csrc_l1": None, "csrc_l2": None},
    ]
    missing = sqlite_store.query(
        "SELECT stock_code FROM missing_list WHERE field_name = 'csrc_industry' AND resolved_at IS NULL"
    )
    assert missing == [{"stock_code": "000002"}]


def test_financial_detail_gap_detects_sparse_core_rows(duckdb_store, sqlite_store) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'sparse', 'SZSE', true)"""
    )
    duckdb_store.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_liabilities, total_equity)
           VALUES ('000001', '2026-06-30', 100, 20, 80)"""
    )
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
           VALUES ('000001', '2026-06-30', 10, 2)"""
    )
    duckdb_store.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES ('000001', '2026-06-30', 3)"""
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._financial_detail_gap_codes() == ["000001"]

    duckdb_store.write_query(
        """UPDATE balance_sheet SET monetary_funds = 1 WHERE stock_code = '000001'"""
    )
    duckdb_store.write_query(
        """UPDATE income_statement SET cost_of_revenue = 1 WHERE stock_code = '000001'"""
    )
    duckdb_store.write_query(
        """UPDATE cash_flow SET cash_received_sales = 1 WHERE stock_code = '000001'"""
    )
    assert updater._financial_detail_gap_codes() == []


def test_financial_detail_gap_uses_sector_universal_probes(
    duckdb_store, sqlite_store,
) -> None:
    """银行等特殊行业不能用营业成本/销售收现探测明细缺口。

    回填成功后 cost_of_revenue / cash_received_sales 仍合法为 NULL；
    缺口检测必须改用 paid_in_capital/interest_income/cash_paid_employees
    等跨行业通用字段，否则这些股票会永久占据回填队列头部。
    """
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'bank', 'SZSE', true)"""
    )
    duckdb_store.write_query(
        """INSERT INTO balance_sheet
               (stock_code, report_date, total_assets, total_liabilities, total_equity,
                monetary_funds, paid_in_capital, undistributed_profit)
           VALUES ('000001', '2026-06-30', 100, 20, 80, 5, 10, 1)"""
    )
    duckdb_store.write_query(
        """INSERT INTO income_statement
               (stock_code, report_date, revenue, parent_net_profit,
                interest_income, interest_expense, administrative_expenses)
           VALUES ('000001', '2026-06-30', 10, 2, 8, 4, 1)"""
    )
    duckdb_store.write_query(
        """INSERT INTO cash_flow
               (stock_code, report_date, cf_from_operating,
                cash_paid_employees, cash_paid_taxes)
           VALUES ('000001', '2026-06-30', 3, 1, 0.5)"""
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._financial_detail_gap_codes() == []
