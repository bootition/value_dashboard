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
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

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

    # `stock_meta` → `Seed.stock()`；`exchange='SSE'` 逐字保留（不靠代码前缀推断——
    # 这条种子用的是历史值，用例断言不以交易所为口径）。
    Seed(duck).stock(
        code, name=code, exchange="SSE",
        listing_date="2020-01-01", is_st=False, is_suspended=False, is_listed=True,
    )
    insert_minimum_screenable_data(duck, code)


def _seed_price(duck: DuckDBStore, code: str, trade_date: date, close: float) -> None:
    # 原 SQL 带 `ON CONFLICT DO NOTHING`（同一行重复播种不报错）→ `if_exists="ignore"`；
    # 逐行入口 `prices()` 不开放 `if_exists`，故走 `prices_bulk()`（单行批次）。
    Seed(duck).prices_bulk(
        [(code, trade_date, close)], if_exists="ignore",
    )


def _seed_dividend(
    duck: DuckDBStore, code: str, ex_date: date, announcement_date: date, dps: float,
) -> None:
    # 原来带 `ON CONFLICT DO NOTHING`（同一行重复调用不报错）→ `if_exists="ignore"`。
    Seed(duck).dividends(
        code, [(ex_date, announcement_date, dps)], if_exists="ignore",
    )


