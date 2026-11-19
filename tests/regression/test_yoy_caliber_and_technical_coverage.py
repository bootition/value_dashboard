"""reports/131 §9.2 P0-4（同比双口径）与 P0-5（技术三指标覆盖 + 门禁）回归。

钉死的四件事
------------
1. **同比双口径**：同一只股票构造"年报口径与 TTM 口径方向相反"的场景 →
   `*_yoy` 必须走 TTM、`*_yoy_fy` 必须走年报，且快照 `*_yoy` 与详情页
   `/metric-history`（`stock_detail._metric_history_rows`，`base_period='ttm'`）
   逐值同源。
2. **技术补偿脚本**：3 只股票中 2 只有完整 qfq、1 只只有 1 天 →
   `scripts/backfill_technical_indicators.py` 只重算前 2 只，第 3 只保持 NULL。
3. **体检规则**：技术三列最新期覆盖率 < 95% → `TECHNICAL_COVERAGE_GAP`
   以 P1/block 触发（规则注册表第 14 条）。
4. **筛选门禁**：技术字段覆盖率 < 95% 时 `/api/screening/indicators` 把它们
   放进 `unavailable(reason=not_available_for_latest_period)`。

全部使用 tmp_path 里的临时 DuckDB/SQLite（S1 隔离），不触碰 data/ 正式库。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.indicators.calculator import IndicatorCalculator
from app.core.quality.asset_health import run_asset_health
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router as screening_router
from app.web.api.stock_detail import _metric_history_rows
from scripts.backfill_technical_indicators import (
    main as backfill_technical_main,
)
from scripts.backfill_technical_indicators import (
    select_candidates as select_technical_candidates,
)

REPORT_DATE = date(2026, 9, 30)
TECHNICAL_FIELDS = ("period_return", "annualized_volatility", "max_drawdown")


# ─────────────────────────────────────────────────────────────────────────────
# 夹具
# ─────────────────────────────────────────────────────────────────────────────


def _seed_stock(duck: DuckDBStore, code: str = "600519") -> None:
    duck.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, is_listed, listing_date,
                total_shares, circ_shares)
           VALUES (?, '测试', 'SHSE', TRUE, '2020-01-01', 1000, 800)""",
        [code],
    )


def _seed_yoy_financials(duck: DuckDBStore, code: str = "600519") -> None:
    """年报口径与 TTM 口径方向相反的财务序列。

    年报：FY2025 营收 100 vs FY2024 200 → FY YoY = -50%
    TTM ：2026Q3 = FY2025(100) + 9M2026(300) − 9M2025(50) = 350
          基期 = 2025-12-31 TTM = 100 → TTM YoY = +250%
    """
    periods = [
        ("2023-12-31", 180.0, 18.0, 16.0),
        ("2024-09-30", 70.0, 7.0, 6.0),
        ("2024-12-31", 200.0, 20.0, 18.0),
        ("2025-09-30", 50.0, 5.0, 4.0),
        ("2025-12-31", 100.0, 10.0, 9.0),
        ("2026-03-31", 80.0, 8.0, 7.0),
        ("2026-06-30", 180.0, 18.0, 16.0),
        ("2026-09-30", 300.0, 30.0, 27.0),
    ]
    with duck.write_connection() as connection:
        connection.executemany(
            """INSERT INTO income_statement
                   (stock_code, report_date, revenue, parent_net_profit,
                    deducted_net_profit)
               VALUES (?, ?, ?, ?, ?)""",
            [(code, report_date, revenue, profit, deducted)
             for report_date, revenue, profit, deducted in periods],
        )
        connection.executemany(
            """INSERT INTO balance_sheet
                   (stock_code, report_date, total_assets, total_liabilities,
                    total_equity, total_equity_parent,
                    total_current_assets, total_current_liabilities, inventory)
               VALUES (?, ?, 1000, 400, 600, 600, 500, 250, 50)""",
            [(code, report_date) for report_date, *_ in periods],
        )
        connection.executemany(
            """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
               VALUES (?, ?, 50)""",
            [(code, report_date) for report_date, *_ in periods],
        )


