"""ROCE（已动用资本回报率）域回归（schema v47，reports/132 + reports/139 §7）。

口径来源：《我从达尔文那里学到的投资知识》第 2 章 + 用户 2026-11-16 三次裁决。

    已动用资本 CE = 总资产 − 流动负债        （= 净营运资本 + 全部长期资产）
    EBIT         = 利润总额 + 财务费用
    单年 ROCE    = 当年 EBIT ÷ ((上年 CE + 本年 CE) / 2)
    主指标        = 最近 10 / 5 / 20 个**有效年报** ROCE 的中位数
    持久性        = 同一 10 年窗口的 min（地板）与 cv = σ ÷ 中位（稳定性）
    **现金计入分母，不剔除。**

本文件钉死七件事，每一条都对应一次真实的口径事故：

1. **分母必须是「总资产 − 流动负债」**，不是「净营运资本 + fixed_assets」。
   只取 PPE 会漏掉油气资产/无形资产/在建工程：中国海油非流动资产 8,003 亿、
   `fixed_assets` 仅 85 亿 → 算出 119% 压过茅台；海天 90.5%、公牛 80.6%。
2. **不剔现金**（reports/132 裁定二）。书里那句「剔除超额现金」是向上修正，
   机械执行会把分母削到零：同花顺现金占总资产 89%，剔完资本只剩 2 亿、EBIT 35 亿
   → **444%**；重庆啤酒分母为负；海天 90.5%、公牛 80.6%。
   判据是书里的标杆（蒂芙尼 16%、好市多 22%、组合中位 42%、Havell 52%）——
   不剔现金时全市场中位 10.3%、p90 19.7%、最高 68.4%、≥20% 仅 9.2%，量级吻合。
3. **不设任何比例护栏**。唯一的 NULL 来自除法不成立（CE ≤ 0），CE 原值必须保留。
4. **只吃年报**。季报累计值直接除年平均资本会系统性低估。
5. **筛选口径**固化为 `roce_eligible`：非金融 + 市值 ≥ 100 亿 + 有效 ROCE 年数 ≥ 5
   （年数门槛 v46 由 8 降到 5）。用「有效年数」而不是「上市满 N 年」：算单年 ROCE
   需要上一年的资本。
6. **严格窗口**：5/10/20 年窗口都必须拿满 N 个有效年报才出值；v47 的
   `roce_10y_min` / `roce_10y_cv` 沿用同一条纪律（`n10 >= 10`）。
7. **登记与重建 SQL 必须逐字一致**（RG4）：`SNAPSHOT_DERIVED_COLUMNS` 少登记一列，
   快照 DELETE+INSERT 重发就会把它静默清成 NULL（reports/131 K5 / reports/139 X-V46）。
"""

from __future__ import annotations

import statistics
from typing import Any

import pytest

from app.core.indicators.roce import (
    DATA_VERSION,
    SOURCE,
    STATUS_INVALID_DENOMINATOR,
    STATUS_MISSING_EBIT,
    STATUS_NO_PRIOR_PERIOD,
    STATUS_OK,
    RoceBuilder,
)
from app.core.storage.duckdb_store import DuckDBStore

#: 一年一期的年报日期
YEARS = ("2019-12-31", "2020-12-31", "2021-12-31", "2022-12-31", "2023-12-31")


def _seed_balance(
    duck: DuckDBStore,
    code: str,
    *,
    total_assets: list[float],
    total_current_liabilities: list[float],
    years: tuple[str, ...] = YEARS,
) -> None:
    for report_date, ta, tcl in zip(years, total_assets, total_current_liabilities, strict=True):
        duck.write_query(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_current_liabilities)
               VALUES (?, CAST(? AS DATE), ?, ?)""",
            [code, report_date, ta, tcl],
        )


def _seed_income(
    duck: DuckDBStore,
    code: str,
    *,
    total_profit: list[float],
    financial_expenses: list[float],
    years: tuple[str, ...] = YEARS,
) -> None:
    for report_date, tp, fin in zip(years, total_profit, financial_expenses, strict=True):
        duck.write_query(
            """INSERT INTO income_statement (stock_code, report_date, total_profit, financial_expenses)
               VALUES (?, CAST(? AS DATE), ?, ?)""",
            [code, report_date, tp, fin],
        )


def _snapshot(duck: DuckDBStore, code: str) -> dict[str, Any]:
    rows = duck.read_query(
        """SELECT roce_latest, roce_5y_median, roce_5y_years, roce_10y_median, roce_10y_years,
                  roce_20y_median, roce_20y_years
           FROM indicator_snapshot WHERE stock_code = ?""",
        [code],
    )
    return rows[0] if rows else {}


def _seed_snapshot_row(duck: DuckDBStore, code: str) -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES (?, ?, 'SZSE', true)",
        [code, code],
    )
    duck.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, calculated_at, data_version)
           VALUES (?, CAST('2023-12-31' AS DATE), now(), 'fixture')""",
        [code],
    )


