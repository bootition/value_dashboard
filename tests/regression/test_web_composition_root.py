"""Web composition roots own database resolution and route injection."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import app.web.main as web_main
from fastapi.testclient import TestClient
from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema


WEB_ROOT = Path(__file__).parents[2] / "app" / "web"


def test_create_app_wires_explicit_database_state(
    database_paths: DatabasePathSet,
) -> None:
    config = Config({}, paths=database_paths)
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)

    app = web_main.create_app(
        paths=database_paths,
        config=config,
        duck=duck,
        sqlite=sqlite,
    )

    assert app.state.paths is database_paths
    assert app.state.config is config
    assert app.state.duck is duck
    assert app.state.sqlite is sqlite


def test_spa_entry_is_never_cached(
    database_paths: DatabasePathSet,
) -> None:
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=DuckDBStore(paths=database_paths),
        sqlite=SQLiteStore(paths=database_paths),
    )

    response = TestClient(app).get("/screening")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_data_status_remains_available_during_same_process_write(
    database_paths: DatabasePathSet,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=duck,
        sqlite=sqlite,
    )

    with duck.transaction():
        response = TestClient(app).get("/api/data-status/summary")

    assert response.status_code == 200
    assert "minimum_data_readiness" in response.json()


def test_health_reports_database_unavailability(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=duck,
        sqlite=sqlite,
    )
    client = TestClient(app)

    assert client.get("/api/health").status_code == 200
    monkeypatch.setattr(duck, "read_query", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "unavailable"


def test_browser_mutations_require_local_host_origin_and_session_token(
    database_paths: DatabasePathSet,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=duck,
        sqlite=sqlite,
    )
    client = TestClient(app)

    assert client.put("/api/screening/draft", json={"draft": {}}).status_code == 403
    token = client.get("/api/session").json()["write_token"]
    assert client.put(
        "/api/screening/draft", json={"draft": {}},
        headers={"X-VD-Write-Token": token, "Origin": "http://attacker.invalid"},
    ).status_code == 403
    assert client.put(
        "/api/screening/draft", json={"draft": {}},
        headers={"X-VD-Write-Token": token, "Origin": "http://testserver"},
    ).status_code == 200
    assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 400


def test_run_server_reuses_one_resolved_database_pair(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    config = Config(
        {"server": {"host": "127.0.0.1", "port": 8765, "open_browser": False}},
        paths=database_paths,
    )
    duck = object()
    sqlite = object()
    calls: dict[str, Any] = {"resolve": 0, "load": 0, "duck": 0, "sqlite": 0}

    def resolve_paths() -> DatabasePathSet:
        calls["resolve"] += 1
        return database_paths

    def load_config(paths: DatabasePathSet) -> Config:
        calls["load"] += 1
        assert paths is database_paths
        return config

    def build_duck(*, paths: DatabasePathSet) -> object:
        calls["duck"] += 1
        assert paths is database_paths
        return duck

    def build_sqlite(*, paths: DatabasePathSet) -> object:
        calls["sqlite"] += 1
        assert paths is database_paths
        return sqlite

    def create_app(**kwargs: object) -> object:
        calls["create_app"] = kwargs
        return object()

    monkeypatch.setattr(web_main, "resolve_and_validate_paths", resolve_paths)
    monkeypatch.setattr(web_main.Config, "load_with_paths", load_config)
    monkeypatch.setattr(web_main, "DuckDBStore", build_duck)
    monkeypatch.setattr(web_main, "SQLiteStore", build_sqlite)
    monkeypatch.setattr(
        web_main,
        "init_all_schema",
        lambda *, duckdb_store, sqlite_store: calls.update(
            schema=(duckdb_store, sqlite_store)
        ),
    )
    monkeypatch.setattr(web_main, "create_app", create_app)
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda app, **kwargs: calls.update(uvicorn=(app, kwargs)),
    )

    web_main.run_server()

    assert calls["resolve"] == 1
    assert calls["load"] == 1
    assert calls["duck"] == 1
    assert calls["sqlite"] == 1
    assert calls["schema"] == (duck, sqlite)
    assert calls["create_app"] == {
        "paths": database_paths,
        "config": config,
        "duck": duck,
        "sqlite": sqlite,
        "startup_readiness": {
            "ready": False,
            "checking": True,
            "cached": False,
            "stock_count": 0,
            "missing": {},
            "missing_counts": {},
            "schema_compatibility": {"compatible": True, "missing": []},
        },
        "start_maintenance_on_lifespan": True,
    }


def test_web_sources_have_no_implicit_database_constructors() -> None:
    forbidden_zero_arg = {
        "DuckDBStore",
        "SQLiteStore",
        "ScreeningEngine",
        "PDFManager",
    }
    violations: list[str] = []

    for path in WEB_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in {"DuckDBStore", "SQLiteStore", "resolve_and_validate_paths"}:
                if path.parent.name == "api":
                    violations.append(f"{path.relative_to(WEB_ROOT)}:{node.lineno}:{name}")
            if name in forbidden_zero_arg and not node.args and not node.keywords:
                violations.append(f"{path.relative_to(WEB_ROOT)}:{node.lineno}:{name}()")

    assert violations == []
