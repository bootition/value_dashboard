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
    monkeypatch.setattr("app.core.storage.duckdb_store.DuckDBStore", lambda: duck)
    monkeypatch.setattr("app.core.storage.sqlite_store.SQLiteStore", lambda: sqlite)
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
