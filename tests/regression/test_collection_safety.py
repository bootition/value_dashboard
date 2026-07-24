"""Verify that archived legacy tests are excluded from pytest collection,
that the root conftest hook is wired correctly, and that active test files
contain no module-level calls to known production-DB mutators.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from _pytest_policy import archived_root, is_archived_legacy_test

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DATABASES = (
    PROJECT_ROOT / "data" / "valuedashboard.duckdb",
    PROJECT_ROOT / "data" / "valuedashboard.sqlite",
)

# ── helpers ────────────────────────────────────────────────────────────


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_file_state() -> dict[Path, str | None]:
    paths = [
        *PRODUCTION_DATABASES,
        *(Path(f"{path}{suffix}") for path in PRODUCTION_DATABASES for suffix in ("-wal", "-shm")),
    ]
    return {path: _sha256(path) if path.exists() else None for path in paths}


# ── existing tests (unchanged) ─────────────────────────────────────────


def test_pytest_discovers_only_regression_tests() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file_handle:
        config = tomllib.load(file_handle)

    pytest_config = config["tool"]["pytest"]["ini_options"]
    assert pytest_config["testpaths"] == ["tests/regression"]


def test_collect_only_does_not_modify_production_databases() -> None:
    assert all(path.is_file() for path in PRODUCTION_DATABASES)
    state_before = _database_file_state()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    state_after = _database_file_state()
    assert result.returncode == 0, result.stderr
    assert state_after == state_before


# ── new: policy unit tests ─────────────────────────────────────────────


class TestArchivedLegacyTestPolicy:
    """``is_archived_legacy_test`` recognises every kind of archived path."""

    def test_archived_directory_returns_true(self) -> None:
        assert is_archived_legacy_test(archived_root()) is True

    def test_archived_python_file_returns_true(self) -> None:
        archived_file = archived_root() / "test_m10_acceptance.py"
        assert is_archived_legacy_test(archived_file) is True

    def test_active_regression_dir_returns_false(self) -> None:
        regression_dir = PROJECT_ROOT / "tests" / "regression"
        assert is_archived_legacy_test(regression_dir) is False

    def test_active_regression_file_returns_false(self) -> None:
        this_file = Path(__file__).resolve()
        assert is_archived_legacy_test(this_file) is False

    def test_unrelated_dir_returns_false(self) -> None:
        assert is_archived_legacy_test(PROJECT_ROOT / "app") is False

    def test_string_path_works(self) -> None:
        assert is_archived_legacy_test(str(archived_root())) is True


class TestRootConftestExists:
    """The root ``conftest.py`` must exist and delegate to the policy module."""

    ROOT_CONFTEST = PROJECT_ROOT / "conftest.py"

    def test_root_conftest_is_file(self) -> None:
        assert self.ROOT_CONFTEST.is_file(), (
            f"Expected root conftest at {self.ROOT_CONFTEST}"
        )

    def test_root_conftest_imports_policy(self) -> None:
        text = self.ROOT_CONFTEST.read_text(encoding="utf-8")
        assert "from _pytest_policy import is_archived_legacy_test" in text

    def test_root_conftest_contains_pytest_ignore_collect(self) -> None:
        text = self.ROOT_CONFTEST.read_text(encoding="utf-8")
        assert "def pytest_ignore_collect" in text


# ── new: static AST guard against module-level mutators ─────────────────

KNOWN_DANGEROUS_MUTATORS = (
    "init_all_schema",
    "Config.load",
)

KNOWN_DANGEROUS_ZERO_ARG_CALLABLES = (
    "DuckDBStore",
    "SQLiteStore",
)


def _iter_active_test_py_files() -> list[Path]:
    """Yield every ``*.py`` under ``tests/`` excluding ``__pycache__``."""
    result: list[Path] = []
    for path in (PROJECT_ROOT / "tests").rglob("*.py"):
        if path.name == "__init__.py":
            continue
        resolved = path.resolve()
        if is_archived_legacy_test(resolved):
            continue
        result.append(resolved)
    return result


def _callable_name(node: ast.AST) -> str | None:
    """Extract the dotted name from a callable AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value  # type: ignore[assignment]
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        elif isinstance(cur, ast.Call):
            # e.g. Config.load()() — skip call chains where the attribute
            # qualifier itself is a call result
            return None
        else:
            return None
        return ".".join(reversed(parts))
    return None


def _is_module_level_dangerous_call(tree: ast.AST) -> list[str]:
    """Return every dangerous callable name found at module level."""
    found: list[str] = []
    for child in ast.walk(tree):
        if not isinstance(child, ast.Module):
            continue
        for stmt in child.body:
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
                continue
            call = stmt.value
            name = _callable_name(call.func)
            if name is None:
                continue

            # Check named mutators
            for mutator in KNOWN_DANGEROUS_MUTATORS:
                if name == mutator or name.endswith(f".{mutator}"):
                    found.append(name)
                    break
            else:
                # Check zero-arg instantiation of known store classes
                if name in KNOWN_DANGEROUS_ZERO_ARG_CALLABLES:
                    n_args = len(call.args) + sum(
                        1 for kw in call.keywords if kw.arg not in ("self", "cls")
                    )
                    if n_args == 0:
                        found.append(name)
    return found


class TestActiveTestsHaveNoModuleLevelMutators:
    """Static AST scan — never imports or executes the scanned modules."""

    @pytest.fixture(scope="class")
    def active_test_files(self) -> list[Path]:
        return _iter_active_test_py_files()

    @pytest.fixture(scope="class")
    def scan_results(self, active_test_files: list[Path]) -> dict[str, list[str]]:
        results: dict[str, list[str]] = {}
        for path in active_test_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            dangerous = _is_module_level_dangerous_call(tree)
            if dangerous:
                results[str(path.relative_to(PROJECT_ROOT))] = dangerous
        return results

    def test_no_init_all_schema_at_module_level(
        self, scan_results: dict[str, list[str]]
    ) -> None:
        assert "init_all_schema" not in str(scan_results), (
            f"Module-level init_all_schema() calls found: {scan_results}"
        )

    def test_no_config_load_at_module_level(
        self, scan_results: dict[str, list[str]]
    ) -> None:
        assert "Config.load" not in str(scan_results), (
            f"Module-level Config.load() calls found: {scan_results}"
        )

    def test_no_zero_arg_duckdbstore_at_module_level(
        self, scan_results: dict[str, list[str]]
    ) -> None:
        assert "DuckDBStore" not in str(scan_results), (
            f"Module-level DuckDBStore() calls found: {scan_results}"
        )

    def test_no_zero_arg_sqlitestore_at_module_level(
        self, scan_results: dict[str, list[str]]
    ) -> None:
        assert "SQLiteStore" not in str(scan_results), (
            f"Module-level SQLiteStore() calls found: {scan_results}"
        )
