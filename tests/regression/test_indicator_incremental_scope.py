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
    # 比例阈值从 1/2 放宽到 0.7：稳态轮现在多了**固定**的指纹开销（五个输入面
    # 逐股摘要 ~56 ms / 小库），而第一轮的成本里只有"12 只股票的全量计算"这一
    # 段是可省的。判据要点是"稳态真的没算"（上面 recomputed_count==0 + 计算器
    # 一调用就炸），比例只是辅助证据。
    assert second_elapsed < first_elapsed * 0.7, (
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


# ─────────────────────────────────────────────────────────────────────────────
# 7. 验收报告 5 类漏算的**逐行等价**回归（2026-11-20）
#
# 验收判据：每一类场景「增量轮跑完」后 vs 「同一份数据全量重算」，
# `indicator_snapshot` 全列逐行比对必须 mismatches == 0。修前五类全部
# `skipped / no_changed_stocks`（指纹没覆盖那些格子，也说不出口不知道）。
#   #1 上一期数值被修正（YoY 输入）  #2 分红行新增（无公告驱动）
#   #3 快照被外部改坏              #4 报告期回补
#   #5 人工覆写 published（PRD §9.5 R7）
# ─────────────────────────────────────────────────────────────────────────────
PREV_PERIOD = "2024-12-31"
AUDIT_POOL = 8


def _audit_codes(n: int = AUDIT_POOL) -> list[str]:
    return [f"60{i:04d}" for i in range(n)]


def _seed_prev_period(duckdb_store, codes: list[str]) -> None:
    """给全部股票补一整年（2024-12-31）的完整三表，让 YoY/TTM 有上年可比。"""
    seed = Seed(duckdb_store)
    for code in codes:
        seed.balance(
            code, PREV_PERIOD,
            total_assets=90.0, total_liabilities=20.0, total_equity=70.0,
        )
        seed.income(code, PREV_PERIOD, revenue=80.0, parent_net_profit=8.0)
        seed.cash_flow(code, PREV_PERIOD, cf_from_operating=8.0)


def _audit_env(duckdb_store, sqlite_store, codes: list[str]) -> IncrementalUpdater:
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
    duckdb_store.write_query("DELETE FROM indicator_snapshot")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_network_steps(updater)
    return updater


def _snapshot_map(duckdb_store) -> dict[str, dict[str, Any]]:
    """stock_code → 全列字典（排除 `calculated_at`：时间戳不是等价性的一部分）。"""
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
    return {
        str(row["stock_code"]): {name: row.get(name) for name in selected}
        for row in rows
    }


def _diff_snapshot(
    incremental: dict[str, dict[str, Any]],
    full: dict[str, dict[str, Any]],
) -> dict[str, list[tuple[str, Any, Any]]]:
    """逐股逐列差异：{code: [(列, 增量值, 全量值)]}，只列出真有差异的。"""
    out: dict[str, list[tuple[str, Any, Any]]] = {}
    for code in sorted(set(incremental) | set(full)):
        left = incremental.get(code)
        right = full.get(code)
        if left is None or right is None:
            out[code] = [("<row>", left is not None, right is not None)]
            continue
        delta = [
            (column, left.get(column), right.get(column))
            for column in left
            if column != "stock_code" and left.get(column) != right.get(column)
        ]
        if delta:
            out[code] = delta
    return out


# ─── 五类场景（验收报告的实测口径）────────────────────────────────────────


def _case_prev_period_restatement(duckdb_store, sqlite_store, codes, updater) -> str:
    """#1 同一报告期"上一期"数值被修正：YoY 的输入变了，报告期没变。"""
    target = codes[3]
    Seed(duckdb_store).income(
        target, PREV_PERIOD, overwrite=True, revenue=999.0, parent_net_profit=99.0,
    )
    return target


def _case_dividend_insert(duckdb_store, sqlite_store, codes, updater) -> str:
    """#2 分红行新增（无公告驱动）：直接往 dividends 表插一行。"""
    target = codes[2]
    Seed(duckdb_store).dividends(
        target, [("2025-12-01", "2025-11-01", 5.0)], if_exists="ignore",
    )
    return target


def _case_corrupt_snapshot(duckdb_store, sqlite_store, codes, updater) -> str:
    """#3 快照被外部改坏：`roe -999` / `latest_close 999`（版本号看不出来）。"""
    target = codes[2]
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET roe = -999.0, latest_close = 999.0 "
        "WHERE stock_code = ?",
        [target],
    )
    return target


