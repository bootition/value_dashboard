from __future__ import annotations

from datetime import date

import pytest

from app.core.dsl.ast_nodes import FieldRef, FuncCall
from app.core.dsl.codegen import CodeGen
from app.core.dsl.parser import parse
from app.core.dsl.registry import ExpressionRegistry
from app.core.dsl.validator import Validator
from app.core.financial_period import expected_financial_period
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.stock_detail import build_freshness_metadata, calculate_ttm_trend


def test_non_annual_ttm_uses_annual_plus_current_minus_prior_period() -> None:
    rows = [
        {
            "report_date": date(2025, 3, 31),
            "revenue": 300.0,
            "cost_of_revenue": 120.0,
            "parent_net_profit": 60.0,
            "total_assets": 1200.0,
        },
        {
            "report_date": date(2024, 12, 31),
            "revenue": 1000.0,
            "cost_of_revenue": 400.0,
            "parent_net_profit": 200.0,
            "total_assets": 1100.0,
        },
        {
            "report_date": date(2024, 3, 31),
            "revenue": 200.0,
            "cost_of_revenue": 80.0,
            "parent_net_profit": 40.0,
            "total_assets": 1000.0,
        },
    ]

    trend = calculate_ttm_trend(rows)

    latest = trend[-1]
    assert latest["report_date"] == "2025-03-31"
    assert latest["revenue"] == 1100.0
    assert latest["gross_profit"] == 660.0
    assert latest["net_profit"] == 220.0
    assert latest["total_assets"] == 1200.0


@pytest.mark.parametrize(
    ("period", "expected"),
    [("TTM", 1100.0), ("YoY", 0.5), ("QoQ", -0.25)],
)
def test_dsl_period_functions_execute_against_normalized_history(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    period: str,
    expected: float,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO indicator_snapshot (stock_code, report_date)
               VALUES ('600519', '2025-03-31')"""
        )
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES
               ('600519', '2025-03-31', 300), ('600519', '2024-12-31', 1000),
               ('600519', '2024-09-30', 600), ('600519', '2024-03-31', 200)"""
        )

    field = FieldRef(table="income", field="revenue", period=period)
    result = duckdb_store.read_query(CodeGen().generate_select(field, stock_code="600519"))

    assert result[0]["result"] == expected


def test_dsl_ttm_function_form_matches_period_suffix(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute("INSERT INTO indicator_snapshot (stock_code, report_date) VALUES ('600519', '2025-03-31')")
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES ('600519', '2025-03-31', 300), ('600519', '2024-12-31', 1000),
                      ('600519', '2024-03-31', 200)"""
        )

    expression = FuncCall(func_name="TTM", args=[FieldRef(table="income", field="revenue")])
    result = duckdb_store.read_query(CodeGen().generate_select(expression, stock_code="600519"))

    assert result[0]["result"] == 1100.0


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("income.revenue@TTM", 1100.0),
        ("YoY(income.revenue)", 0.5),
        ("QoQ(income.revenue)", -0.25),
    ],
)
def test_dsl_textual_period_forms_parse_and_execute(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    expression: str,
    expected: float,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute("INSERT INTO indicator_snapshot (stock_code, report_date) VALUES ('600519', '2025-03-31')")
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES ('600519', '2025-03-31', 300), ('600519', '2024-12-31', 1000),
                      ('600519', '2024-09-30', 600), ('600519', '2024-03-31', 200)"""
        )

    result = duckdb_store.read_query(CodeGen().generate_select(parse(expression), stock_code="600519"))

    assert result[0]["result"] == expected