def test_capital_employed_is_total_assets_minus_current_liabilities(
    duckdb_store: DuckDBStore,
) -> None:
    """分母 = 总资产 − 流动负债；`fixed_assets` 完全不参与（口径 C 的核心）。"""
    _seed_snapshot_row(duckdb_store, "000001")
    _seed_balance(
        duckdb_store, "000001",
        total_assets=[1000.0, 1100.0, 1200.0, 1300.0, 1400.0],
        total_current_liabilities=[400.0, 400.0, 400.0, 400.0, 400.0],
    )
    _seed_income(
        duckdb_store, "000001",
        total_profit=[100.0] * 5, financial_expenses=[10.0] * 5,
    )

    RoceBuilder(duck=duckdb_store).build()

    rows = duckdb_store.read_query(
        """SELECT report_date, ce, ce_prev, ebit
           FROM roce_history WHERE stock_code = '000001' ORDER BY report_date"""
    )
    # 2019 年是序列第一年，没有期初 CE
    assert rows[0]["ce_prev"] is None
    by_date = {str(r["report_date"]): r for r in rows}
    assert by_date["2020-12-31"]["ce"] == 700.0          # 1100 − 400
    assert by_date["2020-12-31"]["ce_prev"] == 600.0     # 1000 − 400
    assert by_date["2020-12-31"]["ebit"] == 110.0        # 100 + 10


def test_single_year_roce_uses_average_capital_employed(
    duckdb_store: DuckDBStore,
) -> None:
    """单年 ROCE = EBIT ÷ ((期初 CE + 期末 CE) / 2)。"""
    _seed_snapshot_row(duckdb_store, "000002")
    _seed_balance(
        duckdb_store, "000002",
        total_assets=[1000.0, 1200.0, 1400.0, 1600.0, 1800.0],
        total_current_liabilities=[0.0] * 5,
    )
    _seed_income(
        duckdb_store, "000002",
        total_profit=[100.0] * 5, financial_expenses=[0.0] * 5,
    )

    RoceBuilder(duck=duckdb_store).build()

    row = duckdb_store.read_query(
        """SELECT roce FROM roce_history
           WHERE stock_code = '000002' AND report_date = CAST('2020-12-31' AS DATE)"""
    )[0]
    # EBIT 100 ÷ ((1000 + 1200) / 2) = 100/1100
    assert row["roce"] == pytest.approx(100.0 / 1100.0)


def test_cash_is_not_excluded_from_capital_employed(duckdb_store: DuckDBStore) -> None:
    """不剔现金：货币资金再多也不动分母（剔除会掏空白酒/家电的分母）。"""
    _seed_snapshot_row(duckdb_store, "000003")
    for report_date in YEARS:
        duckdb_store.write_query(
            """INSERT INTO balance_sheet
               (stock_code, report_date, total_assets, total_current_liabilities,
                monetary_funds, short_term_loans, long_term_loans, bonds_payable)
               VALUES (?, CAST(? AS DATE), 1000, 300, 600, 0, 0, 0)""",
            ["000003", report_date],
        )
    _seed_income(
        duckdb_store, "000003",
        total_profit=[50.0] * 5, financial_expenses=[0.0] * 5,
    )

    RoceBuilder(duck=duckdb_store).build()

    ce = duckdb_store.read_query(
        """SELECT ce FROM roce_history
           WHERE stock_code = '000003' AND report_date = CAST('2023-12-31' AS DATE)"""
    )[0]["ce"]
    # 600 亿现金、0 债务 —— 若按「现金 − 债务」剔除，这里会变成 700−600=100 甚至负数
    assert ce == 700.0


def test_invalid_denominator_keeps_null_roce_but_preserves_ce(
    duckdb_store: DuckDBStore,
) -> None:
    """资本占用 ≤ 0：ROCE 为 NULL（除法不成立），但 CE 原值必须保留可复核。

    用户裁决「不剔除极值」——所以这里不做隐藏，而是**如实标注**。
    """
    _seed_snapshot_row(duckdb_store, "000004")
    _seed_balance(
        duckdb_store, "000004",
        total_assets=[100.0, 90.0, 80.0, 70.0, 60.0],
        total_current_liabilities=[120.0, 120.0, 120.0, 120.0, 120.0],  # 资不抵债
    )
    _seed_income(
        duckdb_store, "000004",
        total_profit=[10.0] * 5, financial_expenses=[0.0] * 5,
    )

    RoceBuilder(duck=duckdb_store).build()

    rows = duckdb_store.read_query(
        """SELECT report_date, roce, status FROM roce_history
           WHERE stock_code = '000004' ORDER BY report_date"""
    )
    assert rows, "资本占用为负的行必须仍然入库"
    assert all(r["roce"] is None for r in rows)
    assert rows[0]["status"] == STATUS_NO_PRIOR_PERIOD
    assert all(r["status"] == STATUS_INVALID_DENOMINATOR for r in rows[1:])

    row = duckdb_store.read_query(
        """SELECT ce FROM roce_history
           WHERE stock_code = '000004' AND report_date = CAST('2023-12-31' AS DATE)"""
    )[0]
    assert row["ce"] == -60.0  # 60 − 120，负数原样保留


