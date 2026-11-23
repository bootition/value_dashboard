"""零财报新股算不出指标 = 正常，不得记成 failed 把整轮拖成 partial。

2026-11-23 实测：`001381 皇冠新材`（上市 2026-11-03）、`688827 钶锐锶`（11-10）、
`920085 森峰激光`（11-06）、`920306 天元重工`（10-29）、`920309 菊乐股份`（11-17）
—— **这 5 只 `income_statement` 一行都没有**，于是价格域增量计算抛异常，
被记成 `failed`，整个 `indicators` 步骤（5,563 只成功）只能报 `partial`。

这与用户明确反感的那类"假告警"同型：真正算错的（有财报却崩）必须红，
"数据还没到"（新股未披露）不该红。
"""

from __future__ import annotations

from app.core.indicators.calculator import _has_any_financials
from tests.support.seed import Seed


def test_stock_without_financials_is_not_ready(duckdb_store) -> None:
    seed = Seed(duckdb_store)
    seed.stock(
        "001381", name="皇冠新材", exchange="SZSE",
        listing_date="2026-11-03", is_st=False, is_suspended=False,
    )
    assert _has_any_financials(duckdb_store, "001381") is False, (
        "零财报新股必须判为 not_ready（否则每轮 partial）"
    )


def test_stock_with_financials_is_ready(duckdb_store) -> None:
    seed = Seed(duckdb_store)
    seed.stock(
        "600519", name="贵州茅台", exchange="SSE",
        listing_date="2001-08-27", is_st=False, is_suspended=False,
    )
    seed.income("600519", "2026-09-30", revenue=1.0)
    assert _has_any_financials(duckdb_store, "600519") is True, (
        "有财报的股票算错时仍必须记 failed（不能借这条把真失败也放过）"
    )


def test_stock_absent_from_db_treated_as_not_ready(duckdb_store) -> None:
    """库里根本没有这只股票的财报 → 也是 not_ready，不是 failed。"""
    assert _has_any_financials(duckdb_store, "999999") is False
