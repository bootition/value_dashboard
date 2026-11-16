"""DSL 括号表达式必须真正可用（2026-11-16，reports/128 缺陷）。

背景
----
`grammar.lark` 里括号是 `primary` 的一个未命名分支 `"(" expr ")"`，而
`_get_parser()` 用了 `keep_all_tokens=True` → 该分支的解析树带 `(`、`)` 两个
Token（共 3 个孩子）→ 语法里 `?primary` 的"单孩子内联"失效，
`Tree('primary')` 原样漏到上层。后果分三种：

1. 括号出现在四则运算里（`a / (b - c + d)`、`2 * (x + 1)`）：
   `multiplicative()` 取 `.unit` 时抛 `AttributeError` → 调用方拿到 500 而不是
   校验错误，**用户完全写不出带括号的复合指标**。ROCE 的
   `EBIT / (流动资产 − 流动负债 + 固定资产)` 正是这种形态。
2. 括号出现在比较左侧（`(a + b) > 100`）：`Validator._check_dimensions()`
   同样在 `.unit` 上崩。
3. 绕过校验直接 codegen：旧 `_gen_expr` 的兜底 `return "NULL"` 会把整段括号
   表达式编译成字面量 `NULL` —— 规则能保存、能运行、**永远命中 0 只且不报错**。

同时暴露的测试盲区：`test_screening_config_limits.py` 里
`assert parse("((1))") is not None` 只断言"没抛异常"，而缺陷下返回的是
`Tree` 而不是 AST，**该断言一直是绿的**。本文件改为断言真实 AST 类型。
"""

from __future__ import annotations

import pytest

from app.core.dsl.ast_nodes import ASTNode, BinaryOp, FieldRef, Literal
from app.core.dsl.codegen import CodeGen, UnsupportedPeriodFunctionError
from app.core.dsl.parser import parse
from app.core.dsl.validator import Validator


def _parse_ast(expression: str) -> ASTNode:
    ast = parse(expression)
    assert isinstance(ast, ASTNode), (
        f"解析结果必须是 AST 节点而不是 {type(ast).__name__}：{expression}"
    )
    return ast


# ─── 解析：括号必须返回真实 AST ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "expression",
    [
        "(1)",
        "((1))",
        "(((1)))",
    ],
)
def test_parenthesized_literal_returns_ast_not_parse_tree(expression: str) -> None:
    """纯括号数字 → Literal（修复前返回 lark Tree）。"""
    ast = _parse_ast(expression)
    assert isinstance(ast, Literal)
    assert ast.value == 1.0


def test_parenthesized_subtraction_inside_division_parses() -> None:
    """`a / (b - c)` —— 修复前抛 AttributeError: 'Tree' object has no attribute 'unit'。"""
    ast = _parse_ast(
        "income.revenue@TTM / (balance.total_assets@LATEST - balance.total_equity@LATEST)"
    )
    assert isinstance(ast, BinaryOp)
    assert ast.op == "/"
    assert isinstance(ast.right, BinaryOp) and ast.right.op == "-"


def test_parenthesized_addition_inside_multiplication_parses() -> None:
    """`2 * (x + 1)` 同上。"""
    ast = _parse_ast("2 * (income.revenue@TTM + 1)")
    assert isinstance(ast, BinaryOp) and ast.op == "*"
    assert isinstance(ast.right, BinaryOp) and ast.right.op == "+"


def test_nested_parentheses_preserve_precedence() -> None:
    """括号改变优先级：`(a + b) / c` 的根是除法，左节点是加法。"""
    ast = _parse_ast("(income.total_profit@TTM + income.income_tax@TTM) / income.revenue@TTM")
    assert isinstance(ast, BinaryOp) and ast.op == "/"
    assert isinstance(ast.left, BinaryOp) and ast.left.op == "+"


def test_comparison_with_parenthesized_left_side_parses() -> None:
    """`(a + b) > 100` —— 修复前在校验器 `.unit` 上崩。"""
    ast = _parse_ast("(income.total_profit@TTM + income.financial_expenses@TTM) > 100")
    assert isinstance(ast, BinaryOp) and ast.op == ">"
    assert isinstance(ast.left, BinaryOp) and ast.left.op == "+"


