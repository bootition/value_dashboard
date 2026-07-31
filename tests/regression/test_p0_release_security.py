"""Isolated P0-6/P0-7 release and local-security regression contracts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import app.core.config as config_module
import app.web.main as web_main
from app.cli.protocol import confirm_plan, consume_confirmed_plan, create_plan
from app.core.adapters.cninfo_adapter import CNINFOAdapter
from app.core.config import Config
from app.core.pdf.manager import PDFManager
from app.core.storage.path_policy import PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore


def test_standard_pep517_backend_and_static_sync_are_declared() -> None:
    root = Path(__file__).parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    sync_script = (root / "frontend" / "scripts" / "sync-static.mjs").read_text(encoding="utf-8")

    assert 'build-backend = "setuptools.build_meta"' in pyproject
    assert "Static bundle copy verification failed" in sync_script
    assert "Served static bundle verification failed" in sync_script
    assert "await cp(resolve(stagingRoot, 'index.html'), resolve(servedRoot, 'index.html'), { force: true })" in sync_script


def test_static_sync_replaces_stale_assets_and_verifies_the_copy(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    source = tmp_path / "build"
    destination = tmp_path / "served"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<main>current</main>", encoding="utf-8")
    (source / "assets" / "app.js").write_text("console.log('current')", encoding="utf-8")
    destination.mkdir()
    (destination / "obsolete.js").write_text("stale", encoding="utf-8")
    environment = os.environ | {
        "VD_FRONTEND_STATIC_SOURCE": str(source),
        "VD_FRONTEND_STATIC_DESTINATION": str(destination),
    }

    subprocess.run(
        ["node", str(root / "frontend" / "scripts" / "sync-static.mjs")],
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert (destination / "index.html").read_text(encoding="utf-8") == "<main>current</main>"
    assert (destination / "assets" / "app.js").read_text(encoding="utf-8") == "console.log('current')"
    assert not (destination / "obsolete.js").exists()


def test_frozen_config_uses_only_bundle_defaults_and_forces_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_config = tmp_path / "bundle" / "config"
    bundle_config.mkdir(parents=True)
    (bundle_config / "default.yaml").write_text(
        "server:\n  host: 0.0.0.0\n  port: 8765\n", encoding="utf-8"
    )
    external_config = tmp_path / "external"
    external_config.mkdir()
    (external_config / "default.yaml").write_text("server:\n  host: 0.0.0.0\n", encoding="utf-8")

    monkeypatch.setattr(config_module, "_FROZEN", True)
    monkeypatch.setattr(config_module, "_CONFIG_DIR", bundle_config)
    monkeypatch.setattr(config_module, "_PROJECT_ROOT", tmp_path / "release")
    monkeypatch.setattr(Config, "_instance", None)

    frozen_config = Config.load()

    assert frozen_config["server"]["host"] == "127.0.0.1"
    with pytest.raises(PathIsolationError, match="bundled resources"):
        Config.load(external_config)
    assert web_main._server_host({"host": "0.0.0.0"}) == "127.0.0.1"


def test_confirmed_plan_is_atomically_single_use(sqlite_store: SQLiteStore) -> None:
    created = create_plan("archive.clean", {"target_dir": "data/parquet"}, sqlite=sqlite_store)
    plan_id = created["result"]["data"]["plan_id"]
    assert confirm_plan(plan_id, sqlite=sqlite_store)["result"]["status"] == "ok"

    error, summary = consume_confirmed_plan("archive.clean", plan_id=plan_id, sqlite=sqlite_store)
    assert error is None
    assert summary == {"target_dir": "data/parquet"}

    error, summary = consume_confirmed_plan("archive.clean", plan_id=plan_id, sqlite=sqlite_store)
    assert error is not None
    assert error["result"]["error_code"] == "E101"
    assert summary is None
    assert sqlite_store.query("SELECT status FROM plans WHERE plan_id = ?", [plan_id]) == [
        {"status": "consumed"}
    ]


def test_cninfo_and_pdf_downloads_require_https() -> None:
    normalized = CNINFOAdapter._normalize_announcement(
        {"adjunctUrl": "/finalpage/2026-01-01/notice.PDF"}, "000001"
    )

    assert normalized["pdf_url"] == "https://static.cninfo.com.cn/finalpage/2026-01-01/notice.PDF"
    assert "https://www.cninfo.com.cn" in __import__("app.core.adapters.cninfo_adapter", fromlist=["x"])._CNINFO_BASE
    assert __import__("app.core.pdf.manager", fromlist=["x"])._PDF_BASE.startswith("https://")


def test_pdf_archives_stay_under_the_configured_profile_root(
    database_paths,
    sqlite_store: SQLiteStore,
) -> None:
    Config({"pdf": {"archive_root": "archive_pdf"}}, paths=database_paths)
    manager = PDFManager(sqlite=sqlite_store)
    source = manager.hot_dir / "000001" / "notice.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-safe")

    rejected = manager.archive_pdfs("000001", target_dir="../outside")

    assert "configured relative root" in rejected["error"] or "must match" in rejected["error"]
    assert source.exists()
    assert manager.archive_pdfs("000001", target_dir="data/archive_pdf")["status"] == "ok"
    manifest = sqlite_store.query("SELECT archive_path FROM pdf_archive_manifest")[0]
    assert Path(manifest["archive_path"]).is_relative_to(database_paths.run_root / "data" / "archive_pdf")


def test_pdf_archive_root_rejects_configured_traversal(
    database_paths,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"pdf": {"archive_root": "../outside"}}, paths=database_paths),
    )

    with pytest.raises(PathIsolationError, match="remain under"):
        PDFManager(sqlite=sqlite_store)
