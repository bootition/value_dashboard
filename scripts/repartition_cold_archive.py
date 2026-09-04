#!/usr/bin/env python
"""Repartition external cold Parquet by year + bounded part size (2026-09-04).

Reads the rollback DuckDB (still containing BLOBs/archive rows), emits:
  raw_response_archive_history/year=YYYY/partNNNN.parquet  (50k rows/part)
  source_audit_archive/year=YYYY/partNNNN.parquet           (500k rows/part)
This replaces the previous hash-ordered global parts with lifecycle/year
partitions so old years can be deleted independently without rewriting peers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import duckdb

HISTORY_PART_ROWS = 5_000
ARCHIVE_PART_ROWS = 500_000


def _connect(path: Path):
    return duckdb.connect(str(path), read_only=True, config={"memory_limit": os.environ.get("VD_DUCKDB_MEMORY_LIMIT", "14GB")})


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def _sha256(path: Path) -> str:
    d = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024 * 16), b""):
            d.update(block)
    return d.hexdigest()


def _export_history(conn, root: Path):
    rows = conn.execute(
        "SELECT DISTINCT EXTRACT(YEAR FROM COALESCE(fetch_time, created_at))::INTEGER AS y "
        "FROM raw_response_archive_history ORDER BY y"
    ).fetchall()
    years = [int(r[0]) for r in rows if r[0] is not None]
    parts = []
    for year in years:
        directory = root / "raw_response_archive_history" / f"year={year}"
        directory.mkdir(parents=True, exist_ok=True)
        last_hash, index = "", 0
        while True:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_response_archive_history "
                "WHERE EXTRACT(YEAR FROM COALESCE(fetch_time, created_at)) = ? "
                "AND raw_response_hash > ?",
                [year, last_hash],
            ).fetchone()[0]
            if cnt == 0:
                break
            out = directory / f"part{index:04d}.parquet"
            conn.execute(
                "COPY (SELECT * FROM raw_response_archive_history "
                "WHERE EXTRACT(YEAR FROM COALESCE(fetch_time, created_at)) = ? "
                "AND raw_response_hash > ? ORDER BY raw_response_hash LIMIT ?) "
                f"TO '{_quote(out)}' (FORMAT PARQUET)",
                [year, last_hash, HISTORY_PART_ROWS],
            )
            chunk_rows = conn.execute(
                "SELECT raw_response_hash FROM raw_response_archive_history "
                "WHERE EXTRACT(YEAR FROM COALESCE(fetch_time, created_at)) = ? "
                "AND raw_response_hash > ? ORDER BY raw_response_hash LIMIT ?",
                [year, last_hash, HISTORY_PART_ROWS],
            ).fetchall()
            last_hash = chunk_rows[-1][0]
            parts.append({"year": year, "filename": str(out.relative_to(root)), "rows": len(chunk_rows), "sha256": _sha256(out)})
            index += 1
            print("history", year, "part", index - 1, "rows", cnt, flush=True)
    return parts


def _export_archive(conn, root: Path):
    rows = conn.execute(
        "SELECT DISTINCT EXTRACT(YEAR FROM report_date)::INTEGER AS y "
        "FROM source_audit_archive WHERE report_date IS NOT NULL ORDER BY y"
    ).fetchall()
    years = [int(r[0]) for r in rows if r[0] is not None]
    parts = []
    for year in years:
        directory = root / "source_audit_archive" / f"year={year}"
        directory.mkdir(parents=True, exist_ok=True)
        last_id, index = 0, 0
        while True:
            ids = conn.execute(
                "SELECT id FROM source_audit_archive "
                "WHERE EXTRACT(YEAR FROM report_date) = ? AND id > ? "
                "ORDER BY id LIMIT ?",
                [year, last_id, ARCHIVE_PART_ROWS],
            ).fetchall()
            if not ids:
                break
            start_id = last_id
            last_id = ids[-1][0]
            out = directory / f"part{index:04d}.parquet"
            conn.execute(
                "COPY (SELECT * FROM source_audit_archive "
                "WHERE EXTRACT(YEAR FROM report_date) = ? AND id > ? AND id <= ? "
                "ORDER BY id) "
                f"TO '{_quote(out)}' (FORMAT PARQUET)",
                [year, start_id, last_id],
            )
            parts.append({"year": year, "filename": str(out.relative_to(root)), "rows": len(ids), "sha256": _sha256(out)})
            index += 1
            print("archive", year, "part", index - 1, "rows", len(ids), flush=True)
    return parts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dest-dir", required=True)
    args = ap.parse_args()
    src = Path(args.src)
    root = Path(args.dest_dir)
    root.mkdir(parents=True, exist_ok=True)
    conn = _connect(src)
    try:
        conn.execute("SET preserve_insertion_order=false")
        conn.execute("SET threads=2")
        manifest = {"src": str(src), "tables": {
            "raw_response_archive_history": {"mode": "year_chunked", "parts": _export_history(conn, root)},
            "source_audit_archive": {"mode": "year_chunked", "parts": _export_archive(conn, root)},
        }}
    finally:
        conn.close()
    out = root / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest", out, flush=True)


if __name__ == "__main__":
    main()
