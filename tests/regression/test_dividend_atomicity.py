from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.backfill import PriceBackfiller
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


class DividendAdapter:
    def __init__(self, result: FetchResult) -> None:
        self._result = result

    def fetch(self, request: FetchRequest) -> FetchResult:
        assert request.data_type == "dividends"
        return self._result


def _result(
    rows: list[dict[str, str | float | None]],
    error: str | None = None,
) -> FetchResult:
    raw_response = b'{"dividends":true}'
    return FetchResult(
        data=rows,
        metadata=SourceMetadata(
            source="baostock",
            fetch_time=datetime.now(timezone.utc),
            raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
            confidence="missing" if error else "approximate",
            error=error,
        ),
        raw_response=raw_response,
    )


def _backfiller(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    result: FetchResult,
) -> PriceBackfiller:
    duckdb_store.write_query(
        """
        INSERT INTO stock_meta (stock_code, name, exchange)
        VALUES ('600519', '贵州茅台', 'SSE')
        """
    )
    backfiller = PriceBackfiller.__new__(PriceBackfiller)
    backfiller.adapter_mgr = DividendAdapter(result)
    backfiller.duck = duckdb_store
    backfiller.sqlite = sqlite_store
    backfiller._batch_id = "test-batch"
    return backfiller


def test_dividend_fetch_failure_records_retry(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    backfiller = _backfiller(
        duckdb_store,
        sqlite_store,
        _result([], error="source unavailable"),
    )

    report = backfiller._backfill_dividends(max_stocks=1)

    retries = sqlite_store.query(
        "SELECT data_type, error FROM retry_list WHERE stock_code = '600519'"
    )
    assert report["status"] == "failed"
    assert retries == [{"data_type": "dividends", "error": "source unavailable"}]


def test_dividend_without_ex_date_records_missing(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    backfiller = _backfiller(
        duckdb_store,
        sqlite_store,
        _result([{"ex_date": None, "dividend_per_share": 1.0}]),
    )

    report = backfiller._backfill_dividends(max_stocks=1)

    missing = sqlite_store.query(
        """
        SELECT field_name, reason_code FROM missing_list
        WHERE stock_code = '600519'
        """
    )
    assert report["status"] == "partial"
    assert missing == [{"field_name": "dividends", "reason_code": "missing_ex_date"}]


def test_dividend_batch_rolls_back_when_any_row_is_invalid(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    backfiller = _backfiller(
        duckdb_store,
        sqlite_store,
        _result(
            [
                {"ex_date": "2025-06-30", "dividend_per_share": 1.0},
                {"ex_date": "not-a-date", "dividend_per_share": 2.0},
            ]
        ),
    )

    report = backfiller._backfill_dividends(max_stocks=1)

    rows = duckdb_store.read_query(
        "SELECT ex_date FROM dividends WHERE stock_code = '600519'"
    )
    retries = sqlite_store.query(
        "SELECT data_type FROM retry_list WHERE stock_code = '600519'"
    )
    assert report["status"] == "failed"
    assert rows == []
    assert retries == [{"data_type": "dividends"}]


def test_dividend_backfill_preserves_verified_values_when_response_is_sparse(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', '2025-06-30', '2025-05-01', 1.5)"""
    )
    backfiller = _backfiller(
        duckdb_store, sqlite_store, _result([{"ex_date": "2025-06-30", "dividend_per_share": None}]),
    )

    assert backfiller._backfill_dividends(max_stocks=1)["status"] == "success"
    row = duckdb_store.read_query(
        "SELECT announcement_date, dividend_per_share FROM dividends WHERE stock_code = '600519'"
    )[0]
    assert str(row["announcement_date"]) == "2025-05-01"
    assert row["dividend_per_share"] == 1.5
