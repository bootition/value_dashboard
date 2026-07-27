"""Safe pytest configuration for audit regression tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import (
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    canonicalize_path,
)
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore


@pytest.fixture
def database_paths(tmp_path: Path) -> DatabasePathSet:
    """Return a validated database pair inside the wrapper-owned test root."""
    run_root = Path(os.environ["VD_TEST_RUN_ROOT"])
    if not run_root.is_dir():
        raise RuntimeError("wrapper-created VD_TEST_RUN_ROOT is missing")

    canonical_run_root = canonicalize_path(run_root)
    canonical_tmp = canonicalize_path(tmp_path)
    if canonical_run_root not in canonical_tmp.parents:
        raise PathIsolationError(f"tmp_path escaped VD_TEST_RUN_ROOT: {canonical_tmp}")

    return DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=canonical_tmp / "valuedashboard.duckdb",
        sqlite_path=canonical_tmp / "valuedashboard.sqlite",
        run_root=canonical_tmp,
    ).validate()


@pytest.fixture
def duckdb_store(database_paths: DatabasePathSet) -> DuckDBStore:
    """Return an isolated DuckDB store initialized inside pytest's temp directory."""
    store = DuckDBStore(paths=database_paths)
    init_duckdb_schema(store)
    return store


@pytest.fixture
def sqlite_store(database_paths: DatabasePathSet) -> SQLiteStore:
    """Return an isolated SQLite store initialized inside pytest's temp directory."""
    store = SQLiteStore(paths=database_paths)
    init_sqlite_schema(store)
    return store


@pytest.fixture
def sample_stock_code() -> str:
    """样本股票代码: 贵州茅台"""
    return "600519"
