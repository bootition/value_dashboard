from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.data_quality import (
    DIVIDEND_INDICATOR_FIELDS,
    indicator_trust,
    mask_untrusted_values,
    read_warning_codes,
)
from app.core.financial_period import expected_financial_period
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.stock_detail import router as stock_detail_router
from app.web.api.watchlist import router as watchlist_router
from tests.conftest import insert_minimum_screenable_data
from tests.support.seed import CURRENT_TIMESTAMP, Seed
from tests.support.seed_sqlite import SeedSqlite

SNAPSHOT_VALUE_FIELDS = (
    "latest_close", "pe_ttm", "pb_mrq", "roe", "gross_margin", "net_margin",
    "debt_ratio", "revenue_yoy", "net_profit_yoy", "dividend_yield",
    "total_market_cap", "circ_market_cap",
)


def _build_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    app.include_router(watchlist_router)
    return TestClient(app)


def _last_bar_date(duck: DuckDBStore, code: str) -> str:
    """播种后 `price_daily_raw` 里这根股票的**最后一根日线**日期（YYYY-MM-DD）。

    为什么快照的 `latest_price_date` 不能用 `CURRENT_DATE`（2026-11-21 周六 00:45
    4 条用例齐红的根因）：`tests/conftest.py::insert_minimum_screenable_data` 的
    `generate_series` 只产**工作日** bar（`WHERE EXTRACT(ISODOW FROM trade_date) < 6`），
    于是周六/周日的 `CURRENT_DATE` 在日线里根本不存在；而
    `app/core/data_quality.py::minimum_data_readiness` 的 `has_coherent_snapshot`
    要求 `indicator_snapshot.latest_price_date` **逐字等于**
    `MAX(price_daily_raw.trade_date)`。写 `CURRENT_DATE` 只在"今天恰好有 bar"
    （周一~周五）时成立 —— 周五夜里绿、过了午夜到周六就红。
    改成"问库里最后一根 bar 是哪天"，与今天几号无关。
    """
    rows = duck.read_query(
        "SELECT CAST(MAX(trade_date) AS VARCHAR) AS last_bar"
        " FROM price_daily_raw WHERE stock_code = ?",
        [code],
    )
    last_bar = rows[0]["last_bar"] if rows else None
    assert last_bar, f"播种后 {code} 必须至少有一根日线（否则测试前提不成立）"
    return last_bar


def _readiness_period() -> str:
    """本次播种用的报告期 = 生产口径的「按今天应当已发布的期」（季末）。

    为什么不能写死 `'2025-12-31'`：`minimum_data_readiness` 的
    `has_complete_financial_period` 判据是
    `complete_financials.report_date >= CURRENT_DATE - INTERVAL '18 months'`
    （`app/core/data_quality.py`）。写死一个日历天，全绿只在它之后的 18 个月内
    成立 —— 这是同一类"时钟耦合"，只是 2027-07-01 之后才炸。
    `expected_financial_period()` 恒落在"距今 0~4 个月的季末"，窗口余量 >1 年，
    且与生产共用同一个"今天"来源（`app/core/financial_period.py`，RG1 单源）。
    """
    return expected_financial_period()


def _rebase_seed_period(duckdb_store: DuckDBStore, code: str, period: str) -> None:
    """把 `insert_minimum_screenable_data` 写死的 `'2025-12-31'` 期整体搬到 `period`。

    三表 / 快照 / `source_audit` 的**报告期**必须一起搬：`minimum_data_readiness`
    除了 18 个月新鲜度，还要求「快照期 == 最新完整三表期」
    （`snapshot_period_alignment`）与「完整期上有逐字段血缘证据」
    （`lineage_coverage`，缺一条即阻断）。只搬其中一张表会换来另一种红。

    只搬**财务期**这几类；`price_daily_raw` / `price_daily_qfq` 两类批次的
    `source_audit.report_date` 是"最新交易日"，与报告期不是同一语义，不能动
    （动了 `lineage_coverage` 的 `raw_price_ok` 立刻缺证据）。
    """
    with duckdb_store.write_connection() as connection:
        for table in ("balance_sheet", "income_statement", "cash_flow", "indicator_snapshot"):
            connection.execute(
                # 表名来自上面四元素白名单常量，值走参数占位符。
                f"UPDATE {table} SET report_date = ? WHERE stock_code = ?",
                [period, code],
            )
        connection.execute(
            """UPDATE source_audit SET report_date = ?
               WHERE stock_code = ?
                 AND fetch_batch_id IN (
                     SELECT batch_id FROM fetch_batch
                     WHERE data_type NOT IN ('price_daily_raw', 'price_daily_qfq')
                 )""",
            [period, code],
        )


