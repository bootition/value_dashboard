from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.data_quality import (
    DIVIDEND_INDICATOR_FIELDS,
    indicator_trust,
    mask_untrusted_values,
    read_warning_codes,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.stock_detail import router as stock_detail_router
from app.web.api.watchlist import router as watchlist_router
from tests.conftest import insert_minimum_screenable_data

SNAPSHOT_VALUE_FIELDS = (
    "latest_close", "pe_ttm", "pb_mrq", "roe", "gross_margin", "net_margin",
    "debt_ratio", "revenue_yoy", "net_profit_yoy", "dividend_yield",
    "total_market_cap", "circ_market_cap",
)


def _build_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    app.include_router(watchlist_router)
    return TestClient(app)


def _seed_screenable(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )


    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
           (stock_code, report_date, latest_close, latest_price_date, calculated_at,
            pe_ttm, pb_mrq, roe, gross_margin, net_margin, debt_ratio,
            revenue_yoy, net_profit_yoy, dividend_yield)
           VALUES ('000001', '2025-12-31', 10, CURRENT_DATE, CURRENT_TIMESTAMP,
                   12.5, 1.5, 0.2, 0.4, 0.1, 0.2, 0.3, 0.25, 0.02)
           ON CONFLICT (stock_code, report_date) DO UPDATE SET
               pe_ttm = excluded.pe_ttm,
               pb_mrq = excluded.pb_mrq,
               roe = excluded.roe,
               gross_margin = excluded.gross_margin,
               net_margin = excluded.net_margin,
               debt_ratio = excluded.debt_ratio,
               revenue_yoy = excluded.revenue_yoy,
               net_profit_yoy = excluded.net_profit_yoy,
               dividend_yield = excluded.dividend_yield"""
    )


def test_stock_search_matches_partial_code_and_name(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('600519', '贵州茅台', 'SSE', '2020-01-01', false, false)"""
    )
    client = _build_client(duckdb_store, sqlite_store)

    by_code = client.get('/api/stock/search', params={'query': '0519'})
    by_name = client.get('/api/stock/search', params={'query': '茅'})

    assert by_code.status_code == 200
    assert by_code.json()['items'][0]['stock_code'] == '600519'
    assert by_name.status_code == 200
    assert by_name.json()['items'][0]['name'] == '贵州茅台'


def _poison_lineage(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id,
            fetch_time, raw_response_hash, confidence)
           VALUES ('000001', 'revenue', DATE '2025-12-31', 1, 'tampered', 'missing-batch',
                   CURRENT_TIMESTAMP - INTERVAL '1 day', 'not-a-sha', 'strict')"""
    )


def _poison_dividend_dates(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('000001', DATE '2024-06-28', NULL, 1.0)"""
    )


def _flat_indicator_metrics(body: dict) -> list[dict]:
    return [
        metric
        for category in body["indicators"].values()
        for metric in category.values()
    ]


