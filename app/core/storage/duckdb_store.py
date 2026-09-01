"""DuckDB 连接管理器 - open-per-query 模式

审查问题1修订：Web 进程不持有长期 DuckDB 连接，每个查询打开→执行→关闭。
CLI 写操作通过应用级写锁文件协调。

DuckDB 进程模型：单进程读写，或多进程只读，二者互斥。
- Web 进程：open-per-query，每次查询打开连接、执行、关闭，不长期占据文件锁
- CLI 写操作：抢锁 → 打开写连接 → 执行 → 释放锁
- backup restore：独占操作，前置条件是 Web 进程退出
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb

from app.core.storage.maintenance import assert_writes_allowed
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError

# 空/半写锁文件宽限期（秒）：与 update_lock.py 同口径，超过即视为死锁
_STALE_MALFORMED_LOCK_SECONDS = 5.0


def archive_raw_response_if_absent(
    conn: Any,
    *,
    raw_response_hash: str,
    source: str,
    fetch_time: Any,
    payload: bytes | None,
    api_version: str | None,
) -> bool:
    """Append a raw response payload unless its content hash already exists.

    Schema v16 splits the archive into a small hot ``raw_response_archive``
    for new writes and ``raw_response_archive_history`` for legacy BLOBs.
    Writers are serialized by the application write lock and DuckDB's
    single-writer rule, so a primary-key probe followed by a plain INSERT is
    race-free and keeps the same first-writer-wins semantics as the old
    ``ON CONFLICT DO NOTHING`` statement.
    """
    existing = conn.execute(
        "SELECT 1 FROM raw_response_archive_all WHERE raw_response_hash = ?",
        [raw_response_hash],
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        """INSERT INTO raw_response_archive
           (raw_response_hash, source, fetch_time, payload, api_version,
            integrity_verified)
           VALUES (?, ?, ?, ?, ?, TRUE)""",
        [raw_response_hash, source, fetch_time, payload, api_version],
    )
    return True





def _is_process_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否仍在运行（Windows 兼容）"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # SYNCHRONIZE access
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


class DuckDBWriteLockError(Exception):
    """DuckDB 写锁获取失败"""


class DuckDBStore:
    """DuckDB 连接管理器

    - read_query(): open-per-query 读操作，无需锁
    - write_transaction(): 获取写锁后执行写操作
    """

    def __init__(self, *, paths: DatabasePathSet) -> None:
        validated = paths.validate()
        self._path_set = validated
        self._db_path = validated.duckdb_path
        self._lock_path = self._db_path.parent / ".duckdb.write.lock"
        self._memory_limit = self._load_memory_limit()

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._revalidate()

    @staticmethod
    def _load_memory_limit() -> str:
        """Read database.duckdb_memory_limit with a conservative fallback."""
        try:
            import importlib
            config_module = importlib.import_module("app.core.config")
            database_cfg = config_module.Config.current().get_value("database", {})
            value = database_cfg.get("duckdb_memory_limit") if isinstance(database_cfg, dict) else None
            if value:
                return str(value)
        except Exception:
            pass
        return "14GB"

    def _connection_config(self) -> dict[str, str]:
        return {"memory_limit": self._memory_limit}

    def _revalidate(self) -> None:
        validated = self._path_set.validate()
        if validated.duckdb_path != self._db_path:
            raise PathIsolationError("DuckDB path identity changed after validation")

    @contextlib.contextmanager
    def read_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """打开一个只读查询连接，用完即关。

        open-per-query 模式：DuckDB 嵌入式打开开销 ~1ms，
        不持有长期连接，避免阻塞 CLI 写操作。

        竞态（reports/76 P1-1）：Windows 上写线程持事务期间，另一线程
        connect 可能短暂失败 "file already open"（同进程亦可能，DuckDB
        平台特性）。固定 5×0.5s 重试在批量写事务（数秒级）前必然耗尽。
        此处改为指数退避（0.25s 起，3s 封顶，共 12 次，约 28s 窗口），
        写锁活跃时另加 2s 基础等待，骑跨批量写事务而非让调用方 500。
        """
        self._revalidate()
        # 2026-08-14 红队 P3：库文件不存在是永久性条件（首次启动的
        # 最小初始化会先建库），不进入竞态重试；立即失败让调用方
        # 决定建库路径，避免 read_only 打开缺失文件空耗 ~28s 退避窗口。
        if not self._db_path.exists():
            raise FileNotFoundError(f"database file not found: {self._db_path}")
        last_error: Exception | None = None
        lock_active = False
        try:
            from app.core.storage.update_lock import update_lock_active

            lock_active = update_lock_active(self._db_path)
        except Exception:
            lock_active = False
        attempts = 12
        allow_same_process_rw = False
        for attempt in range(attempts):
            try:
                # 2026-08-14 红队 P2-2：读连接显式 read_only，读进程不参与
                # 文件锁竞争（DuckDB 允许单写者 + 多只读并发）。
                # 例外：本进程已持有读写连接时 DuckDB 禁止同文件不同配置
                # （"different configuration"）——文件锁本就由本进程持有，
                # 回退同配置读写连接（同进程多连接共享锁，安全）。
                conn = duckdb.connect(
                    str(self._db_path),
                    read_only=not allow_same_process_rw,
                    config=self._connection_config(),
                )
            except duckdb.ConnectionException as error:
                last_error = error
                if "different configuration" in str(error):
                    allow_same_process_rw = True
            except Exception as error:
                last_error = error
            else:
                try:
                    yield conn
                finally:
                    conn.close()
                return
            base_delay = 0.25 * (2 ** min(attempt, 4))
            delay = min(base_delay, 3.0)
            if lock_active and attempt == 0:
                delay += 2.0
            time.sleep(delay)
        assert last_error is not None
        raise last_error

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        """执行单条只读查询，返回字典列表。"""
        with self.read_connection() as conn:
            statements = conn.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("read_query accepts exactly one SELECT statement")
            cursor = conn.execute(sql, params or [])
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    @contextlib.contextmanager
    def _write_lock(self, timeout: float = 30.0) -> Iterator[None]:
        """获取应用级写锁文件（Windows 兼容），超时则抛出异常

        使用 O_CREAT | O_EXCL 原子性创建锁文件。如果文件已存在，
        检查持有者 PID 是否仍存活——若已退出则接管锁，否则等待。

        A dead PID is reclaimed, but an apparently live lock is never expired by
        age: long-running writes must retain serialization. The owner token
        prevents a former holder from removing a lock acquired after replacement.
        """
        self._revalidate()
        assert_writes_allowed(self._db_path)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout

        while True:
            try:
                # 原子性创建：文件已存在则抛 FileExistsError
                fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o644,
                )
                try:
                    token = secrets.token_urlsafe(24)
                    os.write(fd, f"pid={os.getpid()}\ntime={time.time()}\ntoken={token}\n".encode())
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                # 锁文件已存在，检查持有者是否还活着
                try:
                    content = self._lock_path.read_text()
                    lines = content.strip().split("\n")
                    holder_pid = int(lines[0].split("=")[1])
                    if not _is_process_alive(holder_pid):
                        # 持有者已退出，清理僵尸锁并重试
                        self._lock_path.unlink(missing_ok=True)
                        continue
                except (ValueError, IndexError, OSError):
                    # 锁文件格式损坏：可能是"刚创建、尚未写入"的获取竞态
                    # （2026-08-14 红队 P3-9）。新鲜损坏锁视为获取进行中，
                    # 等待重试；超过宽限期才清理，避免双写。
                    try:
                        age = time.time() - self._lock_path.stat().st_mtime
                    except OSError:
                        age = float("inf")
                    if age > _STALE_MALFORMED_LOCK_SECONDS:
                        self._lock_path.unlink(missing_ok=True)
                        continue

                if time.monotonic() > deadline:
                    raise DuckDBWriteLockError(
                        f"无法获取 DuckDB 写锁，可能正在执行其他写操作。"
                        f"请稍后重试或检查 {self._lock_path}"
                    ) from None
                time.sleep(0.5)

        try:
            yield
        finally:
            try:
                # Never let an old holder remove a replacement lock.
                if f"token={token}" in self._lock_path.read_text(encoding="utf-8"):
                    self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @contextlib.contextmanager
    def write_connection(self, timeout: float = 30.0) -> Iterator[duckdb.DuckDBPyConnection]:
        """获取写锁后打开写连接，用完关闭并释放锁。

        2026-08-14 红队 P2-3：锁已到手后 connect 仍可能瞬时失败
        （残留句柄/文件系统抖动），与读侧对齐做有界重试再抛出。
        """
        with self._write_lock(timeout):
            self._revalidate()
            conn = self._connect_writer()
            try:
                yield conn
            finally:
                conn.close()

    def _connect_writer(self) -> duckdb.DuckDBPyConnection:
        """Open a read-write connection with bounded retry (caller holds the write lock).

        同进程 Web 读线程在写锁建立前可能已持有 read_only 连接；Windows
        DuckDB 此时报 "different configuration"，写连接需要等读连接关闭。
        旧窗口约 10s，全市场价格更新时曾让 000534 写入失败；延长到约
        90s，覆盖长查询/慢请求，失败后再交给上层 retry。
        """
        last_error: Exception | None = None
        delays = (0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0)
        for attempt in range(len(delays) + 1):
            try:
                return duckdb.connect(
                    str(self._db_path), config=self._connection_config(),
                )
            except Exception as error:
                last_error = error
                if attempt < len(delays):
                    time.sleep(delays[attempt])
        assert last_error is not None
        raise last_error

    @contextlib.contextmanager
    def transaction(self, timeout: float = 30.0) -> Iterator[duckdb.DuckDBPyConnection]:
        """Run all statements on one connection and roll back unless commit succeeds."""
        with self._write_lock(timeout):
            self._revalidate()
            conn = self._connect_writer()
            committed = False
            try:
                # 2026-08-14 红队 P3：begin() 移入 try，失败时连接由
                # finally 的 rollback/close 兜底，不再依赖 GC。
                conn.begin()
                yield conn
                conn.commit()
                committed = True
            finally:
                if not committed:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                conn.close()

    def write_query(self, sql: str, params: list[Any] | None = None) -> None:
        """执行写操作（自动获取写锁）"""
        with self.write_connection() as conn:
            conn.execute(sql, params or [])

    def execute_script(self, script: str) -> None:
        """执行多语句 SQL 脚本（用于 schema 初始化）"""
        with self.write_connection() as conn:
            conn.execute(script)

    @property
    def db_path(self) -> Path:
        return self._db_path
