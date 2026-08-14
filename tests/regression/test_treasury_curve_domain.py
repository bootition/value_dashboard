"""P3 财政部国债曲线与股息率利差回归（reports/68）

覆盖：
- czb_mof adapter：单日全曲线解析、单期限历史解析、未来日期拒绝、空/畸形→missing、
  网络错误→error、独立限速、session 注入
- TreasuryCurveUpdater：历史回填原子替换、失败保旧值+retry、空结果 missing、日终 upsert、
  非工作日保旧值、align 5 日陈旧边界
- readiness 完全不变；快照利差列计算正确（分红公告/除权边界、陈旧→null、source_audit）
- /treasury-comparison API：series 正确、404、missing 语义
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.czb_mof_adapter import (
    KEY_TENORS,
    TreasuryMofAdapter,
)
from app.core.data_quality import minimum_data_readiness
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.treasury import TreasuryCurveUpdater
from app.web.api.stock_detail import router as stock_detail_router

# ─── 基于 2026-08-10 真实响应结构精简的 fixture ─────────────────────────

DAILY_PAYLOAD = [{
    "ycDefId": "2c9081e50a2f9606010a3068cae70001",
    "ycDefName": "财政部-中国国债收益率曲线",
    "worktime": "2026-08-07",
    "seriesData": [
        [0.0, 0.9633], [0.025, 0.9957], [0.25, 1.1736], [0.5, 1.2100],
        [1.0, 1.23], [2.0, 1.25], [3.0, 1.26], [5.0, 1.40],
        [7.0, 1.53], [10.0, 1.7074], [30.0, 2.16],
    ],
}]

HISTORY_PAYLOAD = [{
    "ycDefId": "2c9081e50a2f9606010a3068cae70001",
    "ycDefName": "10年",
    "worktime": "2026-08-07",
    "seriesData": [
        [1141142400000, 2.9],
        [1785686400000, 1.7169],
        [1785772800000, 1.7126],
        [1786032000000, 1.7114],
    ],
}]

EMPTY_PAYLOAD: list = []

MISSING_PAYLOAD = {"status": 0, "message": "error"}


def _mock_client(*, daily=None, history=None, recorded: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(str(request.url))
        if "czbQueryYz" in str(request.url):
            payload = history if history is not None else HISTORY_PAYLOAD
        else:
            payload = daily if daily is not None else DAILY_PAYLOAD
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _result(
    data: list[dict],
    *,
    error: str | None = None,
    raw: bytes | None = None,
    confidence: str = "strict",
) -> FetchResult:
    raw = raw or json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source="czb_mof",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw).hexdigest(),
            confidence=confidence,
            error=error,
        ),
        raw_response=raw,
    )


def _seed_stock(duck: DuckDBStore, code: str = "600519") -> None:
    """完备可筛选种子（P3-3 修复后：trust 遮蔽由 warning_codes 驱动，
    需要 readiness 通过、warning_codes=[]，否则 /treasury-comparison 会
    按数据质量遮蔽股息率/利差）。"""
    from tests.conftest import insert_minimum_screenable_data

    duck.write_query(
        """INSERT INTO stock_meta
           (stock_code, name, exchange, listing_date, is_st, is_suspended, is_listed)
           VALUES (?, ?, 'SSE', '2020-01-01', false, false, true)""",
        [code, code],
    )
    insert_minimum_screenable_data(duck, code)


def _seed_price(duck: DuckDBStore, code: str, trade_date: date, close: float) -> None:
    duck.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close)
           VALUES (?, ?, ?) ON CONFLICT DO NOTHING""",
        [code, trade_date, close],
    )


def _seed_dividend(
    duck: DuckDBStore, code: str, ex_date: date, announcement_date: date, dps: float,
) -> None:
    duck.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        [code, ex_date, announcement_date, dps],
    )


