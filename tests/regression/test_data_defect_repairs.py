"""数据瑕疵修复 op 的回归（reports/146 §6）。

覆盖两条 P1/P2：
1. `company_employee_history`：东财批次把 `report_date` 写成**抓取当天**
   （2026-08-25 ~ 2026-09-17，5,565 行），导致 `2025-12-31` 这一期整期缺失
   （CSMAR 只到 2024-12-31）。→ 改期到「抓取时能看到的最新年度」`YYYY-12-31`。
2. `report_metadata.declare_date` 仅 36,202/356,728 = **10.15%**，
   而 `financial_report_dates.announce_date` 是 369,648/369,648 = **100%**。
   → 按 `(stock_code, report_date)` 回填（实测可回填 266,948 行）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import repair_data_defects as repairs  # noqa: E402

from app.core.report_periods import annual_period_for  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402


def test_annual_period_for_maps_fetch_date_back() -> None:
    """抓取日 2026-09-11 → 当时能看到的最新年度 = 2025-12-31。"""
    assert annual_period_for(date(2026, 9, 11)) == date(2025, 12, 31)
    assert annual_period_for(date(2026, 1, 15)) == date(2025, 12, 31)
    # 已经是年度期末就原样返回
    assert annual_period_for(date(2024, 12, 31)) == date(2024, 12, 31)
    assert annual_period_for(None) is None


def _seed_employee(
    duck: DuckDBStore, rows: list[tuple[str, str, int, str]],
) -> None:
    for code, report_date, count, source in rows:
        duck.write_query(
            """INSERT INTO company_employee_history
                   (stock_code, report_date, employee_count, source, fetch_time, batch_id)
               VALUES (?, CAST(? AS DATE), ?, ?, now(), 'fixture')""",
            [code, report_date, count, source],
        )


def test_employee_plan_targets_annual_period(duckdb_store: DuckDBStore) -> None:
    _seed_employee(duckdb_store, [
        ("000001", "2024-12-31", 40000, "csmar"),          # 标准期 → 不动
        ("000001", "2026-09-11", 41043, "eastmoney_f10"),  # 抓取日 → 改期
        ("000002", "2026-09-11", 131429, "eastmoney_f10"),
    ])
    plan = repairs.plan_employee_periods(duckdb_store)

    assert len(plan["moves"]) == 2
    assert {item["to"] for item in plan["moves"]} == {"2025-12-31"}
    assert all(item["from"].startswith("2026-09") for item in plan["moves"])


def test_employee_apply_moves_rows(duckdb_store: DuckDBStore) -> None:
    _seed_employee(duckdb_store, [("000001", "2026-09-11", 41043, "eastmoney_f10")])
    plan = repairs.plan_employee_periods(duckdb_store)
    moved, dropped = repairs.apply_employee_periods(duckdb_store, plan)

    assert (moved, dropped) == (1, 0)
    periods = [
        str(row["report_date"])
        for row in duckdb_store.read_query(
            "SELECT report_date FROM company_employee_history WHERE stock_code='000001'"
        )
    ]
    assert periods == ["2025-12-31"], "必须落到年度期末，不能停在抓取日"


def test_employee_apply_drops_when_target_exists(duckdb_store: DuckDBStore) -> None:
    """目标期已有同源行 → 丢弃错记行（不制造重复）。"""
    _seed_employee(duckdb_store, [
        ("000001", "2025-12-31", 41000, "eastmoney_f10"),
        ("000001", "2026-09-11", 41043, "eastmoney_f10"),
    ])
    plan = repairs.plan_employee_periods(duckdb_store)
    moved, dropped = repairs.apply_employee_periods(duckdb_store, plan)

    assert (moved, dropped) == (0, 1)
    rows = duckdb_store.read_query(
        "SELECT employee_count FROM company_employee_history WHERE stock_code='000001'"
    )
    assert [row["employee_count"] for row in rows] == [41000], "保留已存在的年度行"


def test_declare_date_backfill_plan_and_apply(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO report_metadata
               (stock_code, report_date, declare_date, source, fetched_at)
           VALUES ('000001', CAST('2024-12-31' AS DATE), NULL, 'fixture', now())"""
    )
    duckdb_store.write_query(
        """INSERT INTO financial_report_dates
               (stock_code, report_date, announce_date, source, fetch_time, batch_id)
           VALUES ('000001', CAST('2024-12-31' AS DATE), CAST('2025-04-20' AS DATE),
                   'fixture', now(), 'fixture')"""
    )

    plan = repairs.plan_declare_date_backfill(duckdb_store)
    assert plan["fillable"] == 1
    assert repairs.apply_declare_date_backfill(duckdb_store) == 1

    value = duckdb_store.read_query(
        "SELECT declare_date FROM report_metadata WHERE stock_code='000001'"
    )[0]["declare_date"]
    assert str(value) == "2025-04-20"
