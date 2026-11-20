from __future__ import annotations

from datetime import UTC, datetime

from app.core.data_maintenance import legacy_quarantine_summary, quarantine_legacy_records


def test_quarantine_moves_only_unsupported_legacy_records(duckdb_store) -> None:
    timestamp = datetime.now(UTC)
    # 整块在**已持有写锁**的 `write_connection()` 事务里跨 fetch_batch/source_audit/
    # dividends 一起落库，且共用同一个 `timestamp`；这三行的写入就是本用例的论点
    # （哪一批/哪一行进隔离），逐处按下标登记：
    with duckdb_store.write_connection() as connection:
        # 留（第 3 类·隔离搬家论点）：`fetch_batch` 是"哪一批算合法来源"的那一批。
        connection.execute(
            """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash, row_count, confidence)
               VALUES ('valid', 'income_statement', 'akshare_eastmoney', 'test', ?, ?, 1, 'approximate')""",
            [timestamp, "a" * 64],
        )
        # 留（第 3 类·故意造错）：`source_audit` 的 `raw_response_hash = ''` 是孤儿行，
        # 正是"必须被搬进隔离表"的那条；工厂不提供造坏数据的开关。
        connection.execute(
            """INSERT INTO source_audit
               (stock_code, field_name, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
               VALUES
               ('000001', '__price_count__', 'local_cache', 'orphan', ?, '', 'missing'),
               ('000001', 'revenue', 'akshare_eastmoney', 'valid', ?, ?, 'approximate')""",
            [timestamp, timestamp, "a" * 64],
        )
        # 留（第 3 类·故意造错）：`dividends` 里这条"期末占位"行（未证公告日）是
        # 被测隔离逻辑要搬走的那一行，不是普通分红播种。
        connection.execute(
            """INSERT INTO dividends
               (stock_code, ex_date, announcement_date, dividend_per_share)
               VALUES
               ('000001', '2024-12-31', NULL, 1.0),
               ('000001', '2024-07-01', '2024-06-01', 2.0)"""
        )

    assert legacy_quarantine_summary(duckdb_store) == {
        "legacy_lineage_records": 1,
        "unverified_dividend_records": 1,
        "empty_payload_archives": 0,
        "action": "quarantine without deletion; retained records are excluded from research tables",
    }

    assert quarantine_legacy_records(duckdb_store) == {
        "quarantined_lineage_records": 1,
        "quarantined_dividend_records": 1,
        "quarantined_empty_payload_archives": 0,
        "quarantined_empty_payload_audits": 0,
        "quarantined_empty_payload_batches": 0,
        # the fixture's 'valid' batch has no matching archive row either
        "quarantined_orphan_batches": 1,
    }
    assert duckdb_store.read_query("SELECT field_name FROM source_audit") == [{"field_name": "revenue"}]
    assert duckdb_store.read_query(
        "SELECT quarantine_reason FROM source_audit_quarantine"
    ) == [{"quarantine_reason": "unsupported_legacy_lineage"}]
    assert duckdb_store.read_query("SELECT ex_date FROM dividends") == [{"ex_date": datetime(2024, 7, 1).date()}]
    assert duckdb_store.read_query(
        "SELECT quarantine_reason FROM dividends_quarantine"
    ) == [{"quarantine_reason": "unverified_period_end_placeholder"}]


def test_quarantine_moves_empty_payload_archives_with_their_audits(duckdb_store) -> None:
    timestamp = datetime.now(UTC)
    empty_hash = "e" * 64
    other_hash = "f" * 64
    # 整块在**已持有写锁**的 `write_connection()` 事务里（与空 payload 归档一起构成
    # "空归档"场景），三行是同一批证据的三个面：
    with duckdb_store.write_connection() as connection:
        # 留（第 3 类·隔离搬家论点）：`fetch_batch` 是被空载荷归档牵着走的批次。
        connection.execute(
            """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash, row_count, confidence)
               VALUES ('empty-batch', 'price_daily', 'baostock', 'test', ?, ?, 1, 'approximate')""",
            [timestamp, empty_hash],
        )
        # 留（第 3 类·故意造错）：`NULL` 与 `CAST('' AS BLOB)` 两种**空载荷**是论点本体
        # （哪种算"空"、怎么搬），工厂不造这种数据。
        connection.execute(
            """INSERT INTO raw_response_archive
               (raw_response_hash, source, fetch_time, payload, api_version)
               VALUES
               (?, 'baostock', ?, NULL, 'test'),
               (?, 'baostock', ?, CAST('' AS BLOB), 'test')""",
            [empty_hash, timestamp, other_hash, timestamp],
        )
        # 留（第 3 类·隔离搬家论点）：指向空载荷批次的审计行（跟着批次一起被隔离）。
        connection.execute(
            """INSERT INTO source_audit
               (stock_code, field_name, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
               VALUES
               ('000001', 'latest_close', 'baostock', 'empty-batch', ?, ?, 'approximate')""",
            [timestamp, empty_hash],
        )

    summary = legacy_quarantine_summary(duckdb_store)
    assert summary["empty_payload_archives"] == 2

    result = quarantine_legacy_records(duckdb_store)
    assert result["quarantined_empty_payload_archives"] == 2
    assert result["quarantined_empty_payload_audits"] == 1
    assert result["quarantined_empty_payload_batches"] == 1
    assert result["quarantined_orphan_batches"] == 0

    assert duckdb_store.read_query("SELECT raw_response_hash FROM raw_response_archive") == []
    assert duckdb_store.read_query("SELECT field_name FROM source_audit") == []
    assert duckdb_store.read_query("SELECT batch_id FROM fetch_batch") == []
    quarantined = duckdb_store.read_query(
        "SELECT DISTINCT quarantine_reason FROM raw_response_archive_quarantine"
    )
    assert quarantined == [{"quarantine_reason": "unsupported_legacy_empty_payload"}]
