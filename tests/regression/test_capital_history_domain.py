"""P4 历史总股本链与历史统计回归（reports/68）

覆盖：
- CapitalHistoryUpdater：CNINFO 主链回填、东财近邻交叉核验、冲突 fail-closed、
  保旧值/retry/missing、覆盖报告
- StatisticsBuilder：PE/PB/TTM股息率/利差序列、窗口统计（分位带/μσ/zscore）、
  最小样本门槛、覆盖门槛、原子发布与版本
- 筛选引擎：统计字段 join 已发布统计域（5 秒路径之外）、白名单、READINESS 不变
- API：/research-statistics 序列与聚合
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.capital import CapitalHistoryUpdater
from app.core.data_quality import minimum_data_readiness
from app.core.screening.engine import (
    STAT_FIELDS,
    ScreeningEngine,
)
from app.core.statistics import (
    COVERAGE_THRESHOLD_PCT,
    StatisticsBuilder,
    WINDOW_MIN_SAMPLES,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.web.api.stock_detail import router as stock_detail_router

# ─── fixtures ────────────────────────────────────────────────────────────


def _result(data: list[dict], *, error: str | None = None) -> FetchResult:
    raw = __import__("json").dumps(data, ensure_ascii=False).encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source="cninfo_capital",
            fetch_time=datetime.now(timezone.utc),
            raw_response_hash=hashlib.sha256(raw).hexdigest(),
            confidence="strict" if error is None else "missing",
            error=error,
        ),
        raw_response=raw,
    )


class _FakeCapitalAdapter:
    def __init__(self, *, main=None, cross=None, error: str | None = None) -> None:
        self.main = main
        self.cross = cross
        self.error = error
        self.calls: list[dict] = []

    def fetch(self, request: FetchRequest) -> FetchResult:
        params = dict(request.extra_params)
        self.calls.append(params)
        if self.error:
            return _result([], error=self.error)
        if params.get("cross_source") == "eastmoney":
            return _result(self.cross or [])
        return _result(self.main or [])


def _seed_stock(duck: DuckDBStore, code: str = "600519") -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES (?, ?, 'SSE', true)",
        [code, code],
    )


def _seed_price(duck: DuckDBStore, code: str, date_: date, close: float) -> None:
    duck.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?)",
        [code, date_, close],
    )


def _seed_financials(duck: DuckDBStore, code: str, report_date: date, profit: float, equity: float) -> None:
    duck.write_query(
        """INSERT INTO income_statement (stock_code, report_date, parent_net_profit, revenue)
           VALUES (?, ?, ?, 1000.0)""",
        [code, report_date, profit],
    )
    duck.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_equity_parent)
           VALUES (?, ?, ?)""",
        [code, report_date, equity],
    )


def _seed_capital(duck: DuckDBStore, code: str, rows: list[tuple[date, float]]) -> None:
    for effective_date, total in rows:
        duck.write_query(
            """INSERT INTO share_capital_history
               (stock_code, effective_date, total_shares, change_reason, is_anchor,
                verified, source, raw_hash, batch_id)
               VALUES (?, ?, ?, NULL, true, true, 'cninfo_capital', 'x', 'b1')""",
            [code, effective_date, total],
        )


def _updater(duck: DuckDBStore, sqlite: SQLiteStore, adapter) -> CapitalHistoryUpdater:
    return CapitalHistoryUpdater(duck=duck, sqlite=sqlite, adapter=adapter)


# ─── CapitalHistoryUpdater ───────────────────────────────────────────────


def test_main_chain_backfill_units_and_anchors(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    main = [
        {"effective_date": "2020-12-31", "total_shares": 943800000.0, "change_reason": None, "is_anchor": True},
        {"effective_date": "2021-06-30", "total_shares": 943800000.0, "change_reason": None, "is_anchor": True},
    ]
    updater = _updater(duckdb_store, sqlite_store, _FakeCapitalAdapter(main=main, cross=[]))

    report = updater.update_stock("600519")

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT effective_date, total_shares, is_anchor, verified FROM share_capital_history ORDER BY effective_date"
    )
    assert len(rows) == 2
    assert rows[0]["total_shares"] == pytest.approx(943800000.0)
    assert rows[0]["is_anchor"] is True
    # 东财无事件 → 主链保留但 verified=False（来源本身成立，无交叉证据）
    assert rows[0]["verified"] is False


