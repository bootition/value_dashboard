from __future__ import annotations

import pytest

from app.core.archive import DataArchiveManager
from app.core.config import Config
from app.core.pdf.manager import PDFManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore


def test_archive_manager_requires_configured_root_and_verified_checksums(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"archive": {"root": "parquet"}}, paths=database_paths),
    )
    manager = DataArchiveManager(duckdb_store, database_paths, sqlite_store)

    with pytest.raises(PathIsolationError, match="must match"):
        manager.create("elsewhere")
    created = manager.create("data/parquet")
    assert created["status"] == "ok", created
    assert manager.is_verified_for_cleanup("data/parquet")[0] is False
    assert manager.verify("data/parquet")["status"] == "ok"
    assert manager.is_verified_for_cleanup("data/parquet") == (True, None)

    archive_file = manager.archive_root / "price_daily_raw.parquet"
    archive_file.write_bytes(archive_file.read_bytes() + b"tampered")
    assert manager.is_verified_for_cleanup("data/parquet")[0] is False


def test_archive_manager_rejects_configured_root_escape(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"archive": {"root": "../outside"}}, paths=database_paths),
    )

    with pytest.raises(PathIsolationError, match="remain under"):
        DataArchiveManager(duckdb_store, database_paths, sqlite_store)


def test_archive_rejects_cold_pdf_or_sqlite_manifest_changes(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"archive": {"root": "parquet"}, "pdf": {"archive_root": "archive_pdf"}}, paths=database_paths),
    )
    pdf_manager = PDFManager(sqlite=sqlite_store)
    hot_pdf = pdf_manager.hot_dir / "000001" / "notice.pdf"
    hot_pdf.parent.mkdir(parents=True)
    hot_pdf.write_bytes(b"%PDF-cold-archive")
    assert pdf_manager.archive_pdfs("000001")["status"] == "ok"

    manager = DataArchiveManager(duckdb_store, database_paths, sqlite_store)
    assert manager.create("data/parquet")["status"] == "ok"
    assert manager.verify("data/parquet")["status"] == "ok"
    assert manager.is_verified_for_cleanup("data/parquet") == (True, None)

    sqlite_store.execute(
        "UPDATE pdf_archive_manifest SET checksum = ? WHERE stock_code = '000001'",
        ["0" * 64],
    )
    verified, error = manager.is_verified_for_cleanup("data/parquet")
    assert verified is False
    assert error == "PDF archive manifest no longer matches SQLite"


def test_archive_restore_replaces_hot_data_from_verified_archive(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"archive": {"root": "parquet"}}, paths=database_paths),
    )
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO price_daily_raw
               (stock_code, trade_date, open, high, low, close, volume)
               VALUES ('000001', '2026-09-01', 10, 12, 9, 11, 1000)"""
        )
    manager = DataArchiveManager(duckdb_store, database_paths, sqlite_store)
    assert manager.create("data/parquet")["status"] == "ok"
    assert manager.verify("data/parquet")["status"] == "ok"
    before = duckdb_store.read_query(
        "SELECT COUNT(*) AS c, COALESCE(MAX(close), 0) AS hi FROM price_daily_raw"
    )[0]
    assert before["c"] > 0

    duckdb_store.write_query("DELETE FROM price_daily_raw")
    assert duckdb_store.read_query("SELECT COUNT(*) AS c FROM price_daily_raw")[0]["c"] == 0

    result = manager.restore_from_archive("data/parquet")
    assert result["status"] == "ok", result
    after = duckdb_store.read_query(
        "SELECT COUNT(*) AS c, COALESCE(MAX(close), 0) AS hi FROM price_daily_raw"
    )[0]
    assert after["c"] == before["c"]
    assert after["hi"] == before["hi"]


def test_archive_restore_rejects_unverified_archive(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"archive": {"root": "parquet"}}, paths=database_paths),
    )
    manager = DataArchiveManager(duckdb_store, database_paths, sqlite_store)
    assert manager.create("data/parquet")["status"] == "ok"
    result = manager.restore_from_archive("data/parquet")
    assert result["status"] == "error"
    assert "verification" in result["error"] or "record" in result["error"]
