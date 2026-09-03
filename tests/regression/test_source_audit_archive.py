"""source_audit 冷热分离维护测试（2026-09-03）。"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.source_audit_archive import archive_before, read_archive_state
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _seed_audit(duck: DuckDBStore) -> None:
    rows = [
        (1, "000001", "revenue", date(2020, 12, 31), 100.0),
        (2, "000001", "revenue", date(2021, 12, 31), 120.0),
        (3, "000001", "revenue", date(2024, 12, 31), 140.0),
        (4, "000001", "revenue", date(2025, 12, 31), 150.0),
        (5, "000001", "revenue", date(2026, 6, 30), 160.0),
    ]
    for item in rows:
        duck.write_query(
            """INSERT INTO source_audit
               (id, stock_code, field_name, report_date, value, source,
                fetch_batch_id, fetch_time, raw_response_hash, confidence)
               VALUES (?, ?, ?, ?, ?, 'fixture', 'batch-1', CURRENT_TIMESTAMP, ?, 'strict')""",
            [*item, "0" * 64],
        )


def test_archive_before_moves_old_rows_and_keeps_hot(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_audit(duckdb_store)

    report = archive_before(
        duckdb_store, sqlite_store, date(2025, 1, 1),
        batch_size=10_000, max_batches=10,
    )

    assert report["status"] == "success"
    assert report["archived_rows"] == 4
    hot = duckdb_store.read_query(
        "SELECT report_date FROM source_audit ORDER BY report_date"
    )
    archive = duckdb_store.read_query(
        "SELECT report_date FROM source_audit_archive ORDER BY report_date"
    )
    assert [row["report_date"] for row in hot] == [date(2025, 12, 31), date(2026, 6, 30)]
    assert len(archive) == 4
    state = read_archive_state(sqlite_store)
    assert state is not None and state["archived_rows"] == 4


def test_archive_before_is_idempotent(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_audit(duckdb_store)
    archive_before(duckdb_store, sqlite_store, date(2025, 1, 1))
    second = archive_before(duckdb_store, sqlite_store, date(2025, 1, 1))

    assert second["archived_rows"] == 0
    assert second["status"] == "success"
    assert duckdb_store.read_query("SELECT COUNT(*) AS c FROM source_audit")[0]["c"] == 2
    assert duckdb_store.read_query("SELECT COUNT(*) AS c FROM source_audit_archive")[0]["c"] == 4


def test_archive_state_reports_partial_when_capped(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_audit(duckdb_store)

    report = archive_before(
        duckdb_store, sqlite_store, date(2025, 1, 1),
        batch_size=2, max_batches=1,
    )

    assert report["status"] == "partial"
    assert report["archived_rows"] == pytest.approx(2)
