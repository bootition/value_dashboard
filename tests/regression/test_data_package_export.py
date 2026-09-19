"""数据包导出器不变量回归（2026-09-19）。

守住三条底线（都是真实踩过或极易踩的坑）：
1. **绝不导出个人数据**（自选/筛选规则/ETF 流水/预算）—— 由**白名单**保证；
2. **许可分级必须齐全**（A/B/C 三级 + CSMAR 段的 C 级标注）；
3. **load.sql 必须真的能生成**（首版曾因「用 dict 键做 endswith('.parquet')」而全空）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_EXPORTER = _REPO / "scripts" / "export_data_package.py"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("vd_exporter", _EXPORTER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_all_exported_tables_are_whitelisted() -> None:
    """导出面必须是**显式白名单**：任何未登记的表都不会被打包。"""
    exporter = _load_exporter()
    listed = {t for _, t, _, _ in exporter.A_TABLES + exporter.B_TABLES + exporter.LINEAGE_TABLES}
    assert listed, "白名单不能为空"
    # 表名必须全部是普通标识符（防止把 SQL 片段写进白名单）
    for name in listed:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", name), f"非法表名 {name}"


def test_personal_data_is_never_exported() -> None:
    """个人数据（个性化/用户输入）绝不能进包 —— 对应 AR9 约束。"""
    exporter = _load_exporter()
    listed = {t for _, t, _, _ in exporter.A_TABLES + exporter.B_TABLES + exporter.LINEAGE_TABLES}
    forbidden = {
        "watchlist", "screening_rules", "screening_runs", "screening_results",
        "screening_drafts", "manual_overrides", "plans", "dsl_expressions",
        "dsl_dependencies", "etf_meta", "etf_trades", "etf_cash_flows",
        "etf_sell_plans", "etf_settings", "config", "job_logs", "backup_registry",
    }
    leaked = listed & forbidden
    assert not leaked, f"个人数据泄漏进导出白名单: {sorted(leaked)}"


def test_license_tiers_cover_all_tables() -> None:
    """每张导出表都必须标许可级别，且级别只能是 A/B/C。"""
    exporter = _load_exporter()
    for _, table, tier, note in exporter.A_TABLES + exporter.B_TABLES + exporter.LINEAGE_TABLES:
        assert tier in {"A", "B", "C"}, f"{table} 许可级别非法: {tier}"
        assert note, f"{table} 缺少说明"


def test_csmar_derived_tables_are_tier_c() -> None:
    """CSMAR 衍生表必须是 C 级（禁止外发）—— 商业授权数据不可再分发。"""
    exporter = _load_exporter()
    by_table = {t: tier for _, t, tier, _ in exporter.A_TABLES + exporter.B_TABLES}
    for table in (
        "financial_report_dates", "company_employee_history",
        "csmar_disclosure_metrics", "csmar_risk_factors",
        "balance_sheet", "income_statement", "cash_flow",
    ):
        assert by_table.get(table) == "C", f"{table} 应标 C 级（CSMAR 衍生）"


def test_package_layout_matches_the_ab_split() -> None:
    """包按用户要求的二分法组织：A=CSMAR 有的更新版；B=CSMAR 没有的独有数据。"""
    exporter = _load_exporter()
    layers = {layer for layer, _, _, _ in exporter.A_TABLES + exporter.B_TABLES}
    assert layers == {"A_updates", "B_supplements"}
    a_tables = {t for _, t, _, _ in exporter.A_TABLES}
    b_tables = {t for _, t, _, _ in exporter.B_TABLES}
    # CSMAR 完全没有的独有数据必须在 B 类
    for table in ("price_daily_raw", "price_daily_qfq", "xdxr", "dividends",
                  "share_capital_history", "treasury_yield_curve", "etf_daily"):
        assert table in b_tables, f"{table} 应归入 B_supplements（CSMAR 没有）"
    assert not (a_tables & b_tables), "同一张表不能同时属于 A 与 B"


def test_load_sql_builder_uses_file_paths_not_dict_keys() -> None:
    """回归：首版用 files 的**键**（无扩展名）做 endswith('.parquet') 判断，
    导致 load.sql 生成为空。此处直接校验生成逻辑针对 value 的 file 字段。"""
    source = _EXPORTER.read_text(encoding="utf-8")
    assert 'v.get("file", "")' in source or "v.get('file', '')" in source, (
        "load.sql 的 parquet 判定必须基于 value 的 file 字段（含扩展名）"
    )
    assert 'endswith(".parquet")' in source
