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
