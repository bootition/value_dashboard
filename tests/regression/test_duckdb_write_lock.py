from __future__ import annotations

from app.core.storage.duckdb_store import DuckDBStore


def test_old_write_lock_owner_cannot_remove_a_replacement_lock(duckdb_store: DuckDBStore) -> None:
    replacement = "pid=123\ntime=0\ntoken=replacement-owner\n"

    with duckdb_store._write_lock():
        duckdb_store._lock_path.write_text(replacement, encoding="utf-8")

    assert duckdb_store._lock_path.read_text(encoding="utf-8") == replacement
    duckdb_store._lock_path.unlink()
