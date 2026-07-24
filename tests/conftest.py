"""Safe pytest configuration for audit regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore


@pytest.fixture
def duckdb_store(tmp_path: Path) -> DuckDBStore:
    """Return an isolated DuckDB store initialized inside pytest's temp directory."""
    store = DuckDBStore(tmp_path / "test.duckdb")
    init_duckdb_schema(store)
    return store


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SQLiteStore:
    """Return an isolated SQLite store initialized inside pytest's temp directory."""
    store = SQLiteStore(tmp_path / "test.sqlite")
    init_sqlite_schema(store)
    return store


@pytest.fixture
def sample_stock_code() -> str:
    """样本股票代码: 贵州茅台"""
    return "600519"
