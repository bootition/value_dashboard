from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater


class PriceAdapter:
    def fetch(self, request: FetchRequest) -> FetchResult:
        assert request.data_type == "price_daily"
        assert request.adjust == "qfq"
        payload = b'{"trade_date":"2026-07-20","close":11.0}'
        return FetchResult(
            data=[
                {
                    "trade_date": "2026-07-20",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 100.0,
                    "turnover": 1100.0,
                    "turnover_rate": 1.5,
                }
            ],
            metadata=SourceMetadata(
                source="akshare_eastmoney",
                fetch_time=datetime.now(timezone.utc),
                raw_response_hash=hashlib.sha256(payload).hexdigest(),
                confidence="approximate",
            ),
            raw_response=payload,
        )


def test_retry_schema_records_request_metadata(sqlite_store: SQLiteStore) -> None:
    columns = sqlite_store.query("PRAGMA table_info(retry_list)")

    assert "extra_json" in {row["name"] for row in columns}


def test_qfq_retry_persists_before_queue_removal(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO retry_list
                (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            [
                "600519",
                "price_daily",
                "akshare_eastmoney",
                "temporary",
                datetime.now(timezone.utc).isoformat(),
                json.dumps({"adjust": "qfq"}),
            ],
        )
        retry_id = cursor.lastrowid

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = PriceAdapter()
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store

    tasks = sqlite_store.query(
        "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json FROM retry_list"
    )
    report = updater._retry_failed_tasks(tasks)

    prices = duckdb_store.read_query(
        """
        SELECT close, turnover_rate FROM price_daily_qfq
        WHERE stock_code = '600519' AND trade_date = '2026-07-20'
        """
    )
    remaining = sqlite_store.query("SELECT id FROM retry_list WHERE id = ?", [retry_id])
    assert report["status"] == "success"
    assert prices == [{"close": 11.0, "turnover_rate": 1.5}]
    assert remaining == []


def test_retry_is_retained_when_target_persistence_is_unsupported(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO retry_list
                (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
            VALUES ('600519', 'stock_list', 'akshare_eastmoney', 'temporary', 0, ?, '{}')
            """,
            [datetime.now(timezone.utc).isoformat()],
        )
        retry_id = cursor.lastrowid

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = PriceAdapter()
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store

    tasks = sqlite_store.query(
        "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json FROM retry_list"
    )
    report = updater._retry_failed_tasks(tasks)

    remaining = sqlite_store.query(
        "SELECT retry_count FROM retry_list WHERE id = ?",
        [retry_id],
    )
    assert report["status"] == "failed"
    assert remaining == [{"retry_count": 1}]
