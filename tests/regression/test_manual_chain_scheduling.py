"""手工链自动接入回归（reports/119 D28/D29，2026-11-14）。

背景：7 条链路"功能完整、跑通过、有数据"，但唯一触发方式是人工敲命令，
于是停在最后一次有人跑的那天（`indicator_ext` 停在 2026-06-30 → 界面 55 个
扩展筛选字段全部失效）。本文件钉死接入后的四条契约：

1. **到期才跑**：水位未到期时步骤 skipped 且**不产生任何调用**；
2. **成功写水位**：`data_refresh_state.<domain>_last_refresh` 必须落库，
   否则下一轮又会重跑同一批（旧手工链的"停在最后一次"就是这么来的）；
3. **失败隔离**：某个新步骤抛异常时该步骤 failed，但整轮其他步骤照常执行，
   整轮状态最差是 partial（绝不 failed，readiness 也不受影响）；
4. **门控正确**：`indicator_ext` 在"ext 报告期 < 三表完整期"时必须重建；
5. **续传判据**：有 2026-06-30 旧行、缺 2026-09-30 的股票仍必须进 pending
   —— 旧脚本"本表有任意本源行就跳过"的 bug 直接用这条钉死。

全部使用临时 DuckDB/SQLite 夹具（`tests/conftest.py`），网络 updater 一律
monkeypatch，绝不触碰正式库。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.etf_prices import EtfPriceUpdater
from app.core.etf_strategy import upsert_etf_meta
from app.core.hk_dividends import HKDividendUpdater
from app.core.indicators.extended import (
    FINGERPRINT_STATE_KEY,
    ExtendedIndicatorBuilder,
)
from app.core.supplement import (
    CASHFLOW_SUPPLEMENT,
    RegulatoryMetricsUpdater,
    SupplementFetcher,
)
from app.core.update import IncrementalUpdater

TRIO_COLUMNS = ("stock_code", "report_date")
LATEST_PERIOD = "2026-09-30"
OLD_PERIOD = "2026-06-30"


# ─── 夹具/种子 ────────────────────────────────────────────────────────


def _seed_stock(store, code: str, *, name: str = "Test", exchange: str = "SZSE") -> None:
    store.write_query(
        """INSERT INTO stock_meta
           (stock_code, name, exchange, listing_date, is_listed, is_st, is_suspended,
            total_shares, circ_shares)
           VALUES (?, ?, ?, '2015-01-05', TRUE, FALSE, FALSE, 100, 80)""",
        [code, name, exchange],
    )


def _seed_trio(store, code: str, report_date: str) -> None:
    """写入三表各一行（income×balance 是 indicator_ext 的骨架）。"""
    store.write_query(
        """INSERT INTO income_statement
               (stock_code, report_date, revenue, parent_net_profit, net_profit)
           VALUES (?, CAST(? AS DATE), 1000, 100, 100)""",
        [code, report_date],
    )
    store.write_query(
        """INSERT INTO balance_sheet
               (stock_code, report_date, total_assets, total_liabilities, total_equity)
           VALUES (?, CAST(? AS DATE), 2000, 800, 1200)""",
        [code, report_date],
    )
    store.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES (?, CAST(? AS DATE), 200)""",
        [code, report_date],
    )


def _seed_cashflow_activity(
    store, code: str, report_date: str, *, source: str = "eastmoney_f10",
) -> None:
    store.write_query(
        """INSERT INTO cash_flow_activity
               (stock_code, report_date, capex, source, fetch_time,
                raw_response_hash, confidence, batch_id)
           VALUES (?, CAST(? AS DATE), 50, ?, TIMESTAMP '2026-09-20 00:00:00',
                   'hash', 'strict', 'batch')""",
        [code, report_date, source],
    )


def _seed_indicator_ext(store, code: str, report_date: str) -> None:
    store.write_query(
        """INSERT INTO indicator_ext
               (stock_code, report_date, calculated_at, source, data_version)
           VALUES (?, CAST(? AS DATE), TIMESTAMP '2026-09-20 00:00:00',
                   'derived_calculator', 1)""",
        [code, report_date],
    )


def _check_report() -> dict:
    return {
        "new_trading_days": [],
        "retry_tasks": [],
        "latest_local_price_date": "2026-11-13",
        "announcement_check": {"status": "available"},
        "needs_update": True,
        "blocked": False,
        "missing_count": 0,
    }


