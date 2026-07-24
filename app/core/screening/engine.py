"""筛选引擎 — 规则 JSON → DuckDB SQL 执行

两阶段执行策略 (TECH_PLAN §2.3.3, 审查问题3修订):
1. 基础股票池: 从 stock_meta 出发，按 ST/停牌/上市年限过滤 + per-stock 最新快照 (LEFT JOIN LATERAL)
2. 横截面排名: DuckDB 窗口函数 (全市场 + 申万行业)
3. 条件过滤: 应用用户条件 (DSL CodeGen 生成，M3 用内建指标子集)

PRD §12.3 (SC11): 排名分母只由基础股票池预设决定，
用户添加的筛选条件不得重新计算或缩小排名分母。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore

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
    "latest_close", "turnover_rate",
}

# 排名指标（需要计算横截面排名的指标）
RANKABLE_INDICATORS: set[str] = {
    "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "dividend_yield",
    "roe", "roa", "gross_margin", "net_margin", "roic",
    "revenue_yoy", "net_profit_yoy",
    "debt_ratio", "current_ratio", "quick_ratio",
    "payout_ratio", "dps", "consecutive_div_years",
    "total_market_cap", "latest_close",
}


class ScreeningEngine:
    """筛选引擎

    接收规则 JSON，生成并执行 DuckDB SQL，返回筛选结果。
    """

    def __init__(self) -> None:
        self.duck = DuckDBStore()

    def run(
        self,
        rule: dict[str, Any],
        include_st: bool = False,
        include_suspended: bool = False,
        min_listing_years: int = 1,
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

        # 1. 解析规则
        conditions = rule.get("conditions", {})
        sort_spec = rule.get("sort", [])
        columns_spec = rule.get("columns", [])

        # 2. 提取条件中引用的排名指标
        referenced_fields = self._extract_fields(conditions)
        rank_fields = referenced_fields & RANKABLE_INDICATORS

        # 3. 构建 SQL
        sql, params = self._build_sql(
            conditions=conditions,
            sort_spec=sort_spec,
            columns_spec=columns_spec,
            include_st=include_st,
            include_suspended=include_suspended,
            min_listing_years=min_listing_years,
            rank_fields=rank_fields,
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
        pool_conditions: list[str] = []
        if not include_st:
            pool_conditions.append("m.is_st = false")
        if not include_suspended:
            pool_conditions.append("m.is_suspended = false")
        if min_listing_years > 0:
            pool_conditions.append(
                f"m.listing_date <= CURRENT_DATE - INTERVAL '{min_listing_years}' YEAR"
            )

        pool_where = " AND ".join(pool_conditions) if pool_conditions else "1=1"

        # per-stock 最新快照 (LEFT JOIN LATERAL)
        # 确保即使快照缺失的股票也保留在基础池中 (指标为 NULL)
        sql_parts.append(f"""
WITH base_pool AS (
    SELECT
        m.stock_code,
        m.name,
        m.pinyin,
        m.exchange,
        m.sw_level1,
        m.sw_level2,
        m.is_st,
        m.is_suspended,
        m.listing_date,
        s.*
    FROM stock_meta m
    LEFT JOIN LATERAL (
        SELECT *
        FROM indicator_snapshot s
        WHERE s.stock_code = m.stock_code
        ORDER BY s.report_date DESC
        LIMIT 1
    ) s ON true
    WHERE {pool_where}
)""")

        # ─── 阶段2: 横截面排名 ──────────────────────────────────
        # P1-21修复: NULLS LAST 避免NULL值获得rank=1
        # P1-22修复: sw_level1为NULL时行业排名返回NULL (PRD §12.4)
        rank_cols: list[str] = []
        if rank_fields:
            for field in sorted(rank_fields):
                # 全市场排名 (NULLS LAST)
                rank_cols.append(
                    f"RANK() OVER (ORDER BY {field} NULLS LAST) AS {field}_market_rank"
                )
                rank_cols.append(
                    f"PERCENT_RANK() OVER (ORDER BY {field} NULLS LAST) AS {field}_market_percentile"
                )
                # 申万一级排名 (sw_level1为NULL时返回NULL, PRD §12.4)
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY sw_level1 ORDER BY {field} NULLS LAST) END "
                    f"AS {field}_industry_rank"
                )
                rank_cols.append(
                    f"CASE WHEN sw_level1 IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY sw_level1 ORDER BY {field} NULLS LAST) END "
                    f"AS {field}_industry_percentile"
                )

        if rank_cols:
            sql_parts.append(f"""
