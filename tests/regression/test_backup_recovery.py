from __future__ import annotations

import zipfile
import threading
from pathlib import Path

import pytest

from app.core.backup.manager import BackupManager
from app.core.storage.maintenance import MaintenanceLockError, exclusive_maintenance
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore


def test_encrypted_backup_can_be_restored_with_its_recovery_key(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)

    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])
    recovery_key = created["recovery_key"]

    duckdb_store.write_query("DELETE FROM stock_meta")
    sqlite_store.execute("DELETE FROM watchlist")
    restored = manager.restore_from_backup(str(archive), recovery_key=recovery_key)

    assert restored["status"] == "ok"
    assert duckdb_store.read_query("SELECT stock_code FROM stock_meta") == [{"stock_code": "600519"}]
    assert sqlite_store.query("SELECT stock_code FROM watchlist") == [{"stock_code": "600519"}]


def test_backup_preserves_archived_and_quarantined_evidence(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    duckdb_store.write_query(
        """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
           VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
        ["a" * 64, b"source-material"],
    )
    duckdb_store.write_query(
        """INSERT INTO dividends_quarantine
           (stock_code, ex_date, quarantine_reason, quarantined_at)
           VALUES ('600519', '2024-12-31', 'test', CURRENT_TIMESTAMP)"""
    )
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))

    duckdb_store.write_query("DELETE FROM raw_response_archive")
    duckdb_store.write_query("DELETE FROM dividends_quarantine")
    restored = manager.restore_from_backup(created["path"], recovery_key=created["recovery_key"])

    assert restored["status"] == "ok"
    assert duckdb_store.read_query("SELECT payload FROM raw_response_archive") == [
        {"payload": b"source-material"}
    ]
    assert duckdb_store.read_query("SELECT quarantine_reason FROM dividends_quarantine") == [
        {"quarantine_reason": "test"}
    ]


def test_backup_supports_a_complete_pre_migration_schema(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    duckdb_store.write_query("DROP TABLE raw_response_archive")
    duckdb_store.write_query("DROP TABLE source_audit_quarantine")
    duckdb_store.write_query("DROP TABLE dividends_quarantine")
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)

    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))

    assert created["status"] == "ok"
    with zipfile.ZipFile(created["path"]) as bundle:
        names = set(bundle.namelist())
    assert "public/raw_response_archive.parquet" not in names
    assert "public/source_audit_quarantine.parquet" not in names


def test_restore_rejects_archive_with_checksum_mismatch(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])
    archive.write_bytes(archive.read_bytes() + b"tampered")

    result = manager.restore_from_backup(str(archive))

    assert result["status"] == "error"
    assert result["error"] == "backup checksum mismatch"


def test_restore_checksums_the_canonical_backup_path(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])
    archive.write_bytes(archive.read_bytes() + b"tampered")

    result = manager.restore_from_backup(str(archive.parent / "." / archive.name))

    assert result == {"status": "error", "error": "backup checksum mismatch"}


def test_restore_rejects_manifest_missing_a_public_table(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])
    altered = tmp_path / "altered.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(altered, "w") as target:
        for member in source.infolist():
            if member.filename == "public/stock_meta.parquet":
                continue
            target.writestr(member, source.read(member.filename))

    result = manager.restore_from_backup(str(altered), recovery_key=created["recovery_key"])

    assert result == {
        "status": "error",
        "error": "backup manifest validation failed: backup file is missing: public/stock_meta.parquet",
    }


def test_restore_rejects_a_tampered_copied_backup(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    copied = tmp_path / "copied.zip"
    copied.write_bytes(Path(created["path"]).read_bytes())
    with zipfile.ZipFile(copied) as source, zipfile.ZipFile(tmp_path / "tampered.zip", "w") as target:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == "personal_encrypted/personalized.bin":
                content += b"tampered"
            target.writestr(member, content)

    result = manager.restore_from_backup(str(tmp_path / "tampered.zip"), recovery_key=created["recovery_key"])

    assert result["status"] == "error"
    assert "backup manifest validation failed" in result["error"]


def test_restore_maintenance_lock_blocks_other_profile_writes(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    errors: list[Exception] = []

    def attempt_other_process_write() -> None:
        try:
            sqlite_store.execute("INSERT INTO watchlist (stock_code, group_name) VALUES ('600519', 'blocked')")
        except Exception as error:
            errors.append(error)

    with exclusive_maintenance(duckdb_store.db_path):
        thread = threading.Thread(target=attempt_other_process_write)
        thread.start()
        thread.join()

    assert len(errors) == 1
    assert isinstance(errors[0], MaintenanceLockError)


def test_backup_creation_uses_a_unique_generation_per_request(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)

    first = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    second = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))

    assert first["backup_id"] != second["backup_id"]
    assert Path(first["path"]).is_file()
    assert Path(second["path"]).is_file()


def test_personalized_restore_obeys_actual_foreign_key_topology(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status) VALUES ('base_metric', 1, 'pe_ttm', 'published')"""
    )
    expression_id = sqlite_store.query("SELECT id FROM dsl_expressions")[0]["id"]
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status) VALUES ('derived_metric', 1, 'base_metric', 'published')"""
    )
    dependent_id = sqlite_store.query(
        "SELECT id FROM dsl_expressions WHERE name = 'derived_metric'"
    )[0]["id"]
    sqlite_store.execute(
        "INSERT INTO dsl_dependencies (expression_id, depends_on_id, depends_on_version) VALUES (?, ?, 1)",
        [dependent_id, expression_id],
    )
    sqlite_store.execute(
        """INSERT INTO screening_rules (name, version, rule_json, locked_indicators)
           VALUES ('value', 1, '{}', '{}')"""
    )
    rule_id = sqlite_store.query("SELECT id FROM screening_rules")[0]["id"]
    sqlite_store.execute(
        """INSERT INTO screening_results
           (title, rule_id, rule_version, data_date, result_json, columns_json)
           VALUES ('result', ?, 1, CURRENT_TIMESTAMP, '[]', '[]')""",
        [rule_id],
    )
    result_id = sqlite_store.query("SELECT id FROM screening_results")[0]["id"]
    sqlite_store.execute(
        "INSERT INTO watchlist (stock_code, source_rule_id, source_result_id) VALUES ('600519', ?, ?)",
        [rule_id, result_id],
    )
    snapshot = manager._snapshot_personalized_data()
    manager._restore_personalized_data(snapshot)

    assert sqlite_store.query("SELECT COUNT(*) AS count FROM dsl_dependencies")[0]["count"] == 1
    assert sqlite_store.query("SELECT COUNT(*) AS count FROM watchlist")[0]["count"] == 1


def test_pdf_restore_failure_compensates_both_databases(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    hot_pdf = manager._data_root / "pdf" / "600519" / "backup.pdf"
    hot_pdf.parent.mkdir(parents=True, exist_ok=True)
    hot_pdf.write_bytes(b"backup")
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])
    duckdb_store.write_query("UPDATE stock_meta SET name = 'current' WHERE stock_code = '600519'")
    sqlite_store.execute("UPDATE watchlist SET group_name = 'current'")

    original_copytree = __import__("shutil").copytree

    def fail_live_pdf_copy(source, destination, *args, **kwargs):
        if Path(destination) == manager._data_root / "pdf":
            raise OSError("injected live PDF copy failure")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr("app.core.backup.manager.shutil.copytree", fail_live_pdf_copy)
    result = manager.restore_from_backup(str(archive), recovery_key=created["recovery_key"])

    assert result["status"] == "error"
    assert duckdb_store.read_query("SELECT name FROM stock_meta WHERE stock_code = '600519'") == [
        {"name": "current"}
    ]
    assert sqlite_store.query("SELECT group_name FROM watchlist WHERE stock_code = '600519'") == [
        {"group_name": "current"}
    ]


def test_restore_failure_rolls_back_all_public_tables(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])

    duckdb_store.write_query("UPDATE stock_meta SET name = 'current' WHERE stock_code = '600519'")
    original_transaction = duckdb_store.transaction

    def failing_transaction(*args, **kwargs):
        context = original_transaction(*args, **kwargs)

        class FailingConnection:
            def __enter__(self):
                connection = context.__enter__()

                class ConnectionProxy:
                    def execute(self, sql, *execute_args, **execute_kwargs):
                        if "COPY" in sql:
                            raise RuntimeError("injected restore copy failure")
                        return connection.execute(sql, *execute_args, **execute_kwargs)

                return ConnectionProxy()

            def __exit__(self, exc_type, exc, tb):
                return context.__exit__(exc_type, exc, tb)

        return FailingConnection()

    monkeypatch.setattr(duckdb_store, "transaction", failing_transaction)
    result = manager.restore_from_backup(str(archive))

    assert result["status"] == "error"
    assert duckdb_store.read_query("SELECT name FROM stock_meta WHERE stock_code = '600519'") == [
        {"name": "current"}
    ]


def test_restore_matches_parquet_columns_by_name(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
           (stock_code, report_date, latest_close, latest_price_date)
           VALUES ('600519', '2025-12-31', 1500, '2026-01-02')"""
    )
    parquet = tmp_path / "snapshot-reordered.parquet"
    with duckdb_store.write_connection() as connection:
        target = str(parquet).replace("'", "''")
        connection.execute(
            "COPY (SELECT * EXCLUDE (latest_price_date), latest_price_date "
            f"FROM indicator_snapshot) TO '{target}' (FORMAT PARQUET)"
        )

    duckdb_store.write_query("DELETE FROM indicator_snapshot")
    manager._restore_public_tables([("indicator_snapshot", parquet)])

    assert duckdb_store.read_query(
        "SELECT stock_code, latest_close, latest_price_date FROM indicator_snapshot"
    ) == [{
        "stock_code": "600519",
        "latest_close": 1500.0,
        "latest_price_date": __import__("datetime").date(2026, 1, 2),
    }]