def _stub_flow(updater: IncrementalUpdater) -> None:
    """把主流程里所有网络/重活步骤替换成离线桩（只留手工链新步骤可被观察）。"""
    updater.run_incremental_check = lambda **kwargs: _check_report()
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {
        "status": "skipped", "success": 0,
    }
    updater._share_capital_fingerprint = lambda: "same"
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}
    updater._check_new_announcements = lambda persist=False, **kwargs: {
        "status": "available", "checked_remote": True,
        "affected_stock_codes": [], "affected_announcements": {},
        "all_new_announcements": {}, "errors": [], "warnings": [],
    }
    updater._pending_announcement_items = lambda: []
    updater._refresh_financial_detail_backfill = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub", "succeeded_codes": [],
    }
    updater._refresh_buyback = lambda: {
        "status": "skipped", "reason": "test_stub", "changed_codes": [],
    }
    updater._refresh_funding = lambda: {
        "status": "skipped", "reason": "test_stub", "changed_codes": [],
    }
    updater._resolve_legal_empty_funding_missing = lambda: 0
    updater._refresh_treasury_curve = lambda: {"status": "skipped", "reason": "test_stub"}
    updater._cleanup_unretryable_tasks = lambda: 0
    updater._cleanup_completed_announcement_retries = lambda: 0
    updater._cleanup_redundant_index_valuation_retries = lambda: 0
    updater._resolve_complete_missing_records = lambda: 0
    updater._refresh_business_overview = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub",
    }
    updater._refresh_capital_history = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub",
    }
    updater._refresh_research_statistics = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub",
    }
    updater._refresh_index_valuation = lambda: {"status": "skipped", "reason": "test_stub"}


def _supplement_success(**overrides) -> dict:
    report = {
        "status": "success", "reason": None, "targeted": 3, "updated": 2,
        "skipped": 1, "failed": 0, "deferred": 0,
        "rows_written": {"cash_flow_indirect": 4}, "periods": [LATEST_PERIOD],
        "failed_codes": [], "elapsed_seconds": 0.1,
    }
    report.update(overrides)
    return report


def _refreshed_today(sqlite, key: str) -> None:
    now = datetime.now(UTC).isoformat()
    sqlite.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        [key, now, now],
    )


# ─── 1. 水位与到期 ────────────────────────────────────────────────────