def _seed_curve(
    duck: DuckDBStore, curve_date: date, tenor: float, yield_pct: float,
    *,
    fetch_time: datetime | None = None,
    batch: str = "b1",
) -> None:
    duck.write_query(
        """INSERT INTO treasury_yield_curve
           (curve_date, tenor_years, yield_pct, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES (?, ?, ?, 'czb_mof', ?, ?, 'strict', ?)""",
        [curve_date, tenor, yield_pct,
         fetch_time or datetime.now(UTC), "0" * 64, batch],
    )


def _treasury_updater(duck: DuckDBStore, sqlite: SQLiteStore, adapter) -> TreasuryCurveUpdater:
    return TreasuryCurveUpdater(duck=duck, sqlite=sqlite, adapter=adapter)


class _FakeCzbAdapter:
    def __init__(self, *, daily=None, history=None, error: str | None = None) -> None:
        self.daily = daily
        self.history = history
        self.error = error
        self.calls: list[dict] = []

    def fetch(self, request: FetchRequest) -> FetchResult:
        params = dict(request.extra_params)
        self.calls.append(params)
        if self.error:
            return _result([], error=self.error, confidence="missing")
        mode = params.get("mode")
        if mode == "daily":
            return _result(self.daily or [], confidence="strict")
        return _result(self.history or [], confidence="strict")


# ─── adapter：解析 / 未来拒绝 / 缺失 / 限速 / 注入 ────────────────────


def test_adapter_parses_daily_curve() -> None:
    client = _mock_client()
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": "2026-08-07"},
    ))
    assert result.metadata.error is None
    assert result.metadata.confidence == "strict"
    assert result.metadata.source == "czb_mof"
    assert len(result.metadata.raw_response_hash) == 64
    rows = {row["tenor_years"]: row["yield_pct"] for row in result.data}
    assert rows[10.0] == pytest.approx(1.7074)
    assert rows[0.25] == pytest.approx(1.1736)
    assert all(str(row["curve_date"]) == "2026-08-07" for row in result.data)
    client.close()


def test_adapter_parses_history_sequence() -> None:
    client = _mock_client()
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "history", "tenor": 10.0,
                      "start": "2006-01-01", "end": "2026-08-07"},
    ))
    assert result.metadata.error is None
    dates = [row["curve_date"] for row in result.data]
    assert dates == ["2006-03-01", "2026-08-03", "2026-08-04", "2026-08-07"]
    assert result.data[-1]["yield_pct"] == pytest.approx(1.7114)
    client.close()


def test_adapter_rejects_future_work_time() -> None:
    recorded: list[str] = []
    client = _mock_client(recorded=recorded)
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    future = (date.today() + timedelta(days=30)).isoformat()
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": future},
    ))
    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"
    assert recorded == [], "未来日期不得发起网络请求"
    client.close()


def test_adapter_drops_future_points_from_history() -> None:
    payload = [{
        "ycDefName": "10年", "worktime": "2099-01-01",
        "seriesData": [[1141142400000, 2.9], [4102444800000, 5.0]],
    }]
    client = _mock_client(history=payload)
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "history", "tenor": 10.0,
                      "start": "2006-01-01", "end": "2099-12-31"},
    ))
    assert [row["curve_date"] for row in result.data] == ["2006-03-01"]
    client.close()


def test_adapter_empty_and_malformed_are_missing() -> None:
    for payload in (EMPTY_PAYLOAD, MISSING_PAYLOAD, b"not-json{{{"):
        # B023：循环变量通过默认参数绑定，避免晚绑定读取最后一个 payload
        def handler(request: httpx.Request, _payload: bytes | dict = payload) -> httpx.Response:
            content = _payload if isinstance(_payload, bytes) else \
                json.dumps(_payload, ensure_ascii=False).encode("utf-8")
            return httpx.Response(200, content=content)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = TreasuryMofAdapter(rate_limit=0, session=client)
        result = adapter.fetch(FetchRequest(
            data_type="treasury_yield_curve",
            extra_params={"mode": "daily", "work_time": "2026-08-07"},
        ))
        assert result.data == []
        assert result.metadata.error is None
        assert result.metadata.confidence == "missing"
        client.close()


def test_adapter_network_error_sets_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": "2026-08-07"},
    ))
    assert result.data == []
    assert result.metadata.error is not None
    client.close()


