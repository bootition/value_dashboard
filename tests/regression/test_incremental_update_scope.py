"""P0-2 回归: 增量更新必须覆盖股票池/上市状态/股本与 CSRC 行业（PRD §7.7 第 4 项）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.update import IncrementalUpdater


def _check_report(blocked: bool = False) -> dict:
    return {
        "new_trading_days": [], "retry_tasks": [],
        "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"},
        "needs_update": True, "blocked": blocked,
    }


def _stub_network_steps(updater: IncrementalUpdater) -> None:
    """Replace every network-touching step except the one under test."""
    updater.run_incremental_check = lambda **kwargs: _check_report()
    updater._check_new_announcements = lambda persist=False, **kwargs: {
        "status": "available", "affected_stock_codes": [],
        "affected_announcements": {}, "all_new_announcements": {},
    }
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "success", "succeeded_codes": codes, "failed_codes": [],
    }
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success"}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}


def test_incremental_update_runs_universe_listing_and_csrc_steps(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_network_steps(updater)
    calls: list[str] = []

    def fake_universe(self) -> dict:
        calls.append("universe")
        return {
            "status": "success",
            "steps": {
                "stock_list": {"status": "success", "count": 1},
                "listing_info": {"status": "success", "count": 1},
                "csrc_industry": {"status": "success", "count": 1},
            },
        }

    monkeypatch.setattr(IncrementalUpdater, "_refresh_universe_metadata", fake_universe)

    report = updater.run_incremental_update()

    assert calls == ["universe"]
    assert report["steps"]["universe"]["status"] == "success"
    assert report["steps"]["universe"]["steps"]["stock_list"]["status"] == "success"
    assert report["steps"]["universe"]["steps"]["csrc_industry"]["status"] == "success"


def test_universe_step_degrades_without_blocking_prices(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_network_steps(updater)

    def fake_universe(self) -> dict:
        return {
            "status": "partial",
            "steps": {
                "stock_list": {"status": "failed", "error": "source down"},
                "listing_info": {"status": "success", "count": 0},
                "csrc_industry": {"status": "skipped", "reason": "refreshed_recently"},
            },
        }

    monkeypatch.setattr(IncrementalUpdater, "_refresh_universe_metadata", fake_universe)

    report = updater.run_incremental_update()

    assert report["steps"]["universe"]["status"] == "partial"
    assert report["status"] in {"success", "partial"}


def test_csrc_refresh_due_initial_run() -> None:
    from app.core.update import IncrementalUpdater as Updater

    updater = Updater.__new__(Updater)
    updater.sqlite = _FakeSQLite([])
    updater.csrc_refresh_interval_days = 30

    assert updater._csrc_refresh_due() is True


def test_csrc_refresh_throttled_by_persisted_marker(duckdb_store, sqlite_store) -> None:
    today = datetime.now(UTC).date().isoformat()
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES ('csrc_industry_last_refresh', ?, ?)""",
        [today, datetime.now(UTC).isoformat()],
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._csrc_refresh_due() is False

    stale = (datetime.now(UTC).date() - timedelta(days=31)).isoformat()
    sqlite_store.execute(
        """UPDATE data_refresh_state SET value = ? WHERE key = 'csrc_industry_last_refresh'""",
        [stale],
    )
    assert updater._csrc_refresh_due() is True


