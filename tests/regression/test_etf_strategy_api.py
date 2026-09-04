"""ETF 轮动工作台 API 回归（2026-09-05）

GET 只读（不落卖出计划）、POST 录入、参数校验 400。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.etf_strategy import add_etf_trade, upsert_etf_meta
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.etf_strategy import router as etf_router


def _build_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(etf_router)
    return TestClient(app)


def _seed(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    upsert_etf_meta(
        sqlite, etf_code="512880", name="证券ETF",
        track_index_code="SW801790", track_index_name="非银金融",
        primary_metric="pb", industry_group="金融", budget=1000.0, step_pct=5.0,
    )
    add_etf_trade(sqlite, etf_code="512880", trade_date="2026-01-05",
                  direction="buy", price=1.0, shares=100, fee=0.1)
    duck.write_query(
        """INSERT INTO index_valuation
           (index_code, trade_date, pe_ttm, pe_metric, pb, source,
            fetch_time, raw_hash, confidence, batch_id)
           VALUES ('SW801790', '2026-09-01', 20.0, 'sws_daily', 1.0, 'sws',
                   CURRENT_TIMESTAMP, 'h', 'approximate', 'b')"""
    )
    duck.write_query(
        """INSERT INTO etf_daily
           (etf_code, trade_date, close_price, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('512880', '2026-09-03', 1.1, 'ths', CURRENT_TIMESTAMP, 'h', 'strict', 'b')"""
    )


def test_overview_and_detail_are_read_only(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed(duckdb_store, sqlite_store)
    client = _build_client(duckdb_store, sqlite_store)

    overview = client.get("/api/etf/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["etf_code"] == "512880"
    assert item["signal"] == "buy", "PB 分位 0%（只有 1 样本）应落在买入观察区"
    assert item["current_price"] == 1.1
    assert item["next_buy_price"] == 0.95
    # GET 只读：即便 signal=sell 也不得写 etf_sell_plans
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_sell_plans")[0]["c"] == 0

    detail = client.get("/api/etf/512880/detail")
    assert detail.status_code == 200
    assert detail.json()["trades"][0]["fee"] == 0.1
    assert detail.json()["track_valuation"]["code"] == "SW801790"

    missing = client.get("/api/etf/999999/detail")
    assert missing.status_code == 404


def test_overview_uses_ths_track_percentile_fallback(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """港股/中概无指数估值历史：同花顺跟踪指数 PE-TTM 五年分位兜底信号。"""
    upsert_etf_meta(sqlite_store, etf_code="513130", name="恒生科技",
                    track_index_code=None, primary_metric="pe")
    duckdb_store.write_query(
        """INSERT INTO etf_daily
           (etf_code, trade_date, close_price, track_pe_ttm_five_year_percentile,
            source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('513130', '2026-09-03', 0.6, 25.0, 'ths',
                   CURRENT_TIMESTAMP, 'h', 'strict', 'b')"""
    )
    client = _build_client(duckdb_store, sqlite_store)

    overview = client.get("/api/etf/overview").json()
    item = overview["items"][0]
    assert item["percentile"] == 25.0
    assert "同花顺5年" in item["percentile_label"]
    assert item["signal"] == "neutral"


def test_post_endpoints_validate_and_write(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed(duckdb_store, sqlite_store)
    client = _build_client(duckdb_store, sqlite_store)

    trade = client.post("/api/etf/trades", json={
        "etf_code": "512880", "trade_date": "2026-02-05",
        "direction": "buy", "price": 0.95, "shares": 100,
    })
    assert trade.status_code == 200

    oversell = client.post("/api/etf/trades", json={
        "etf_code": "512880", "trade_date": "2026-02-06",
        "direction": "sell", "price": 1.2, "shares": 9999,
    })
    assert oversell.status_code == 400
    assert "超过持仓" in oversell.json()["detail"]

    cash = client.post("/api/etf/cash-flows", json={
        "flow_date": "2026-03-23", "direction": "in", "amount": 500.0,
    })
    assert cash.status_code == 200

    setting = client.post("/api/etf/settings", json={
        "key": "total_assets", "value": "4100.99",
    })
    assert setting.status_code == 200
    assert sqlite_store.query(
        "SELECT value FROM etf_settings WHERE key='total_assets'"
    )[0]["value"] == "4100.99"

    bad_setting = client.post("/api/etf/settings", json={
        "key": "anything", "value": "x",
    })
    assert bad_setting.status_code == 400

    bad_meta = client.post("/api/etf/meta", json={
        "etf_code": "512000", "name": "测试", "primary_metric": "ps",
    })
    assert bad_meta.status_code == 400
