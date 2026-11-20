"""事故出处：reports/117 / 2026-11-12。

Regression: legacy-record quarantine must tolerate quarantine-table column drift.

2026-11-12 production finding: `vd.bat data quarantine_legacy_records_execute`
crashed with

    ConversionException: Unimplemented type for cast (BOOLEAN -> TIMESTAMP)
    when casting from source column integrity_verified

because every ``INSERT INTO <x>_quarantine SELECT x.*, 'reason', ?`` relied on
positional column order.  The quarantine tables are created by ``CTAS`` from the
hot tables and later mutated (``ALTER TABLE ... ADD COLUMN storage``), while
``raw_response_archive_all`` is a view whose column order differs from the hot
table.  Positional inserts therefore bind a BOOLEAN into a TIMESTAMP column and
the whole maintenance command fails (the transaction rolls back, so nothing is
lost — but the approved repair path is unusable).

The fix is to insert ``BY NAME`` so column order drift can never corrupt the
mapping.  This test pins that behaviour with a deliberately drifted table.
"""

from __future__ import annotations

from app.core.data_maintenance import legacy_quarantine_summary, quarantine_legacy_records
from app.core.storage.duckdb_store import DuckDBStore

_MD5_HASH = "df29aee0216a2996352c30ea0141100a"


def _archive_all_columns(duck: DuckDBStore) -> list[str]:
    with duck.read_connection() as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info('raw_response_archive_all')").fetchall()]


def test_quarantine_survives_column_order_drift(duckdb_store: DuckDBStore) -> None:
    duck = duckdb_store
    columns = _archive_all_columns(duck)
    assert columns, "raw_response_archive_all must exist in the schema"

    # Simulate a quarantine table built by an older release: same column names,
    # different physical order (this is exactly what made the positional INSERT
    # cast a BOOLEAN into a TIMESTAMP).
    # 留（第 3 类·隔离归档 / 故意造错）：下面三条 INSERT 是**本文件的论点载荷**，不是"造一行"：
    #   * `fetch_batch` 的 `batch_id='B-LEGACY'` 是"老版本批次"，工厂没有也不该有这张表的
    #     方法（整批搬家用例的写入本身就是被断言的对象）；
    #   * `raw_response_archive` 故意写 `payload = NULL`（"空载荷归档"正是
    #     `empty_payload_archives` 要数的那类行）；
    #   * `source_audit` 与归档行共用 `_MD5_HASH`（32 位 md5，**不是** 64 位 sha256），
    #     故意造的是"溯源已被篡改/非本系统写入"的坏 hash，工厂按生产口径只发正确形状。
    # 三条还都写在调用点自己的 `duck.transaction()` 里（要与 `DROP/CREATE` 漂移表同事务）。
    drifted = list(reversed(columns))
    with duck.transaction() as connection:
        connection.execute("DROP TABLE IF EXISTS raw_response_archive_quarantine")
        connection.execute(
            "CREATE TABLE raw_response_archive_quarantine ("
            + ", ".join(f"{name} VARCHAR" for name in drifted)
            + ", quarantine_reason VARCHAR, quarantined_at TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, "
            "fetch_time, raw_response_hash, row_count, confidence, created_at) "
            "VALUES ('B-LEGACY', 'income_statement', 'csmar', 'csmar-c17-fi_t2/1', "
            "TIMESTAMP '2026-09-19 00:07:51', ?, 1, 'strict', TIMESTAMP '2026-09-19 00:07:51')",
            [_MD5_HASH],
        )
        connection.execute(
            "INSERT INTO raw_response_archive "
            "(raw_response_hash, source, fetch_time, payload, api_version, integrity_verified, created_at) "
            "VALUES (?, 'csmar', TIMESTAMP '2026-09-19 00:07:51', NULL, 'v1', TRUE, "
            "TIMESTAMP '2026-09-19 00:07:51')",
            [_MD5_HASH],
        )
        connection.execute(
            "INSERT INTO source_audit (stock_code, field_name, report_date, value, source, "
            "fetch_batch_id, fetch_time, raw_response_hash, confidence, created_at) "
            "VALUES ('600848', 'deducted_net_profit', DATE '2009-12-31', 1.0, 'csmar', "
            "'B-LEGACY', TIMESTAMP '2026-09-19 00:07:51', ?, 'strict', "
            "TIMESTAMP '2026-09-19 00:07:51')",
            [_MD5_HASH],
        )

    summary = legacy_quarantine_summary(duck)
    assert summary["legacy_lineage_records"] >= 1
    assert summary["empty_payload_archives"] >= 1

    result = quarantine_legacy_records(duck)

    assert result["quarantined_lineage_records"] >= 1
    assert result["quarantined_empty_payload_archives"] >= 1
    with duck.read_connection() as connection:
        archived = connection.execute(
            "SELECT quarantine_reason, quarantined_at FROM raw_response_archive_quarantine "
            "WHERE raw_response_hash = ?",
            [_MD5_HASH],
        ).fetchall()
        assert len(archived) == 1, archived
        assert archived[0][0] == "unsupported_legacy_empty_payload"
        assert archived[0][1] is not None
        # Nothing is deleted: the row lives on in the quarantine table.
        remaining = connection.execute(
            "SELECT COUNT(*) FROM source_audit WHERE raw_response_hash = ?", [_MD5_HASH]
        ).fetchone()[0]
        assert remaining == 0, "quarantined audit rows must leave the active table"
        quarantined_audit = connection.execute(
            "SELECT COUNT(*) FROM source_audit_quarantine WHERE raw_response_hash = ?",
            [_MD5_HASH],
        ).fetchone()[0]
        assert quarantined_audit == 1
