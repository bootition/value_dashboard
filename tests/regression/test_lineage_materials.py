from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.backfill import PriceBackfiller
from app.core.init import DataInitializer


def test_ingestion_retains_raw_response_material_by_hash(duckdb_store, sqlite_store) -> None:
    raw_response = b'{"source":"fixture"}'
    result = FetchResult(
        data=[], raw_response=raw_response,
        metadata=SourceMetadata(source="akshare_eastmoney", fetch_time=datetime.now(UTC),
                                raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate"),
    )
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    initializer._record_batch(result, "income_statement", 0)

    archive = duckdb_store.read_query(
        "SELECT source, payload FROM raw_response_archive WHERE raw_response_hash = ?", [hashlib.sha256(raw_response).hexdigest()]
    )
    assert archive == [{"source": "akshare_eastmoney", "payload": b'{"source":"fixture"}'}]


def test_xdxr_backfill_batch_archives_its_raw_response(duckdb_store, sqlite_store) -> None:
    raw_response = b'{"xdxr":true}'
    result = FetchResult(
        data=[{"event_date": "2025-01-01", "category": 1}], raw_response=raw_response,
        metadata=SourceMetadata(source="tdx", fetch_time=datetime.now(UTC),
                                raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate"),
    )
    backfiller = PriceBackfiller(duck=duckdb_store, sqlite=sqlite_store)

    backfiller._record_batch(result, "xdxr", len(result.data))

    assert duckdb_store.read_query(
        "SELECT payload FROM raw_response_archive WHERE raw_response_hash = ?", [hashlib.sha256(raw_response).hexdigest()]
    ) == [{"payload": b'{"xdxr":true}'}]
