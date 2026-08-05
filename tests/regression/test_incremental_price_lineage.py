from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater


def test_price_refetch_records_field_lineage_after_persisting(
    duckdb_store,
    sqlite_store,
) -> None:
    class Adapter:
        def fetch(self, request):
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2025-12-31", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
                ),
                raw_response=raw_response,
            )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    result = updater.refetch_one("000001", "price_daily")

    assert result["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT field_name, value, confidence FROM source_audit WHERE stock_code = '000001' ORDER BY field_name"
    )
    assert {row["field_name"] for row in rows} >= {"latest_close", "volume"}
    assert {row["confidence"] for row in rows} == {"approximate"}
    assert duckdb_store.read_query("SELECT COUNT(*) AS count FROM fetch_batch")[0]["count"] == 2


def test_incremental_bse_price_update_requires_qfq(duckdb_store, sqlite_store) -> None:
    requests = []

    class Adapter:
        def fetch(self, request):
            requests.append(request)
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2025-12-31", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('430001', 'BSE', 'BSE')"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._get_latest_local_price_date = lambda: "2025-12-30"

    result = updater._update_prices_incremental(max_stocks=1)

    assert result["success"] == 1
    assert [request.adjust for request in requests] == ["raw", "qfq"]
    assert duckdb_store.read_query("SELECT close FROM price_daily_raw WHERE stock_code = '430001'") == [{"close": 10.0}]
    assert duckdb_store.read_query("SELECT close FROM price_daily_qfq WHERE stock_code = '430001'") == [{"close": 10.0}]


def test_xdxr_stock_gets_full_qfq_refetch(duckdb_store, sqlite_store) -> None:
    """发生除权除息的股票，qfq 必须全历史重拉（start_date=None），保证复权口径一致。"""
    requests = []

    class Adapter:
        def fetch(self, request):
            requests.append(request)
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2025-12-31", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('000001', 'PA', 'SZSE')"
    )
    duckdb_store.write_query(
        "INSERT INTO xdxr (stock_code, event_date, category) VALUES ('000001', '2026-07-30', 1)"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._get_latest_local_price_date = lambda: "2026-07-29"

    result = updater._update_prices_incremental(max_stocks=1)

    assert result["success"] == 1
    assert result["xdxr_full_refetch"] == 1
    # xdxr 股票的 raw 与 qfq 请求都不带 start_date（全历史重拉）
    assert all(request.start_date is None for request in requests)
    assert result["status"] == "success"


def test_non_xdxr_stock_keeps_incremental_window(duckdb_store, sqlite_store) -> None:
    """无除权事件的股票保持增量窗口（start_date = 本地最新日期）。"""
    requests = []

    class Adapter:
        def fetch(self, request):
            requests.append(request)
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2026-07-30", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('000002', 'VANKE', 'SZSE')"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._get_latest_local_price_date = lambda: "2026-07-29"

    result = updater._update_prices_incremental(max_stocks=1)

    assert result["success"] == 1
    assert result["xdxr_full_refetch"] == 0
    assert all(request.start_date == "2026-07-29" for request in requests)


def test_partial_resume_uses_oldest_target_date_for_xdxr_window(
    duckdb_store, sqlite_store,
) -> None:
    requests = []

    class Adapter:
        def fetch(self, request):
            requests.append(request)
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2026-08-05", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('000004', 'RESUME', 'SZSE')"
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES ('000004', '2026-08-01', 9)"
        )
    duckdb_store.write_query(
        "INSERT INTO xdxr (stock_code, event_date, category) VALUES ('000004', '2026-08-02', 1)"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._latest_expected_trading_date = lambda today: "2026-08-05"
    updater._get_latest_local_price_date = lambda: "2026-08-05"

    result = updater._update_prices_incremental(max_stocks=1)

    assert result["xdxr_full_refetch"] == 1
    assert all(request.start_date is None for request in requests)


def test_stale_local_price_triggers_raw_full_refetch(duckdb_store, sqlite_store) -> None:
    """本地价格陈旧超过窗口上限（默认 30 天）时，raw 也整段重拉。"""
    requests = []

    class Adapter:
        def fetch(self, request):
            requests.append(request)
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2026-07-30", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('000003', 'TEST', 'SZSE')"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._get_latest_local_price_date = lambda: "2026-06-01"

    result = updater._update_prices_incremental(max_stocks=1)

    assert result["success"] == 1
    assert result["raw_full_refetch"] is True
    assert all(request.start_date is None for request in requests)


def test_get_xdxr_codes_since_returns_only_matching_stocks(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    duckdb_store.write_query(
        "INSERT INTO xdxr (stock_code, event_date, category) VALUES "
        "('000001', '2026-07-30', 1), ('000002', '2026-06-01', 1)"
    )

    codes = updater._get_xdxr_codes_since("2026-07-01")

    assert codes == {"000001"}


def test_expected_price_date_waits_for_market_close(duckdb_store, sqlite_store) -> None:
    sqlite_store.execute("CREATE TABLE IF NOT EXISTS trading_dates (trade_date TEXT PRIMARY KEY)")
    sqlite_store.execute("INSERT INTO trading_dates VALUES ('2026-08-04'), ('2026-08-05')")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    before_close = updater._latest_expected_trading_date(
        "2026-08-05", now=datetime(2026, 8, 5, 14, 0),
    )
    after_close = updater._latest_expected_trading_date(
        "2026-08-05", now=datetime(2026, 8, 5, 16, 0),
    )

    assert before_close == "2026-08-04"
    assert after_close == "2026-08-05"


def test_price_refetch_rolls_back_rows_when_source_material_is_invalid(duckdb_store, sqlite_store) -> None:
    class Adapter:
        def fetch(self, request):
            return FetchResult(
                data=[{"trade_date": "2025-12-31", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(b"expected").hexdigest(), confidence="approximate",
                ),
                raw_response=b"tampered",
            )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())

    result = updater.refetch_one("000001", "price_daily")

    assert result["status"] == "failed"
    assert duckdb_store.read_query("SELECT * FROM price_daily_raw") == []
    assert duckdb_store.read_query("SELECT * FROM price_daily_qfq") == []
    assert duckdb_store.read_query("SELECT * FROM fetch_batch") == []