def test_universe_step_skips_csrc_when_recently_refreshed(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    from app.core.init import DataInitializer

    today = datetime.now(UTC).date().isoformat()
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES ('csrc_industry_last_refresh', ?, ?)""",
        [today, datetime.now(UTC).isoformat()],
    )
    monkeypatch.setattr(
        DataInitializer, "_fetch_stock_universe",
        lambda self: {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        DataInitializer, "_fetch_listing_info",
        lambda self: {"status": "success", "count": 1},
    )

    def forbidden(self) -> dict:
        raise AssertionError("csrc refresh must be skipped inside the interval")

    monkeypatch.setattr(DataInitializer, "_fetch_csrc_industry", forbidden)

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    step = updater._refresh_universe_metadata()

    assert step["steps"]["csrc_industry"]["status"] == "skipped"
    assert step["steps"]["csrc_industry"]["interval_days"] == updater.csrc_refresh_interval_days


def test_universe_step_marks_csrc_refreshed_after_success(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    from app.core.init import DataInitializer

    monkeypatch.setattr(
        DataInitializer, "_fetch_stock_universe",
        lambda self: {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        DataInitializer, "_fetch_listing_info",
        lambda self: {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        DataInitializer, "_fetch_csrc_industry",
        lambda self: {"status": "success", "count": 1},
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    step = updater._refresh_universe_metadata()

    assert step["status"] == "success"
    rows = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = 'csrc_industry_last_refresh'"
    )
    assert rows
    assert str(rows[0]["value"])[:10] == datetime.now(UTC).date().isoformat()
    assert updater._csrc_refresh_due() is False


def test_universe_steps_are_throttled_by_daily_marker(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """P2: universe 步骤（stock_list/listing_info）按日节流，避免每轮 ~104s 网络开销。"""
    from app.core.init import DataInitializer

    now = datetime.now(UTC)
    for key in ("stock_list_last_refresh", "listing_info_last_refresh"):
        sqlite_store.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)""",
            [key, now.isoformat(), now.isoformat()],
        )

    def forbidden(self) -> dict:
        raise AssertionError("stock_list/listing_info must be skipped within the daily interval")

    monkeypatch.setattr(DataInitializer, "_fetch_stock_universe", forbidden)
    monkeypatch.setattr(DataInitializer, "_fetch_listing_info", forbidden)
    monkeypatch.setattr(
        DataInitializer, "_fetch_csrc_industry",
        lambda self: {"status": "success", "count": 1},
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    step = updater._refresh_universe_metadata()

    assert step["steps"]["stock_list"]["status"] == "skipped"
    assert step["steps"]["listing_info"]["status"] == "skipped"
    assert step["steps"]["csrc_industry"]["status"] == "success"


def test_universe_steps_run_when_marker_stale_or_absent(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """无刷新标记或标记过期时，universe 步骤照常执行。"""
    from app.core.init import DataInitializer

    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES ('stock_list_last_refresh', ?, ?)""",
        [stale, stale],
    )
    calls: list[str] = []
    monkeypatch.setattr(
        DataInitializer, "_fetch_stock_universe",
        lambda self: calls.append("stock_list") or {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        DataInitializer, "_fetch_listing_info",
        lambda self: calls.append("listing_info") or {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        DataInitializer, "_fetch_csrc_industry",
        lambda self: {"status": "success", "count": 1},
    )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    step = updater._refresh_universe_metadata()

    assert calls == ["stock_list", "listing_info"]
    assert step["steps"]["stock_list"]["status"] == "success"
    assert step["steps"]["listing_info"]["status"] == "success"


def test_full_init_skip_csrc_discloses_skipped(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """P2: --skip-csrc 先行建立最小可用，CSRC 步骤披露为 skipped 且不抓取。"""
    from app.core.init import DataInitializer

    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        initializer, "_fetch_stock_universe",
        lambda: {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        initializer, "_fetch_listing_info",
        lambda: {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        initializer, "_fetch_trading_dates",
        lambda: {"status": "success", "count": 1},
    )
    monkeypatch.setattr(
        initializer, "_fetch_daily_prices",
        lambda years=5: {"status": "skipped", "reason": "skip_prices"},
    )
    monkeypatch.setattr(
        initializer, "_fetch_financial_statements",
        lambda: {"status": "skipped", "reason": "skip_financials"},
    )

    def forbidden(self) -> dict:
        raise AssertionError("csrc fetch must be skipped by --skip-csrc")

    monkeypatch.setattr(initializer, "_fetch_csrc_industry", forbidden)

    report = initializer.run_full_init(skip_prices=True, skip_financials=True, skip_csrc=True)

    assert report["steps"]["sw_industry"]["status"] == "skipped"
    assert "skipped_by_flag" in report["steps"]["sw_industry"]["reason"]


class _FakeSQLite:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        return self._rows
