from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_ingestion_records_normalized_field_level_lineage(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    result = FetchResult(
        data=[],
        metadata=SourceMetadata(
            source="akshare_eastmoney", fetch_time=datetime.now(UTC),
            raw_response_hash="a" * 64, confidence="approximate",
        ),
    )

    initializer._record_field_audit(
        result,
        [{"REPORT_DATE": "2025-12-31", "OPERATE_INCOME": 120.0, "PARENT_NETPROFIT": 12.0}],
        stock_code="000001",
    )

    rows = duckdb_store.read_query(
        "SELECT field_name, report_date, value FROM source_audit ORDER BY field_name"
    )
    assert rows == [
        {"field_name": "parent_net_profit", "report_date": date(2025, 12, 31), "value": 12.0},
        {"field_name": "revenue", "report_date": date(2025, 12, 31), "value": 120.0},
    ]


def test_field_audit_references_the_unique_batch_created_for_its_fetch(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    raw_response = b'{"income_statement":true}'
    result = FetchResult(
        data=[],
        metadata=SourceMetadata(
            source="akshare_eastmoney", fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="approximate",
        ),
        raw_response=raw_response,
    )

    initializer._record_batch(result, "income_statement", 1)
    batch_id = initializer._last_fetch_batch_id
    initializer._record_field_audit(
        result, [{"REPORT_DATE": "2025-12-31", "OPERATE_INCOME": 10.0}], stock_code="000001"
    )
    initializer._record_batch(result, "income_statement", 1)

    assert batch_id != initializer._last_fetch_batch_id
    assert duckdb_store.read_query("SELECT fetch_batch_id FROM source_audit")[0]["fetch_batch_id"] == batch_id
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM fetch_batch WHERE batch_id = ?", [batch_id]
    )[0]["count"] == 1