def test_missing_financial_expenses_marks_missing_ebit(duckdb_store: DuckDBStore) -> None:
    """金融股利润表没有「财务费用」这一行 → EBIT 缺失，不能当成 0。"""
    _seed_snapshot_row(duckdb_store, "000005")
    for report_date in YEARS:
        duckdb_store.write_query(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_current_liabilities)
               VALUES (?, CAST(? AS DATE), 1000, 300)""",
            ["000005", report_date],
        )
        duckdb_store.write_query(
            """INSERT INTO income_statement (stock_code, report_date, total_profit)
               VALUES (?, CAST(? AS DATE), 100)""",
            ["000005", report_date],
        )

    RoceBuilder(duck=duckdb_store).build()

    rows = duckdb_store.read_query(
        """SELECT report_date, roce, status FROM roce_history
           WHERE stock_code = '000005' ORDER BY report_date"""
    )
    assert rows
    assert all(r["roce"] is None for r in rows)
    # 序列首年（2019）没有上年报表 → NO_PRIOR_PERIOD；其余年份是财务费用缺失
    assert rows[0]["status"] == STATUS_NO_PRIOR_PERIOD
    assert all(r["status"] == STATUS_MISSING_EBIT for r in rows[1:])


def _seed_n_years(duck: DuckDBStore, code: str, n_reports: int) -> None:
    """铺 n_reports 份连续年报（CE 恒 1000、EBIT 恒 100），得到 n_reports-1 个有效 ROCE 年。"""
    for i in range(n_reports):
        report_date = f"{2000 + i}-12-31"
        duck.write_query(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_current_liabilities)
               VALUES (?, CAST(? AS DATE), 1000, 0)""",
            [code, report_date],
        )
        duck.write_query(
            """INSERT INTO income_statement (stock_code, report_date, total_profit, financial_expenses)
               VALUES (?, CAST(? AS DATE), 100, 0)""",
            [code, report_date],
        )
    _seed_snapshot_row(duck, code)


def _windows(duck: DuckDBStore, code: str) -> dict[str, Any]:
    rows = duck.read_query(
        """SELECT roce_5y_median, roce_5y_years, roce_10y_median, roce_10y_years,
                  roce_20y_median, roce_20y_years, roce_latest
           FROM indicator_snapshot WHERE stock_code = ?""",
        [code],
    )
    return rows[0]


def test_window_requires_exactly_n_years(duckdb_store: DuckDBStore) -> None:
    """**严格窗口**：N 年中位必须由恰好 N 个有效年报算出，不足则留空。

    2026-11-16 用户裁定：「10 年中位，就一定要 10 年的数据，不要给我搞什么
    7 年、8 年这些。」旧实现用 `rn <= N` 直接取中位，会让只有 7 年数据的公司
    冒出一个"10 年中位"——名实不符。
    """
    # 4 份年报 → 3 个有效 ROCE 年（首年无期初资本）
    _seed_n_years(duckdb_store, "200004", 4)
    # 5 份 → 4 个有效年
    _seed_n_years(duckdb_store, "200005", 5)
    # 6 份 → 5 个有效年
    _seed_n_years(duckdb_store, "200006", 6)
    # 11 份 → 10 个有效年
    _seed_n_years(duckdb_store, "200011", 11)

    RoceBuilder(duck=duckdb_store).build()

    w3 = _windows(duckdb_store, "200004")
    assert w3["roce_5y_years"] == 3
    assert w3["roce_5y_median"] is None, "3 年数据不能冒充 5 年中位"
    assert w3["roce_10y_median"] is None

    w4 = _windows(duckdb_store, "200005")
    assert w4["roce_5y_years"] == 4
    assert w4["roce_5y_median"] is None, "4 年数据不能冒充 5 年中位"

    w5 = _windows(duckdb_store, "200006")
    assert w5["roce_5y_years"] == 5
    assert w5["roce_5y_median"] == pytest.approx(0.10), "拿满 5 年才出 5 年中位"
    assert w5["roce_10y_years"] == 5
    assert w5["roce_10y_median"] is None, "5 年数据不能冒充 10 年中位"
    assert w5["roce_20y_median"] is None

    w10 = _windows(duckdb_store, "200011")
    assert w10["roce_10y_years"] == 10
    assert w10["roce_10y_median"] == pytest.approx(0.10)
    assert w10["roce_20y_years"] == 10
    assert w10["roce_20y_median"] is None, "10 年数据不能冒充 20 年中位"


