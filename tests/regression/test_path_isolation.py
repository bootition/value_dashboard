"""Pure path-policy tests: no DB, Store, Config, or business imports."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.core.storage.path_policy import (
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    canonicalize_path,
    require_formal_maintenance_paths,
)


ENVIRONMENT_VARIABLES = (
    "VD_ENV",
    "VD_FORMAL_ACK",
    "VD_DUCKDB_PATH",
    "VD_SQLITE_PATH",
    "VD_TEST_RUN_ROOT",
    "VD_STAGING_ROOT",
)


def clear_path_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_policy_module_has_only_allowed_imports() -> None:
    policy_path = Path(__file__).parents[2] / "app" / "core" / "storage" / "path_policy.py"
    tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    allowed = {
        "__future__",
        "os",
        "sys",
        "dataclasses",
        "enum",
        "pathlib",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "<relative>")
    assert imported <= allowed


def test_direct_construction_requires_every_field(tmp_path: Path) -> None:
    with pytest.raises(PathIsolationError, match="requires env"):
        DatabasePathSet(
            env=VdEnv.TEST,
            duckdb_path=tmp_path / "valuedashboard.duckdb",
            sqlite_path=tmp_path / "valuedashboard.sqlite",
        )


def test_fields_are_frozen_after_construction(tmp_path: Path) -> None:
    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=tmp_path / "valuedashboard.duckdb",
        sqlite_path=tmp_path / "valuedashboard.sqlite",
        run_root=tmp_path,
    ).validate()
    with pytest.raises(FrozenInstanceError):
        paths.run_root = tmp_path.parent  # type: ignore[misc]


def test_from_env_fails_closed_when_all_variables_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_path_environment(monkeypatch)
    with pytest.raises(PathIsolationError, match="VD_ENV"):
        DatabasePathSet.from_env()


def test_policy_source_never_assigns_environment_defaults() -> None:
    policy_path = Path(__file__).parents[2] / "app" / "core" / "storage" / "path_policy.py"
    source = policy_path.read_text(encoding="utf-8")
    assert 'os.environ["VD_ENV"] =' not in source
    assert 'os.environ["VD_FORMAL_ACK"] =' not in source


def test_from_env_rejects_unknown_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_path_environment(monkeypatch)
    monkeypatch.setenv("VD_ENV", "production")
    monkeypatch.setenv("VD_DUCKDB_PATH", str(tmp_path / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(tmp_path / "valuedashboard.sqlite"))
    with pytest.raises(PathIsolationError, match="Unknown VD_ENV"):
        DatabasePathSet.from_env()


def test_test_environment_requires_run_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_path_environment(monkeypatch)
    monkeypatch.setenv("VD_ENV", "test")
    monkeypatch.setenv("VD_DUCKDB_PATH", str(tmp_path / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(tmp_path / "valuedashboard.sqlite"))
    with pytest.raises(PathIsolationError, match="VD_TEST_RUN_ROOT"):
        DatabasePathSet.from_env()


def test_formal_environment_requires_ack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_path_environment(monkeypatch)
    monkeypatch.setenv("VD_ENV", "formal")
    monkeypatch.setenv("VD_DUCKDB_PATH", str(tmp_path / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(tmp_path / "valuedashboard.sqlite"))
    with pytest.raises(PathIsolationError, match="VD_FORMAL_ACK"):
        DatabasePathSet.from_env()


def test_maintenance_paths_reject_a_test_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_path_environment(monkeypatch)
    run_root = tmp_path / "run"
    monkeypatch.setenv("VD_ENV", "test")
    monkeypatch.setenv("VD_TEST_RUN_ROOT", str(run_root))
    monkeypatch.setenv("VD_DUCKDB_PATH", str(run_root / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(run_root / "valuedashboard.sqlite"))

    with pytest.raises(PathIsolationError, match="Expected VD_ENV=formal"):
        require_formal_maintenance_paths()


def test_from_env_accepts_safe_nonexistent_external_test_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_path_environment(monkeypatch)
    run_root = tmp_path / "new-run"
    monkeypatch.setenv("VD_ENV", "test")
    monkeypatch.setenv("VD_TEST_RUN_ROOT", str(run_root))
    monkeypatch.setenv("VD_DUCKDB_PATH", str(run_root / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(run_root / "valuedashboard.sqlite"))

    paths = DatabasePathSet.from_env()

    assert paths.env is VdEnv.TEST
    assert paths.run_root == canonicalize_path(run_root)
    assert not run_root.exists()


def test_existing_instance_does_not_follow_environment_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_path_environment(monkeypatch)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.setenv("VD_ENV", "test")
    monkeypatch.setenv("VD_TEST_RUN_ROOT", str(first_root))
    monkeypatch.setenv("VD_DUCKDB_PATH", str(first_root / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(first_root / "valuedashboard.sqlite"))
    first = DatabasePathSet.from_env()
    monkeypatch.setenv("VD_TEST_RUN_ROOT", str(second_root))
    monkeypatch.setenv("VD_DUCKDB_PATH", str(second_root / "valuedashboard.duckdb"))
    monkeypatch.setenv("VD_SQLITE_PATH", str(second_root / "valuedashboard.sqlite"))

    second = DatabasePathSet.from_env()

    assert first.run_root != second.run_root


def test_validate_rejects_repository_data_path() -> None:
    data = Path(__file__).parents[2] / "data"
    with pytest.raises(PathIsolationError, match="repository"):
        DatabasePathSet(
            env=VdEnv.TEST,
            duckdb_path=data / "valuedashboard.duckdb",
            sqlite_path=data / "valuedashboard.sqlite",
            run_root=data,
        ).validate()


def test_validate_rejects_non_sibling_database_paths(tmp_path: Path) -> None:
    with pytest.raises(PathIsolationError, match="sibling"):
        DatabasePathSet(
            env=VdEnv.TEST,
            duckdb_path=tmp_path / "one" / "valuedashboard.duckdb",
            sqlite_path=tmp_path / "two" / "valuedashboard.sqlite",
            run_root=tmp_path,
        ).validate()


def test_validate_does_not_create_nonexistent_root(tmp_path: Path) -> None:
    run_root = tmp_path / "not-created"
    DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=run_root / "valuedashboard.duckdb",
        sqlite_path=run_root / "valuedashboard.sqlite",
        run_root=run_root,
    ).validate()
    assert not run_root.exists()


def test_canonicalize_rejects_relative_path() -> None:
    with pytest.raises(PathIsolationError, match="absolute"):
        canonicalize_path(Path("relative/test.duckdb"))


def test_canonicalize_allows_absolute_path() -> None:
    result = canonicalize_path(Path("C:/some/path"))
    assert result.is_absolute()


def test_write_lock_checks_windows_process_exit_code() -> None:
    source = (Path(__file__).parents[2] / "app" / "core" / "storage" / "duckdb_store.py").read_text(
        encoding="utf-8"
    )
    assert "GetExitCodeProcess" in source
    assert "exit_code.value == 259" in source


# ─── 2026-08-14 红队 F4：冻结态双击 exe 无环境变量自动推导正式路径 ───

def test_from_env_frozen_formal_defaults_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结态（打包 exe）无任何 VD_* 环境变量时推导 exe 同级 data/ 正式路径。"""
    import sys

    clear_path_environment(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        paths = DatabasePathSet.from_env()
    finally:
        monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert paths.env is VdEnv.FORMAL
    assert paths.duckdb_path.name == "valuedashboard.duckdb"
    assert paths.sqlite_path.name == "valuedashboard.sqlite"
    assert paths.run_root.name == "data"


def test_from_env_frozen_formal_skips_ack_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结态下 VD_FORMAL_ACK 缺失不报错（双击即显式意图）。"""
    import sys

    clear_path_environment(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("VD_ENV", "formal")
    try:
        paths = DatabasePathSet.from_env()
    finally:
        monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.env is VdEnv.FORMAL


def test_from_env_frozen_test_profile_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结态下显式 test 环境不得回退正式路径（隔离防线不变）。"""
    import sys

    clear_path_environment(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("VD_ENV", "test")
    try:
        with pytest.raises(PathIsolationError, match="Missing environment variables"):
            DatabasePathSet.from_env()
    finally:
        monkeypatch.setattr(sys, "frozen", False, raising=False)


def test_non_frozen_missing_environment_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码/venv 形态不受 F4 影响：无环境变量必须 fail-closed。"""
    clear_path_environment(monkeypatch)
    with pytest.raises(PathIsolationError, match="VD_ENV"):
        DatabasePathSet.from_env()
