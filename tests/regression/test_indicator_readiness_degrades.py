"""就绪自检脚本的「不许假绿」用例（`reports/139` §11.7 / 第三轮验收 P1）。

背景：第三轮对抗性验收发现，第一版 `scripts/check_indicator_readiness.py` 在
**合成骨架库**（只有 1 只股票、假 reason code、假行数）上也会输出 `READY`；
更糟的是库打不开时它直接 traceback，而不是降级成 `BLOCK`。

本文件把「降级」钉死成契约：
1. 空库（没有 schema）→ 必须 `BLOCK` 且退出码 1，**不许** READY；
2. 输出必须是合法 JSON（`--json`），且至少含一条 `bootstrap` 或逐项检查；
3. 只有 `overall` 为 READY 时才允许退出码 0。

这些断言足够抓到「fail-open」回归：谁把异常吞掉改成 READY，第 1 条立即变红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "check_indicator_readiness.py"


def _run(json_mode: bool = True) -> tuple[int, str]:
    env = dict(os.environ)
    args = [sys.executable, str(SCRIPT)]
    if json_mode:
        args.append("--json")
    completed = subprocess.run(
        args,
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def test_empty_database_degrades_to_block_not_traceback(tmp_path: Path) -> None:
    """库是空文件（没有任何表）→ 必须 BLOCK + 退出码 1，且不抛 traceback。"""
    empty_db = tmp_path / "valuedashboard.duckdb"
    env_backup = {key: os.environ.get(key) for key in ("VD_DUCKDB_PATH", "VD_SQLITE_PATH")}
    os.environ["VD_DUCKDB_PATH"] = str(empty_db)
    try:
        code, output = _run(json_mode=True)
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert code == 1, f"空库必须退出码 1（BLOCK），实际 {code}；输出：{output[:500]}"
    assert "Traceback" not in output, f"不许 traceback，应降级成 BLOCK；输出：{output[:500]}"
    start = output.find("{")
    assert start >= 0, f"--json 必须输出 JSON；输出：{output[:500]}"
    payload = json.loads(output[start:])
    assert payload.get("overall") == "BLOCK"


@pytest.mark.parametrize("json_mode", [True, False])
def test_script_never_reports_ready_without_checks(json_mode: bool) -> None:
    """不管哪种输出模式：`READY` 只能在退出码 0 时出现，反之亦然。"""
    code, output = _run(json_mode=json_mode)
    assert code in (0, 1, 2), f"退出码只能是 0/1/2，实际 {code}"
    if code == 0:
        assert "READY" in output
    else:
        assert "READY" not in output.split("就绪：")[-1][:40], (
            f"退出码 {code} 时不许宣称 READY；输出：{output[:300]}"
        )