def test_20y_window_available_when_20_years_present(duckdb_store: DuckDBStore) -> None:
    """有 21 份年报（20 个有效 ROCE 年）时，20 年中位才出值。"""
    _seed_n_years(duckdb_store, "200021", 21)
    RoceBuilder(duck=duckdb_store).build()
    w = _windows(duckdb_store, "200021")
    assert w["roce_20y_years"] == 20
    assert w["roce_20y_median"] == pytest.approx(0.10)
    assert w["roce_latest"] == pytest.approx(0.10)


def test_median_is_median_of_valid_years(duckdb_store: DuckDBStore) -> None:
    """窗口内的中位数确实是中位数，不是平均数。"""
    _seed_snapshot_row(duckdb_store, "200007")
    years = ("2019-12-31", "2020-12-31", "2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31")
    for report_date, profit in zip(years, [100.0, 200.0, 300.0, 100.0, 100.0, 100.0], strict=True):
        duckdb_store.write_query(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_current_liabilities)
               VALUES (?, CAST(? AS DATE), 1000, 0)""",
            ["200007", report_date],
        )
        duckdb_store.write_query(
            """INSERT INTO income_statement (stock_code, report_date, total_profit, financial_expenses)
               VALUES (?, CAST(? AS DATE), ?, 0)""",
            ["200007", report_date, profit],
        )
    RoceBuilder(duck=duckdb_store).build()
    snap = _snapshot(duckdb_store, "200007")
    # 5 个有效年 ROCE = 20%/30%/10%/10%/10% → median = 10%
    assert snap["roce_5y_years"] == 5
    assert snap["roce_5y_median"] == pytest.approx(0.10)


def test_quarterly_reports_are_ignored(duckdb_store: DuckDBStore) -> None:
    """只吃年报：季报累计值直接除年平均资本会系统性低估。"""
    _seed_snapshot_row(duckdb_store, "000007")
    duckdb_store.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_current_liabilities)
           VALUES ('000007', CAST('2023-09-30' AS DATE), 999, 0),
                  ('000007', CAST('2023-12-31' AS DATE), 1000, 0),
                  ('000007', CAST('2022-12-31' AS DATE), 900, 0)"""
    )
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, total_profit, financial_expenses)
           VALUES ('000007', CAST('2023-09-30' AS DATE), 75, 0),
                  ('000007', CAST('2023-12-31' AS DATE), 100, 0),
                  ('000007', CAST('2022-12-31' AS DATE), 80, 0)"""
    )

    RoceBuilder(duck=duckdb_store).build()

    dates = [
        str(r["report_date"])
        for r in duckdb_store.read_query(
            "SELECT report_date FROM roce_history WHERE stock_code = '000007' ORDER BY report_date"
        )
    ]
    assert dates == ["2022-12-31", "2023-12-31"], "季报行不得进入 ROCE 序列"


def test_no_ratio_guard_is_applied(duckdb_store: DuckDBStore) -> None:
    """不设任何比例护栏：极高的 ROCE 必须原样保留（用户 2026-11-16 裁决）。"""
    _seed_snapshot_row(duckdb_store, "000008")
    _seed_balance(
        duckdb_store, "000008",
        total_assets=[1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        total_current_liabilities=[999.0] * 5,   # CE = 1 元
    )
    _seed_income(
        duckdb_store, "000008",
        total_profit=[100.0] * 5, financial_expenses=[0.0] * 5,  # EBIT 100 元
    )

    RoceBuilder(duck=duckdb_store).build()

    snap = _snapshot(duckdb_store, "000008")
    assert snap["roce_latest"] == pytest.approx(100.0), "100 倍的 ROCE 不得被任何护栏吃掉"


def test_builder_reports_status_breakdown(duckdb_store: DuckDBStore) -> None:
    """构建报告必须能一眼看出多少行有效、多少行分母非正。"""
    _seed_snapshot_row(duckdb_store, "000009")
    _seed_balance(
        duckdb_store, "000009",
        total_assets=[1000.0] * 5, total_current_liabilities=[400.0] * 5,
    )
    _seed_income(
        duckdb_store, "000009",
        total_profit=[100.0] * 5, financial_expenses=[0.0] * 5,
    )

    report = RoceBuilder(duck=duckdb_store).build()

    assert report["status"] == "success"
    assert report["data_version"] == DATA_VERSION
    assert report["history_rows"] == len(YEARS)
    assert report["history_roce_ok"] == len(YEARS) - 1  # 首年无期初 CE
    # YEARS 是 5 份年报 → 4 个有效 ROCE 年，严格 5 年窗口拿不满，因此 0 只出值
    assert report["snapshot"]["roce_5y_median"] == 0
    assert report["snapshot"]["roce_10y_median"] == 0


def test_source_constant_is_recorded(duckdb_store: DuckDBStore) -> None:
    """溯源列必须写实，便于日后核对是哪一版口径算出来的。"""
    _seed_snapshot_row(duckdb_store, "000010")
    _seed_balance(
        duckdb_store, "000010",
        total_assets=[1000.0] * 5, total_current_liabilities=[0.0] * 5,
    )
    _seed_income(
        duckdb_store, "000010",
        total_profit=[100.0] * 5, financial_expenses=[0.0] * 5,
    )

    RoceBuilder(duck=duckdb_store).build()

    rows = duckdb_store.read_query(
        """SELECT source, data_version, status FROM roce_history
           WHERE stock_code = '000010' ORDER BY report_date"""
    )
    assert all(r["source"] == SOURCE for r in rows)
    assert all(r["data_version"] == DATA_VERSION for r in rows)
    assert rows[0]["status"] == STATUS_NO_PRIOR_PERIOD
    assert all(r["status"] == STATUS_OK for r in rows[1:])


# ─── 快照重发不得清空 ROCE 列（reports/131 §4.3，P0 定时炸弹）──────────────


def test_snapshot_republish_preserves_roce_columns(duckdb_store: DuckDBStore) -> None:
    """`indicator_snapshot` 的 DELETE+INSERT BY NAME 会把 roce_* 写回 NULL。

    `roce_*` 五列由 `roce_history` 聚合成，**不在计算器 staging 表里**；
    `_publish_snapshot` 必须在 INSERT 之后重新聚合一次，否则每次快照重发
    都会把全市场 ROCE 静默清空（D28 同型事故）。
    """
    from app.core.indicators.roce import (
        ELIGIBLE_MIN_MARKET_CAP,
        ELIGIBLE_MIN_YEARS,
        SNAPSHOT_UPDATE_SQL,
    )

    _seed_n_years(duckdb_store, "000011", 6)  # 6 份年报 → 5 个有效 ROCE 年
    RoceBuilder(duck=duckdb_store).build()
    before = _snapshot(duckdb_store, "000011")
    assert before["roce_5y_median"] is not None

    # 模拟计算器 republish：先清空这五列（等价于 staging 表缺列时的 BY NAME 行为）
    duckdb_store.write_query(
        """UPDATE indicator_snapshot
           SET roce_latest=NULL, roce_5y_median=NULL, roce_5y_years=NULL,
               roce_10y_median=NULL, roce_10y_years=NULL
           WHERE stock_code='000011'"""
    )
    assert _snapshot(duckdb_store, "000011")["roce_5y_median"] is None

    # publish 末尾的重新聚合必须把它们恢复
    with duckdb_store.write_connection() as conn:
        conn.execute(SNAPSHOT_UPDATE_SQL, [ELIGIBLE_MIN_YEARS, ELIGIBLE_MIN_MARKET_CAP])

    after = _snapshot(duckdb_store, "000011")
    assert after["roce_5y_median"] == pytest.approx(before["roce_5y_median"])
    assert after["roce_5y_years"] == before["roce_5y_years"]


# ─── 筛选口径 roce_eligible（reports/132）────────────────────────────────


def _seed_eligibility(
    duck: DuckDBStore, code: str, *, years: int, mcap: float, industry: str | None,
) -> None:
    """构造 n 年连续年报 + 快照行（市值/行业可控）。"""
    import datetime as _dt

    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed, sw_level1, listing_date) "
        "VALUES (?, ?, 'SZSE', true, ?, CAST('2000-01-01' AS DATE))",
        [code, code, industry],
    )
    for i in range(years + 1):  # +1：第一条没有期初资本
        report_date = f"{2010 + i}-12-31"
        duck.write_query(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_current_liabilities)
               VALUES (?, CAST(? AS DATE), 1000, 400)""",
            [code, report_date],
        )
        duck.write_query(
            """INSERT INTO income_statement (stock_code, report_date, total_profit, financial_expenses)
               VALUES (?, CAST(? AS DATE), 100, 0)""",
            [code, report_date],
        )
    duck.write_query(
        """INSERT INTO indicator_snapshot
               (stock_code, report_date, calculated_at, data_version, total_market_cap)
           VALUES (?, CAST(? AS DATE), now(), 'fixture', ?)""",
        [code, f"{2010 + years}-12-31", mcap],
    )
    _ = _dt  # 保持导入可读性


