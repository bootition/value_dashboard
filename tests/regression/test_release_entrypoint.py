"""Release entrypoint contracts that do not require a frozen executable."""

from __future__ import annotations

from pathlib import Path


def test_packaged_entrypoint_routes_arguments_to_cli() -> None:
    source = (Path(__file__).parents[2] / "app" / "launcher.py").read_text(encoding="utf-8")
    assert "from app.cli.main import app" in source
    assert "from app.web.main import run_server" in source


def test_release_scripts_keep_formal_data_beside_the_executable() -> None:
    root = Path(__file__).parents[2]
    for relative_path in ("start.bat", "vd.bat"):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert 'set "RELEASE_ROOT=%CD%"' in source
        assert '%RELEASE_ROOT%\\data\\valuedashboard.duckdb' in source
        assert '%RELEASE_ROOT%\\data\\valuedashboard.sqlite' in source


def test_release_scripts_bootstrap_an_empty_distribution_profile() -> None:
    root = Path(__file__).parents[2]
    for relative_path in ("start.bat", "vd.bat"):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert 'set "VD_ENV=formal"' in source
        assert 'set "VD_DUCKDB_PATH=%RELEASE_ROOT%\\data\\valuedashboard.duckdb"' in source
        assert 'set "VD_SQLITE_PATH=%RELEASE_ROOT%\\data\\valuedashboard.sqlite"' in source
        assert "goto :eof" in source or "goto :end" in source


def test_spec_uses_the_unified_packaged_entrypoint() -> None:
    source = (Path(__file__).parents[2] / "value-dashboard.spec").read_text(encoding="utf-8")
    assert "['app/launcher.py']" in source


def test_release_contract_excludes_mutable_formal_data() -> None:
    source = (Path(__file__).parents[2] / "value-dashboard.spec").read_text(encoding="utf-8")
    assert "Mutable user data is deliberately" in source
    assert "required_directory('data')" not in source
    assert "required_file('data/valuedashboard.duckdb')" not in source


def test_release_contract_includes_akshare_runtime_resources() -> None:
    source = (Path(__file__).parents[2] / "value-dashboard.spec").read_text(encoding="utf-8")

    assert "collect_data_files" in source
    assert "collect_data_files('akshare')" in source


def test_clean_release_builder_uses_locked_dependencies() -> None:
    root = Path(__file__).parents[2]
    source = (root / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
    assert "npm ci" in source
    assert "s1-pytest.ps1" in source
    assert "npm run lint" in source
    assert "npm run test" in source
    assert "npm run build" in source
    assert '"uv.lock"' in source
    assert "uv run --locked --extra release python -m PyInstaller" in source
    assert 'release = [' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert (root / "uv.lock").is_file()
    assert "IsPathFullyQualified($OutputDirectory)" in source
    assert "Copy-Item -LiteralPath" in source
    assert "Release must not package formal data" in source
    package = (root / "frontend" / "package.json").read_text(encoding="utf-8")
    assert '"test": "node --experimental-strip-types --test' in package
