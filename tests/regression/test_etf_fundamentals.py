"""ETF 基本面聚合回归（2026-09-09）：成分解析、业绩/市值合计、全A增速与行业贡献。"""

from __future__ import annotations

import pytest

from app.core.etf_fundamentals import (
    fundamental_detail,
    industry_contribution,
    profit_growth_series,
    resolve_constituents,
)
from app.core.storage.duckdb_store import DuckDBStore


def _seed_basics(duck: DuckDBStore) -> None:
    for code, shares, industry in [
        ("000001", 1_000_000, "农林牧渔"),
        ("000002", 2_000_000, "食品饮料"),
    ]:
        duck.write_query(
            """INSERT INTO stock_meta (stock_code, name, exchange, is_listed, sw_level1, total_shares)
               VALUES (?, ?, 'SZSE', TRUE, ?, ?)""",
            [code, code, industry, shares],
        )
    for stock_code, base in [("000001", 100.0), ("000002", 200.0)]:
        duck.write_query(
            """INSERT INTO indicator_snapshot (stock_code, report_date, total_market_cap)
               VALUES (?, '2026-06-30', ?)""",
            [stock_code, base * 10],
        )
        for offset, price in [("2026-06-30", 10.0), ("2025-06-30", 8.0), ("2024-06-30", 6.0)]:
            duck.write_query(
                """INSERT INTO price_daily_qfq (stock_code, trade_date, close)
                   VALUES (?, ?, ?)""",
                [stock_code, offset, price],
            )
    duck.write_query(
        """INSERT INTO income_statement (stock_code, report_date, parent_net_profit)
           VALUES ('000001', '2026-06-30', 1000000),
                  ('000001', '2025-06-30', 800000),
                  ('000002', '2026-06-30', 2000000),
                  ('000002', '2025-06-30', 1000000)"""
    )


def test_resolve_sw_constituents_by_industry_name(duckdb_store: DuckDBStore) -> None:
    _seed_basics(duckdb_store)
    resolved = resolve_constituents(duckdb_store, {
        "etf_code": "159825", "track_index_code": "SW801010",
    })
    assert resolved["codes"] == ["000001"]
    assert "农林牧渔" in resolved["method"]


def test_fundamental_aggregates_and_growth(duckdb_store: DuckDBStore) -> None:
    _seed_basics(duckdb_store)
    detail = fundamental_detail(duckdb_store, {
        "etf_code": "ALL_A", "name": "全A指数", "track_index_code": None,
    })
    assert detail["companies"] == 2
    assert len(detail["earnings"]) >= 1
    assert detail["market_cap"][-1]["value"] > 0
    assert detail["profit_growth"][-1]["value"] == pytest.approx(200 / 3, abs=1e-6)
    assert any(item["industry"] == "农林牧渔" for item in detail["industry_contribution"])


def test_profit_growth_requires_prior_year() -> None:
    earnings = [
        {"report_date": "2026-06-30", "value": 120.0, "companies": 1},
        {"report_date": "2025-06-30", "value": 100.0, "companies": 1},
        {"report_date": "2024-06-30", "value": 80.0, "companies": 1},
    ]
    growth = profit_growth_series(earnings)
    assert growth[-2]["value"] == pytest.approx(20.0)
    assert growth[-1]["value"] == pytest.approx(25.0)


def test_industry_contribution_needs_comparable_year(duckdb_store: DuckDBStore) -> None:
    _seed_basics(duckdb_store)
    earnings = [
        {"report_date": "2025-06-30", "value": 100.0, "companies": 1},
        {"report_date": "2026-06-30", "value": 120.0, "companies": 1},
    ]
    contribution = industry_contribution(duckdb_store, earnings)
    assert contribution
    assert abs(sum(item["contribution_pct"] for item in contribution) - 100.0) < 1e-6