def _eligible(duck: DuckDBStore, code: str) -> Any:
    rows = duck.read_query(
        "SELECT roce_eligible, roce_5y_years, roce_10y_years "
        "FROM indicator_snapshot WHERE stock_code = ?",
        [code],
    )
    return rows[0]


def test_eligible_when_all_three_conditions_met(duckdb_store: DuckDBStore) -> None:
    """非金融 + 市值 ≥ 100 亿 + 有效 ROCE 年数 ≥ 5 → 可筛选。"""
    _seed_eligibility(duckdb_store, "100001", years=9, mcap=2e10, industry="食品饮料")
    RoceBuilder(duck=duckdb_store).build()
    row = _eligible(duckdb_store, "100001")
    # 窗口年数是「该窗口实际用到的年数」，5 年窗口最多报 5
    assert row["roce_5y_years"] == 5
    assert row["roce_10y_years"] == 9
    assert row["roce_eligible"] is True, "门槛是 5 年有效 ROCE（书里的下限），不是 8 年"


def test_ineligible_when_market_cap_below_100_yi(duckdb_store: DuckDBStore) -> None:
    """市值 < 100 亿 → 不可筛选（微盘分母噪音）。"""
    _seed_eligibility(duckdb_store, "100002", years=9, mcap=5e9, industry="食品饮料")
    RoceBuilder(duck=duckdb_store).build()
    assert _eligible(duckdb_store, "100002")["roce_eligible"] is False


