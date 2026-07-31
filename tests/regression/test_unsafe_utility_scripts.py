from __future__ import annotations

from pathlib import Path


def test_no_script_bypasses_the_indicator_calculator_with_direct_snapshot_writes() -> None:
    script = Path(__file__).parents[2] / "scripts" / "compute_indicators.py"
    assert not script.exists()
