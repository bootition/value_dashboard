"""Regression: 日节流标记必须按中国交易日归一，不能直接截 UTC 字符串。

2026-11-12 生产/回归发现：`_mark_refreshed()` 用 **UTC ISO** 写标记，而
`_refresh_due()` 旧实现取字符串前 10 位（= UTC 日期）与
`datetime.now(CN_TZ).date()` 相减。在北京时间 00:00–08:00 区间，UTC 日期比
本地日期早一天，于是"今天刚刷新过"被判为"已过期"：

* 生产：每次凌晨的自动更新都会重跑一次约 104s 的 universe 全量网络刷新；
* 回归：跨过本地零点后 `test_incremental_update_scope` 两条用例假失败
  （`stock_list` 由 skipped 变 failed、`stock_codes` 由缺口集变 None）。

修复：统一按 `_CN_TZ` 归一后再比较日期。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.core.financial_period import CN_TZ
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater


def test_marker_is_normalized_to_china_trading_date() -> None:
    normalize = IncrementalUpdater._marker_cn_date
    # UTC 16:30 == 北京时间次日 00:30：旧实现会取到 11-12，正确答案是 11-13。
    assert normalize("2026-11-12T16:30:00+00:00").date() == date(2026, 11, 13)
    # UTC 15:59 == 北京时间 23:59 同一天。
    assert normalize("2026-11-12T15:59:00+00:00").date() == date(2026, 11, 12)
    # 已带 +08:00 偏移的时间戳按自身日期。
    assert normalize("2026-11-12T09:00:00+08:00").date() == date(2026, 11, 12)
    # 纯日期字符串（历史标记）按日期解释。
    assert normalize("2026-11-12").date() == date(2026, 11, 12)
    # 无法解析的标记视为没有标记，由调用方判定为"到期"。
    assert normalize("not-a-timestamp") is None
    assert normalize("") is None
    assert normalize(None) is None


def _write_marker(sqlite_store: SQLiteStore, key: str, value: str) -> None:
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES (?, ?, ?)""",
        [key, value, value],
    )


def test_daily_marker_written_in_utc_is_not_due_within_the_day(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _write_marker(sqlite_store, "stock_list_last_refresh", datetime.now(UTC).isoformat())
    assert updater._refresh_due("stock_list_last_refresh", 1) is False

    _write_marker(sqlite_store, "listing_info_last_refresh", datetime.now(CN_TZ).isoformat())
    assert updater._refresh_due("listing_info_last_refresh", 1) is False


def test_stale_or_invalid_markers_are_due(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    _write_marker(sqlite_store, "stock_list_last_refresh", stale)
    assert updater._refresh_due("stock_list_last_refresh", 1) is True

    _write_marker(sqlite_store, "listing_info_last_refresh", "garbage")
    assert updater._refresh_due("listing_info_last_refresh", 1) is True

    assert updater._refresh_due("never_written_key", 1) is True
