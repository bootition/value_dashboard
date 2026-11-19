"""报告期合法性 + 非标准期修复脚本（reports/146）。

事故
----
`cash_flow_indirect` 有 **5,481 行 `report_date = 2024-01-01`**，且 2013–2024
**每年**都有一个 `YYYY-01-01` 批次，与 `(YYYY-1)-12-31` 在 90%+ 股票上数值逐位相同
（实测 2024-01-01 vs 2023-12-31：5,382 只两期都有值，4,885 只完全相同）。
CSMAR 历史批次把「上年年报」重复记成了「次年 1 月 1 日」。

全库 dry-run 实测（reports/146 §2）：

    cash_flow_indirect   非标准期 62,369 行 → 可删重复 58,022 / 可改期 4,347
    cash_flow_activity   非标准期 62,370 行 → 可删重复 58,087 / 可改期 4,283
    其余 5 张表          非标准期 0 行

本文件钉死：
1. 合法报告期判定（只认 03-31 / 06-30 / 09-30 / 12-31）；
2. `YYYY-01-01` → `(YYYY-1)-12-31` 的错记规则；
3. 修复脚本的两条分支：上期年报存在 → 删除；不存在 → 改期；
4. **写库必须显式二次确认**（默认 dry-run）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import repair_nonstandard_report_dates as repair  # noqa: E402

from app.core.report_periods import (  # noqa: E402
    canonical_year_start_target,
    is_standard_report_date,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402


@pytest.mark.parametrize(
    "value,expected",
    [
        (date(2026, 3, 31), True),
        (date(2026, 6, 30), True),
        (date(2026, 9, 30), True),
        (date(2026, 12, 31), True),
        (date(2024, 1, 1), False),
        (date(2026, 1, 31), False),
        (date(2026, 2, 28), False),
        ("2026-06-30", True),
        ("2024-01-01", False),
        (None, False),
        ("", False),
        ("not-a-date", False),
    ],
)
def test_is_standard_report_date(value: object, expected: bool) -> None:
    assert is_standard_report_date(value) is expected


def test_year_start_maps_to_previous_annual() -> None:
    """`YYYY-01-01` 的固定错法 → 它本该属于 `(YYYY-1)-12-31`。"""
    assert canonical_year_start_target(date(2024, 1, 1)) == date(2023, 12, 31)
    assert canonical_year_start_target(date(2001, 1, 1)) == date(2000, 12, 31)
    # 非 01-01 不归本规则管
    assert canonical_year_start_target(date(2024, 2, 1)) is None
    assert canonical_year_start_target(date(2023, 12, 31)) is None


def _seed_indirect(duck: DuckDBStore, rows: list[tuple[str, str, float | None]]) -> None:
    for code, report_date, dep in rows:
        duck.write_query(
            """INSERT INTO cash_flow_indirect
                   (stock_code, report_date, report_type, fixed_asset_depreciation,
                    source, fetch_time, raw_response_hash, confidence, batch_id)
               VALUES (?, CAST(? AS DATE), 'fixture', ?,
                       'fixture', now(), 'hash', 1.0, 'batch-fixture')""",
            [code, report_date, dep],
        )


def test_plan_deletes_duplicate_when_previous_annual_exists(duckdb_store: DuckDBStore) -> None:
    """`2024-01-01` 与 `2023-12-31` 同时存在 → 判为可删重复。"""
    _seed_indirect(duckdb_store, [
        ("000001", "2023-12-31", 18.79),
        ("000001", "2024-01-01", 18.79),
        ("000002", "2023-12-31", 40.93),
        ("000002", "2024-01-01", 40.93),
    ])
    plan = repair._plan_table(duckdb_store, "cash_flow_indirect")

    assert plan.nonstandard_rows == 2
    assert len(plan.deletable) == 2
    assert plan.reschedulable == []


def test_plan_reschedules_orphan_year_start(duckdb_store: DuckDBStore) -> None:
    """`YYYY-01-01` 是唯一副本（无上期年报）→ 判为改期而不是删除。"""
    _seed_indirect(duckdb_store, [("600145", "2008-01-01", 5.0)])
    plan = repair._plan_table(duckdb_store, "cash_flow_indirect")

    assert plan.reschedulable == [("600145", "2008-01-01", "2007-12-31")]
    assert plan.deletable == []


def test_plan_ignores_standard_rows(duckdb_store: DuckDBStore) -> None:
    _seed_indirect(duckdb_store, [
        ("000001", "2024-03-31", 1.0),
        ("000001", "2024-06-30", 2.0),
        ("000001", "2024-09-30", 3.0),
        ("000001", "2024-12-31", 4.0),
    ])
    plan = repair._plan_table(duckdb_store, "cash_flow_indirect")

    assert plan.nonstandard_rows == 0
    assert not plan.needs_write


def test_apply_deletes_and_reschedules(duckdb_store: DuckDBStore) -> None:
    """执行计划：重复行删除、孤行改期，标准行不受影响。"""
    _seed_indirect(duckdb_store, [
        ("000001", "2023-12-31", 18.79),
        ("000001", "2024-01-01", 18.79),   # 重复 → 删
        ("600145", "2008-01-01", 5.0),     # 孤行 → 改期为 2007-12-31
        ("000002", "2024-06-30", 9.0),     # 标准期 → 不动
    ])
    plan = repair._plan_table(duckdb_store, "cash_flow_indirect")
    deleted, updated = repair._apply_plan(duckdb_store, plan)

    assert deleted == 1
    assert updated == 1
    remaining = {
        (row["stock_code"], str(row["report_date"]))
        for row in duckdb_store.read_query(
            "SELECT stock_code, report_date FROM cash_flow_indirect"
        )
    }
    assert remaining == {
        ("000001", "2023-12-31"),
        ("600145", "2007-12-31"),
        ("000002", "2024-06-30"),
    }


def test_default_mode_is_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认必须 dry-run：不传 `--apply --confirm-write` 时绝不写库。"""
    calls: list[str] = []

    def _fake_apply(duck, plan):  # noqa: ANN001, ANN202
        calls.append(plan.table)
        return (0, 0)

    monkeypatch.setattr(repair, "_apply_plan", _fake_apply)
    # 没有 --apply / --confirm-write → 走 dry-run 分支
    assert repair.main([]) == 0
    assert calls == [], "dry-run 不得调用写库"


def test_supplement_guard_drops_nonstandard_periods(duckdb_store: DuckDBStore) -> None:
    """写库守卫：补采落库时非标准报告期一律丢弃并记数（reports/146）。

    注意：`self._dropped_nonstandard_periods += 1` 依赖实例初始化，
    且**不能被 pandas NaN 判成合法日期**（NaT.strftime 会崩）。
    """
    import pandas as pd

    from app.core.supplement import SupplementFetcher

    fetcher = SupplementFetcher(duck=duckdb_store)
    frame = pd.DataFrame([
        {"stock_code": "000001", "report_date": pd.Timestamp("2024-03-31")},
        {"stock_code": "000001", "report_date": pd.Timestamp("2024-01-01")},
        {"stock_code": "000002", "report_date": pd.NaT},
    ])

    kept = fetcher._drop_nonstandard_periods(frame, table="cash_flow_indirect")

    assert list(kept["report_date"].dt.strftime("%Y-%m-%d")) == ["2024-03-31"]
    assert fetcher.last_dropped_nonstandard_periods == 2
    assert {item["report_date"] for item in fetcher.last_dropped_nonstandard_sample} == {
        "2024-01-01", "NaT",
    }
