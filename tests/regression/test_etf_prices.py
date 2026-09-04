"""ETF 行情采集器（同花顺）回归（2026-09-05）

覆盖：日线与跟踪分位同日期合并、失败保旧值+retry、missing 语义、
update_all 汇总、status_report。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.etf_prices import EtfPriceUpdater
from app.core.etf_strategy import upsert_etf_meta
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _make_result(data, error=None, source="ths"):
    raw = json.dumps(data, ensure_ascii=False).encode()
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source=source,  # type: ignore[arg-type]
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw).hexdigest(),
            confidence="approximate" if error is None else "missing",
            error=error,
        ),
        raw_response=raw,
    )


class FakeThs:
    def __init__(self, *, daily_error=None, track_error=None, with_track=True) -> None:
        self.daily_error = daily_error
        self.track_error = track_error
        self.with_track = with_track
        self.calls: list[str] = []

    def fetch(self, request: FetchRequest):
        self.calls.append(request.data_type)
        if request.data_type == "etf_daily":
            if self.daily_error:
                return _make_result([], error=self.daily_error)
            return _make_result([
                {"trade_date": "2026-09-03", "close_price": 1.1, "open_price": 1.08,
                 "high_price": 1.12, "low_price": 1.07, "volume": 100, "turnover": 110},
                {"trade_date": "2026-09-04", "close_price": 1.15, "open_price": 1.1,
                 "high_price": 1.16, "low_price": 1.09, "volume": 120, "turnover": 138},
            ])
        if self.track_error:
            return _make_result([], error=self.track_error)
        if not self.with_track:
            return _make_result([])
        return _make_result([
            {"trade_date": "2026-09-03",
             "track_index_pe_ttm_five_year_percentile": 25.0},
            {"trade_date": "2026-09-04",
             "track_index_pe_ttm_five_year_percentile": 27.0},
        ])


def test_update_etf_merges_track_percentile(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    upsert_etf_meta(sqlite_store, etf_code="513130", name="恒生科技",
                    track_index_code=None, primary_metric="pe")
    updater = EtfPriceUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._ths = FakeThs()  # type: ignore[assignment]

    report = updater.update_etf("513130")

    assert report["status"] == "success"
    assert report["rows"] == 2
    assert report["track_rows"] == 2
    rows = duckdb_store.read_query(
        "SELECT trade_date, close_price, track_pe_ttm_five_year_percentile "
        "FROM etf_daily WHERE etf_code='513130' ORDER BY trade_date"
    )
    assert rows[0]["close_price"] == 1.1
    assert rows[0]["track_pe_ttm_five_year_percentile"] == 25.0
    assert rows[1]["track_pe_ttm_five_year_percentile"] == 27.0


def test_update_etf_failure_preserves_old_and_records_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    upsert_etf_meta(sqlite_store, etf_code="513130", name="恒生科技",
                    track_index_code=None, primary_metric="pe")
    duckdb_store.write_query(
        """INSERT INTO etf_daily
           (etf_code, trade_date, close_price, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('513130', '2026-09-01', 1.0, 'ths', CURRENT_TIMESTAMP, 'old', 'strict', 'b')"""
    )
    updater = EtfPriceUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._ths = FakeThs(daily_error="quota exhausted")  # type: ignore[assignment]

    report = updater.update_etf("513130")

    assert report["status"] == "failed"
    rows = duckdb_store.read_query(
        "SELECT close_price FROM etf_daily WHERE etf_code='513130'"
    )
    assert len(rows) == 1 and rows[0]["close_price"] == 1.0, "失败必须保留旧值"
    retries = sqlite_store.query(
        "SELECT adapter FROM retry_list WHERE stock_code='513130' AND data_type='etf_daily'"
    )
    assert retries and retries[0]["adapter"] == "ths"


def test_update_all_summary_and_status(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    upsert_etf_meta(sqlite_store, etf_code="513130", name="恒生科技",
                    track_index_code=None, primary_metric="pe")
    updater = EtfPriceUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._ths = FakeThs()  # type: ignore[assignment]

    report = updater.update_all()
    assert report["status"] == "success"
    assert report["summary"]["success"] == 1

    status = updater.status_report()
    assert status["status"] == "ok"
    assert status["coverage"][0]["etf_code"] == "513130"
    assert status["coverage"][0]["rows"] == 2
    assert status["coverage"][0]["track_rows"] == 2


def test_track_dates_without_daily_rows_get_independent_rows(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """QDII（如 513130）：跟踪分位日期与行情日期不重叠时，
    补写 close=NULL 的独立分位行，不污染价格、保证最新分位可查。"""
    upsert_etf_meta(sqlite_store, etf_code="513130", name="恒生科技",
                    track_index_code=None, primary_metric="pe")
    updater = EtfPriceUpdater(duck=duckdb_store, sqlite=sqlite_store)

    class QdiiFakeThs:
        def fetch(self, request: FetchRequest):
            if request.data_type == "etf_daily":
                return _make_result([
                    {"trade_date": "2026-09-04", "close_price": 0.56},
                ])
            return _make_result([
                {"trade_date": "2026-09-03",
                 "track_index_pe_ttm_five_year_percentile": 9.0},
                {"trade_date": "2026-09-04",
                 "track_index_pe_ttm_five_year_percentile": 9.5},
            ])

    updater._ths = QdiiFakeThs()  # type: ignore[assignment]
    report = updater.update_etf("513130")

    assert report["track_merged"] == 1
    assert report["track_extra_rows"] == 1
    rows = duckdb_store.read_query(
        "SELECT trade_date, close_price, track_pe_ttm_five_year_percentile "
        "FROM etf_daily WHERE etf_code='513130' ORDER BY trade_date"
    )
    assert len(rows) == 2
    assert rows[0]["close_price"] is None and rows[0]["track_pe_ttm_five_year_percentile"] == 9.0
    assert rows[1]["close_price"] == 0.56 and rows[1]["track_pe_ttm_five_year_percentile"] == 9.5
