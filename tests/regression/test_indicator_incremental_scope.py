"""指标重算**只算变化的部分**（reports/145）：增量 vs 全量的判据与结果等价性。

本文件钉的是"用户催了很久的核心功能"的五条硬约束：

1. **不变 → 一只都不算**：同一份数据连跑两轮，第二轮 `mode=incremental`、
   `recomputed_count==0`，且真的没有调用计算器（不是"算了但没发布"）。
2. **单股变化 → 只算它**：改一只股票的股本/财报/价格，`recomputed_codes` 恰好是它。
3. **股本变化不再等于全量**：受影响代码集 = 逐股指纹差异集（不是全市场 5575 只），
   并用"全量路径一被调用就炸"的假计算器把旧实现（`compute_snapshot_for_all`）
   钉成必红 —— 这就是 reports/145 §变异验证 的常驻版本。
4. **全量只由可解释原因触发**：`first_run` / `computation_version` /
   `incremental_over_threshold`，且 reason 出现在 `report_step("indicators")`。
5. **结果等价（最重要）**：增量跑完的 `indicator_snapshot` 与"同一份数据全量重算"
   **逐行相等**（除 `calculated_at` 时间戳外所有列，50 只样本实测）。

为什么等价断言不能省：增量路径按"完整/价格域/事件域"三条不同的计算函数发布，
任何一条把该算的股票漏在集合外，指标就永久停在旧值——而 readiness 只会说
"价格与快照一致"，不会发现某个基本面列是上一代的。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.core.indicators import calculator as calculator_module
from app.core.indicators.calculator import IndicatorCalculator
from app.core.update import IncrementalUpdater
from app.core.update_parts.indicator_scope import (
    bootstrap_indicator_scope,
    plan_indicator_scope,
)
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed

PERIOD = "2025-12-31"


def _codes(n: int) -> list[str]:
    """合法的深市主板代码段（`Seed.stock` 按代码段推断交易所）。"""
    return [f"60{i:04d}" for i in range(n)]


def _seed_pool(duckdb_store, sqlite_store, codes: list[str]) -> None:
    seed = Seed(duckdb_store)
    for code in codes:
        seed.stock(
            code,
            name=f"S{code}",
            exchange="SZSE",
            listing_date="2020-01-01",
            is_st=False,
            is_suspended=False,
        )
        insert_minimum_screenable_data(duckdb_store, code)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # fixture 会顺手塞一行占位快照；本文件要的"第一轮"是**真空表**，
    # 否则 `first_run` 这条全量原因就测不到（会先命中 computation_version）。
    duckdb_store.write_query("DELETE FROM indicator_snapshot")


def _stub_network_steps(updater: IncrementalUpdater) -> None:
    """除"指标重算"以外的所有域都打桩：本文件不测抓取，只测重算范围。"""
    updater.run_incremental_check = lambda **kwargs: {
        "new_trading_days": [],
        "retry_tasks": [],
        "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"},
        "needs_update": True,
        "blocked": False,
    }
    updater._check_new_announcements = lambda persist=False, **kwargs: {
        "status": "available",
        "affected_stock_codes": [],
        "affected_announcements": {},
        "all_new_announcements": {},
    }
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "success",
        "succeeded_codes": list(codes),
    }
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success"}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {
        "status": "skipped",
        "success": 0,
    }
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}
    updater._refresh_financial_detail_backfill = lambda **kwargs: {
        "status": "skipped",
        "succeeded_codes": [],
    }
    updater._refresh_buyback = lambda: {"status": "skipped", "changed_codes": []}
    updater._refresh_funding = lambda: {"status": "skipped", "changed_codes": []}
    updater._refresh_treasury_curve = lambda: {"status": "skipped"}
    updater._refresh_business_overview = lambda **kwargs: {"status": "skipped"}
    updater._refresh_capital_history = lambda **kwargs: {"status": "skipped"}
    updater._refresh_research_statistics = lambda **kwargs: {"status": "skipped"}
    updater._refresh_etf_prices = lambda: {"status": "skipped"}
    updater._refresh_hk_dividends = lambda: {"status": "skipped"}
    updater._refresh_index_valuation = lambda: {"status": "skipped"}
    updater._refresh_analyst_forecast = lambda: {"status": "skipped"}
    updater._refresh_cashflow_supplement = lambda: {"status": "skipped"}
    updater._refresh_balance_sheet_ext = lambda: {"status": "skipped"}
    updater._refresh_deducted_profit = lambda: {"status": "skipped"}
    updater._refresh_fixed_assets = lambda: {"status": "skipped"}
    updater._refresh_regulatory_metrics = lambda: {"status": "skipped"}
    updater._refresh_indicator_ext = lambda: {"status": "skipped"}
    updater._refresh_roce = lambda: {"status": "skipped"}


def _updater(duckdb_store, sqlite_store) -> IncrementalUpdater:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_network_steps(updater)
    return updater


def _snapshot_rows(duckdb_store) -> list[tuple]:
    """快照逐行内容（除 `calculated_at`：每次计算必然不同，不是等价性的一部分）。"""
    columns = [
        str(row["column_name"])
        for row in duckdb_store.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'indicator_snapshot' ORDER BY ordinal_position"
        )
    ]
    selected = [name for name in columns if name != "calculated_at"]
    column_sql = ", ".join(f'"{name}"' for name in selected)
    rows = duckdb_store.read_query(
        f"SELECT {column_sql} FROM indicator_snapshot ORDER BY stock_code, report_date"
    )
    return [tuple(row.get(name) for name in selected) for row in rows]


def _advance_report_period(duckdb_store, code: str) -> None:
    """这只股票多了一个**完整**的 2026-03-31 报告期（三大报表同时到位）。"""
    seed = Seed(duckdb_store)
    seed.balance(
        code, "2026-03-31",
        total_assets=200.0, total_liabilities=40.0, total_equity=160.0,
    )
    seed.income(code, "2026-03-31", revenue=200.0, parent_net_profit=20.0)
    seed.cash_flow(code, "2026-03-31", cf_from_operating=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 不变 → 跳过（且真的没算）
# ─────────────────────────────────────────────────────────────────────────────


def test_unchanged_inputs_second_round_skips_and_is_much_faster(
    duckdb_store, sqlite_store,
) -> None:
    codes = _codes(12)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)

    started = time.perf_counter()
    first = updater.run_incremental_update()
    first_elapsed = time.perf_counter() - started
    first_step = first["steps"]["indicators"]
    assert first_step["mode"] == "full"
    assert first_step["reason"] == "first_run"
    assert first_step["recomputed_count"] == len(codes)

    # 第二轮：数据一个字节都没变。任何一次计算器调用都算 bug。
    def _explode(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError("输入没变却调用了指标计算器（全量/增量都不该算）")

    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    for name in (
        "compute_snapshot_for_all",
        "compute_snapshot_for_codes",
        "compute_price_sensitive_for_codes",
        "refresh_funding_dividend_fields",
        "refresh_treasury_spreads",
    ):
        if hasattr(calculator, name):
            setattr(calculator, name, _explode)
    updater_indicator = _updater(duckdb_store, sqlite_store)
    import unittest.mock as mock

    started = time.perf_counter()
    with mock.patch(
        "app.core.indicators.calculator.IndicatorCalculator", lambda **_kw: calculator,
    ):
        second = updater_indicator.run_incremental_update()
    second_elapsed = time.perf_counter() - started

    second_step = second["steps"]["indicators"]
    assert second_step["mode"] == "incremental"
    assert second_step["recomputed_count"] == 0, "第二轮必须一只都不算"
    assert second_step["reason"] == "no_changed_stocks"
    assert second_step["skipped_count"] == len(codes)
    assert all(count == 0 for count in second_step["changed_sources"].values())
    assert second_elapsed < first_elapsed / 2, (
        f"第二轮没有显著变快：first={first_elapsed:.2f}s second={second_elapsed:.2f}s"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 单股变化 → 只算它（股本 / 财报 / 价格 三条输入面各一只）
# ─────────────────────────────────────────────────────────────────────────────


def test_single_stock_change_recomputes_only_that_stock(duckdb_store, sqlite_store) -> None:
    codes = _codes(8)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()          # 第一轮：全量建立基线
    bootstrap_indicator_scope(updater)        # 快照已是当前口径 → 水位对齐

    target = codes[3]
    duckdb_store.write_query(
        "UPDATE stock_meta SET total_shares = 4321, circ_shares = 4000 "
        "WHERE stock_code = ?",
        [target],
    )

    computed: list[list[str]] = []
    real_for_codes = IndicatorCalculator.compute_snapshot_for_codes

    def spy(self, stock_codes, **kwargs):
        computed.append(list(stock_codes))
        return real_for_codes(self, stock_codes, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(IndicatorCalculator, "compute_snapshot_for_codes", spy):
        report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert step["mode"] == "incremental"
    assert step["changed_sources"]["universe_fingerprint"] == 1
    assert step["recomputed_codes"] == [target], (
        f"股本只变了一只，却重算了 {step['recomputed_codes'][:20]}"
    )
    assert computed == [[target]]
    assert step["recomputed_count"] == 1
    assert step["skipped_count"] == len(codes) - 1


def test_share_capital_changes_touch_only_affected_stocks(duckdb_store, sqlite_store) -> None:
    """3 只股本变了 → 只算这 3 只（旧实现是 5,575 只全量 35 分钟）。"""
    codes = _codes(10)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    changed = [codes[0], codes[4], codes[9]]
    for index, code in enumerate(changed):
        duckdb_store.write_query(
            "UPDATE stock_meta SET total_shares = ?, circ_shares = ? WHERE stock_code = ?",
            [1000.0 + index, 800.0 + index, code],
        )
    # 上市状态/ST/停牌/行业 同样进逐股指纹：改一只 ST 也要进集合
    duckdb_store.write_query(
        "UPDATE stock_meta SET is_st = TRUE WHERE stock_code = ?", [codes[6]],
    )

    report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert step["mode"] == "incremental", "股本变化不得再退回全量"
    assert sorted(step["recomputed_codes"]) == sorted([*changed, codes[6]])
    assert step["changed_sources"]["universe_fingerprint"] == 4
    assert step["recomputed_count"] == 4


def test_price_change_recomputes_price_domain_only(duckdb_store, sqlite_store) -> None:
    codes = _codes(6)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    target = codes[2]
    latest = duckdb_store.read_query(
        "SELECT MAX(trade_date) AS d FROM price_daily_raw WHERE stock_code = ?", [target],
    )[0]["d"]
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"UPDATE {table} SET close = 12.5 WHERE stock_code = ? AND trade_date = ?",
            [target, latest],
        )
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {
        "status": "success",
        "success": 1,
        "_updated_codes": [target],
        "_xdxr_codes": [],
        "_raw_full_refetch": False,
    }

    report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert step["mode"] == "incremental"
    assert step["changed_sources"]["price"] == 1
    assert step["recomputed_codes"] == [target]
    assert step["recomputed_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. 全量只由可解释原因触发
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_snapshot_first_run_goes_full(duckdb_store, sqlite_store) -> None:
    _seed_pool(duckdb_store, sqlite_store, _codes(4))
    updater = _updater(duckdb_store, sqlite_store)

    report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert step["mode"] == "full"
    assert step["reason"] == "first_run"
    assert report["indicator_scope"]["mode"] == "full"


def test_computation_version_bump_goes_full(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    codes = _codes(6)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    baseline_step = updater.run_incremental_update()["steps"]["indicators"]
    assert baseline_step["mode"] == "incremental", "版本没动时必须是增量"

    monkeypatch.setattr(
        calculator_module, "INDICATOR_COMPUTATION_VERSION", "2026-11-19.bumped",
    )

    report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert step["mode"] == "full"
    assert step["reason"] == "computation_version"
    assert step["computation_version"] == "2026-11-19.bumped"
    data_version = duckdb_store.read_query(
        "SELECT DISTINCT data_version FROM indicator_snapshot"
    )
    assert [row["data_version"] for row in data_version] == [
        "audit-safe-v1;calc=2026-11-19.bumped"
    ], "口径版本必须写进快照，否则事后查不出版本"


def test_incremental_over_threshold_falls_back_to_full(duckdb_store, sqlite_store) -> None:
    codes = _codes(12)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    # 9/12 = 75% > 60% 阈值 → 一次全扫比逐股维护更省
    for code in codes[:9]:
        duckdb_store.write_query(
            "UPDATE stock_meta SET total_shares = total_shares + 1 WHERE stock_code = ?",
            [code],
        )

    report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert step["mode"] == "full"
    assert step["reason"] == "incremental_over_threshold"
    assert step["changed_sources"]["universe_fingerprint"] == 9


# ─────────────────────────────────────────────────────────────────────────────
# 4. 变异守卫：股本变化**永远**不许调全量（旧实现一接回就红）
# ─────────────────────────────────────────────────────────────────────────────


def test_share_capital_change_never_calls_full_recompute(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """把"股本变化 → 全量"改回去，本用例必红（reports/145 §变异验证）。

    这不是结构断言：`compute_snapshot_for_all` 被换成"一调用就炸"的桩，
    用例本身仍然真跑一轮更新——行为只要退回旧实现就立刻变红。
    """
    codes = _codes(6)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    void = duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM indicator_snapshot"
    )[0]["c"]
    assert void == len(codes)

    def _full_is_forbidden(self, *args, **kwargs):
        raise AssertionError(
            "股本/上市名单变化触发了 compute_snapshot_for_all —— 又回到每轮全量 5575 只"
        )

    monkeypatch.setattr(
        IndicatorCalculator, "compute_snapshot_for_all", _full_is_forbidden,
    )
    duckdb_store.write_query(
        "UPDATE stock_meta SET total_shares = 777 WHERE stock_code = ?", [codes[1]],
    )
    # 旧实现靠"整体指纹变化"触发全量；新实现必须无视整体指纹、只算差异代码集。
    calls = {"n": 0}

    def _moving_fingerprint() -> str:
        calls["n"] += 1
        return "before" if calls["n"] == 1 else "after"

    monkeypatch.setattr(updater, "_share_capital_fingerprint", _moving_fingerprint)

    report = updater.run_incremental_update()

    step = report["steps"]["indicators"]
    assert report["share_capital_changed"] is True
    assert step["mode"] == "incremental"
    assert step["recomputed_codes"] == [codes[1]]


# ─────────────────────────────────────────────────────────────────────────────
# 5. 结果正确性：增量 vs 全量逐行相等（最重要）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pool_size", [50])
def test_incremental_snapshot_equals_full_recompute_row_by_row(
    duckdb_store, sqlite_store, pool_size: int,
) -> None:
    """混合改动后：增量跑完的快照 == 同一份数据全量重算的快照（逐行逐列）。

    样本 50 只（要求 50–200）。改动覆盖三条输入面：
    * 财报：4 只推进报告期、3 只修正同期数值（覆盖写）；
    * 股本：5 只改 total/circ（含一只 ST 状态翻转）；
    * 价格：6 只改最新收盘价（价格域路径）。
    """
    codes = _codes(pool_size)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    baseline = updater.run_incremental_update()["steps"]["indicators"]
    assert baseline["mode"] == "full"

    new_period = codes[0:4]
    restated = codes[4:7]
    shares = codes[7:11]
    price_changed = codes[11:17]

    for code in new_period:
        _advance_report_period(duckdb_store, code)
    for index, code in enumerate(restated):
        Seed(duckdb_store).balance(
            code, PERIOD, overwrite=True,
            total_assets=150.0 + index, total_liabilities=30.0, total_equity=120.0,
        )
    for index, code in enumerate(shares):
        duckdb_store.write_query(
            "UPDATE stock_meta SET total_shares = ?, circ_shares = ? WHERE stock_code = ?",
            [2000.0 + index, 1500.0 + index, code],
        )
    duckdb_store.write_query(
        "UPDATE stock_meta SET is_st = TRUE WHERE stock_code = ?", [shares[-1]],
    )
    for code in price_changed:
        latest = duckdb_store.read_query(
            "SELECT MAX(trade_date) AS d FROM price_daily_raw WHERE stock_code = ?", [code],
        )[0]["d"]
        for table in ("price_daily_raw", "price_daily_qfq"):
            duckdb_store.write_query(
                f"UPDATE {table} SET close = close * 1.05 "
                "WHERE stock_code = ? AND trade_date = ?",
                [code, latest],
            )
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {
        "status": "success",
        "success": len(price_changed),
        "_updated_codes": list(price_changed),
        "_xdxr_codes": [],
        "_raw_full_refetch": False,
    }

    report = updater.run_incremental_update()
    step = report["steps"]["indicators"]
    assert step["mode"] == "incremental", (
        f"这套改动不该触发全量，却走了 {step['reason']}"
    )
    assert step["changed_sources"]["universe_fingerprint"] == len(shares)
    assert step["changed_sources"]["financial_fingerprint"] >= len(restated)
    incremental_rows = _snapshot_rows(duckdb_store)
    assert len(incremental_rows) == pool_size

    # 同一份数据 → 全量重算一代，逐行逐列对比。
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    full_report = calculator.compute_snapshot_for_all()
    assert full_report["status"] == "success", full_report
    full_rows = _snapshot_rows(duckdb_store)
    assert len(full_rows) == len(incremental_rows)

    mismatches = [
        (index, incremental, full)
        for index, (incremental, full) in enumerate(zip(incremental_rows, full_rows, strict=True))
        if incremental != full
    ]
    assert not mismatches, (
        f"增量与全量结果不一致 {len(mismatches)}/{len(incremental_rows)} 行；"
        f"首例（行号 {mismatches[0][0]}）：{mismatches[0][1]} != {mismatches[0][2]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. 判据本身：指纹差异集合"宁可多算、不得漏算"
# ─────────────────────────────────────────────────────────────────────────────


def test_financial_fingerprint_detects_same_period_restatement(
    duckdb_store, sqlite_store,
) -> None:
    """同期修正值（报告期没变、数值变了）也必须进集合 —— 只看 report_date 会漏。"""
    codes = _codes(5)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    target = codes[2]
    Seed(duckdb_store).income(
        target, PERIOD, overwrite=True, revenue=999.0, parent_net_profit=99.0,
    )

    plan = plan_indicator_scope(updater, sources={})
    assert plan.mode == "incremental"
    assert plan.full_codes == [target], "同期覆盖写没被指纹差异抓到 = 会永久漏算"


def test_universe_fingerprint_ignores_irrelevant_metadata(duckdb_store, sqlite_store) -> None:
    """`updated_at`/name 每轮都会被刷新——它们进指纹就等于每轮全量。"""
    codes = _codes(4)
    _seed_pool(duckdb_store, sqlite_store, codes)
    updater = _updater(duckdb_store, sqlite_store)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    duckdb_store.write_query(
        "UPDATE stock_meta SET updated_at = now(), name = 'renamed'"
    )

    plan = plan_indicator_scope(updater, sources={})
    assert plan.mode == "incremental"
    assert plan.planned_codes == [], "updated_at/name 不是指标输入，不该触发重算"
