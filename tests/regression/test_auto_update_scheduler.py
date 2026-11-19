from __future__ import annotations

from datetime import datetime

from app.web.main import auto_update_is_due
from tests.support.seed_sqlite import SeedSqlite

# 2026-11-23 修正：原来这里 monkeypatch 的是 `app.web.main.datetime`，
# 但 `auto_update_is_due()` 用的是 **`cn_now()`** —— 补丁打在了一个该函数根本不读的符号上，
# **完全无效**。测试之所以是绿的，只是因为"真实时钟"恰好晚于种子里的 2026-09-08；
# 而且 15:30 收盘切换那条分支从来没有被真正走到过（改坏了也测不出来）。
# 现在改成 patch `cn_now`，并把"收盘前/收盘后"两个时点都钉住。
_FAKE_CLOSE = datetime(2026, 9, 8, 16, 0, 0)   # 收盘后
_FAKE_INTRADAY = datetime(2026, 9, 9, 10, 0, 0)  # 次日盘中（<15:30）


def _pin_now(monkeypatch, value):  # noqa: ANN001
    """把 `auto_update_is_due` 真正读的时钟固定住。"""
    monkeypatch.setattr("app.web.main.cn_now", lambda: value)


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
    _pin_now(monkeypatch, _FAKE_CLOSE)
    _ensure_auto_update_state(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    seed.auto_update_state(state="enabled", paused=0, current_stage="idle")
    seed.trading_dates(["2026-09-08"])
    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', '2026-09-07', 10)"
    )

    assert auto_update_is_due(duckdb_store, sqlite_store) is True

    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', '2026-09-08', 11)"
    )
    assert auto_update_is_due(duckdb_store, sqlite_store) is False


def test_close_cutoff_switches_target_between_previous_and_today(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """15:30 收盘切换：**同一份数据**，收盘前不该到期、收盘后该到期。

    这条用例之前根本不存在 —— 老用例把 `datetime` patched 掉了，
    而 `auto_update_is_due()` 读的是 `cn_now()`，于是那个 15:30 分支
    从来没有被真正走到过（改坏了也测不出来）。
    """
    _ensure_auto_update_state(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    seed.auto_update_state(state="enabled", paused=0, current_stage="idle")
    # 日历里同时有 09-08 与 09-09；价格只补到 09-08
    seed.trading_dates(["2026-09-08", "2026-09-09"])
    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', '2026-09-08', 11)"
    )

    # 09-09 盘中（<15:30）：目标仍是 09-08 → 已达标 → 不该到期
    _pin_now(monkeypatch, _FAKE_INTRADAY)
    assert auto_update_is_due(duckdb_store, sqlite_store) is False

    # 09-09 收盘后：目标切成 09-09 → 还缺 → 该到期
    _pin_now(monkeypatch, datetime(2026, 9, 9, 16, 0, 0))
    assert auto_update_is_due(duckdb_store, sqlite_store) is True


def test_auto_update_not_due_when_disabled_or_paused(duckdb_store, sqlite_store, monkeypatch) -> None:
    _pin_now(monkeypatch, _FAKE_CLOSE)
    _ensure_auto_update_state(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    seed.auto_update_state(state="enabled", paused=1, current_stage="idle")
    seed.trading_dates(["2026-09-08"])
    assert auto_update_is_due(duckdb_store, sqlite_store) is False

    with sqlite_store.transaction() as conn:
        conn.execute("UPDATE auto_update_state SET state = 'disabled', paused = 0 WHERE id = 1")
    assert auto_update_is_due(duckdb_store, sqlite_store) is False


def test_auto_update_due_when_retry_task_is_ready(
    duckdb_store, sqlite_store, monkeypatch
) -> None:
    """价格已最新但存在到期 retry 时，调度器仍须启动下一轮更新。"""
    _pin_now(monkeypatch, _FAKE_CLOSE)
    _ensure_auto_update_state(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    seed.auto_update_state(state="enabled", paused=0, current_stage="idle")
    seed.trading_dates(["2026-09-08"])
    seed.retry(
        "601091", "dividends", adapter="manager", error="source error",
        retry_count=0, max_retries=5,
    )
    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) "
        "VALUES ('000001', '2026-09-08', 11)"
    )

    assert auto_update_is_due(duckdb_store, sqlite_store) is True

    with sqlite_store.transaction() as conn:
        conn.execute(
            "UPDATE retry_list SET retry_count = max_retries "
            "WHERE stock_code = '601091'"
        )
    assert auto_update_is_due(duckdb_store, sqlite_store) is False
