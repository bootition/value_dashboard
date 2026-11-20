"""更新锁僵尸回收 + 悬挂作业结算的判定（reports/152，2026-11-20）。

事故：自动更新子进程连续多轮"没跑完"，`job_logs` 上却是
`dead_update_lock_owner`。原因是**症状不是原因**：

* 子进程原生崩溃 / 被 TerminateProcess / 控制台 Ctrl+C 杀掉后，锁文件留在
  `data/.value-dashboard.update.lock`，属主 PID 已死；
* 父进程 `recover_interrupted_auto_update` 只靠 `_pid_exists`/创建时间猜死活，
  一旦探测把死属主看成"活着"就直接放弃结算 —— 僵尸锁 + `running` 行要等到
  下一次**服务重启**才被结算，于是下一轮更新把上一轮写成 `dead_update_lock_owner`。

本文件钉死三个方向（用户要求）：
1. 崩溃后不留僵尸锁：父进程 wait 到子进程退出的 pid 是**证据**，直接回收，
   哪怕 `_pid_exists` 说谎；
2. 死 PID 可回收：`exclusive_update` 能接管死属主的锁并触发结算回调；
3. 活 PID 不被抢：活属主的锁既不会被 `exclusive_update` 偷，也不会被
   `reclaim_update_lock` 按错误的 owner_pid 删掉。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

import pytest

from app.core.storage import update_lock as update_lock_module
from app.core.storage.update_lock import (
    UpdateLockError,
    _lock_path,
    exclusive_update,
    reclaim_update_lock,
    update_lock_active,
)
from app.core.update import IncrementalUpdater

DEAD_PID = 2**31 - 1  # Windows/Unix 都不可能存在的 PID


def _write_lock(db_path: Path, pid: int, *, created: float | None = None) -> None:
    created_line = f"created={created:.3f}\n" if created is not None else ""
    _lock_path(db_path).write_text(
        f"pid={pid}\ntime=1795174183.67\n{created_line}", encoding="ascii",
    )


def test_crash_leaves_no_zombie_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """崩溃后不留僵尸锁：父进程 wait 到的 pid 足以证明锁已死。"""
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")
    child_pid = 4444
    _write_lock(db_path, child_pid)

    # 最坏情况：探测把"已 reaped 的子进程"报成活着（Windows 句柄/PID 复用）。
    monkeypatch.setattr(update_lock_module, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(update_lock_module, "_pid_creation_time", lambda pid: 1795174180.0)
    assert update_lock_active(db_path) is True, "前提：无证据时探测会误判为活锁"

    assert reclaim_update_lock(db_path, owner_pid=child_pid) is True
    assert _lock_path(db_path).exists() is False
    assert update_lock_active(db_path) is False


def test_crashed_code_path_settles_running_job_right_away(tmp_path: Path) -> None:
    """父进程回收僵尸锁之后，本轮就能结算 running 行，不必等下一次重启。"""
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")
    _write_lock(db_path, DEAD_PID)

    assert reclaim_update_lock(db_path, owner_pid=DEAD_PID) is True
    assert not _lock_path(db_path).exists()


def test_dead_pid_lock_is_reclaimed_and_callback_fires(tmp_path: Path) -> None:
    """死 PID 可回收：exclusive_update 接管死锁并通知调用方结算悬挂作业。"""
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")
    _write_lock(db_path, DEAD_PID)
    assert update_lock_active(db_path) is False

    calls: list[str] = []
    with exclusive_update(db_path, on_stale_lock=lambda: calls.append("stale")):
        assert update_lock_active(db_path) is True
    assert calls == ["stale"], "回收了死锁就必须让调用方去结算悬挂作业"
    assert not _lock_path(db_path).exists(), "正常退出后也不许留锁"


def test_live_owner_lock_is_not_stolen(tmp_path: Path) -> None:
    """活 PID 不被抢：exclusive_update 拒绝，reclaim 按别的 pid 也拒绝。"""
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")
    _write_lock(db_path, os.getpid())

    with pytest.raises(UpdateLockError), exclusive_update(db_path):
        pass

    # 换个自称的 owner_pid（例如父进程误传了自己/别的子进程）也不许删活锁。
    assert reclaim_update_lock(db_path, owner_pid=os.getpid() + 1) is False
    assert _lock_path(db_path).exists() is True
    # 无证据模式的常规回收同样不许动活锁。
    assert reclaim_update_lock(db_path) is False
    assert _lock_path(db_path).exists() is True


class _StubDuck:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path


class _StubSQLite:
    """只实现 `_reconcile_crashed_incremental_jobs` 用到的两个方法。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def query(self, sql: str, params: object = None) -> list[dict]:
        cursor = self._connection.execute(sql, params or [])
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    @contextlib.contextmanager
    def transaction(self):
        yield self._connection
        self._connection.commit()


def _job_logs_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE job_logs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               job_type TEXT, status TEXT, started_at TEXT, finished_at TEXT,
               details_json TEXT)"""
    )
    connection.execute(
        """INSERT INTO job_logs (job_type, status, started_at, details_json)
           VALUES ('incremental_update', 'running', '2026-11-20T11:29:43+00:00',
                   '{"job_id": "x", "max_stocks": 0}')"""
    )
    connection.commit()
    return connection


def test_running_job_is_not_settled_while_owner_is_alive(tmp_path: Path) -> None:
    """服务重启时子进程可能还活着：绝不许把它写成 dead_update_lock_owner。"""
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")
    _write_lock(db_path, os.getpid())  # 活属主
    connection = _job_logs_connection()
    stub = type(
        "Stub", (), {"duck": _StubDuck(db_path), "sqlite": _StubSQLite(connection)}
    )()

    settled = IncrementalUpdater._reconcile_crashed_incremental_jobs(
        stub, only_if_owner_dead=True
    )

    assert settled == 0
    status = connection.execute("SELECT status FROM job_logs").fetchone()[0]
    assert status == "running", "活着的更新被结算 = 自造假失败"


def test_running_job_is_settled_when_owner_is_dead(tmp_path: Path) -> None:
    """属主确实死了：结算成 failed(dead_update_lock_owner) 并返回条数。"""
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")
    _write_lock(db_path, DEAD_PID)
    connection = _job_logs_connection()
    stub = type(
        "Stub", (), {"duck": _StubDuck(db_path), "sqlite": _StubSQLite(connection)}
    )()

    settled = IncrementalUpdater._reconcile_crashed_incremental_jobs(
        stub, only_if_owner_dead=True
    )

    assert settled == 1
    row = connection.execute("SELECT status, details_json FROM job_logs").fetchone()
    assert row[0] == "failed"
    assert "dead_update_lock_owner" in row[1]
