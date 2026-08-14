"""Revert approximated SSE circ_shares back to NULL. Keep paid_in_capital total_shares.

2026-08-14 红队 P2：破坏性脚本显式确认（--yes）+ 写前自动备份 stock_meta。
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from _maintenance_safety import backup_tables, confirm_destructive
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import require_formal_maintenance_paths
from app.core.storage.schema import init_duckdb_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="回滚上证近似 circ_shares 为 NULL")
    parser.add_argument("--yes", action="store_true", help="确认修改正式数据库")
    args = parser.parse_args()

    paths = require_formal_maintenance_paths()
    if not confirm_destructive(args.yes):
        return 2

    duck = DuckDBStore(paths=paths)
    init_duckdb_schema(duck)
    backup_tables(duck, ["stock_meta"], tag="revert-sse-circ")

    # Only revert circ_shares for SSE stocks, keep total_shares from paid_in_capital
    with duck.transaction() as conn:
        conn.execute(
            """UPDATE stock_meta SET circ_shares = NULL
               WHERE exchange = 'SSE'
                 AND is_listed IS TRUE
                 AND circ_shares IS NOT NULL
                 AND circ_shares > 0"""
        )

    r = duck.read_query(
        """SELECT exchange, COUNT(*) listed,
           COUNT(*) FILTER (WHERE total_shares > 0) t,
           COUNT(*) FILTER (WHERE circ_shares > 0) c
           FROM stock_meta WHERE is_listed IS TRUE GROUP BY exchange"""
    )
    for row in r:
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
