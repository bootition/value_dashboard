from __future__ import annotations

from datetime import datetime

from app.web.main import auto_update_is_due


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        # 固定为北京时间收盘后 16:00，期望目标为当天交易日。
        value = datetime(2026, 9, 8, 16, 0, 0)
        return value.astimezone(tz) if tz is not None else value


def _ensure_auto_update_state(sqlite_store) -> None:
    with sqlite_store.transaction() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS auto_update_state (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   state TEXT NOT NULL,
                   paused INTEGER NOT NULL DEFAULT 0,
                   current_stage TEXT,
                   progress_json TEXT,
                   last_error TEXT,
                   last_success_at TEXT,
                   updated_at TEXT
               )"""
        )


def test_auto_update_due_after_close_uses_today(duckdb_store, sqlite_store, monkeypatch) -> None:
    monkeypatch.setattr("app.web.main.datetime", _FakeDatetime)
    _ensure_auto_update_state(sqlite_store)
    with sqlite_store.transaction() as conn:
        conn.execute(
            "INSERT INTO auto_update_state (id, state, paused, current_stage) VALUES (1, 'enabled', 0, 'idle')"
        )
        conn.execute("INSERT INTO trading_dates (trade_date) VALUES ('2026-09-08')")
    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', '2026-09-07', 10)"
    )

    assert auto_update_is_due(duckdb_store, sqlite_store) is True

    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', '2026-09-08', 11)"
    )
    assert auto_update_is_due(duckdb_store, sqlite_store) is False


def test_auto_update_not_due_when_disabled_or_paused(duckdb_store, sqlite_store, monkeypatch) -> None:
    monkeypatch.setattr("app.web.main.datetime", _FakeDatetime)
    _ensure_auto_update_state(sqlite_store)
    with sqlite_store.transaction() as conn:
        conn.execute(
            "INSERT INTO auto_update_state (id, state, paused, current_stage) VALUES (1, 'enabled', 1, 'idle')"
        )
        conn.execute("INSERT INTO trading_dates (trade_date) VALUES ('2026-09-08')")
    assert auto_update_is_due(duckdb_store, sqlite_store) is False

    with sqlite_store.transaction() as conn:
        conn.execute("UPDATE auto_update_state SET state = 'disabled', paused = 0 WHERE id = 1")
    assert auto_update_is_due(duckdb_store, sqlite_store) is False
