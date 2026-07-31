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
