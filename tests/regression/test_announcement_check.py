from __future__ import annotations

from datetime import datetime, timezone

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater


class EmptySQLite:
    def query(self, sql: str) -> list[dict[str, int]]:
        return [{"cnt": 0}]


def test_announcement_check_does_not_mark_a_filing_seen_before_refresh(duckdb_store, sqlite_store) -> None:
    class AnnouncementAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{"announcement_id": "notice-1", "announcement_time": "2026-07-28T00:00:00Z", "title": "annual"}],
                metadata=SourceMetadata(source="cninfo", fetch_time=datetime.now(timezone.utc), raw_response_hash="a" * 64, confidence="strict"),
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('000001', 'Test', 'SZSE')"
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
        "affected_announcements": {"000001": [{"announcement_id": "notice-1"}]},
    }
    updater.run_incremental_check = lambda: check
    updater._check_new_announcements = lambda persist=False: pending
    updater._refresh_financials = lambda codes: {
        "status": "partial", "succeeded_codes": [], "failed_codes": codes,
    }
    updater._refresh_market_actions = lambda codes: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks: {"status": "skipped", "success": 0}

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
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "annual"}]},
    }
    updater.run_incremental_check = lambda: check
    updater._check_new_announcements = lambda persist=False: pending
    updater._refresh_financials = lambda codes: {
        "status": "success", "succeeded_codes": codes, "failed_codes": [],
    }
    updater._refresh_market_actions = lambda codes: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks: {"status": "skipped", "success": 0}

    updater.run_incremental_update()

    assert sqlite_store.query(
        "SELECT announcement_id, stock_code FROM announcement_registry"
    ) == [{"announcement_id": "notice-1", "stock_code": "000001"}]


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
