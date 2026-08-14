"""跨进程增量更新互斥（单写者串行，PRD §7.3 计划 Phase D）。

CLI（vd data update）与 Web 启动后台线程分属不同进程，各自的
AutoUpdateController._lock 只是实例锁，不能阻止两个更新交错写入
SQLite/网络抓取。本模块提供基于锁文件 + PID 属主的跨进程互斥：
- 持锁写入 pid + 时间戳 + 进程创建时间；另一进程检测到活着的属主即拒绝（skipped）
- 属主进程已死（崩溃）时自动回收锁（与 maintenance.py 同策略）
- PID 复用防护：属主进程创建时间与锁内记录不符即视为死锁（2026-08-14 红队 P3-9）
- 空/半写锁文件（创建与写入之间的窗口）按年龄宽限处理，避免误判活锁
- 同一进程内重复获取直接通过（线程安全由上层实例锁保证）
"""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import os
import time
from pathlib import Path
from typing import Callable, Iterator

_held = contextvars.ContextVar("value_dashboard_update_lock_held", default=False)

# 空/半写锁文件宽限期：正常情况下锁文件在创建后微秒级写入内容；
# 超过该时长仍为空/损坏视为死锁（写进程已崩溃在写入前）。
_STALE_MALFORMED_LOCK_SECONDS = 5.0


class UpdateLockError(RuntimeError):
    """Raised when another live process owns the incremental update."""


def _lock_path(database_path: Path) -> Path:
    return database_path.parent / ".value-dashboard.update.lock"


def _pid_creation_time(pid: int) -> float | None:
    """Process creation time (Unix epoch seconds), None when unavailable."""
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                creation = ctypes.c_ulonglong()
                exit_t = ctypes.c_ulonglong()
                kernel_t = ctypes.c_ulonglong()
                user_t = ctypes.c_ulonglong()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_t),
                    ctypes.byref(kernel_t),
                    ctypes.byref(user_t),
                ):
                    return None
                # FILETIME → Unix epoch seconds
                return (creation.value - 116444736000000000) / 10_000_000
            finally:
                kernel32.CloseHandle(handle)
        stat = os.stat(f"/proc/{pid}")
        return stat.st_mtime  # close enough: process dir mtime = spawn time
    except Exception:
        return None


def _pid_exists(pid: int) -> bool:
    """Check if a PID is still alive (cross-platform)."""
    if hasattr(ctypes, "windll"):
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parse_lock(lock_path: Path) -> dict[str, str] | None:
    """Parse a well-formed lock file into {pid, time, created}; None if malformed."""
    try:
        text = lock_path.read_text(encoding="ascii")
    except OSError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields if fields.get("pid") else None


def _lock_file_age(lock_path: Path) -> float:
    try:
        return time.time() - lock_path.stat().st_mtime
    except OSError:
        return float("inf")


def _owner_is_dead(lock_path: Path) -> bool:
    """Return true only for a lock whose recorded owner is provably dead.

    - Malformed (empty/half-written) lock: fresh files are treated as a live
      acquisition-in-progress (not dead); stale ones (>grace period) are dead.
    - Well-formed lock: dead if the PID is gone, or if the process at that PID
      was created after the lock was recorded (PID reuse).
    """
    fields = _parse_lock(lock_path)
    if fields is None:
        return _lock_file_age(lock_path) > _STALE_MALFORMED_LOCK_SECONDS
    try:
        pid = int(fields["pid"])
    except ValueError:
        return _lock_file_age(lock_path) > _STALE_MALFORMED_LOCK_SECONDS
    if not _pid_exists(pid):
        return True
    recorded_created = fields.get("created")
    if recorded_created is not None:
        try:
            recorded_value = float(recorded_created)
        except ValueError:
            recorded_value = None
        if recorded_value is not None:
            current_created = _pid_creation_time(pid)
            # 创建时间偏差 > 1s 即认为 PID 被复用（Windows 时钟精度足够）
            if current_created is not None and abs(current_created - recorded_value) > 1.0:
                return True
    return False


def update_lock_active(database_path: Path) -> bool:
    """True when a live process holds the incremental-update write lock.

    Cheap check (single stat + read) used by read paths to decide whether to
    serve stale-cached results / skip expensive consistency scans while the
    auto-update writer holds the DuckDB file (reports/76 P1).
    """
    lock_path = _lock_path(database_path)
    if not lock_path.exists():
        return False
    return not _owner_is_dead(lock_path)


def duckdb_write_lock_active(database_path: Path) -> bool:
    """True when the DuckDB application-level write lock is held by a live owner.

    2026-08-14 红队 P2-1：维护/回填/发布类 CLI 写操作持有
    `.duckdb.write.lock`（duckdb_store._write_lock），并不持有
    `.value-dashboard.update.lock`；此前只查后者会把真实写窗口误判为空闲，
    导致 stale 缓存降级与筛选快照口径标注失效。
    """
    lock_path = database_path.parent / ".duckdb.write.lock"
    if not lock_path.exists():
        return False
    return not _owner_is_dead(lock_path)


def any_write_lock_active(database_path: Path) -> bool:
    """Unified write-window predicate: auto-update lock OR DuckDB write lock."""
    return update_lock_active(database_path) or duckdb_write_lock_active(database_path)


@contextlib.contextmanager
def exclusive_update(
    database_path: Path,
    *,
    on_stale_lock: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Reserve the incremental-update cycle for one process.

    Raises UpdateLockError immediately when another live process holds the
    lock; a dead owner's lock is reclaimed so a crash never blocks recovery.
    """
    if _held.get():
        yield
        return
    lock_path = _lock_path(database_path)
    reclaimed_stale_lock = False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if _owner_is_dead(lock_path):
            lock_path.unlink(missing_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            reclaimed_stale_lock = True
        else:
            raise UpdateLockError("another incremental update is running")
    try:
        created = _pid_creation_time(os.getpid())
        created_line = f"created={created:.3f}\n" if created is not None else ""
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"pid={os.getpid()}\ntime={time.time()}\n{created_line}")
        if reclaimed_stale_lock and on_stale_lock is not None:
            on_stale_lock()
        token = _held.set(True)
        try:
            yield
        finally:
            _held.reset(token)
    finally:
        lock_path.unlink(missing_ok=True)