def test_adapter_default_rate_limit_and_supported_types() -> None:
    adapter = TreasuryMofAdapter()
    assert adapter.rate_limit_interval >= 0.5
    assert adapter.name == "czb_mof"
    assert adapter.supported_data_types == {"treasury_yield_curve"}


def test_adapter_uses_injected_session() -> None:
    client = _mock_client()
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    assert adapter.client is client
    adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": "2026-08-07"},
    ))
    assert adapter.client is client
    adapter.close()
    client.close()


# ─── updater：回填 / 日终 / 保旧值 / retry / missing / align ────────────


def test_backfill_replaces_tenor_atomically(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_curve(duckdb_store, date(2026, 8, 1), 10.0, 1.5)
    _seed_curve(duckdb_store, date(2026, 8, 1), 5.0, 1.2)
    history = [
        {"curve_date": "2026-08-03", "tenor_years": 10.0, "yield_pct": 1.71},
        {"curve_date": "2026-08-07", "tenor_years": 10.0, "yield_pct": 1.70},
    ]
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter(history=history))

    report = updater.backfill([10.0])

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT curve_date, yield_pct FROM treasury_yield_curve "
        "WHERE tenor_years = 10.0 ORDER BY curve_date"
    )
    assert len(rows) == 2
    assert rows[0]["yield_pct"] == pytest.approx(1.71)
    # 5 年期限不受影响
    other = duckdb_store.read_query(
        "SELECT yield_pct FROM treasury_yield_curve WHERE tenor_years = 5.0"
    )
    assert other[0]["yield_pct"] == pytest.approx(1.2)


def test_backfill_failure_preserves_old_values_and_records_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_curve(duckdb_store, date(2026, 8, 1), 10.0, 1.5)
    updater = _treasury_updater(
        duckdb_store, sqlite_store, _FakeCzbAdapter(error="source down"),
    )

    report = updater.backfill([10.0])

    assert report["status"] == "failed"
    rows = duckdb_store.read_query(
        "SELECT yield_pct FROM treasury_yield_curve WHERE tenor_years = 10.0"
    )
    assert rows[0]["yield_pct"] == pytest.approx(1.5)
    retries = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM retry_list WHERE data_type = 'treasury_yield_curve'"
    )
    assert retries[0]["c"] == 1


def test_backfill_empty_keeps_old_values_and_records_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_curve(duckdb_store, date(2026, 8, 1), 10.0, 1.5)
    updater = _treasury_updater(
        duckdb_store, sqlite_store, _FakeCzbAdapter(history=[]),
    )

    report = updater.backfill([10.0])

    assert report["status"] == "failed"
    assert report["results"][10.0]["reason"] == "source_empty"
    rows = duckdb_store.read_query(
        "SELECT yield_pct FROM treasury_yield_curve WHERE tenor_years = 10.0"
    )
    assert rows[0]["yield_pct"] == pytest.approx(1.5)
    missing = sqlite_store.query(
        "SELECT field_name FROM missing_list WHERE resolved_at IS NULL"
    )
    assert missing == [{"field_name": "treasury_curve_10.0"}]


def test_update_daily_upserts_and_keeps_old_on_empty(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    daily = [
        {"curve_date": "2026-08-07", "tenor_years": 10.0, "yield_pct": 1.70},
        {"curve_date": "2026-08-07", "tenor_years": 5.0, "yield_pct": 1.40},
    ]
    updater = _treasury_updater(
        duckdb_store, sqlite_store, _FakeCzbAdapter(daily=daily),
    )

    report = updater.update_daily(["2026-08-07"])

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT tenor_years, yield_pct FROM treasury_yield_curve "
        "WHERE curve_date = '2026-08-07' ORDER BY tenor_years"
    )
    assert len(rows) == 2

    # 非工作日（空结果）：保留旧值 + missing，不覆盖
    updater = _treasury_updater(
        duckdb_store, sqlite_store, _FakeCzbAdapter(daily=[]),
    )
    report = updater.update_daily(["2026-08-08"])
    assert report["status"] == "failed"
    rows = duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM treasury_yield_curve WHERE curve_date = '2026-08-08'"
    )
    assert rows[0]["c"] == 0


