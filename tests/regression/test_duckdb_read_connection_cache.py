"""只读连接复用（per-thread read-only connection cache）的契约钉子。

2026-11-24：`DuckDBStore.read_query()` 从「每次查询开一条连接」改成「每个 store、
每个线程复用一条只读连接」。速度差来自实测（`.probe/p2.py`）：

* 冷开一条 `duckdb.connect(read_only=True)` + close：**15.4 ms**（小库）/
  **49.3 ms**（正式库量级）；而进程内已有同一 Database 实例时再开只要
  **0.22 ms** —— 差 70~200 倍，open-per-query 的时间几乎全在建实例/加载 catalog；
* 打开后的连接上跑一条 SELECT：**0.21 ms**。

复用把这条成本摊掉了，但同时引入三类**正确性**风险，本文件逐个钉死：

1. **写后必读到新值**：缓存连接绝不能返回写之前的旧页；
2. **写路径先关缓存**：同进程 read_only 与读写连接不能共存（DuckDB 直接抛
   `Can't open a connection to same database file with a different configuration`），
   所以写之前必须先把缓存只读连接关掉，否则写只能靠 ~90s 退避硬等；
3. **线程安全 + 外部持锁**：DuckDB Python 连接非线程安全（一线程一条，不共享）；
   外部写进程持锁时必须如实抛 `DuckDBReadLockedError`，绝不能把"打不开"缓存成
   "读到成功"。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import pytest

from app.core.storage import duckdb_store as duckdb_store_module
from app.core.storage.duckdb_store import DuckDBReadLockedError, DuckDBStore


def _cache_entries(store: DuckDBStore) -> dict[int, Any]:
    cache = store._read_cache
    assert cache is not None, "test 环境必须开启只读连接缓存"
    return cache._entries


# ---------------------------------------------------------------------------
# 1. 写后必读到新值
# ---------------------------------------------------------------------------
def test_read_query_sees_rows_written_right_after_a_cached_read(
    duckdb_store: DuckDBStore,
) -> None:
    """写一行 → 立刻 read_query 必须看到它（INSERT / 就地 UPDATE / 事务三条路径）。"""
    duckdb_store.write_query("CREATE TABLE cache_probe(value INTEGER)")
    duckdb_store.write_query("INSERT INTO cache_probe VALUES (1)")
    # 第一次读到后面几次都走缓存连接（同一线程复用）。
    assert duckdb_store.read_query("SELECT value FROM cache_probe") == [{"value": 1}]
    for _ in range(3):
        assert duckdb_store.read_query("SELECT value FROM cache_probe") == [{"value": 1}]
    assert len(_cache_entries(duckdb_store)) == 1

    duckdb_store.write_query("INSERT INTO cache_probe VALUES (2)")
    assert duckdb_store.read_query("SELECT sum(value) AS s FROM cache_probe") == [{"s": 3}]

    # 就地 UPDATE：行数不变——最容易"缓存读到旧页"的形状。
    duckdb_store.write_query("UPDATE cache_probe SET value = 100 WHERE value = 1")
    assert duckdb_store.read_query("SELECT sum(value) AS s FROM cache_probe") == [{"s": 102}]

    # transaction() 是第二条写路径，同样必须让缓存失效。
    with duckdb_store.transaction() as connection:
        connection.execute("INSERT INTO cache_probe VALUES (1000)")
    assert duckdb_store.read_query("SELECT sum(value) AS s FROM cache_probe") == [{"s": 1102}]


def test_repeated_reads_reuse_one_connection(duckdb_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一线程连续 20 次 read_query 最多只应该再开 1 条连接。"""
    calls: list[str] = []
    real_connect = duckdb.connect

    def counting_connect(database: Any = ":memory:", *args: Any, **kwargs: Any) -> Any:
        calls.append(str(database))
        return real_connect(database, *args, **kwargs)

    duckdb_store.write_query("CREATE TABLE reuse_probe(value INTEGER)")
    monkeypatch.setattr(duckdb_store_module.duckdb, "connect", counting_connect)

    assert duckdb_store.read_query("SELECT 1 AS one") == [{"one": 1}]
    baseline = len(calls)
    for _ in range(20):
        assert duckdb_store.read_query("SELECT 1 AS one") == [{"one": 1}]
    assert len(calls) - baseline <= 1, (
        f"20 次读又开了 {len(calls) - baseline} 条连接——连接没有复用"
    )


