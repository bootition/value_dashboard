"""The CLI must not bypass the validated database path boundary."""

from __future__ import annotations

import ast
from pathlib import Path


def test_cli_and_protocol_never_construct_zero_argument_database_dependencies() -> None:
    project_root = Path(__file__).parents[2]
    violations: list[str] = []
    for relative_path in ("app/cli/main.py", "app/cli/protocol.py"):
        path = project_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in {
                "DuckDBStore", "SQLiteStore", "DataInitializer", "PriceBackfiller",
                "IncrementalUpdater", "IndicatorCalculator", "DSLEngine", "ScreeningEngine",
                "BackupManager", "PDFManager", "CorrectionManager",
            } and not node.args and not node.keywords:
                violations.append(f"{relative_path}:{node.lineno}:{node.func.id}")
    assert violations == []


def test_restore_execution_requires_a_specific_plan_id() -> None:
    source = (Path(__file__).parents[2] / "app" / "cli" / "main.py").read_text(encoding="utf-8")
    assert 'plan_id: str = typer.Option(..., "--plan-id"' in source
    assert "backup_path does not match the confirmed plan" in source


def test_backup_can_prompt_for_a_hidden_password() -> None:
    source = (Path(__file__).parents[2] / "app" / "cli" / "main.py").read_text(encoding="utf-8")
    assert '"--prompt-password"' in source
    assert 'typer.prompt("Backup password", hide_input=True, confirmation_prompt=True)' in source


def test_archive_cleanup_has_a_confirmed_execution_path() -> None:
    source = (Path(__file__).parents[2] / "app" / "cli" / "main.py").read_text(encoding="utf-8")
    assert '@archive_app.command("clean_execute")' in source
    assert 'consume_confirmed_plan("archive.clean", plan_id=plan_id, sqlite=sqlite)' in source


def test_scoped_refetch_has_a_confirmed_execution_path() -> None:
    source = (Path(__file__).parents[2] / "app" / "cli" / "main.py").read_text(encoding="utf-8")
    assert '@data_app.command("refetch_execute")' in source
    assert 'consume_confirmed_plan("data.refetch", plan_id=plan_id, sqlite=sqlite)' in source