def test_cross_check_conflict_fails_closed(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    # 主链：2020-12-31 与 2021-06-30 均为 9.438 亿股（无变动）
    main = [
        {"effective_date": "2020-12-31", "total_shares": 943800000.0, "change_reason": None, "is_anchor": True},
        {"effective_date": "2021-06-30", "total_shares": 943800000.0, "change_reason": None, "is_anchor": True},
    ]
    # 东财在区间内报告 2021-03-15 增发至 12 亿股 → 与主链冲突
    cross = [
        {"effective_date": "2021-03-15", "total_shares": 1200000000.0, "change_reason": "增发送股", "is_anchor": False},
    ]
    updater = _updater(duckdb_store, sqlite_store, _FakeCapitalAdapter(main=main, cross=cross))

    report = updater.update_stock("600519")

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT verified FROM share_capital_history ORDER BY effective_date"
    )
    assert [row["verified"] for row in rows] == [False, False], "冲突区间必须 fail-closed"


def test_cross_check_agreement_verifies_chain(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    main = [
        {"effective_date": "2021-06-30", "total_shares": 1250000000.0, "change_reason": None, "is_anchor": True},
        {"effective_date": "2022-12-31", "total_shares": 1256000000.0, "change_reason": "增发", "is_anchor": False},
    ]
    cross = [
        {"effective_date": "2022-12-31", "total_shares": 1256000000.0, "change_reason": "增发", "is_anchor": False},
    ]
    updater = _updater(duckdb_store, sqlite_store, _FakeCapitalAdapter(main=main, cross=cross))

    report = updater.update_stock("600519")

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT verified FROM share_capital_history ORDER BY effective_date"
    )
    assert rows[0]["verified"] is True
    assert rows[1]["verified"] is True


def test_cross_cache_reused_and_skips_eastmoney(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """reports/74 修复：交叉核验结果落盘后，重跑不再请求东财（绝不重跑已核验）。"""
    _seed_stock(duckdb_store)
    main = [
        {"effective_date": "2021-06-30", "total_shares": 1250000000.0, "change_reason": None, "is_anchor": True},
        {"effective_date": "2022-12-31", "total_shares": 1256000000.0, "change_reason": "增发", "is_anchor": False},
    ]
    cross = [
        {"effective_date": "2022-12-31", "total_shares": 1256000000.0, "change_reason": "增发", "is_anchor": False},
    ]
    fake = _FakeCapitalAdapter(main=main, cross=cross)
    updater = _updater(duckdb_store, sqlite_store, fake)

    report = updater.update_stock("600519")
    assert report["status"] == "success"
    assert report.get("cross_status") == "verified"
    assert len(fake.calls) == 2  # 主链 + 东财各一次

    # 缓存已写入；再次 update_stock 直接复用缓存，不再请求东财
    # （主链仍刷新一次，这是预期；东财交叉请求必须为零新增）
    cross_calls = [c for c in fake.calls if c.get("cross_source") == "eastmoney"]
    assert len(cross_calls) == 1, "已缓存股票不得再请求东财"
    report2 = updater.update_stock("600519")
    assert report2["status"] == "success"
    assert report2.get("cross_status") == "cached"
    cross_calls = [c for c in fake.calls if c.get("cross_source") == "eastmoney"]
    assert len(cross_calls) == 1, "已缓存股票不得再请求东财"

    # due 游标：已缓存股票不再入选
    fake.calls.clear()
    report_all = updater.update_all(max_stocks=20)
    assert report_all.get("status") in ("skipped", "success")
    assert fake.calls == [], "已缓存股票不得进入 due 重跑"

    # 缓存过期后重新请求（TLT 7 天）
    sqlite_store.execute(
        "UPDATE capital_cross_cache SET fetched_at = ? WHERE stock_code = '600519'",
        [(datetime.now(timezone.utc) - timedelta(days=8)).isoformat()],
    )
    report3 = updater.update_stock("600519")
    assert report3.get("cross_status") == "verified"


def test_main_chain_failure_preserves_old_values_and_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_capital(duckdb_store, "600519", [(date(2020, 12, 31), 943800000.0)])
    updater = _updater(duckdb_store, sqlite_store, _FakeCapitalAdapter(error="source down"))

    report = updater.update_stock("600519")

    assert report["status"] == "failed"
    rows = duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM share_capital_history WHERE stock_code = '600519'"
    )
    assert rows[0]["c"] == 1, "失败必须保留旧值"
    retry = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM retry_list WHERE data_type = 'share_capital_history'"
    )
    assert retry[0]["c"] == 1


def test_empty_main_chain_records_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    updater = _updater(duckdb_store, sqlite_store, _FakeCapitalAdapter(main=[]))

    report = updater.update_stock("600519")

    assert report["status"] == "failed"
    assert report["reason"] == "source_empty"
    missing = sqlite_store.query(
        "SELECT reason_code FROM missing_list WHERE field_name = 'share_capital_history' AND resolved_at IS NULL"
    )
    assert missing == [{"reason_code": "source_empty"}]


