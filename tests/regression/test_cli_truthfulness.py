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


def test_cli_maps_unhealthy_diagnosis_to_error() -> None:
    response = make_response("data.diagnose", {"healthy": False})

    assert response["result"]["status"] == "error"


def test_cli_detects_nested_partial_status() -> None:
    response = make_response(
        "data.update",
        {"steps": {"prices": {"status": "success"}, "retries": {"status": "partial"}}},
    )

    assert response["result"]["status"] == "partial"
