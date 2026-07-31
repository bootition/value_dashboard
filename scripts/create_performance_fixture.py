"""Create an external, consistent performance fixture without mutating formal data."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import duckdb

COMPOSITE_NAME = "performance_value_blend"
COMPOSITE_EXPRESSION = "pe_ttm + pb_mrq"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_fixture(source_root: Path, target_root: Path) -> dict[str, object]:
    """Copy both stores consistently into a new external fixture root."""
    source_root = source_root.resolve()
    target_root = target_root.resolve()
    source_duck = source_root / "valuedashboard.duckdb"
    source_sqlite = source_root / "valuedashboard.sqlite"
    if not source_duck.is_file() or not source_sqlite.is_file():
        raise ValueError("source root must contain valuedashboard.duckdb and valuedashboard.sqlite")
    if target_root == source_root or source_root in target_root.parents:
        raise ValueError("performance fixture target must be outside the formal source root")
    if target_root.exists():
        raise ValueError(f"refusing to overwrite existing fixture root: {target_root}")

    target_root.mkdir(parents=True)
    target_duck = target_root / "valuedashboard.duckdb"
    target_sqlite = target_root / "valuedashboard.sqlite"
    try:
        # DuckDB copies from a read-only attached source, rather than copying a
        # live database file and risking an inconsistent file/WAL combination.
        with duckdb.connect(str(target_duck)) as connection:
            escaped = str(source_duck).replace("'", "''")
            connection.execute(f"ATTACH '{escaped}' AS formal_source (READ_ONLY)")
            tables = connection.execute(
                """SELECT table_name FROM duckdb_tables()
                   WHERE database_name = 'formal_source' AND schema_name = 'main'
                   ORDER BY table_name"""
            ).fetchall()
            if not tables:
                raise ValueError("formal source contains no main-schema tables")
            for (table_name,) in tables:
                quoted = table_name.replace('"', '""')
                connection.execute(
                    f'CREATE TABLE "{quoted}" AS SELECT * FROM formal_source.main."{quoted}"'
                )
            connection.execute("DETACH formal_source")

        # SQLite's online backup API gives the same snapshot guarantee without
        # altering the source database or its journal files.
        with sqlite3.connect(f"file:{source_sqlite}?mode=ro", uri=True) as source:
            with sqlite3.connect(target_sqlite) as target:
                source.backup(target)

        content_hash = hashlib.sha256(COMPOSITE_EXPRESSION.encode("utf-8")).hexdigest()
        with sqlite3.connect(target_sqlite) as connection:
            connection.execute(
                """INSERT INTO dsl_expressions
                   (name, version, expression_text, status, description, direction, historical_capable, content_hash)
                   VALUES (?, 1, ?, 'published', 'PRD 19.1 fixed performance fixture', 'lower_is_better', 0, ?)""",
                [COMPOSITE_NAME, COMPOSITE_EXPRESSION, content_hash],
            )

        # The benchmark must exercise a coherent current research pool. Exclude
        # only fixture-local stocks whose latest snapshot and three statements
        # disagree; never change the formal source during performance work.
        with duckdb.connect(str(target_duck)) as connection:
            excluded = connection.execute(
                """
                WITH snapshot_dates AS (
                    SELECT stock_code, MAX(report_date) AS report_date
                    FROM indicator_snapshot GROUP BY stock_code
                ), balance_dates AS (
                    SELECT stock_code, MAX(report_date) AS report_date
                    FROM balance_sheet GROUP BY stock_code
                ), income_dates AS (
                    SELECT stock_code, MAX(report_date) AS report_date
                    FROM income_statement GROUP BY stock_code
                ), cashflow_dates AS (
                    SELECT stock_code, MAX(report_date) AS report_date
                    FROM cash_flow GROUP BY stock_code
                )
                SELECT meta.stock_code
                FROM stock_meta meta
                JOIN snapshot_dates snapshot ON snapshot.stock_code = meta.stock_code
                LEFT JOIN balance_dates balance ON balance.stock_code = meta.stock_code
                LEFT JOIN income_dates income ON income.stock_code = meta.stock_code
                LEFT JOIN cashflow_dates cashflow ON cashflow.stock_code = meta.stock_code
                WHERE meta.is_listed IS TRUE
                  AND (balance.report_date IS DISTINCT FROM snapshot.report_date
                    OR income.report_date IS DISTINCT FROM snapshot.report_date
                    OR cashflow.report_date IS DISTINCT FROM snapshot.report_date)
                """
            ).fetchall()
            if excluded:
                connection.executemany(
                    "UPDATE stock_meta SET is_listed = FALSE WHERE stock_code = ?", excluded
                )

        fixture = {
            "schema_version": 1,
            "database_root": str(target_root),
            "source": {
                "root": str(source_root),
                "duckdb_sha256": _sha256(source_duck),
                "sqlite_sha256": _sha256(source_sqlite),
            },
            "fixture_local_excluded_incoherent_stocks": [code for (code,) in excluded],
            "composite_indicator": {"name": COMPOSITE_NAME, "version": 1},
            "rule": {
                "conditions": {
                    "logic": "AND",
                    "rules": [
                        {"field": "pe_ttm", "op": ">", "value": 0},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": "pe_ttm", "op": "is_not_null"},
                        {"field": COMPOSITE_NAME, "op": "is_not_null"},
                        {"field": "pe_ttm_industry_rank", "op": "<=", "value": 5000},
                    ],
                },
                "sort": [{"field": "pe_ttm_industry_rank", "direction": "asc"}],
                "columns": [
                    "stock_code", "name", "sw_level1", "sw_level2", "pe_ttm", "roe",
                    "debt_ratio", COMPOSITE_NAME, "pe_ttm_industry_rank",
                ],
            },
        }
        fixture_path = target_root / "screening-performance-fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"fixture": str(fixture_path), "target_root": str(target_root)}
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create_fixture(args.source_root, args.target_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