def test_ineligible_when_history_shorter_than_min_years(duckdb_store: DuckDBStore) -> None:
    """有效 ROCE 年数 < 门槛（`ELIGIBLE_MIN_YEARS` = 5，v46）→ 不可筛选。

    用「有效年数」而不是「上市满 N 年」：算单年 ROCE 需要上一年的资本，
    上市满 5 年实际只够算出 4 个值（书里要求「过去 5 到 10 年」的历史）。
    """
    # 5 份年报 → 4 个有效 ROCE 年 < 5，不满足门槛
    _seed_eligibility(duckdb_store, "100003", years=4, mcap=2e10, industry="食品饮料")
    RoceBuilder(duck=duckdb_store).build()
    row = _eligible(duckdb_store, "100003")
    assert row["roce_5y_years"] == 4
    assert row["roce_eligible"] is False


def test_ineligible_for_financial_sector(duckdb_store: DuckDBStore) -> None:
    """金融股 → 不可筛选。

    银行/保险/券商没有流动-非流动划分、利润表无「财务费用」行，
    ROCE 在会计上不适用（书里也明写「非金融企业的 ROCE」）。
    """
    _seed_eligibility(duckdb_store, "100004", years=9, mcap=5e10, industry="银行")
    RoceBuilder(duck=duckdb_store).build()
    assert _eligible(duckdb_store, "100004")["roce_eligible"] is False


# ─── v47 持久性两列 roce_10y_min / roce_10y_cv（reports/139 §7 前置 3 / #27）──
#
# #27「ROCE 持久性」的全部内容就是这两列：10 年地板（min）与 10 年稳定性（cv）。
# 窗口纪律与 roce_10y_median 完全一致（**恰好** 10 个有效年报），cv 另加一道
# 分母门：MEDIAN(roce) >= 0.02，否则 NULL——实测 158 只 cv>10 全部由
# 0 < 中位 < 0.01 造成的伪变异系数（reports/139 #27）。


def _seed_history(duck: DuckDBStore, code: str, values: list[float]) -> None:
    """直接铺 `roce_history`（绕过报表），让 10 年窗口的统计量可以手算。"""
    for index, value in enumerate(values):
        duck.write_query(
            """INSERT INTO roce_history
                   (stock_code, report_date, ce, ce_prev, ebit, roce, status,
                    calculated_at, source, data_version)
               VALUES (?, CAST(? AS DATE), 1000, 1000, 200, ?, 'OK', now(), 'fixture', 1)""",
            [code, f"{2000 + index}-12-31", value],
        )


def _rebuild(duck: DuckDBStore) -> None:
    """跑派生域登记的正式重建钩子（快照发布路径用的就是这一个）。"""
    from app.core.indicators.roce import rebuild_snapshot_columns

    with duck.write_connection() as connection:
        rebuild_snapshot_columns(connection)


def _persistence(duck: DuckDBStore, code: str) -> dict[str, Any]:
    rows = duck.read_query(
        """SELECT roce_10y_median, roce_10y_years, roce_10y_min, roce_10y_cv
           FROM indicator_snapshot WHERE stock_code = ?""",
        [code],
    )
    return rows[0]


