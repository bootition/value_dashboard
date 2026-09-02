"""Checksum-verified cold archives for the CLI's public data tables."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv
from app.core.storage.sqlite_store import SQLiteStore

ARCHIVE_TABLES = (
    "price_daily_raw",
    "price_daily_qfq",
    "balance_sheet",
    "income_statement",
    "cash_flow",
    "dividends",
    "xdxr",
    "indicator_snapshot",
    "fetch_batch",
    "source_audit",
    "raw_response_archive",
    "raw_response_archive_history",
)
_MANIFEST_NAME = "manifest.json"
_VERIFIED_NAME = "verified.json"


class DataArchiveManager:
    """Keep archive creation and destructive cleanup inside one safe root."""

    def __init__(
        self,
        duck: DuckDBStore,
        paths: DatabasePathSet,
        sqlite: SQLiteStore | None = None,
    ) -> None:
        self._duck = duck
        self._paths = paths.validate()
        self._sqlite = sqlite or SQLiteStore(paths=self._paths)
        if self._sqlite.db_path != self._paths.sqlite_path:
            raise PathIsolationError("archive stores must share one database path set")
        archive_config = Config.current().get_value("archive", {})
        configured_root = archive_config.get("root") if isinstance(archive_config, dict) else None
        if not isinstance(configured_root, str) or not configured_root:
            raise PathIsolationError("archive.root must be configured")

        self._data_root = (
            self._paths.run_root
            if self._paths.env is VdEnv.FORMAL
            else self._paths.run_root / "data"
        ).resolve()
        candidate = (self._data_root / configured_root).resolve()
        try:
            candidate.relative_to(self._data_root)
        except ValueError as error:
            raise PathIsolationError("archive.root must remain under the profile data root") from error
        self._archive_root = candidate

    @property
    def archive_root(self) -> Path:
        return self._archive_root

    def resolve_target(self, target_dir: str | None) -> Path:
        """Accept only the configured root, including its legacy ``data/`` spelling."""
        if target_dir is None:
            return self._archive_root
        requested = Path(target_dir)
        if requested.is_absolute():
            raise PathIsolationError("archive target must be the configured relative root")
        if requested.parts[:1] == ("data",):
            requested = Path(*requested.parts[1:])
        candidate = (self._data_root / requested).resolve()
        if candidate != self._archive_root:
            raise PathIsolationError("archive target must match archive.root")
        return candidate

    def create(self, target_dir: str | None = None) -> dict[str, Any]:
        root = self.resolve_target(target_dir)
        root.mkdir(parents=True, exist_ok=True)
        # Any export attempt invalidates a previous approval, even if it fails.
        (root / _VERIFIED_NAME).unlink(missing_ok=True)

        temporary_files: dict[str, Path] = {}
        try:
            # Keep DuckDB stable for the complete table export, then record the
            # SQLite PDF manifest in the same archive generation.
            with self._duck.write_connection() as connection:
                for table in ARCHIVE_TABLES:
                    temporary = root / f".{table}.{uuid.uuid4().hex}.tmp"
                    target = str(temporary).replace("'", "''")
                    connection.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
                    temporary_files[table] = temporary
                pdf_manifest = _pdf_archive_state(self._sqlite)["entries"]
        except Exception as error:
            for temporary in temporary_files.values():
                temporary.unlink(missing_ok=True)
            return {"status": "error", "error": f"archive export failed: {error}"}

        files: dict[str, dict[str, Any]] = {}
        try:
            for table, temporary in temporary_files.items():
                destination = root / f"{table}.parquet"
                temporary.replace(destination)
                files[table] = {
                    "filename": destination.name,
                    "size_bytes": destination.stat().st_size,
                    "sha256": _checksum(destination),
                    # The Parquet bytes are a point-in-time source fingerprint.
                    # cleanup re-exports and compares it before deleting hot data.
                    "source_sha256": _checksum(destination),
                }
            pdf_manifest_path = root / "pdf_archive_manifest.json"
            _write_json(pdf_manifest_path, {"entries": pdf_manifest})
            pdf_files = _copy_and_manifest_cold_pdfs(root / "pdf_archive", pdf_manifest)
            manifest = {
                "format_version": 2,
                "created_at": datetime.now(UTC).isoformat(),
                "tables": files,
                "pdf_archive_manifest": _file_entry(pdf_manifest_path, root),
                "cold_pdfs": pdf_files,
            }
            _write_json(root / _MANIFEST_NAME, manifest)
        except Exception as error:
            for temporary in temporary_files.values():
                temporary.unlink(missing_ok=True)
            return {"status": "error", "error": f"archive manifest failed: {error}"}

        return {
            "status": "ok", "target": str(root),
            "exported": [*ARCHIVE_TABLES, "pdf_archive_manifest", "cold_pdfs"],
        }

    def verify(self, target_dir: str | None = None) -> dict[str, Any]:
        root = self.resolve_target(target_dir)
        valid, error, manifest_checksum = self._validate_manifest(root)
        if not valid:
            return {"status": "error", "error": error}
        _write_json(root / _VERIFIED_NAME, {
            "format_version": 1,
            "manifest_sha256": manifest_checksum,
            "verified_at": datetime.now(UTC).isoformat(),
        })
        return {"status": "ok", "target": str(root), "count": len(ARCHIVE_TABLES) + 2}

    def is_verified_for_cleanup(self, target_dir: str | None = None) -> tuple[bool, str | None]:
        """Verify and delete under one DuckDB write lock to close the TOCTOU window."""
        root = self.resolve_target(target_dir)
        with self._duck.write_connection() as connection:
            return self._is_verified_for_cleanup_locked(root, connection)

    def _is_verified_for_cleanup_locked(self, root: Path, connection: Any) -> tuple[bool, str | None]:
        valid, error, manifest_checksum = self._validate_manifest(root)
        if not valid:
            return False, error
        try:
            verified = _read_json(root / _VERIFIED_NAME)
        except (OSError, ValueError, json.JSONDecodeError):
            return False, "archive has no valid verification record"
        if verified.get("manifest_sha256") != manifest_checksum:
            return False, "archive verification record does not match its manifest"
        manifest = _read_json(root / _MANIFEST_NAME)
        for table, entry in manifest["tables"].items():
            temporary = root / f".{table}.{uuid.uuid4().hex}.cleanup-check.tmp"
            try:
                target = str(temporary).replace("'", "''")
                connection.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
                if _checksum(temporary) != entry.get("source_sha256"):
                    return False, f"hot data changed after archive verification: {table}"
            except Exception as error:
                return False, f"unable to validate current hot data: {table}: {error}"
            finally:
                temporary.unlink(missing_ok=True)
        if _pdf_archive_state(self._sqlite) != _read_json(root / "pdf_archive_manifest.json"):
            return False, "PDF archive manifest changed after archive verification"
        valid_pdfs, pdf_error = _validate_cold_pdfs(root, manifest.get("cold_pdfs"))
        if not valid_pdfs:
            return False, pdf_error
        return True, None

    def delete_verified_hot_data(self, target_dir: str | None = None) -> tuple[bool, str | None]:
        """Revalidate and delete one immutable hot-data generation atomically."""
        root = self.resolve_target(target_dir)
        with self._duck.transaction() as connection:
            verified, error = self._is_verified_for_cleanup_locked(root, connection)
            if not verified:
                return False, error
            for table in ARCHIVE_TABLES:
                connection.execute(f"DELETE FROM {table}")
        return True, None

    def restore_from_archive(self, target_dir: str | None = None) -> dict[str, Any]:
        """从已验证的冷归档恢复热表（reports/102 §6 P2 配套命令）。"""
        root = self.resolve_target(target_dir)
        valid, error, manifest_checksum = self._validate_manifest(root)
        if not valid:
            return {"status": "error", "error": error or "archive manifest invalid"}
        try:
            verified = _read_json(root / _VERIFIED_NAME)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "error", "error": "archive has no valid verification record"}
        if verified.get("manifest_sha256") != manifest_checksum:
            return {"status": "error", "error": "archive verification record does not match its manifest"}
        manifest = _read_json(root / _MANIFEST_NAME)
        restored: list[str] = []
        row_counts: dict[str, int] = {}
        try:
            with self._duck.transaction() as connection:
                for table in ARCHIVE_TABLES:
                    entry = manifest["tables"][table]
                    path = root / entry["filename"]
                    connection.execute(f"DELETE FROM {table}")
                    quoted = str(path).replace("'", "''")
                    connection.execute(
                        f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet('{quoted}')"
                    )
                    row = connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                    restored.append(table)
                    row_counts[table] = int(row[0]) if row else 0
            pdf_manifest_path = root / "pdf_archive_manifest.json"
            if pdf_manifest_path.is_file():
                pdf_state = _read_json(pdf_manifest_path).get("entries", [])
                with self._sqlite.transaction() as conn:
                    conn.execute("DELETE FROM pdf_archive_manifest")
                    conn.executemany(
                        "INSERT INTO pdf_archive_manifest "
                        "(stock_code, filename, archive_path, checksum, archived_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [
                            (
                                row["stock_code"], row["filename"], row["archive_path"],
                                row["checksum"], row.get("archived_at"),
                            )
                            for row in pdf_state
                        ],
                    )
        except Exception as error:
            return {"status": "error", "error": f"restore failed: {error}"}
        return {
            "status": "ok",
            "archive": str(root),
            "restored": restored,
            "row_counts": row_counts,
        }

    def _validate_manifest(self, root: Path) -> tuple[bool, str | None, str | None]:
        try:
            manifest_path = root / _MANIFEST_NAME
            manifest = _read_json(manifest_path)
            tables = manifest.get("tables")
            if manifest.get("format_version") != 2 or not isinstance(tables, dict):
                return False, "archive manifest is invalid", None
            if set(tables) != set(ARCHIVE_TABLES):
                return False, "archive manifest does not cover all required tables", None
            for table in ARCHIVE_TABLES:
                entry = tables[table]
                path = root / f"{table}.parquet"
                if not isinstance(entry, dict) or entry.get("filename") != path.name:
                    return False, f"archive manifest entry is invalid: {table}", None
                if entry.get("source_sha256") != entry.get("sha256"):
                    return False, f"archive source fingerprint is invalid: {table}", None
                if not path.is_file() or path.stat().st_size != entry.get("size_bytes"):
                    return False, f"archive file is missing or changed: {table}", None
                if _checksum(path) != entry.get("sha256"):
                    return False, f"archive checksum mismatch: {table}", None
            pdf_entry = manifest.get("pdf_archive_manifest")
            pdf_manifest_path = root / "pdf_archive_manifest.json"
            if not _matches_file_entry(pdf_manifest_path, pdf_entry):
                return False, "PDF archive manifest file is missing or changed", None
            if _pdf_archive_state(self._sqlite) != _read_json(pdf_manifest_path):
                return False, "PDF archive manifest no longer matches SQLite", None
            valid_pdfs, pdf_error = _validate_cold_pdfs(root, manifest.get("cold_pdfs"))
            if not valid_pdfs:
                return False, pdf_error, None
            return True, None, _checksum(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return False, f"archive manifest is unreadable: {error}", None


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    return {
        "filename": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _checksum(path),
    }


def _matches_file_entry(path: Path, entry: Any) -> bool:
    return bool(
        isinstance(entry, dict)
        and entry.get("filename") == path.name
        and path.is_file()
        and path.stat().st_size == entry.get("size_bytes")
        and _checksum(path) == entry.get("sha256")
    )


def _pdf_archive_state(sqlite: SQLiteStore) -> dict[str, list[dict[str, Any]]]:
    try:
        entries = sqlite.query(
            "SELECT stock_code, filename, archive_path, checksum, archived_at "
            "FROM pdf_archive_manifest ORDER BY stock_code, filename"
        )
    except Exception as error:
        # A pre-v6 profile has no cold-PDF feature to archive. Do not weaken
        # verification for a current schema: any other SQLite failure propagates.
        if "no such table: pdf_archive_manifest" not in str(error):
            raise
        entries = []
    return {"entries": entries}


def _copy_and_manifest_cold_pdfs(target_dir: Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        source = Path(entry["archive_path"])
        if not source.is_file() or _checksum(source) != entry["checksum"]:
            raise ValueError(f"cold PDF is missing or invalid: {entry['stock_code']}/{entry['filename']}")
        destination = target_dir / f"{index:06d}-{entry['stock_code']}-{entry['filename']}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        copied.append({
            "stock_code": entry["stock_code"],
            "filename": entry["filename"],
            "archive_path": entry["archive_path"],
            "filename_in_archive": destination.relative_to(target_dir.parent).as_posix(),
            "size_bytes": destination.stat().st_size,
            "sha256": _checksum(destination),
        })
    return copied


def _validate_cold_pdfs(root: Path, entries: Any) -> tuple[bool, str | None]:
    if not isinstance(entries, list):
        return False, "cold PDF archive manifest is invalid"
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename_in_archive"), str):
            return False, "cold PDF archive entry is invalid"
        archived_copy = (root / entry["filename_in_archive"]).resolve()
        if not archived_copy.is_relative_to(root.resolve()):
            return False, "cold PDF archive entry escapes root"
        source = Path(entry.get("archive_path", ""))
        if (
            not archived_copy.is_file()
            or archived_copy.stat().st_size != entry.get("size_bytes")
            or _checksum(archived_copy) != entry.get("sha256")
        ):
            return False, f"cold PDF archive copy is missing or changed: {entry.get('filename', '')}"
        if not source.is_file() or _checksum(source) != entry.get("sha256"):
            return False, f"live cold PDF changed after archive verification: {entry.get('filename', '')}"
    return True, None


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)
