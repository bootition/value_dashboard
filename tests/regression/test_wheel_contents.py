from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path


def test_pep517_wheel_contains_runtime_resources(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    dist = tmp_path / "dist"

    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(dist), ".",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("value_dashboard-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    assert "app/core/dsl/grammar.lark" in names
    assert "app/resources/config/default.yaml" in names
    assert "app/web/static/index.html" in names
    assert any(name.startswith("app/web/static/assets/") for name in names)
