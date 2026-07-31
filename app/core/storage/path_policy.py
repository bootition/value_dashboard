"""Database path policy — environment-based isolation for formal/test/staging."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class VdEnv(str, Enum):
    FORMAL = "formal"
    TEST = "test"
    STAGING = "staging"


class PathIsolationError(Exception):
    pass


# Frozen binaries keep code/resources under _internal but must retain mutable
# formal data beside the launcher, not inside the bundle.
_PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[3]
)
_DATA_ROOT = _PROJECT_ROOT / "data"
_FORMAL_DUCKDB = _DATA_ROOT / "valuedashboard.duckdb"
_FORMAL_SQLITE = _DATA_ROOT / "valuedashboard.sqlite"


def project_root() -> Path:
    return _PROJECT_ROOT


def data_root() -> Path:
    return _DATA_ROOT


def canonicalize_path(path: str | Path, *, require_absolute: bool = True) -> Path:
    candidate = Path(path)
    if require_absolute and not candidate.is_absolute():
        raise PathIsolationError(f"Path must be absolute: {path}")
    return candidate.resolve(strict=False)


@dataclass(frozen=True, init=False)
class DatabasePathSet:
    env: VdEnv
    duckdb_path: Path
    sqlite_path: Path
    run_root: Path

    def __init__(
        self,
        *,
        env: VdEnv | None = None,
        duckdb_path: Path | None = None,
        sqlite_path: Path | None = None,
        run_root: Path | None = None,
    ) -> None:
        if env is None or duckdb_path is None or sqlite_path is None or run_root is None:
            raise PathIsolationError(
                "DatabasePathSet requires env, duckdb_path, sqlite_path, and run_root"
            )
        if not isinstance(env, VdEnv):
            raise PathIsolationError(f"env must be a VdEnv value, got {env!r}")
        object.__setattr__(self, "env", env)
        object.__setattr__(self, "duckdb_path", Path(duckdb_path))
        object.__setattr__(self, "sqlite_path", Path(sqlite_path))
        object.__setattr__(self, "run_root", Path(run_root))

    @classmethod
    def from_env(cls) -> DatabasePathSet:
        """Create paths from environment variables.
        """
        env_raw = os.environ.get("VD_ENV")
        duckdb_raw = os.environ.get("VD_DUCKDB_PATH")
        sqlite_raw = os.environ.get("VD_SQLITE_PATH")

        missing = [
            name
            for name, value in (
                ("VD_ENV", env_raw),
                ("VD_DUCKDB_PATH", duckdb_raw),
                ("VD_SQLITE_PATH", sqlite_raw),
            )
            if not value
        ]
        if missing:
            raise PathIsolationError(f"Missing environment variables: {', '.join(missing)}")
        try:
            env = VdEnv(env_raw)
        except ValueError as error:
            raise PathIsolationError(f"Unknown VD_ENV value: {env_raw!r}") from error

        if env is VdEnv.TEST:
            root_raw = os.environ.get("VD_TEST_RUN_ROOT")
            if not root_raw:
                raise PathIsolationError("VD_TEST_RUN_ROOT is required for test profile")
        elif env is VdEnv.STAGING:
            root_raw = os.environ.get("VD_STAGING_ROOT")
            if not root_raw:
                raise PathIsolationError("VD_STAGING_ROOT is required for staging profile")
        else:
            if os.environ.get("VD_FORMAL_ACK") != "confirmed":
                raise PathIsolationError("VD_FORMAL_ACK=confirmed is required for formal profile")
            root_raw = str(Path(duckdb_raw).parent)

        return cls(
            env=env,
            duckdb_path=Path(duckdb_raw),
            sqlite_path=Path(sqlite_raw),
            run_root=Path(root_raw),
        ).validate()

    def validate(self) -> DatabasePathSet:
        run_root = canonicalize_path(self.run_root)
        duckdb_path = canonicalize_path(self.duckdb_path)
        sqlite_path = canonicalize_path(self.sqlite_path)

        if duckdb_path.parent != run_root or sqlite_path.parent != run_root:
            raise PathIsolationError("DuckDB and SQLite paths must be siblings under run_root")
        if duckdb_path == sqlite_path:
            raise PathIsolationError("DuckDB and SQLite paths must be distinct sibling files")
        if duckdb_path.name != "valuedashboard.duckdb":
            raise PathIsolationError("DuckDB path must end with valuedashboard.duckdb")
        if sqlite_path.name != "valuedashboard.sqlite":
            raise PathIsolationError("SQLite path must end with valuedashboard.sqlite")

        if self.env in {VdEnv.TEST, VdEnv.STAGING}:
            project = canonicalize_path(_PROJECT_ROOT)
            if _is_inside(run_root, project):
                raise PathIsolationError(
                    "Test/staging database roots must be outside the repository"
                )
        else:
            formal_root = canonicalize_path(_DATA_ROOT)
            if run_root != formal_root:
                raise PathIsolationError("Formal run_root must be the repository data directory")
            if duckdb_path != canonicalize_path(_FORMAL_DUCKDB):
                raise PathIsolationError("Formal DuckDB path does not match the canonical file")
            if sqlite_path != canonicalize_path(_FORMAL_SQLITE):
                raise PathIsolationError("Formal SQLite path does not match the canonical file")
            if os.environ.get("VD_FORMAL_ACK") != "confirmed":
                raise PathIsolationError("VD_FORMAL_ACK=confirmed is required for formal profile")

        return _validated_path_set(self.env, duckdb_path, sqlite_path, run_root)


def _is_inside(child: Path, parent: Path) -> bool:
    child_str = os.path.normcase(str(child)).rstrip("\\/")
    parent_str = os.path.normcase(str(parent)).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def _validated_path_set(
    env: VdEnv,
    duckdb_path: Path,
    sqlite_path: Path,
    run_root: Path,
) -> DatabasePathSet:
    instance = object.__new__(DatabasePathSet)
    object.__setattr__(instance, "env", env)
    object.__setattr__(instance, "duckdb_path", duckdb_path)
    object.__setattr__(instance, "sqlite_path", sqlite_path)
    object.__setattr__(instance, "run_root", run_root)
    return instance


def resolve_and_validate_paths(env: VdEnv | None = None) -> DatabasePathSet:
    paths = DatabasePathSet.from_env()
    if env is not None and paths.env is not env:
        raise PathIsolationError(f"Expected VD_ENV={env.value}, got {paths.env.value}")
    return paths


def require_formal_maintenance_paths() -> DatabasePathSet:
    """Return the explicit formal profile required by repository maintenance tools."""
    return resolve_and_validate_paths(VdEnv.FORMAL)
