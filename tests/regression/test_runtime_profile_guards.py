"""Runtime entrypoints must not silently grant formal database authority."""

from __future__ import annotations

from pathlib import Path


def test_python_entrypoints_do_not_assign_formal_environment() -> None:
    project_root = Path(__file__).parents[2]
    for relative_path in ("app/cli/main.py", "app/web/main.py"):
        source = (project_root / relative_path).read_text(encoding="utf-8")
        assert 'os.environ["VD_ENV"] =' not in source
        assert 'os.environ["VD_FORMAL_ACK"] =' not in source


def test_batch_entrypoints_create_only_their_own_canonical_formal_profile() -> None:
    project_root = Path(__file__).parents[2]
    for relative_path in ("start.bat", "vd.bat"):
        source = (project_root / relative_path).read_text(encoding="utf-8")
        assert 'set "VD_ENV=formal"' in source
        assert 'set "VD_FORMAL_ACK=confirmed"' in source
        assert 'VD_DUCKDB_PATH=%RELEASE_ROOT%\\data\\valuedashboard.duckdb' in source
        assert 'goto :missing_profile' not in source


def test_frozen_entrypoint_does_not_grant_formal_database_authority() -> None:
    source = (Path(__file__).parents[2] / "app" / "web" / "main.py").read_text(encoding="utf-8")
    assert 'os.environ["VD_ENV"] =' not in source
    assert 'os.environ["VD_FORMAL_ACK"] =' not in source
