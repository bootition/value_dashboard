from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.cli.protocol import confirm_plan, create_plan


def test_stale_job_reconciliation_requires_a_confirmed_plan(sqlite_store) -> None:
    result = create_plan(
        "data.reconcile_jobs",
        {"job_ids": [1], "count": 1},
        sqlite=sqlite_store,
    )

    assert result["result"]["data"]["plan_id"]
    confirmed = confirm_plan(result["result"]["data"]["plan_id"], sqlite=sqlite_store)
    assert confirmed["result"]["data"]["operation"] == "data.reconcile_jobs"


def test_reconciliation_plan_records_stale_job_ids(sqlite_store) -> None:
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    sqlite_store.execute(
        "INSERT INTO job_logs (job_type, status, started_at) VALUES (?, 'running', ?)",
        ["full_init", old],
    )
    jobs = sqlite_store.query(
        "SELECT id FROM job_logs WHERE status = 'running' AND started_at < ?", [datetime.now(UTC).isoformat()]
    )

    plan = create_plan("data.reconcile_jobs", {"job_ids": [jobs[0]["id"]], "count": 1}, sqlite=sqlite_store)

    assert plan["result"]["data"]["plan_summary"]["job_ids"] == [jobs[0]["id"]]
