"""自动更新控制器（PRD §7.3, §7.7, §16.1）

启动后自动执行增量更新，追赶上次运行之后缺失的数据。
- 生命周期状态：idle / running / paused / disabled / finished / failed
- 状态持久化到 SQLite（auto_update_state 表），供网页只读展示
- CLI 控制：enable / disable / run / pause / resume / status
- 与手动更新共用 IncrementalUpdater，同一时刻至多一个更新在运行
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

STATE_TABLE = "auto_update_state"

# "enabled"/"disabled" 是持久化 state 列的实际取值（enable/disable 命令写入），
# 必须纳入合法状态集，否则 _load_persisted_state 会拒绝加载已持久化的状态。
VALID_STATES = {"idle", "running", "paused", "disabled", "enabled", "finished", "failed"}


class AutoUpdateController:
    """自动更新生命周期控制器（线程安全）。

    - 默认 enabled=True；disabled 时不自动触发，也不更新状态表
    - running 时其他触发请求被拒绝（单写者串行）
    - paused 时暂停推进；resume 后继续
    - P0-3修复: 构造时加载持久化状态（SQLite 为跨进程唯一真相源），
      新控制器（CLI/Web 各自实例）不会把用户已 disable/pause 的
      状态重置回 enabled；仅当从未持久化过时才使用构造参数默认值。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        enabled: bool = True,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("AutoUpdateController requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("AutoUpdateController stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        self._lock = threading.Lock()
        self._state: str = "enabled" if enabled else "disabled"
        self._paused = False
        self._current_stage: str = "idle"
        self._progress: dict[str, Any] = {}
        self._last_error: str | None = None
        self._last_success_at: str | None = None
        self._ensure_state_table()
        self._load_persisted_state()

    # ─── 状态持久化 ──────────────────────────────────────────────

    def _ensure_state_table(self) -> None:
        with self.sqlite.transaction() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT,
                    progress_json TEXT,
                    last_error TEXT,
                    last_success_at TEXT,
                    updated_at TEXT
                )"""
            )

    def _load_persisted_state(self) -> None:
        """Adopt the last persisted lifecycle state so a fresh controller in
        another process (CLI or web) never resurrects an explicitly disabled
        or paused update.

        A stale `running` marker from a crashed process is reset to idle so
        run_once can start a new cycle; terminal stages are kept for display.
        """
        rows = self.sqlite.query(f"SELECT * FROM {STATE_TABLE} WHERE id = 1")
        if not rows:
            return
        row = rows[0]
        state = row.get("state")
        if state not in VALID_STATES:
            return
        self._state = state
        self._paused = bool(row.get("paused"))
        stage = row.get("current_stage") or "idle"
        self._current_stage = "idle" if stage == "running" else stage
        try:
            self._progress = json.loads(row.get("progress_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            self._progress = {}
        self._last_error = row.get("last_error")
        self._last_success_at = row.get("last_success_at")

    def _persist(self) -> None:
        with self.sqlite.transaction() as conn:
            conn.execute(
                f"""INSERT INTO {STATE_TABLE}
                    (id, state, paused, current_stage, progress_json, last_error, last_success_at, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      state=excluded.state, paused=excluded.paused,
                      current_stage=excluded.current_stage,
                      progress_json=excluded.progress_json,
                      last_error=excluded.last_error,
                      last_success_at=excluded.last_success_at,
                      updated_at=excluded.updated_at""",
                [
                    self._state, int(self._paused), self._current_stage,
                    json.dumps(self._progress, ensure_ascii=False, default=str),
                    self._last_error, self._last_success_at,
                    datetime.now(timezone.utc).isoformat(),
                ],
            )

    # ─── 状态查询 ────────────────────────────────────────────────

    def _status_unlocked(self) -> dict[str, Any]:
        """构造状态字典（调用方必须已持有 self._lock）。"""
        return {
            "state": "paused" if self._paused and self._state == "enabled" else self._state,
            "enabled": self._state == "enabled",
            "paused": self._paused,
            "current_stage": self._current_stage,
            "progress": dict(self._progress),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
        }

    def status(self) -> dict[str, Any]:
        """返回当前自动更新状态（网页只读展示用）。"""
        with self._lock:
            return self._status_unlocked()

    def persisted_status(self) -> dict[str, Any]:
        """从 SQLite 读取最近一次持久化状态（供跨进程/启动恢复）。"""
        rows = self.sqlite.query(f"SELECT * FROM {STATE_TABLE} WHERE id = 1")
        if not rows:
            return self.status()
        row = rows[0]
        state = row.get("state")
        paused = bool(row.get("paused"))
        return {
            "state": "paused" if paused and state == "enabled" else state,
            "enabled": state != "disabled",
            "paused": paused,
            "current_stage": row.get("current_stage"),
            "progress": json.loads(row.get("progress_json") or "{}"),
            "last_error": row.get("last_error"),
            "last_success_at": row.get("last_success_at"),
            "updated_at": row.get("updated_at"),
        }

    # ─── 控制命令 ────────────────────────────────────────────────

    def enable(self) -> dict[str, Any]:
        """开启自动更新（默认行为）。"""
        with self._lock:
            self._state = "enabled"
            self._paused = False
            self._persist()
            return self._status_unlocked()

    def disable(self) -> dict[str, Any]:
        """关闭自动更新（完全手动模式）。"""
        with self._lock:
            self._state = "disabled"
            self._paused = False
            self._current_stage = "idle"
            self._persist()
            return self._status_unlocked()

    def pause(self) -> dict[str, Any]:
        """暂停自动更新推进。"""
        with self._lock:
            if self._state != "enabled":
                return {"error": "auto update is disabled"}
            self._paused = True
            self._persist()
            return self._status_unlocked()

    def resume(self) -> dict[str, Any]:
        """继续自动更新推进。"""
        with self._lock:
            self._paused = False
            self._persist()
            return self._status_unlocked()

    # ─── 执行 ────────────────────────────────────────────────────

    def run_once(self, *, max_stocks: int = 0) -> dict[str, Any]:
        """立即执行一次增量更新（手动触发或启动后自动触发）。

        返回更新报告；同时更新持久化状态。
        P1修复: 每个步骤完成后即持久化当前阶段/进度（含 job_id 与
        started_at），页面在抓取过程中可见阶段与进度，而非只有
        starting/done 两个终态。
        """
        with self._lock:
            if self._state != "enabled":
                return {"status": "skipped", "reason": "auto_update_disabled"}
            if self._paused:
                return {"status": "skipped", "reason": "auto_update_paused"}
            if self._current_stage == "running":
                return {"status": "skipped", "reason": "already_running"}
            job_id = str(uuid.uuid4())
            started_at = datetime.now(timezone.utc).isoformat()
            self._current_stage = "running"
            self._progress = {
                "phase": "starting",
                "job_id": job_id,
                "started_at": started_at,
                "steps": {},
            }
            self._last_error = None
            self._persist()

        from app.core.update import IncrementalUpdater

        def progress(step_name: str, step: dict[str, Any]) -> None:
            with self._lock:
                self._progress = {
                    "phase": f"step:{step_name}",
                    "job_id": job_id,
                    "started_at": started_at,
                    "steps": {
                        **self._progress.get("steps", {}),
                        step_name: step.get("status"),
                    },
                }
                self._persist()

        try:
            updater = IncrementalUpdater(duck=self.duck, sqlite=self.sqlite)
            report = updater.run_incremental_update(max_stocks=max_stocks, progress_cb=progress)

            with self._lock:
                self._current_stage = (
                    "finished" if report.get("status") == "success" else "failed"
                )
                self._progress = {
                    "phase": "done",
                    "job_id": job_id,
                    "started_at": started_at,
                    "status": report.get("status"),
                    "steps": {k: v.get("status") for k, v in report.get("steps", {}).items()},
                }
                if report.get("status") == "success":
                    self._last_success_at = datetime.now(timezone.utc).isoformat()
                    self._last_error = None
                else:
                    self._last_error = f"update status: {report.get('status')}"
                self._persist()
            return report
        except Exception as error:
            logger.exception("自动更新失败")
            with self._lock:
                self._current_stage = "failed"
                self._last_error = str(error)
                self._persist()
            return {"status": "failed", "error": str(error)}
