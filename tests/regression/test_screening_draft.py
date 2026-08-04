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


def test_screening_draft_payload_size_limit_returns_413(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """C7(报告41): 草稿 PUT 超过字节上限返回 413，不接受超限负载。"""
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    huge = {"conditions": {"logic": "AND", "rules": []}, "padding": "x" * (200 * 1024)}
    response = client.put("/api/screening/draft", json={"draft": huge, "revision": 0})
    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


def test_save_rule_ignores_client_status_and_forces_saved(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """C6(报告41): save_rule 由服务端固定状态机写 'saved'，忽略客户端 status。"""
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)
    resp = client.post(
        "/api/screening/rules/save",
        json={
            "name": "c6-rule",
            "rule_json": {"conditions": {"logic": "AND", "rules": []}},
            "status": "draft",
        },
    )
    assert resp.status_code == 200
    row = sqlite_store.query(
        "SELECT status FROM screening_rules WHERE name = ?", ["c6-rule"]
    )[0]
    assert row["status"] == "saved"
