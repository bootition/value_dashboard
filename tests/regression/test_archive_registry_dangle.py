"""归档分区登记表有「死链」时，视图重建不得崩（2026-11-23 生产事故）。

现场：`raw_response_archive_partitions` 里 4 条指向**已被删除的实体表**
（`raw_response_archive_20260917_044324` 等），而
`_recreate_raw_archive_view()` 原来**无条件相信登记表**、把每一行拼进
`UNION ALL` → 视图建不出来 → **每一次价格落库都报**
`Catalog Error: Table with name raw_response_archive_20260917_044324 does not exist`
→ 单只股票写入 100% 失败、整轮更新永远跑不完、日志刷屏数小时。

本用例钉住：**登记表是记账，实体表才是事实**；不一致时以事实为准，
并让死链以 WARNING 暴露出来（不静默吞掉）。
"""

from __future__ import annotations

import logging

import duckdb

from app.core.storage import duckdb_store as store_module


def _make_registry(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE raw_response_archive_partitions (
            partition_table VARCHAR PRIMARY KEY,
            created_at      TIMESTAMP,
            closed_at       TIMESTAMP,
            row_count       BIGINT,
            estimated_bytes BIGINT
        )
        """
    )


def _make_archive_partition(conn: duckdb.DuckDBPyConnection, name: str, hashes: list[str]) -> None:
    conn.execute(
        f'CREATE TABLE "{name}" ('
        "raw_response_hash VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, "
        "fetch_time TIMESTAMP NOT NULL, payload BLOB, api_version VARCHAR, "
        "integrity_verified BOOLEAN DEFAULT FALSE, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    for item in hashes:
        conn.execute(
            f'INSERT INTO "{name}" (raw_response_hash, source, fetch_time) '
            "VALUES (?, 'test', TIMESTAMP '2026-11-23 00:00:00')",
            [item],
        )


def _register(conn: duckdb.DuckDBPyConnection, name: str) -> None:
    conn.execute(
        "INSERT INTO raw_response_archive_partitions (partition_table, created_at) "
        "VALUES (?, TIMESTAMP '2026-11-23 00:00:00')",
        [name],
    )


def test_dangling_registry_row_does_not_break_view(caplog) -> None:
    """登记行指向不存在的表 → 视图照样建得出来，且只包含**存在**的分区。"""
    conn = duckdb.connect(":memory:")
    _make_registry(conn)
    _make_archive_partition(conn, "raw_response_archive_20261101_000000", ["h1", "h2"])
    _register(conn, "raw_response_archive_20261101_000000")
    # 死链：登记了但实体不存在（正是生产现场那 4 条）
    _register(conn, "raw_response_archive_20260917_044324")

    with caplog.at_level(logging.WARNING, logger=store_module.__name__):
        store_module._recreate_raw_archive_view(conn)

    # ① 视图建出来了（改进前这里直接 Catalog Error）
    rows = conn.execute(
        "SELECT raw_response_hash FROM raw_response_archive_all "
        "ORDER BY raw_response_hash"
    ).fetchall()
    assert [r[0] for r in rows] == ["h1", "h2"]
    # ② 死链被如实暴露，不静默
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("raw_response_archive_20260917_044324" in w for w in warnings), warnings
    conn.close()


def test_all_registry_rows_dangling_does_not_create_broken_view() -> None:
    """全都死链 → 不建视图（而不是建一个引用不存在表的坏视图）。"""
    conn = duckdb.connect(":memory:")
    _make_registry(conn)
    _register(conn, "raw_response_archive_20260101_000000")

    store_module._recreate_raw_archive_view(conn)

    existing = {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "raw_response_archive_all" not in existing
    conn.close()


def test_healthy_registry_still_unions_everything() -> None:
    """正常情况（全部实体存在）→ 行为与改前一致，一个分区都不许漏。"""
    conn = duckdb.connect(":memory:")
    _make_registry(conn)
    _make_archive_partition(conn, "raw_response_archive_20261101_000000", ["a"])
    _make_archive_partition(conn, "raw_response_archive_20261102_000000", ["b"])
    _register(conn, "raw_response_archive_20261101_000000")
    _register(conn, "raw_response_archive_20261102_000000")

    store_module._recreate_raw_archive_view(conn)

    rows = conn.execute(
        "SELECT raw_response_hash FROM raw_response_archive_all "
        "ORDER BY raw_response_hash"
    ).fetchall()
    assert [r[0] for r in rows] == ["a", "b"]
    conn.close()
