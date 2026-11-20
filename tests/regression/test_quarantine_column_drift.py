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
from tests.support.seed import Seed

_MD5_HASH = "df29aee0216a2996352c30ea0141100a"


def _archive_all_columns(duck: DuckDBStore) -> list[str]:
    with duck.read_connection() as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info('raw_response_archive_all')").fetchall()]


def test_quarantine_survives_column_order_drift(duckdb_store: DuckDBStore) -> None:
    duck = duckdb_store
    columns = _archive_all_columns(duck)
    assert columns, "raw_response_archive_all must exist in the schema"

    drifted = list(reversed(columns))
    with duck.transaction() as connection:
        connection.execute("DROP TABLE IF EXISTS raw_response_archive_quarantine")
        connection.execute(
            "CREATE TABLE raw_response_archive_quarantine ("
            + ", ".join(f"{name} VARCHAR" for name in drifted)
            + ", quarantine_reason VARCHAR, quarantined_at TIMESTAMP)"
        )
        # 留（第 3 类·隔离归档 / 故意造错）：`fetch_batch` 这一行是**论点载荷**，不是"造一行"：
        # `batch_id='B-LEGACY'` 本身就是"老版本批次"（`legacy_lineage_records` 要数的孤儿批次），
        # 工厂也没有这张表的方法 —— 归档搬运用例的写入本身就是被断言的对象。
        connection.execute(
            "INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, "
            "fetch_time, raw_response_hash, row_count, confidence, created_at) "
            "VALUES ('B-LEGACY', 'income_statement', 'csmar', 'csmar-c17-fi_t2/1', "
            "TIMESTAMP '2026-09-19 00:07:51', ?, 1, 'strict', TIMESTAMP '2026-09-19 00:07:51')",
            [_MD5_HASH],
        )
        # 留（第 3 类·隔离归档 / 故意造错）：故意写 `payload = NULL` —— "空载荷归档"正是
        # `empty_payload_archives` 要数的那类行；它必须与 `fetch_batch` 共用同一个 `_MD5_HASH`，
        # 否则"有审计行、没归档行"的坏溯源就造不出来。
        connection.execute(
            "INSERT INTO raw_response_archive "
            "(raw_response_hash, source, fetch_time, payload, api_version, integrity_verified, created_at) "
            "VALUES (?, 'csmar', TIMESTAMP '2026-09-19 00:07:51', NULL, 'v1', TRUE, "
            "TIMESTAMP '2026-09-19 00:07:51')",
            [_MD5_HASH],
        )
        # 这一条走工厂：`source_audit()` 的批次列叫 `fetch_batch_id`（不是 `batch_id`），
        # 抄错列名当场 `UnknownSeedColumn`；坏 hash 由调用点逐字给（工厂不校验形状）。
        # `join(connection)` 让它留在本用例这条事务里，与上面 `DROP/CREATE` 漂移表同进同退。
        Seed(duck).join(connection).source_audit(
            "600848", "deducted_net_profit",
            report_date="2009-12-31", value=1.0, source="csmar",
            fetch_batch_id="B-LEGACY",
            fetch_time="2026-09-19 00:07:51",
            # 32 位 md5，**不是** 64 位 sha256：故意造"非本系统写入"的坏摘要。
            raw_response_hash=_MD5_HASH,
            confidence="strict",
            created_at="2026-09-19 00:07:51",
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
