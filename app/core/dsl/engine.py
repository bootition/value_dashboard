"""DSL 引擎管理器 — 整合 parser/validator/codegen/registry

实现完整生命周期 (PRD §11.5 DL13):
草稿 → 校验 → 单股预览 → 小样本预览 → 发布

简写展开 (PRD §11.5 DL10-11):
- 流量字段裸名 (revenue) → income.revenue@TTM
- 时点字段裸名 (total_assets) → balance.total_assets@LATEST
- 内建指标裸名 (pe_ttm) → 保持原样
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.dsl.parser import parse
from app.core.dsl.validator import Validator
from app.core.dsl.codegen import CodeGen
from app.core.dsl.registry import ExpressionRegistry, STATUS_PUBLISHED
from app.core.dsl.ast_nodes import FIELD_METADATA, INDICATOR_METADATA, ASTNode
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# 简写展开: 裸字段名 → 完整引用 (PRD §11.5 DL11)
# 流量与盈利能力字段简写默认展开到 TTM 口径
# 资产负债表时点字段默认展开到最新报告期口径
_SHORTHAND_MAP: dict[str, str] = {}
for full_key, meta in FIELD_METADATA.items():
    table, field = full_key.split(".", 1)
    if field not in _SHORTHAND_MAP:  # 第一个匹配的表优先
        if meta["period_type"] == "cumulative":
            _SHORTHAND_MAP[field] = f"{table}.{field}@TTM"
        elif meta["period_type"] == "point_in_time":
            _SHORTHAND_MAP[field] = f"{table}.{field}@LATEST"
        else:
            _SHORTHAND_MAP[field] = f"{table}.{field}"


def expand_shorthand(expression: str) -> str:
    """展开简写 (PRD §11.5 DL10-11)

    识别表达式中的裸字段名，根据 FIELD_METADATA 的 period_type 自动添加表前缀和周期后缀。

    规则:
    - 已有表前缀的 (如 income.revenue) 不展开
    - 已有 @ 周期后缀的不展开
    - 内建指标名 (如 pe_ttm) 不展开
    - 流量字段 (cumulative) → table.field@TTM
    - 时点字段 (point_in_time) → table.field@LATEST

    Examples:
        "revenue / total_assets" → "income.revenue@TTM / balance.total_assets@LATEST"
        "pe_ttm > 0 AND revenue > 1000" → "pe_ttm > 0 AND income.revenue@TTM > 1000"
    """
    # 获取所有已知字段名（不含表前缀）
    known_fields = set(_SHORTHAND_MAP.keys())
    # 获取所有内建指标名（不展开）
    known_indicators = set(INDICATOR_METADATA.keys())

    # 用正则匹配裸标识符 (不在 table.field 或 func() 上下文中)
    # 策略: 分割为 token，逐个替换裸字段名
    # 使用更精确的正则: 只匹配纯字母开头的标识符
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*|[\d.]+|[^\w\s]+|\s+', expression)
    result_parts: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # 检查是否是 table.field 模式
        if (token in known_fields
                and token not in known_indicators
                and i + 1 < len(tokens) and tokens[i + 1].strip() == "."
                and i + 2 < len(tokens) and tokens[i + 2][0].isalpha()):
            # 这是 table.field 模式的前半，不展开
            result_parts.append(token)
        elif token in known_fields and token not in known_indicators:
            # 检查前一个非空token是否是"."
            prev_non_space = ""
            for j in range(len(result_parts) - 1, -1, -1):
                if result_parts[j].strip():
                    prev_non_space = result_parts[j].strip()
                    break
            if prev_non_space == ".":
                # 前一个是".", 说明是 table.field 的后半，不展开
                result_parts.append(token)
            else:
                # 裸字段名，展开
                expanded = _SHORTHAND_MAP[token]
                result_parts.append(expanded)
        else:
            result_parts.append(token)
        i += 1

    expanded = "".join(result_parts)
    if expanded != expression:
        logger.info(f"简写展开: '{expression}' → '{expanded}'")
    return expanded


class DSLEngine:
    """DSL 引擎: 解析→校验→预览→发布 完整流程"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        registry: ExpressionRegistry | None = None,
        validator: Validator | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("DSLEngine requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("DSLEngine stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.registry = registry or ExpressionRegistry(sqlite=sqlite)
        if self.registry.sqlite is not sqlite:
            raise PathIsolationError("DSLEngine registry must use the injected SQLite store")
        self.validator = validator or Validator(self.registry, sqlite=sqlite)
        if self.validator._registry is not self.registry or self.validator._sqlite is not sqlite:
            raise PathIsolationError("DSLEngine validator must share its registry and SQLite store")
        self.codegen = CodeGen()
        self.duck = duck

    def create(self, name: str, expression: str,
               description: str = "", direction: str = "none") -> dict[str, Any]:
        """创建表达式草稿"""
        return self.registry.create(name, expression, description, direction)

    def validate(self, name: str, version: int) -> dict[str, Any]:
        """校验表达式

        1. 简写展开 (PRD §11.5 DL10-11)
        2. 解析表达式为 AST
        3. 维度校验
        4. 历史能力推导
        5. 依赖检测
        6. 记录 AST 到 registry
        """
        expr = self.registry.get(name, version)
        if not expr:
            return {"error": "expression not found"}
        if expr["status"] != "draft":
            return {"error": "expression must be a draft before validation"}

        expression_text = expr["expression_text"]

        try:
            # 1. 简写展开
            expanded = expand_shorthand(expression_text)

            # 2. 解析展开后的表达式
            ast = parse(expanded)

            # 3. 校验
            result = self.validator.validate(ast)

            if result["valid"]:
                # 4. 记录 AST
                ast_json = json.dumps(ast.to_dict(), ensure_ascii=False)
                self.registry.validate(
                    name, version, ast_json, result["historical_capable"]
                )

                # 5. 记录依赖
                for dep in result["dependencies"]:
                    if "." in dep:
                        continue  # 标准化字段, 不需要版本锁定
                    # 查找已发布的依赖指标
                    dep_expr = self.registry.get(dep)
                    if dep_expr and dep_expr["status"] == STATUS_PUBLISHED:
                        self.registry.add_dependency(
                            name, version, dep, dep_expr["version"]
                        )

                result["name"] = name
                result["version"] = version
                result["status"] = "validated"
                result["expanded_expression"] = expanded  # PRD §11.5 DL10: 保存前展示展开结果
                return result
            else:
                return {
                    "name": name, "version": version,
                    "status": "draft",
                    "valid": False,
                    "errors": result["errors"],
                    "warnings": result["warnings"],
                    "expanded_expression": expanded,
                }

        except Exception as e:
            logger.error(f"校验失败: {e}")
            return {"error": str(e), "valid": False}

    def preview_single(self, name: str, version: int,
                       stock_code: str) -> dict[str, Any]:
        """单股预览 (PRD §11.5 DL13: 生命周期第3步)

        PRD §11.3 DL6: current_only 表达式不能生成历史序列
        PRD §11.4 DL9: 值为 null 时返回原因码
        """
        expr = self.registry.get(name, version)
        if not expr:
            return {"error": "expression not found"}
        if expr["status"] != "validated":
            return {"error": "expression must be validated before single-stock preview"}

        try:
            # P1-19修复: preview时也展开简写（与validate保持一致）
            expanded_expr = expand_shorthand(expr["expression_text"])
            ast = parse(expanded_expr)

            # DL6: 如果表达式是 current_only，拒绝历史序列请求
            # 当前预览只返回当前值，但如果未来添加 period 参数时需要检查
            if not ast.historical_capable:
                logger.debug(f"表达式 {name} v{version} 是 current_only，仅返回当前值")

            sql = self.codegen.generate_select(ast, stock_code=stock_code, alias="result")

            results = self.duck.read_query(sql)
            value = results[0]["result"] if results else None

            # DL9: 值为 null 时推断原因码
            reason_codes: list[str] = []
            if value is None:
                reason_codes = self._infer_reason_codes(ast, stock_code)

            self.registry.preview_single(name, version)
            return {
                "name": name, "version": version,
                "stock_code": stock_code,
                "value": value,
                "sql": sql,
                "historical_capable": ast.historical_capable,
                "reason_codes": reason_codes,
            }
        except Exception as e:
            return {"error": str(e)}

    def preview_sample(self, name: str, version: int,
                       limit: int = 10) -> dict[str, Any]:
        """小样本预览 (PRD §11.5 DL13: 生命周期第4步)"""
        expr = self.registry.get(name, version)
        if not expr:
            return {"error": "expression not found"}
        if expr["status"] != "single_previewed":
            return {"error": "expression must complete single-stock preview before sample preview"}

        try:
            # P1-19修复: preview_sample也展开简写
            expanded_expr = expand_shorthand(expr["expression_text"])
            ast = parse(expanded_expr)
            sql = self.codegen.generate_select(ast, stock_code=None, alias="result")
            sql += f" LIMIT {limit}"

            results = self.duck.read_query(sql)

            self.registry.preview(name, version)
            return {
                "name": name, "version": version,
                "results": results,
                "count": len(results),
                "historical_capable": ast.historical_capable,
            }
        except Exception as e:
            return {"error": str(e)}

    def _infer_reason_codes(self, ast: ASTNode, stock_code: str) -> list[str]:
        """推断值为 null 的原因码 (PRD §11.4 DL9)"""
        from app.core.dsl.validator import (
            REASON_DIVISION_BY_ZERO, REASON_FIELD_MISSING,
        )
        codes: list[str] = []

        # 检查是否有除法 (可能分母为零)
        if self._has_division_by_zero_pattern(ast):
            codes.append(REASON_DIVISION_BY_ZERO)

        # 检查引用的字段是否缺失
        deps = self._collect_field_deps(ast)
        if deps:
            for dep in deps:
                table, field = dep.split(".", 1) if "." in dep else ("", dep)
                if not self._field_has_data(stock_code, table, field):
                    codes.append(f"{REASON_FIELD_MISSING}: {dep}")
                    break

        return codes

    def _has_division_by_zero_pattern(self, node: ASTNode) -> bool:
        """检查 AST 中是否有除法运算"""
        from app.core.dsl.ast_nodes import BinaryOp, UnaryOp, FuncCall
        if isinstance(node, BinaryOp):
            if node.op == "/":
                return True
            if node.left and self._has_division_by_zero_pattern(node.left):
                return True
            if node.right and self._has_division_by_zero_pattern(node.right):
                return True
        elif isinstance(node, UnaryOp):
            if node.operand:
                return self._has_division_by_zero_pattern(node.operand)
        elif isinstance(node, FuncCall):
            for arg in node.args:
                if self._has_division_by_zero_pattern(arg):
                    return True
        return False

    def _collect_field_deps(self, node: ASTNode) -> set[str]:
        """收集 AST 中引用的字段"""
        from app.core.dsl.ast_nodes import FieldRef, BinaryOp, UnaryOp, FuncCall, IndicatorRef
        deps: set[str] = set()
        if isinstance(node, FieldRef):
            deps.add(f"{node.table}.{node.field}")
        elif isinstance(node, IndicatorRef):
            deps.add(node.name)
        elif isinstance(node, BinaryOp):
            if node.left:
                deps.update(self._collect_field_deps(node.left))
            if node.right:
                deps.update(self._collect_field_deps(node.right))
        elif isinstance(node, UnaryOp):
            if node.operand:
                deps.update(self._collect_field_deps(node.operand))
        elif isinstance(node, FuncCall):
            for arg in node.args:
                deps.update(self._collect_field_deps(arg))
        return deps

    def _field_has_data(self, stock_code: str, table: str, field: str) -> bool:
        """检查字段是否有数据"""
        try:
            table_map = {"balance": "balance_sheet", "income": "income_statement", "cashflow": "cash_flow"}
            db_table = table_map.get(table, table)
            rows = self.duck.read_query(
                f"SELECT COUNT(*) as cnt FROM {db_table} WHERE stock_code = ? AND {field} IS NOT NULL",
                [stock_code],
            )
            return rows[0]["cnt"] > 0 if rows else False
        except Exception:
            return True  # 无法确认时假设有数据

    def publish(self, name: str, version: int) -> dict[str, Any]:
        """发布表达式 (PRD §11.5 DL14: 已发布版本不可变更)"""
        expr = self.registry.get(name, version)
        if not expr:
            return {"error": "expression not found"}

        if expr["status"] != "previewed":
            return {"error": f"表达式必须完成校验和两次预览 (当前状态: {expr['status']})"}

        # Screening only materializes current snapshot-period values. Refuse
        # expressions whose preview requires historical rows rather than
        # publishing an indicator with different runtime semantics.
        expanded = expand_shorthand(expr["expression_text"])
        if any(token in expanded for token in ("@MRQ", "@TTM", "@YoY", "@QoQ", "TTM(", "YoY(", "QoQ(")):
            return {"error": "screening cannot publish historical-period DSL expressions"}

        # 检查循环依赖
        if self.registry.check_circular(name, version):
            return {"error": "循环依赖检测到 (PRD §11.5 DL15)"}

        try:
            return self.registry.publish(name, version)
        except ValueError as error:
            return {"error": str(error)}

    def list_published(self) -> list[dict[str, Any]]:
        """列出已发布的复合指标"""
        return self.registry.list_published()

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有表达式"""
        return self.registry.list_expressions()

    def discover_fields(self) -> list[str]:
        """发现可用字段 (PRD §16.1 CL4: discover fields)"""
        from app.core.dsl.ast_nodes import FIELD_METADATA
        return sorted(FIELD_METADATA.keys())

    def discover_indicators(self) -> list[str]:
        """发现可用内建指标"""
        from app.core.dsl.ast_nodes import INDICATOR_METADATA
        return sorted(INDICATOR_METADATA.keys())

    def discover_functions(self) -> list[str]:
        """发现可用函数"""
        from app.core.dsl.codegen import CodeGen
        return sorted(CodeGen.FUNC_SQL.keys())

    def discover_reason_codes(self) -> list[str]:
        """发现原因码"""
        from app.core.dsl.validator import (
            REASON_DIVISION_BY_ZERO, REASON_FIELD_MISSING,
            REASON_INSUFFICIENT_HISTORY, REASON_DIMENSION_MISMATCH,
            REASON_CYCLE_DETECTED,
        )
        return [
            REASON_DIVISION_BY_ZERO, REASON_FIELD_MISSING,
            REASON_INSUFFICIENT_HISTORY, REASON_DIMENSION_MISMATCH,
            REASON_CYCLE_DETECTED,
        ]