def test_dsl_period_function_returns_null_when_exact_prior_quarter_is_missing(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute("INSERT INTO indicator_snapshot (stock_code, report_date) VALUES ('600519', '2025-03-31')")
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES ('600519', '2025-03-31', 300), ('600519', '2024-09-30', 600)"""
        )

    result = duckdb_store.read_query(CodeGen().generate_select(parse("income.revenue@QoQ"), stock_code="600519"))

    assert result[0]["result"] is None


def test_dsl_qoq_uses_march_31_as_the_prior_period_for_june_30(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute("INSERT INTO indicator_snapshot (stock_code, report_date) VALUES ('600519', '2025-06-30')")
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue)
               VALUES ('600519', '2025-06-30', 500), ('600519', '2025-03-31', 200)"""
        )

    result = duckdb_store.read_query(CodeGen().generate_select(parse("income.revenue@QoQ"), stock_code="600519"))

    assert result[0]["result"] == 0.5


def test_dsl_mrq_and_yoy_convert_cumulative_flows_to_single_quarters(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute("INSERT INTO indicator_snapshot (stock_code, report_date) VALUES ('600519', '2025-06-30')")
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue) VALUES
               ('600519', '2025-03-31', 100), ('600519', '2025-06-30', 300),
               ('600519', '2024-03-31', 80), ('600519', '2024-06-30', 200)"""
        )

    mrq = duckdb_store.read_query(CodeGen().generate_select(parse("income.revenue@MRQ"), stock_code="600519"))
    yoy = duckdb_store.read_query(CodeGen().generate_select(parse("income.revenue@YoY"), stock_code="600519"))

    assert mrq[0]["result"] == 200
    assert yoy[0]["result"] == pytest.approx((200 - 120) / 120)


@pytest.mark.parametrize("expression", ["balance.total_assets@TTM", "QoQ(balance.total_assets)"])
def test_dsl_rejects_flow_periods_for_point_in_time_fields(
    sqlite_store: SQLiteStore,
    expression: str,
) -> None:
    validation = Validator(ExpressionRegistry(sqlite=sqlite_store), sqlite=sqlite_store).validate(parse(expression))

    assert validation["valid"] is False


def test_dsl_growth_value_cannot_be_added_to_a_currency_flow(
    sqlite_store: SQLiteStore,
) -> None:
    validation = Validator(ExpressionRegistry(sqlite=sqlite_store), sqlite=sqlite_store).validate(
        parse("QoQ(income.revenue) + income.revenue")
    )

    assert validation["valid"] is False


def test_expected_financial_period_uses_beijing_time() -> None:
    from datetime import UTC, datetime

    # 北京 8月1日 00:00 = UTC 7月31日 16:00，必须按半年报季返回 06-30。
    assert expected_financial_period(datetime(2026, 7, 31, 16, 0, tzinfo=UTC)) == "2026-06-30"
    # 北京 11月1日 00:00 = UTC 10月31日 16:00，必须按三季报季返回 09-30。
    assert expected_financial_period(datetime(2026, 10, 31, 16, 0, tzinfo=UTC)) == "2026-09-30"


def test_current_half_year_report_is_not_stale() -> None:
    from datetime import UTC, datetime

    # 半年报季中，报告期 06-30 距今天自然有 60+ 天，但不应告警。
    metadata = build_freshness_metadata(
        financial_date=date(2026, 6, 30),
        price_date=date(2026, 9, 7),
        calculated_at=datetime(2026, 9, 7, tzinfo=UTC),
        data_version="audit-safe-v1",
    )
    assert metadata["stale_warning"] is False
    assert metadata["financial_lagging"] is False


def test_indicator_freshness_flags_large_price_gap() -> None:
    metadata = build_freshness_metadata(
        financial_date=date(2025, 3, 31),
        price_date=date(2026, 7, 17),
        calculated_at=None,
        data_version=None,
    )

    assert metadata["stale_warning"] is True
    assert metadata["stale_days"] is not None
    assert metadata["financial_age_days"] is not None
    assert metadata["price_age_days"] is not None
    assert metadata["snapshot_age_days"] is None
    assert metadata["financial_effective_date"] == "2025-03-31"
    assert metadata["price_date"] == "2026-07-17"
