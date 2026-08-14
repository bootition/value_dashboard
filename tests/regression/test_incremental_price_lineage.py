from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, datetime

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
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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
    audits = duckdb_store.read_query(
        """SELECT field_name, COUNT(*) AS count FROM source_audit
           WHERE stock_code = '430001' GROUP BY field_name"""
    )
    assert audits == [{"field_name": "latest_close", "count": 2}]


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
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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


def test_non_xdxr_stock_without_local_rows_gets_full_refetch(duckdb_store, sqlite_store) -> None:
    """无本地价格的一侧必须全量拉取，不借用其他股票的全库日期。"""
    requests = []

    class Adapter:
        def fetch(self, request):
            requests.append(request)
            raw_response = b'{"price_daily":true}'
            return FetchResult(
                data=[{"trade_date": "2026-07-30", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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
    assert all(request.start_date is None for request in requests)


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
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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
    starts = {request.adjust: request.start_date for request in requests}
    assert starts == {"raw": "2026-08-01", "qfq": None}


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
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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


def test_raw_and_qfq_fetch_in_parallel(duckdb_store, sqlite_store) -> None:
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    class Adapter:
        def fetch(self, request):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            raw_response = request.adjust.encode("ascii")
            return FetchResult(
                data=[{"trade_date": "2026-08-05", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="local_cache",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
                    confidence="strict",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600000', 'PAR', 'SHSE')"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._latest_expected_trading_date = lambda today: "2026-08-05"

    report = updater._update_prices_incremental(max_stocks=1)

    assert report["success"] == 1
    assert maximum_active == 2


def test_current_prices_without_latest_lineage_are_refetched(
    duckdb_store, sqlite_store,
) -> None:
    calls: list[str] = []

    class Adapter:
        def fetch(self, request):
            calls.append(request.adjust)
            raw_response = request.adjust.encode("ascii")
            return FetchResult(
                data=[{"trade_date": "2026-08-05", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="local_cache",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
                    confidence="strict",
                ),
                raw_response=raw_response,
            )

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600002', 'LINEAGE', 'SHSE')"
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES ('600002', '2026-08-05', 9)"
        )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._latest_expected_trading_date = lambda today: "2026-08-05"
    updater._get_latest_local_price_date = lambda: "2026-08-05"

    report = updater._update_prices_incremental(max_stocks=1)

    assert report["success"] == 1
    assert set(calls) == {"raw", "qfq"}
    audits = duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM source_audit WHERE stock_code = '600002' AND field_name = 'latest_close'"
    )
    assert audits[0]["count"] == 2


def test_watchlist_stocks_are_updated_first_with_limit(duckdb_store, sqlite_store) -> None:
    calls: list[str] = []

    class Adapter:
        def fetch(self, request):
            calls.append(request.stock_codes[0])
            raw_response = request.adjust.encode("ascii")
            return FetchResult(
                data=[{"trade_date": "2026-08-05", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="local_cache",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
                    confidence="strict",
                ),
                raw_response=raw_response,
            )

    for code in ("000001", "600519"):
        duckdb_store.write_query(
            "INSERT INTO stock_meta (stock_code, name, exchange) VALUES (?, ?, 'SZSE')",
            [code, code],
        )
    sqlite_store.execute(
        "INSERT INTO watchlist (stock_code, group_name) VALUES ('600519', '重点研究')"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._latest_expected_trading_date = lambda today: "2026-08-05"

    report = updater._update_prices_incremental(max_stocks=1)

    assert report["priority_count"] == 1
    assert set(calls) == {"600519"}


def test_price_fetch_timeout_records_retry(duckdb_store, sqlite_store) -> None:
    release = threading.Event()

    class Adapter:
        def fetch(self, request):
            release.wait(timeout=2)
            raise RuntimeError("late response")

        def recover_after_timeout(self) -> None:
            pass

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600001', 'SLOW', 'SHSE')"
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater.price_fetch_timeout_seconds = 1
    updater._latest_expected_trading_date = lambda today: "2026-08-05"
    started = time.monotonic()
    try:
        report = updater._update_prices_incremental(max_stocks=1)
    finally:
        release.set()

    assert time.monotonic() - started < 1.8
    assert report["status"] == "partial"
    assert report["failed"] == 1
    retry = sqlite_store.query(
        "SELECT error FROM retry_list WHERE stock_code = '600001'"
    )
    assert "timeout after 1s" in retry[0]["error"]


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
                    source="akshare_eastmoney", fetch_time=datetime.now(UTC),
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


def test_price_refetch_rolls_back_both_adjustments_when_pair_write_fails(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    class Adapter:
        def fetch(self, request):
            raw_response = f'{{"adjust":"{request.adjust}"}}'.encode()
            return FetchResult(
                data=[{"trade_date": "2025-12-31", "close": 10.0, "volume": 100.0}],
                metadata=SourceMetadata(
                    source="local_cache", fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="strict",
                ),
                raw_response=raw_response,
            )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    original = updater._persist_price_pair_in_connection

    def fail_after_raw(connection, stock_code, raw_result, qfq_result, **kwargs):
        original(connection, stock_code, raw_result, qfq_result, **kwargs)
        raise RuntimeError("simulated pair failure")

    monkeypatch.setattr(updater, "_persist_price_pair_in_connection", fail_after_raw)

    result = updater.refetch_one("000001", "price_daily")

    assert result["status"] == "failed"
    assert duckdb_store.read_query("SELECT * FROM price_daily_raw") == []
    assert duckdb_store.read_query("SELECT * FROM price_daily_qfq") == []
    assert duckdb_store.read_query("SELECT * FROM fetch_batch") == []


def test_large_response_uses_full_replace_and_preserves_lineage(
    duckdb_store, sqlite_store,
) -> None:
    def make_result(adjust: str, rows: list[dict]) -> FetchResult:
        raw_response = (adjust + ":" + str(len(rows))).encode("ascii")
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="local_cache", fetch_time=datetime.now(UTC),
                raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
                confidence="strict",
            ),
            raw_response=raw_response,
        )

    class Adapter:
        def fetch(self, request):
            rows = [
                {"trade_date": f"2026-07-{i:02d}", "close": 10.0, "volume": 100.0}
                for i in range(1, 31)
            ]
            return make_result(request.adjust, rows)

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600600', 'REPLACE', 'SHSE')"
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES "
            "('600600', '2026-06-01', 9), ('600600', '2026-06-02', 9)"
        )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._PRICE_FULL_REPLACE_THRESHOLD = 10
    updater._latest_expected_trading_date = lambda today: "2026-08-05"

    report = updater._update_prices_incremental(max_stocks=1)

    assert report["success"] == 1
    assert report["failed"] == 0
    old_rows = duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM price_daily_raw WHERE stock_code = '600600' "
        "AND trade_date < '2026-07-01'"
    )
    assert old_rows[0]["count"] == 0, "full replace must drop replaced rows"
    audits = duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM source_audit "
        "WHERE stock_code = '600600' AND field_name = 'latest_close'"
    )
    assert audits[0]["count"] == 2


def test_full_replace_rejects_truncated_source_and_keeps_old_data(
    duckdb_store, sqlite_store,
) -> None:
    def make_result(adjust: str, rows: list[dict]) -> FetchResult:
        raw_response = (adjust + ":" + str(len(rows))).encode("ascii")
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="local_cache", fetch_time=datetime.now(UTC),
                raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
                confidence="strict",
            ),
            raw_response=raw_response,
        )

    class Adapter:
        def fetch(self, request):
            return make_result(request.adjust, [
                {"trade_date": "2026-07-01", "close": 10.0, "volume": 100.0},
            ])

    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600601', 'TRUNC', 'SHSE')"
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES "
            "('600601', '2026-06-01', 9), ('600601', '2026-06-02', 9), "
            "('600601', '2026-06-03', 9)"
        )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._PRICE_FULL_REPLACE_THRESHOLD = 0
    updater._PRICE_FULL_REPLACE_MIN_RATIO = 0.5
    updater._latest_expected_trading_date = lambda today: "2026-08-05"

    report = updater._update_prices_incremental(max_stocks=1)

    assert report["failed"] == 1
    kept = duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM price_daily_raw WHERE stock_code = '600601'"
    )
    assert kept[0]["count"] == 3, "truncated source must not replace old rows"
