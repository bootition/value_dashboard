"""`deferred` 不是失败（2026-11-21 线上：整轮永远 partial）。

事故现场（job 260/261/262）：`cashflow_supplement` / `balance_sheet_ext` /
`deducted_profit` / `fixed_assets` 常态 `deferred`（`all_pending_in_cooldown`，
即本轮预算/冷却用完顺延到下一轮），`etf_prices` 20/20 全成功但 `deferred=14`。
旧实现把这两类都算成"非 success"→ 整轮永远 `partial` → 真失败被淹没。
"""

from __future__ import annotations

import pytest

from app.core.job_status import aggregate_job_status


@pytest.mark.parametrize(
    "steps,expected",
    [
        # deferred 不算失败
        ({"prices": {"status": "success"}, "cashflow": {"status": "deferred"}}, "success"),
        ({"a": {"status": "deferred"}, "b": {"status": "deferred"}}, "success"),
        # skipped 照旧不算失败
        ({"a": {"status": "skipped"}}, "success"),
        # 真失败仍然是失败
        ({"a": {"status": "failed"}}, "failed"),
        ({"a": {"status": "failed"}, "b": {"status": "deferred"}}, "failed"),
        # 混合（真失败 + 成功）→ partial
        ({"a": {"status": "failed"}, "b": {"status": "success"}}, "partial"),
        # partial 仍是 partial（不能因为加了 deferred 就掩盖）
        ({"a": {"status": "partial"}}, "partial"),
        ({"a": {"status": "partial"}, "b": {"status": "deferred"}}, "partial"),
    ],
)
def test_deferred_is_not_a_failure(steps, expected) -> None:
    assert aggregate_job_status(steps) == expected