def _seed_screenable(duckdb_store: DuckDBStore) -> None:
    # `stock_meta` / `indicator_snapshot` 走工厂的按域方法（`stock` / `snapshot`）。
    # 快照的 `calculated_at` 照原 SQL 继续写成库侧求值（`CURRENT_TIMESTAMP`
    # 哨兵），不是固定字面量：原 SQL 里就是这个函数，换成工厂默认的历史字面量
    # 会静默改掉 `calculated_at` 的语义（`has_current_snapshot_inputs` 要拿它跟
    # `source_audit.fetch_time` 比先后，两者必须同一个时钟源）。
    Seed(duckdb_store).stock(
        "000001",
        name="Test",
        exchange="SZSE",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
    )


    insert_minimum_screenable_data(duckdb_store)
    period = _readiness_period()
    _rebase_seed_period(duckdb_store, "000001", period)
    # `overwrite=True` = DuckDB 的 `INSERT OR REPLACE`（upsert）：只改本次给出的列，
    # 与原来 `ON CONFLICT ... DO UPDATE SET <这九列>` 的粒度逐列一致
    # （`latest_close`/`latest_price_date`/`calculated_at` 仍取 `insert_minimum_screenable_data`
    # 先写入的那一份）。
    Seed(duckdb_store).snapshot(
        "000001",
        period,
        latest_close=10,
        # 不是 `CURRENT_DATE`：周末/节假日今天没有 bar，见 `_last_bar_date`。
        latest_price_date=_last_bar_date(duckdb_store, "000001"),
        calculated_at=CURRENT_TIMESTAMP,
        pe_ttm=12.5,
        pb_mrq=1.5,
        roe=0.2,
        gross_margin=0.4,
        net_margin=0.1,
        debt_ratio=0.2,
        revenue_yoy=0.3,
        net_profit_yoy=0.25,
        dividend_yield=0.02,
        overwrite=True,
    )


