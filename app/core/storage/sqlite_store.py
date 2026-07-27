"""SQLite 连接管理器 - WAL 模式，支持跨进程多读单写

SQLite WAL 模式天然支持跨进程并发，无需应用级锁协调。
CLI 和 Web 可同时访问，写操作自动排队。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.core.storage.path_policy import DatabasePathSet, PathIsolationError


class SQLiteStore:
    """SQLite 操作库连接管理器"""

    def __init__(self, *, paths: DatabasePathSet) -> None:
        validated = paths.validate()
        self._path_set = validated
        self._db_path = validated.sqlite_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._revalidate()

        self._init_wal()

    def _revalidate(self) -> None:
        validated = self._path_set.validate()
        if validated.sqlite_path != self._db_path:
            raise PathIsolationError("SQLite path identity changed after validation")

    def _init_wal(self) -> None:
        """初始化 WAL 模式和基本 PRAGMA"""
        conn = self._raw_connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        finally:
            conn.close()

    def _raw_connect(self) -> sqlite3.Connection:
        self._revalidate()
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """获取一个 SQLite 连接，用完即关"""
        conn = self._raw_connect()
        # P2修复: 每连接设置PRAGMA（SQLite的PRAGMA是per-connection的）
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """获取一个事务连接，提交或回滚"""
        conn = self._raw_connect()
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
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
