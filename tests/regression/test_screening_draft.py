from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router


def test_screening_draft_is_replaced_and_restored(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)
    first = {"conditions": {"logic": "AND", "rules": []}, "strict_only": False}
    second = {"conditions": {"logic": "OR", "rules": []}, "strict_only": True}

    assert client.get("/api/screening/draft").json()["draft"] is None
    first_response = client.put("/api/screening/draft", json={"draft": first, "revision": 0})
    assert first_response.status_code == 200
    second_response = client.put(
        "/api/screening/draft",
        json={"draft": second, "revision": first_response.json()["revision"]},
    )
    assert second_response.status_code == 200
    restored = client.get("/api/screening/draft").json()
    assert restored["draft"] == second
    assert restored["revision"] == 2