def test_align_5_day_staleness_boundary(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_curve(duckdb_store, date(2026, 8, 1), 10.0, 1.5)
    updater = TreasuryCurveUpdater(duck=duckdb_store, sqlite=sqlite_store)

    ok = updater.align(date(2026, 8, 6), 10.0)   # 5 天 → 可用
    assert ok["status"] == "ok"
    assert ok["yield_pct"] == pytest.approx(1.5)
    assert ok["staleness_days"] == 5

    stale = updater.align(date(2026, 8, 7), 10.0)  # 6 天 → 陈旧
    assert stale["status"] == "stale"
    assert stale["yield_pct"] is None

    missing = updater.align(date(2026, 8, 1), 5.0)  # 该期限无数据
    assert missing["status"] == "missing"


def test_readiness_unchanged_by_treasury_domain(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    before = minimum_data_readiness(duckdb_store, sqlite_store)
    assert before["schema_compatibility"]["compatible"] is True

    _seed_curve(duckdb_store, date(2026, 8, 1), 10.0, 1.5)

    after = minimum_data_readiness(duckdb_store, sqlite_store)
    assert after == before, "国债曲线数据不得改变 A 股 readiness"


def test_readiness_code_has_no_treasury_reference() -> None:
    import inspect

    from app.core import data_quality as module
    source = inspect.getsource(module)
    assert "treasury_yield_curve" not in source


# ─── 快照计算：TTM 股息率与利差列 ───────────────────────────────────────


def test_snapshot_ttm_dividend_yield_and_spread(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from app.core.indicators.calculator import IndicatorCalculator

    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)
    _seed_dividend(duckdb_store, "600519",
                   date(2026, 6, 30), date(2026, 5, 1), 1.0)   # 12 个月内
    _seed_dividend(duckdb_store, "600519",
                   date(2025, 6, 30), date(2025, 5, 1), 0.5)   # 12 个月外
    _seed_dividend(duckdb_store, "600519",
                   date(2026, 9, 30), date(2026, 9, 1), 2.0)   # 未来除权不计入
    _seed_curve(duckdb_store, date(2026, 8, 10), 10.0, 1.5)
    _seed_curve(duckdb_store, date(2026, 8, 10), 5.0, 1.4)
    _seed_curve(duckdb_store, date(2026, 7, 20), 2.0, 1.3)     # 超过 5 日陈旧

    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    result = calculator.compute_all_for_stock("600519")

    assert result["ttm_dividend_yield"] == pytest.approx(10.0)   # 1.0/10.0*100
    assert result["div_yield_spread_10y"] == pytest.approx(8.5)  # 10.0-1.5
    assert result["div_yield_spread_5y"] == pytest.approx(8.6)
    assert result["div_yield_spread_2y"] is None                 # 陈旧超限
    assert result["div_yield_spread_30y"] is None                # 曲线缺失


def test_snapshot_no_dividend_yields_null_spread(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from app.core.indicators.calculator import IndicatorCalculator

    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)
    _seed_curve(duckdb_store, date(2026, 8, 5), 10.0, 1.5)

    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    result = calculator.compute_all_for_stock("600519")

    assert result["ttm_dividend_yield"] is None
    assert result["div_yield_spread_10y"] is None


# ─── /treasury-comparison API ───────────────────────────────────────────


def _api_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    return TestClient(app)


def test_treasury_comparison_api_series_and_alignment(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519", date(2026, 8, 6), 10.0)
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)
    _seed_dividend(duckdb_store, "600519",
                   date(2026, 6, 30), date(2026, 5, 1), 1.0)
    _seed_curve(duckdb_store, date(2026, 8, 5), 10.0, 1.5)

    client = _api_client(duckdb_store, sqlite_store)
    response = client.get("/api/stock/600519/treasury-comparison", params={"tenor": 10.0})

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenor"] == 10.0
    assert payload["tenors_available"] == list(KEY_TENORS)
    assert payload["max_staleness_days"] == 5
    # fixture 价格覆盖到今日：series 按价格日降序（最新在前）
    assert payload["series"][0]["price_date"] == str(date.today())
    day_0807 = next(
        item for item in payload["series"] if item["price_date"] == "2026-08-07"
    )
    assert day_0807["ttm_div_yield"] == pytest.approx(10.0)
    assert day_0807["curve_yield"] == pytest.approx(1.5)
    assert day_0807["spread"] == pytest.approx(8.5)
    assert day_0807["curve_date"] == "2026-08-05"
    assert day_0807["reason"] is None
    # 8-06 同样可用
    day_0806 = next(
        item for item in payload["series"] if item["price_date"] == "2026-08-06"
    )
    assert day_0806["spread"] == pytest.approx(8.5)
    assert payload["provenance"]["source"] == "czb_mof"
    assert payload["provenance"]["batch_id"] == "b1"


def test_treasury_comparison_api_missing_semantics(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)

    client = _api_client(duckdb_store, sqlite_store)
    response = client.get("/api/stock/600519/treasury-comparison")

    assert response.status_code == 200
    payload = response.json()
    assert payload["series"][0]["reason"] == "curve_missing"
    assert payload["missing"] is True
    assert payload["provenance"] is None


def test_treasury_comparison_api_unknown_stock_and_bad_tenor(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    client = _api_client(duckdb_store, sqlite_store)
    assert client.get("/api/stock/000001/treasury-comparison").status_code == 404
    # 股票不存在优先 404；已知股票但期限非法 → 422
    _seed_stock(duckdb_store)
    assert client.get(
        "/api/stock/600519/treasury-comparison", params={"tenor": 99}
    ).status_code == 422


def test_export_provenance_includes_curve_alignment_for_spread(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3（PRD §12.5）：利差结果列的溯源必须附带期限、曲线日与数据差。"""
    from app.web.api.screening import _field_provenance

    _seed_stock(duckdb_store)
    _seed_curve(duckdb_store, date(2026, 8, 5), 10.0, 1.5)
    duckdb_store.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id,
            fetch_time, raw_response_hash, confidence)
           VALUES ('600519', 'div_yield_spread_10y', '2026-08-07', 8.5,
                   'derived_calculator', 'b1', CURRENT_TIMESTAMP, ?, 'approximate')""",
        ["0" * 64],
    )
    results = [{
        "stock_code": "600519",
        "div_yield_spread_10y": 8.5,
        "_report_date": "2026-08-07",
    }]

    provenance = _field_provenance(
        duckdb_store, sqlite_store, results, ["div_yield_spread_10y"],
    )

    entry = provenance[0].get("div_yield_spread_10y", {})
    assert entry["tenor_years"] == 10.0
    assert entry["curve_date"] == "2026-08-05"
    assert entry["staleness_days"] == 2
    assert entry["source"] == "derived_calculator"


def test_export_provenance_aligns_on_latest_price_date(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3-2 修复（reports/73）：导出溯源对齐锚点应为快照实际使用的
    最新价格日（latest_price_date），而非财务报告期 _report_date。"""
    from app.web.api.screening import _field_provenance

    _seed_stock(duckdb_store)
    _seed_curve(duckdb_store, date(2026, 8, 5), 10.0, 1.5)
    duckdb_store.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id,
            fetch_time, raw_response_hash, confidence)
           VALUES ('600519', 'div_yield_spread_10y', '2026-06-30', 8.5,
                   'derived_calculator', 'b1', CURRENT_TIMESTAMP, ?, 'approximate')""",
        ["0" * 64],
    )
    results = [{
        "stock_code": "600519",
        "div_yield_spread_10y": 8.5,
        # 财务报告期为 6-30，但快照利差实际按 8-07 价格日计算
        "_report_date": "2026-06-30",
        "latest_price_date": "2026-08-07",
    }]

    provenance = _field_provenance(
        duckdb_store, sqlite_store, results, ["div_yield_spread_10y"],
    )

    entry = provenance[0].get("div_yield_spread_10y", {})
    # 8-05 曲线点距 8-07 价格日 2 天；若错误对齐 6-30 报告期则陈旧 36 天
    assert entry["curve_date"] == "2026-08-05"
    assert entry["staleness_days"] == 2


def test_treasury_comparison_masks_dividend_when_unverified(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3-3 修复（reports/73）：DIVIDEND_DATES_UNVERIFIED 时
    /treasury-comparison 遮蔽股息率与利差，与全站信任模型一致。"""
    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)
    _seed_dividend(duckdb_store, "600519",
                   date(2026, 6, 30), date(2026, 5, 1), 1.0)
    _seed_curve(duckdb_store, date(2026, 8, 5), 10.0, 1.5)
    duckdb_store.write_query(
        """INSERT INTO dividends
           (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES ('600519', DATE '2024-06-28', NULL, 1.0)"""
    )

    client = _api_client(duckdb_store, sqlite_store)
    response = client.get("/api/stock/600519/treasury-comparison", params={"tenor": 10.0})

    assert response.status_code == 200
    payload = response.json()
    assert payload["trust"]["dividend_trusted"] is False
    assert payload["trust"]["warning_codes"] == ["DIVIDEND_DATES_UNVERIFIED"]
    # 8-05 当日曲线可用：股息率与利差被遮蔽，国债基准不受分红可信度影响
    day = next(
        item for item in payload["series"] if item["price_date"] == "2026-08-05"
    )
    assert day["ttm_div_yield"] is None
    assert day["spread"] is None
    assert day["reason"] == "dividend_untrusted"
    assert day["curve_yield"] == pytest.approx(1.5)


def test_adapter_rejects_non_finite_yields(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3-10 修复（reports/73）：NaN/±Infinity 收益率不得入库。"""
    from app.core.adapters.czb_mof_adapter import _to_float

    assert _to_float("nan") is None
    assert _to_float("inf") is None
    assert _to_float("-inf") is None
    assert _to_float(1.7) == pytest.approx(1.7)

    payload = [{
        "ycDefName": "10年", "worktime": "2026-08-07",
        "seriesData": [[1785686400000, float("nan")], [1785772800000, 1.7126]],
    }]
    client = _mock_client(history=payload)
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "history", "tenor": 10.0,
                      "start": "2006-01-01", "end": "2026-08-07"},
    ))
    assert len(result.data) == 1
    assert result.data[0]["yield_pct"] == pytest.approx(1.7126)
    client.close()


def test_backfill_invalid_tenors_fails_explicitly(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3-8 修复（reports/73）：显式传入非法期限不再静默空转。"""
    updater = _treasury_updater(
        duckdb_store, sqlite_store, _FakeCzbAdapter(history=[]),
    )
    report = updater.backfill([99.0, 999.0])
    assert report["status"] == "failed"
    assert report["reason"] == "no_valid_tenors"
    assert report["targeted"] == 0


def test_refresh_if_due_gates_on_marker(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3-4 修复（reports/73）：当日已刷新则 skip，不再重复请求源站。"""
    from app.core.treasury import REFRESH_MARKER_KEY

    fake = _FakeCzbAdapter(daily=[
        {"curve_date": "2026-08-07", "tenor_years": 10.0, "yield_pct": 1.7},
    ])
    updater = _treasury_updater(duckdb_store, sqlite_store, fake)
    first = updater.refresh_if_due()
    assert first["status"] != "failed"
    assert len(fake.calls) >= 1

    # 标记已写入 → 第二次直接 skip，无网络请求
    calls_after_first = len(fake.calls)
    second = updater.refresh_if_due()
    assert second["status"] == "skipped"
    assert second["reason"] == "refreshed_today"
    assert len(fake.calls) == calls_after_first

    # 标记过期（两天前）→ 重新执行
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        [REFRESH_MARKER_KEY,
         (datetime.now(UTC) - timedelta(days=2)).isoformat(),
         datetime.now(UTC).isoformat()],
    )
    third = updater.refresh_if_due()
    assert third["status"] != "skipped"
