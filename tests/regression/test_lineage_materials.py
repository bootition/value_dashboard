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


def test_duplicate_raw_response_is_archived_once(duckdb_store, sqlite_store) -> None:
    raw_response = b'{"source":"duplicate"}'
    result = FetchResult(
        data=[], raw_response=raw_response,
        metadata=SourceMetadata(source="akshare_eastmoney", fetch_time=datetime.now(UTC),
                                raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate"),
    )
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    initializer._record_batch(result, "income_statement", 0)
    initializer._record_batch(result, "income_statement", 0)

    digest = hashlib.sha256(raw_response).hexdigest()
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM raw_response_archive_all WHERE raw_response_hash = ?", [digest]
    ) == [{"count": 1}]


def test_raw_archive_rotates_on_threshold_and_view_stays_complete(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    from app.core.storage import duckdb_store as archive_module

    monkeypatch.setattr(archive_module, "_RAW_ARCHIVE_ROTATE_BYTES", 1)
    monkeypatch.setattr(archive_module, "_RAW_ARCHIVE_ROTATE_ROWS", 1)
    monkeypatch.setattr(archive_module, "_RAW_ARCHIVE_ROTATE_DAYS", 0)
    with duckdb_store.transaction() as conn:
        archive_module.archive_raw_response_if_absent(
            conn,
            raw_response_hash="c" * 64,
            source="sina",
            fetch_time=datetime.now(UTC),
            payload=b"first-payload",
            api_version=None,
        )
    partitions = duckdb_store.read_query(
        "SELECT partition_table, closed_at FROM raw_response_archive_partitions ORDER BY created_at"
    )
    names = [row["partition_table"] for row in partitions]
    assert "raw_response_archive" in names
    active = next(row for row in partitions if row["partition_table"] == "raw_response_archive")
    assert active["closed_at"] is None
    assert any(
        name.startswith("raw_response_archive_20") and name != "raw_response_archive"
        for name in names
    )
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM raw_response_archive_all WHERE raw_response_hash = ?", ["c" * 64]
    ) == [{"c": 1}]
