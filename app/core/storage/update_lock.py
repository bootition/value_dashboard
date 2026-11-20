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
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

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


def update_lock_owned_by_current_process(database_path: Path) -> bool:
    """True when *this* process is the recorded owner of the update lock.

    读路径用它区分两种情况：外部写进程持锁（应立即降级/503）与本进程自己
    的写事务和并发读连接争用文件锁（应退避重试）。2026-10-20 实测：更新
    子进程内并发的财务刷新会让只读连接回退为读写连接，撞上 Web 进程的
    只读连接，Windows 报同一句 "Cannot open file"，被误判为外部锁后立即
    失败，导致"最新报告期"读不到（返回 None）。
    """
    fields = _parse_lock(_lock_path(database_path))
    if not fields:
        return False
    try:
        return int(fields["pid"]) == os.getpid()
    except ValueError:
        return False


def reclaim_update_lock(database_path: Path, *, owner_pid: int | None = None) -> bool:
    """Force-remove a zombie update lock; True only when this call removed it.

    Two modes:

    * ``owner_pid is None`` — 常规回收：只删"属主已死"的锁（PID 不在、PID 复用、
      或超龄的损坏锁），活锁一律不动。
    * ``owner_pid=<pid>`` — **证据模式**：调用方刚刚 `Popen.wait()` 到这个进程
      退出（它就是我们启动的更新子进程）。这时它写下的锁**必然是僵尸**，不再
      依赖 `_pid_exists`/创建时间这类会骗人的探测：Windows 上句柄未关、PID 被
      复用、OpenProcess 权限失败都会让"已死"看起来"还活着"。
      若锁已被别的进程换主（记录的 pid ≠ owner_pid），仍然拒绝回收——绝不抢活锁。

    为什么需要这条路径（2026-11-20 reports/152）：子进程原生崩溃/被
    `TerminateProcess` 后，父进程 `recover_interrupted_auto_update` 先看
    `any_write_lock_active`，一旦探测把死属主看成"活着"就直接返回 False，
    于是僵尸锁 + `running` 行要留到下一次**服务重启**才被结算 —— job_logs
    上就是连续多轮 `dead_update_lock_owner`（症状，不是原因）。
    """
    lock_path = _lock_path(database_path)
    if not lock_path.exists():
        return False
    recorded: int | None = None
    fields = _parse_lock(lock_path)
    if fields is not None:
        with contextlib.suppress(ValueError):
            recorded = int(fields["pid"])
    if owner_pid is None:
        if not _owner_is_dead(lock_path):
            return False
    elif recorded is not None and recorded != owner_pid:
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    return True


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


def _lease_path(database_path: Path) -> Path:
    """租约文件路径（**纯路径计算，不是上下文管理器**）。

    2026-11-19 修：这里曾被误加 `@contextlib.contextmanager`，于是
    `_lease_path(x)` 返回的是 `_GeneratorContextManager` 而不是 Path，
    下游 `lease.parent.mkdir(...)` 直接 `AttributeError` ——
    而它被 `exclusive_update` 调用，导致**每一个** `with exclusive_update(...)`
    的调用方全部崩掉（CLI 的 `data auto-update run` / 各类重建命令 / ROCE 重建）。
    同一次提交里 `exclusive_update` 本身又**漏了**同一装饰器（纯生成器被当上下文管理器用），
    两个错误互相掩盖：修掉其中一个才会暴露另一个。
    """
    return database_path.parent / ".locks" / "db-write.lease.json"


def _write_session_lease(database_path: Path) -> bool:
    """把当前进程登记成「正式库写租约」持有者（供其他会话看到）。

    与 `session_lease.py` 共用同一个文件格式；这里只写不抢占 —— 能走到这一步
    说明单写者锁已经拿到了。写失败（目录不可写等）只返回 False，不影响更新。
    """
    try:
        import json
        import socket
        from datetime import datetime, timedelta

        lease = _lease_path(database_path)
        lease.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        payload = {
            "owner": "auto-update",
            "purpose": os.environ.get("VD_LEASE_PURPOSE", "自动更新（data auto-update run）"),
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": now.isoformat(),
            # 自动更新可能跑很久；给 2 小时，宁宽勿窄（它随时可被 PID 判定回收）
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "ttl_seconds": 7200,
            "command": " ".join(sys.argv)[:400],
        }
        lease.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def _clear_session_lease(database_path: Path) -> None:
    """更新结束：删掉自己写的租约（只删 owner=auto-update 且 pid 是自己的）。"""
    try:
        import json

        lease = _lease_path(database_path)
        if not lease.exists():
            return
        payload = json.loads(lease.read_text(encoding="utf-8"))
        if payload.get("owner") == "auto-update" and int(payload.get("pid") or 0) == os.getpid():
            lease.unlink(missing_ok=True)
    except (OSError, ValueError):
        return


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
            raise UpdateLockError("another incremental update is running") from None
    try:
        created = _pid_creation_time(os.getpid())
        created_line = f"created={created:.3f}\n" if created is not None else ""
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"pid={os.getpid()}\ntime={time.time()}\n{created_line}")
        if reclaimed_stale_lock and on_stale_lock is not None:
            on_stale_lock()
        # 2026-11-16（reports/147）：单写者锁同时写一份**跨会话可见的租约**。
        # 为什么：`exclusive_update` 只让「同机进程之间」互斥，别的 AI 会话
        # 看不到「现在有人在写库」——它们只会看到 `session_preflight` 说 PASS，
        # 然后 start 自己的一轮写入。租约文件让 `session_lease.py status` 与
        # preflight 都能看见「auto-update 正在写库」。
        lease_written = _write_session_lease(database_path)
        token = _held.set(True)
        try:
            yield
        finally:
            _held.reset(token)
            if lease_written:
                _clear_session_lease(database_path)
    finally:
        lock_path.unlink(missing_ok=True)
