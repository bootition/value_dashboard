"""Cross-process exclusion for destructive database maintenance."""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from pathlib import Path
from typing import Iterator


class MaintenanceLockError(RuntimeError):
    """Raised when a write is attempted while restore owns the profile."""


_held = contextvars.ContextVar("value_dashboard_maintenance_held", default=False)



def _lock_path(database_path: Path) -> Path:
    return database_path.parent / ".value-dashboard.maintenance.lock"


def assert_writes_allowed(database_path: Path) -> None:
    """Fail closed for another process while a destructive restore is active.

    A dead maintenance owner is not enough to make a profile writable: its
    restore journal must first be recovered by the restore path.
    """
    if _held.get():
        return
    if _restore_journal_exists(database_path):
        raise MaintenanceLockError(
            "interrupted restore journal exists; complete restore recovery before writes"
        )
    lock_path = _lock_path(database_path)
    if not lock_path.exists():
        return
    try:
        text = lock_path.read_text(encoding="ascii")
        pid_line = text.splitlines()[0] if text else ""
        if pid_line.startswith("pid="):
            pid = int(pid_line.split("=", 1)[1])
            if not _pid_exists(pid):
                raise MaintenanceLockError(
                    "abandoned database maintenance requires restore recovery before writes"
                )
        raise MaintenanceLockError("database maintenance is active; retry after it completes")
    except (OSError, ValueError, IndexError):
        raise MaintenanceLockError("database maintenance is active; retry after it completes")


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


@contextlib.contextmanager
def exclusive_maintenance(
    database_path: Path,
    *,
    reclaim_abandoned: bool = False,
) -> Iterator[None]:
    """Reserve a profile for restore and let only this context issue writes.

    Never infer staleness from elapsed time: a large PDF restore can legitimately
    run for hours. Recovery may reclaim a dead owner only after validating the
    restore journal and rolling the profile back.
    """
    lock_path = _lock_path(database_path)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        if reclaim_abandoned and _lock_owner_is_dead(lock_path):
            lock_path.unlink(missing_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        else:
            assert_writes_allowed(database_path)
            raise MaintenanceLockError("database maintenance is already active") from error
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


def _lock_owner_is_dead(lock_path: Path) -> bool:
    """Return true only for a well-formed lock whose recorded PID is dead."""
    try:
        text = lock_path.read_text(encoding="ascii")
        pid_line = text.splitlines()[0] if text else ""
        if not pid_line.startswith("pid="):
            return False
        return not _pid_exists(int(pid_line.split("=", 1)[1]))
    except (OSError, ValueError, IndexError):
        return False


def _restore_journal_exists(database_path: Path) -> bool:
    """Support the formal and isolated-test data-root layouts."""
    parent = database_path.parent
    return any(
        path.is_file()
        for path in (
            parent / ".restore-journal.json",
            parent / "data" / ".restore-journal.json",
        )
    )