, ranked AS (
    SELECT base_pool.*,
        {', '.join(rank_cols)}
    FROM base_pool
)""")
            source_table = "ranked"
        else:
            source_table = "base_pool"

        # ─── 阶段3: 条件过滤 ────────────────────────────────────
        where_clause, where_params = self._build_where(conditions)
        params.extend(where_params)

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
            raise ValueError(f"规则嵌套超过3层限制 (PRD §12.2)")

        logic = node.get("logic", "AND").upper()
        rules = node.get("rules", [])

        if not rules:
            return "1=1", []

        parts: list[str] = []
        params: list[Any] = []

        for rule in rules:
            if "logic" in rule:
                # 嵌套节点
                sub_where, sub_params = self._build_where(rule, level + 1)
                if sub_where and sub_where != "1=1":
                    parts.append(f"({sub_where})")
                    params.extend(sub_params)
            else:
                # 叶子条件
                cond_sql, cond_params = self._build_condition(rule)
                if cond_sql:
                    parts.append(cond_sql)
                    params.extend(cond_params)

        if not parts:
            return "1=1", []

        joiner = f" {logic} "
        return joiner.join(parts), params

    def _build_condition(self, cond: dict) -> tuple[str, list[Any]]:
        """构建单个条件"""
        field = cond.get("field", "")
        op = cond.get("op", "")
        value = cond.get("value")

        # 验证字段名安全（防注入）
        if field and field not in SNAPSHOT_COLUMNS and not field.endswith("_market_rank") \
                and not field.endswith("_market_percentile") \
                and not field.endswith("_industry_rank") \
                and not field.endswith("_industry_percentile") \
                and field not in ("name", "stock_code", "exchange", "sw_level1", "sw_level2"):
            logger.warning(f"未知字段: {field}")
            return "", []

        sql_op = OP_MAP.get(op)
        if not sql_op:
            logger.warning(f"未知操作符: {op}")
            return "", []

        if op == "is_null":
            return f"{field} IS NULL", []
        elif op == "is_not_null":
            return f"{field} IS NOT NULL", []
        elif op == "between":
            if isinstance(value, list) and len(value) == 2:
                return f"{field} BETWEEN ? AND ?", [value[0], value[1]]
            return "", []
        elif op == "in":
            if isinstance(value, list) and value:
                placeholders = ", ".join(["?"] * len(value))
                return f"{field} IN ({placeholders})", value
            return "", []
        else:
            return f"{field} {sql_op} ?", [value]

    def _build_order(self, sort_spec: list[dict]) -> str:
        """构建 ORDER BY"""
        if not sort_spec:
            return ""
        # P0#13修复: 白名单验证排序字段, 防止 SQL 注入
        import re
        # 允许的排序字段: 字母/数字/下划线, 可选表别名前缀 (如 s.stock_code)
        safe_field_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")
        parts: list[str] = []
        for s in sort_spec:
            field = s.get("field", "")
            direction = s.get("direction", "asc").upper()
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            if field and safe_field_pattern.match(field):
                parts.append(f"{field} {direction}")
        if not parts:
            return ""
        return "ORDER BY " + ", ".join(parts)

    def _build_select(self, columns_spec: list[str], rank_fields: set[str]) -> str:
        """构建 SELECT 列"""
        # P0#13修复: 白名单验证列名, 防止 SQL 注入
        import re
        safe_col_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\s+AS\s+[a-zA-Z_][a-zA-Z0-9_]*)?$", re.IGNORECASE)
        if not columns_spec:
            # 默认列
            cols = [
                "stock_code", "name", "exchange", "sw_level1",
                "latest_close", "pe_ttm", "pb_mrq", "roe",
                "gross_margin", "net_margin", "debt_ratio",
            ]
        else:
            # 过滤不安全的列名
            cols = [c for c in columns_spec if safe_col_pattern.match(c.strip())]

        # 确保关键列总是包含
        for essential in ("stock_code", "name"):
            if essential not in cols:
                cols.insert(0, essential)

        return ", ".join(cols)

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
        return fields

    def _get_base_pool_size(
        self, include_st: bool, include_suspended: bool, min_listing_years: int
    ) -> int:
        """获取基础池大小"""
        conds: list[str] = []
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