def test_registry_and_rebuild_sql_agree_column_for_column(
    duckdb_store: DuckDBStore,
) -> None:
    """**登记面 == 重建 SQL 的 SET 列表**（RG4 的进程内版本，双向一致）。

    v46 加 20y 两列时只改了 SQL 没改登记 → `scripts/check_rework_guards.py` RG4
    红了整整一轮（reports/139 X-V46）。少登记一列的后果不是"少一列值"，而是
    快照 DELETE+INSERT 重发时该列**不保全**，下一次发布就把它清成 NULL（K5 同型）。
    """
    import re

    from app.core.indicators.roce import SNAPSHOT_DERIVED_COLUMNS, SNAPSHOT_UPDATE_SQL

    set_clause = re.search(r"\bSET\b(.*?)\bFROM\b", SNAPSHOT_UPDATE_SQL, re.S | re.I)
    assert set_clause, "SNAPSHOT_UPDATE_SQL 里找不到 UPDATE ... SET ... FROM"
    sql_columns = set(re.findall(r"([a-z_][a-z0-9_]*)\s*=", set_clause.group(1)))
    registered = set(SNAPSHOT_DERIVED_COLUMNS)

    assert sql_columns == registered, (
        f"重建 SQL 写了但没登记（不保全）={sorted(sql_columns - registered)}；"
        f"登记了但 SQL 没写={sorted(registered - sql_columns)}"
    )
    assert len(registered) == len(SNAPSHOT_DERIVED_COLUMNS), "登记元组里有重复列"
    for column in ("roce_20y_median", "roce_20y_years", "roce_10y_min", "roce_10y_cv"):
        assert column in registered, f"{column} 必须进 SNAPSHOT_DERIVED_COLUMNS"
        assert column in sql_columns, f"{column} 必须出现在 SNAPSHOT_UPDATE_SQL 的 SET 里"

    # DDL 与 v47 迁移两面都要真的把列建出来（fixture 走 init_duckdb_schema 的 DDL 路径）
    columns = {
        str(row["column_name"])
        for row in duckdb_store.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'indicator_snapshot'"
        )
    }
    assert {"roce_10y_min", "roce_10y_cv"} <= columns


def test_persistence_columns_filled_after_rebuild_with_ten_years(
    duckdb_store: DuckDBStore,
) -> None:
    """走真实报表链路：11 份年报 → 10 个有效 ROCE 年 → min / cv 都必须有值。"""
    _seed_snapshot_row(duckdb_store, "300001")
    # 11 份年报 → LAG 后 10 个有效年；CE 恒 1000，故 roce = 利润 ÷ 1000
    profits = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0, 10.0]
    years = tuple(f"{2000 + i}-12-31" for i in range(len(profits)))
    _seed_balance(
        duckdb_store, "300001",
        total_assets=[1000.0] * 11, total_current_liabilities=[0.0] * 11, years=years,
    )
    _seed_income(
        duckdb_store, "300001",
        total_profit=profits, financial_expenses=[0.0] * 11, years=years,
    )

    RoceBuilder(duck=duckdb_store).build()

    row = _persistence(duckdb_store, "300001")
    expected = [value / 1000.0 for value in profits[1:]]
    assert row["roce_10y_years"] == 10
    assert row["roce_10y_min"] == pytest.approx(min(expected))
    assert row["roce_10y_median"] == pytest.approx(0.045)  # (0.04 + 0.05) / 2
    assert row["roce_10y_cv"] == pytest.approx(statistics.stdev(expected) / 0.045)


def test_persistence_columns_are_null_below_ten_valid_years(
    duckdb_store: DuckDBStore,
) -> None:
    """严格窗口：只有 5 个有效年报时 min / cv 必须留空（不许 5 年冒充 10 年）。"""
    _seed_n_years(duckdb_store, "300002", 6)  # 6 份年报 → 5 个有效 ROCE 年
    RoceBuilder(duck=duckdb_store).build()

    row = _persistence(duckdb_store, "300002")
    assert row["roce_10y_years"] == 5
    assert row["roce_10y_median"] is None
    assert row["roce_10y_min"] is None, "5 年数据不能冒充 10 年地板"
    assert row["roce_10y_cv"] is None, "5 年数据不能冒充 10 年稳定性"


def test_cv_is_null_when_median_below_two_percent(duckdb_store: DuckDBStore) -> None:
    """**cv 分母门**：MEDIAN(roce) = 0.005 → cv NULL，但 min 照常出值。

    实测 158 只 cv>10 / 12 只 cv>100 全部由 0 < 中位 < 0.01 造成：中位趋零时
    σ/中位 是算术产物，不是"波动"。这里挡的是**分母失去解释力**，不是极值
    （与"不设比例护栏"的裁决不冲突：min 与 median 都照原样输出）。
    """
    _seed_snapshot_row(duckdb_store, "300003")
    _seed_history(duckdb_store, "300003", [0.004] * 5 + [0.006] * 5)
    _rebuild(duckdb_store)

    row = _persistence(duckdb_store, "300003")
    assert row["roce_10y_years"] == 10
    assert row["roce_10y_median"] == pytest.approx(0.005)
    assert row["roce_10y_min"] == pytest.approx(0.004), "分母门只挡 cv，不挡 min"
    assert row["roce_10y_cv"] is None, "中位 0.5% 时的 σ/中位 是伪变异系数"


