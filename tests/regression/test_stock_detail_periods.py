from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.stock_detail import _to_single_quarter
from app.web.api.stock_detail import router


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
