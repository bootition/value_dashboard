"""Populate CSRC industry classification on the formal database (maintenance).

O7 Phase F / O1 解锁：正式库 csrc_l1/csrc_l2 当前全为 NULL（重建时 --skip-csrc），
本脚本在正式 profile 下只执行 init 的 CSRC 步骤（幂等、可断点续传：
只补抓 csrc_l1 IS NULL 的上市股票，每块独立事务并记录进度），
并把证据写入 docs/evidence/ 治理目录。

Usage:
  python scripts/populate_csrc_industry.py [--evidence docs/evidence/evidence-csrc-populate-<date>.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.sqlite_store import SQLiteStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path("docs/evidence/evidence-csrc-populate-20260803.json"),
    )
    args = parser.parse_args()

    paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)

    initializer = DataInitializer(duck=duck, sqlite=sqlite)
    report = initializer._fetch_csrc_industry()  # noqa: SLF001 - 官方维护路径复用 init 步骤

    evidence = {
        "command": "populate_csrc_industry",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "report": report,
        "after": {
            "csrc_l1_not_null": duck.read_query(
                "SELECT COUNT(*) AS c FROM stock_meta WHERE csrc_l1 IS NOT NULL"
            )[0]["c"],
            "csrc_l2_not_null": duck.read_query(
                "SELECT COUNT(*) AS c FROM stock_meta WHERE csrc_l2 IS NOT NULL"
            )[0]["c"],
            "listed_total": duck.read_query(
                "SELECT COUNT(*) AS c FROM stock_meta WHERE is_listed IS TRUE"
            )[0]["c"],
        },
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.evidence.with_suffix(args.evidence.suffix + ".tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.evidence)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())