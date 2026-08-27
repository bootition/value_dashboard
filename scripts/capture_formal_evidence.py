"""Capture a read-only, hash-bound formal-data evidence record."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.data_quality import build_data_quality_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import require_formal_maintenance_paths
from app.core.storage.sqlite_store import SQLiteStore


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture() -> dict:
    """Read the formal stores only; never initialize schema or write state."""
    paths = require_formal_maintenance_paths()
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    duck_counts = {
        table: duck.read_query(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
        for table in (
            "stock_meta", "price_daily_raw", "price_daily_qfq", "balance_sheet",
            "income_statement", "cash_flow", "dividends", "xdxr", "indicator_snapshot",
            "fetch_batch", "source_audit", "raw_response_archive",
        )
    }
    sqlite_counts = {
        table: sqlite.query(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
        for table in (
            "manual_overrides", "screening_rules", "screening_results", "watchlist",
            "retry_list", "missing_list", "job_logs",
        )
    }
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "profile": "formal",
        "database_paths": {
            "duckdb": str(paths.duckdb_path),
            "sqlite": str(paths.sqlite_path),
        },
        "sha256": {
            "duckdb": _sha256(paths.duckdb_path),
            "sqlite": _sha256(paths.sqlite_path),
            "duckdb_wal": _sha256(paths.duckdb_path.with_suffix(".duckdb.wal")),
            "sqlite_wal": _sha256(paths.sqlite_path.with_name(paths.sqlite_path.name + "-wal")),
            "sqlite_shm": _sha256(paths.sqlite_path.with_name(paths.sqlite_path.name + "-shm")),
        },
        "table_counts": {"duckdb": duck_counts, "sqlite": sqlite_counts},
        "data_quality": build_data_quality_status(duck, sqlite),
        "verdict": "PASS" if build_data_quality_status(duck, sqlite)["minimum_data_readiness"]["ready"] else "BLOCK",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = capture()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
