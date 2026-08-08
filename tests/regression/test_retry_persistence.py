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


def test_price_retry_refetches_incrementally_from_local_latest(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    seen_requests: list[FetchRequest] = []

    class TrackingAdapter:
        def fetch(self, request: FetchRequest) -> FetchResult:
            seen_requests.append(request)
            payload = b'{"trade_date":"2026-08-05","close":11.0}'
            return FetchResult(
                data=[
                    {
                        "trade_date": "2026-08-05",
                        "open": 10.0, "high": 12.0, "low": 9.0,
                        "close": 11.0, "volume": 100.0,
                        "turnover": 1100.0, "turnover_rate": 1.5,
                    }
                ],
                metadata=SourceMetadata(
                    source="tencent",
                    fetch_time=datetime.now(timezone.utc),
                    raw_response_hash=hashlib.sha256(payload).hexdigest(),
                    confidence="approximate",
                ),
                raw_response=payload,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600519', 'MT', 'SHSE')"
    )
    duckdb_store.write_query(
        "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES "
        "('600519', '2026-08-01', 10)"
    )
    with sqlite_store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO retry_list
                (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
            VALUES (?, 'price_daily', 'tencent', 'temporary', 0, ?, ?)
            """,
            [
                "600519",
                datetime.now(timezone.utc).isoformat(),
                json.dumps({"adjust": "qfq"}),
            ],
        )
        retry_id = cursor.lastrowid

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = TrackingAdapter()
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store

    tasks = sqlite_store.query(
        "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json FROM retry_list"
    )
    report = updater._retry_failed_tasks(tasks)

    assert report["status"] == "success"
    assert len(seen_requests) == 1
    assert seen_requests[0].start_date == "2026-08-01"
    assert seen_requests[0].end_date == datetime.now().strftime("%Y-%m-%d")
    assert sqlite_store.query("SELECT id FROM retry_list WHERE id = ?", [retry_id]) == []


def test_cleanup_redundant_retries_drops_up_to_date_entries(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    def seed(code: str) -> None:
        duckdb_store.write_query(
            "INSERT INTO stock_meta (stock_code, name, exchange) VALUES (?, ?, 'SHSE')",
            [code, code],
        )
        for table in ("price_daily_raw", "price_daily_qfq"):
            duckdb_store.write_query(
                f"INSERT INTO {table} (stock_code, trade_date, close) VALUES (?, '2026-08-05', 10)",
                [code],
            )
            payload = f"{code}:{table}".encode("ascii")
            digest = hashlib.sha256(payload).hexdigest()
            batch_id = f"b-{code}-{table}"
            duckdb_store.write_query(
                "INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload) "
                "VALUES (?, 'tencent', CURRENT_TIMESTAMP, ?)",
                [digest, payload],
            )
            duckdb_store.write_query(
                "INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time, "
                "raw_response_hash, row_count, confidence) VALUES (?, ?, 'tencent', '1', "
                "CURRENT_TIMESTAMP, ?, 1, 'strict')",
                [batch_id, table, digest],
            )
            duckdb_store.write_query(
                "INSERT INTO source_audit (stock_code, field_name, report_date, value, source, "
                "fetch_batch_id, fetch_time, raw_response_hash, confidence) "
                "VALUES (?, 'latest_close', '2026-08-05', 10, 'tencent', ?, CURRENT_TIMESTAMP, ?, 'strict')",
                [code, batch_id, digest],
            )
        sqlite_store.execute(
            "INSERT INTO retry_list (stock_code, data_type, adapter, error, retry_count, last_attempt) "
            "VALUES (?, 'price_daily', 'local_cache', 'no available adapter', 0, ?)",
            [code, datetime.now(timezone.utc).isoformat()],
        )

    seed("600001")
    seed("600002")

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    cleaned = updater._cleanup_redundant_retries("2026-08-05")

    assert cleaned == 2
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM retry_list")[0]["c"] == 0