def _seed_prices(
    duck: DuckDBStore,
    code: str,
    *,
    raw_dates: list[str],
    qfq_dates: list[str],
) -> None:
    for index, trade_date in enumerate(raw_dates):
        duck.write_query(
            """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
               VALUES (?, ?, ?, ?)""",
            [code, trade_date, 10.0 + index, 1000.0],
        )
    for index, trade_date in enumerate(qfq_dates):
        duck.write_query(
            """INSERT INTO price_daily_qfq (stock_code, trade_date, close)
               VALUES (?, ?, ?)""",
            [code, trade_date, 10.0 + index],
        )


def _seed_trading_dates(sqlite: SQLiteStore, dates: list[str]) -> None:
    with sqlite.transaction() as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO trading_dates (trade_date) VALUES (?)",
            [(trade_date,) for trade_date in dates],
        )


def _insert_snapshot_row(
    duck: DuckDBStore,
    code: str,
    *,
    period_return: float | None = None,
    report_date: date = REPORT_DATE,
) -> None:
    duck.write_query(
        """INSERT INTO indicator_snapshot
               (stock_code, report_date, period_return, calculated_at)
           VALUES (?, ?, ?, ?)""",
        [code, report_date, period_return, datetime.now(UTC)],
    )


def _make_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(screening_router)
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 同比双口径：快照与详情页同源
# ─────────────────────────────────────────────────────────────────────────────


