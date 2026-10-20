from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater

CODES = ["600001", "600002", "600003"]


def _make_result(request: FetchRequest, data: list[dict]) -> FetchResult:
    raw = (request.data_type + ":" + str(len(data))).encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source="tdx",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw).hexdigest(),
            confidence="approximate",
        ),
        raw_response=raw,
    )


def _dividend_row(code: str) -> dict:
    return {
        "stock_code": code,
        "ex_date": "2026-06-10",
        "announcement_date": "2026-06-01",
        "dividend_per_share": 0.5,
    }


def _xdxr_row(code: str) -> dict:
    return {
        "stock_code": code,
        "event_date": "2026-06-10",
        "category": 1,
        "fenhong": 0.5,
    }


class BatchAdapter:
    """Returns one row per requested code unless rows_by_code says otherwise."""

    def __init__(self, rows_by_code: dict[str, dict[str, list[dict]]] | None = None) -> None:
        self.requests: list[FetchRequest] = []
        self.rows_by_code = rows_by_code or {}

    def fetch(self, request: FetchRequest) -> FetchResult:
        self.requests.append(request)
        data: list[dict] = []
        for code in request.stock_codes:
            per_type = self.rows_by_code.get(code)
            if per_type is not None:
                data.extend(per_type.get(request.data_type, []))
            elif request.data_type == "dividends":
                data.append(_dividend_row(code))
            else:
                data.append(_xdxr_row(code))
        return _make_result(request, data)


def _seed(duckdb_store) -> None:
    for code in CODES:
        duckdb_store.write_query(
            f"INSERT INTO stock_meta (stock_code, name, exchange) "
            f"VALUES ('{code}', 'TEST', 'SHSE')"
        )


def test_market_actions_batches_one_request_per_data_type(duckdb_store, sqlite_store) -> None:
    """3 只股票 2 个数据域只应产生 2 次请求（而不是逐股 6 次）。"""
    _seed(duckdb_store)
    adapter = BatchAdapter()
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter,
    )
    progress: list[dict] = []

    report = updater._refresh_market_actions(
        CODES, detail_cb=lambda _step, info: progress.append(info),
    )

    assert report["status"] == "success"
    assert report["total"] == 3
    assert report["success"] == 3
    dividend_requests = [r for r in adapter.requests if r.data_type == "dividends"]
    xdxr_requests = [r for r in adapter.requests if r.data_type == "xdxr"]
    assert len(dividend_requests) == 1
    assert len(xdxr_requests) == 1
    assert sorted(dividend_requests[0].stock_codes) == CODES
    assert sorted(xdxr_requests[0].stock_codes) == CODES

    dividend_rows = duckdb_store.read_query(
        "SELECT stock_code, COUNT(*) AS count FROM dividends GROUP BY stock_code"
    )
    assert {row["stock_code"] for row in dividend_rows} == set(CODES)
    xdxr_rows = duckdb_store.read_query(
        "SELECT stock_code, COUNT(*) AS count FROM xdxr GROUP BY stock_code"
    )
    assert {row["stock_code"] for row in xdxr_rows} == set(CODES)
    assert duckdb_store.read_query("SELECT COUNT(*) AS count FROM fetch_batch")[0]["count"] == 2
    audit_codes = {
        row["stock_code"]
        for row in duckdb_store.read_query("SELECT DISTINCT stock_code FROM source_audit")
    }
    assert audit_codes == set(CODES)
    assert [item["done"] for item in progress] == [1, 2, 3]
    assert all(item["total"] == 3 for item in progress)


def test_market_actions_records_retry_when_batch_misses_a_stock(
    duckdb_store, sqlite_store,
) -> None:
    """批内没有记录的股票要逐股复核，失败必须进 retry_list（不再静默丢弃）。"""
    _seed(duckdb_store)
    adapter = BatchAdapter(rows_by_code={
        CODES[0]: {"dividends": [_dividend_row(CODES[0])], "xdxr": [_xdxr_row(CODES[0])]},
        CODES[1]: {"dividends": [], "xdxr": []},
        CODES[2]: {"dividends": [], "xdxr": []},
    })
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter,
    )

    report = updater._refresh_market_actions(CODES)

    assert report["status"] == "partial"
    assert report["success"] == 1
    assert report["failed"] == 2
    assert set(report["failed_codes"]) == {CODES[1], CODES[2]}
    retries = sqlite_store.query(
        "SELECT stock_code, data_type FROM retry_list ORDER BY stock_code, data_type"
    )
    assert len(retries) == 4
    assert {row["stock_code"] for row in retries} == {CODES[1], CODES[2]}
    assert {row["data_type"] for row in retries} == {"dividends", "xdxr"}
    # 逐股复核确实发生：每个数据域 1 次批量请求 + 2 只缺失股票各 1 次
    assert len([r for r in adapter.requests if r.data_type == "dividends"]) == 3
    assert len([r for r in adapter.requests if r.data_type == "xdxr"]) == 3