# ---------------------------------------------------------------------------
# 2. 写路径必须先把缓存只读连接关掉（同进程 read_only vs 读写不能共存）
# ---------------------------------------------------------------------------
def test_write_path_closes_cached_read_connections_before_opening_the_writer(
    duckdb_store: DuckDBStore,
) -> None:
    duckdb_store.write_query("CREATE TABLE drain_probe(value INTEGER)")
    duckdb_store.read_query("SELECT count(*) AS c FROM drain_probe")
    assert len(_cache_entries(duckdb_store)) == 1

    observed: dict[str, int] = {}
    real_connect_writer = duckdb_store._connect_writer

    def spy_connect_writer() -> Any:
        observed["cache_size_when_writer_opens"] = len(_cache_entries(duckdb_store))
        return real_connect_writer()

    duckdb_store._connect_writer = spy_connect_writer  # type: ignore[method-assign]
    duckdb_store.write_query("INSERT INTO drain_probe VALUES (1)")
    duckdb_store._connect_writer = real_connect_writer  # type: ignore[method-assign]

    assert observed == {"cache_size_when_writer_opens": 0}, (
        "写连接打开时缓存只读连接还活着——DuckDB 会报 different configuration"
    )
    assert duckdb_store.read_query("SELECT count(*) AS c FROM drain_probe") == [{"c": 1}]


def test_bare_duckdb_connect_drops_cached_read_connections(duckdb_store: DuckDBStore) -> None:
    """绕过 store 的裸 `duckdb.connect(path)`（测试自建表 / 脚本）不能被缓存挡死。"""
    duckdb_store.write_query("CREATE TABLE bare_probe(value INTEGER)")
    assert duckdb_store.read_query("SELECT count(*) AS c FROM bare_probe") == [{"c": 0}]
    assert len(_cache_entries(duckdb_store)) == 1

    connection = duckdb.connect(str(duckdb_store.db_path))
    try:
        assert connection.execute("SELECT count(*) FROM bare_probe").fetchall() == [(0,)]
    finally:
        connection.close()
    assert len(_cache_entries(duckdb_store)) == 0
    assert duckdb_store.read_query("SELECT count(*) AS c FROM bare_probe") == [{"c": 0}]


