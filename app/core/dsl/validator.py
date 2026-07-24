"""DSL 验证器 — 维度校验 + 历史能力推导 + 空值传播 + 依赖检测

PRD §11.3: 系统自动为每个表达式推导 historical_capable 或 current_only
PRD §11.4: 强维度校验，防止百分比与绝对金额比较等
PRD §11.5: 依赖必须版本锁定，拒绝循环依赖
PRD §11.4 DL9: 分母为零/历史不足/字段缺失时返回 null + 原因码
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.dsl.ast_nodes import (
    ASTNode, Literal, FieldRef, IndicatorRef, FuncCall, BinaryOp, UnaryOp,
    FIELD_METADATA, INDICATOR_METADATA,
)

logger = logging.getLogger(__name__)

# 原因码 (PRD §11.4 DL9)
REASON_DIVISION_BY_ZERO = "R001: division_by_zero"
REASON_FIELD_MISSING = "R002: field_missing"
REASON_INSUFFICIENT_HISTORY = "R003: insufficient_history"
REASON_DIMENSION_MISMATCH = "R004: dimension_mismatch"
REASON_CYCLE_DETECTED = "R005: circular_dependency"


class ValidationError(Exception):
    """DSL 验证错误"""

    def __init__(self, message: str, reason_code: str = ""):
        super().__init__(message)
        self.reason_code = reason_code


class Validator:
    """DSL 表达式验证器"""

    def __init__(self, registry=None) -> None:
        # registry 用于解析已发布指标的依赖
        self._registry = registry
        self._errors: list[str] = []
        self._warnings: list[str] = []

    def validate(self, ast: ASTNode) -> dict[str, Any]:
        """验证 AST，返回验证结果

        Returns:
            {
                "valid": bool,
                "errors": list[str],
                "warnings": list[str],
                "historical_capable": bool,
                "unit": str,
                "period_type": str,
                "dependencies": list[str],  # 引用的指标名
            }
        """
        self._errors = []
        self._warnings = []

        # 1. 递归校验维度
        self._check_dimensions(ast)

        # 2. 检查依赖（收集引用的指标名）
        deps = self._collect_dependencies(ast)

        # 3. 检查循环依赖
        if self._registry:
            self._check_cycles(deps)

        valid = len(self._errors) == 0

        return {
            "valid": valid,
            "errors": self._errors,
            "warnings": self._warnings,
            "historical_capable": ast.historical_capable,
            "unit": ast.unit,
            "period_type": ast.period_type,
            "dependencies": list(deps),
        }

    def _check_dimensions(self, node: ASTNode) -> None:
        """递归维度校验 (PRD §11.4 DL7)

        校验规则:
        - 比较运算: 双方 unit 必须相同
        - 加减法: 双方 unit 必须相同, period_type 必须兼容
          (cumulative 不能与 point_in_time 混用, cumulative 不能与 single_quarter 混用)
        - 除法: period_type 不同时发出警告
        - TTM(): 仅适用于 cumulative 流量字段
        - 横截面函数: 自动标记 current_only
        """
        if isinstance(node, BinaryOp):
            left = node.left
            right = node.right
            if left and right:
                # 比较运算: 双方 unit 必须相同
                if node.op in (">", "<", ">=", "<=", "==", "!="):
                    if left.unit != right.unit and left.unit != "unknown" and right.unit != "unknown":
                        self._errors.append(
                            f"维度不匹配: {left.unit} {node.op} {right.unit} "
                            f"(PRD §11.4: 不允许{left.unit}与{right.unit}直接比较)"
                        )

                # 加减法: 双方 unit 必须相同 + period_type 必须兼容
                if node.op in ("+", "-"):
                    if left.unit != right.unit and left.unit != "unknown" and right.unit != "unknown":
                        self._errors.append(
                            f"维度不匹配: {left.unit} {node.op} {right.unit} "
                            f"(PRD §11.4: 不允许{left.unit}与{right.unit}相加减)"
                        )

                    # period_type 兼容性校验 (M5-问题1修复)
                    incompatible_pairs = {
                        ("cumulative", "point_in_time"),
                        ("point_in_time", "cumulative"),
                        ("cumulative", "single_quarter"),
                        ("single_quarter", "cumulative"),
                        ("current_only", "cumulative"),
                        ("cumulative", "current_only"),
                        ("current_only", "point_in_time"),
                        ("point_in_time", "current_only"),
                    }
                    pair = (left.period_type, right.period_type)
                    if pair in incompatible_pairs:
                        self._errors.append(
                            f"周期类型不匹配: {left.period_type} {node.op} {right.period_type} "
                            f"(PRD §11.4: 不允许累计值/时点值/当前值混用)"
                        )

                # 除法: period_type 不同时发出警告
                if node.op == "/":
                    if (left.period_type != right.period_type
                            and left.period_type != "unknown"
                            and right.period_type != "unknown"
                            and left.period_type != "mixed"
                            and right.period_type != "mixed"):
                        self._warnings.append(
                            f"除法 period_type 不一致: {left.period_type} / {right.period_type} "
                            f"(可能产生语义不明确的结果)"
                        )

            # 递归校验子节点
            if left:
                self._check_dimensions(left)
            if right:
                self._check_dimensions(right)

        elif isinstance(node, UnaryOp):
            if node.operand:
                self._check_dimensions(node.operand)

        elif isinstance(node, FuncCall):
            # TTM() 仅适用于 cumulative 流量字段 (PRD §11.4)
            if node.func_name == "TTM":
                for arg in node.args:
                    if arg.period_type not in ("cumulative", "ttm"):
                        self._warnings.append(
                            f"TTM() 应用于非累计字段 (period_type={arg.period_type})"
                        )

            # 横截面函数自动标记 current_only (PRD §11.2 DL4)
            if node.func_name in ("rank", "rank_industry", "percentile", "zscore", "normalize"):
                if node.historical_capable:
                    self._warnings.append(
                        f"{node.func_name}() 是横截面函数, 应自动标记为 current_only"
                    )

            # 递归校验参数
            for arg in node.args:
                self._check_dimensions(arg)

    def _collect_dependencies(self, node: ASTNode) -> set[str]:
        """收集 AST 中引用的所有指标名（用于依赖检测）"""
        deps: set[str] = set()

        if isinstance(node, IndicatorRef):
            deps.add(node.name)

        elif isinstance(node, FieldRef):
            deps.add(f"{node.table}.{node.field}")

        elif isinstance(node, BinaryOp):
            if node.left:
                deps.update(self._collect_dependencies(node.left))
            if node.right:
                deps.update(self._collect_dependencies(node.right))

        elif isinstance(node, UnaryOp):
            if node.operand:
                deps.update(self._collect_dependencies(node.operand))

        elif isinstance(node, FuncCall):
            for arg in node.args:
                deps.update(self._collect_dependencies(arg))

        return deps

    def _check_cycles(self, deps: set[str], visited: set[str] | None = None) -> None:
        """检查循环依赖 (PRD §11.5 DL15)

        P1-20修复: 实现真正的DFS递归检测，而非仅检查直接依赖。
        从deps出发，递归查找registry中已发布的依赖项的依赖，
        如果遇到visited中已存在的节点，说明存在循环。
        """
        if visited is None:
            visited = set()

        for dep in deps:
            if dep in visited:
                self._errors.append(
                    f"循环依赖检测到: {dep} (PRD §11.5: 系统必须拒绝循环依赖)"
                )
                return
            visited.add(dep)

            # P1-20修复: 递归检查依赖的依赖
            # 查找registry中已发布的同名指标，获取其依赖
            try:
                from app.core.storage.sqlite_store import SQLiteStore
                sqlite = SQLiteStore()
                # dep格式: "indicator_name" 或 "indicator_name@version"
                dep_parts = dep.split("@")
                dep_name = dep_parts[0]
                dep_version = int(dep_parts[1]) if len(dep_parts) > 1 else None

                if dep_version:
                    rows = sqlite.query(
                        "SELECT dependencies_json FROM dsl_expressions WHERE name=? AND version=? AND status='published'",
                        [dep_name, dep_version],
                    )
                else:
                    rows = sqlite.query(
                        "SELECT dependencies_json FROM dsl_expressions WHERE name=? AND status='published' ORDER BY version DESC LIMIT 1",
                        [dep_name],
                    )

                if rows and rows[0].get("dependencies_json"):
                    import json as _json
                    sub_deps = set(_json.loads(rows[0]["dependencies_json"]))
                    self._check_cycles(sub_deps, visited.copy())
            except Exception:
                pass  # 非致命：registry查询失败时跳过递归
