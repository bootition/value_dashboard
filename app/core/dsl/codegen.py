"""DSL SQL 代码生成 — 将验证后的 AST 转换为 DuckDB SQL

生成的 SQL 可在 indicator_snapshot 表上执行。
"""

from __future__ import annotations

import logging

from app.core.dsl.ast_nodes import (
    ASTNode, Literal, FieldRef, IndicatorRef, FuncCall, BinaryOp, UnaryOp,
)

logger = logging.getLogger(__name__)


class UnsupportedPeriodFunctionError(ValueError):
    """Raised when an expression requires historical rows unavailable to code generation."""


class CodeGen:
    """DuckDB SQL 代码生成器"""

    # 函数 → SQL 模板
    FUNC_SQL: dict[str, str] = {
        "rank": "RANK() OVER (ORDER BY {arg})",
        "rank_industry": "RANK() OVER (PARTITION BY sw_level1 ORDER BY {arg})",
        "percentile": "PERCENT_RANK() OVER (ORDER BY {arg})",
        "zscore": "({arg} - AVG({arg}) OVER ()) / NULLIF(STDDEV({arg}) OVER (), 0)",
        "normalize": "({arg} - MIN({arg}) OVER ()) / NULLIF((MAX({arg}) OVER () - MIN({arg}) OVER ()), 0)",
        "TTM": "{arg}",  # TTM 在数据层已处理，这里透传
        "YoY": "LAG({arg}, 4) OVER (ORDER BY report_date)",  # 简化: 4期前
        "QoQ": "LAG({arg}, 1) OVER (ORDER BY report_date)",
        "CAGR": "POWER({arg} / NULLIF(LAG({arg}, {n}) OVER (ORDER BY report_date), 0), 1.0/{n}) - 1",
        "rolling_avg": "AVG({arg}) OVER (ORDER BY report_date ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)",
        "rolling_max": "MAX({arg}) OVER (ORDER BY report_date ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)",
        "rolling_min": "MIN({arg}) OVER (ORDER BY report_date ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)",
        "lag": "LAG({arg}, {n}) OVER (ORDER BY report_date)",
        "avg": "AVG({arg})",
        "max": "MAX({arg})",
        "min": "MIN({arg})",
    }

    def generate(self, ast: ASTNode, alias: str = "result") -> str:
        """生成 SELECT 子句中的 SQL 表达式

        Args:
            ast: 验证后的 AST
            alias: 结果列别名

        Returns:
            SQL 片段, 如 "income.revenue / NULLIF(balance.total_assets, 0) AS result"
        """
        sql = self._gen_expr(ast)
        return f"{sql} AS {alias}"

    def generate_select(
        self, ast: ASTNode, stock_code: str | None = None, alias: str = "result"
    ) -> str:
        """生成完整的 SELECT 语句

        自动检测需要 JOIN 的财务报表表，生成相应 JOIN 子句。
        """
        expr_sql = self._gen_expr(ast)

        # 检测需要 JOIN 的表
        tables_needed = set()
        self._collect_tables(ast, tables_needed)

        # 构建 FROM + JOIN
        from_clause = "indicator_snapshot s"
        for table_full, alias_short in [
            ("balance_sheet", "bs"), ("income_statement", "ic"), ("cash_flow", "cf")
        ]:
                        # 检查 ast 中是否引用了这个表
            table_prefix = {"balance_sheet": "balance", "income_statement": "income", "cash_flow": "cashflow"}
            if table_prefix[table_full] in tables_needed:
                from_clause += f"\nLEFT JOIN {table_full} {alias_short} ON s.stock_code = {alias_short}.stock_code AND s.report_date = {alias_short}.report_date"

        # P0#12修复: 白名单验证 stock_code, 防止 SQL 注入
        # stock_code 必须是 6 位数字, 否则拒绝
        import re
        if stock_code:
            if not re.match(r"^\d{6}$", stock_code):
                raise ValueError(f"无效的股票代码: {stock_code}")
            where = f"WHERE s.stock_code = '{stock_code}'"
        else:
            where = ""
        return f"SELECT s.stock_code, {expr_sql} AS {alias}\nFROM {from_clause}\n{where}"

    def _collect_tables(self, node: ASTNode, tables: set[str]) -> None:
        """收集 AST 中引用的表名"""
        if isinstance(node, FieldRef):
            tables.add(node.table)
        elif isinstance(node, BinaryOp):
            if node.left:
                self._collect_tables(node.left, tables)
            if node.right:
                self._collect_tables(node.right, tables)
        elif isinstance(node, UnaryOp):
            if node.operand:
                self._collect_tables(node.operand, tables)
        elif isinstance(node, FuncCall):
            for arg in node.args:
                self._collect_tables(arg, tables)

    def _gen_expr(self, node: ASTNode) -> str:
        """递归生成 SQL 表达式"""
        if isinstance(node, Literal):
            return str(node.value)

        if isinstance(node, FieldRef):
            if node.period in {"TTM", "YoY", "QoQ"}:
                raise UnsupportedPeriodFunctionError(
                    f"{node.period} requires a historical execution context"
                )
            # field_ref → table_alias.field_name
            table_alias = {"balance": "bs", "income": "ic", "cashflow": "cf"}.get(node.table, "s")
            return f"{table_alias}.{node.field}"

        if isinstance(node, IndicatorRef):
            # indicator_ref → indicator_snapshot 中的列名
            return node.name

        if isinstance(node, BinaryOp):
            left = self._gen_expr(node.left) if node.left else ""
            right = self._gen_expr(node.right) if node.right else ""

            if node.op in ("AND", "OR"):
                return f"({left} {node.op} {right})"

            # 除法: 防止除零 (PRD §11.4 DL9: 分母为零返回 null)
            if node.op == "/":
                return f"({left} / NULLIF({right}, 0))"

            return f"({left} {node.op} {right})"

        if isinstance(node, UnaryOp):
            operand = self._gen_expr(node.operand) if node.operand else ""
            return f"(-{operand})"

        if isinstance(node, FuncCall):
            if node.func_name in {"TTM", "YoY", "QoQ"}:
                raise UnsupportedPeriodFunctionError(
                    f"{node.func_name} requires a historical execution context"
                )
            # 处理带参数的函数
            args_sql = [self._gen_expr(a) for a in node.args]

            if node.func_name in ("CAGR", "rolling_avg", "rolling_max", "rolling_min", "lag"):
                # 这些函数需要额外的数字参数
                # 简化: 使用第2个参数作为 n
                n = "4"  # 默认
                if len(node.args) >= 2:
                    n = args_sql[1]
                template = self.FUNC_SQL.get(node.func_name, "{arg}")
                return template.replace("{arg}", args_sql[0] if args_sql else "1").replace("{n}", n)

            template = self.FUNC_SQL.get(node.func_name, "{arg}")
            arg_str = args_sql[0] if args_sql else "1"
            return template.replace("{arg}", arg_str)

        return "NULL"
