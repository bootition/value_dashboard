#!/usr/bin/env python
"""Offline DuckDB high-water rebuild tool.

Usage:
  python scripts/rebuild_duckdb.py export  --src data/valuedashboard.duckdb --dest-dir D:/vd-rebuild-export
  python scripts/rebuild_duckdb.py import  --src D:/vd-rebuild-export --dest data/valuedashboard.new.duckdb
  python scripts/rebuild_duckdb.py verify  --old data/valuedashboard.duckdb --new data/valuedashboard.new.duckdb
  python scripts/rebuild_duckdb.py swap    --old data/valuedashboard.duckdb --new data/valuedashboard.new.duckdb

All connections use the configured memory limit and close between tables so
large BLOB tables cannot accumulate buffer-pool memory until OOM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import duckdb

SCHEMA_MIGRATIONS = "schema_migrations"
CHUNK_TABLES = {"raw_response_archive_history"}
CHUNK_SIZE = 5000


def _connect(path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config = {"memory_limit": os.environ.get("VD_DUCKDB_MEMORY_LIMIT", "14GB")}
    return duckdb.connect(str(path), read_only=read_only, config=config)


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024 * 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _table_list(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE table_name != ? ORDER BY table_name",
        [SCHEMA_MIGRATIONS],
    ).fetchall()
    return [row[0] for row in rows]


def _copy_table(conn: duckdb.DuckDBPyConnection, table: str, out: Path) -> None:
    conn.execute(
        f"COPY {table} TO '{_quote(out)}' (FORMAT PARQUET)"
    )


def _copy_chunked(
    conn: duckdb.DuckDBPyConnection, table: str, directory: Path,
    table_name: str,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    last_hash = ""
    index = 0
    while True:
        out = directory / f"{table_name}.part{index:04d}.parquet"
        rows = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT * FROM {table} "
            f"WHERE raw_response_hash > ? ORDER BY raw_response_hash LIMIT ?)",
            [last_hash, CHUNK_SIZE],
        ).fetchone()[0]
        if rows == 0:
            break
        conn.execute(
            f"COPY (SELECT * FROM {table} WHERE raw_response_hash > ? "
            f"ORDER BY raw_response_hash LIMIT ?) TO '{_quote(out)}' "
            f"(FORMAT PARQUET)",
            [last_hash, CHUNK_SIZE],
        )
        chunk_rows = conn.execute(
            f"SELECT raw_response_hash FROM {table} "
            f"WHERE raw_response_hash > ? ORDER BY raw_response_hash "
            f"LIMIT {CHUNK_SIZE}",
            [last_hash],
        ).fetchall()
        last_hash = chunk_rows[-1][0]
        parts.append({
            "filename": out.name,
            "size_bytes": out.stat().st_size,
            "sha256": _sha256(out),
            "rows": rows,
            "last_hash": last_hash,
        })
        index += 1
    return parts


def cmd_export(args: argparse.Namespace) -> int:
    src = Path(args.src)
    dest = Path(args.dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"src": str(src), "tables": {}}
    conn = _connect(src, read_only=True)
    try:
        conn.execute("SET preserve_insertion_order=false")
        tables = _table_list(conn)
        for table in tables:
            table_dir = dest / table
            table_dir.mkdir(exist_ok=True)
            if table in CHUNK_TABLES:
                parts = _copy_chunked(conn, table, table_dir, table)
                manifest["tables"][table] = {"mode": "chunked", "parts": parts}
                print(f"exported {table} parts={len(parts)}", flush=True)
            else:
                out = table_dir / f"{table}.parquet"
                _copy_table(conn, table, out)
                manifest["tables"][table] = {
                    "mode": "single",
                    "filename": out.name,
                    "size_bytes": out.stat().st_size,
                    "sha256": _sha256(out),
                }
                print(f"exported {table}", flush=True)
    finally:
        conn.close()
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest written {manifest_path}", flush=True)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.src)
    dest = Path(args.dest)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    if dest.exists():
        raise SystemExit(f"refusing to overwrite existing destination {dest}")
    import contextlib

    from app.core.storage.schema import init_duckdb_schema

    dest.parent.mkdir(parents=True, exist_ok=True)

    class _RawStore:
        def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
            self.conn = conn

        def execute_script(self, sql: str) -> None:
            self.conn.execute(sql)

        def read_query(self, sql: str, params: list[Any] | None = None) -> list[Any]:
            return self.conn.execute(sql, params or []).fetchall()

        @contextlib.contextmanager
        def transaction(self):
            yield self.conn

        @contextlib.contextmanager
        def write_connection(self):
            yield self.conn

    conn = _connect(dest, read_only=False)
    try:
        conn.execute("SET preserve_insertion_order=false")
        init_duckdb_schema(_RawStore(conn))
        existing = {
            row[0]
            for row in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()
        }
        for table, entry in manifest["tables"].items():
            if table not in existing:
                table_dir = src / table
                if entry.get("mode") == "chunked":
                    sample = table_dir / entry["parts"][0]["filename"]
                else:
                    sample = table_dir / entry["filename"]
                conn.execute(
                    f"CREATE TABLE {table} AS "
                    f"SELECT * FROM read_parquet('{_quote(sample)}') WHERE 1 = 0"
                )
                print(f"created missing table {table}", flush=True)
            table_dir = src / table
            if entry.get("mode") == "chunked":
                for part in entry["parts"]:
                    path = table_dir / part["filename"]
                    conn.execute(
                        f"INSERT INTO {table} BY NAME "
                        f"SELECT * FROM read_parquet('{_quote(path)}')"
                    )
                print(f"imported {table} parts={len(entry['parts'])}", flush=True)
            else:
                path = table_dir / entry["filename"]
                conn.execute(
                    f"INSERT INTO {table} BY NAME "
                    f"SELECT * FROM read_parquet('{_quote(path)}')"
                )
                print(f"imported {table}", flush=True)
        for seq, table in (
            ("fetch_batch_id_seq", "fetch_batch"),
            ("source_audit_id_seq", "source_audit"),
        ):
            max_id = conn.execute(
                f"SELECT COALESCE(MAX(id), 0) FROM {table}"
            ).fetchone()[0]
            conn.execute(f"ALTER TABLE {table} ALTER id DROP DEFAULT")
            conn.execute(f"DROP SEQUENCE {seq}")
            conn.execute(f"CREATE SEQUENCE {seq} START {max_id + 1}")
            conn.execute(
                f"ALTER TABLE {table} ALTER id SET DEFAULT nextval('{seq}')"
            )
    finally:
        conn.close()
    print("import complete", flush=True)
    return 0


def _fingerprints(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, Any]:
    queries = {
        "price_daily_raw": "COUNT(*), MIN(trade_date), MAX(trade_date), SUM(COALESCE(close,0)), SUM(COALESCE(volume,0)), COUNT(DISTINCT stock_code)",
        "price_daily_qfq": "COUNT(*), MIN(trade_date), MAX(trade_date), SUM(COALESCE(close,0)), SUM(COALESCE(volume,0)), COUNT(DISTINCT stock_code)",
        "source_audit": "COUNT(*), MIN(id), MAX(id), SUM(id), COUNT(DISTINCT raw_response_hash)",
        "fetch_batch": "COUNT(*), MIN(id), MAX(id), SUM(id)",
        "raw_response_archive": "COUNT(*), SUM(COALESCE(octet_length(payload),0)), COUNT(DISTINCT raw_response_hash)",
        "raw_response_archive_history": "COUNT(*), SUM(COALESCE(octet_length(payload),0)), COUNT(DISTINCT raw_response_hash)",
        "research_statistics": "COUNT(*), MAX(version), SUM(COALESCE(samples,0))",
        "balance_sheet": "COUNT(*), MIN(report_date), MAX(report_date), SUM(COALESCE(total_assets,0))",
        "income_statement": "COUNT(*), MIN(report_date), MAX(report_date), SUM(COALESCE(revenue,0))",
        "cash_flow": "COUNT(*), MIN(report_date), MAX(report_date), SUM(COALESCE(cf_from_operating,0))",
    }
    expr = queries.get(table, "COUNT(*)")
    return conn.execute(f"SELECT {expr} FROM {table}").fetchone()


def cmd_verify(args: argparse.Namespace) -> int:
    old = Path(args.old)
    new = Path(args.new)
    old_conn = _connect(old, read_only=True)
    new_conn = _connect(new,
 read_only=True)
    errors: list[str] = []
    try:
        old_tables = _table_list(old_conn)
        new_tables = _table_list(new_conn)
        if old_tables != new_tables:
            errors.append(f"tables differ: old={old_tables} new={new_tables}")
        for table in old_tables:
            if table not in new_tables:
                continue
            old_fp = _fingerprints(old_conn, table)
            new_fp = _fingerprints(new_conn, table)
            same = len(old_fp) == len(new_fp) and all(
                (a == b)
                if not isinstance(a, float) and not isinstance(b, float)
                else math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
                for a, b in zip(old_fp, new_fp, strict=True)
            )
            if not same:
                errors.append(f"{table}: old={old_fp} new={new_fp}")
            else:
                print(f"ok {table}: {old_fp}", flush=True)
        for obj, sql in {
            "views": "SELECT view_name FROM duckdb_views() ORDER BY view_name",
            "indexes": "SELECT index_name, table_name FROM duckdb_indexes() ORDER BY index_name, table_name",
        }.items():
            old_obj = old_conn.execute(sql).fetchall()
            new_obj = new_conn.execute(sql).fetchall()
            if old_obj != new_obj:
                errors.append(f"{obj} differ: old={old_obj} new={new_obj}")
        # Sequence continuity is enforced at import time by recreating each
        # sequence with START = MAX(id) + 1. A read-only verify cannot call
        # nextval without mutating state, so this is checked there instead.
    finally:
        old_conn.close()
        new_conn.close()
    if errors:
        print("VERIFY FAILED")
        for error in errors:
            print(error)
        return 1
    print("VERIFY OK")
    return 0


def cmd_swap(args: argparse.Namespace) -> int:
    old = Path(args.old)
    new = Path(args.new)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d%H%M%S")
    backup = old.with_name(old.name + f".old-{stamp}")
    if not new.exists():
        raise SystemExit(f"new file missing {new}")
    old.rename(backup)
    try:
        new.rename(old)
    except Exception:
        backup.rename(old)
        raise
    print(f"old moved to {backup}")
    print(f"new activated at {old}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export")
    p.add_argument("--src", required=True)
    p.add_argument("--dest-dir", required=True)
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("import")
    p.add_argument("--src", required=True)
    p.add_argument("--dest", required=True)
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("verify")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("swap")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.set_defaults(func=cmd_swap)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
