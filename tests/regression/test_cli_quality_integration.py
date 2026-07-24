from __future__ import annotations

import json
from pathlib import Path

from app.cli.main import data_diagnose, data_status
from app.core.config import Config
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _configure_temp_databases(tmp_path: Path, monkeypatch) -> None:
    duck_path = tmp_path / "cli.duckdb"
    sqlite_path = tmp_path / "cli.sqlite"
    config = Config(
        {
            "database": {
                "duckdb_path": str(duck_path),
                "sqlite_path": str(sqlite_path),
            }
        }
    )
    monkeypatch.setattr(Config, "_instance", config)
    monkeypatch.setattr(
        Config,
        "load",
        classmethod(lambda cls, config_dir=None: config),
    )
    init_duckdb_schema(DuckDBStore(duck_path))
    init_sqlite_schema(SQLiteStore(sqlite_path))


def test_data_status_includes_structured_quality(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _configure_temp_databases(tmp_path, monkeypatch)

    data_status()

    response = json.loads(capsys.readouterr().out)
    assert "data_quality" in response["result"]["data"]


def test_data_diagnose_is_unhealthy_when_quality_warnings_exist(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _configure_temp_databases(tmp_path, monkeypatch)

    data_diagnose()

    response = json.loads(capsys.readouterr().out)
    assert response["result"]["status"] == "error"
    assert response["result"]["data"]["healthy"] is False
    assert "SNAPSHOT_STALE" in response["result"]["data"]["data_quality"]["warning_codes"]
