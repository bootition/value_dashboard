from __future__ import annotations

from datetime import date

import pytest

from app.core.dsl.ast_nodes import FieldRef, FuncCall
from app.core.dsl.codegen import CodeGen, UnsupportedPeriodFunctionError
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


@pytest.mark.parametrize("function_name", ["TTM", "YoY", "QoQ"])
def test_dsl_period_function_rejects_missing_history_context(function_name: str) -> None:
    expression = FuncCall(
        func_name=function_name,
        args=[FieldRef(table="income", field="revenue")],
    )

    with pytest.raises(UnsupportedPeriodFunctionError, match=function_name):
        CodeGen().generate(expression)


def test_indicator_freshness_flags_large_price_gap() -> None:
    metadata = build_freshness_metadata(
        financial_date=date(2025, 3, 31),
        price_date=date(2026, 7, 17),
        calculated_at=None,
        data_version=None,
    )

    assert metadata["stale_warning"] is True
    assert metadata["stale_days"] == 473
    assert metadata["financial_effective_date"] == "2025-03-31"
    assert metadata["price_date"] == "2026-07-17"