# ─── StatisticsBuilder ───────────────────────────────────────────────────


def _seed_statistics_inputs(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    _seed_stock(duck)
    # 1250 个价格日（覆盖 10 年窗口最小 1200 样本；数值变化保证 σ>0）
    # 批量写入避免逐行 open-per-query 拖慢整个回归
    price_rows = [
        ("600519", date(2022, 8, 1) + timedelta(days=i), 10.0 + (i % 100))
        for i in range(1250)
    ]
    with duck.write_connection() as conn:
        conn.executemany(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?)",
            price_rows,
        )
    _seed_price(duck, "600519", date(2026, 8, 7), 25.0)
    _seed_financials(duck, "600519", date(2021, 12, 31), 80e8, 450e8)
    _seed_financials(duck, "600519", date(2022, 12, 31), 90e8, 470e8)
    _seed_financials(duck, "600519", date(2023, 12, 31), 100e8, 500e8)
    _seed_financials(duck, "600519", date(2024, 12, 31), 120e8, 520e8)
    _seed_financials(duck, "600519", date(2025, 12, 31), 140e8, 540e8)
    _seed_capital(duck, "600519", [(date(2016, 6, 30), 1000000000.0)])
    duck.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', '2026-06-30', '2026-05-01', 1.0)""",
    )
    duck.write_query(
        """INSERT INTO treasury_yield_curve
           (curve_date, tenor_years, yield_pct, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('2026-08-05', 10.0, 1.5, 'czb_mof', CURRENT_TIMESTAMP, 'x', 'strict', 'b1')""",
    )


def test_series_pe_pb_dividend_spread() -> None:
    from tests.conftest import database_paths as _  # noqa: F401


def test_statistics_build_and_window_stats(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)

    series = builder.build_series("600519")

    latest = series[-1]
    assert latest["pe_ttm"] is not None
    # 25 元 × 10 亿股 / 140 亿元净利 ≈ 1.79
    assert latest["pe_ttm"] == pytest.approx(25 * 1e9 / 140e8, rel=0.02)
    assert latest["pb_mrq"] == pytest.approx(25 * 1e9 / 540e8, rel=0.02)
    assert latest["ttm_dividend_yield"] == pytest.approx(4.0)  # 1.0/25*100
    # 10Y 曲线 1.5% → 利差 2.5
    assert latest["spread_10y"] == pytest.approx(2.5, rel=0.02)

    stats = builder.window_stats(series, "pe_ttm", 1, WINDOW_MIN_SAMPLES[1])
    assert stats["reason"] is None
    assert stats["samples"] >= 30
    assert stats["p50"] is not None
    assert stats["mean"] is not None
    assert stats["zscore"] is not None


def test_insufficient_samples_and_rebuild_version(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    series = builder.build_series("600519")

    # 1 年窗口样本不足 120 → reason
    single_day = [series[-1]]
    stats = builder.window_stats(single_day, "pe_ttm", 1, WINDOW_MIN_SAMPLES[1])
    assert stats["reason"] == "insufficient_samples"

    report = builder.rebuild_all(["600519"])
    assert report["status"] == "success"
    assert report["version"] == 1
    rows = duckdb_store.read_query(
        "SELECT metric, window_years, method, value FROM research_statistics "
        "WHERE stock_code = '600519' ORDER BY metric, window_years, method"
    )
    assert len(rows) > 0
    p10_row = [r for r in rows if r["method"] == "percentile" and r["window_years"] == 10 and r["metric"] == "pe_ttm"]
    assert p10_row and p10_row[0]["value"] is not None

    # 二次重建版本递增且原子替换
    report2 = builder.rebuild_all(["600519"])
    assert report2["version"] == 2
    rows2 = duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM research_statistics WHERE version = 2"
    )
    assert rows2[0]["c"] == len(rows)


