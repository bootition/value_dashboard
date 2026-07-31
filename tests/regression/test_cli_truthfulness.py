from __future__ import annotations

from app.cli.protocol import make_response


def test_cli_preserves_partial_job_status() -> None:
    response = make_response(
        "data.update",
        {"status": "partial", "steps": {"prices": {"status": "partial"}}},
    )

    assert response["result"]["status"] == "partial"


def test_cli_maps_failed_job_status_to_error() -> None:
    response = make_response("data.update", {"status": "failed"})

    assert response["result"]["status"] == "error"


def test_cli_maps_blocked_job_status_to_error() -> None:
    response = make_response("data.update", {"status": "blocked"})

    assert response["result"]["status"] == "error"


def test_cli_maps_unhealthy_diagnosis_to_error() -> None:
    response = make_response("data.diagnose", {"healthy": False})

    assert response["result"]["status"] == "error"


def test_cli_keeps_operational_warnings_non_blocking() -> None:
    response = make_response(
        "data.diagnose",
        {"healthy": True, "operational_warnings": ["UNPUBLISHED_OVERRIDES"]},
    )

    assert response["result"]["status"] == "ok"


def test_cli_detects_nested_partial_status() -> None:
    response = make_response(
        "data.update",
        {"steps": {"prices": {"status": "success"}, "retries": {"status": "partial"}}},
    )

    assert response["result"]["status"] == "partial"


def test_cli_schema_declares_all_runtime_statuses_and_plan_executors() -> None:
    from app.cli.protocol import get_capabilities, get_schema

    assert "partial" in get_schema()["response_format"]["result"]["status"]
    assert "clean_execute" in get_capabilities()["commands"]["archive"]
    assert "refetch_execute" in get_capabilities()["commands"]["data"]