def test_indicators_returns_values_and_trust_when_quality_clean(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/000001/indicators")

    assert response.status_code == 200
    body = response.json()
    assert body["trust"]["warning_codes"] == []
    assert body["trust"]["untrusted_all"] is False
    assert body["trust"]["untrusted_fields"] == []
    assert body["indicators"]["valuation"]["pe_ttm"]["value"] == 12.5
    assert body["indicators"]["valuation"]["pe_ttm"]["untrusted"] is False
    assert body["latest_close"] == 10


def test_indicators_masks_snapshot_values_when_lineage_invalid(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    _poison_lineage(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/000001/indicators")

    assert response.status_code == 200
    body = response.json()
    assert "LINEAGE_INVALID" in body["trust"]["warning_codes"]
    assert body["trust"]["untrusted_all"] is True
    metrics = _flat_indicator_metrics(body)
    assert metrics
    assert all(metric["value"] is None for metric in metrics)
    assert all(metric["untrusted"] is True for metric in metrics)
    assert body["latest_close"] is None
    # 只读展示保留：报告期/新鲜度等元数据仍然可见
    assert body["report_date"] == "2025-12-31"
    assert body["freshness"] is not None


def test_indicators_masks_only_dividend_fields_when_dividend_dates_unverified(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    _poison_dividend_dates(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/000001/indicators")

    assert response.status_code == 200
    body = response.json()
    assert body["trust"]["warning_codes"] == ["DIVIDEND_DATES_UNVERIFIED"]
    assert body["trust"]["untrusted_all"] is False
    assert body["trust"]["untrusted_fields"] == sorted(DIVIDEND_INDICATOR_FIELDS)
    dividend_yield = body["indicators"]["valuation"]["dividend_yield"]
    assert dividend_yield["value"] is None
    assert dividend_yield["untrusted"] is True
    assert body["indicators"]["shareholder_return"]["payout_ratio"]["untrusted"] is True
    pe_ttm = body["indicators"]["valuation"]["pe_ttm"]
    assert pe_ttm["value"] == 12.5
    assert pe_ttm["untrusted"] is False
    assert body["latest_close"] == 10


def test_watchlist_returns_values_and_trust_when_quality_clean(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    sqlite_store.execute(
        "INSERT INTO watchlist (stock_code, group_name) VALUES ('000001', 'default')"
    )
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200
    body = response.json()
    assert body["trust"]["warning_codes"] == []
    assert body["trust"]["untrusted_all"] is False
    item = body["items"][0]
    assert item["pe_ttm"] == 12.5
    assert item["latest_close"] == 10
    assert item["untrusted_fields"] == []


def test_watchlist_masks_snapshot_values_when_lineage_invalid(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    _poison_lineage(duckdb_store)
    sqlite_store.execute(
        "INSERT INTO watchlist (stock_code, group_name) VALUES ('000001', 'default')"
    )
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200
    body = response.json()
    assert "LINEAGE_INVALID" in body["trust"]["warning_codes"]
    assert body["trust"]["untrusted_all"] is True
    item = body["items"][0]
    assert all(item[field] is None for field in SNAPSHOT_VALUE_FIELDS)
    assert set(item["untrusted_fields"]) == set(SNAPSHOT_VALUE_FIELDS)
    # 只读展示保留：身份与来源元数据仍然可见
    assert item["name"] == "Test"
    assert item["group_name"] == "default"


def test_indicator_trust_policy_matches_frontend_is_indicator_untrusted() -> None:
    clean = indicator_trust([])
    assert clean["untrusted_all"] is False
    assert clean["untrusted_fields"] == []

    operational = indicator_trust(["UNPUBLISHED_OVERRIDES", "STALE_RUNNING_JOBS"])
    assert operational["untrusted_all"] is False
    assert operational["untrusted_fields"] == []

    dividend = indicator_trust(["DIVIDEND_DATES_UNVERIFIED"])
    assert dividend["untrusted_all"] is False
    assert dividend["untrusted_fields"] == sorted(DIVIDEND_INDICATOR_FIELDS)

    for code in (
        "FINANCIAL_SHELL_ROWS", "SNAPSHOT_STALE", "LINEAGE_INVALID",
        "CODE_IDENTITY_ALIAS", "LIVE_SCHEMA_INCOMPATIBLE",
    ):
        assert indicator_trust([code])["untrusted_all"] is True

    # 前端 data-quality.ts 2026-08-27 起将 MINIMUM_DATA_NOT_READY 视为
    # 少数股票缺口提示，不遮蔽全量快照指标。
    assert indicator_trust(["MINIMUM_DATA_NOT_READY"])["untrusted_all"] is False


def test_mask_untrusted_values_fail_closed() -> None:
    values = {"pe_ttm": 12.5, "dividend_yield": 0.02}

    masked_all = mask_untrusted_values(values, indicator_trust(["LINEAGE_INVALID"]))
    assert masked_all == {"pe_ttm": None, "dividend_yield": None}

    masked_dividend = mask_untrusted_values(
        values, indicator_trust(["DIVIDEND_DATES_UNVERIFIED"])
    )
    assert masked_dividend == {"pe_ttm": 12.5, "dividend_yield": None}

    masked_clean = mask_untrusted_values(values, indicator_trust([]))
    assert masked_clean == values


def test_read_warning_codes_fail_closed_and_failures_not_cached() -> None:
    calls = {"count": 0}

    class _BrokenStore:
        db_path = "broken-store"

        def read_query(self, *args: object, **kwargs: object) -> list:
            calls["count"] += 1
            raise RuntimeError("database unreadable")

    broken = _BrokenStore()
    first = read_warning_codes(broken, broken)  # type: ignore[arg-type]
    second = read_warning_codes(broken, broken)  # type: ignore[arg-type]

    assert first == ["LINEAGE_INVALID"]
    assert second == ["LINEAGE_INVALID"]
    assert calls["count"] == 2
