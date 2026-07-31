"""Run the PRD 19.1 screening measurement against an explicit read-only fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, VdEnv
from app.core.storage.sqlite_store import SQLiteStore


def run_acceptance(fixture_path: Path, host_spec_path: Path | None = None) -> dict[str, Any]:
    """Measure a supplied hot-data fixture without creating or changing its databases."""
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1:
        raise ValueError("unsupported performance fixture schema_version")
    root = Path(fixture["database_root"]).resolve()
    paths = DatabasePathSet(
        env=VdEnv.STAGING,
        run_root=root,
        duckdb_path=root / "valuedashboard.duckdb",
        sqlite_path=root / "valuedashboard.sqlite",
    ).validate()
    if not paths.duckdb_path.is_file() or not paths.sqlite_path.is_file():
        raise ValueError("fixture must contain valuedashboard.duckdb and valuedashboard.sqlite")

    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    composite = fixture["composite_indicator"]
    rows = sqlite.query(
        """SELECT name, version, content_hash FROM dsl_expressions
           WHERE name = ? AND version = ? AND status = 'published'""",
        [composite["name"], composite["version"]],
    )
    if len(rows) != 1:
        raise ValueError("fixture composite indicator must be published at the declared version")
    rule = fixture["rule"]
    conditions = rule.get("conditions", {}).get("rules", [])
    uses_industry_rank = any("_industry_rank" in item.get("field", "") for item in conditions)
    fixture_checks = {
        "at_least_5000_listed_stocks": duck.read_query(
            "SELECT COUNT(*) AS count FROM stock_meta WHERE is_listed IS TRUE"
        )[0]["count"] >= 5000,
        "exactly_20_conditions": len(conditions) == 20,
        "uses_composite_indicator": any(item.get("field") == composite["name"] for item in conditions),
        "uses_current_industry_rank": uses_industry_rank,
    }
    if not all(fixture_checks.values()):
        raise ValueError(f"fixture does not meet PRD 19.1 shape: {fixture_checks}")

    engine = ScreeningEngine(duck=duck, sqlite=sqlite)
    locked_indicators = {
        rows[0]["name"]: {"version": rows[0]["version"], "content_hash": rows[0]["content_hash"]}
    }
    warmup = engine.run(rule, locked_indicators=locked_indicators)
    runs = [engine.run(rule, locked_indicators=locked_indicators) for _ in range(10)]
    durations = [run["execution_time_ms"] for run in runs]
    passing_runs = sum(duration <= 5000 for duration in durations)
    complete_results_returned = warmup["total"] > 0 and all(run["total"] > 0 for run in runs)

    host_spec: dict[str, Any] | None = None
    host_spec_hash: str | None = None
    if host_spec_path is not None:
        raw_host_spec = host_spec_path.read_bytes()
        host_spec = yaml.safe_load(raw_host_spec)
        if not isinstance(host_spec, dict) or not isinstance(host_spec.get("host"), dict):
            raise ValueError("host specification must contain a host object")
        host_spec_hash = hashlib.sha256(raw_host_spec).hexdigest()

    performance_goal_met = passing_runs >= 9
    return {
        "prd_section": "19.1",
        "fixture": str(fixture_path.resolve()),
        "database_root": str(root),
        "fixture_checks": fixture_checks,
        "warmup": {
            "execution_time_ms": warmup["execution_time_ms"],
            "result_count": warmup["total"],
        },
        "runs": [
            {"execution_time_ms": run["execution_time_ms"], "result_count": run["total"]}
            for run in runs
        ],
        "metrics": {
            "threshold_ms": 5000,
            "runs": 10,
            "passing_runs": passing_runs,
            "complete_results_returned": complete_results_returned,
            "performance_goal_met": performance_goal_met and complete_results_returned,
            "min_ms": min(durations),
            "max_ms": max(durations),
            "avg_ms": round(sum(durations) / len(durations), 1),
        },
        "host_spec": host_spec,
        "host_spec_sha256": host_spec_hash,
        "prd_acceptance": (
            "PASS" if performance_goal_met and complete_results_returned and host_spec is not None else "NOT_ATTESTED"
        ),
        "residual_requirement": (
            None if performance_goal_met and complete_results_returned and host_spec is not None else
            "Run this command against the target host's hot-data fixture with --host-spec and retain the output before claiming PRD 19.1 acceptance."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--host-spec", type=Path)
    parser.add_argument("--output", type=Path, help="write the JSON evidence atomically to this path")
    args = parser.parse_args()
    result = run_acceptance(args.fixture, args.host_spec)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.output)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
