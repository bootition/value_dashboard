from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.dsl import router


def test_web_dsl_cannot_publish_a_draft(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    created = client.post(
        "/api/dsl/expressions",
        json={"name": "web_assets", "expression": "balance.total_assets", "description": "总资产"},
    )
    assert created.status_code == 200

    expression_id = client.get("/api/dsl/expressions").json()["expressions"][0]["id"]
    published = client.put(f"/api/dsl/expressions/{expression_id}/publish")

    assert published.status_code == 400
    assert "必须完成校验和两次预览" in published.json()["detail"]
