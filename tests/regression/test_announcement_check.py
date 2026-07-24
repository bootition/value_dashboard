from __future__ import annotations

from app.core.update import IncrementalUpdater


class EmptySQLite:
    def query(self, sql: str) -> list[dict[str, int]]:
        return [{"cnt": 0}]


def test_announcement_check_does_not_claim_remote_comparison() -> None:
    updater = IncrementalUpdater.__new__(IncrementalUpdater)

    result = updater._check_new_announcements()

    assert result["status"] == "not_implemented"
    assert result["checked_remote"] is False


def test_incremental_check_exposes_announcement_capability_gap() -> None:
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.sqlite = EmptySQLite()
    updater._check_new_trading_days = lambda: []
    updater._get_latest_local_price_date = lambda: "2026-07-20"
    updater._check_retry_tasks = lambda: []

    report = updater.run_incremental_check()

    assert report["announcement_check"]["status"] == "not_implemented"
    assert "latest_cninfo_announcement_time" not in report
