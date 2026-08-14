"""SQLite 连接管理器 - WAL 写入与无副作用只读查询

SQLite WAL 模式天然支持跨进程并发，无需应用级锁协调。
CLI 和 Web 可同时访问，写操作自动排队。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.storage.maintenance import assert_writes_allowed
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError


class SQLiteStore:
    """SQLite 操作库连接管理器"""

    def __init__(self, *, paths: DatabasePathSet) -> None:
        validated = paths.validate()
        self._path_set = validated
        self._db_path = validated.sqlite_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._revalidate()

    def _revalidate(self) -> None:
        validated = self._path_set.validate()
        if validated.sqlite_path != self._db_path:
            raise PathIsolationError("SQLite path identity changed after validation")

    def _write_connect(self) -> sqlite3.Connection:
        self._revalidate()
        assert_writes_allowed(self._db_path)
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _read_only_connect(self) -> sqlite3.Connection:
        """Open a consistent SQLite read connection that cannot execute writes."""
        self._revalidate()
        uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only SQLite connection for queries."""
        conn = self._read_only_connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """获取一个事务连接，提交或回滚"""
        conn = self._write_connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: list[Any] | None = None) -> int:
        """执行单条语句，返回受影响行数"""
        with self.transaction() as conn:
            cursor = conn.execute(sql, params or [])
            return cursor.rowcount

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        """执行查询，返回字典列表"""
        with self.connection() as conn:
            cursor = conn.execute(sql, params or [])
            return [dict(row) for row in cursor.fetchall()]

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def paths(self) -> DatabasePathSet:
        """Return the validated profile that owns this store."""
        return self._path_set