def test_yoy_ttm_matches_detail_and_fy_is_stored_separately(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_yoy_financials(duckdb_store)
    _seed_prices(
        duckdb_store, "600519",
        raw_dates=["2026-09-28", "2026-09-29", "2026-09-30"],
        qfq_dates=["2026-09-28", "2026-09-29", "2026-09-30"],
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)

    result = calculator.compute_all_for_stock("600519")
    calculator._write_batch([result])

    snapshot = duckdb_store.read_query(
        """SELECT revenue_yoy, revenue_yoy_fy,
                  net_profit_yoy, net_profit_yoy_fy,
                  deducted_profit_yoy, deducted_profit_yoy_fy
           FROM indicator_snapshot WHERE stock_code = '600519'"""
    )[0]

    # TTM 口径：+250%；FY 口径：−50%。两个口径方向相反，绝不能互相回填。
    assert snapshot["revenue_yoy"] == pytest.approx(2.5)
    assert snapshot["revenue_yoy_fy"] == pytest.approx(-0.5)
    assert snapshot["net_profit_yoy"] == pytest.approx(2.5)
    assert snapshot["net_profit_yoy_fy"] == pytest.approx(-0.5)
    assert snapshot["deducted_profit_yoy"] == pytest.approx(32.0 / 9.0 - 1.0)
    assert snapshot["deducted_profit_yoy_fy"] == pytest.approx(-0.5)
    assert (snapshot["revenue_yoy"] > 0) is not (snapshot["revenue_yoy_fy"] > 0)

    # 一致性断言：详情页（base_period='ttm'）与快照对同一股票同一字段同号同量级。
    for metric in ("revenue_yoy", "net_profit_yoy", "deducted_profit_yoy"):
        detail_rows = _metric_history_rows(duckdb_store, "600519", metric, "ttm")
        detail_latest = next(
            row["value"] for row in reversed(detail_rows) if row["value"] is not None
        )
        assert detail_latest == pytest.approx(snapshot[metric]), (
            f"{metric}: snapshot={snapshot[metric]} detail={detail_latest}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 同比补偿脚本：存量旧值改写为 TTM + 回填 FY
# ─────────────────────────────────────────────────────────────────────────────


def test_backfill_yoy_script_rewrites_ttm_and_fills_fy(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, tmp_path: Path,
) -> None:
    from scripts.backfill_yoy_calibers import main as backfill_yoy_main

    _seed_stock(duckdb_store)
    _seed_yoy_financials(duckdb_store)
    _seed_prices(
        duckdb_store, "600519",
        raw_dates=["2026-09-28", "2026-09-29", "2026-09-30"],
        qfq_dates=["2026-09-28", "2026-09-29", "2026-09-30"],
    )
    # 迁移后的存量行：*_yoy 还是旧的年报口径 −50%，*_yoy_fy 为空。
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
               (stock_code, report_date, revenue_yoy, net_profit_yoy,
                deducted_profit_yoy)
           VALUES ('600519', ?, -0.5, -0.5, -0.5)""",
        [REPORT_DATE],
    )
    backup = tmp_path / "backup-yoy"

    assert backfill_yoy_main(
        ["--db", str(duckdb_store.db_path), "--yes", "--backup-dir", str(backup)]
    ) == 0

    row = duckdb_store.read_query(
        """SELECT revenue_yoy, revenue_yoy_fy, net_profit_yoy, net_profit_yoy_fy,
                  deducted_profit_yoy, deducted_profit_yoy_fy
           FROM indicator_snapshot WHERE stock_code = '600519'"""
    )[0]
    assert row["revenue_yoy"] == pytest.approx(2.5)
    assert row["revenue_yoy_fy"] == pytest.approx(-0.5)
    assert row["net_profit_yoy"] == pytest.approx(2.5)
    assert row["net_profit_yoy_fy"] == pytest.approx(-0.5)
    assert row["deducted_profit_yoy"] == pytest.approx(32.0 / 9.0 - 1.0)
    assert row["deducted_profit_yoy_fy"] == pytest.approx(-0.5)
    assert (backup / "indicator_snapshot.parquet").is_file()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 技术补偿脚本：只重算有 ≥2 条 qfq 收盘的股票
# ─────────────────────────────────────────────────────────────────────────────


def _seed_technical_case(
    duck: DuckDBStore, sqlite: SQLiteStore, tmp_path: Path,
) -> dict[str, str]:
    trade_dates = ["2026-09-28", "2026-09-29", "2026-09-30"]
    for code in ("000001", "000002"):
        _seed_stock(duck, code)
        _seed_prices(duck, code, raw_dates=trade_dates, qfq_dates=trade_dates)
        _insert_snapshot_row(duck, code)
    # 第 3 只：raw 三天都有，但 qfq 只有最后一天 → 不满足"qfq 收盘 ≥2 条"。
    _seed_stock(duck, "000003")
    _seed_prices(
        duck, "000003",
        raw_dates=trade_dates, qfq_dates=["2026-09-30"],
    )
    _insert_snapshot_row(duck, "000003")
    _seed_trading_dates(sqlite, trade_dates)
    return {
        "db": str(duck.db_path),
        "backup": str(tmp_path / "backup-technical"),
    }


def test_backfill_technical_script_only_recomputes_stocks_with_qfq_series(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    paths = _seed_technical_case(duckdb_store, sqlite_store, tmp_path)

    candidates = select_technical_candidates(duckdb_store)
    assert [row["stock_code"] for row in candidates] == ["000001", "000002"]

    # 默认 dry-run：不写库、不落备份。
    assert backfill_technical_main(
        ["--db", paths["db"], "--backup-dir", paths["backup"]]
    ) == 0
    still_null = duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM indicator_snapshot WHERE period_return IS NULL"
    )[0]["c"]
    assert still_null == 3
    assert not Path(paths["backup"]).exists()

    # --yes：只重算前 2 只，第 3 只保持 NULL；写前备份必须落盘。
    assert backfill_technical_main(
        ["--db", paths["db"], "--yes", "--backup-dir", paths["backup"]]
    ) == 0
    rows = {
        row["stock_code"]: row
        for row in duckdb_store.read_query(
            """SELECT stock_code, period_return, annualized_volatility, max_drawdown
               FROM indicator_snapshot ORDER BY stock_code"""
        )
    }
    assert rows["000001"]["period_return"] is not None
    assert rows["000002"]["period_return"] is not None
    assert rows["000003"]["period_return"] is None
    assert rows["000001"]["max_drawdown"] is not None
    assert rows["000002"]["max_drawdown"] is not None
    assert (Path(paths["backup"]) / "indicator_snapshot.parquet").is_file()


# ─────────────────────────────────────────────────────────────────────────────
# 3. asset_health：TECHNICAL_COVERAGE_GAP 规则（P1 / block）
# ─────────────────────────────────────────────────────────────────────────────


def test_technical_coverage_gap_rule_fires_below_95_pct(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_technical_case(duckdb_store, sqlite_store, tmp_path)
    # 2/3 有 period_return，1/3 有 annualized_volatility/max_drawdown → < 95%。
    duckdb_store.write_query(
        """UPDATE indicator_snapshot SET period_return = 0.1
           WHERE stock_code IN ('000001', '000002')"""
    )

    payload = run_asset_health(
        duckdb_store,
        sqlite_store,
        now=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
        settings={"min_domain_rows": 1},
        allow_expensive=False,
    )
    findings = [
        finding for finding in payload["findings"]
        if finding["code"] == "TECHNICAL_COVERAGE_GAP"
    ]

    assert findings, "技术三列覆盖率 33%-67% 必须触发 TECHNICAL_COVERAGE_GAP"
    assert {finding["field"] for finding in findings} == set(TECHNICAL_FIELDS)
    assert all(finding["level"] == "P1" for finding in findings)
    assert all(finding["severity"] == "block" for finding in findings)
    assert "TECHNICAL_COVERAGE_GAP" in [rule["code"] for rule in payload["rules"]]
    assert payload["rules"][-1]["code"] == "TECHNICAL_COVERAGE_GAP"


# ─────────────────────────────────────────────────────────────────────────────
# 4. /api/screening/indicators：覆盖率不足 → unavailable
# ─────────────────────────────────────────────────────────────────────────────


def test_screening_indicators_hide_low_coverage_technical_fields(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _seed_technical_case(duckdb_store, sqlite_store, tmp_path)
    # 3 只里只有 1 只有 period_return（33.3%），另两只全空。
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET period_return = 0.1 WHERE stock_code = '000001'"
    )
    client = _make_client(duckdb_store, sqlite_store)

    response = client.get("/api/screening/indicators")

    assert response.status_code == 200
    body = response.json()
    unavailable = {item["name"]: item for item in body["unavailable"]}
    for field in TECHNICAL_FIELDS:
        assert field in unavailable, f"{field} 覆盖率不足时不应出现在可选指标里"
        assert unavailable[field]["reason"] == "not_available_for_latest_period"
        assert unavailable[field]["coverage_pct"] < 95.0
    listed = {item["name"] for item in body["indicators"]}
    assert not (set(TECHNICAL_FIELDS) & listed)


# ─────────────────────────────────────────────────────────────────────────────
# 5. technical_pending 的写入/消费/清除
# ─────────────────────────────────────────────────────────────────────────────


def test_technical_pending_roundtrip_and_source_wiring(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    import hashlib

    from app.core.adapters.base import FetchResult, SourceMetadata
    from app.core.update import IncrementalUpdater
    from tests.support import source_guard

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._mark_technical_pending({"000001": "2026-09-30"}) == 1
    assert updater._load_technical_pending() == ["000001"]

    # 同水位重复写入不重复入列（避免每轮无条件重算）。
    assert updater._mark_technical_pending({"000001": "2026-09-30"}) == 0

    # 重算成功后移出；失败/被拒的保留。
    assert updater._finalize_technical_pending(
        {"status": "success", "success": 1, "failed": 0},
        updater._load_technical_pending(),
    ) == 1
    assert updater._load_technical_pending() == []

    # ── 价格侧接线：行为断言（真跑一轮价格步骤，见 qfq 回归同一套假适配器）──
    target_date, local_date = "2026-11-13", "2026-11-12"
    _seed_stock(duckdb_store, "600519")
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES (?, ?, 9.0)",
            ["600519", local_date],
        )

    def _result(adjust: str, trade_date: str) -> FetchResult:
        payload = f"{adjust}:{trade_date}".encode()
        return FetchResult(
            data=[{"trade_date": trade_date, "close": 10.0, "volume": 100.0}],
            metadata=SourceMetadata(
                source="local_cache",
                fetch_time=datetime.now(UTC),
                raw_response_hash=hashlib.sha256(payload).hexdigest(),
                confidence="strict",
            ),
            raw_response=payload,
        )

    class _Adapter:
        def fetch(self, request):
            return _result(
                request.adjust,
                target_date if request.adjust == "raw" else local_date,
            )

    price_updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_Adapter(),
    )
    price_updater._latest_expected_trading_date = lambda today: target_date
    price_updater._get_latest_local_price_date = lambda: local_date

    report = price_updater._update_prices_incremental(max_stocks=0)

    assert report["success"] == 1
    assert report["technical_pending_marked"] == 1
    assert price_updater._load_technical_pending() == ["600519"], (
        "价格步骤没给 qfq 写入成功的股票打 technical_pending → 技术三列永久 NULL"
    )

    # ── 指标侧接线：流程函数体太大、真跑要打网络，只能结构守卫 ──
    # AST 调用节点（注释/日志里的同名文字不算数）+ `*` 展开断言
    # （"读了名单却没并进重算集合"必须被拦住）；resolve_body 会顺委托壳追实现。
    source_guard.assert_calls(
        IncrementalUpdater._run_incremental_update_flow,
        "_load_technical_pending",
        "_finalize_technical_pending",
        starred={"technical_pending_codes": 2},
    )


def test_ttm_yoy_uses_abs_denominator_on_negative_base(
    duckdb_store, sqlite_store,
) -> None:
    """TTM 同比在**基期为负**时必须用 abs 分母（reports/135 回归）。

    事故：`_calc_ttm_yoy` 原来写 `latest = curr / base - 1`，而 FY 口径用
    `(curr - prev) / abs(prev)`。基期为负时两个公式方向相反：

    - 亏损从 −100 收窄到 −50（**变好**）→ `curr/base-1` = −0.5（显示变差）
    - 亏损从 −100 扩大到 −200（**变差**）→ `curr/base-1` = +1.0（显示变好）

    正式库实测受影响：`net_profit_yoy` 负基期 1,455 只（763 亏损收窄被报成负增长、
    692 亏损扩大被报成正增长）；`deducted_profit_yoy` 1,697 只。
    """
    from app.core.indicators.calculator import IndicatorCalculator

    code = "000901"
    _seed_stock(duckdb_store, code)
    # 全部为年报期，避免季度 TTM 复合：TTM(2024)=−100、TTM(2025)=−50（亏损收窄）
    periods = [
        ("2023-12-31", 100.0, -20.0, -20.0),
        ("2024-12-31", 100.0, -100.0, -100.0),
        ("2025-12-31", 100.0, -50.0, -50.0),
    ]
    with duckdb_store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO income_statement
                   (stock_code, report_date, revenue, parent_net_profit,
                    deducted_net_profit)
               VALUES (?, ?, ?, ?, ?)""",
            [(code, d, rev, profit, ded) for d, rev, profit, ded in periods],
        )
        connection.executemany(
            """INSERT INTO balance_sheet
                   (stock_code, report_date, total_assets, total_liabilities,
                    total_equity, total_equity_parent,
                    total_current_assets, total_current_liabilities, inventory)
               VALUES (?, ?, 1000, 400, 600, 600, 500, 250, 50)""",
            [(code, d) for d, *_ in periods],
        )

    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    growth = calc._calc_ttm_yoy(code)

    # 亏损从 −100 收窄到 −50 = 变好 → 同比必须是**正数**
    assert growth["net_profit_yoy"] == pytest.approx(0.5), (
        f"负基数用了 curr/base-1 会得到 −0.5（把亏损收窄报成负增长），实测 {growth['net_profit_yoy']}"
    )
