"""Data-maintenance scripts must honor the same database boundary as the application."""

from __future__ import annotations

import ast
from pathlib import Path


WRITER_SCRIPTS = (
    "import_csmar.py",
    "supplement_akshare.py",
    "import_csv_to_db.py",
    "patch_deducted_profit.py",
)


def test_data_writer_scripts_use_validated_store_instead_of_direct_connections() -> None:
    scripts_dir = Path(__file__).parents[2] / "scripts"
    for script_name in WRITER_SCRIPTS:
        source = (scripts_dir / script_name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=script_name)
        direct_connections = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "duckdb"
            and node.func.attr == "connect"
        ]
        assert direct_connections == [], f"{script_name} bypasses DuckDBStore at {direct_connections}"
        assert "require_formal_maintenance_paths" in source
        assert "DuckDBStore(paths=paths)" in source