def _seed_curve(
    duck: DuckDBStore, curve_date: date, tenor: float, yield_pct: float,
    *,
    fetch_time: datetime | None = None,
    batch: str = "b1",
) -> None:
    # `fetch_time` 缺省是"刚抓的"（原来的 `datetime.now(UTC)`）→ `trace_now=True`；
    # 用例要钉某一刻就显式传 `fetch_time`（调用点给的列覆盖工厂默认值）。
    explicit = {} if fetch_time is None else {"fetch_time": fetch_time}
    Seed(duck, trace_now=True).treasury_yield_curve(
        curve_date, tenor, yield_pct, raw_hash="0" * 64, batch_id=batch, **explicit,
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


def test_adapter_empty_payloads_are_legal_missing() -> None:
    for payload in (EMPTY_PAYLOAD, MISSING_PAYLOAD):
        # B023：循环变量通过默认参数绑定，避免晚绑定读取最后一个 payload
        def handler(request: httpx.Request, _payload: dict = payload) -> httpx.Response:
            content = json.dumps(_payload, ensure_ascii=False).encode("utf-8")
            return httpx.Response(200, content=content)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = TreasuryMofAdapter(rate_limit=0, session=client)
        result = adapter.fetch(FetchRequest(
            data_type="treasury_yield_curve",
            extra_params={"mode": "daily", "work_time": "2026-08-07"},
        ))
        assert result.data == []
        assert result.metadata.error is None
        assert result.metadata.error_kind == "legal_empty"
        assert result.metadata.reason
        assert result.metadata.confidence == "missing"
        client.close()


def test_adapter_malformed_response_is_source_unavailable() -> None:
    """2026-11-12 契约 §1：非 JSON 畸形响应 → source_unavailable。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json{{{")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TreasuryMofAdapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": "2026-08-07"},
    ))
    assert result.data == []
    assert result.metadata.error is not None
    assert result.metadata.error_kind == "source_unavailable"
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


def test_daily_success_resolves_only_matching_daily_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """daily 成功只能删除对应 daily 重试条目，history 未解决条目必须保留。"""
    history_extra = json.dumps({"mode": "history", "tenor": 10.0}, ensure_ascii=False)
    daily_extra = json.dumps({"mode": "daily", "work_date": "2026-08-07"}, ensure_ascii=False)
    # `retry_list` → `SeedSqlite.retry()`；两条在主键 `request` 五件套上不同
    # （`extra_json` 的 mode/tenor 不同），所以能在同一个事务里共存。
    with sqlite_store.transaction() as conn:
        retry_seed = SeedSqlite(sqlite_store).join(conn)
        retry_seed.retry(
            "__market__", "treasury_yield_curve", adapter="czb_mof",
            error="history boom", last_attempt=datetime.now(UTC).isoformat(),
            extra_json=history_extra,
        )
        retry_seed.retry(
            "__market__", "treasury_yield_curve", adapter="czb_mof",
            error="daily boom", last_attempt=datetime.now(UTC).isoformat(),
            extra_json=daily_extra,
        )
    daily = [{
        "curve_date": "2026-08-07", "tenor_years": 10.0, "yield_pct": 1.70,
    }]
    updater = _treasury_updater(
        duckdb_store, sqlite_store, _FakeCzbAdapter(daily=daily),
    )

    report = updater.update_daily(["2026-08-07"])

    assert report["status"] == "success"
    remaining = sqlite_store.query(
        "SELECT extra_json FROM retry_list WHERE data_type = 'treasury_yield_curve'"
    )
    assert remaining == [{"extra_json": history_extra}], \
        "daily 成功不得误删 history 未解决 retry"


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


# ─── 缺口补齐（2026-08-14 红队 F2 回归）：backfill_missing_days ─────────


_FIXED_TODAY = date(2026, 8, 14)


def _seed_trading_dates(sqlite: SQLiteStore, days: list[str]) -> None:
    with sqlite.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO trading_dates (trade_date) VALUES (?)",
            [(day,) for day in days],
        )


def _seed_full_curve(duck: DuckDBStore, curve_date: date) -> None:
    for tenor in KEY_TENORS:
        _seed_curve(duck, curve_date, tenor, 1.5)


def _seed_missing_entry(sqlite: SQLiteStore, day: str) -> None:
    # 原来卡在 A（写在调用点事务里，工厂会自开事务）+ B（`missing_list` 无方法）。
    with sqlite.transaction() as conn:
        SeedSqlite(sqlite).join(conn).missing_list(
            "__market__", f"treasury_curve_daily_{day}", "source_empty"
        )


def _gap_fill_stub(recorded: list):
    def fake(self, work_date):
        recorded.append(work_date)
        return {"status": "success"}

    return fake


def test_gap_fill_targets_uncovered_days_ascending_bounded(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.treasury._cn_today", lambda: _FIXED_TODAY)
    days = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    _seed_trading_dates(sqlite_store, days)
    # 8-10 / 8-11 已全期限覆盖，8-12..8-14 缺失
    _seed_full_curve(duckdb_store, date(2026, 8, 10))
    _seed_full_curve(duckdb_store, date(2026, 8, 11))
    recorded: list = []
    monkeypatch.setattr(TreasuryCurveUpdater, "_update_daily_one", _gap_fill_stub(recorded))
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter())

    report = updater.backfill_missing_days(max_days=3, lookback_trading_days=10)

    assert report["status"] == "success"
    assert report["targeted"] == ["2026-08-12", "2026-08-13", "2026-08-14"]
    assert recorded == [date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)]
    assert report["succeeded"] == 3
    assert report["failed"] == []


def test_gap_fill_respects_max_days_cap(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.treasury._cn_today", lambda: _FIXED_TODAY)
    _seed_trading_dates(sqlite_store, ["2026-08-12", "2026-08-13", "2026-08-14"])
    recorded: list = []
    monkeypatch.setattr(TreasuryCurveUpdater, "_update_daily_one", _gap_fill_stub(recorded))
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter())

    report = updater.backfill_missing_days(max_days=1, lookback_trading_days=10)

    assert report["targeted"] == ["2026-08-12"]
    assert recorded == [date(2026, 8, 12)]


def test_gap_fill_includes_unresolved_missing_list_entries(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.treasury._cn_today", lambda: _FIXED_TODAY)
    _seed_missing_entry(sqlite_store, "2026-08-07")  # 周五，合法候选
    recorded: list = []
    monkeypatch.setattr(TreasuryCurveUpdater, "_update_daily_one", _gap_fill_stub(recorded))
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter())

    report = updater.backfill_missing_days(max_days=5, lookback_trading_days=10)

    assert report["targeted"] == ["2026-08-07"]
    assert recorded == [date(2026, 8, 7)]


def test_gap_fill_resolves_weekend_missing_entries_without_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.treasury._cn_today", lambda: _FIXED_TODAY)
    _seed_missing_entry(sqlite_store, "2026-08-09")  # 周日，非交易日
    recorded: list = []
    monkeypatch.setattr(TreasuryCurveUpdater, "_update_daily_one", _gap_fill_stub(recorded))
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter())

    report = updater.backfill_missing_days(max_days=5, lookback_trading_days=10)

    assert report == {"status": "skipped", "reason": "no_missing_days"}
    assert recorded == []
    rows = sqlite_store.query(
        "SELECT resolved_at FROM missing_list "
        "WHERE field_name = 'treasury_curve_daily_2026-08-09'"
    )
    assert rows and rows[0]["resolved_at"] is not None


def test_gap_fill_excludes_future_days(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.treasury._cn_today", lambda: _FIXED_TODAY)
    _seed_missing_entry(sqlite_store, "2026-08-20")  # 未来日期
    recorded: list = []
    monkeypatch.setattr(TreasuryCurveUpdater, "_update_daily_one", _gap_fill_stub(recorded))
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter())

    report = updater.backfill_missing_days(max_days=5, lookback_trading_days=10)

    assert report == {"status": "skipped", "reason": "no_missing_days"}
    assert recorded == []


def test_gap_fill_skips_when_all_days_covered(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.treasury._cn_today", lambda: _FIXED_TODAY)
    _seed_trading_dates(sqlite_store, ["2026-08-13", "2026-08-14"])
    _seed_full_curve(duckdb_store, date(2026, 8, 13))
    _seed_full_curve(duckdb_store, date(2026, 8, 14))
    recorded: list = []
    monkeypatch.setattr(TreasuryCurveUpdater, "_update_daily_one", _gap_fill_stub(recorded))
    updater = _treasury_updater(duckdb_store, sqlite_store, _FakeCzbAdapter())

    report = updater.backfill_missing_days(max_days=3, lookback_trading_days=10)

    assert report == {"status": "skipped", "reason": "no_missing_days"}
    assert recorded == []


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
    """`data_quality` 不得引用国债曲线域（负向耦合，import 图 + AST 符号表）。

    拦的事故：曲线域被接进 readiness/数据质量判定（曲线缺一天 → 整站不可筛），
    或把 `app.core.treasury` 的 import 藏进某个函数体里绕过模块级依赖审查。
    旧写法 `"treasury_yield_curve" not in inspect.getsource(module)` 拦的是文本，
    重构（搬函数、改注释、加 docstring）会假红，真耦合未必拦住。
    """
    from app.core import data_quality as module
    from tests.support import source_guard

    source_guard.assert_no_coupling(
        module,
        forbidden_modules=("app.core.treasury",),
        forbidden_symbols=("treasury_yield_curve",),
        why="国债曲线是可选参照系：它缺失时利差列留 NULL，不该改变 A 股 readiness",
    )


# ─── 快照计算：TTM 股息率与利差列 ───────────────────────────────────────


def test_snapshot_ttm_dividend_yield_and_spread(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from app.core.indicators.calculator import IndicatorCalculator

    _seed_stock(duckdb_store)
    _seed_price(duckdb_store, "600519", date(2026, 8, 7), 10.0)
    # _seed_stock 会通过 conftest 写入价格史，曲线与分红日期都必须动态对齐
    # 该股最新价格日，不能用固定日期或 Python 的 date.today()。
    # 2026-11-12 修复（用例时间炸弹）：旧 fixture 把 2026-09-30 标成"未来除权"，
    # 但 TTM 窗口锚在最新价格日（此处=CURRENT_DATE）。过了 2026-09-30 之后
    # 这笔分红就落进 12 个月窗口，实测 ttm_dividend_yield=30.0（1.0+2.0）/10。
    # 计算口径本身正确（`_calc_treasury_spread`：价格日前 12 个月内已除权且
    # 已公告），因此只把三个分红日改为相对最新价格日，业务断言不变：
    # TTM 股息率只包含"过去 12 个月内已实施"的现金分红。
    latest_row = duckdb_store.read_query(
        "SELECT CAST(MAX(trade_date) AS DATE) AS d FROM price_daily_raw WHERE stock_code = '600519'"
    )[0]["d"]
    latest = latest_row if isinstance(latest_row, date) else date.fromisoformat(str(latest_row)[:10])
    _seed_dividend(duckdb_store, "600519",
                   latest - timedelta(days=60), latest - timedelta(days=90), 1.0)    # 12 个月内
    _seed_dividend(duckdb_store, "600519",
                   latest - timedelta(days=400), latest - timedelta(days=430), 0.5)  # 12 个月外
    _seed_dividend(duckdb_store, "600519",
                   latest + timedelta(days=60), latest - timedelta(days=10), 2.0)   # 未来除权不计入
    _seed_curve(duckdb_store, latest, 10.0, 1.5)
    _seed_curve(duckdb_store, latest, 5.0, 1.4)
    _seed_curve(duckdb_store, latest - timedelta(days=18), 2.0, 1.3)  # 超过 5 日陈旧

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
    # fixture 价格覆盖到该股最新价格日：series 按价格日降序
    latest_row = duckdb_store.read_query(
        "SELECT CAST(MAX(trade_date) AS DATE) AS d FROM price_daily_raw WHERE stock_code = '600519'"
    )[0]["d"]
    latest = latest_row if isinstance(latest_row, date) else date.fromisoformat(str(latest_row)[:10])
    assert payload["series"][0]["price_date"] == str(latest)
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
    Seed(duckdb_store, trace_now=True).source_audit(
        "600519", "div_yield_spread_10y",
        report_date="2026-08-07", value=8.5, source="derived_calculator",
        fetch_batch_id="b1", raw_response_hash="0" * 64, confidence="approximate",
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
    Seed(duckdb_store, trace_now=True).source_audit(
        "600519", "div_yield_spread_10y",
        report_date="2026-06-30", value=8.5, source="derived_calculator",
        fetch_batch_id="b1", raw_response_hash="0" * 64, confidence="approximate",
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
    Seed(duckdb_store).dividends("600519", [("2024-06-28", None, 1.0)])

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


def test_non_finite_yields_rejected_at_both_layers(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3-10 修复（reports/73）：NaN/±Infinity 收益率不得入库。

    2026-11-17（reports/137）：`_to_float` 已收口到 `app.core.numbers.to_float`
    （默认 `allow_inf=False`），所以这里**分两层断言**，避免"只测共享函数、适配器
    悄悄改回宽松口径也照样绿"：
      ① 共享工具本身：nan/±inf → None；
      ② 适配器**不再自带第二份实现**：czb_mof 模块里不得再有 `_to_float`，
         否则说明有人把口径分叉又抄回来了。
    """
    from app.core.numbers import to_float as _to_float

    assert _to_float("nan") is None
    assert _to_float("inf") is None
    assert _to_float("-inf") is None
    assert _to_float(1.7) == pytest.approx(1.7)

    import app.core.adapters.czb_mof_adapter as _czb
    from app.core.numbers import to_float_strict

    # 适配器里的 `_to_float` 必须是**共享实现本身**（同一对象），不是又抄一份。
    # 一旦有人写回本地 def，这里的 identity 断言立刻红。
    assert _czb._to_float is to_float_strict, (
        "czb_mof_adapter 里的 _to_float 不是 app.core.numbers.to_float_strict："
        "请改回 `from app.core.numbers import to_float_strict as _to_float`（reports/137 §2.3）"
    )

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
    # `data_refresh_state` → `SeedSqlite.refresh_state()`。新鲜度判据读的是
    # **`value`**（`TreasuryCurveUpdater._refreshed_today()` 只看 value），所以
    # 把 value 写成"两天前"就够；`overwrite=True` 覆盖工厂先前写下的那一行。
    SeedSqlite(sqlite_store).refresh_state(
        REFRESH_MARKER_KEY,
        (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        overwrite=True,
    )
    third = updater.refresh_if_due()
    assert third["status"] != "skipped"


def test_today_not_published_yet_is_deferred_not_failed(duckdb_store, sqlite_store) -> None:
    """当天国债曲线源侧还没发布 → `deferred`，**不许**记 failed 把整轮打成 partial。

    2026-11-24 实测：凌晨 00:52 跑更新时去抓**当天**的国债日报，源侧必然 `source_empty`
    → 本步 `failed` → `treasury_curve` 步骤 `partial` → 整轮 `partial`。
    「今天的数据还没公布」不是失败，是"等一会儿再来"。
    """
    from app.core.treasury import TreasuryCurveUpdater

    class EmptyTodayAdapter:
        """源侧对任何日期都回空（模拟"今天还没发布"）。"""

        def fetch(self, request):
            from datetime import UTC, datetime

            from app.core.adapters.base import FetchResult, SourceMetadata

            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="czb_mof", fetch_time=datetime.now(UTC),
                    raw_response_hash="c" * 64, confidence="strict",
                ),
            )

    service = TreasuryCurveUpdater(duck=duckdb_store, sqlite=sqlite_store)
    service.adapter = EmptyTodayAdapter()

    from app.core.treasury import _cn_today  # noqa: PLC0415 - 与生产同一口径

    report = service.refresh_if_due()
    daily = report.get("daily") or {}
    results = daily.get("results") or {}
    today_text = _cn_today().isoformat()

    # 今天的这一天必须是 deferred（不是 failed）
    if today_text in results:
        assert results[today_text]["status"] == "deferred", results[today_text]
        assert results[today_text]["reason"] == "source_not_published_yet"
    # 关键：整步不许因为"今天没发布"变成 partial
    assert report.get("status") != "partial" or today_text not in (daily.get("failed") or []), report
    assert today_text not in (daily.get("failed") or []), daily.get("failed")


