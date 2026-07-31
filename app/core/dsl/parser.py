"""DSL 解析器 — 将表达式文本解析为 AST

使用 lark Earley 解析器，支持 PRD §11.2 的全部运算与派生。
不支持自定义函数/Python/SQL (PRD §11.6)。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from lark import Lark, Transformer, Token

from app.core.dsl.ast_nodes import (
    ASTNode, Literal, FieldRef, IndicatorRef, FuncCall,
    BinaryOp, UnaryOp, FIELD_METADATA, INDICATOR_METADATA,
)

logger = logging.getLogger(__name__)

_GRAMMAR_PATH = Path(__file__).parent / "grammar.lark"
_parser: Lark | None = None
MAX_EXPRESSION_BYTES = 10_000
MAX_EXPRESSION_TOKENS = 500
MAX_NESTING_DEPTH = 50
MAX_FUNCTION_ARGS = 10
_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?|[()@.,+*/<>=!-]")


def _get_parser() -> Lark:
    global _parser
    if _parser is None:
        grammar_text = _GRAMMAR_PATH.read_text(encoding="utf-8")
        _parser = Lark(grammar_text, parser="earley", start="expr", keep_all_tokens=True)
    return _parser


class DSLTransformer(Transformer):
    """将 lark 解析树转换为 AST"""

    def NUMBER(self, token: Token) -> Literal:
        return Literal(value=float(token), unit="unknown", historical_capable=True)

    def TABLE(self, token: Token) -> str:
        return str(token)

    def FIELD_NAME(self, token: Token) -> str:
        return str(token)

    def INDICATOR_NAME(self, token: Token) -> str:
        return str(token)

    def FUNC_NAME(self, token: Token) -> str:
        return str(token)

    def PERIOD_KEYWORD(self, token: Token) -> str:
        return str(token)

    def period_spec(self, items):
        # items: ["@", "TTM"] → return "TTM"
        if len(items) >= 2:
            return str(items[1])
        return "LATEST"

    def field_ref(self, items):
        # items: [TABLE, ".", FIELD_NAME, optional period_spec Tree]
        table = str(items[0])
        field_name = str(items[2])
        period = "LATEST"
        if len(items) > 3 and items[3] is not None:
            # period_spec is a string returned by period_spec()
            period = str(items[3])

        key = f"{table}.{field_name}"
        meta = FIELD_METADATA.get(key, {})
        unit = meta.get("unit", "unknown")
        period_type = meta.get("period_type", "point_in_time")
        if period == "TTM":
            period_type = "ttm"
        elif period in {"YoY", "QoQ"}:
            unit = "ratio"
            period_type = "single_quarter"
        return FieldRef(
            table=table,
            field=field_name,
            period=period,
            unit=unit,
            period_type=period_type,
            historical_capable=meta.get("historical_capable", True),
        )

    def indicator_ref(self, items):
        name = str(items[0])
        meta = INDICATOR_METADATA.get(name, {})
        return IndicatorRef(
            name=name,
            unit=meta.get("unit", "unknown"),
            period_type=meta.get("period_type", "current_only"),
            historical_capable=meta.get("historical_capable", False),
        )

    def func_call(self, items):
        # P0#11修复: keep_all_tokens=True 导致 items 含 "(" ")" 等标点 Token
        # 原: items[1] 是 "(" Token 而非 arg_list, 导致所有 FUNC(field) 失败
        # 修复: 过滤掉标点 Token, 只保留实际参数
        func_name = str(items[0])
        func_args: list[ASTNode] = []
        for item in items[1:]:
            if item is None:
                continue
            if isinstance(item, Token):
                # 跳过标点 Token: ( ) ,
                continue
            if isinstance(item, list):
                func_args.extend(item)
            else:
                func_args.append(item)

        is_cross = func_name in ("rank", "rank_industry", "percentile", "zscore", "normalize")
        if len(func_args) > MAX_FUNCTION_ARGS:
            raise ValueError(f"{func_name}() exceeds {MAX_FUNCTION_ARGS} arguments")
        is_growth = func_name in ("YoY", "QoQ")
        is_ttm = func_name == "TTM"
        return FuncCall(
            func_name=func_name,
            args=func_args,
            unit="ratio" if is_cross or is_growth else (func_args[0].unit if func_args else "unknown"),
            period_type=(
                "current_only" if is_cross else "single_quarter" if is_growth else "ttm"
                if is_ttm else (func_args[0].period_type if func_args else "current_only")
            ),
            historical_capable=False if is_cross else (func_args[0].historical_capable if func_args else False),
        )

    def arg_list(self, items):
        return [a for a in items if a is not None and not isinstance(a, Token)]

    def additive(self, items):
        result = items[0]
        i = 1
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            result = BinaryOp(
                op=op, left=result, right=right,
                unit=result.unit if result.unit == right.unit else "mixed",
                period_type=result.period_type if result.period_type == right.period_type else "mixed",
                historical_capable=result.historical_capable and right.historical_capable,
            )
            i += 2
        return result

    def multiplicative(self, items):
        result = items[0]
        i = 1
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            if op == "/" and result.unit == right.unit and result.unit != "unknown":
                result_unit = "ratio"
            elif op == "*":
                result_unit = result.unit if result.unit == right.unit else "mixed"
            else:
                result_unit = "mixed"
            result = BinaryOp(
                op=op, left=result, right=right,
                unit=result_unit,
                period_type=result.period_type if result.period_type == right.period_type else "mixed",
                historical_capable=result.historical_capable and right.historical_capable,
            )
            i += 2
        return result

    def or_expr(self, items):
        operands = [item for item in items if not isinstance(item, Token)]
        result = operands[0]
        for operand in operands[1:]:
            result = BinaryOp(op="OR", left=result, right=operand, unit="unknown", historical_capable=False)
        return result

    def and_expr(self, items):
        operands = [item for item in items if not isinstance(item, Token)]
        result = operands[0]
        for operand in operands[1:]:
            result = BinaryOp(op="AND", left=result, right=operand, unit="unknown", historical_capable=False)
        return result

    def comparison(self, items):
        if len(items) == 1:
            return items[0]
        op = str(items[1])
        return BinaryOp(
            op=op, left=items[0], right=items[2],
            unit="unknown", historical_capable=False,
        )

    def unary(self, items):
        if len(items) == 2 and str(items[0]) == "-":
            return UnaryOp(op="-", operand=items[1], unit=items[1].unit, historical_capable=items[1].historical_capable)
        return items[0]

    def expr(self, items):
        return items[0] if items else Literal()

    def start(self, items):
        return items[0] if items else Literal()


def parse(expression: str) -> ASTNode:
    """解析 DSL 表达式为 AST

    Args:
        expression: DSL 表达式文本, 如 "income.revenue@TTM / balance.total_assets"

    Returns:
        AST 根节点

    Raises:
        ParseError: 语法错误
    """
    _validate_expression_budget(expression)
    parser = _get_parser()
    tree = parser.parse(expression)
    transformer = DSLTransformer()
    ast = transformer.transform(tree)
    logger.debug(f"解析 '{expression}' → {ast.__class__.__name__}")
    return ast


def _validate_expression_budget(expression: str) -> None:
    """Reject oversized input before Earley parsing consumes unbounded resources."""
    if len(expression.encode("utf-8")) > MAX_EXPRESSION_BYTES:
        raise ValueError(f"expression exceeds {MAX_EXPRESSION_BYTES} bytes")
    tokens = _TOKEN_PATTERN.findall(expression)
    if len(tokens) > MAX_EXPRESSION_TOKENS:
        raise ValueError(f"expression exceeds {MAX_EXPRESSION_TOKENS} tokens")
    depth = 0
    for character in expression:
        if character == "(":
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise ValueError(f"expression exceeds {MAX_NESTING_DEPTH} nesting depth")
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("expression has unmatched closing parenthesis")
    if depth:
        raise ValueError("expression has unmatched opening parenthesis")
