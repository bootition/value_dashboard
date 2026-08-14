"""Auditable maintenance for legacy records that cannot support current research."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore


def legacy_quarantine_summary(duck: DuckDBStore) -> dict[str, Any]:
    """Report only records whose provenance or event date cannot be verified."""
    lineage = duck.read_query(
        """
        SELECT COUNT(*) AS count
        FROM source_audit audit
        LEFT JOIN fetch_batch batch ON audit.fetch_batch_id = batch.batch_id
        WHERE starts_with(audit.field_name, '__')
           OR LENGTH(audit.raw_response_hash) != 64
           OR batch.batch_id IS NULL
        """
    )[0]["count"]
    dividends = duck.read_query(
        """
        SELECT COUNT(*) AS count
        FROM dividends
        WHERE announcement_date IS NULL
          AND (
              (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
              OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
          )
        """
    )[0]["count"]
    empty_payloads = duck.read_query(
        """
        SELECT COUNT(*) AS count
        FROM raw_response_archive
        WHERE payload IS NULL OR OCTET_LENGTH(payload) = 0
        """
    )[0]["count"]
    return {
        "legacy_lineage_records": lineage,
        "unverified_dividend_records": dividends,
        "empty_payload_archives": empty_payloads,
        "action": "quarantine without deletion; retained records are excluded from research tables",
    }


def quarantine_legacy_records(duck: DuckDBStore) -> dict[str, int]:
    """Move unsupported legacy records out of active research tables atomically."""
    timestamp = datetime.now(UTC)
    with duck.transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_audit_quarantine AS
            SELECT audit.*, CAST(NULL AS VARCHAR) AS quarantine_reason,
                   CAST(NULL AS TIMESTAMP) AS quarantined_at
            FROM source_audit audit WHERE FALSE
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dividends_quarantine AS
            SELECT dividend.*, CAST(NULL AS VARCHAR) AS quarantine_reason,
                   CAST(NULL AS TIMESTAMP) AS quarantined_at
            FROM dividends dividend WHERE FALSE
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_response_archive_quarantine AS
            SELECT archive.*, CAST(NULL AS VARCHAR) AS quarantine_reason,
                   CAST(NULL AS TIMESTAMP) AS quarantined_at
            FROM raw_response_archive archive WHERE FALSE
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_batch_quarantine AS
            SELECT batch.*, CAST(NULL AS VARCHAR) AS quarantine_reason,
                   CAST(NULL AS TIMESTAMP) AS quarantined_at
            FROM fetch_batch batch WHERE FALSE
            """
        )

        lineage_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM source_audit audit
            LEFT JOIN fetch_batch batch ON audit.fetch_batch_id = batch.batch_id
            WHERE starts_with(audit.field_name, '__')
               OR LENGTH(audit.raw_response_hash) != 64
               OR batch.batch_id IS NULL
            """
        ).fetchone()[0]
        dividend_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM dividends
            WHERE announcement_date IS NULL
              AND (
                  (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
                  OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
              )
            """
        ).fetchone()[0]
        empty_archive_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM raw_response_archive archive
            WHERE archive.payload IS NULL OR OCTET_LENGTH(archive.payload) = 0
            """
        ).fetchone()[0]
        empty_audit_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM source_audit audit
            JOIN raw_response_archive archive ON audit.raw_response_hash = archive.raw_response_hash
            WHERE archive.payload IS NULL OR OCTET_LENGTH(archive.payload) = 0
            """
        ).fetchone()[0]
        orphan_batch_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM fetch_batch batch
            LEFT JOIN raw_response_archive archive ON batch.raw_response_hash = archive.raw_response_hash
            WHERE archive.raw_response_hash IS NULL
            """
        ).fetchone()[0]

        connection.execute(
            """
            INSERT INTO source_audit_quarantine
            SELECT audit.*, 'unsupported_legacy_lineage', ?
            FROM source_audit audit
            LEFT JOIN fetch_batch batch ON audit.fetch_batch_id = batch.batch_id
            WHERE starts_with(audit.field_name, '__')
               OR LENGTH(audit.raw_response_hash) != 64
               OR batch.batch_id IS NULL
            """,
            [timestamp],
        )
        connection.execute(
            """
            DELETE FROM source_audit
            WHERE id IN (
                SELECT audit.id
                FROM source_audit audit
                LEFT JOIN fetch_batch batch ON audit.fetch_batch_id = batch.batch_id
                WHERE starts_with(audit.field_name, '__')
                   OR LENGTH(audit.raw_response_hash) != 64
                   OR batch.batch_id IS NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO dividends_quarantine
            SELECT dividend.*, 'unverified_period_end_placeholder', ?
            FROM dividends dividend
            WHERE announcement_date IS NULL
              AND (
                  (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
                  OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
              )
            """,
            [timestamp],
        )
        connection.execute(
            """
            DELETE FROM dividends
            WHERE announcement_date IS NULL
              AND (
                  (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
                  OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
              )
            """
        )

        # Legacy empty-payload archives are content-free by definition: no
        # replayable source material exists. Quarantine them together with the
        # audit rows and batches that referenced them, so active lineage only
        # contains verifiable bytes. Nothing is deleted. The empty-hash set is
        # snapshotted in a temp table so later orphan detection does not
        # double-count the same rows.
        if empty_archive_count or empty_audit_count or orphan_batch_count:
            connection.execute(
                """
                CREATE TEMP TABLE _vd_empty_hashes AS
                SELECT DISTINCT raw_response_hash FROM raw_response_archive
                WHERE payload IS NULL OR OCTET_LENGTH(payload) = 0
                """
            )
            empty_batch_count = connection.execute(
                """
                SELECT COUNT(*) FROM fetch_batch
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO raw_response_archive_quarantine
                SELECT archive.*, 'unsupported_legacy_empty_payload', ?
                FROM raw_response_archive archive
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """,
                [timestamp],
            )
            connection.execute(
                """
                INSERT INTO source_audit_quarantine
                SELECT audit.*, 'unsupported_legacy_empty_payload', ?
                FROM source_audit audit
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """,
                [timestamp],
            )
            connection.execute(
                """
                INSERT INTO fetch_batch_quarantine
                SELECT batch.*, 'unsupported_legacy_empty_payload', ?
                FROM fetch_batch batch
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """,
                [timestamp],
            )
            connection.execute(
                """
                DELETE FROM raw_response_archive
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """
            )
            connection.execute(
                """
                DELETE FROM source_audit
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """
            )
            connection.execute(
                """
                DELETE FROM fetch_batch
                WHERE raw_response_hash IN (SELECT raw_response_hash FROM _vd_empty_hashes)
                """
            )
            # Remaining orphans: batches pointing at hashes that never existed
            # in the archive at all (excludes the empty-hash set above).
            remaining_orphan_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM fetch_batch batch
                LEFT JOIN raw_response_archive archive ON batch.raw_response_hash = archive.raw_response_hash
                WHERE archive.raw_response_hash IS NULL
                """
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO fetch_batch_quarantine
                SELECT batch.*, 'unsupported_legacy_orphan_batch', ?
                FROM fetch_batch batch
                LEFT JOIN raw_response_archive archive ON batch.raw_response_hash = archive.raw_response_hash
                WHERE archive.raw_response_hash IS NULL
                """,
                [timestamp],
            )
            connection.execute(
                """
                DELETE FROM fetch_batch
                WHERE batch_id IN (
                    SELECT batch.batch_id
                    FROM fetch_batch batch
                    LEFT JOIN raw_response_archive archive ON batch.raw_response_hash = archive.raw_response_hash
                    WHERE archive.raw_response_hash IS NULL
                )
                """
            )
            connection.execute("DROP TABLE IF EXISTS _vd_empty_hashes")

    return {
        "quarantined_lineage_records": lineage_count,
        "quarantined_dividend_records": dividend_count,
        "quarantined_empty_payload_archives": empty_archive_count,
        "quarantined_empty_payload_audits": empty_audit_count,
        "quarantined_empty_payload_batches": empty_batch_count,
        "quarantined_orphan_batches": remaining_orphan_count,
    }
