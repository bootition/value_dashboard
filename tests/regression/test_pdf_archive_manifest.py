from __future__ import annotations

import os

import pytest

from app.core.config import Config
from app.core.pdf.manager import PDFManager
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv
from app.core.storage.schema import init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore


def test_pdf_archive_manifest_supports_configured_target_and_verified_restore(
    database_paths,
    sqlite_store: SQLiteStore,
) -> None:
    Config({"pdf": {"archive_root": "archive_pdf"}}, paths=database_paths)
    manager = PDFManager(sqlite=sqlite_store)
    hot_path = manager.hot_dir / "000001" / "notice.pdf"
    hot_path.parent.mkdir(parents=True)
    hot_path.write_bytes(b"%PDF-test-content")

    archived = manager.archive_pdfs("000001", target_dir="data/archive_pdf")

    assert archived["archived_count"] == 1
    assert not hot_path.exists()
    archived_file = manager.list_archived_pdfs("000001")
    assert len(archived_file) == 1
    assert archived_file[0]["filename"] == "notice.pdf"
    assert archived_file[0]["size_bytes"] == len(b"%PDF-test-content")
    assert archived_file[0]["archived"] is True
    assert archived_file[0]["integrity_verified"] is True
    restored = manager.restore_pdf("000001", "notice.pdf")
    assert restored["status"] == "ok"
    assert hot_path.read_bytes() == b"%PDF-test-content"


def test_pdf_archive_rejects_checksum_tampering(
    database_paths,
    sqlite_store: SQLiteStore,
) -> None:
    Config({"pdf": {"archive_root": "archive_pdf"}}, paths=database_paths)
    manager = PDFManager(sqlite=sqlite_store)
    hot_path = manager.hot_dir / "000001" / "notice.pdf"
    hot_path.parent.mkdir(parents=True)
    hot_path.write_bytes(b"%PDF-original")
    manager.archive_pdfs("000001", target_dir="data/archive_pdf")
    archive_path = sqlite_store.query(
        "SELECT archive_path FROM pdf_archive_manifest WHERE stock_code = ?", ["000001"]
    )[0]["archive_path"]
    hot_path.unlink(missing_ok=True)
    type(hot_path)(archive_path).write_bytes(b"%PDF-tampered")

    assert manager.is_in_archive("000001", "notice.pdf") is False
    assert manager.restore_pdf("000001", "notice.pdf")["error"] == "archive checksum verification failed"


def test_pdf_manager_uses_the_staging_run_root(tmp_path) -> None:
    paths = DatabasePathSet(
        env=VdEnv.STAGING,
        duckdb_path=tmp_path / "valuedashboard.duckdb",
        sqlite_path=tmp_path / "valuedashboard.sqlite",
        run_root=tmp_path,
    ).validate()
    sqlite = SQLiteStore(paths=paths)
    init_sqlite_schema(sqlite)
    Config({"pdf": {"archive_root": "archive_pdf"}}, paths=paths)

    assert PDFManager(sqlite=sqlite).hot_dir == tmp_path / "data" / "pdf"


def test_pdf_archive_requires_a_six_digit_code_and_a_hot_directory_source(
    database_paths,
    sqlite_store: SQLiteStore,
    tmp_path,
) -> None:
    Config({"pdf": {"archive_root": "archive_pdf"}}, paths=database_paths)
    manager = PDFManager(sqlite=sqlite_store)
    assert manager.archive_pdfs("1") == {"error": "Invalid stock code"}
    with pytest.raises(PathIsolationError, match="Invalid stock code"):
        manager._stock_dir("..\\000001")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notice.pdf").write_bytes(b"%PDF-outside")
    manager.hot_dir.mkdir(parents=True)
    link = manager.hot_dir / "000001"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        return

    try:
        assert manager.archive_pdfs("000001") == {"error": "PDF archive source escapes hot_dir"}
        assert (outside / "notice.pdf").exists()
    finally:
        link.unlink(missing_ok=True)
