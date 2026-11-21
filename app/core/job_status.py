"""Truthful aggregation for multi-step job outcomes."""

from __future__ import annotations

from typing import Any

__all__ = ["aggregate_job_status", "merge_step_status"]

# 2026-11-12 契约 §2：严重度阶梯。数值越大越严重，用于"最坏步骤"归并。
# rejected 位于顶端：一次被门禁拒绝的步骤绝不能被后续 success 覆盖。
_STATUS_SEVERITY: dict[str, int] = {
    "rejected": 5,
    "failed": 4,
    "error": 4,
    "partial": 3,
    "success": 2,
    "ok": 2,
    "skipped": 1,
}
# 未登记的状态码按 partial 处理（保守：不认识的情况不允许显示为成功）。
_UNKNOWN_STATUS_RANK = _STATUS_SEVERITY["partial"]


def _step_status(step: dict[str, Any] | None) -> str:
    """Normalize one step's status without inventing a success."""
    raw = step.get("status") if isinstance(step, dict) else step
    text = str(raw).strip().lower() if raw is not None else ""
    return text or "partial"


def _status_rank(status: str) -> int:
    return _STATUS_SEVERITY.get(status, _UNKNOWN_STATUS_RANK)


def aggregate_job_status(steps: dict[str, Any]) -> str:
    """Derive a parent status without hiding failed or incomplete steps."""
    statuses = [
        str(step.get("status", "partial")) if isinstance(step, dict) else str(step)
        for step in steps.values()
    ]
    # `deferred` = 本轮预算/冷却用完、顺延到下一轮继续（有界续传），**不是失败**。
    # 2026-11-21 实测：cashflow_supplement / balance_sheet_ext / deducted_profit /
    # fixed_assets 常态 `deferred`（`all_pending_in_cooldown`），旧聚合把它们当
    # "非 success" → 整轮永远 partial → 真失败被淹没。按 `skipped` 同级处理，
    # deferred 的数量仍在各自 step 的 details 里如实可查。
    executed = [
        status for status in statuses if status not in {"skipped", "deferred"}
    ]
    if not executed or all(status in {"success", "ok"} for status in executed):
        return "success"
    if all(status in {"failed", "error"} for status in executed):
        return "failed"
    return "partial"


def merge_step_status(
    *steps: dict[str, Any] | None,
    reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Merge child step results into one truthful parent step (contract §2).

    2026-11-12 P0 修复：此前 update.py 用
    ``{**后一个子步骤, "full_recompute": 前一个}`` 覆盖外层 status，
    于是"国债利差刷新成功"抹掉了"指标快照被门禁 rejected"的事实。
    这里按严重度取最坏值，``rejected`` 永远赢，再也不会被 ``success`` 掩盖。

    Args:
        *steps: 已执行的子步骤结果（dict；允许 ``None`` / 纯状态字符串）。
            ``status`` 缺失按 ``partial`` 处理。
        reason: 兜底 reason；只有在最坏步骤自身没有 reason 时才使用。
        **extra: 追加到返回值的字段（如 ``full_recompute``）。

    Returns:
        ``{"status": 最坏状态, "reason": 最坏步骤的 reason, "sub_steps": [...], **extra}``

    严重度：``rejected > failed/error > partial > success/ok > skipped``；
    ``skipped``（未执行）不参与最坏值计算，全部 skipped 时结果为 ``skipped``。
    """
    executed: list[dict[str, Any]] = []
    for step in steps:
        if step is None:
            continue
        normalized = dict(step) if isinstance(step, dict) else {"status": str(step)}
        if _step_status(normalized) == "skipped":
            continue
        normalized.setdefault("status", "partial")
        executed.append(normalized)

    if not executed:
        return {"status": "skipped", "reason": reason, "sub_steps": [], **extra}

    worst = executed[0]
    for step in executed[1:]:
        if _status_rank(_step_status(step)) > _status_rank(_step_status(worst)):
            worst = step

    worst_status = _step_status(worst)
    worst_reason = reason
    for step in executed:
        if _step_status(step) != worst_status:
            continue
        candidate = step.get("reason")
        if candidate:
            worst_reason = candidate
            break

    merged: dict[str, Any] = {
        "status": worst_status,
        "reason": worst_reason,
        "sub_steps": executed,
    }
    merged.update(extra)
    return merged
