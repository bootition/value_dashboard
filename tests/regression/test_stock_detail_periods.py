from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.stock_detail import _to_single_quarter, get_kline, router


def _insert_kline_rows(
    duck: DuckDBStore, stock_code: str, rows: list[tuple]
) -> None:
    """插入日线原始数据（换手率统一 1.23，用于验证周/月 fail-closed 置 null）"""
    for trade_date, open_, high, low, close, volume, turnover in rows:
        duck.write_query(
            """INSERT INTO price_daily_raw
               (stock_code, trade_date, open, high, low, close, volume, turnover, turnover_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [stock_code, trade_date, open_, high, low, close, volume, turnover, 1.23],
        )


def _kline_request(duck: DuckDBStore) -> Request:
    return Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(duck=duck))}
    )


def test_quarterly_trend_converts_cumulative_flows_but_preserves_balances() -> None:
    rows = [
        {
            "report_date": "2025-06-30", "revenue": 150.0, "parent_net_profit": 30.0,
            "cf_from_operating": 45.0, "total_assets": 1100.0,
        },
        {
            "report_date": "2025-03-31", "revenue": 50.0, "parent_net_profit": 10.0,
            "cf_from_operating": 15.0, "total_assets": 1000.0,
        },
        {
            "report_date": "2024-12-31", "revenue": 400.0, "parent_net_profit": 80.0,
            "cf_from_operating": 120.0, "total_assets": 900.0,
        },
    ]

    quarterly = _to_single_quarter(rows)

    assert [row["report_date"] for row in quarterly] == ["2024-12-31", "2025-03-31", "2025-06-30"]
    assert quarterly[1]["revenue"] == 50.0
    assert quarterly[2]["revenue"] == 100.0
    assert quarterly[2]["parent_net_profit"] == 20.0
    assert quarterly[2]["cf_from_operating"] == 30.0
    assert quarterly[2]["total_assets"] == 1100.0


def test_missing_pdf_returns_not_found_not_a_runtime_name_error(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)

    response = TestClient(app).get("/api/stock/000001/pdf/missing.pdf")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "PDF not found"


def test_quarterly_trend_fails_closed_when_in_year_period_is_missing() -> None:
    """P1-A: 缺 Q2 时，Q3 单季值必须为 NULL（fail-closed），
    禁止把 Q3−Q1（包含 Q2 的累计差额）当作单季值输出。"""
    rows = [
        {
            "report_date": "2025-09-30", "revenue": 340.0, "parent_net_profit": 34.0,
            "cf_from_operating": 68.0,
        },
        {
            "report_date": "2025-03-31", "revenue": 100.0, "parent_net_profit": 10.0,
            "cf_from_operating": 20.0,
        },
    ]

    quarterly = _to_single_quarter(rows)

    by_date = {str(row["report_date"])[:10]: row for row in quarterly}
    # 年初首期（Q1）本身就是单季值，保留累计值
    assert by_date["2025-03-31"]["revenue"] == 100.0
    # Q3 缺少紧邻的 Q2：单季值不可推导 → NULL，而非 240.0
    assert by_date["2025-09-30"]["revenue"] is None
    assert by_date["2025-09-30"]["parent_net_profit"] is None
    assert by_date["2025-09-30"]["cf_from_operating"] is None


def test_quarterly_trend_fails_closed_when_prior_row_is_not_adjacent_quarter() -> None:
    """P1-A: 年内只有 Q1 与 Q4（缺 Q2/Q3）时，Q4 单季值也必须为 NULL。"""
    rows = [
        {
            "report_date": "2025-12-31", "revenue": 500.0, "parent_net_profit": 50.0,
        },
        {
            "report_date": "2025-03-31", "revenue": 100.0, "parent_net_profit": 10.0,
        },
    ]

    quarterly = _to_single_quarter(rows)

    by_date = {str(row["report_date"])[:10]: row for row in quarterly}
    assert by_date["2025-03-31"]["revenue"] == 100.0
    assert by_date["2025-12-31"]["revenue"] is None


def test_quarterly_trend_uses_cumulative_prior_not_single_quarter_value() -> None:
    """F2（第六轮复审）: 同一年 4 个连续季度，单季值必须为
    100/130/170/180（差分用上一期累计原值，绝不能用差分后的单季值）。"""
    rows = [
        {"report_date": "2025-12-31", "revenue": 580.0, "parent_net_profit": 58.0, "cf_from_operating": 116.0},
        {"report_date": "2025-09-30", "revenue": 400.0, "parent_net_profit": 40.0, "cf_from_operating": 80.0},
        {"report_date": "2025-06-30", "revenue": 230.0, "parent_net_profit": 23.0, "cf_from_operating": 46.0},
        {"report_date": "2025-03-31", "revenue": 100.0, "parent_net_profit": 10.0, "cf_from_operating": 20.0},
    ]

    quarterly = _to_single_quarter(rows)

    by_date = {str(row["report_date"])[:10]: row for row in quarterly}
    assert by_date["2025-03-31"]["revenue"] == 100.0
    assert by_date["2025-06-30"]["revenue"] == 130.0
    assert by_date["2025-09-30"]["revenue"] == 170.0
    assert by_date["2025-12-31"]["revenue"] == 180.0
    assert by_date["2025-12-31"]["parent_net_profit"] == 18.0
    assert by_date["2025-12-31"]["cf_from_operating"] == 36.0


def test_quarterly_trend_year_starting_at_non_q1_fails_closed() -> None:
    """F1（第六轮复审）: 年内首行不是 Q1（如数据自 Q2 起始，累计 230/400/580）
    时，Q2 累计值不得直通为单季值（NULL）；Q3/Q4 仍按紧邻差分推导。"""
    rows = [
        {"report_date": "2025-12-31", "revenue": 580.0, "parent_net_profit": 58.0},
        {"report_date": "2025-09-30", "revenue": 400.0, "parent_net_profit": 40.0},
        {"report_date": "2025-06-30", "revenue": 230.0, "parent_net_profit": 23.0},
    ]

    quarterly = _to_single_quarter(rows)

    by_date = {str(row["report_date"])[:10]: row for row in quarterly}
    # 年起于 Q2：无 Q1 累计，Q2 单季值不可推导 → NULL
    assert by_date["2025-06-30"]["revenue"] is None
    assert by_date["2025-09-30"]["revenue"] == 170.0
    assert by_date["2025-12-31"]["revenue"] == 180.0


def test_quarterly_trend_mid_year_gap_keeps_later_quarters_derivable() -> None:
    """P2-1（第六轮复审）: 仅缺 Q2（Q1/Q3/Q4 连续）时 Q3 为 NULL，
    但 Q4 = Q4累计 − Q3累计 仍可推导（不得级联置 NULL）。"""
    rows = [
        {"report_date": "2025-12-31", "revenue": 580.0, "parent_net_profit": 58.0},
        {"report_date": "2025-09-30", "revenue": 340.0, "parent_net_profit": 34.0},
        {"report_date": "2025-03-31", "revenue": 100.0, "parent_net_profit": 10.0},
    ]

    quarterly = _to_single_quarter(rows)

    by_date = {str(row["report_date"])[:10]: row for row in quarterly}
    assert by_date["2025-03-31"]["revenue"] == 100.0
    assert by_date["2025-09-30"]["revenue"] is None
    assert by_date["2025-12-31"]["revenue"] == 240.0


# ─── GET /api/stock/{code}/kline period=day|week|month ─────────────────


def test_monthly_kline_aggregates_ohlcv_with_last_trading_day(
    duckdb_store: DuckDBStore,
) -> None:
    _insert_kline_rows(
        duckdb_store,
        "600519",
        [
            (date(2026, 1, 5), 10, 12, 9, 11, 100, 1000),
            (date(2026, 1, 6), 11, 13, 10, 12, 100, 1000),
            (date(2026, 1, 30), 12, 14, 11, 13, 100, 1000),
            (date(2026, 2, 2), 13, 15, 12, 14, 100, 1000),
            (date(2026, 2, 27), 14, 16, 13, 15, 100, 1000),
        ],
    )

    result = get_kline("600519", request=_kline_request(duckdb_store), days=10, period="month")

    assert result["period"] == "month"
    assert result["count"] == 2
    bars = result["candles"]
    # 每根月K的 trade_date 取桶内最后一个真实交易日，升序
    assert [bar["trade_date"] for bar in bars] == [date(2026, 1, 30), date(2026, 2, 27)]
    jan, feb = bars
    assert (jan["open"], jan["high"], jan["low"], jan["close"]) == (10, 14, 9, 13)
    assert (jan["volume"], jan["turnover"]) == (300, 3000)
    # 周/月聚合 turnover_rate fail-closed 置 null
    assert jan["turnover_rate"] is None
    assert (feb["open"], feb["high"], feb["low"], feb["close"]) == (13, 16, 12, 15)
    assert (feb["volume"], feb["turnover"]) == (200, 2000)
    assert feb["turnover_rate"] is None


def test_weekly_kline_uses_iso_week_buckets_across_year_boundary(
    duckdb_store: DuckDBStore,
) -> None:
    # 2025-12-29(周一) 与 2026-01-02 属于同一 ISO 周(2026-W01)；2026-01-05 起为 2026-W02
    _insert_kline_rows(
        duckdb_store,
        "600519",
        [
            (date(2025, 12, 29), 1, 3, 1, 2, 10, 100),
            (date(2026, 1, 2), 2, 4, 2, 3, 10, 100),
            (date(2026, 1, 5), 3, 6, 3, 5, 10, 100),
            (date(2026, 1, 6), 5, 7, 4, 6, 10, 100),
        ],
    )

    result = get_kline("600519", request=_kline_request(duckdb_store), days=10, period="week")

    assert result["period"] == "week"
    bars = result["candles"]
    assert len(bars) == 2
    first, second = bars
    # 跨年同一周合并为一根，trade_date 取桶内最后交易日
    assert first["trade_date"] == date(2026, 1, 2)
    assert (first["open"], first["high"], first["low"], first["close"]) == (1, 4, 1, 3)
    assert (first["volume"], first["turnover"]) == (20, 200)
    assert first["turnover_rate"] is None
    assert second["trade_date"] == date(2026, 1, 6)
    assert (second["open"], second["high"], second["low"], second["close"]) == (3, 7, 3, 6)


def test_kline_month_days_limits_bars_and_reuses_ma(
    duckdb_store: DuckDBStore,
) -> None:
    monthly_rows = [
        (date(2025, 8, 1), 1, 1.5, 0.5, 1.0, 10, 100),
        (date(2025, 9, 1), 2, 2.5, 1.5, 2.0, 10, 100),
        (date(2025, 10, 1), 3, 3.5, 2.5, 3.0, 10, 100),
        (date(2025, 11, 3), 4, 4.5, 3.5, 4.0, 10, 100),
        (date(2025, 12, 1), 5, 5.5, 4.5, 5.0, 10, 100),
        (date(2026, 1, 5), 6, 6.5, 5.5, 6.0, 10, 100),
    ]
    _insert_kline_rows(duckdb_store, "600519", monthly_rows)
    request = _kline_request(duckdb_store)

    # days 表示返回的 K 线根数：取最新 days 根后升序
    limited = get_kline("600519", request=request, days=2, period="month")
    assert limited["count"] == 2
    assert [bar["trade_date"] for bar in limited["candles"]] == [
        date(2025, 12, 1),
        date(2026, 1, 5),
    ]

    full = get_kline("600519", request=request, days=2000, period="month")
    assert full["count"] == 6
    assert [bar["trade_date"] for bar in full["candles"]] == [
        date(2025, 8, 1),
        date(2025, 9, 1),
        date(2025, 10, 1),
        date(2025, 11, 3),
        date(2025, 12, 1),
        date(2026, 1, 5),
    ]
    assert [bar["close"] for bar in full["candles"]] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    # MA 基于聚合后的收盘价复用 _calc_ma
    assert full["candles"][0]["ma5"] is None
    assert full["candles"][4]["ma5"] == pytest.approx(3.0)
    assert full["candles"][5]["ma5"] == pytest.approx(4.0)
    assert full["candles"][5]["ma10"] is None


def test_kline_default_period_day_is_backward_compatible(
    duckdb_store: DuckDBStore,
) -> None:
    _insert_kline_rows(
        duckdb_store,
        "600519",
        [
            (date(2026, 1, 5), 10, 12, 9, 11, 100, 1000),
            (date(2026, 1, 6), 11, 13, 10, 12, 100, 1000),
        ],
    )

    result = get_kline("600519", request=_kline_request(duckdb_store), days=250)

    assert result["period"] == "day"
    assert result["count"] == 2
    bars = result["candles"]
    # 默认 day 不聚合：原始日线升序返回
    assert [bar["trade_date"] for bar in bars] == [date(2026, 1, 5), date(2026, 1, 6)]
    assert bars[0]["open"] == 10
    assert bars[1]["close"] == 12
    # 日K保留表内换手率（不被周/月的 fail-closed 影响）
    assert bars[0]["turnover_rate"] == pytest.approx(1.23)


def test_kline_week_month_null_turnover_rate_ignores_source_values(
    duckdb_store: DuckDBStore,
) -> None:
    """周/月聚合即使源表换手率有值，也必须 fail-closed 置 null"""
    _insert_kline_rows(
        duckdb_store,
        "600519",
        [(date(2026, 1, 5), 10, 12, 9, 11, 100, 1000)],
    )

    for period in ("week", "month"):
        result = get_kline(
            "600519", request=_kline_request(duckdb_store), days=10, period=period
        )
        assert result["count"] == 1
        assert result["candles"][0]["turnover_rate"] is None


def test_kline_period_open_fails_closed_when_first_trading_day_open_is_missing(
    duckdb_store: DuckDBStore,
) -> None:
    _insert_kline_rows(
        duckdb_store,
        "600519",
        [
            (date(2026, 1, 5), None, 12, 9, 11, 100, 1000),
            (date(2026, 1, 6), 11, 13, 10, 12, 100, 1000),
        ],
    )

    for period in ("week", "month"):
        result = get_kline(
            "600519", request=_kline_request(duckdb_store), days=10, period=period
        )
        assert result["candles"][0]["open"] is None


def test_kline_empty_data_returns_period(
    duckdb_store: DuckDBStore,
) -> None:
    request = _kline_request(duckdb_store)
    for period in ("day", "week", "month"):
        result = get_kline("600519", request=request, days=250, period=period)
        assert result["candles"] == []
        assert result["adjust"] == "raw"
        assert result["period"] == period


def test_kline_invalid_params_rejected_over_http(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    for params in (
        {"period": "quarter"},
        {"period": "weekly"},
        {"days": 0},
        {"days": 3000},
    ):
        response = client.get("/api/stock/000001/kline", params=params)
        assert response.status_code == 422, params


def test_kline_period_over_http_echoes_period_and_ohlcv(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_kline_rows(
        duckdb_store,
        "000001",
        [
            (date(2026, 1, 5), 10, 12, 9, 11, 100, 1000),
            (date(2026, 1, 30), 12, 14, 11, 13, 100, 1000),
        ],
    )
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    response = client.get(
        "/api/stock/000001/kline", params={"period": "month", "days": 10}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == "month"
    assert payload["count"] == 1
    bar = payload["candles"][0]
    assert bar["trade_date"] == "2026-01-30"
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (10, 14, 9, 13)
    assert bar["volume"] == 200
    assert bar["turnover"] == 2000
    assert bar["turnover_rate"] is None
