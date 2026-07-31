"""筛选引擎 — 规则 JSON → DuckDB SQL 执行

两阶段执行策略 (TECH_PLAN §2.3.3, 审查问题3修订):
1. 基础股票池: 从 stock_meta 出发，按 ST/停牌/上市年限过滤 + per-stock 最新快照 (LEFT JOIN LATERAL)
2. 横截面排名: DuckDB 窗口函数 (全市场 + 申万行业)
3. 条件过滤: 应用用户条件 (DSL CodeGen 生成，M3 用内建指标子集)

PRD §12.3 (SC11): 排名分母只由基础股票池预设决定，
用户添加的筛选条件不得重新计算或缩小排名分母。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore
from app.core.dsl.codegen import quote_identifier
from app.core.dsl.registry import validate_expression_identifier

logger = logging.getLogger(__name__)

# ─── 操作符 → SQL 映射 ─────────────────────────────────────────────

OP_MAP: dict[str, str] = {
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "=": "=",
    "!=": "!=",
    "between": "BETWEEN",
    "in": "IN",
    "is_null": "IS NULL",
    "is_not_null": "IS NOT NULL",
}

# 指标快照表中可用的指标列
SNAPSHOT_COLUMNS: set[str] = {
    "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "dividend_yield",
    "total_market_cap", "circ_market_cap",
    "roe", "roa", "gross_margin", "net_margin", "roic", "cf_to_net_profit",
    "revenue_yoy", "net_profit_yoy", "deducted_profit_yoy",
    "revenue_cagr3", "revenue_cagr5", "net_profit_cagr3", "net_profit_cagr5",
    "deducted_profit_cagr3", "deducted_profit_cagr5",
    "debt_ratio", "current_ratio", "quick_ratio",
    "interest_bearing_debt", "interest_coverage", "goodwill_ratio",
    "payout_ratio", "dps", "consecutive_div_years",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "latest_close", "turnover_rate", "avg_volume", "period_return",
    "annualized_volatility", "max_drawdown",
}

# Every normalized statement column is available to screening under its stable
# DSL-style name. SQL only ever sees the generated internal alias below.
STATEMENT_FIELDS: dict[str, set[str]] = {
    "balance": {
        "monetary_funds", "trading_financial_assets", "notes_receivable", "accounts_receivable",
        "prepayments", "other_receivables", "inventory", "contract_assets", "total_current_assets",
        "long_term_equity_investment", "fixed_assets", "construction_in_progress", "right_of_use_assets",
        "intangible_assets", "goodwill", "deferred_tax_assets", "total_non_current_assets", "total_assets",
        "short_term_loans", "notes_payable", "accounts_payable", "prepayments_received",
        "contract_liabilities", "employee_benefits_payable", "taxes_payable", "total_current_liabilities",
        "long_term_loans", "bonds_payable", "lease_liabilities", "total_non_current_liabilities",
         "total_liabilities", "paid_in_capital", "capital_reserve", "surplus_reserve",
         "undistributed_profit", "minority_interest", "total_equity", "total_equity_parent",
         "core_tier1_capital_adequacy_ratio", "tier1_capital_adequacy_ratio",
         "capital_adequacy_ratio", "non_performing_loan_ratio", "provision_coverage_ratio",
         "risk_coverage_ratio",
    },
    "income": {
        "total_operating_revenue", "revenue", "total_operating_cost", "cost_of_revenue",
        "taxes_and_surcharges", "selling_expenses", "administrative_expenses", "rd_expenses",
        "financial_expenses", "interest_expense", "interest_income", "asset_impairment_loss",
        "credit_impairment_loss", "exchange_gain", "investment_income", "operating_profit",
        "non_operating_income", "non_operating_expenses", "total_profit", "income_tax", "net_profit",
        "parent_net_profit", "minority_shareholder_profit", "deducted_net_profit", "basic_eps", "diluted_eps",
    },
    "cashflow": {
        "cash_received_sales", "taxes_refunded", "other_operating_cf_in", "total_operating_cf_in",
        "cash_paid_goods", "cash_paid_employees", "cash_paid_taxes", "other_operating_cf_out",
        "total_operating_cf_out", "cf_from_operating", "cf_from_investing", "cf_from_financing",
        "exchange_rate_effect", "cf_net", "cash_beginning", "cash_ending",
    },
}
NORMALIZED_FIELDS = {f"{table}.{field}" for table, fields in STATEMENT_FIELDS.items() for field in fields}

# 排名指标（需要计算横截面排名的指标）
RANKABLE_INDICATORS: set[str] = {
    "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "dividend_yield",
    "roe", "roa", "gross_margin", "net_margin", "roic",
    "revenue_yoy", "net_profit_yoy",
    "debt_ratio", "current_ratio", "quick_ratio",
    "payout_ratio", "dps", "consecutive_div_years",
    "total_market_cap", "latest_close",
} | NORMALIZED_FIELDS

RANK_SUFFIXES = (
    "_market_rank", "_market_percentile",
    "_industry_rank", "_industry_percentile",
    "_sw1_rank", "_sw1_percentile", "_sw2_rank", "_sw2_percentile",
)
METADATA_COLUMNS = {"stock_code", "name", "exchange", "sw_level1", "sw_level2"}
MAX_RULE_LEAVES = 100
MAX_IN_VALUES = 1_000


class ScreeningEngine:
    """筛选引擎

    接收规则 JSON，生成并执行 DuckDB SQL，返回筛选结果。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if duck is None and paths is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if duck is None and paths is None:
            raise PathIsolationError("ScreeningEngine requires a DuckDB store or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path:
                raise PathIsolationError("ScreeningEngine store does not match injected paths")

        assert duck is not None
        self.duck = duck
        self.sqlite = sqlite
        self._custom_fields: set[str] = set()

    def run(
        self,
        rule: dict[str, Any],
        include_st: bool = False,
        include_suspended: bool = False,
        min_listing_years: int = 1,
        strict_only: bool = False,
        locked_indicators: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """执行筛选

        Args:
            rule: 规则 JSON (conditions + sort + columns)
            include_st: 是否包含 ST/*ST 股票
            include_suspended: 是否包含停牌股票
            min_listing_years: 最低上市年限

        Returns:
            {
                "results": [...],
                "total": int,
                "execution_time_ms": float,
                "base_pool_size": int,
                "data_date": str,
            }
        """
        start_time = time.monotonic()

        self._require_known_pool_status(
            include_st,
            include_suspended,
            min_listing_years,
        )

        # 1. 解析规则
        conditions = rule.get("conditions", {})
        sort_spec = rule.get("sort", [])
        columns_spec = rule.get("columns", [])

        compiled_indicators = self._compile_published_indicators(locked_indicators or {})
        self._custom_fields = set(compiled_indicators)

        # 2. 提取条件中引用的排名指标
        strict_referenced_fields = self._extract_fields(conditions)
        strict_referenced_fields.update(item.get("field", "") for item in sort_spec)
        referenced_fields = strict_referenced_fields | set(columns_spec)
        rank_fields = {
            base for field in referenced_fields
            if (base := self._rank_base(field)) is not None
        }
        strict_fields = {
            base
            for field in referenced_fields
            for base in (field, self._rank_base(field))
            if base in SNAPSHOT_COLUMNS or base in NORMALIZED_FIELDS or base in self._custom_fields
        }

        self._reject_mixed_report_dates()

        # 3. 构建 SQL
        sql, params = self._build_sql(
            conditions=conditions,
            sort_spec=sort_spec,
            columns_spec=columns_spec,
            include_st=include_st,
            include_suspended=include_suspended,
            min_listing_years=min_listing_years,
            rank_fields=rank_fields,
            strict_fields=strict_fields if strict_only else set(),
            compiled_indicators=compiled_indicators,
        )

        # 4. 执行
        results = self.duck.read_query(sql, params)
        execution_time = (time.monotonic() - start_time) * 1000

        # 5. 获取基础池大小（用于报告）
        base_pool_size = self._get_base_pool_size(
            include_st, include_suspended, min_listing_years
        )

        # 6. 获取数据日期
        data_date = self._get_latest_data_date()

        logger.info(
            f"筛选完成: {len(results)} 条结果, "
            f"基础池 {base_pool_size} 只, "
            f"耗时 {execution_time:.1f}ms"
        )

        return {
            "results": results,
            "total": len(results),
            "execution_time_ms": round(execution_time, 1),
            "base_pool_size": base_pool_size,
            "data_date": data_date,
            "strict_only": strict_only,
            "strict_fields": sorted(strict_fields),
            "locked_indicators": locked_indicators or {},
        }

    def _build_sql(
        self,
        conditions: dict,
        sort_spec: list[dict],
        columns_spec: list[str],
        include_st: bool,
        include_suspended: bool,
        min_listing_years: int,
        rank_fields: set[str],
        strict_fields: set[str],
        compiled_indicators: dict[str, str],
    ) -> tuple[str, list[Any]]:
        """构建完整的筛选 SQL

        策略 (审查问题3修订):
        - 基础池从 stock_meta 出发（不依赖 indicator_snapshot 的 report_date）
        - per-stock 最新快照用 LEFT JOIN LATERAL
        - 排名窗口函数作用于过滤后的基础池
        - 用户条件在排名之后应用
        """
        params: list[Any] = []

        # ─── 阶段1: 基础池 + per-stock 最新快照 ──────────────────
        sql_parts: list[str] = []

        # 基础池过滤条件
        pool_conditions: list[str] = ["m.is_listed IS TRUE"]
        if not include_st:
            pool_conditions.append("m.is_st = false")
        if not include_suspended:
            pool_conditions.append("m.is_suspended = false")
        if min_listing_years > 0:
            pool_conditions.append(
                f"m.listing_date <= CURRENT_DATE - INTERVAL '{min_listing_years}' YEAR"
            )

        pool_where = " AND ".join(pool_conditions) if pool_conditions else "1=1"

        # Statement values are pinned to the snapshot period. Selecting each
        # table's independent latest row would create fabricated cross-period ratios.
        aliases = {"balance": "bs", "income": "ic", "cashflow": "cf"}
        overrides = self._published_statement_overrides()
        normalized_selects = [
            self._override_aware_field_sql(table, field, aliases[table], bool(overrides))
            for table, fields in STATEMENT_FIELDS.items()
            for field in sorted(fields)
        ]
        if overrides:
            placeholders = ", ".join("(?, ?, ?, ?)" for _ in overrides)
            sql_parts.append(
                "WITH overrides(stock_code, report_date, field_name, override_value) AS "
                f"(VALUES {placeholders}),"
            )
            params.extend(value for row in overrides for value in row)
        else:
            sql_parts.append("WITH")
        sql_parts.append(f"""
base_pool AS (
    SELECT
        m.stock_code, m.name, m.pinyin, m.exchange, m.sw_level1, m.sw_level2,
        m.is_st, m.is_suspended, m.listing_date, s.*, {', '.join(normalized_selects)}
    FROM stock_meta m
    LEFT JOIN LATERAL (
        SELECT * FROM indicator_snapshot s
        WHERE s.stock_code = m.stock_code ORDER BY s.report_date DESC LIMIT 1
    ) s ON true
    LEFT JOIN LATERAL (
        SELECT * FROM balance_sheet bs
        WHERE bs.stock_code = m.stock_code AND bs.report_date = s.report_date
    ) bs ON true
    LEFT JOIN LATERAL (
        SELECT * FROM income_statement ic
        WHERE ic.stock_code = m.stock_code AND ic.report_date = s.report_date
    ) ic ON true
    LEFT JOIN LATERAL (
        SELECT * FROM cash_flow cf
        WHERE cf.stock_code = m.stock_code AND cf.report_date = s.report_date
    ) cf ON true
    WHERE {pool_where}
)""")

        if compiled_indicators:
            sql_parts.append(
                ", computed AS (SELECT base_pool.*, "
                + ", ".join(
                    f"{expression} AS {quote_identifier(name)}"
                    for name, expression in compiled_indicators.items()
                )
                + " FROM base_pool)"
            )
            rank_source = "computed"
        else:
            rank_source = "base_pool"

        # ─── 阶段2: 横截面排名 ──────────────────────────────────
        # P1-21修复: NULLS LAST 避免NULL值获得rank=1
        # P1-22修复: sw_level1为NULL时行业排名返回NULL (PRD §12.4)
        rank_cols: list[str] = []
        if rank_fields:
            for field in sorted(rank_fields):
                # 全市场排名 (NULLS LAST)
                sql_field = self._field_sql_name(field)
                rank_cols.append(
                    f"CASE WHEN {sql_field} IS NULL THEN NULL ELSE "
                    f"RANK() OVER (ORDER BY {sql_field} NULLS LAST) END AS {self._field_sql_name(field + '_market_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN {sql_field} IS NULL THEN NULL ELSE "
                    f"PERCENT_RANK() OVER (ORDER BY {sql_field} NULLS LAST) END AS {self._field_sql_name(field + '_market_percentile')}"
                )
                # 申万一级排名 (sw_level1为NULL时返回NULL, PRD §12.4)
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY sw_level1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_industry_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY sw_level1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_industry_percentile')}"
                )
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY sw_level1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw1_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY sw_level1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw1_percentile')}"
                )
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL OR sw_level2 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY sw_level1, sw_level2 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw2_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL OR sw_level2 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY sw_level1, sw_level2 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw2_percentile')}"
                )

        if rank_cols:
            sql_parts.append(f"""
, ranked AS (
    SELECT {rank_source}.*,
        {', '.join(rank_cols)}
    FROM {rank_source}
)""")
            source_table = "ranked"
        else:
            source_table = rank_source

        # ─── 阶段3: 条件过滤 ────────────────────────────────────
        where_clause, where_params = self._build_where(conditions)
        params.extend(where_params)
        strict_clause = self._build_strict_clause(strict_fields, source_table)
        if strict_clause:
            where_clause = f"({where_clause}) AND ({strict_clause})"

        # ─── 排序 ────────────────────────────────────────────────
        order_clause = self._build_order(sort_spec)

        # ─── 列选择 ──────────────────────────────────────────────
        select_cols = self._build_select(columns_spec, rank_fields)

        sql_parts.append(f"""
SELECT {select_cols}
FROM {source_table}
WHERE {where_clause if where_clause else '1=1'}
{order_clause}
LIMIT 5000
""")

        return "".join(sql_parts), params

    def _build_where(self, node: dict, level: int = 0) -> tuple[str, list[Any]]:
        """递归构建 WHERE 子句

        支持嵌套 AND/OR (最多3层, PRD SC3)
        """
        if level > 3:
            raise ValueError("规则嵌套超过3层限制 (PRD §12.2)")

        if not isinstance(node, dict):
            raise ValueError("规则节点必须是对象")
        logic = node.get("logic", "AND").upper()
        if logic not in {"AND", "OR"}:
            raise ValueError("规则逻辑只能是 AND 或 OR")
        rules = node.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("规则 rules 必须是数组")

        if not rules:
            return "1=1", []
        if self._count_rule_leaves(node) > MAX_RULE_LEAVES:
            raise ValueError(f"筛选条件超过{MAX_RULE_LEAVES}项限制")

        parts: list[str] = []
        params: list[Any] = []

        for rule in rules:
            if "logic" in rule:
                # 嵌套节点
                sub_where, sub_params = self._build_where(rule, level + 1)
                if sub_where != "1=1":
                    parts.append(f"({sub_where})")
                    params.extend(sub_params)
            else:
                # 叶子条件
                cond_sql, cond_params = self._build_condition(rule)
                parts.append(cond_sql)
                params.extend(cond_params)

        if not parts:
            raise ValueError("规则组不能为空")

        joiner = f" {logic} "
        return joiner.join(parts), params

    def _build_condition(self, cond: dict) -> tuple[str, list[Any]]:
        """构建单个条件"""
        if not isinstance(cond, dict):
            raise ValueError("筛选条件必须是对象")
        field = cond.get("field", "")
        op = cond.get("op", "")
        value = cond.get("value")
        right_field = cond.get("right_field")

        # 验证字段名安全（防注入）
        if not self._is_known_field(field):
            raise ValueError(f"未知筛选字段: {field}")
        if right_field is not None:
            if op in {"between", "in", "is_null", "is_not_null"}:
                raise ValueError(f"{op} 条件不能比较另一字段")
            if not isinstance(right_field, str) or not self._is_known_field(right_field):
                raise ValueError(f"未知比较字段: {right_field}")
            if not self._compatible_fields(field, right_field):
                raise ValueError(f"不兼容的字段比较: {field} 与 {right_field}")

        sql_op = OP_MAP.get(op)
        if not sql_op:
            raise ValueError(f"未知筛选操作符: {op}")

        if op == "is_null":
            return f"{self._field_sql_name(field)} IS NULL", []
        elif op == "is_not_null":
            return f"{self._field_sql_name(field)} IS NOT NULL", []
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("between 条件必须提供两个值")
            return f"{self._field_sql_name(field)} BETWEEN ? AND ?", [value[0], value[1]]
        elif op == "in":
            if not isinstance(value, list) or not value:
                raise ValueError("in 条件必须提供非空数组")
            if len(value) > MAX_IN_VALUES:
                raise ValueError(f"in 条件最多允许 {MAX_IN_VALUES} 个值")
            placeholders = ", ".join(["?"] * len(value))
            return f"{self._field_sql_name(field)} IN ({placeholders})", value
        else:
            if right_field is not None:
                return f"{self._field_sql_name(field)} {sql_op} {self._field_sql_name(right_field)}", []
            if value is None:
                raise ValueError(f"{op} 条件必须提供值")
            return f"{self._field_sql_name(field)} {sql_op} ?", [value]

    @staticmethod
    def _count_rule_leaves(node: dict) -> int:
        return sum(
            ScreeningEngine._count_rule_leaves(child) if isinstance(child, dict) and "logic" in child else 1
            for child in node.get("rules", [])
        )

    def _build_order(self, sort_spec: list[dict]) -> str:
        """构建 ORDER BY"""
        if not sort_spec:
            return ""
        # P0#13修复: 白名单验证排序字段, 防止 SQL 注入
        # 允许的排序字段: 字母/数字/下划线, 可选表别名前缀 (如 s.stock_code)
        safe_field_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")
        parts: list[str] = []
        for s in sort_spec:
            field = s.get("field", "")
            direction = s.get("direction", "asc").upper()
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            if field and safe_field_pattern.match(field) and self._is_known_field(field):
                parts.append(f"{self._field_sql_name(field)} {direction}")
        if not parts:
            return ""
        return "ORDER BY " + ", ".join(parts)

    def _build_select(self, columns_spec: list[str], rank_fields: set[str]) -> str:
        """构建 SELECT 列"""
        # P0#13修复: 白名单验证列名, 防止 SQL 注入
        safe_col_pattern = re.compile(
            r"^[a-zA-Z_][a-zA-Z0-9_.]*(\s+AS\s+[a-zA-Z_][a-zA-Z0-9_]*)?$",
            re.IGNORECASE,
        )
        if not columns_spec:
            # 默认列
            cols = [
                "stock_code", "name", "exchange", "sw_level1",
                "latest_close", "pe_ttm", "pb_mrq", "roe",
                "gross_margin", "net_margin", "debt_ratio",
            ]
        else:
            # 过滤不安全的列名
            cols = [c for c in columns_spec if safe_col_pattern.match(c.strip()) and self._is_known_field(c)]

        # 确保关键列总是包含
        for essential in ("stock_code", "name"):
            if essential not in cols:
                cols.insert(0, essential)

        return ", ".join(
            column if column in METADATA_COLUMNS or column in SNAPSHOT_COLUMNS
            else f'{self._field_sql_name(column)} AS "{column}"'
            for column in cols
        )

    def _extract_fields(self, node: dict) -> set[str]:
        """递归提取条件中引用的所有字段名"""
        fields: set[str] = set()
        rules = node.get("rules", [])
        for rule in rules:
            if "logic" in rule:
                fields.update(self._extract_fields(rule))
            else:
                f = rule.get("field")
                if f:
                    fields.add(f)
                right_field = rule.get("right_field")
                if right_field:
                    fields.add(right_field)
        return fields

    def _rank_base(self, field: str) -> str | None:
        """Return the base indicator for a generated rank field, if valid."""
        for suffix in RANK_SUFFIXES:
            if field.endswith(suffix):
                base = field.removesuffix(suffix)
                return base if base in RANKABLE_INDICATORS or base in self._custom_fields else None
        return None

    @staticmethod
    def _field_sql_name(field: str) -> str:
        """Map a validated public field to its generated SQL identifier."""
        return field.replace(".", "__")

    def _is_known_field(self, field: str) -> bool:
        return (
            field in SNAPSHOT_COLUMNS
            or field in NORMALIZED_FIELDS
            or field in METADATA_COLUMNS
            or field in self._custom_fields
            or self._rank_base(field) is not None
        )

    def _published_statement_overrides(self) -> list[tuple[str, str, str, float]]:
        if self.sqlite is None:
            return []
        statement_fields = {field for fields in STATEMENT_FIELDS.values() for field in fields}
        return [
            (row["stock_code"], str(row["report_date"])[:10], row["field_name"], row["override_value"])
            for row in self.sqlite.query(
                """SELECT stock_code, report_date, field_name, override_value FROM manual_overrides
                   WHERE status = 'published' AND rolled_back_at IS NULL AND report_date IS NOT NULL"""
            )
            if row["field_name"] in statement_fields
        ]

    def _override_aware_field_sql(
        self, table: str, field: str, alias: str, has_overrides: bool,
    ) -> str:
        if has_overrides:
            expression = (
                f"COALESCE((SELECT override_value FROM overrides "
                f"WHERE stock_code = m.stock_code AND report_date = s.report_date "
                f"AND field_name = '{field}' LIMIT 1), {alias}.{field})"
            )
        else:
            expression = f"{alias}.{field}"
        return f"{expression} AS {self._field_sql_name(f'{table}.{field}')}"

    def _compile_published_indicators(
        self, locked_indicators: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Compile only the immutable, server-locked subset of DSL expressions."""
        if not locked_indicators:
            return {}
        if self.sqlite is None:
            raise ValueError("published DSL indicators require an injected SQLite store")
        compiled: dict[tuple[str, int, str], str] = {}
        active: set[tuple[str, int, str]] = set()

        def compile_expression(name: str, version: int, expected_hash: str | None = None) -> str:
            validate_expression_identifier(name)
            rows = self.sqlite.query(
                """SELECT id, expression_text, content_hash FROM dsl_expressions
                   WHERE name = ? AND version = ? AND status = 'published'""",
                [name, version],
            )
            if not rows:
                raise ValueError(f"published DSL indicator is unavailable: {name} v{version}")
            expression = rows[0]
            if expected_hash and expression["content_hash"] != expected_hash:
                raise ValueError(f"published DSL indicator content changed: {name} v{version}")
            key = (name, version, expression["content_hash"])
            if key in compiled:
                return compiled[key]
            if key in active:
                raise ValueError(f"published DSL dependency cycle: {name} v{version}")
            active.add(key)
            from app.core.dsl.ast_nodes import BinaryOp, FieldRef, FuncCall, IndicatorRef, Literal, UnaryOp
            from app.core.dsl.engine import expand_shorthand
            from app.core.dsl.parser import parse

            dependencies = self.sqlite.query(
                """SELECT dependency.name, dependency.version, dependency.content_hash
                   FROM dsl_dependencies link
                   JOIN dsl_expressions dependency ON dependency.id = link.depends_on_id
                   WHERE link.expression_id = ?""",
                [expression["id"]],
            )
            dependency_versions = {row["name"]: row for row in dependencies}

            def compile_node(node: Any) -> str:
                if isinstance(node, Literal):
                    return str(node.value)
                if isinstance(node, FieldRef):
                    if node.period != "LATEST":
                        raise ValueError(
                            f"screening currently requires a materialized current value for {name}; "
                            f"unsupported period {node.period}"
                        )
                    field = f"{node.table}.{node.field}"
                    if field not in NORMALIZED_FIELDS:
                        raise ValueError(f"unknown normalized DSL field: {field}")
                    return self._field_sql_name(field)
                if isinstance(node, IndicatorRef):
                    if node.name in SNAPSHOT_COLUMNS:
                        return node.name
                    dependency = dependency_versions.get(node.name)
                    if dependency is None:
                        raise ValueError(f"unlocked DSL dependency: {node.name}")
                    return f"({compile_expression(node.name, dependency['version'], dependency['content_hash'])})"
                if isinstance(node, UnaryOp):
                    return f"(-{compile_node(node.operand)})"
                if isinstance(node, BinaryOp):
                    left, right = compile_node(node.left), compile_node(node.right)
                    return f"({left} / NULLIF({right}, 0))" if node.op == "/" else f"({left} {node.op} {right})"
                if isinstance(node, FuncCall):
                    if node.func_name in {"CAGR", "rolling_avg", "rolling_max", "rolling_min", "lag", "avg", "max", "min"}:
                        raise ValueError(
                            f"screening historical DSL planner is required for function: {node.func_name}"
                        )
                    if len(node.args) != 1:
                        raise ValueError(f"DSL function requires one argument: {node.func_name}")
                    argument = compile_node(node.args[0])
                    if node.func_name == "rank":
                        return f"CASE WHEN {argument} IS NULL THEN NULL ELSE RANK() OVER (ORDER BY {argument} NULLS LAST) END"
                    if node.func_name == "rank_industry":
                        return (
                            f"CASE WHEN sw_level1 IS NULL OR {argument} IS NULL THEN NULL ELSE "
                            f"RANK() OVER (PARTITION BY sw_level1 ORDER BY {argument} NULLS LAST) END"
                        )
                    if node.func_name == "percentile":
                        return f"CASE WHEN {argument} IS NULL THEN NULL ELSE PERCENT_RANK() OVER (ORDER BY {argument} NULLS LAST) END"
                    if node.func_name == "zscore":
                        return (
                            f"(({argument} - AVG({argument}) OVER ()) / "
                            f"NULLIF(STDDEV({argument}) OVER (), 0))"
                        )
                    if node.func_name == "normalize":
                        return (
                            f"(({argument} - MIN({argument}) OVER ()) / "
                            f"NULLIF(MAX({argument}) OVER () - MIN({argument}) OVER (), 0))"
                        )
                    raise ValueError(f"unsupported DSL function: {node.func_name}")
                raise ValueError("unsupported DSL AST node")

            compiled[key] = compile_node(parse(expand_shorthand(expression["expression_text"])))
            active.remove(key)
            return compiled[key]

        for name, lock in locked_indicators.items():
            version = lock.get("version")
            content_hash = lock.get("content_hash")
            if not isinstance(name, str) or not isinstance(version, int) or not isinstance(content_hash, str):
                raise ValueError("invalid locked DSL indicator reference")
            compile_expression(name, version, content_hash)
        return {
            name: compile_expression(name, lock["version"], lock["content_hash"])
            for name, lock in locked_indicators.items()
        }

    @staticmethod
    def _compatible_fields(left: str, right: str) -> bool:
        """Only compare numeric data fields; metadata and rank values are not interchangeable."""
        return left not in METADATA_COLUMNS and right not in METADATA_COLUMNS

    def _build_strict_clause(self, strict_fields: set[str], source_table: str) -> str:
        """Require a latest, field-level strict audit record for every used metric.

        Derived metrics without a recorded field-level source deliberately fail this
        predicate: strict mode may omit data, but must never claim unsupported
        confidence.
        """
        clauses: list[str] = []
        for field in sorted(strict_fields):
            clauses.append(
                "COALESCE((SELECT audit.confidence = 'strict' AND audit.value IS NOT DISTINCT FROM "
                f"{source_table}.{self._field_sql_name(field)} FROM source_audit audit "
                f"WHERE audit.stock_code = {source_table}.stock_code AND audit.field_name = '{self._audit_field_name(field)}' "
                f"AND audit.report_date = {source_table}.report_date "
                "ORDER BY fetch_time DESC, id DESC LIMIT 1), false)"
            )
        return " AND ".join(clauses)

    def _reject_mixed_report_dates(self) -> None:
        """Fail closed when a snapshot would otherwise combine newer statement rows."""
        rows = self.duck.read_query("""
            SELECT m.stock_code, s.report_date AS snapshot_date,
                   bs.latest_date AS balance_date, ic.latest_date AS income_date,
                   cf.latest_date AS cashflow_date
            FROM stock_meta m
            LEFT JOIN LATERAL (
                SELECT report_date FROM indicator_snapshot
                WHERE stock_code = m.stock_code
                ORDER BY report_date DESC LIMIT 1
            ) s ON true
            LEFT JOIN LATERAL (
                SELECT MAX(report_date) AS latest_date FROM balance_sheet
                WHERE stock_code = m.stock_code
            ) bs ON true
            LEFT JOIN LATERAL (
                SELECT MAX(report_date) AS latest_date FROM income_statement
                WHERE stock_code = m.stock_code
            ) ic ON true
            LEFT JOIN LATERAL (
                SELECT MAX(report_date) AS latest_date FROM cash_flow
                WHERE stock_code = m.stock_code
            ) cf ON true
            WHERE m.is_listed IS TRUE AND s.report_date IS NOT NULL
              AND (
                  (bs.latest_date IS NOT NULL AND bs.latest_date <> s.report_date)
                  OR (ic.latest_date IS NOT NULL AND ic.latest_date <> s.report_date)
                  OR (cf.latest_date IS NOT NULL AND cf.latest_date <> s.report_date)
              )
            LIMIT 1
        """)
        if rows:
            row = rows[0]
            raise ValueError(
                "mixed snapshot/statement report dates for "
                f"{row['stock_code']}: snapshot={row['snapshot_date']}, "
                f"balance={row['balance_date']}, income={row['income_date']}, "
                f"cashflow={row['cashflow_date']}"
            )

    @staticmethod
    def _audit_field_name(field: str) -> str:
        return field.split(".", 1)[-1]

    def _get_base_pool_size(
        self, include_st: bool, include_suspended: bool, min_listing_years: int
    ) -> int:
        """获取基础池大小"""
        conds: list[str] = ["is_listed IS TRUE"]
        if not include_st:
            conds.append("is_st = false")
        if not include_suspended:
            conds.append("is_suspended = false")
        if min_listing_years > 0:
            conds.append(
                f"listing_date <= CURRENT_DATE - INTERVAL '{min_listing_years}' YEAR"
            )
        where = " AND ".join(conds) if conds else "1=1"
        rows = self.duck.read_query(
            f"SELECT COUNT(*) as cnt FROM stock_meta WHERE {where}"
        )
        return rows[0]["cnt"] if rows else 0

    def _require_known_pool_status(
        self,
        include_st: bool,
        include_suspended: bool,
        min_listing_years: int,
    ) -> None:
        """Reject an unknown current-pool property instead of silently filtering it out."""
        unknown_conditions: list[str] = []
        if not include_st:
            unknown_conditions.append("is_st IS NULL")
        if not include_suspended:
            unknown_conditions.append("is_suspended IS NULL")
        if min_listing_years > 0:
            unknown_conditions.append("listing_date IS NULL")
        if not unknown_conditions:
            return
        unknown_count = self.duck.read_query(
            "SELECT COUNT(*) AS count FROM stock_meta "
            f"WHERE is_listed IS TRUE AND ({' OR '.join(unknown_conditions)})"
        )[0]["count"]
        if unknown_count:
            raise ValueError(
                f"base pool metadata is incomplete for {unknown_count} listed stocks; "
                "refresh verified listing metadata before screening"
            )

    def _get_latest_data_date(self) -> str | None:
        """获取最新数据日期"""
        rows = self.duck.read_query(
            "SELECT MAX(report_date) as latest FROM indicator_snapshot"
        )
        if rows and rows[0]["latest"]:
            return str(rows[0]["latest"])
        return None

    def generate_entry_explanation(
        self, stock: dict, conditions: dict
    ) -> str:
        """为入选股票生成入选解释 (PRD §12.5 SC13)

        说明该股票满足哪些条件。
        """
        explanations: list[str] = []
        rules = conditions.get("rules", [])
        logic = conditions.get("logic", "AND")

        for rule in rules:
            if "logic" in rule:
                # 嵌套条件
                sub_expl = self.generate_entry_explanation(stock, rule)
                if sub_expl:
                    explanations.append(f"({sub_expl})")
            else:
                field = rule.get("field", "")
                op = rule.get("op", "")
                value = rule.get("value")
                actual = stock.get(field)

                if actual is not None:
                    explanations.append(
                        f"{field} {op} {value} (实际: {actual:.4f})"
                        if isinstance(actual, float)
                        else f"{field} {op} {value} (实际: {actual})"
                    )

        return f" {logic} ".join(explanations)
