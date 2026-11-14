"""2026-11-13 P1（2）：capital_history 的 due 判据必须有"最近核验时间水位"。

旧判据只有 ``h.latest_cap < latest_price``（股本链最新**事件日** vs 最新价格日）。
股本变化一年才几次，所以几乎全部 5,572 只每天每轮都 due → 每轮固定 20 只、
永远追不上，且每轮还会让统计域重算那 20 只。

新判据：``capital_cross_cache.main_fetched_at`` 在最近 7 天内（无论主链成功还是
失败）→ 本轮不 due；失败交给 retry_list（有 max_retries 上限）。无缓存/无记录
照常 due；``latest_cap >= latest_price`` 的原有"不 due"语义不变。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.capital import CapitalHistoryUpdater

PRICE_DATE = date(2026, 11, 13)


class _NoopAdapter:
    """due 判定不应触发任何抓取。"""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request) -> FetchResult:
        self.calls += 1
        return FetchResult(
            data=[],
            metadata=SourceMetadata(
                source="cninfo_capital",
                fetch_time=datetime.now(UTC),
                raw_response_hash=hashlib.sha256(b"noop").hexdigest(),
                confidence="missing",
            ),
            raw_response=b"noop",
        )


def _seed_stock(duck, code: str = "600519") -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES (?, ?, 'SSE', true)",
        [code, code],
    )


def _seed_price(duck, code: str, trade_date: date = PRICE_DATE) -> None:
    duck.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, 10.0)",
        [code, trade_date],
    )


def _seed_capital(duck, code: str, entries: list[date]) -> None:
    for effective_date in entries:
        duck.write_query(
            """INSERT INTO share_capital_history
               (stock_code, effective_date, total_shares, change_reason, is_anchor,
                verified, source, raw_hash, batch_id)
               VALUES (?, ?, 1000000000.0, NULL, true, true,
                       'cninfo_capital', 'x', 'b1')""",
            [code, effective_date],
        )


def _seed_cross_cache(
    sqlite,
    code: str,
    *,
    main_fetched_at: datetime | None,
    main_status: str | None = "ok",
    cross_status: str = "verified",
) -> None:
    sqlite.execute(
        """INSERT INTO capital_cross_cache
               (stock_code, events_json, verified_points, total_points, fetched_at,
                cross_status, main_fetched_at, main_status)
           VALUES (?, '[]', 0, 0, ?, ?, ?, ?)""",
        [
            code,
            datetime.now(UTC).isoformat(),
            cross_status,
            main_fetched_at.isoformat() if main_fetched_at else None,
            main_status,
        ],
    )


def _updater(duck, sqlite) -> CapitalHistoryUpdater:
    updater = CapitalHistoryUpdater(duck=duck, sqlite=sqlite, adapter=_NoopAdapter())
    return updater


def test_main_chain_verified_within_ttl_is_not_due(duckdb_store, sqlite_store) -> None:
    """①7 天内核验过（main_status=ok）→ 不 due。"""
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    _seed_capital(duckdb_store, "600519", [date(2025, 12, 31)])
    updater = _updater(duckdb_store, sqlite_store)
    _seed_cross_cache(
        sqlite_store, "600519",
        main_fetched_at=datetime.now(UTC) - timedelta(days=3),
    )

    assert updater._due_stock_codes() == []


def test_main_chain_verified_eight_days_ago_is_due(duckdb_store, sqlite_store) -> None:
    """②8 天前核验过 → 水位过期 → due（主链锚点早于最新价格，需要周期复刷）。"""
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    _seed_capital(duckdb_store, "600519", [date(2025, 12, 31)])
    updater = _updater(duckdb_store, sqlite_store)
    _seed_cross_cache(
        sqlite_store, "600519",
        main_fetched_at=datetime.now(UTC) - timedelta(days=8),
    )

    assert updater._due_stock_codes() == ["600519"]


def test_stock_without_any_cache_is_due(duckdb_store, sqlite_store) -> None:
    """③无缓存/无记录 → 照常 due（新股/首次回填不能被水位挡住）。"""
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)

    assert "600519" in updater._due_stock_codes()


def test_capital_covering_latest_price_is_not_due(duckdb_store, sqlite_store) -> None:
    """④latest_cap >= latest_price → 不 due（原有语义不变）。

    同时必须给一条 verified 交叉缓存，否则会从 no_cache 通道重新入选。
    """
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    _seed_capital(duckdb_store, "600519", [PRICE_DATE])
    updater = _updater(duckdb_store, sqlite_store)
    _seed_cross_cache(sqlite_store, "600519", main_fetched_at=None, main_status=None)

    assert updater._due_stock_codes() == []


def test_failed_main_chain_within_ttl_is_not_due(duckdb_store, sqlite_store) -> None:
    """主链失败（main_status=error）但 7 天内核验过 → 不进 due 扫描。

    失败由 retry_list 负责（max_retries 上限）；否则同一批队头股票每 30 分钟
    重新入选，"每轮 20 只"永远推进不动。
    """
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    _seed_capital(duckdb_store, "600519", [date(2025, 12, 31)])
    updater = _updater(duckdb_store, sqlite_store)
    _seed_cross_cache(
        sqlite_store, "600519",
        main_fetched_at=datetime.now(UTC) - timedelta(hours=1),
        main_status="error",
    )

    assert updater._due_stock_codes() == []


def test_legacy_row_without_main_status_is_still_watermarked(
    duckdb_store, sqlite_store,
) -> None:
    """历史行只有 main_fetched_at、没有 main_status → 也要被水位挡住。

    旧代码只在 ``main_status == "ok"`` 时才看 TTL，这类行每一轮都 due。
    """
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    _seed_capital(duckdb_store, "600519", [date(2025, 12, 31)])
    updater = _updater(duckdb_store, sqlite_store)
    _seed_cross_cache(
        sqlite_store, "600519",
        main_fetched_at=datetime.now(UTC) - timedelta(days=2),
        main_status=None,
    )

    assert updater._due_stock_codes() == []


def test_naive_timestamp_does_not_break_due_computation(
    duckdb_store, sqlite_store,
) -> None:
    """不带时区的历史时间戳不得抛 TypeError 炸掉整条 due 计算。"""
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    _seed_capital(duckdb_store, "600519", [date(2025, 12, 31)])
    updater = _updater(duckdb_store, sqlite_store)
    naive = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)).isoformat()
    _seed_cross_cache(sqlite_store, "600519", main_fetched_at=None, main_status=None)
    sqlite_store.execute(
        "UPDATE capital_cross_cache SET main_fetched_at = ?, main_status = 'ok' "
        "WHERE stock_code = '600519'",
        [naive],
    )

    assert updater._due_stock_codes() == []


def test_noop_adapter_is_never_called_by_due_probe(duckdb_store, sqlite_store) -> None:
    """due 判定是纯读：不得触发任何抓取。"""
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519")
    adapter = _NoopAdapter()
    updater = CapitalHistoryUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter=adapter)

    updater._due_stock_codes()

    assert adapter.calls == 0
