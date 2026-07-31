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
