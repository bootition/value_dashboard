"""Revert approximated SSE circ_shares back to NULL. Keep paid_in_capital total_shares."""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import require_formal_maintenance_paths
from app.core.storage.schema import init_duckdb_schema

paths = require_formal_maintenance_paths()
duck = DuckDBStore(paths=paths)
init_duckdb_schema(duck)

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
