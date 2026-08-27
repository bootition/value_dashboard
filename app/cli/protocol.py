"""JSON 协议处理 (PRD §16.1-16.3)

所有 CLI 命令通过此协议输出，OpenCode 通过同一协议调用。
- schema_version: 主版本兼容管理
- 错误输出格式与原因格式稳定
- 危险操作两段式确认: plan_id + 15分钟有效期
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.storage.sqlite_store import SQLiteStore

SCHEMA_VERSION = "1.0"
PLAN_EXPIRY_MINUTES = 15  # PRD §16.3 CL11


# 稳定错误码
ERROR_CODES: dict[str, str] = {
    "E001": "invalid_arguments",
    "E002": "expression_not_found",
    "E003": "validation_failed",
    "E004": "database_error",
    "E005": "adapter_unavailable",
    "E101": "plan_not_found",
    "E102": "plan_expired",
    "E103": "plan_already_executed",
    "E104": "plan_already_consumed",
    "E201": "backup_failed",
    "E202": "restore_failed",
    "E301": "screening_rule_not_found",
    "E401": "override_not_found",
}

# 稳定原因码
REASON_CODES: dict[str, str] = {
    "R001": "division_by_zero",
    "R002": "field_missing",
    "R003": "insufficient_history",
    "R004": "dimension_mismatch",
    "R005": "circular_dependency",
    "R006": "source_unavailable",
    "R007": "data_not_initialized",
    "R008": "rate_limited",
}


def make_response(
    command: str,
    data: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """构建标准 JSON 协议响应 (PRD §16.1 CL3)"""
    status = "error" if error_code is not None else _derive_result_status(data)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "result": {
            "status": status,
            "data": data,
            "error_code": error_code,
            "error_message": error_message,
            "reason_code": reason_code,
        },
    }


def _derive_result_status(data: Any) -> str:
    """Map domain-level outcomes to the stable CLI protocol status."""
    if not isinstance(data, dict):
        return "ok"
    if data.get("error"):
        return "error"
    if data.get("healthy") is False:
        return "error"

    domain_status = data.get("status")
    if domain_status in {"failed", "error", "blocked"}:
        return "error"
    if domain_status in {"partial", "missing"}:
        return "partial"

    steps = data.get("steps")
    if not isinstance(steps, dict):
        return "ok"
    child_statuses = [_derive_result_status(step) for step in steps.values()]
    if "error" in child_statuses:
        return "error"
    if "partial" in child_statuses:
        return "partial"
    return "ok"


# ─── 两段式确认 (PRD §16.3 CL10-11) ────────────────────────────────

# 危险操作列表
DANGEROUS_OPERATIONS: set[str] = {
    "backup.restore",
    "archive.clean",
    "data.refetch",  # 大范围重抓
    "data.reconcile_jobs",
    "data.quarantine_legacy_records",
}


def is_dangerous(operation: str) -> bool:
    """判断操作是否需要两段式确认"""
    return operation in DANGEROUS_OPERATIONS


def create_plan(
    operation: str,
    plan_summary: dict[str, Any],
    *,
    sqlite: SQLiteStore,
) -> dict[str, Any]:
    """创建危险操作计划 (第一步)

    PRD §16.3 CL10: 第一步返回计划摘要与 plan_id
    PRD §16.3 CL11: plan_id 有效期固定 15 分钟
    """
    plan_id = str(uuid.uuid4())[:12]
    expires_at = datetime.now(UTC) + timedelta(minutes=PLAN_EXPIRY_MINUTES)

    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO plans (plan_id, operation, plan_summary, expires_at, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            [plan_id, operation, json.dumps(plan_summary, ensure_ascii=False, default=str),
             expires_at.isoformat()],
        )

    return make_response(
        command="plan.create",
        data={
            "plan_id": plan_id,
            "plan_summary": plan_summary,
            "expires_at": expires_at.isoformat(),
            "instruction": f"请在 {PLAN_EXPIRY_MINUTES} 分钟内执行: vd plan confirm {plan_id}",
        },
    )


def confirm_plan(plan_id: str, *, sqlite: SQLiteStore) -> dict[str, Any]:
    """确认危险操作 (第二步)

    PRD §16.3 CL11: 在有效期内基于 plan_id 确认执行
    """
    now = datetime.now(UTC)
    with sqlite.transaction() as conn:
        plan = conn.execute("SELECT * FROM plans WHERE plan_id = ?", [plan_id]).fetchone()
        if plan is None:
            return make_response(
                command="plan.confirm",
                error_code="E101",
                error_message=f"plan_id {plan_id} 不存在",
            )
        if plan["status"] != "pending":
            return make_response(
                command="plan.confirm",
                error_code="E103",
                error_message=f"plan_id {plan_id} is no longer pending",
            )
        expires_at = datetime.fromisoformat(plan["expires_at"])
        if now > expires_at:
            conn.execute("UPDATE plans SET status = 'expired' WHERE plan_id = ?", [plan_id])
            return make_response(
                command="plan.confirm",
                error_code="E102",
                error_message=f"plan_id {plan_id} 已过期 (有效期 {PLAN_EXPIRY_MINUTES} 分钟)",
            )
        cursor = conn.execute(
            """UPDATE plans SET status = 'executed', confirmed_at = ?
               WHERE plan_id = ? AND status = 'pending'""",
            [now.isoformat(), plan_id],
        )
        if cursor.rowcount != 1:
            return make_response(
                command="plan.confirm",
                error_code="E103",
                error_message=f"plan_id {plan_id} is no longer pending",
            )

    plan_summary = json.loads(plan["plan_summary"]) if plan["plan_summary"] else {}
    return make_response(
        command="plan.confirm",
        data={
            "plan_id": plan_id,
            "operation": plan["operation"],
            "plan_summary": plan_summary,
            "status": "confirmed",
        },
    )


def consume_confirmed_plan(
    operation: str,
    *,
    plan_id: str,
    sqlite: SQLiteStore,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Atomically consume one confirmed plan and return its immutable summary.

    A successful confirmation authorizes exactly one execution attempt. The
    conditional update occurs in the same SQLite transaction as the summary
    read, preventing concurrent callers from replaying an irreversible plan.
    """
    now = datetime.now(UTC).isoformat()
    with sqlite.transaction() as conn:
        row = conn.execute(
            """SELECT plan_summary FROM plans
               WHERE plan_id = ? AND operation = ? AND status = 'executed'
               AND expires_at > ?""",
            [plan_id, operation, now],
        ).fetchone()
        if row is None:
            return make_response(
                command=operation,
                error_code="E101",
                error_message=f"操作 {operation} 需要确认有效的 plan_id (PRD §16.3 CL10)",
            ), None
        cursor = conn.execute(
            """UPDATE plans SET status = 'consumed'
               WHERE plan_id = ? AND operation = ? AND status = 'executed'
               AND expires_at > ?""",
            [plan_id, operation, now],
        )
        if cursor.rowcount != 1:
            return make_response(
                command=operation,
                error_code="E104",
                error_message=f"plan_id {plan_id} was already consumed",
            ), None
    return None, json.loads(row["plan_summary"]) if row["plan_summary"] else {}


