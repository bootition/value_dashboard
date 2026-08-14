"""Verify that archived legacy tests are excluded from pytest collection,
that the root conftest hook is wired correctly, and that active test files
contain no module-level calls to known production-DB mutators.

2026-08-14 红队 P2-9（合约 path-isolation-contract.md §8.4）：
- 移除递归 subprocess 的 collect-only 测试（正式库哈希/嵌套 pytest 由
  S1 包装器指纹覆盖，见 s1-pytest.ps1 / s1-path-preflight.ps1）。
- AST 守卫增强：KNOWN_DANGEROUS_MUTATORS 扩至 Config.current，并把
  DuckDBStore/SQLiteStore 的模块级零参实例化从"仅表达式语句"扩展到
  赋值/注解赋值等语句（防御纵深）。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from _pytest_policy import archived_root, is_archived_legacy_test

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ── existing tests (unchanged) ─────────────────────────────────────────


def test_pytest_discovers_only_regression_tests() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file_handle:
        config = tomllib.load(file_handle)

    pytest_config = config["tool"]["pytest"]["ini_options"]
    assert pytest_config["testpaths"] == ["tests/regression"]


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


# ── AST guard against module-level mutators (P2-9 增强) ─────────────────

KNOWN_DANGEROUS_MUTATORS = (
    "init_all_schema",
    "Config.load",
    "Config.current",
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
        cur: ast.AST = node
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


def _dangerous_calls_in_expression(expr: ast.AST) -> list[str]:
    """Return dangerous call names inside one module-level expression.

    Walks the expression tree but never descends into function/lambda bodies
    (those live in separate statements and are not module-level executions).
    """
    found: list[str] = []
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        name = _callable_name(node.func)
        if name is None:
            continue
        for mutator in KNOWN_DANGEROUS_MUTATORS:
            if name == mutator or name.endswith(f".{mutator}"):
                found.append(name)
                break
        else:
            if name in KNOWN_DANGEROUS_ZERO_ARG_CALLABLES:
                n_args = len(node.args) + sum(
                    1 for kw in node.keywords if kw.arg not in ("self", "cls")
                )
                if n_args == 0:
                    found.append(name)
    return found


def _module_level_dangerous_calls(tree: ast.AST) -> list[str]:
    """Return every dangerous callable found at module level.

    Covers expression statements (``Config.load()``) and assignments
    (``store = DuckDBStore()``), which the pre-P2-9 guard missed.
    """
    found: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr):
            found.extend(_dangerous_calls_in_expression(stmt.value))
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(stmt, ast.Assign):
                expressions = [stmt.value]
            elif isinstance(stmt, ast.AnnAssign):
                expressions = [stmt.value] if stmt.value is not None else []
            else:
                expressions = [stmt.value]
            for expr in expressions:
                found.extend(_dangerous_calls_in_expression(expr))
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
            dangerous = _module_level_dangerous_calls(tree)
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

    def test_no_config_current_at_module_level(
        self, scan_results: dict[str, list[str]]
    ) -> None:
        assert "Config.current" not in str(scan_results), (
            f"Module-level Config.current() calls found: {scan_results}"
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
