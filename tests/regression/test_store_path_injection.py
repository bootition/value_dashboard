"""Store and schema boundaries require an explicit validated path set."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv
from app.core.storage.schema import init_all_schema
from app.core.storage.sqlite_store import SQLiteStore


def make_paths(tmp_path: Path) -> DatabasePathSet:
    return DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=tmp_path / "valuedashboard.duckdb",
        sqlite_path=tmp_path / "valuedashboard.sqlite",
        run_root=tmp_path,
    ).validate()


@pytest.mark.parametrize("store_type", [DuckDBStore, SQLiteStore])
def test_store_constructor_requires_keyword_only_paths(store_type: type) -> None:
    parameters = inspect.signature(store_type).parameters
    assert list(parameters) == ["paths"]
    assert parameters["paths"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["paths"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "source_path",
    [
        Path(__file__).parents[2] / "app" / "core" / "storage" / "duckdb_store.py",
        Path(__file__).parents[2] / "app" / "core" / "storage" / "sqlite_store.py",
    ],
)
def test_store_modules_do_not_import_config(source_path: Path) -> None:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.core.config" not in imported


def test_duckdb_store_uses_injected_path_set(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = DuckDBStore(paths=paths)
    assert store.db_path == paths.duckdb_path
    store.write_query("CREATE TABLE injected_boundary (id INTEGER)")
    assert paths.duckdb_path.is_file()


def test_sqlite_store_uses_injected_path_set(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SQLiteStore(paths=paths)
    assert store.db_path == paths.sqlite_path
    store.execute("CREATE TABLE injected_boundary (id INTEGER)")
    assert paths.sqlite_path.is_file()


def test_sqlite_query_is_read_only(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SQLiteStore(paths=paths)
    store.execute("CREATE TABLE query_boundary (id INTEGER PRIMARY KEY)")
    assert store.query("SELECT COUNT(*) AS count FROM query_boundary") == [{"count": 0}]
    with pytest.raises(Exception, match="readonly|read-only"):
        with store.connection() as conn:
            conn.execute("INSERT INTO query_boundary (id) VALUES (1)")


@pytest.mark.parametrize("store_type", [DuckDBStore, SQLiteStore])
def test_store_revalidates_untrusted_path_set_before_side_effects(
    store_type: type,
    tmp_path: Path,
) -> None:
    repository_data = Path(__file__).parents[2] / "data"
    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=repository_data / "valuedashboard.duckdb",
        sqlite_path=repository_data / "valuedashboard.sqlite",
        run_root=repository_data,
    )
    with pytest.raises(PathIsolationError):
        store_type(paths=paths)
    assert not (tmp_path / "unexpected").exists()


def test_init_all_schema_requires_explicit_boundary() -> None:
    parameters = inspect.signature(init_all_schema).parameters
    assert set(parameters) == {"duckdb_store", "sqlite_store", "paths", "skip_if_current"}
    assert parameters["paths"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["skip_if_current"].kind is inspect.Parameter.KEYWORD_ONLY


def test_init_all_schema_accepts_explicit_stores(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite)
    assert duck.read_query("SELECT COUNT(*) AS count FROM stock_meta")[0]["count"] == 0
    assert sqlite.query("SELECT COUNT(*) AS count FROM schema_migrations")[0]["count"] >= 1


def test_config_database_path_cannot_fall_back_to_yaml(tmp_path: Path) -> None:
    config = Config(
        {
            "database": {
                "duckdb_path": str(tmp_path / "unsafe.duckdb"),
                "sqlite_path": str(tmp_path / "unsafe.sqlite"),
            }
        }
    )
    with pytest.raises(PathIsolationError):
        config.get_path("database", "duckdb_path")
