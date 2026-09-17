from __future__ import annotations

import pytest

from app.core.storage.duckdb_store import DuckDBStore


def test_read_query_works_while_same_process_write_connection_is_open(
    duckdb_store: DuckDBStore,
) -> None:
    with duckdb_store.transaction():
        rows = duckdb_store.read_query("SELECT 1 AS ready")

    assert rows == [{"ready": 1}]


def test_read_query_rejects_write_statements(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="exactly one SELECT"):
        duckdb_store.read_query("CREATE TABLE forbidden_write(value INTEGER)")

    tables = duckdb_store.read_query(
        "SELECT table_name FROM information_schema.tables WHERE table_name = 'forbidden_write'"
    )
    assert tables == []


def test_old_write_lock_owner_cannot_remove_a_replacement_lock(duckdb_store: DuckDBStore) -> None:
    replacement = "pid=123\ntime=0\ntoken=replacement-owner\n"

    with duckdb_store._write_lock():
        duckdb_store._lock_path.write_text(replacement, encoding="utf-8")

    assert duckdb_store._lock_path.read_text(encoding="utf-8") == replacement
    duckdb_store._lock_path.unlink()


def test_connection_config_always_has_uniform_triple(duckdb_store: DuckDBStore) -> None:
    """同一进程所有 DuckDB 连接必须使用同一套配置（reports/104）。

    差异配置并发会触发 different-configuration 错误，普通请求重试约 28s
    后 500/503。这里锁定配置三元组永远完整输出。
    """
    config = duckdb_store._connection_config()

    assert set(config) == {"memory_limit", "threads", "preserve_insertion_order"}
    assert config["threads"]
    assert str(config["preserve_insertion_order"]).lower() in {"true", "false"}


def test_external_file_lock_detection_requires_specific_evidence() -> None:
    """2026-09-17：泛化的 "Cannot open file" 也可能是权限/路径等永久性
    IO 错误，不能一律当成"外部更新持锁"，否则用户会永远看到"正在更新中"。"""
    from app.core.storage.duckdb_store import _is_external_file_lock

    assert _is_external_file_lock(
        'IO Error: Cannot open file "D:\\a.duckdb": '
        "另一个程序正在使用此文件，进程无法访问。"
    )
    assert _is_external_file_lock(
        'IO Error: Cannot open file "D:\\a.duckdb"\n'
        "File is already open in python.exe (PID 28840)"
    )
    # 无应用级更新锁时，泛化消息不得判定为锁竞争。
    assert not _is_external_file_lock('IO Error: Cannot open file "D:\\a.duckdb": 拒绝访问。')
    # 应用级更新锁文件确认存在时才接受泛化消息。
    assert _is_external_file_lock('IO Error: Cannot open file "D:\\a.duckdb"', lock_active=True)