def get_capabilities() -> dict[str, Any]:
    """获取 CLI 能力清单 (PRD §16.2 CL6)"""
    return {
        "schema_version": SCHEMA_VERSION,
        "commands": {
            "discover": ["schema", "capabilities", "examples", "fields", "indicators", "functions", "reason_codes"],
            "indicator": ["create", "validate", "preview_single", "preview_sample", "publish", "list", "discover"],
            "screening": ["create", "run", "save_result", "export_csv", "add_to_watchlist", "list"],
            "data": ["init", "refresh_universe", "update", "replenish_missing_core_data", "status", "compute_indicators", "diagnose", "switch_source", "refetch", "refetch_execute", "reconcile_jobs", "reconcile_jobs_execute", "quarantine_legacy_records", "quarantine_legacy_records_execute", "download_pdf", "list_pdfs", "archive_pdfs", "restore_pdf", "backfill_prices", "auto-update"],
            "override": ["list_conflicts", "submit", "revoke", "submit_template", "validate_template", "preview_template", "publish_template", "list_templates"],
            "backup": ["create", "restore", "restore_execute", "list", "store_credential", "retrieve_credential"],
            "archive": ["create", "verify", "clean", "clean_execute"],
            "plan": ["confirm"],
        },
        "dangerous_operations": list(DANGEROUS_OPERATIONS),
        "plan_expiry_minutes": PLAN_EXPIRY_MINUTES,
    }


def get_schema() -> dict[str, Any]:
    """获取 JSON schema (PRD §16.2 CL6)"""
    return {
        "schema_version": SCHEMA_VERSION,
        "response_format": {
            "schema_version": "string (e.g. '1.0')",
            "command": "string",
            "result": {
                "status": "'ok' | 'partial' | 'missing' | 'error'",
                "data": "any (command-specific)",
                "error_code": "string | null (e.g. 'E001')",
                "error_message": "string | null",
                "reason_code": "string | null (e.g. 'R001')",
            },
        },
        "error_codes": ERROR_CODES,
        "reason_codes": REASON_CODES,
    }


def get_examples() -> list[dict[str, Any]]:
    """获取示例 (PRD §16.2 CL6)"""
    return [
        {
            "command": "vd discover capabilities",
            "description": "获取能力清单",
            "response": make_response("discover.capabilities", data={"commands": {}}),
        },
        {
            "command": "vd indicator create my_indicator 'income.revenue@TTM / balance.total_assets' --desc '资产周转率'",
            "description": "创建复合指标草稿",
            "response": make_response("indicator.create", data={"name": "my_indicator", "version": 1, "status": "draft"}),
        },
        {
            "command": "vd screening run --rule '{\"conditions\":{\"logic\":\"AND\",\"rules\":[{\"field\":\"pe_ttm\",\"op\":\">\",\"value\":0}]}}'",
            "description": "运行筛选",
            "response": make_response("screening.run", data={"total": 100, "execution_time_ms": 45.0}),
        },
        {
            "command": "vd backup create",
            "description": "创建备份",
            "response": make_response("backup.create", data={"backup_id": "bk_001", "type": "full"}),
        },
        {
            "command": "vd plan confirm abc123",
            "description": "确认危险操作",
            "response": make_response("plan.confirm", data={"plan_id": "abc123", "status": "confirmed"}),
        },
    ]