def test_parentheses_do_not_change_units() -> None:
    """括号只影响优先级，不得污染量纲：括号内 CNY + CNY 仍是 CNY。"""
    plain = _parse_ast("income.total_profit@TTM + income.income_tax@TTM")
    wrapped = _parse_ast("(income.total_profit@TTM + income.income_tax@TTM)")
    assert wrapped.unit == plain.unit
    assert wrapped.period_type == plain.period_type


# ─── 代码生成：括号表达式必须编译成真实 SQL，不能是 NULL ────────────────────


def test_parenthesized_expression_compiles_to_real_sql() -> None:
    """ROCE 形态必须编译出含各字段的 SQL，而不是字面量 NULL。"""
    ast = _parse_ast(
        "(income.total_profit@TTM + income.financial_expenses@TTM) / "
        "(balance.total_current_assets@LATEST - balance.total_current_liabilities@LATEST "
        "+ balance.fixed_assets@LATEST)"
    )
    sql = CodeGen().generate(ast, "roce_point")
    assert " NULL " not in sql.replace("NULLIF", " "), sql
    assert not sql.startswith("NULL"), sql
    for column in (
        "total_profit",
        "financial_expenses",
        "total_current_assets",
        "total_current_liabilities",
        "fixed_assets",
    ):
        assert column in sql


def test_codegen_refuses_to_emit_null_for_unknown_node() -> None:
    """兜底不许再返回 "NULL"：非 AST 节点必须显式失败（fail-closed）。"""

    class NotAnAst:
        unit = "unknown"

    with pytest.raises(UnsupportedPeriodFunctionError, match="unsupported AST node"):
        CodeGen()._gen_expr(NotAnAst())  # type: ignore[arg-type]


# ─── 校验：括号表达式必须能通过维度校验 ────────────────────────────────────


def _isolated_sqlite(tmp_path):
    """构造一个只存在于临时目录的 SQLiteStore（不碰正式库）。"""
    from app.core.storage.path_policy import DatabasePathSet, VdEnv
    from app.core.storage.schema import init_sqlite_schema
    from app.core.storage.sqlite_store import SQLiteStore

    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=tmp_path / "valuedashboard.duckdb",
        sqlite_path=tmp_path / "valuedashboard.sqlite",
        run_root=tmp_path,
    )
    store = SQLiteStore(paths=paths)
    init_sqlite_schema(store)
    return store


def test_validator_accepts_parenthesized_ratio(tmp_path) -> None:
    """带括号的复合表达式必须通过校验（修复前整条链路走不到这里）。"""
    ast = _parse_ast(
        "(income.total_profit@TTM + income.financial_expenses@TTM) / "
        "(balance.total_current_assets@LATEST - balance.total_current_liabilities@LATEST "
        "+ balance.fixed_assets@LATEST)"
    )
    result = Validator(sqlite=_isolated_sqlite(tmp_path)).validate(ast)
    assert result["valid"] is True, result["errors"]
    assert result["unit"] in {"unknown", "ratio"}


def test_validated_parenthesized_expression_keeps_field_dependencies(tmp_path) -> None:
    """括号不能把字段吃掉：校验后仍能列出全部引用字段。"""
    ast = _parse_ast(
        "(balance.total_current_assets@LATEST - balance.total_current_liabilities@LATEST) "
        "+ balance.fixed_assets@LATEST"
    )
    result = Validator(sqlite=_isolated_sqlite(tmp_path)).validate(ast)
    assert result["valid"] is True, result["errors"]

    seen: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, FieldRef):
            seen.append(f"{node.table}.{node.field}")
        elif isinstance(node, BinaryOp):
            if node.left is not None:
                _walk(node.left)
            if node.right is not None:
                _walk(node.right)

    _walk(ast)
    assert sorted(seen) == [
        "balance.fixed_assets",
        "balance.total_current_assets",
        "balance.total_current_liabilities",
    ]
