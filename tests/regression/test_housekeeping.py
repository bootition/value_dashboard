"""C8/C16(报告41): 操作型表有界清理（过期 plan / 旧 job_logs / 已解析 missing）。"""

from __future__ import annotations

from app.core.housekeeping import gc_operational_tables
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed_sqlite import SeedSqlite, sql_now


def test_gc_clears_expired_pending_plans(
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as conn:
        # 留（第 6 类·库侧相对时间）：`plans` 的 `created_at`/`expires_at` 必须相对**库侧**
        conn.execute(
            """INSERT INTO plans (plan_id, operation, plan_summary, created_at, expires_at, status)
               VALUES ('old-plan', 'op', '{}', datetime('now', '-50 days'), datetime('now', '-30 days'), 'pending'),
                      ('live-plan', 'op', '{}', datetime('now'), datetime('now', '+1 hour'), 'pending')"""
        )
    result = gc_operational_tables(sqlite_store)
    assert result["expired_plans"] == 1
    remaining = sqlite_store.query("SELECT plan_id FROM plans")[0]["plan_id"]
    assert remaining == "live-plan"


def test_gc_keeps_old_confirmed_plan_and_running_job(
    sqlite_store: SQLiteStore,
) -> None:
    """已确认的 plan 与 running job 不被清理（审计保留）。"""
    with sqlite_store.transaction() as conn:
        # 留（第 6 类·库侧相对时间）：同上——400 天前建、370 天前过期，判据是库侧时钟；
        conn.execute(
            """INSERT INTO plans (plan_id, operation, plan_summary, created_at, expires_at, status)
               VALUES ('confirmed-old', 'op', '{}', datetime('now', '-400 days'), datetime('now', '-370 days'), 'executed')"""
        )
        # 留（第 6 类·库侧相对时间）：`job_logs.started_at` 用 `datetime('now','-1 hour')`/
        conn.execute(
            """INSERT INTO job_logs (job_type, status, started_at, finished_at)
               VALUES ('update', 'running', datetime('now', '-1 hour'), NULL),
                      ('update', 'success', datetime('now', '-200 days'), datetime('now', '-200 days'))"""
        )
    result = gc_operational_tables(sqlite_store)
    assert result["expired_plans"] == 0
    # 旧终态 job（-200 天）应被清理；running 永不清理
    assert result["old_job_logs"] == 1
    remaining_jobs = sqlite_store.query(
        "SELECT status FROM job_logs WHERE started_at >= datetime('now', '-100 days')"
    )
    assert [row["status"] for row in remaining_jobs] == ["running"]


def test_gc_clears_old_resolved_missing(
    sqlite_store: SQLiteStore,
) -> None:
    # 原来卡在三个缺口上：写在调用点事务里（A）、`missing_list` 无 SQLite 方法（B）、
    # `resolved_at` 要"库侧 200 天前"（C 的相对哨兵）。
    with sqlite_store.transaction() as conn:
        seed_sqlite = SeedSqlite(sqlite_store).join(conn)
        seed_sqlite.missing_list(
            "000001", "pe_ttm", "source_incomplete", resolved_at=sql_now("-200 days")
        )
        seed_sqlite.missing_list("000002", "pe_ttm", "source_incomplete")
    result = gc_operational_tables(sqlite_store)
    assert result["resolved_missing"] == 1
    remaining = sqlite_store.query("SELECT COUNT(*) AS cnt FROM missing_list")[0]["cnt"]
    assert remaining == 1