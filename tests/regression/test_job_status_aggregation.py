from __future__ import annotations

from app.core.job_status import aggregate_job_status


def test_parent_is_success_when_all_steps_succeed_or_skip() -> None:
    steps = {
        "prices": {"status": "success"},
        "optional": {"status": "skipped"},
    }

    assert aggregate_job_status(steps) == "success"


def test_parent_is_failed_when_every_executed_step_fails() -> None:
    steps = {
        "prices": {"status": "failed"},
        "optional": {"status": "skipped"},
    }

    assert aggregate_job_status(steps) == "failed"


def test_parent_is_partial_when_success_and_failure_are_mixed() -> None:
    steps = {
        "prices": {"status": "success"},
        "financials": {"status": "failed"},
    }

    assert aggregate_job_status(steps) == "partial"


def test_parent_is_partial_when_a_step_is_missing() -> None:
    steps = {
        "stock_universe": {"status": "success"},
        "sw_industry": {"status": "missing"},
    }

    assert aggregate_job_status(steps) == "partial"
