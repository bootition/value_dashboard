"""Truthful aggregation for multi-step job outcomes."""

from __future__ import annotations

from typing import Any


def aggregate_job_status(steps: dict[str, Any]) -> str:
    """Derive a parent status without hiding failed or incomplete steps."""
    statuses = [
        str(step.get("status", "partial")) if isinstance(step, dict) else str(step)
        for step in steps.values()
    ]
    executed = [status for status in statuses if status != "skipped"]
    if not executed or all(status in {"success", "ok"} for status in executed):
        return "success"
    if all(status in {"failed", "error"} for status in executed):
        return "failed"
    return "partial"
