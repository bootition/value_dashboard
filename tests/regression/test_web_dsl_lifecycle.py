from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.dsl.engine import DSLEngine
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


def test_web_dsl_delete_referenced_draft_returns_409(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """C12(报告41): 被依赖引用的草稿删除时返回 409 而非 500（FK 约束兜底）。"""
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    client.post(
        "/api/dsl/expressions",
        json={"name": "base_ref", "expression": "balance.total_assets", "description": "基"},
    )
    client.post(
        "/api/dsl/expressions",
        json={"name": "dep_ref", "expression": "balance.total_assets", "description": "依赖方"},
    )
    expressions = client.get("/api/dsl/expressions").json()["expressions"]
    base_id = next(item["id"] for item in expressions if item["name"] == "base_ref")
    dep_id = next(item["id"] for item in expressions if item["name"] == "dep_ref")

    # 直接构造依赖关系（dep_ref -> base_ref），触发删除时的 FK 约束
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO dsl_dependencies (expression_id, depends_on_id, depends_on_version)
               VALUES (?, ?, 1)""",
            [dep_id, base_id],
        )

    response = client.delete(f"/api/dsl/expressions/{base_id}")
    assert response.status_code == 409
    assert "cannot be deleted" in response.json()["detail"]


def test_web_dsl_create_rejects_expression_over_10000_bytes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/dsl/expressions",
        json={"name": "too_big", "expression": "1" * 10_001},
    )

    assert response.status_code == 400
    assert "exceeds 10000 bytes" in response.json()["detail"]
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM dsl_expressions WHERE name = 'too_big'"
    )[0]["c"] == 0


def test_web_dsl_publish_and_delete_do_not_scan_whole_table(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch,
) -> None:
    def list_all_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("publish/delete must query by id, not list_all()")

    monkeypatch.setattr(DSLEngine, "list_all", list_all_must_not_be_called)

    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(router)
    client = TestClient(app)

    created = client.post(
        "/api/dsl/expressions",
        json={"name": "by_id_only", "expression": "balance.total_assets"},
    )
    assert created.status_code == 200
    expression_id = sqlite_store.query(
        "SELECT id FROM dsl_expressions WHERE name = 'by_id_only'"
    )[0]["id"]

    # 草稿状态 publish 应返回 400（而不是调用 list_all 抛 AssertionError）。
    published = client.put(f"/api/dsl/expressions/{expression_id}/publish")
    assert published.status_code == 400

    deleted = client.delete(f"/api/dsl/expressions/{expression_id}")
    assert deleted.status_code == 200
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM dsl_expressions WHERE id = ?", [expression_id]
    )[0]["c"] == 0
