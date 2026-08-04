"""有界清理（C8/C16，报告41）。

对操作型表的过期记录做保守、可幂等的清理：
- plans：清除已过期且仍为 pending（从未确认）的确认计划
- job_logs：清除 180 天前的终态作业记录（running 永不清理）
- missing_list：清除已解析（resolved_at 非空）90 天前的记录

已确认/已执行的 plan 及其审计记录不在本清理范围（保留审计所需）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

JOB_LOG_RETENTION_DAYS = 180
MISSING_RESOLVED_RETENTION_DAYS = 90


def gc_operational_tables(sqlite: Any) -> dict[str, int]:
    """有界清理越界记录；返回各表删除条数。"""
    with sqlite.transaction() as conn:
        expired_plans = conn.execute(
            """DELETE FROM plans
               WHERE status = 'pending' AND expires_at < datetime('now')""",
        ).rowcount

        old_jobs = conn.execute(
            """DELETE FROM job_logs
               WHERE status != 'running' AND finished_at IS NOT NULL
                 AND finished_at < datetime('now', ?)""",
            [f"-{JOB_LOG_RETENTION_DAYS} days"],
        ).rowcount

        resolved_missing = conn.execute(
            """DELETE FROM missing_list
               WHERE resolved_at IS NOT NULL
                 AND resolved_at < datetime('now', ?)""",
            [f"-{MISSING_RESOLVED_RETENTION_DAYS} days"],
        ).rowcount

    total = expired_plans + old_jobs + resolved_missing
    if total:
        logger.info(
            "运维清理: 过期plan=%d job_logs=%d 已解析missing=%d",
            expired_plans, old_jobs, resolved_missing,
        )
    return {
        "expired_plans": expired_plans,
        "old_job_logs": old_jobs,
        "resolved_missing": resolved_missing,
    }