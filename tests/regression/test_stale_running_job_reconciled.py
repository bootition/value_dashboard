"""持锁开跑前必须结算"僵尸 running 行"（**防御性加固**，非已发生故障）。

## 真实情况（2026-11-24 更正）
我最初把 `job_logs` 里的 **UTC 时间**当成本地时间，误判"job 299 挂了 8 小时"。
实际那是本地 `13:45:57 → 13:46:55` 的一轮**正常更新（58 秒）**。

## 仍然值得堵的窗口（可能性，不是已知故障）
更新子进程若被强杀/休眠中断，`job_logs` 会留下 `status='running'` 的行；
启动维护里的结算 `_reconcile_crashed_incremental_jobs(only_if_owner_dead=True)`
在"此刻已有新子进程持锁"时会**正确跳过**（它要防的是误杀活作业）。
两条合起来有个小窗口：新子进程先抢到锁 → 启动结算跳过 → 脏行留到下次启动。

## 为什么可以在持锁后无条件结算
`_run_incremental_update_locked()` 已经在**写锁**内。DuckDB 是跨进程独占的
（单写者 + 排他文件锁），所以宿主机上不可能有另一个活着的更新进程。
因此此刻库里任何 `running` 的增量作业都**必然是死的**。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.update import IncrementalUpdater


def _insert_running_job(sqlite) -> int:
    """按真实表结构播种一条 8 小时前的 `running` 行（job_logs 没有 job_id 列）。"""
    started = datetime(2026, 11, 24, 5, 45, 57, tzinfo=UTC).isoformat()
    sqlite.execute(
        "INSERT INTO job_logs (job_type, status, started_at, details_json) "
        "VALUES ('incremental_update', 'running', ?, '{}')",
        [started],
    )
    return int(sqlite.query(
        "SELECT id FROM job_logs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    )[0]["id"])


def test_stale_running_job_is_reconciled_when_lock_held(duckdb_store, sqlite_store) -> None:
    row_id = _insert_running_job(sqlite_store)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    # 持锁语义由调用方保证；这里直接验证结算函数本身
    reconciled = updater._reconcile_crashed_incremental_jobs()
    assert reconciled == 1, f"僵尸 running 行必须被结算，实际 {reconciled}"

    row = sqlite_store.query(
        "SELECT status, finished_at, details_json FROM job_logs WHERE id = ?", [row_id]
    )[0]
    assert row["status"] == "failed", row
    assert row["finished_at"], row
    assert "dead_update_lock_owner" in (row["details_json"] or ""), row


def test_reconcile_is_noop_when_nothing_is_running(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._reconcile_crashed_incremental_jobs() == 0


def test_reconcile_does_not_touch_finished_jobs(duckdb_store, sqlite_store) -> None:
    """只许动 `running` 的行：已完成的作业绝不能被改成 failed。"""
    sqlite_store.execute(
        "INSERT INTO job_logs (job_type, status, started_at, finished_at, details_json) "
        "VALUES ('incremental_update', 'success', ?, ?, '{}')",
        ["2026-11-24T05:00:00+00:00", "2026-11-24T05:02:00+00:00"],
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._reconcile_crashed_incremental_jobs() == 0
    row = sqlite_store.query(
        "SELECT status FROM job_logs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
    )[0]
    assert row["status"] == "success"


def test_new_update_run_reconciles_stale_row_before_creating_its_own(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """**调用点**验证：`_run_incremental_update_locked` 开跑前就把僵尸行结算掉。

    只看结算函数本身是不够的 —— 变异掉"调用"那一行，函数测试照样全绿。
    这里把更新主体整个换成桩，直接跑 `_run_incremental_update_locked`，
    断言"僵尸行在**新作业创建之前**已经变成 failed"。
    """
    stale_id = _insert_running_job(sqlite_store)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    seen_running_at_flow_start: list[int] = []

    def fake_flow(max_stocks, progress_cb, job_id, started_at, detail_cb=None):
        # 进入主体时，僵尸行必须已经不在 running 了
        seen_running_at_flow_start.append(
            len(sqlite_store.query(
                "SELECT id FROM job_logs WHERE status = 'running'"
            )),
        )
        return {"status": "skipped", "reason": "test_stub", "steps": {}}

    monkeypatch.setattr(updater, "_run_incremental_update_flow", fake_flow)
    report = updater._run_incremental_update_locked(0, None)

    assert report["status"] == "skipped"
    assert seen_running_at_flow_start == [1], (
        "进入主体时 running 的应该**只有本轮自己新建的那一条** ——"
        f"实际 {seen_running_at_flow_start}（>1 说明僵尸行没被结算）"
    )
    stale = sqlite_store.query("SELECT status FROM job_logs WHERE id = ?", [stale_id])[0]
    assert stale["status"] == "failed", stale
    own = sqlite_store.query(
        "SELECT status FROM job_logs ORDER BY id DESC LIMIT 1"
    )[0]
    assert own["status"] == "skipped", own
