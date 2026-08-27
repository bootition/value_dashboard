"""DSL SQL 代码生成 — 将验证后的 AST 转换为 DuckDB SQL

生成的 SQL 可在 indicator_snapshot 表上执行。
"""

from __future__ import annotations

import logging
import re

from app.core.dsl.ast_nodes import (
    FIELD_METADATA,
    ASTNode,
    BinaryOp,
    FieldRef,
    FuncCall,
    IndicatorRef,
    Literal,
    UnaryOp,
)

logger = logging.getLogger(__name__)
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def quote_identifier(identifier: str) -> str:
    """Quote a validated SQL identifier; values must never become SQL structure."""
    if not isinstance(identifier, str) or not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier}")
    return f'"{identifier}"'


class UnsupportedPeriodFunctionError(ValueError):
    """Raised when an expression requires historical rows unavailable to code generation."""


class CodeGen:
    """DuckDB SQL 代码生成器"""

    # 函数 → SQL 模板
    FUNC_SQL: dict[str, str] = {
        "rank": "RANK() OVER (ORDER BY {arg})",
        "rank_industry": "RANK() OVER (PARTITION BY csrc_l1 ORDER BY {arg})",
        "percentile": "PERCENT_RANK() OVER (ORDER BY {arg})",
        "zscore": "({arg} - AVG({arg}) OVER ()) / NULLIF(STDDEV({arg}) OVER (), 0)",
        "normalize": "({arg} - MIN({arg}) OVER ()) / NULLIF((MAX({arg}) OVER () - MIN({arg}) OVER ()), 0)",
        "TTM": "{arg}",  # TTM 在数据层已处理，这里透传
        "YoY": "LAG({arg}, 4) OVER (PARTITION BY stock_code ORDER BY report_date)",
        "QoQ": "LAG({arg}, 1) OVER (PARTITION BY stock_code ORDER BY report_date)",
        "CAGR": "POWER({arg} / NULLIF(LAG({arg}, {n}) OVER (PARTITION BY stock_code ORDER BY report_date), 0), 1.0/{n}) - 1",
        "rolling_avg": "AVG({arg}) OVER (PARTITION BY stock_code ORDER BY report_date ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)",
        "rolling_max": "MAX({arg}) OVER (PARTITION BY stock_code ORDER BY report_date ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)",
        "rolling_min": "MIN({arg}) OVER (PARTITION BY stock_code ORDER BY report_date ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)",
        "lag": "LAG({arg}, {n}) OVER (PARTITION BY stock_code ORDER BY report_date)",
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
        return f"{sql} AS {quote_identifier(alias)}"

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
        return f"SELECT s.stock_code, {expr_sql} AS {quote_identifier(alias)}\nFROM {from_clause}\n{where}"

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
            return self._gen_field(node, node.period)

        if isinstance(node, IndicatorRef):
            # indicator_ref → indicator_snapshot 中的列名
            return quote_identifier(node.name)

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
                if len(node.args) != 1 or not isinstance(node.args[0], FieldRef):
                    raise UnsupportedPeriodFunctionError(
                        f"{node.func_name} requires exactly one normalized financial field"
                    )
                return self._gen_field(node.args[0], node.func_name)
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

    def _gen_field(self, field: FieldRef, period: str) -> str:
        """Generate a field expression relative to the snapshot report date.

        Snapshot rows provide the current reporting period. Historical periods
        are correlated by stock code and exact report dates, so missing reports
        produce NULL rather than an order-dependent approximation.
        """
        aliases = {"balance": "bs", "income": "ic", "cashflow": "cf"}
        tables = {
            "balance": "balance_sheet",
            "income": "income_statement",
            "cashflow": "cash_flow",
        }
        alias = aliases.get(field.table, "s")
        table = tables.get(field.table)
        current = f"{alias}.{field.field}"
        if table is None:
            return current
        is_cumulative = FIELD_METADATA.get(
            f"{field.table}.{field.field}", {}
        ).get("period_type") == "cumulative"
        if period == "LATEST":
            return current
        if period == "MRQ":
            return self._quarter_value_sql(table, field.field, alias) if is_cumulative else current
        if period == "TTM":
            if field.table == "balance":
                raise UnsupportedPeriodFunctionError("TTM requires a cumulative flow field")
            return self._ttm_sql(table, field.field, current)
        if period == "YoY":
            if is_cumulative:
                current_quarter = self._quarter_value_sql(table, field.field, alias)
                prior_quarter = self._prior_year_same_quarter_value_sql(table, field.field)
                return f"(({current_quarter} - ({prior_quarter})) / NULLIF(ABS(({prior_quarter})), 0))"
            prior = self._prior_year_same_period_sql(table, field.field)
            return f"(({current} - ({prior})) / NULLIF(ABS(({prior})), 0))"
        if period == "QoQ":
            if field.table == "balance":
                raise UnsupportedPeriodFunctionError("QoQ requires a cumulative flow field")
            current_quarter = self._quarter_value_sql(table, field.field, alias)
            prior_quarter = self._prior_quarter_value_sql(table, field.field)
            return f"(({current_quarter} - ({prior_quarter})) / NULLIF(ABS(({prior_quarter})), 0))"
        raise UnsupportedPeriodFunctionError(f"unsupported field period: {period}")

    @staticmethod
    def _prior_year_same_period_sql(table: str, field: str) -> str:
        return (
            f"SELECT prior.{field} FROM {table} prior "
            "WHERE prior.stock_code = s.stock_code "
            "AND prior.report_date = MAKE_DATE("
            "CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER) - 1, "
            "CAST(EXTRACT(MONTH FROM s.report_date) AS INTEGER), "
            "CAST(EXTRACT(DAY FROM s.report_date) AS INTEGER))"
        )

    @staticmethod
    def _prior_exact_quarter_sql(table: str, field: str) -> str:
        return (
            f"SELECT prior.{field} FROM {table} prior "
            "WHERE prior.stock_code = s.stock_code "
            f"AND prior.report_date = {CodeGen._prior_quarter_end_sql('s.report_date')}"
        )

    @staticmethod
    def _prior_quarter_end_sql(date_expression: str) -> str:
        """Return the exact preceding fiscal quarter end, preserving month lengths."""
        return (
            f"CASE EXTRACT(MONTH FROM {date_expression}) "
            f"WHEN 3 THEN MAKE_DATE(CAST(EXTRACT(YEAR FROM {date_expression}) AS INTEGER) - 1, 12, 31) "
            f"WHEN 6 THEN MAKE_DATE(CAST(EXTRACT(YEAR FROM {date_expression}) AS INTEGER), 3, 31) "
            f"WHEN 9 THEN MAKE_DATE(CAST(EXTRACT(YEAR FROM {date_expression}) AS INTEGER), 6, 30) "
            f"WHEN 12 THEN MAKE_DATE(CAST(EXTRACT(YEAR FROM {date_expression}) AS INTEGER), 9, 30) "
            "ELSE NULL END"
        )

    @staticmethod
    def _quarter_value_sql(table: str, field: str, alias: str) -> str:
        return (
            f"CASE EXTRACT(MONTH FROM s.report_date) "
            f"WHEN 3 THEN {alias}.{field} "
            f"WHEN 6 THEN {alias}.{field} - (SELECT q1.{field} FROM {table} q1 "
            "WHERE q1.stock_code = s.stock_code "
            "AND q1.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER), 3, 31)) "
            f"WHEN 9 THEN {alias}.{field} - (SELECT q2.{field} FROM {table} q2 "
            "WHERE q2.stock_code = s.stock_code "
            "AND q2.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER), 6, 30)) "
            f"WHEN 12 THEN {alias}.{field} - (SELECT q3.{field} FROM {table} q3 "
            "WHERE q3.stock_code = s.stock_code "
            "AND q3.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER), 9, 30)) "
            "ELSE NULL END"
        )

    @staticmethod
    def _prior_quarter_value_sql(table: str, field: str) -> str:
        return (
            "SELECT CASE EXTRACT(MONTH FROM prior.report_date) "
            f"WHEN 3 THEN prior.{field} "
            f"WHEN 6 THEN prior.{field} - (SELECT q1.{field} FROM {table} q1 "
            "WHERE q1.stock_code = prior.stock_code "
            "AND q1.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM prior.report_date) AS INTEGER), 3, 31)) "
            f"WHEN 9 THEN prior.{field} - (SELECT q2.{field} FROM {table} q2 "
            "WHERE q2.stock_code = prior.stock_code "
            "AND q2.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM prior.report_date) AS INTEGER), 6, 30)) "
            f"WHEN 12 THEN prior.{field} - (SELECT q3.{field} FROM {table} q3 "
            "WHERE q3.stock_code = prior.stock_code "
            "AND q3.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM prior.report_date) AS INTEGER), 9, 30)) "
            "ELSE NULL END "
            f"FROM {table} prior WHERE prior.stock_code = s.stock_code "
            f"AND prior.report_date = {CodeGen._prior_quarter_end_sql('s.report_date')}"
        )

    @staticmethod
    def _prior_year_same_quarter_value_sql(table: str, field: str) -> str:
        """Return last year's same-quarter flow, preserving cumulative semantics."""
        return (
            "SELECT CASE EXTRACT(MONTH FROM prior.report_date) "
            f"WHEN 3 THEN prior.{field} "
            f"WHEN 6 THEN prior.{field} - (SELECT q1.{field} FROM {table} q1 "
            "WHERE q1.stock_code = prior.stock_code "
            "AND q1.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM prior.report_date) AS INTEGER), 3, 31)) "
            f"WHEN 9 THEN prior.{field} - (SELECT q2.{field} FROM {table} q2 "
            "WHERE q2.stock_code = prior.stock_code "
            "AND q2.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM prior.report_date) AS INTEGER), 6, 30)) "
            f"WHEN 12 THEN prior.{field} - (SELECT q3.{field} FROM {table} q3 "
            "WHERE q3.stock_code = prior.stock_code "
            "AND q3.report_date = MAKE_DATE(CAST(EXTRACT(YEAR FROM prior.report_date) AS INTEGER), 9, 30)) "
            "ELSE NULL END "
            f"FROM {table} prior WHERE prior.stock_code = s.stock_code "
            "AND prior.report_date = MAKE_DATE("
            "CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER) - 1, "
            "CAST(EXTRACT(MONTH FROM s.report_date) AS INTEGER), "
            "CAST(EXTRACT(DAY FROM s.report_date) AS INTEGER))"
        )

    @staticmethod
    def _ttm_sql(table: str, field: str, current: str) -> str:
        return (
            "CASE WHEN EXTRACT(MONTH FROM s.report_date) = 12 "
            "AND EXTRACT(DAY FROM s.report_date) = 31 "
            f"THEN {current} ELSE ("
            f"SELECT annual.{field} + current_row.{field} - prior.{field} "
            f"FROM {table} current_row "
            f"JOIN {table} annual ON annual.stock_code = current_row.stock_code "
            "AND annual.report_date = MAKE_DATE("
            "CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER) - 1, 12, 31) "
            f"JOIN {table} prior ON prior.stock_code = current_row.stock_code "
            "AND prior.report_date = MAKE_DATE("
            "CAST(EXTRACT(YEAR FROM s.report_date) AS INTEGER) - 1, "
            "CAST(EXTRACT(MONTH FROM s.report_date) AS INTEGER), "
            "CAST(EXTRACT(DAY FROM s.report_date) AS INTEGER)) "
            "WHERE current_row.stock_code = s.stock_code "
            "AND current_row.report_date = s.report_date) END"
        )