def test_close_releases_cached_connections_and_keeps_reading(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query("CREATE TABLE close_probe(value INTEGER)")
    duckdb_store.read_query("SELECT count(*) AS c FROM close_probe")
    entries = dict(_cache_entries(duckdb_store))
    assert len(entries) == 1

    duckdb_store.close()
    assert len(_cache_entries(duckdb_store)) == 0
    for entry in entries.values():
        with pytest.raises(duckdb.Error):
            entry.conn.execute("SELECT 1").fetchall()
    # 关掉缓存不等于关掉 store：之后照常能读。
    assert duckdb_store.read_query("SELECT count(*) AS c FROM close_probe") == [{"c": 0}]


# ---------------------------------------------------------------------------
# 3. 并发 + 写事务期间的读（与改前 open-per-query 行为逐字一致）
# ---------------------------------------------------------------------------
def test_concurrent_read_query_from_four_threads(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query("CREATE TABLE concurrent_probe(value INTEGER)")
    duckdb_store.write_query("INSERT INTO concurrent_probe SELECT range FROM range(100)")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                assert duckdb_store.read_query("SELECT count(*) AS c FROM concurrent_probe") == [
                    {"c": 100}
                ]
        except BaseException as error:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            errors.append(error)

    threads = [threading.Thread(target=worker, name=f"vd-read-{index}") for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    # 一线程一条，绝不跨线程共享同一个 connection。
    thread_ids = list(_cache_entries(duckdb_store))
    assert len(thread_ids) == len(set(thread_ids)) == 4


def _read_while_another_thread_writes(store: DuckDBStore, table: str) -> dict[str, Any]:
    """写事务开着的时候，另一个线程 read_query 会得到什么（缓存开/关各跑一遍）。"""
    store.write_query(f"CREATE TABLE {table}(value INTEGER)")
    store.write_query(f"INSERT INTO {table} VALUES (1)")
    started = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with store.transaction() as connection:
            connection.execute(f"INSERT INTO {table} VALUES (2)")
            started.set()
            release.wait(10)

    thread = threading.Thread(target=writer, name="vd-cache-ab-writer")
    thread.start()
    outcome: dict[str, Any] = {}
    try:
        assert started.wait(10), "写事务没有起来"
        try:
            outcome["rows"] = store.read_query(f"SELECT count(*) AS c FROM {table}")
        except BaseException as error:  # noqa: BLE001 - 异常类型本身就是要比较的结果
            outcome["error"] = type(error).__name__
    finally:
        release.set()
        thread.join(60)
    return outcome


def test_read_query_during_write_transaction_matches_open_per_query(
    duckdb_store: DuckDBStore,
) -> None:
    """写事务进行中，别的线程 read_query 的结果必须与改前（open-per-query）一致。

    这条盯的是"别静默返回旧数据/别偷偷读到未提交数据"：
    * 缓存开着时，读线程拿到的是一个**回退出来的读写连接**（不进缓存），
      看到的是提交前的数据（1 行），与改前逐字一致；
    * 关掉缓存（= 改前的 open-per-query）再跑一遍，两边结果必须相等。
    """
    with_cache = _read_while_another_thread_writes(duckdb_store, "ab_with_cache")
    # 回退出来的读写连接绝不能进缓存：否则本进程会长期握着读写句柄。
    assert len(_cache_entries(duckdb_store)) == 0

    cache = duckdb_store._read_cache
    assert cache is not None
    cache.close()
    duckdb_store._read_cache = None
    without_cache = _read_while_another_thread_writes(duckdb_store, "ab_without_cache")

    assert with_cache == without_cache
    assert with_cache == {"rows": [{"c": 1}]}


# ---------------------------------------------------------------------------
# 4. 更新窗口：写锁文件出现即放弃缓存；外部进程持锁仍如实报错
# ---------------------------------------------------------------------------
def test_update_lock_file_invalidates_the_cache(duckdb_store: DuckDBStore) -> None:
    """`.value-dashboard.update.lock` 一出现就丢缓存——更新窗口里绝不用旧连接。"""
    duckdb_store.write_query("CREATE TABLE lock_probe(value INTEGER)")
    duckdb_store.write_query("INSERT INTO lock_probe VALUES (1)")
    assert duckdb_store.read_query("SELECT value FROM lock_probe") == [{"value": 1}]
    assert len(_cache_entries(duckdb_store)) == 1

    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"
    lock_path.write_text(f"pid={0}\ntime={time.time()}\n", encoding="utf-8")
    try:
        assert duckdb_store.read_query("SELECT value FROM lock_probe") == [{"value": 1}]
        assert len(_cache_entries(duckdb_store)) == 0, (
            "写锁窗口里还留着缓存只读连接——会把更新子进程挡在门外"
        )
    finally:
        lock_path.unlink(missing_ok=True)


_EXTERNAL_WRITER = '''
import sys
import time

import duckdb

database, marker = sys.argv[1], sys.argv[2]
config = {
    "memory_limit": "1GB",
    "threads": "2",
    "preserve_insertion_order": "false",
}
connection = duckdb.connect(database, config=config)
connection.execute("INSERT INTO external_probe VALUES (42)")
open(marker, "w", encoding="utf-8").write("locked")
try:
    time.sleep(120)  # 持锁直到父进程杀掉我
finally:
    connection.close()
'''


def test_external_writer_process_still_raises_read_locked_error(
    duckdb_store: DuckDBStore,
    tmp_path: Path,
) -> None:
    """外部写进程持锁时：如实抛 `DuckDBReadLockedError`，且**不把失败缓存成成功**。"""
    duckdb_store.write_query("CREATE TABLE external_probe(value INTEGER)")

    script = tmp_path / "external_writer.py"
    script.write_text(_EXTERNAL_WRITER, encoding="utf-8")
    marker = tmp_path / "writer-ready.marker"
    lock_path = duckdb_store.db_path.parent / ".value-dashboard.update.lock"

    child = subprocess.Popen(  # noqa: S603 - 参数全部由本测试给定
        [sys.executable, str(script), str(duckdb_store.db_path), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                _out, err = child.communicate()
                pytest.fail(f"外部写进程提前退出了：{err[-2000:]}")
            time.sleep(0.05)
        assert marker.exists(), "外部写进程没有抢到文件锁"

        lock_path.write_text(f"pid={child.pid}\ntime={time.time()}\n", encoding="utf-8")
        # 两次都必须抛：第二次如果"缓存成功"就是假绿。
        for _attempt in range(2):
            with pytest.raises(DuckDBReadLockedError):
                duckdb_store.read_query("SELECT count(*) AS c FROM external_probe")
        assert len(_cache_entries(duckdb_store)) == 0
    finally:
        child.terminate()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            child.kill()
            child.wait(timeout=30)
        lock_path.unlink(missing_ok=True)

    # 写进程走了 → 必须能读到它刚写下的值（说明失败没有被缓存成"成功"）。
    # 终止子进程后 Windows 释放文件句柄有延迟（机器负载高时可达秒级），
    # 这里只等句柄释放，不放宽断言：最终必须读到 42。
    deadline = time.monotonic() + 30
    while True:
        try:
            assert duckdb_store.read_query("SELECT value FROM external_probe") == [{"value": 42}]
            break
        except DuckDBReadLockedError:
            if time.monotonic() > deadline:  # pragma: no cover - 句柄一直没释放
                raise
            time.sleep(0.2)
