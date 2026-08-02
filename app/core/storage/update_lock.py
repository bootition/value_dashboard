"""跨进程增量更新互斥（单写者串行，PRD §7.3 计划 Phase D）。

CLI（vd data update）与 Web 启动后台线程分属不同进程，各自的
AutoUpdateController._lock 只是实例锁，不能阻止两个更新交错写入
SQLite/网络抓取。本模块提供基于锁文件 + PID 属主的跨进程互斥：
- 持锁写入 pid + 时间戳；另一进程检测到活着的属主即拒绝（skipped）
- 属主进程已死（崩溃）时自动回收锁（与 maintenance.py 同策略）
- 同一进程内重复获取直接通过（线程安全由上层实例锁保证）
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from pathlib import Path
from typing import Iterator

_held = contextvars.ContextVar("value_dashboard_update_lock_held", default=False)


class UpdateLockError(RuntimeError):
    """Raised when another live process owns the incremental update."""


def _lock_path(database_path: Path) -> Path:
    return database_path.parent / ".value-dashboard.update.lock"


def _pid_exists(pid: int) -> bool:
    """Check if a PID is still alive (cross-platform)."""
    import ctypes
    if hasattr(ctypes, "windll"):
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _owner_is_dead(lock_path: Path) -> bool:
    """Return true only for a well-formed lock whose recorded PID is dead."""
    try:
        text = lock_path.read_text(encoding="ascii")
        pid_line = text.splitlines()[0] if text else ""
        if not pid_line.startswith("pid="):
            return False
        return not _pid_exists(int(pid_line.split("=", 1)[1]))
    except (OSError, ValueError, IndexError):
        return False


@contextlib.contextmanager
def exclusive_update(database_path: Path) -> Iterator[None]:
    """Reserve the incremental-update cycle for one process.

    Raises UpdateLockError immediately when another live process holds the
    lock; a dead owner's lock is reclaimed so a crash never blocks recovery.
    """
    if _held.get():
        yield
        return
    lock_path = _lock_path(database_path)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if _owner_is_dead(lock_path):
            lock_path.unlink(missing_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        else:
            raise UpdateLockError("another incremental update is running")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"pid={os.getpid()}\ntime={time.time()}\n")
        token = _held.set(True)
        try:
            yield
        finally:
            _held.reset(token)
    finally:
        lock_path.unlink(missing_ok=True)
