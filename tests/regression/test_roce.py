"""ROCE（已动用资本回报率）域回归（schema v42，2026-11-16，reports/130）。

口径来源：《我从达尔文那里学到的投资知识》第 2 章 + 用户 2026-11-16 裁决。

    已动用资本 CE = 总资产 − 流动负债        （= 净营运资本 + 全部长期资产）
    EBIT         = 利润总额 + 财务费用
    单年 ROCE    = 当年 EBIT ÷ ((上年 CE + 本年 CE) / 2)
    快照值       = 最近 5 / 10 个**有效年报** ROCE 的中位数

本文件钉死四件事，每一条都对应一次真实的口径事故：

1. **分母必须是「总资产 − 流动负债」，不是「净营运资本 + fixed_assets」**。
   只取 `fixed_assets`(PPE) 会漏掉油气资产/长期应收款/在建工程等长期资产，
   实测把中国海油算成 119% 高于贵州茅台（reports/130 §4）。
2. **不剔现金**。按 `现金 − 债务` 全额剔除会把白酒/家电的分母掏空：格力已动用资本
   在 −204 亿到 +26 亿之间穿过零点，ROCE 从 −81% 跳到 4,405%；五粮液 2024 年分母
   −54 亿直接算出 −7,194%。书里那句「如果你存疑，也可以将部分现金计入投入资本」
   就是给这种情况的授权。
3. **不设任何比例护栏**（用户裁决：极值不假，假的是数据源）。唯一的 NULL 来自
   除法不成立（CE ≤ 0），`status` 如实标注，CE 原值保留可复核。
4. **只吃年报**。季报累计值直接除年平均资本会系统性低估。
"""

from __future__ import annotations

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
) -> None:
    for report_date, ta, tcl in zip(YEARS, total_assets, total_current_liabilities, strict=True):
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
) -> None:
    for report_date, tp, fin in zip(YEARS, total_profit, financial_expenses, strict=True):
        duck.write_query(
            """INSERT INTO income_statement (stock_code, report_date, total_profit, financial_expenses)
               VALUES (?, CAST(? AS DATE), ?, ?)""",
            [code, report_date, tp, fin],
        )


def _snapshot(duck: DuckDBStore, code: str) -> dict[str, Any]:
    rows = duck.read_query(
        """SELECT roce_latest, roce_5y_median, roce_5y_years, roce_10y_median, roce_10y_years
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


def test_snapshot_median_and_year_counts(duckdb_store: DuckDBStore) -> None:
    """快照 = 最近 N 个有效年报的中位数，并如实记录用到了几年。"""
    _seed_snapshot_row(duckdb_store, "000006")
    # CE 固定 1000；EBIT 依次 100/200/300 —— ROCE = 10% / 20% / 30%
    _seed_balance(
        duckdb_store, "000006",
        total_assets=[1000.0] * 5, total_current_liabilities=[0.0] * 5,
    )
    _seed_income(
        duckdb_store, "000006",
        total_profit=[100.0, 200.0, 300.0, 100.0, 100.0],
        financial_expenses=[0.0] * 5,
    )

    RoceBuilder(duck=duckdb_store).build()

    snap = _snapshot(duckdb_store, "000006")
    # 有效年报 4 个（2019 无期初 CE）→ ROCE 为 2020/2021/2022/2023 = 10%/20%/30%/10%
    assert snap["roce_5y_years"] == 4
    assert snap["roce_latest"] == pytest.approx(0.10)          # 2023 年
    assert snap["roce_5y_median"] == pytest.approx(0.15)       # median(0.1,0.2,0.3,0.1)
    assert snap["roce_10y_years"] == 4
    assert snap["roce_10y_median"] == pytest.approx(0.15)


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
    assert report["snapshot"]["roce_5y_median"] == 1


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
    from app.core.indicators.roce import SNAPSHOT_UPDATE_SQL

    _seed_snapshot_row(duckdb_store, "000011")
    _seed_balance(
        duckdb_store, "000011",
        total_assets=[1000.0] * 5, total_current_liabilities=[0.0] * 5,
    )
    _seed_income(
        duckdb_store, "000011",
        total_profit=[100.0] * 5, financial_expenses=[0.0] * 5,
    )
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
        conn.execute(SNAPSHOT_UPDATE_SQL)

    after = _snapshot(duckdb_store, "000011")
    assert after["roce_5y_median"] == pytest.approx(before["roce_5y_median"])
    assert after["roce_5y_years"] == before["roce_5y_years"]
