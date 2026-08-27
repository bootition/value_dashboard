"""C8/C16(报告41): 操作型表有界清理（过期 plan / 旧 job_logs / 已解析 missing）。"""

from __future__ import annotations

from app.core.housekeeping import gc_operational_tables
from app.core.storage.sqlite_store import SQLiteStore


def test_gc_clears_expired_pending_plans(
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as conn:
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
        conn.execute(
            """INSERT INTO plans (plan_id, operation, plan_summary, created_at, expires_at, status)
               VALUES ('confirmed-old', 'op', '{}', datetime('now', '-400 days'), datetime('now', '-370 days'), 'executed')"""
        )
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
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO missing_list (stock_code, field_name, reason_code, resolved_at)
               VALUES ('000001', 'pe_ttm', 'source_incomplete', datetime('now', '-200 days')),
                      ('000002', 'pe_ttm', 'source_incomplete', NULL)"""
        )
    result = gc_operational_tables(sqlite_store)
    assert result["resolved_missing"] == 1
    remaining = sqlite_store.query("SELECT COUNT(*) AS cnt FROM missing_list")[0]["cnt"]
    assert remaining == 1