def test_coverage_threshold_blocks_pe_stats(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    # 价格早于股本首点 5 年 → 覆盖不足 90%
    duckdb_store.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close)
           SELECT '600519', (DATE '2018-01-01' + INTERVAL (i) DAY), 10.0
           FROM range(1400) AS t(i)""",
    )
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    series = builder.build_series("600519")
    stats = builder.window_stats(
        series, "pe_ttm", 10, WINDOW_MIN_SAMPLES[10], coverage_pct=80.0,
    )
    assert stats["reason"] == "coverage_below_threshold"


# ─── 筛选引擎：统计字段 join ─────────────────────────────────────────────


def test_screening_engine_joins_statistics_domain(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store).rebuild_all(["600519"])

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    rule = {
        "logic": "AND",
        "columns": ["stock_code", "pe_ttm_stat_10y_percentile", "pe_ttm_stat_10y_zscore"],
        "rules": [{
            "field": "pe_ttm_stat_10y_percentile",
            "op": ">",
            "value": 50,
        }],
    }
    result = engine.run(
        rule,
        include_st=True, include_suspended=True, min_listing_years=0,
    )
    rows = result["results"]
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "600519"
    assert rows[0]["pe_ttm_stat_10y_percentile"] is not None


def test_stat_field_whitelist_and_known_gate() -> None:
    assert "pe_ttm_stat_10y_percentile" in STAT_FIELDS
    assert "spread_10y_stat_99y_zscore" in STAT_FIELDS
    assert "pe_ttm_stat_10y_median" not in STAT_FIELDS


def test_readiness_unchanged_and_no_data_quality_reference(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    before = minimum_data_readiness(duckdb_store, sqlite_store)
    assert before["schema_compatibility"]["compatible"] is True
    _seed_capital(duckdb_store, "600519", [(date(2020, 12, 31), 943800000.0)])
    after = minimum_data_readiness(duckdb_store, sqlite_store)
    assert after == before

    from app.core import data_quality as module
    source = inspect.getsource(module)
    assert "share_capital_history" not in source
    assert "research_statistics" not in source


# ─── API ─────────────────────────────────────────────────────────────────


def _api_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    return TestClient(app)


def test_research_statistics_api(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/600519/research-statistics", params={"metric": "pe_ttm"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["metric"] == "pe_ttm"
    assert len(payload["series"]) > 0
    assert "10y" in payload["statistics"]
    assert payload["coverage_threshold_pct"] == COVERAGE_THRESHOLD_PCT

    assert client.get("/api/stock/000001/research-statistics").status_code == 404


def test_schema_migration_v10_tables(
    database_paths,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    tables = {row["table_name"] for row in duck.read_query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    )}
    assert "share_capital_history" in tables
    assert "research_statistics" in tables
    migration = duck.read_query(
        "SELECT description FROM schema_migrations WHERE version = 10"
    )
    assert migration and "capital" in migration[0]["description"].lower()


# ─── P4-1/P4-2/P4-3/P4-4/P4-9 修复回归（reports/73）──────────────────────


def test_cninfo_adapter_forwards_explicit_dates_and_guards_staleness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P4-1：显式日期参数（防 akshare 默认 20091227~20241021 截断）+
    最新锚点新鲜度断言（fail-closed → retry）。"""
    from app.core.adapters.capital_history_adapter import CapitalHistoryAdapter
    import akshare as ak

    calls: list[dict] = []

    def fake_cninfo(symbol: str, start_date: str, end_date: str):
        calls.append({"symbol": symbol, "start_date": start_date, "end_date": end_date})
        import pandas as pd
        return pd.DataFrame([
            {"变动日期": "2026-06-30", "总股本": 94380.0, "变动原因简称": ""},
            {"变动日期": "2025-12-31", "总股本": 94380.0, "变动原因简称": "定期报告"},
        ])

    monkeypatch.setattr(ak, "stock_share_change_cninfo", fake_cninfo)
    adapter = CapitalHistoryAdapter(rate_limit=0)
    rows = adapter._fetch_cninfo("600519")

    assert calls and calls[0]["start_date"] == "19900101"
    assert calls[0]["end_date"] == str(date.today()).replace("-", "")
    assert rows[0]["total_shares"] == pytest.approx(94380.0 * 10000)

    # 新鲜度断言：最新锚点过旧 → 抛异常（源被截断回归）
    def stale_cninfo(symbol: str, start_date: str, end_date: str):
        import pandas as pd
        return pd.DataFrame([
            {"变动日期": "2020-12-31", "总股本": 94380.0, "变动原因简称": ""},
        ])

    monkeypatch.setattr(ak, "stock_share_change_cninfo", stale_cninfo)
    with pytest.raises(RuntimeError, match="最新锚点"):
        adapter._fetch_cninfo("600519")