def test_personal_restore_failure_compensates_the_public_restore(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    archive = Path(created["path"])

    duckdb_store.write_query("UPDATE stock_meta SET name = 'current' WHERE stock_code = '600519'")
    sqlite_store.execute("UPDATE watchlist SET group_name = 'current'")
    original_restore_personalized = manager._restore_personalized_data
    calls = 0

    def fail_once(data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected personal restore failure")
        return original_restore_personalized(data)

    monkeypatch.setattr(manager, "_restore_personalized_data", fail_once)
    result = manager.restore_from_backup(str(archive), recovery_key=created["recovery_key"])

    assert result["status"] == "error"
    assert duckdb_store.read_query("SELECT name FROM stock_meta WHERE stock_code = '600519'") == [
        {"name": "current"}
    ]
    assert sqlite_store.query("SELECT group_name FROM watchlist WHERE stock_code = '600519'") == [
        {"group_name": "current"}
    ]


def test_startup_recovery_repairs_a_restore_interrupted_by_system_exit(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_backup_data(duckdb_store, sqlite_store)
    manager = BackupManager(duck=duckdb_store, sqlite=sqlite_store, paths=database_paths)
    created = manager.create_full_backup(user_password="password", target_dir=str(tmp_path / "backup"))
    duckdb_store.write_query("UPDATE stock_meta SET name = 'current' WHERE stock_code = '600519'")
    sqlite_store.execute("UPDATE watchlist SET group_name = 'current'")
    original_restore_personalized = manager._restore_personalized_data

    def interrupt_after_public_restore(data):
        raise SystemExit("simulated process termination")

    monkeypatch.setattr(manager, "_restore_personalized_data", interrupt_after_public_restore)
    with pytest.raises(SystemExit, match="simulated process termination"):
        manager.restore_from_backup(created["path"], recovery_key=created["recovery_key"])

    assert manager._restore_journal_path.exists()
    assert duckdb_store.read_query("SELECT name FROM stock_meta WHERE stock_code = '600519'") == [
        {"name": "贵州茅台"}
    ]
    assert sqlite_store.query("SELECT group_name FROM watchlist WHERE stock_code = '600519'") == [
        {"group_name": "current"}
    ]

    monkeypatch.setattr(manager, "_restore_personalized_data", original_restore_personalized)
    manager.recover_interrupted_restore()

    assert not manager._restore_journal_path.exists()
    assert duckdb_store.read_query("SELECT name FROM stock_meta WHERE stock_code = '600519'") == [
        {"name": "current"}
    ]
    assert sqlite_store.query("SELECT group_name FROM watchlist WHERE stock_code = '600519'") == [
        {"group_name": "current"}
    ]


def _seed_backup_data(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange)
           VALUES ('600519', '贵州茅台', 'SSE')"""
    )
    sqlite.execute("INSERT INTO watchlist (stock_code, group_name) VALUES ('600519', 'default')")
