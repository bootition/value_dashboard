from __future__ import annotations

from datetime import datetime, timezone

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater, classify_announcement


class EmptySQLite:
    def query(self, sql: str) -> list[dict[str, int]]:
        return [{"cnt": 0}]


def test_announcement_check_does_not_mark_a_filing_seen_before_refresh(duckdb_store, sqlite_store) -> None:
    class AnnouncementAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{"announcement_id": "notice-1", "announcement_time": "2026-07-28T00:00:00Z",
                       "title": "2026年半年度报告", "stock_code": "000001"}],
                metadata=SourceMetadata(source="cninfo", fetch_time=datetime.now(timezone.utc), raw_response_hash="a" * 64, confidence="strict"),
            )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=AnnouncementAdapter())

    first = updater._check_new_announcements(persist=False)
    second = updater._check_new_announcements(persist=False)
    persisted = updater._check_new_announcements(persist=True)

    assert first["status"] == "available"
    assert first["affected_stock_codes"] == ["000001"]
    assert second["affected_stock_codes"] == ["000001"]
    assert persisted["affected_stock_codes"] == ["000001"]
    assert sqlite_store.query("SELECT * FROM announcement_registry") == []


def test_failed_financial_refresh_keeps_announcement_pending_and_records_retry(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False: pending
    updater._refresh_financials = lambda codes: {
        "status": "partial", "succeeded_codes": [], "failed_codes": codes,
    }
    updater._refresh_market_actions = lambda codes: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query("SELECT * FROM announcement_registry") == []
    assert sqlite_store.query(
        "SELECT data_type FROM retry_list WHERE stock_code = '000001'"
    ) == [{"data_type": "announcements"}]


def test_successful_financial_refresh_marks_announcement_seen(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False: pending
    updater._refresh_financials = lambda codes: {
        "status": "success", "succeeded_codes": codes, "failed_codes": [],
    }
    updater._refresh_market_actions = lambda codes: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query(
        "SELECT announcement_id, stock_code FROM announcement_registry"
    ) == [{"announcement_id": "notice-1", "stock_code": "000001"}]


def test_non_financial_announcement_is_registered_without_financial_refresh(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": [],
        "affected_announcements": {},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-2", "title": "关于召开临时股东大会的通知"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False: pending
    updater._refresh_financials = lambda codes: (_ for _ in ()).throw(AssertionError("financial refresh must not run"))
    updater._update_prices_incremental = lambda max_stocks: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query(
        "SELECT announcement_id, stock_code FROM announcement_registry"
    ) == [{"announcement_id": "notice-2", "stock_code": "000001"}]


def test_classify_announcement_financial_keywords() -> None:
    assert classify_announcement("2026年半年度报告") == "financial"
    assert classify_announcement("2025年年度报告") == "financial"
    assert classify_announcement("2026年第一季度报告") == "financial"
    assert classify_announcement("2026年第三季度报告") == "financial"
    assert classify_announcement("2026年半年度业绩预告") == "financial"
    assert classify_announcement("2026年年度业绩快报") == "financial"


def test_classify_announcement_dividend_and_other() -> None:
    assert classify_announcement("2025年度权益分派实施公告") == "dividend"
    assert classify_announcement("关于召开临时股东大会的通知") == "other"
    assert classify_announcement("高管辞职公告") == "other"
    assert classify_announcement(None) == "other"


def test_announcement_refreshes_dividends_and_corporate_actions(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    calls: list[tuple[str, str]] = []
    updater.refetch_one = lambda code, data_type: calls.append((code, data_type)) or {"status": "success"}

    result = updater._refresh_market_actions(["000001"])

    assert result["status"] == "success"
    assert calls == [("000001", "dividends"), ("000001", "xdxr")]


def test_incremental_check_exposes_announcement_capability_gap() -> None:
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.sqlite = EmptySQLite()
    updater._check_new_trading_days = lambda: []
    updater._get_latest_local_price_date = lambda: "2026-07-20"
    updater._check_retry_tasks = lambda: []

    report = updater.run_incremental_check()

    assert report["announcement_check"]["status"] == "unavailable"
    assert report["needs_update"] is True
    assert report["blocked"] is True


def test_refetch_one_incremental_writes_new_report_period(duckdb_store, sqlite_store) -> None:
    """本地已有 2026Q1，数据源返回 2026Q2 → 只写入新报告期，Q1 不变。"""
    import hashlib

    raw_response = b'{"num":1}'

    class FinancialAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{
                    "stock_code": "000001", "report_date": "2026-06-30",
                    "total_assets": 200.0,
                }],
                metadata=SourceMetadata(
                    source="sina", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="strict",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO balance_sheet (stock_code, report_date, total_assets) "
        "VALUES ('000001', '2026-03-31', 100.0)"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FinancialAdapter())

    result = updater.refetch_one("000001", "balance_sheet", incremental=True)

    assert result["status"] == "success"
    assert result["skipped"] is False
    rows = duckdb_store.read_query(
        "SELECT report_date, total_assets FROM balance_sheet WHERE stock_code = '000001' ORDER BY report_date"
    )
    assert [(str(r["report_date"]), r["total_assets"]) for r in rows] == [
        ("2026-03-31", 100.0), ("2026-06-30", 200.0),
    ]


def test_refetch_one_incremental_skips_when_source_has_no_new_period(duckdb_store, sqlite_store) -> None:
    """数据源仍返回旧报告期（财报延迟）→ skipped，不写入、不覆盖旧值。"""
    import hashlib

    raw_response = b'{"num":1}'

    class FinancialAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{
                    "stock_code": "000001", "report_date": "2026-03-31",
                    "total_assets": 99.0,
                }],
                metadata=SourceMetadata(
                    source="sina", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="strict",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO balance_sheet (stock_code, report_date, total_assets) "
        "VALUES ('000001', '2026-03-31', 100.0)"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FinancialAdapter())

    result = updater.refetch_one("000001", "balance_sheet", incremental=True)

    assert result["status"] == "success"
    assert result["skipped"] is True
    rows = duckdb_store.read_query(
        "SELECT total_assets FROM balance_sheet WHERE stock_code = '000001' AND report_date = '2026-03-31'"
    )
    assert rows == [{"total_assets": 100.0}]  # 旧值保留，不被 99.0 覆盖


def test_refresh_financials_marks_pending_when_source_lags(duckdb_store, sqlite_store) -> None:
    """三表均无新报告期（数据源延迟）→ 股票进入 pending_codes，不标记 seen。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater.refetch_one = lambda code, data_type, incremental=False: {
        "status": "success", "skipped": True,
    }

    result = updater._refresh_financials(["000001"])

    assert result["status"] == "success"
    assert result["succeeded_codes"] == []
    assert result["pending_codes"] == ["000001"]


def test_pending_financial_refresh_keeps_announcement_pending_and_retries(duckdb_store, sqlite_store) -> None:
    """数据源延迟时，公告保持 pending 并进入重试列表（下次启动重试）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False: pending
    updater._refresh_financials = lambda codes: {
        "status": "success", "succeeded_codes": [], "failed_codes": [],
        "pending_codes": codes,
    }
    updater._refresh_market_actions = lambda codes: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query("SELECT * FROM announcement_registry") == []
    retry = sqlite_store.query(
        "SELECT data_type, error FROM retry_list WHERE stock_code = '000001'"
    )
    assert retry == [{
        "data_type": "announcements",
        "error": "financial data source not yet ready; announcement remains pending",
    }]