def test_update_all_uses_due_cursor(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P4-2：update_all 只处理缺失/陈旧的上市股票（真正的续传游标）。"""
    _seed_stock(duckdb_store, "600519")
    _seed_stock(duckdb_store, "600000")
    # 600519 已有覆盖到 2026-08-07 的股本链 + 价格 → 非 due
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)
    _seed_capital(duckdb_store, "600519", [(date(2026, 8, 7), 943800000.0)])
    # 600000 有价格但无股本链 → due
    _seed_price(duckdb_store, "600000", date(2026, 8, 7), 10.0)

    updater = _updater(duckdb_store, sqlite_store, _FakeCapitalAdapter(
        main=[{
            "effective_date": "2026-08-07", "total_shares": 1000000000.0,
            "change_reason": None, "is_anchor": True,
        }],
    ))
    report = updater.update_all(max_stocks=20)

    assert report["status"] == "success"
    assert report["targeted"] == 1
    assert report["results"].get("600000") is not None
    assert "600519" not in report["results"], "已覆盖股票不得重复入选"
    rows = duckdb_store.read_query(
        "SELECT stock_code FROM share_capital_history WHERE stock_code = '600000'"
    )
    assert rows and rows[0]["stock_code"] == "600000"


def test_coverage_per_window_uses_verified_chain(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P4-3/P4-6：覆盖按窗口计算且只认 verified 延续。

    价格历史早于股本链首点的老股：10 年窗口覆盖不足，但 1 年窗口
    （股本已覆盖）仍可出统计；unverified 点不计覆盖。
    """
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    # 股本链 2025-08-01 起（窗口起点前）：1 年窗口全覆盖，全历史（2022 起价格）覆盖不足
    duckdb_store.write_query(
        """DELETE FROM share_capital_history WHERE stock_code = '600519'"""
    )
    duckdb_store.write_query(
        """INSERT INTO share_capital_history
           (stock_code, effective_date, total_shares, change_reason, is_anchor,
            verified, source, raw_hash, batch_id)
           VALUES ('600519', '2025-08-01', 1000000000.0, NULL, true, true,
                   'cninfo_capital', 'x', 'b1')"""
    )
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    series = builder.build_series("600519")

    coverage_99y = builder._capital_coverage("600519", 99)
    coverage_1y = builder._capital_coverage("600519", 1)
    assert coverage_99y < COVERAGE_THRESHOLD_PCT, "老股全历史覆盖应不足"
    assert coverage_1y == 100.0, "近期窗口（股本已覆盖）应 100%"

    stats_99y = builder.window_stats(series, "pe_ttm", 99, 1200, coverage_pct=coverage_99y)
    stats_1y = builder.window_stats(series, "pe_ttm", 1, WINDOW_MIN_SAMPLES[1], coverage_pct=coverage_1y)
    assert stats_99y["reason"] == "coverage_below_threshold"
    assert stats_1y.get("reason") is None

    # unverified 主链点不计覆盖
    duckdb_store.write_query(
        """UPDATE share_capital_history SET verified = false WHERE stock_code = '600519'"""
    )
    assert builder._capital_coverage("600519", 1) == 0.0


def test_publish_columns_fixed_when_first_record_is_reason(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P4-4：首记录为 reason 行时，后续成功行的 min_date/max_date 不丢失。"""
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    # 制造 pe_ttm 覆盖失败（reason 行在前），ttm_dividend_yield 在 1y 窗口成功
    duckdb_store.write_query(
        """DELETE FROM share_capital_history WHERE stock_code = '600519'"""
    )
    # 分红生效期覆盖窗口内价格日（ex 2025-06-30 / ann 2025-01-01）
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', '2025-06-30', '2025-01-01', 1.0)
           ON CONFLICT DO NOTHING"""
    )
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    report = builder.rebuild_all(["600519"])
    assert report["status"] == "success"

    rows = duckdb_store.read_query(
        """SELECT metric, method, window_years, min_date, max_date, value
           FROM research_statistics
           WHERE stock_code = '600519' AND method = 'zscore'
           ORDER BY metric, window_years"""
    )
    ttm_1y = [
        r for r in rows
        if r["metric"] == "ttm_dividend_yield" and r["window_years"] == 1
    ]
    assert ttm_1y and ttm_1y[0]["value"] is not None
    assert ttm_1y[0]["min_date"] is not None, "成功行不得丢失 min_date/max_date"
    assert ttm_1y[0]["max_date"] is not None
    pe = [r for r in rows if r["metric"] == "pe_ttm"]
    assert pe and pe[0]["value"] is None and pe[0]["min_date"] is None


def test_input_fingerprint_includes_dividends(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P4-9：分红变化必须改变输入指纹（触发统计重建）。"""
    _seed_statistics_inputs(duckdb_store, sqlite_store)
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    before = builder._input_fingerprint()

    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', '2026-06-30', '2026-05-01', 1.0)
           ON CONFLICT DO NOTHING"""
    )
    duckdb_store.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', '2026-07-30', '2026-07-01', 0.5)"""
    )
    after = builder._input_fingerprint()
    assert after != before