def test_cv_is_computed_at_the_two_percent_boundary(duckdb_store: DuckDBStore) -> None:
    """分母门是 `>= 0.02`（含边界）：中位恰好 2% 时必须出值。"""
    _seed_snapshot_row(duckdb_store, "300004")
    values = [0.01] * 4 + [0.02] * 2 + [0.03] * 4  # 中位 = (0.02 + 0.02) / 2 = 0.02
    _seed_history(duckdb_store, "300004", values)
    _rebuild(duckdb_store)

    row = _persistence(duckdb_store, "300004")
    assert row["roce_10y_median"] == pytest.approx(0.02)
    assert row["roce_10y_min"] == pytest.approx(0.01)
    assert row["roce_10y_cv"] == pytest.approx(statistics.stdev(values) / 0.02)


def test_persistence_columns_survive_snapshot_republish(duckdb_store: DuckDBStore) -> None:
    """min / cv 与其余 ROCE 列一样，重发后必须被重建回值（不是一次性写入）。"""
    _seed_snapshot_row(duckdb_store, "300005")
    _seed_history(duckdb_store, "300005", [0.10] * 10)
    _rebuild(duckdb_store)
    before = _persistence(duckdb_store, "300005")
    assert before["roce_10y_min"] == pytest.approx(0.10)
    assert before["roce_10y_cv"] == pytest.approx(0.0)

    duckdb_store.write_query(
        """UPDATE indicator_snapshot
           SET roce_10y_min = NULL, roce_10y_cv = NULL
           WHERE stock_code = '300005'"""
    )
    _rebuild(duckdb_store)

    after = _persistence(duckdb_store, "300005")
    assert after["roce_10y_min"] == before["roce_10y_min"]
    assert after["roce_10y_cv"] == before["roce_10y_cv"]


# ─── 金融股判据单一真相源（app/core/sector.py，reports/139 §5.1 / §7 前置 1）──


def test_non_financial_predicate_keeps_null_industry_rows(
    duckdb_store: DuckDBStore,
) -> None:
    """`sw_level1 IS NULL` 的行不能被三值逻辑静默丢掉（全库 751 行）。

    手写 `NOT IN ('银行','非银金融')` 对 NULL 求值为 NULL → WHERE 过滤掉整行，
    "非金融"筛选会凭空少 751 只。`is_non_financial_sql()` 用 COALESCE 把它变成
    TRUE，并用 csrc 兜底只判「申万缺失 + 证监会金融业」。
    """
    from app.core.indicators.roce import SNAPSHOT_UPDATE_SQL
    from app.core.sector import is_non_financial_sql

    assert is_non_financial_sql("meta") in SNAPSHOT_UPDATE_SQL, (
        "roce.py 必须用 sector.is_non_financial_sql()，不许再手写行业字符串"
    )
    assert "NOT IN ('银行', '非银金融')" not in SNAPSHOT_UPDATE_SQL

    rows = duckdb_store.read_query(
        f"""SELECT m.sw_level1 AS label, m.csrc_l1 AS csrc_l1,
                   {is_non_financial_sql('m')} AS non_financial
            FROM (VALUES
                ('银行',      NULL),
                ('非银金融',  NULL),
                (NULL,        NULL),
                (NULL,        '金融业'),
                ('食品饮料',  '制造业')
            ) AS m(sw_level1, csrc_l1)"""
    )
    verdict = {(row["label"], row["csrc_l1"]): row["non_financial"] for row in rows}
    assert verdict[("银行", None)] is False
    assert verdict[("非银金融", None)] is False
    assert verdict[(None, None)] is True, "sw_level1 为 NULL 的行必须保留"
    assert verdict[(None, "金融业")] is False, "csrc 兜底：申万缺失 + 证监会金融业 → 金融股"
    assert verdict[("食品饮料", "制造业")] is True


def test_eligible_when_industry_label_is_null(duckdb_store: DuckDBStore) -> None:
    """端到端：`sw_level1 IS NULL` + 非金融 csrc + 市值够 → 仍然可筛选。"""
    _seed_eligibility(duckdb_store, "100005", years=9, mcap=2e10, industry=None)
    RoceBuilder(duck=duckdb_store).build()
    assert _eligible(duckdb_store, "100005")["roce_eligible"] is True, (
        "行业标签缺失不等于金融股，不能把 751 行 NULL 静默丢掉"
    )


def test_ineligible_when_sw_missing_but_csrc_is_financial(
    duckdb_store: DuckDBStore,
) -> None:
    """csrc 兜底：申万一级缺失但证监会门类是「金融业」→ 仍按金融股剔除。"""
    _seed_eligibility(duckdb_store, "100006", years=9, mcap=2e10, industry=None)
    duckdb_store.write_query(
        "UPDATE stock_meta SET csrc_l1 = '金融业' WHERE stock_code = ?", ["100006"]
    )
    RoceBuilder(duck=duckdb_store).build()
    assert _eligible(duckdb_store, "100006")["roce_eligible"] is False
