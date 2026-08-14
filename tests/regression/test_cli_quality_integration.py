from __future__ import annotations

import json

from app.cli.main import data_diagnose, data_status
from app.core.config import Config
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _configure_temp_databases(database_paths: DatabasePathSet, monkeypatch) -> None:
    config = Config({}, paths=database_paths)
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    monkeypatch.setattr(Config, "_instance", config)
    monkeypatch.setattr(
        Config,
        "load",
        classmethod(lambda cls, config_dir=None, paths=None: config),
    )
    monkeypatch.setattr(
        "app.cli.main._database_stores", lambda *, initialize=True: (duck, sqlite),
    )
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)


def test_data_status_includes_structured_quality(
    database_paths: DatabasePathSet,
    monkeypatch,
    capsys,
) -> None:
    _configure_temp_databases(database_paths, monkeypatch)

    data_status()

    response = json.loads(capsys.readouterr().out)
    assert "data_quality" in response["result"]["data"]


def test_data_diagnose_is_unhealthy_when_quality_warnings_exist(
    database_paths: DatabasePathSet,
    monkeypatch,
    capsys,
) -> None:
    _configure_temp_databases(database_paths, monkeypatch)

    data_diagnose()

    response = json.loads(capsys.readouterr().out)
    assert response["result"]["status"] == "error"
    assert response["result"]["data"]["healthy"] is False
    assert "SNAPSHOT_STALE" in response["result"]["data"]["data_quality"]["warning_codes"]


def test_data_diagnose_uses_the_read_only_database_composition(
    database_paths: DatabasePathSet,
    monkeypatch,
) -> None:
    _configure_temp_databases(database_paths, monkeypatch)
    calls: list[bool] = []
    original = __import__("app.cli.main", fromlist=["_database_stores"])._database_stores

    def stores(*, initialize: bool = True):
        calls.append(initialize)
        return original(initialize=initialize)

    monkeypatch.setattr("app.cli.main._database_stores", stores)

    data_diagnose()

    assert calls == [False]


def test_database_context_reports_path_isolation_friendly(
    monkeypatch,
    capsys,
) -> None:
    """P2-17：路径策略拒绝时输出协议化错误并以退出码 2 结束，不抛 traceback。"""
    import typer

    from app.core.storage.path_policy import PathIsolationError

    def raise_isolation():
        raise PathIsolationError("VD_FORMAL_ACK=confirmed is required for formal profile")

    monkeypatch.setattr(
        "app.core.storage.path_policy.resolve_and_validate_paths", raise_isolation
    )

    try:
        __import__("app.cli.main", fromlist=["_database_context"])._database_context(
            initialize=False
        )
    except typer.Exit as error:
        assert error.exit_code == 2
    else:  # pragma: no cover - must always exit
        raise AssertionError("expected typer.Exit(2)")

    response = json.loads(capsys.readouterr().out)
    assert response["result"]["error_code"] == "E004"
    assert response["result"]["error_message"] == "path_isolation_violation"
    assert "VD_FORMAL_ACK" in response["result"]["data"]["detail"]
