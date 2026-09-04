"""指数看板（多指数 ERP）只读域与 API 回归（2026-09-05）

覆盖：
- ERP 公式：1/PE*100 − 10Y 国债收益率（百分点口径）
- 分位带：p20/p50/p80 与当前分位；样本不足/无数据语义
- 同日期多源去重主源优先（宽基 legulegu、行业 sws）
- API：/api/index/catalog、/overview、/erp-compare、/{code}/erp、
  /{code}/valuation 与 404 守卫
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.index_dashboard import (
    _as_date,
    compute_erp_series,
    erp_compare,
    erp_detail,
    index_catalog,
    valuation_detail,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.index_dashboard import router as index_router


def _seed_index_valuation(duck: DuckDBStore) -> None:
    """000300：乐咕月度 40 点；SW801010：申万日度 40 点（同窗口无国债的点用于测试跳过）。"""
    base = date(2026, 1, 31)
    for i in range(40):
        d = base - timedelta(days=30 * i)
        pe = 10.0 + (i % 10) * 0.5  # 10.0 ~ 14.5
        duck.write_query(
            """INSERT INTO index_valuation
               (index_code, trade_date, pe_ttm, pe_metric, pb, div_yield, source,
                fetch_time, raw_hash, confidence, batch_id, extra)
               VALUES (?, ?, ?, 'ttm', ?, NULL, 'legulegu', CURRENT_TIMESTAMP, ?, 'approximate', 'b', NULL)""",
            ["000300", d.isoformat(), pe, 1.0 + i * 0.01, f"h{i:02d}"],
        )
    for i in range(40):
        d = date(2026, 8, 31) - timedelta(days=i)
        pe = 20.0 + (i % 10) * 0.4
        duck.write_query(
            """INSERT INTO index_valuation
               (index_code, trade_date, pe_ttm, pe_metric, pb, div_yield, source,
                fetch_time, raw_hash, confidence, batch_id, extra)
               VALUES (?, ?, ?, 'sws_daily', ?, ?, 'sws', CURRENT_TIMESTAMP, ?, 'approximate', 'b', NULL)""",
            ["SW801010", d.isoformat(), pe, 2.0 + i * 0.01, 1.5, f"s{i:02d}"],
        )


def _seed_treasury(duck: DuckDBStore) -> None:
    base = date(2026, 9, 1)
    for i in range(50):
        d = base - timedelta(days=30 * i)
        duck.write_query(
            """INSERT INTO treasury_yield_curve
               (curve_date, tenor_years, yield_pct, source, fetch_time, raw_hash, confidence, batch_id)
               VALUES (?, 10, 1.72, 'czb_mof', CURRENT_TIMESTAMP, ?, 'strict', 'b')""",
            [d.isoformat(), f"t{i:02d}"],
        )


def test_erp_formula_matches_article_caliber() -> None:
    points = compute_erp_series(
        [{"trade_date": "2026-07-31", "pe_ttm": 14.6, "source": "legulegu"}],
        [{"curve_date": "2026-07-31", "yield_pct": 1.72}],
    )
    assert points[0]["erp"] == pytest.approx((1 / 14.6 * 100) - 1.72, abs=1e-9)
    assert points[0]["erp"] == pytest.approx(5.129, abs=1e-2)


def test_as_date_normalizes_pandas_timestamp() -> None:
    """DuckDB Python 驱动返回 pandas.Timestamp（datetime 子类），
    直接比较 datetime.date 会 TypeError（正式库实锤），必须归一为纯 date。"""
    import pandas as pd

    value = _as_date(pd.Timestamp("2026-09-03"))
    assert value == date(2026, 9, 3)
    assert _as_date("2026-09-03") == date(2026, 9, 3)
    assert _as_date(None) is None


def test_erp_skips_missing_treasury_and_nonpositive_pe() -> None:
    points = compute_erp_series(
        [
            {"trade_date": "2026-01-01", "pe_ttm": 14.0, "source": "legulegu"},  # 无国债 → 跳过
            {"trade_date": "2026-02-01", "pe_ttm": 0.0, "source": "legulegu"},  # 非正 PE → 跳过
            {"trade_date": "2026-03-01", "pe_ttm": 10.0, "source": "legulegu"},
        ],
        [{"curve_date": "2026-03-01", "yield_pct": 2.0}],
    )
    assert len(points) == 1
    assert points[0]["erp"] == pytest.approx(8.0)


def test_overview_and_erp_detail(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)

    overview = erp_compare(duckdb_store)
    assert len(overview["items"]) == 43  # 12 宽基 + 31 申万一级
    by_code = {item["code"]: item for item in overview["items"]}
    hs300 = by_code["000300"]
    assert hs300["status"] == "ok"
    assert hs300["category"] == "broad"
    assert hs300["backtest_validated"] is True
    assert hs300["pe"] is not None
    assert hs300["erp"] is not None
    assert hs300["erp_percentile"] is not None
    assert hs300["erp_bands"]["p20"] <= hs300["erp_bands"]["p50"] <= hs300["erp_bands"]["p80"]

    sw = by_code["SW801010"]
    assert sw["category"] == "industry"
    assert sw["backtest_validated"] is False
    assert sw["pe_metric"] == "sws_daily"

    detail = erp_detail(duckdb_store, "000300")
    assert len(detail["series"]) > 0
    assert detail["erp_bands"]["p50"] is not None
    assert "宽基" in detail["disclaimer"]

    industry_detail = erp_detail(duckdb_store, "SW801010")
    assert "暂无回测验证" in industry_detail["disclaimer"]

    valuation = valuation_detail(duckdb_store, "000300")
    assert len(valuation["pe_series"]) == 40
    assert valuation["pe_bands"]["p20"] is not None


def test_unavailable_index_is_honest(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    overview = erp_compare(duckdb_store)
    unavailable = [i for i in overview["items"] if i["status"] == "unavailable"]
    assert len(unavailable) == 43, "空库时全部指数必须如实 unavailable，不伪造"


def test_index_api_endpoints(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)

    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(index_router)
    client = TestClient(app)

    catalog = client.get("/api/index/catalog")
    assert catalog.status_code == 200
    assert len(catalog.json()["items"]) == 43

    overview = client.get("/api/index/overview")
    assert overview.status_code == 200
    assert len(overview.json()["items"]) == 43

    compare = client.get("/api/index/erp-compare")
    assert compare.status_code == 200

    erp = client.get("/api/index/000300/erp")
    assert erp.status_code == 200
    assert erp.json()["name"] == "沪深300"
    assert erp.json()["series"]

    valuation = client.get("/api/index/SW801010/valuation")
    assert valuation.status_code == 200
    assert valuation.json()["pe_metric"] == "sws_daily"

    missing = client.get("/api/index/999999/erp")
    assert missing.status_code == 404


def test_catalog_contains_agreed_universe() -> None:
    codes = {item["code"] for item in index_catalog()}
    assert {"000300", "000905", "000852", "000016"} <= codes
    assert len({c for c in codes if c.startswith("SW")}) == 31
    assert "SW801150" in codes  # 医药生物