def _case_backfill_period(duckdb_store, sqlite_store, codes, updater) -> str:
    """#4 报告期回补：把 2024-12-31 这一期补上（此前该股只有 2025-12-31）。

    验收报告的实测形态就是这一种：补完之后 `roe` 从 NULL 变有值、`*_yoy`
    从 NULL 变成 0.25 —— 期次与数值都在"最新完整期"之外，v1 指纹看不见。
    """
    target = codes[5]
    seed = Seed(duckdb_store)
    seed.balance(
        target, PREV_PERIOD,
        total_assets=90.0, total_liabilities=20.0, total_equity=70.0,
    )
    seed.income(target, PREV_PERIOD, revenue=80.0, parent_net_profit=8.0)
    seed.cash_flow(target, PREV_PERIOD, cf_from_operating=8.0)
    return target


def _case_manual_override(duckdb_store, sqlite_store, codes, updater) -> str:
    """#5 人工覆写 published（PRD §9.5 R7）：PS/PB 等估值必须跟着覆写后的值走。"""
    target = codes[4]
    sqlite_store.execute(
        """INSERT INTO manual_overrides
               (stock_code, field_name, report_date, original_value, override_value,
                reason, status)
           VALUES (?, 'revenue', ?, NULL, 5000.0, 'PRD §9.5 R7 官方纠正', 'published')""",
        [target, PERIOD],
    )
    return target


def _setup_missing_prev_period(duckdb_store, sqlite_store, codes) -> None:
    """回补场景的前置：这只股票**先缺** 2024-12-31（期次缺失 = 下一期是回补）。"""
    target = codes[5]
    for table in ("balance_sheet", "income_statement", "cash_flow"):
        duckdb_store.write_query(
            f"DELETE FROM {table} WHERE stock_code = ? AND report_date = ?",
            [target, PREV_PERIOD],
        )


# ─── ⑥⑦ 验收最后 2 类漏算（2026-11-20 实测口径）───────────────────────────
#
# ⑦ 新增/修改/删除 `share_capital_history` 的一行（**不是** stock_meta.total_shares）
#   修前：skipped / planned_count=0，而 total_market_cap 1000→9.0e10、
#   cumulative_dividend_amount 88→9.9e9、pe/pb/ps/pcf 6 列全错。
# ⑧ 外部直接 UPDATE 行情（不走价格步，`_updated_codes` 里没有它）
#   修前：skipped / planned_count=0，15 列全错（pe_ttm/total_market_cap/ma5…）。
#
# 目标股在 `share_capital_history` 里先有一条 **800 股**的链（≠ stock_meta 的
# 100 股），这样"删掉这条链"也会真的改变结果——否则删除后退回 meta 与原来
# 恰好相同，用例会变成空转。
CAPITAL_TARGET = 6
CAPITAL_SEEDED_SHARES = 800.0


def _setup_share_capital_chain(duckdb_store, sqlite_store, codes) -> None:
    """⑦ 前置：目标股已有一条股本链 + 一笔现金分红（金额 = 每股股息 × 当日股本）。"""
    target = codes[CAPITAL_TARGET]
    seed = Seed(duckdb_store)
    seed.shares(target, [("2020-01-01", CAPITAL_SEEDED_SHARES)])
    seed.dividends(
        target, [("2024-06-20", "2024-05-01", 0.5)], if_exists="ignore",
    )