def test_deferred_daily_does_not_consume_today_marker(duckdb_store, sqlite_store) -> None:
    """当天曲线未发布 → `deferred` 时**不许**写"今天已刷新"标记。

    否则凌晨那次 defer 会把当天标记消费掉 → 曲线要等到明天才补
    → 全市场股息率/利差整整滞后一天（比"整轮 partial"严重得多）。
    """
    from datetime import UTC, datetime

    from app.core.adapters.base import FetchResult, SourceMetadata
    from app.core.treasury import REFRESH_MARKER_KEY, TreasuryCurveUpdater

    class EmptyAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="czb_mof", fetch_time=datetime.now(UTC),
                    raw_response_hash="d" * 64, confidence="strict",
                ),
            )

    service = TreasuryCurveUpdater(duck=duckdb_store, sqlite=sqlite_store)
    service.adapter = EmptyAdapter()

    first = service.refresh_if_due()
    # 只断"当天窗口"这一步：本用例的假源对所有数据都回空，backfill 那条路径
    # 也会失败（那是另一回事，与"当天未发布"无关）。
    assert first["daily"]["status"] == "deferred", first["daily"]
    # 关键：标记没被消费 → 下一轮还会再试
    assert service._refreshed_today() is False, (
        "deferred 之后不许标记今天已刷新（否则当天曲线要等到明天才补）"
    )
    rows = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = ?", [REFRESH_MARKER_KEY]
    )
    assert rows == [], rows
