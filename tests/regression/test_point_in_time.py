"""时点可见性（point-in-time）回归（2026-09-19）。

守住三件事：
1. `latest_visible_annual` 只认 `announce_date <= as_of` 的报告期；
2. 无公布日记录时返回 None / `was_visible` 返回 None（未知 ≠ 不可见）；
3. 边界如实暴露（仅年度频率、截止 2024、缺失不推断）。
"""

from __future__ import annotations

from datetime import date

from app.core.point_in_time import (
    PIT_LIMITATIONS,
    coverage,
    latest_visible_annual,
    was_visible,
)
from app.core.storage.duckdb_store import DuckDBStore

STOCK = "600519"
SEED = [
    ("2021-12-31", "2022-03-31"),
    ("2022-12-31", "2023-04-03"),
    ("2023-12-31", "2024-04-03"),
    ("2024-12-31", "2025-03-28"),
    # 季度行（2026-09-20 补全公布日后新增的能力）
    ("2025-03-31", "2025-04-30"),
    ("2025-06-30", "2025-08-30"),
    ("2025-09-30", "2025-10-30"),
]


def _seed(store: DuckDBStore) -> None:
    with store.transaction() as conn:
        for report_date, announce_date in SEED:
            conn.execute(
                """INSERT INTO financial_report_dates
                   (stock_code, report_date, announce_date, source, fetch_time, batch_id)
                   VALUES (?, CAST(? AS DATE), CAST(? AS DATE), 'csmar', '2026-09-19 00:00:00', 'b')""",
                [STOCK, report_date, announce_date],
            )


def test_picks_latest_period_whose_announce_date_has_passed(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    # 2024-06-01：FY2023 已于 2024-04-03 公布，FY2024 未公布 → 应取 FY2023
    visible = latest_visible_annual(duckdb_store, STOCK, date(2024, 6, 1))
    assert visible is not None
    assert str(visible.report_date) == "2023-12-31"
    assert str(visible.announce_date) == "2024-04-03"


def test_returns_none_before_first_announcement(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    assert latest_visible_annual(duckdb_store, STOCK, date(2022, 1, 1)) is None


def test_returns_none_for_unknown_stock(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    assert latest_visible_annual(duckdb_store, "999999", date(2025, 1, 1)) is None


def test_was_visible_distinguishes_unknown_from_false(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    # 已知报告期 + 早于公布日 → False
    assert was_visible(duckdb_store, STOCK, date(2024, 12, 31), date(2025, 1, 15)) is False
    # 已知报告期 + 晚于公布日 → True
    assert was_visible(duckdb_store, STOCK, date(2024, 12, 31), date(2025, 6, 1)) is True
    # 未登记公布日 → None（未知，不得当成 False）
    assert was_visible(duckdb_store, STOCK, date(2019, 12, 31), date(2025, 1, 1)) is None


def test_stale_flag_uses_one_year_threshold(duckdb_store: DuckDBStore) -> None:
    """正常年报节奏下不应误报 stale；长期未披露年报（如退市整理）应为 stale。"""
    _seed(duckdb_store)
    fresh = latest_visible_annual(duckdb_store, STOCK, date(2024, 6, 1))
    assert fresh is not None and fresh.is_stale is False

    # 某股票最后一期年报为 2010 年报（2011-04-30 公布），此后长期无披露
    with duckdb_store.transaction() as conn:
        conn.execute(
            """INSERT INTO financial_report_dates
               (stock_code, report_date, announce_date, source, fetch_time, batch_id)
               VALUES ('000999', CAST('2010-12-31' AS DATE), CAST('2011-04-30' AS DATE),
                       'csmar', '2026-09-19 00:00:00', 'b')"""
        )
    stale = latest_visible_annual(duckdb_store, "000999", date(2013, 1, 1))
    assert stale is not None
    assert stale.days_since_announce > 400
    assert stale.is_stale is True


def test_coverage_exposes_honest_limitations(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    info = coverage(duckdb_store)
    assert info["rows"] == len(SEED)
    assert info["stocks"] == 1
    assert info["limitations"] == list(PIT_LIMITATIONS)
    # 2026-09-20 起公布日已补到**季度频率**（此前仅年报），边界声明须同步更新 ——
    # 若仍有「仅年度频率」字样说明文档没跟上能力升级。
    assert any("季度频率" in item for item in info["limitations"])
    assert not any("仅年度频率" in item for item in info["limitations"])
    # 无公布日记录的报告期不得参与判定（不能假设未登记即当天可见）
    assert any("不参与判定" in item for item in info["limitations"])


def test_frequency_filter_returns_expected_granularity(duckdb_store: DuckDBStore) -> None:
    """`frequency` 参数：any 取最近一期，annual 只取年报。

    这是 2026-09-20 公布日补全后的能力升级：此前只有年报可选，
    现在「当时可见」可以精确到季度。
    """
    from datetime import date

    from app.core.point_in_time import latest_visible_period

    _seed(duckdb_store)
    any_period = latest_visible_period(duckdb_store, STOCK, date(2025, 12, 31), frequency="any")
    annual = latest_visible_period(duckdb_store, STOCK, date(2025, 12, 31), frequency="annual")
    assert any_period is not None and annual is not None
    # 年报口径的结果月份必须是 12
    assert annual.report_date.month == 12
    # any 口径能看到更新的期（季度披露），annual 只能看到年报 —— 这正是补全公布日
    # 带来的精度提升（此前 any 与 annual 结果相同）。
    assert any_period.report_date > annual.report_date
    assert any_period.report_date.month != 12


def test_unknown_frequency_raises(duckdb_store: DuckDBStore) -> None:
    import pytest

    from app.core.point_in_time import latest_visible_period

    with pytest.raises(ValueError):
        latest_visible_period(duckdb_store, "000001", __import__("datetime").date(2024, 1, 1),
                              frequency="monthly")