def test_supplement_step_runs_when_due_and_writes_watermark(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """到期 → 调用补采；成功后必须写水位（否则下一轮重复抓同一批）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    calls: list[tuple[str, dict]] = []

    def fake_refresh(self, key, **kwargs):  # noqa: ANN001 - 测试桩
        calls.append((key, kwargs))
        return _supplement_success()

    monkeypatch.setattr(SupplementFetcher, "refresh", fake_refresh)

    step = updater._refresh_cashflow_supplement()

    assert step["status"] == "success"
    assert step["updated"] == 2
    assert step["failed"] == 0
    assert step["elapsed_seconds"] >= 0
    assert calls and calls[0][0] == CASHFLOW_SUPPLEMENT
    # 每轮上限/并发/截止时间必须由配置注入，不能是"无界全量"
    assert calls[0][1]["max_stocks"] == updater.cashflow_supplement_max_stocks_per_run
    assert calls[0][1]["deadline_seconds"] == updater.cashflow_supplement_deadline_seconds
    keys = {
        row["key"] for row in sqlite_store.query("SELECT key FROM data_refresh_state")
    }
    # 两套读取方（到期判断 / asset_health MANUAL_CHAIN_STALE）必须同时可见
    assert "cashflow_supplement_last_refresh" in keys, "成功必须写 data_refresh_state 水位"
    assert "cashflow_supplement_last_success_at" in keys, "D29 健康探针读的是 *_last_success_at"
    assert updater._refresh_due("cashflow_supplement_last_refresh", 7) is False


def test_supplement_step_skips_within_interval_without_calling(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """未到期 → skipped，且绝不允许触碰网络层。"""
    _refreshed_today(sqlite_store, "cashflow_supplement_last_refresh")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    def forbidden(self, key, **kwargs):  # noqa: ANN001
        raise AssertionError("未到期时不得调用补采")

    monkeypatch.setattr(SupplementFetcher, "refresh", forbidden)

    step = updater._refresh_cashflow_supplement()

    assert step["status"] == "skipped"
    assert step["reason"] == "refreshed_within_interval"
    assert step["updated"] == 0


def test_etf_and_hk_steps_respect_watermark(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """ETF（1 天）/港股分红（30 天）同样是"未到期不调用"。"""
    _refreshed_today(sqlite_store, "etf_prices_last_refresh")
    _refreshed_today(sqlite_store, "hk_dividends_last_refresh")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("未到期时不得刷新 ETF/港股分红")

    monkeypatch.setattr(EtfPriceUpdater, "update_all", forbidden)
    monkeypatch.setattr(HKDividendUpdater, "update_many", forbidden)

    etf_step = updater._refresh_etf_prices()
    hk_step = updater._refresh_hk_dividends()

    assert etf_step["status"] == "skipped"
    assert etf_step["reason"] == "refreshed_within_interval"
    assert hk_step["status"] == "skipped"
    assert hk_step["reason"] == "refreshed_within_interval"


# ─── 2. 每轮上限 / 代码门控 ───────────────────────────────────────────


def test_etf_step_only_targets_etf_meta_codes(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """ETF 只刷 `etf_meta` 里 enabled 的代码，不做全市场扫描。"""
    upsert_etf_meta(sqlite_store, etf_code="510300", name="沪深300ETF",
                    track_index_code="000300", primary_metric="pe")
    upsert_etf_meta(sqlite_store, etf_code="513130", name="恒生科技",
                    track_index_code=None, primary_metric="pe")
    sqlite_store.execute("UPDATE etf_meta SET enabled = 0 WHERE etf_code = '513130'")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    called: list[str] = []

    def fake_update_etf(self, etf_code, years=5):  # noqa: ANN001
        called.append(etf_code)
        return {"status": "success", "rows": 3}

    monkeypatch.setattr(EtfPriceUpdater, "update_etf", fake_update_etf)

    step = updater._refresh_etf_prices()

    assert called == ["510300"], "只有 etf_meta 中 enabled 的代码可被刷新"
    assert step["status"] == "success"
    assert step["updated"] == 1
    assert updater._refresh_due("etf_prices_last_refresh", 1) is False


def test_hk_dividends_step_only_targets_mapped_codes(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """港股分红只刷 A+H 映射表里的股票，并按每轮上限截断。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater.hk_dividends_max_stocks_per_run = 2
    seen: list[list[str]] = []

    monkeypatch.setattr(
        HKDividendUpdater, "resolve_mapping",
        lambda self, **kwargs: {"mapping": {
            "000001": {"hk_code": "00939"},
            "000002": {"hk_code": "01211"},
            "000003": {"hk_code": "02318"},
        }},
    )

    def fake_update_many(self, codes, **kwargs):  # noqa: ANN001
        seen.append(list(codes))
        return {"status": "success", "succeeded": len(codes), "failed": 0, "unmapped": 0}

    monkeypatch.setattr(HKDividendUpdater, "update_many", fake_update_many)

    step = updater._refresh_hk_dividends()

    assert step["status"] == "success"
    assert step["targeted"] == 2, "每轮上限必须生效"
    assert sorted(code for chunk in seen for code in chunk) == ["000001", "000002"]
    assert step["updated"] == 2
    assert updater._refresh_due("hk_dividends_last_refresh", 30) is False


# ─── 3. 失败隔离（整轮不失败、其他步骤照常执行）────────────────────────


def test_new_step_failure_marks_only_itself_and_round_continues(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """补采抛异常 → 只让补采步骤 failed；ETF/港股/ext 等步骤仍必须执行。"""
    _seed_stock(duckdb_store, "000001")
    _seed_trio(duckdb_store, "000001", LATEST_PERIOD)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_flow(updater)

    def boom(self, key, **kwargs):  # noqa: ANN001
        raise RuntimeError("source down")

    monkeypatch.setattr(SupplementFetcher, "refresh", boom)
    monkeypatch.setattr(
        RegulatoryMetricsUpdater, "refresh",
        lambda self, **kwargs: {"status": "success", "targeted": 1, "updated": 1,
                                "failed": 0, "skipped": 0, "deferred": 0,
                                "rows_updated": 1, "failed_codes": []},
    )
    monkeypatch.setattr(
        ExtendedIndicatorBuilder, "build",
        lambda self, codes=None: {"status": "success", "rows_before": 1,
                                  "rows_after": 1, "rows_written": 0,
                                  "filled": {}, "report_date_range": []},
    )
    monkeypatch.setattr(
        EtfPriceUpdater, "update_all",
        lambda self, **kwargs: {"status": "success", "targeted": 0, "deferred": 0,
                                "summary": {"success": 0, "missing": 0, "failed": 0}},
    )
    monkeypatch.setattr(
        HKDividendUpdater, "resolve_mapping",
        lambda self, **kwargs: {"mapping": {"000001": {"hk_code": "00939"}}},
    )
    monkeypatch.setattr(
        HKDividendUpdater, "update_many",
        lambda self, codes, **kwargs: {"status": "success", "succeeded": len(codes),
                                       "failed": 0, "unmapped": 0},
    )

    report = updater.run_incremental_update()
    steps = report["steps"]

    assert steps["cashflow_supplement"]["status"] == "failed"
    assert "source down" in steps["cashflow_supplement"]["reason"]
    # 失败步骤绝不能吞掉后续步骤
    for name in ("deducted_profit", "regulatory_metrics", "indicator_ext",
                 "etf_prices", "hk_dividends"):
        assert name in steps, f"{name} 步骤必须在失败之后仍然执行"
    assert steps["etf_prices"]["status"] == "success"
    assert steps["hk_dividends"]["status"] == "success"
    assert steps["regulatory_metrics"]["status"] == "success"
    assert report["status"] == "partial", "单个步骤失败只能是 partial，不能整轮 failed"


def test_disabled_switch_skips_without_calling(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """`update.*_auto_enabled=false` 时不产生任何调用（回滚开关可用）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater.cashflow_supplement_auto_enabled = False

    def forbidden(self, key, **kwargs):  # noqa: ANN001
        raise AssertionError("开关关闭时不得调用补采")

    monkeypatch.setattr(SupplementFetcher, "refresh", forbidden)

    step = updater._refresh_cashflow_supplement()

    assert step["status"] == "skipped"
    assert step["reason"] == "disabled_by_config"


# ─── 4. indicator_ext 门控（D28）──────────────────────────────────────


def test_indicator_ext_rebuilds_when_ext_period_behind_trio(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """ext 停在 2026-06-30、三表已完整到 2026-09-30 → 必须重建（P0 场景）。"""
    for code in ("000001", "000002", "000003"):
        _seed_stock(duckdb_store, code)
        _seed_trio(duckdb_store, code, OLD_PERIOD)
        _seed_trio(duckdb_store, code, LATEST_PERIOD)
        _seed_indicator_ext(duckdb_store, code, OLD_PERIOD)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    builds: list[int] = []

    def fake_build(self, codes=None):  # noqa: ANN001
        builds.append(1)
        return {"status": "success", "rows_before": 3, "rows_after": 3,
                "rows_written": 0, "filled": {}, "report_date_range": []}

    monkeypatch.setattr(ExtendedIndicatorBuilder, "build", fake_build)

    step = updater._refresh_indicator_ext()

    assert builds, "报告期落后时必须触发重建"
    assert step["status"] == "success"
    assert step["reason"] == "ext_period_behind_trio"
    assert step["ext_max_period"] == OLD_PERIOD
    assert step["complete_trio_period"] == LATEST_PERIOD
    assert sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = 'indicator_ext_last_refresh'"
    )
    assert sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = ?", [FINGERPRINT_STATE_KEY]
    )


def test_indicator_ext_skips_when_up_to_date(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """ext 已跟上三表完整期 + 指纹未变 + 水位未到期 → skipped，不重建。"""
    for code in ("000001", "000002", "000003"):
        _seed_stock(duckdb_store, code)
        _seed_trio(duckdb_store, code, LATEST_PERIOD)
        _seed_indicator_ext(duckdb_store, code, LATEST_PERIOD)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    from app.core.indicators.extended import input_state

    fingerprint = input_state(duckdb_store)["fingerprint"]
    now = datetime.now(UTC).isoformat()
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)""",
        [FINGERPRINT_STATE_KEY, fingerprint, now],
    )
    _refreshed_today(sqlite_store, "indicator_ext_last_refresh")

    def forbidden(self, codes=None):  # noqa: ANN001
        raise AssertionError("输入未变且未到期时不得重建 indicator_ext")

    monkeypatch.setattr(ExtendedIndicatorBuilder, "build", forbidden)

    step = updater._refresh_indicator_ext()

    assert step["status"] == "skipped"
    assert step["reason"] == "up_to_date"


# ─── 5. 续传判据（"跑了也白跑"的根因）────────────────────────────────


def test_pending_ignores_stock_that_has_old_source_rows(duckdb_store) -> None:
    """有 2026-06-30 旧行、缺 2026-09-30 的股票**仍然**必须进 pending。

    旧脚本判据是"本表有任意 eastmoney_f10 行就跳过"，于是第一次跑完后再也
    补不到新报告期（2026-11-14 生产实测：40 只待补，5540 只永久跳过）。
    """
    _seed_stock(duckdb_store, "000001")   # 只有 06-30 旧行 → 必须进 pending
    _seed_stock(duckdb_store, "000002")   # 已有 09-30 → 不该进 pending
    for code in ("000001", "000002"):
        _seed_trio(duckdb_store, code, LATEST_PERIOD)
    _seed_cashflow_activity(duckdb_store, "000001", OLD_PERIOD)
    _seed_cashflow_activity(duckdb_store, "000002", LATEST_PERIOD)

    fetcher = SupplementFetcher(duck=duckdb_store)
    pending = fetcher.pending_codes(
        CASHFLOW_SUPPLEMENT, [LATEST_PERIOD, OLD_PERIOD],
    )

    codes = [code for code, _ in pending]
    assert "000001" in codes, "旧判据会把它永久跳过 —— 这正是 D29 ② 的根因"
    assert "000002" not in codes


def test_pending_flags_balance_sheet_ext_with_same_criteria(duckdb_store) -> None:
    """`balance_sheet_ext` 走同一套判据（同一 bug、同一修法）。"""
    from app.core.supplement import BALANCE_SHEET_EXT

    _seed_stock(duckdb_store, "000001")
    _seed_trio(duckdb_store, "000001", LATEST_PERIOD)
    duckdb_store.write_query(
        """INSERT INTO balance_sheet_ext
               (stock_code, report_date, other_payables, source, fetch_time,
                raw_response_hash, confidence, batch_id)
           VALUES ('000001', CAST(? AS DATE), 10, 'eastmoney_f10',
                   TIMESTAMP '2026-09-20 00:00:00', 'hash', 'strict', 'batch')""",
        [OLD_PERIOD],
    )

    fetcher = SupplementFetcher(duck=duckdb_store)
    pending = fetcher.pending_codes(BALANCE_SHEET_EXT, [LATEST_PERIOD, OLD_PERIOD])

    assert [code for code, _ in pending] == ["000001"]


def test_deducted_profit_pending_uses_null_gap_in_recent_window(duckdb_store) -> None:
    """扣非补采按"目标窗口内该列仍为 NULL"判据（不是"有行就跳过"）。"""
    from app.core.supplement import DEDUCTED_PROFIT

    _seed_stock(duckdb_store, "000001")
    _seed_stock(duckdb_store, "000002")
    for code in ("000001", "000002"):
        _seed_trio(duckdb_store, code, LATEST_PERIOD)
    duckdb_store.write_query(
        """UPDATE income_statement SET deducted_net_profit = 90
           WHERE stock_code = '000002'"""
    )

    fetcher = SupplementFetcher(duck=duckdb_store)
    periods = fetcher.resolve_periods(DEDUCTED_PROFIT, periods=[LATEST_PERIOD, OLD_PERIOD])
    pending = fetcher.pending_codes(DEDUCTED_PROFIT, periods)

    assert [code for code, _ in pending] == ["000001"]


def test_supplement_refresh_writes_source_rows_and_respects_deadline(
    duckdb_store, monkeypatch,
) -> None:
    """端到端：抓取→解析→落库，且只写 `source='eastmoney_f10'` 的新报告期行。"""
    _seed_stock(duckdb_store, "000001")
    _seed_trio(duckdb_store, "000001", LATEST_PERIOD)
    _seed_cashflow_activity(duckdb_store, "000001", OLD_PERIOD)
    fetcher = SupplementFetcher(duck=duckdb_store)

    def fake_fetch(self, spec, code, exchange, dates, **kwargs):  # noqa: ANN001
        return [{
            "REPORT_DATE": f"{LATEST_PERIOD} 00:00:00",
            "FA_IR_DEPR": 12.0,
            "IA_AMORTIZE": 3.0,
            "CONSTRUCT_LONG_ASSET": 40.0,
        }]

    monkeypatch.setattr(SupplementFetcher, "_fetch_rows", fake_fetch)

    report = fetcher.refresh(
        CASHFLOW_SUPPLEMENT, periods=[LATEST_PERIOD, OLD_PERIOD], deadline_seconds=30,
    )

    assert report["status"] == "success"
    assert report["updated"] == 1
    rows = duckdb_store.read_query(
        """SELECT report_date, fixed_asset_depreciation, intangible_asset_amortization
           FROM cash_flow_indirect WHERE stock_code = '000001' AND source = 'eastmoney_f10'"""
    )
    assert len(rows) == 1, "按股票先删同源再插，不能留下 06-30 的旧同源行"
    assert str(rows[0]["report_date"])[:10] == LATEST_PERIOD
    assert rows[0]["fixed_asset_depreciation"] == pytest.approx(12.0)
    capex = duckdb_store.read_query(
        """SELECT capex FROM cash_flow_activity
           WHERE stock_code = '000001' AND source = 'eastmoney_f10'"""
    )
    assert [row["capex"] for row in capex] == [pytest.approx(40.0)]


def test_deducted_profit_never_inserts_or_overwrites_existing_values(
    duckdb_store, monkeypatch,
) -> None:
    """扣非补采**只填 NULL**：既不 INSERT 新行，也不覆盖既有 CSMAR/主链值。

    这条契约是安全底线：源字段名若将来变了，结果只会是"没更新"，
    不会把主链三表写坏（旧脚本是 DELETE+CSV 回灌，风险高得多）。
    """
    from app.core.supplement import DEDUCTED_PROFIT

    _seed_stock(duckdb_store, "000001")
    _seed_trio(duckdb_store, "000001", LATEST_PERIOD)
    _seed_trio(duckdb_store, "000001", OLD_PERIOD)
    # 旧期已有 CSMAR 值 → 绝不能被覆盖；新期 NULL → 应被填上
    duckdb_store.write_query(
        """UPDATE income_statement SET deducted_net_profit = 77
           WHERE stock_code = '000001' AND report_date = CAST(? AS DATE)""",
        [OLD_PERIOD],
    )
    fetcher = SupplementFetcher(duck=duckdb_store)

    def fake_fetch(self, spec, code, exchange, dates, **kwargs):  # noqa: ANN001
        return [
            {"REPORT_DATE": f"{LATEST_PERIOD} 00:00:00", "DEDUCT_PARENT_NETPROFIT": 55.0},
            {"REPORT_DATE": f"{OLD_PERIOD} 00:00:00", "DEDUCT_PARENT_NETPROFIT": 11.0},
        ]

    monkeypatch.setattr(SupplementFetcher, "_fetch_rows", fake_fetch)

    report = fetcher.refresh(
        DEDUCTED_PROFIT, periods=[LATEST_PERIOD, OLD_PERIOD], deadline_seconds=30,
    )

    assert report["status"] == "success"
    values = {
        str(row["report_date"])[:10]: row["deducted_net_profit"]
        for row in duckdb_store.read_query(
            """SELECT report_date, deducted_net_profit FROM income_statement
               WHERE stock_code = '000001'"""
        )
    }
    assert values[LATEST_PERIOD] == pytest.approx(55.0)
    assert values[OLD_PERIOD] == pytest.approx(77.0), "既有值不得被覆盖"
    assert len(values) == 2, "绝不 INSERT 新行"


def test_supplement_keeps_draining_while_backlog_remains(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """一次季报的全市场缺口 ±5,575 只，单轮跑不完时**不得**推进水位。

    否则"到期判断"会白挡 7 天，续传卡死在第一轮 —— 这正是手工链
    "停在最后一次有人跑的那天"的另一种复现方式。
    """
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    remaining = {"value": 500}

    def fake_refresh(self, key, **kwargs):  # noqa: ANN001
        return _supplement_success(
            targeted=1000, updated=1000, skipped=0, failed=0,
            remaining_pending=remaining["value"],
        )

    monkeypatch.setattr(SupplementFetcher, "refresh", fake_refresh)

    first = updater._refresh_cashflow_supplement()
    assert first["remaining_pending"] == 500
    assert not sqlite_store.query(
        "SELECT value FROM data_refresh_state "
        "WHERE key = 'cashflow_supplement_last_refresh'"
    ), "还有残留待补时不得推进水位，否则下一轮会被间隔白等 7 天"
    assert updater._refresh_due("cashflow_supplement_last_refresh", 7) is True

    remaining["value"] = 0
    second = updater._refresh_cashflow_supplement()
    assert second["remaining_pending"] == 0
    assert sqlite_store.query(
        "SELECT value FROM data_refresh_state "
        "WHERE key = 'cashflow_supplement_last_refresh'"
    ), "追平后才推进水位"
    assert updater._refresh_due("cashflow_supplement_last_refresh", 7) is False