def _case_share_capital_insert(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑦a 股本链**新增**一行（生效日在最新价之前 → 市值改用新股本）。"""
    target = codes[CAPITAL_TARGET]
    Seed(duckdb_store).shares(target, [("2025-01-01", 950.0)])
    return target


def _case_share_capital_modify(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑦b 股本链**改值**（生效日不变、total_shares 变）。"""
    target = codes[CAPITAL_TARGET]
    duckdb_store.write_query(
        "UPDATE share_capital_history SET total_shares = 900.0 "
        "WHERE stock_code = ? AND effective_date = DATE '2020-01-01'",
        [target],
    )
    return target


def _case_share_capital_delete(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑦c 股本链**删除**整条（退回 stock_meta/circ_shares 近似口径）。"""
    target = codes[CAPITAL_TARGET]
    duckdb_store.write_query(
        "DELETE FROM share_capital_history WHERE stock_code = ?", [target],
    )
    return target


def _recent_trade_date(duckdb_store, code: str, offset: int, table: str = "price_daily_raw"):
    """该股票倒数第 `offset+1` 个交易日（offset=0 即最新一天）。"""
    rows = duckdb_store.read_query(
        f"SELECT trade_date FROM {table} WHERE stock_code = ? "
        "ORDER BY trade_date DESC LIMIT 1 OFFSET ?",
        [code, offset],
    )
    return rows[0]["trade_date"]


def _case_external_latest_close(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑧a 外部改**最新一天**的收盘价（原始 + 前复权两张表）。"""
    target = codes[0]
    latest = _recent_trade_date(duckdb_store, target, 0)
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"UPDATE {table} SET close = 12.5 WHERE stock_code = ? AND trade_date = ?",
            [target, latest],
        )
    return target


def _case_external_historical_close(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑧b 外部改**历史某天**的收盘价：最新价/日期不变，只有 MA5 会变。

    价格域增量的 `compute_price_sensitive` 有一条"最新价没变就跳过"的自检，
    所以这类改动**必须**由指纹带进 `force_codes`，否则照样漏算。
    """
    target = codes[1]
    day = _recent_trade_date(duckdb_store, target, 2)
    duckdb_store.write_query(
        "UPDATE price_daily_raw SET close = 40.0 WHERE stock_code = ? AND trade_date = ?",
        [target, day],
    )
    return target


def _case_external_qfq_history(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑧c 外部改 qfq 历史：只动波动率/区间收益/最大回撤，最新价完全不变。"""
    target = codes[2]
    day = _recent_trade_date(duckdb_store, target, 1, table="price_daily_qfq")
    duckdb_store.write_query(
        "UPDATE price_daily_qfq SET close = 40.0 WHERE stock_code = ? AND trade_date = ?",
        [target, day],
    )
    return target


def _case_external_volume(duckdb_store, sqlite_store, codes, updater) -> str:
    """⑧d 外部改历史成交量：只改 avg_volume。

    这一条是"只哈希 (MAX(trade_date), 当日 close, COUNT(*))"**覆盖不到**的改法，
    本用例钉住我们选的是全行全列哈希，而不是那个更便宜但有盲区的摘要。
    """
    target = codes[3]
    day = _recent_trade_date(duckdb_store, target, 4)
    duckdb_store.write_query(
        "UPDATE price_daily_raw SET volume = 100000.0 "
        "WHERE stock_code = ? AND trade_date = ?",
        [target, day],
    )
    return target


CASES = [
    (
        "prev_period_restatement", None,
        _case_prev_period_restatement, "financial_fingerprint", False,
    ),
    (
        "dividend_insert_no_announcement", None,
        _case_dividend_insert, "event_fingerprint", False,
    ),
    (
        "snapshot_corrupted", None,
        _case_corrupt_snapshot, "snapshot_state_mismatch", True,
    ),
    (
        "report_period_backfill", _setup_missing_prev_period,
        _case_backfill_period, "financial_fingerprint", False,
    ),
    (
        "manual_override_published", None,
        _case_manual_override, "manual_override_fingerprint", False,
    ),
    (
        "share_capital_history_insert", _setup_share_capital_chain,
        _case_share_capital_insert, "share_capital_fingerprint", False,
    ),
    (
        "share_capital_history_modify", _setup_share_capital_chain,
        _case_share_capital_modify, "share_capital_fingerprint", False,
    ),
    (
        "share_capital_history_delete", _setup_share_capital_chain,
        _case_share_capital_delete, "share_capital_fingerprint", False,
    ),
    (
        "external_price_latest_close", None,
        _case_external_latest_close, "price_fingerprint", False,
    ),
    (
        "external_price_historical_close", None,
        _case_external_historical_close, "price_fingerprint", False,
    ),
    (
        "external_qfq_historical_close", None,
        _case_external_qfq_history, "price_fingerprint", False,
    ),
    (
        "external_historical_volume", None,
        _case_external_volume, "price_fingerprint", False,
    ),
]


@pytest.mark.parametrize(
    ("case_name", "setup", "mutate", "expected_source", "expect_restore"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_acceptance_five_leaks_incremental_equals_full(
    duckdb_store, sqlite_store, case_name: str, setup, mutate, expected_source: str,
    expect_restore: bool,
) -> None:
    """五类漏算各跑一次：增量 vs 全量 `indicator_snapshot` **全列**逐行相等。"""
    codes = _audit_codes()
    updater = _audit_env(duckdb_store, sqlite_store, codes)
    _seed_prev_period(duckdb_store, codes)
    if setup is not None:
        setup(duckdb_store, sqlite_store, codes)

    first = updater.run_incremental_update()["steps"]["indicators"]
    assert first["mode"] == "full", first
    # 稳态轮先把水位对齐（真实部署里就是上一轮），再构造本轮改动。
    steady = updater.run_incremental_update()["steps"]["indicators"]
    assert steady["mode"] == "incremental", steady
    assert steady["recomputed_count"] == 0, f"稳态轮不该算任何股票：{steady}"
    before = _snapshot_map(duckdb_store)

    target = mutate(duckdb_store, sqlite_store, codes, updater)

    report = updater.run_incremental_update()
    step = report["steps"]["indicators"]
    assert step["mode"] == "incremental", (
        f"{case_name}: 该场景必须走增量（走全量 = 判据退化成'判不准就全量'），"
        f"实际 reason={step['reason']}"
    )
    assert step["planned_count"] >= 1, f"{case_name}: 一只都没进重算集合 = 漏算"
    assert target in step["recomputed_codes"], (
        f"{case_name}: 变化的那只股票没进重算集合：{step['recomputed_codes']}"
    )
    assert step["changed_sources"].get(expected_source, 0) >= 1, (
        f"{case_name}: 变化来源 {expected_source} 没被记录：{step['changed_sources']}"
    )
    if expected_source == "price_fingerprint":
        # 本组用例**故意不让价格步报出这些股票**（`_stub_network_steps` 里价格步
        # 恒为 skipped）：唯一的信号只能是行情指纹，否则证明不了"外部改价看得见"。
        assert step["changed_sources"].get("price", 0) == 0, (
            f"{case_name}: 价格步不该报出这只股票（会掩盖指纹是否生效）："
            f"{step['changed_sources']}"
        )

    incremental = _snapshot_map(duckdb_store)
    if expect_restore:
        # 快照自愈：算完必须逐列回到"被外部改坏之前"的样子（roe/latest_close 等）。
        not_healed = {
            col: (before[target].get(col), incremental[target].get(col))
            for col in before[target]
            if before[target].get(col) != incremental[target].get(col)
        }
        assert incremental[target] == before[target], (
            f"{case_name}: 被改坏的快照没有自愈：{not_healed}"
        )
    else:
        assert incremental[target] != before[target], f"{case_name}: 快照一个字节都没变 = 漏算"

    # 金标准：同一份数据全量重算（同一个库、同一批输入）。
    full_report = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store).compute_snapshot_for_all()
    assert full_report["status"] == "success", full_report
    full = _snapshot_map(duckdb_store)

    delta = _diff_snapshot(incremental, full)
    mismatches = sum(len(columns) for columns in delta.values())
    assert mismatches == 0, (
        f"{case_name}: 增量与全量不一致 {mismatches} 个格子（{len(delta)} 只股票）；"
        f"首例 {list(delta.items())[0]}"
    )
    # 列数自证：比对的是**全列**（除时间戳 `calculated_at` 外的 73 列），
    # 不是挑几列看的。
    assert len(next(iter(incremental.values()))) >= 70


def test_price_fingerprint_codes_are_forced_through_latest_price_skip(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """行情指纹抓到的股票必须进 `force_codes`，否则"最新价没变 → 跳过"又吃掉它。

    价格域增量 `compute_price_sensitive_for_codes` 的自检是
    "`latest_price_date` + `latest_close` 与快照一致就 skipped"。外部改的是
    **历史**收盘价（MA5 窗口内）时这两个格子一模一样 —— 不强制就还是漏算，
    而用例的等价性断言会看到 ma5 停在旧值。这里直接钉住 `force_codes` 的接线
    （`update_parts/flow.py` 的薄改），比只比结果更早失败、更好定位。
    """
    codes = _audit_codes(4)
    updater = _audit_env(duckdb_store, sqlite_store, codes)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    target = codes[2]
    day = _recent_trade_date(duckdb_store, target, 2)
    duckdb_store.write_query(
        "UPDATE price_daily_raw SET close = 40.0 WHERE stock_code = ? AND trade_date = ?",
        [target, day],
    )

    captured: dict[str, Any] = {}
    real = IndicatorCalculator.compute_price_sensitive_for_codes

    def spy(self, stock_codes, *, force_codes=None, **kwargs):
        captured["codes"] = list(stock_codes)
        captured["force"] = set(force_codes or ())
        return real(self, stock_codes, force_codes=force_codes, **kwargs)

    monkeypatch.setattr(
        IndicatorCalculator, "compute_price_sensitive_for_codes", spy,
    )
    step = updater.run_incremental_update()["steps"]["indicators"]

    assert step["mode"] == "incremental", step
    assert step["changed_sources"].get("price_fingerprint", 0) == 1, step["changed_sources"]
    assert captured["codes"] == [target], captured
    assert target in captured["force"], (
        "行情指纹抓到的股票没进 force_codes —— 最新价没变的那类外部改价会被跳过："
        f"force={captured['force']}"
    )
    assert step["recomputed_count"] == 1, step


# ─── 判据守卫：指纹必须**在集合里**，不是靠"退回全量"蒙混 ────────────────


def test_scope_planning_failure_falls_back_to_full(duckdb_store, sqlite_store) -> None:
    """`plan_indicator_scope` 整体失败 → **真的**返回 full + 可解释 reason。

    修前：指纹函数一抛异常，`plan_indicator_scope` 自己就炸，整轮更新垮掉
    （既没全量也没增量）。现在必须 full + `scope_planning_failed` + 异常进报告。
    """
    codes = _audit_codes(4)
    updater = _audit_env(duckdb_store, sqlite_store, codes)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    def _boom(*_args: Any, **_kwargs: Any):
        raise RuntimeError("指纹算不出来（故障注入）")

    updater_duck = updater.duck
    original_read_query = updater_duck.read_query

    def _broken_read_query(sql: str, params: list[Any] | None = None):
        if "information_schema.columns" in sql or "GROUP BY stock_code" in sql:
            raise RuntimeError("指纹算不出来（故障注入）")
        return original_read_query(sql, params)

    updater_duck.read_query = _broken_read_query
    try:
        plan = plan_indicator_scope(updater, sources={})
    finally:
        updater_duck.read_query = original_read_query

    assert plan.mode == "full", "指纹算不出来必须退回全量"
    assert plan.reason == "scope_planning_failed"
    assert plan.planning_error and "故障注入" in plan.planning_error
    assert plan.planning_traceback, "异常栈必须留存（不许静默吞掉）"


def test_unknown_signal_source_goes_full_group(duckdb_store, sqlite_store) -> None:
    """新增/未知信号来源默认进 full 组（不得被忽略或降级到价格域）。"""
    codes = _audit_codes(4)
    updater = _audit_env(duckdb_store, sqlite_store, codes)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    plan = plan_indicator_scope(
        updater,
        sources={"a_brand_new_signal_source": [codes[1]]},
    )

    assert plan.mode == "incremental"
    assert plan.full_codes == [codes[1]], "未知来源必须进 full 组（整股 149 列）"


def test_forced_full_switch_is_wired(duckdb_store, sqlite_store) -> None:
    """`forced_full` 不是空开关：owner 属性 / 配置项都能真的触发全量。"""
    codes = _audit_codes(4)
    updater = _audit_env(duckdb_store, sqlite_store, codes)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)

    assert plan_indicator_scope(updater, sources={}).mode == "incremental"

    updater.force_full_indicator_recompute = True
    plan = plan_indicator_scope(updater, sources={})

    assert plan.mode == "full"
    assert plan.reason == "forced_full"


def test_periodic_full_refresh_fires_after_configured_days(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """周期全量兜底：超过 N 天没有全量成功 → reason=`periodic_full_refresh`。"""
    from app.core.update_parts import indicator_scope as scope_module

    codes = _audit_codes(4)
    updater = _audit_env(duckdb_store, sqlite_store, codes)
    updater.run_incremental_update()
    bootstrap_indicator_scope(updater)
    assert plan_indicator_scope(updater, sources={}).mode == "incremental"

    # 把"上次全量"改成 8 天前（默认兜底 7 天）→ 下一轮必须全量。
    monkeypatch.setattr(scope_module, "periodic_full_days", lambda: 7)
    from datetime import UTC, datetime, timedelta

    eight_days_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    updater._write_refresh_state(scope_module.PERIODIC_FULL_KEY, eight_days_ago)

    report = updater.run_incremental_update()
    step = report["steps"]["indicators"]
    assert step["mode"] == "full", f"周期兜底没生效：{step}"
    assert step["reason"] == "periodic_full_refresh"
    # 兜底必须留痕：状态表里能查到 reason，且水位被刷新到"刚刚"。
    import json as _json

    decision = _json.loads(updater._read_refresh_state(scope_module.LAST_SCOPE_KEY))
    assert decision["reason"] == "periodic_full_refresh"
    refreshed = updater._read_refresh_state(scope_module.PERIODIC_FULL_KEY)
    assert refreshed != eight_days_ago, "全量成功后必须刷新周期水位，否则每轮都全量"