def test_stock_search_matches_partial_code_and_name(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    # `exchange='SSE'` 逐字传：断言的名称/代码之外，交易所是这条搜索用例的前提。
    Seed(duckdb_store).stock(
        "600519",
        name="贵州茅台",
        exchange="SSE",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
    )
    client = _build_client(duckdb_store, sqlite_store)

    by_code = client.get('/api/stock/search', params={'query': '0519'})
    by_name = client.get('/api/stock/search', params={'query': '茅'})

    assert by_code.status_code == 200
    assert by_code.json()['items'][0]['stock_code'] == '600519'
    assert by_name.status_code == 200
    assert by_name.json()['items'][0]['name'] == '贵州茅台'


def _poison_lineage(duckdb_store: DuckDBStore) -> None:
    """毒源：孤儿批次（`missing-batch` 不在 `fetch_batch`）+ 长度非 64 的 hash。

    这两点才是 `LINEAGE_INVALID` 的判据（`app/core/data_quality.py` 的
    `invalid_hash_rows` / `orphan_batch_rows`）。`fetch_time` 不参与判定，用工厂的
    固定字面量即可（仍早于快照的 `calculated_at`，与原 `now() - INTERVAL '1 day'`
    同一方向）；`raw_response_hash` 是**故意**写成 `'not-a-sha'` 的，工厂逐字照收、
    不校验形状 —— 校验是要被测的生产逻辑。
    """
    Seed(duckdb_store).source_audit(
        "000001", "revenue",
        report_date="2025-12-31", value=1.0, source="tampered",
        fetch_batch_id="missing-batch", raw_response_hash="not-a-sha", confidence="strict",
    )


def _poison_dividend_dates(duckdb_store: DuckDBStore) -> None:
    """毒源：`announcement_date` 缺着 → `DIVIDEND_DATES_UNVERIFIED`。"""
    Seed(duckdb_store).dividends("000001", [("2024-06-28", None, 1.0)])


def _flat_indicator_metrics(body: dict) -> list[dict]:
    return [
        metric
        for category in body["indicators"].values()
        for metric in category.values()
    ]


def test_indicators_returns_values_and_trust_when_quality_clean(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/000001/indicators")

    assert response.status_code == 200
    body = response.json()
    assert body["trust"]["warning_codes"] == []
    assert body["trust"]["untrusted_all"] is False
    assert body["trust"]["untrusted_fields"] == []
    assert body["indicators"]["valuation"]["pe_ttm"]["value"] == 12.5
    assert body["indicators"]["valuation"]["pe_ttm"]["untrusted"] is False
    assert body["latest_close"] == 10


def test_indicators_masks_snapshot_values_when_lineage_invalid(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    _poison_lineage(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/000001/indicators")

    assert response.status_code == 200
    body = response.json()
    assert "LINEAGE_INVALID" in body["trust"]["warning_codes"]
    assert body["trust"]["untrusted_all"] is True
    metrics = _flat_indicator_metrics(body)
    assert metrics
    assert all(metric["value"] is None for metric in metrics)
    assert all(metric["untrusted"] is True for metric in metrics)
    assert body["latest_close"] is None
    # 只读展示保留：报告期/新鲜度等元数据仍然可见
    # 报告期是本次播种的相对期（`_readiness_period()`），不是写死的日历天。
    assert body["report_date"] == _readiness_period()
    assert body["freshness"] is not None


def test_indicators_masks_only_dividend_fields_when_dividend_dates_unverified(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    _poison_dividend_dates(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/000001/indicators")

    assert response.status_code == 200
    body = response.json()
    assert body["trust"]["warning_codes"] == ["DIVIDEND_DATES_UNVERIFIED"]
    assert body["trust"]["untrusted_all"] is False
    assert body["trust"]["untrusted_fields"] == sorted(DIVIDEND_INDICATOR_FIELDS)
    dividend_yield = body["indicators"]["valuation"]["dividend_yield"]
    assert dividend_yield["value"] is None
    assert dividend_yield["untrusted"] is True
    assert body["indicators"]["shareholder_return"]["payout_ratio"]["untrusted"] is True
    pe_ttm = body["indicators"]["valuation"]["pe_ttm"]
    assert pe_ttm["value"] == 12.5
    assert pe_ttm["untrusted"] is False
    assert body["latest_close"] == 10


def test_watchlist_returns_values_and_trust_when_quality_clean(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    # 自选行走 `SeedSqlite.watchlist`：`group_name='default'` 是 schema 默认值，
    # 但 `item["group_name"] == "default"` 这条断言读的就是它，所以逐字写出来。
    SeedSqlite(sqlite_store).watchlist("000001", group_name="default")
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200
    body = response.json()
    assert body["trust"]["warning_codes"] == []
    assert body["trust"]["untrusted_all"] is False
    item = body["items"][0]
    assert item["pe_ttm"] == 12.5
    assert item["latest_close"] == 10
    assert item["untrusted_fields"] == []


def test_watchlist_masks_snapshot_values_when_lineage_invalid(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store)
    _poison_lineage(duckdb_store)
    # 同上：自选行同形状（`group_name` 被断言读到，逐字传）。
    SeedSqlite(sqlite_store).watchlist("000001", group_name="default")
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200
    body = response.json()
    assert "LINEAGE_INVALID" in body["trust"]["warning_codes"]
    assert body["trust"]["untrusted_all"] is True
    item = body["items"][0]
    assert all(item[field] is None for field in SNAPSHOT_VALUE_FIELDS)
    assert set(item["untrusted_fields"]) == set(SNAPSHOT_VALUE_FIELDS)
    # 只读展示保留：身份与来源元数据仍然可见
    assert item["name"] == "Test"
    assert item["group_name"] == "default"


def test_indicator_trust_policy_matches_frontend_is_indicator_untrusted() -> None:
    clean = indicator_trust([])
    assert clean["untrusted_all"] is False
    assert clean["untrusted_fields"] == []

    operational = indicator_trust(["UNPUBLISHED_OVERRIDES", "STALE_RUNNING_JOBS"])
    assert operational["untrusted_all"] is False
    assert operational["untrusted_fields"] == []

    dividend = indicator_trust(["DIVIDEND_DATES_UNVERIFIED"])
    assert dividend["untrusted_all"] is False
    assert dividend["untrusted_fields"] == sorted(DIVIDEND_INDICATOR_FIELDS)

    for code in (
        "FINANCIAL_SHELL_ROWS", "SNAPSHOT_STALE", "LINEAGE_INVALID",
        "CODE_IDENTITY_ALIAS", "LIVE_SCHEMA_INCOMPATIBLE",
    ):
        assert indicator_trust([code])["untrusted_all"] is True

    # 前端 data-quality.ts 2026-08-27 起将 MINIMUM_DATA_NOT_READY 视为
    # 少数股票缺口提示，不遮蔽全量快照指标。
    assert indicator_trust(["MINIMUM_DATA_NOT_READY"])["untrusted_all"] is False


def test_mask_untrusted_values_fail_closed() -> None:
    values = {"pe_ttm": 12.5, "dividend_yield": 0.02}

    masked_all = mask_untrusted_values(values, indicator_trust(["LINEAGE_INVALID"]))
    assert masked_all == {"pe_ttm": None, "dividend_yield": None}

    masked_dividend = mask_untrusted_values(
        values, indicator_trust(["DIVIDEND_DATES_UNVERIFIED"])
    )
    assert masked_dividend == {"pe_ttm": 12.5, "dividend_yield": None}

    masked_clean = mask_untrusted_values(values, indicator_trust([]))
    assert masked_clean == values


def test_read_warning_codes_fail_closed_and_failures_not_cached() -> None:
    calls = {"count": 0}

    class _BrokenStore:
        db_path = "broken-store"

        def read_query(self, *args: object, **kwargs: object) -> list:
            calls["count"] += 1
            raise RuntimeError("database unreadable")

    broken = _BrokenStore()
    first = read_warning_codes(broken, broken)  # type: ignore[arg-type]
    second = read_warning_codes(broken, broken)  # type: ignore[arg-type]

    assert first == ["LINEAGE_INVALID"]
    assert second == ["LINEAGE_INVALID"]
    assert calls["count"] == 4, "fail-closed 每次调用含锁探测与构建两条查询，且不得缓存"
