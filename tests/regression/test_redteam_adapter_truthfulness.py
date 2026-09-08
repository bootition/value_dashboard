"""红队修复回归：适配器截断/partial 披露、manager 熔断与关闭语义。

覆盖（2026-09 红队）：
- CNINFO 公告截断必须显式报错；100 页日期二分递归保留；北京公告日
- Tencent 分页上限未覆盖显式 start_date 必须返回 error
- AKShare/BaoStock 多代码/多年份子请求失败计入 metadata.error 并保留成功部分
- TDX 财报文件截断/单文件失败披露；逐主机失败关闭候选连接
- AdapterManager：partial 结果保留、熔断只计空数据错误、close 重置并清理
- DataInitializer：retry ON CONFLICT 不重置 retry_count；财务三表按 error 记 retry
- schema：retry_list 建唯一索引前按请求键去重；v12 funding_events 只在未执行时 DROP
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pandas as pd
import pytest

from app.core.adapters.akshare_adapter import AKShareAdapter
from app.core.adapters.baostock_adapter import BaoStockAdapter
from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.cninfo_adapter import _CN_TZ, CNINFOAdapter
from app.core.adapters.manager import AdapterManager
from app.core.adapters.tdx_adapter import TDXAdapter
from app.core.adapters.tencent_adapter import TencentAdapter
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore


def _result(
    data: list[dict[str, Any]],
    *,
    source: str = "tencent",
    error: str | None = None,
) -> FetchResult:
    raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source=source,  # type: ignore[arg-type]
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw).hexdigest(),
            confidence="approximate" if error is None else "missing",
            error=error,
        ),
        raw_response=raw,
    )


# ─── CNINFO：截断与北京公告日 ─────────────────────────────────────────────


def _cninfo_mock_client(
    page_payloads: dict[str, dict[str, Any]],
) -> httpx.Client:
    """按 (se_date, page_num) 返回 payload；未命中返回 total=30 的单页。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        page_num = int(body.get("pageNum", ["1"])[0])
        se_date = body.get("seDate", [""])[0]
        payload = page_payloads.get((se_date, page_num)) or {
            "announcements": [_ANN_RAW] * 30,
            "totalRecordNum": 30,
        }
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


_ANN_RAW = {
    "secCode": "000001",
    "secName": "平安银行",
    "announcementId": "ann-1",
    "announcementTitle": "2026年半年度报告",
    "announcementTime": 1785964800000,
    "adjunctUrl": "",
}


def _cninfo_adapter(monkeypatch, client: httpx.Client) -> CNINFOAdapter:
    adapter = CNINFOAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_resolve_org_id", lambda code: "org-1")
    monkeypatch.setattr(adapter, "_get_client", lambda: client)
    return adapter


def test_cninfo_max_pages_truncation_raises_runtime_error(monkeypatch) -> None:
    """max_pages<100 且 totalRecordNum>已取条数 → 必须显式 RuntimeError。"""
    client = _cninfo_mock_client({
        ("2026-01-01~2026-12-31", 1): {
            "announcements": [_ANN_RAW] * 30,
            "totalRecordNum": 90,
        },
    })
    adapter = _cninfo_adapter(monkeypatch, client)

    with pytest.raises(RuntimeError, match="truncated"):
        adapter._query_announcements(
            stock_code="000001",
            category=None,
            start_date="2026-01-01",
            end_date="2026-12-31",
            max_pages=1,
        )
    client.close()


def test_cninfo_page_100_date_split_recursion_is_preserved(monkeypatch) -> None:
    """page_num>=100 超量时仍按日期二分递归补齐，而不是返回前 100 页。"""
    parent_payload = {"announcements": [_ANN_RAW] * 30, "totalRecordNum": 3100}
    client = _cninfo_mock_client({
        (se_date, page_num): parent_payload
        for se_date in ("2026-01-01~2026-12-31",)
        for page_num in range(1, 101)
    })
    adapter = _cninfo_adapter(monkeypatch, client)

    items = adapter._query_announcements(
        stock_code="000001",
        category=None,
        start_date="2026-01-01",
        end_date="2026-12-31",
        max_pages=100,
    )

    # 本层 100 页被丢弃，子窗口各返回 30 条 → 60 条
    assert len(items) == 60
    client.close()


def test_cninfo_announcement_date_uses_beijing_calendar_day() -> None:
    # 2026-08-07 16:30 UTC = 2026-08-08 00:30 Asia/Shanghai
    ms = int(datetime(2026, 8, 7, 16, 30, tzinfo=UTC).timestamp() * 1000)
    normalized = CNINFOAdapter._normalize_announcement(
        {"secCode": "000001", "announcementTime": ms}, "000001"
    )

    assert normalized["announcement_date"].isoformat() == "2026-08-08"
    assert normalized["announcement_time"].hour == 16
    assert normalized["announcement_time"].tzinfo is not None
    assert normalized["announcement_time"].astimezone(_CN_TZ).hour == 0


def test_cninfo_dividends_is_explicitly_unsupported_and_removed_from_chain() -> None:
    from app.core.adapters.manager import DEFAULT_ADAPTER_PRIORITY

    adapter = CNINFOAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="dividends", stock_codes=["000001"]))

    assert result.data == []
    assert result.metadata.error is not None
    assert "unsupported" in result.metadata.error
    assert DEFAULT_ADAPTER_PRIORITY["dividends"] == ["akshare_eastmoney", "baostock"]


# ─── Tencent：显式 start_date 截断 ────────────────────────────────────────


def test_tencent_explicit_start_truncation_reports_error(monkeypatch) -> None:
    class SameBarResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "bj920000": {
                        "qfqday": [["2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174"]],
                    }
                }
            }

    monkeypatch.setattr(
        "app.core.adapters.tencent_adapter.requests.Session.get",
        lambda *args, **kwargs: SameBarResponse(),
    )
    result = TencentAdapter(rate_limit=0).fetch(
        FetchRequest(
            data_type="price_daily", stock_codes=["920000"],
            start_date="2020-01-01", end_date="2026-07-31", adjust="qfq",
        )
    )

    assert len(result.data) == 12
    assert result.metadata.error is not None
    assert "truncated" in result.metadata.error


# ─── AKShare：多代码子请求失败必须计入 error ───────────────────────────────


def test_akshare_price_daily_partial_failure_reports_error(monkeypatch) -> None:
    import app.core.adapters.akshare_adapter as module

    def fake_hist(**kwargs: Any) -> pd.DataFrame:
        if kwargs["symbol"] == "600519":
            return pd.DataFrame([{
                "日期": "2026-08-05", "股票代码": "600519", "开盘": 10.0,
                "收盘": 11.0, "最高": 12.0, "最低": 9.0, "成交量": 100.0,
                "成交额": 1000.0, "振幅": 1.0, "涨跌幅": 2.0, "涨跌额": 1.0,
                "换手率": 3.0,
            }])
        raise ConnectionError("down")

    monkeypatch.setattr(module, "ak", type("FakeAK", (), {
        "stock_zh_a_hist": staticmethod(fake_hist),
    })())
    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="price_daily", stock_codes=["600519", "000001"])
    )

    assert [row["stock_code"] for row in result.data] == ["600519"]
    assert result.metadata.error is not None
    assert "000001" in result.metadata.error


def test_akshare_financial_partial_failure_reports_error(monkeypatch) -> None:
    import app.core.adapters.akshare_adapter as module

    def fake_balance(symbol: str) -> pd.DataFrame:
        if symbol == "SH600519":
            return pd.DataFrame([{"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 1.0}])
        raise TimeoutError("source down")

    monkeypatch.setattr(module, "ak", type("FakeAK", (), {
        "stock_balance_sheet_by_report_em": staticmethod(fake_balance),
    })())
    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["600519", "000001"])
    )

    assert len(result.data) == 1
    assert result.metadata.error is not None
    assert "000001" in result.metadata.error


def test_akshare_dividends_partial_failure_reports_error(monkeypatch) -> None:
    import app.core.adapters.akshare_adapter as module

    def fake_dividends(symbol: str) -> pd.DataFrame:
        if symbol == "600519":
            return pd.DataFrame([{
                "实施方案公告日期": "2026-06-22", "分红类型": "年度分红",
                "送股比例": 1.0, "转增比例": 2.0, "派息比例": 6.0,
                "股权登记日": "2026-06-25", "除权日": "2026-06-26",
                "派息日": "2026-06-30", "实施方案分红说明": "10送1转2派6元(含税)",
            }])
        raise ConnectionError("dividend source down")

    monkeypatch.setattr(module, "ak", type("FakeAK", (), {
        "stock_dividend_cninfo": staticmethod(fake_dividends),
    })())
    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="dividends", stock_codes=["600519", "000001"])
    )

    assert [row["stock_code"] for row in result.data] == ["600519"]
    assert result.metadata.error is not None
    assert "000001" in result.metadata.error


# ─── BaoStock：子请求失败计入 error ────────────────────────────────────────


class _BarsResult:
    def __init__(self, error_code: str, error_msg: str, rows: list[list[str]] | None = None) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self._rows = list(rows or [])
        self.fields = None

    def next(self) -> bool:
        if self._rows:
            self._current = self._rows.pop(0)
            return True
        return False

    def get_row_data(self) -> list[str]:
        return getattr(self, "_current", [])


def test_baostock_price_daily_partial_failure_reports_error(monkeypatch) -> None:
    import app.core.adapters.baostock_adapter as module

    def fake_query(code, fields, **kwargs: Any):
        if code == "sh.600519":
            return _BarsResult("1", "source down")
        return _BarsResult("0", "ok", [[
            "2026-08-05", "10.0", "10.1", "10.2", "10.3", "1000", "10000", "0.5",
        ]])

    monkeypatch.setattr(module, "bs", type("FakeBS", (), {
        "login": staticmethod(lambda: type("L", (), {"error_code": "0", "error_msg": "ok"})()),
        "logout": staticmethod(lambda: None),
        "query_history_k_data_plus": staticmethod(fake_query),
    })())
    adapter = BaoStockAdapter(rate_limit=0)
    result = adapter.fetch(
        FetchRequest(
            data_type="price_daily", stock_codes=["600519", "000001"], adjust="raw",
        )
    )

    assert [row["stock_code"] for row in result.data] == ["000001"]
    assert result.metadata.error is not None
    assert "sh.600519" in result.metadata.error


class _DividendResult:
    FIELDS = [
        "code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate",
        "dividPlanDate", "dividRegistDate", "dividOperateDate", "dividPayDate",
        "dividStockMarketDate", "dividCashPsBeforeTax", "dividCashPsAfterTax",
        "dividStocksPs", "dividCashStock", "dividReserveToStockPs",
    ]

    def __init__(self, error_code: str, error_msg: str,
                 rows: list[list[str]] | None = None) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = self.FIELDS
        self._rows = list(rows or [])

    def next(self) -> bool:
        if self._rows:
            self._current = self._rows.pop(0)
            return True
        return False

    def get_row_data(self) -> list[str]:
        return getattr(self, "_current", [])


def test_baostock_dividends_partial_year_failure_reports_error(monkeypatch) -> None:
    import app.core.adapters.baostock_adapter as module

    start_year = datetime.now().year - 1
    calls: list[int] = []

    def fake_dividends(code, year, yearType=None):
        calls.append(year)
        if year == start_year:
            return _DividendResult("1", "year source down")
        return _DividendResult("0", "ok", [[
            "600519", "", "", "2026-05-01", "", "2026-05-20", "2026-06-01",
            "2026-06-05", "", "0.5", "0.4", "0.0", "", "0.0",
        ]])

    monkeypatch.setattr(module, "bs", type("FakeBS", (), {
        "login": staticmethod(lambda: type("L", (), {"error_code": "0", "error_msg": "ok"})()),
        "logout": staticmethod(lambda: None),
        "query_dividend_data": staticmethod(fake_dividends),
    })())
    adapter = BaoStockAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(
        data_type="dividends", stock_codes=["600519"],
        start_date=f"{start_year}-01-01",
    ))

    assert [row["ex_date"] for row in result.data] == ["2026-06-01"]
    assert result.metadata.error is not None
    assert f"{start_year}" in result.metadata.error
    assert calls == [start_year, start_year + 1]


# ─── TDX：财报截断/失败披露 + 失败连接关闭 ─────────────────────────────────


def test_tdx_financial_truncation_and_file_failure_reported(monkeypatch) -> None:
    filenames = [f"gpcw2026{i:02d}31.zip" for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
    failing = "gpcw20261031.zip"

    class FakeFinancialClient:
        def get_financial_file_list(self) -> pd.DataFrame:
            return pd.DataFrame([
                {"filename": name, "filesize": 20000} for name in filenames
            ])

        def get_financial_records(self, full_path: str) -> pd.DataFrame:
            if full_path.endswith(failing):
                raise OSError("download failed")
            return pd.DataFrame([{
                "code": "600519",
                "report_date": int(full_path[-12:-4]),
                "fields": [0.0] * 80,
            }])

    @contextlib.contextmanager
    def fake_session():
        yield FakeFinancialClient()

    adapter = TDXAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_tdx_session", fake_session)
    result = adapter.fetch(FetchRequest(
        data_type="balance_sheet", stock_codes=["600519"],
    ))

    assert len(result.data) >= 7
    assert result.metadata.error is not None
    assert "截断" in result.metadata.error
    assert failing in result.metadata.error


def test_tdx_bars_pagination_truncation_reported(monkeypatch) -> None:
    import app.core.adapters.tdx_adapter as module

    class FakeBarsClient:
        def get_security_bars(self, market, code, category, start=0, count=800):
            return pd.DataFrame([
                {"date": pd.Timestamp("2026-08-05"), "open": 1.0, "high": 2.0,
                 "low": 0.5, "close": 1.5, "vol": 100.0, "amount": 200.0},
                {"date": pd.Timestamp("2026-08-04"), "open": 1.0, "high": 2.0,
                 "low": 0.5, "close": 1.5, "vol": 100.0, "amount": 200.0},
            ])

    monkeypatch.setattr(module, "_BARS_PAGE_SIZE", 2)
    monkeypatch.setattr(module, "_MAX_BARS_PAGES", 3)

    @contextlib.contextmanager
    def fake_bars_session(request):
        yield FakeBarsClient()

    adapter = TDXAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_bars_session", fake_bars_session)
    result = adapter.fetch(FetchRequest(
        data_type="price_daily", stock_codes=["600519"],
        start_date="2024-01-01", adjust="raw",
    ))

    assert result.data
    assert result.metadata.error is not None
    assert "分页达到上限" in result.metadata.error


def test_tdx_closes_failed_fallback_candidate(monkeypatch) -> None:
    import app.core.adapters.tdx_adapter as module

    instances: list[Any] = []

    class FakeTdxClient:
        @classmethod
        def from_best_host(cls, timeout=None, auto_reconnect=None):
            raise ConnectionError("no best host")

        def __init__(self, host: str = "", timeout: float = 8.0, auto_reconnect: bool = True) -> None:
            self.host = host
            self.closed = False
            instances.append(self)

        def connect(self) -> None:
            raise ConnectionError(f"{self.host} down")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(module, "TdxClient", FakeTdxClient)
    monkeypatch.setattr(module, "_KNOWN_BARS_HOSTS", ["host-1", "host-2"])
    adapter = TDXAdapter(rate_limit=0)

    with adapter._tdx_session() as client:
        assert client is None

    assert len(instances) == 2
    assert all(instance.closed for instance in instances)


# ─── Manager：partial 保留与熔断口径 ───────────────────────────────────────


class _FakeManagerAdapter:
    def __init__(self, result: FetchResult, *, close_calls: list[str] | None = None,
                 name: str = "fake") -> None:
        self.result = result
        self.close_calls = close_calls
        self.name = name
        self.closed = False

    def can_handle(self, request: FetchRequest) -> bool:
        return True

    def fetch(self, request: FetchRequest) -> FetchResult:
        return self.result

    def close(self) -> None:
        self.closed = True
        if self.close_calls is not None:
            self.close_calls.append(self.name)


def _manager_with(adapters: dict[str, Any]) -> AdapterManager:
    manager = AdapterManager()
    manager._initialized = True
    manager._adapters.update(adapters)
    return manager


def test_manager_keeps_first_partial_and_merges_followup_errors() -> None:
    partial = _result([{"trade_date": "2026-08-05"}], source="tencent", error="partial page")
    failure = _result([], source="cninfo", error="cninfo down")
    manager = _manager_with({
        "tencent": _FakeManagerAdapter(partial),
        "cninfo": _FakeManagerAdapter(failure),
    })

    result = manager.fetch_with_sources(
        FetchRequest(data_type="price_daily", stock_codes=["920000"]),
        ["tencent", "cninfo"],
    )

    assert result.data == partial.data
    assert result.metadata.error == "partial page; cninfo down"


def test_manager_circuit_counts_only_empty_error_results() -> None:
    partial = _result([{"trade_date": "2026-08-05"}], source="tencent", error="partial")
    empty_error = _result([], source="tencent", error="source down")

    partial_manager = _manager_with({"tencent": _FakeManagerAdapter(partial)})
    for _ in range(5):
        partial_manager.fetch_with_sources(
            FetchRequest(data_type="price_daily", stock_codes=["920000"]),
            ["tencent"],
        )
    state = partial_manager._circuit_breaker.get("tencent", {})
    assert state.get("failures", 0) == 0
    assert not state.get("tripped_until")

    error_manager = _manager_with({"tencent": _FakeManagerAdapter(empty_error)})
    for _ in range(5):
        error_manager.fetch_with_sources(
            FetchRequest(data_type="price_daily", stock_codes=["920000"]),
            ["tencent"],
        )
    state = error_manager._circuit_breaker["tencent"]
    assert state["failures"] == 5
    assert state["tripped_until"] is not None


def test_manager_close_resets_initialized_and_closes_adapters() -> None:
    close_calls: list[str] = []
    manager = _manager_with({
        "tencent": _FakeManagerAdapter(_result([]), close_calls=close_calls, name="tencent"),
    })

    manager.close()

    assert manager._initialized is False
    assert manager._adapters == {}
    assert close_calls == ["tencent"]


def test_tencent_close_clears_thread_local_session() -> None:
    adapter = TencentAdapter(rate_limit=0)
    first = adapter._session()
    assert getattr(adapter._session_local, "session", None) is first

    adapter.close()

    assert not hasattr(adapter._session_local, "session")
    assert adapter._sessions == []
    second = adapter._session()
    assert second is not first
    second.close()


# ─── DataInitializer：retry 语义与财务三表分支 ─────────────────────────────


def test_initializer_record_failure_preserves_retry_count(sqlite_store: SQLiteStore) -> None:
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.sqlite = sqlite_store
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO retry_list
               (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
               VALUES ('600519', 'balance_sheet', 'sina', 'old', 3, ?, '{}')""",
            [datetime.now(UTC).isoformat()],
        )

    initializer._record_failure("600519", "balance_sheet", "sina", "new error")

    rows = sqlite_store.query(
        """SELECT error, retry_count FROM retry_list
           WHERE stock_code='600519' AND data_type='balance_sheet' AND adapter='sina'"""
    )
    assert rows == [{"error": "new error", "retry_count": 3}]


class _FinancialTrioAdapter:
    def fetch(self, request: FetchRequest) -> FetchResult:
        if request.data_type == "balance_sheet":
            return _result([], source="sina", error="balance source down")
        if request.data_type == "income_statement":
            return _result([], source="sina")
        return _result(
            [{"stock_code": "600519", "report_date": "2026-06-30",
              "NETCASH_OPERATE": 1.0}],
            source="sina",
        )


def test_initializer_financial_branches_distinguish_error_and_source_empty(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('600519', '贵州茅台', 'SSE', true)"""
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = _FinancialTrioAdapter()
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store

    report = initializer._fetch_financial_statements()

    assert report["cash_flow"] == 1
    retries = sqlite_store.query(
        """SELECT data_type FROM retry_list WHERE stock_code='600519' ORDER BY data_type"""
    )
    assert retries == [{"data_type": "balance_sheet"}]
    missing = sqlite_store.query(
        """SELECT field_name, reason_code FROM missing_list
           WHERE stock_code='600519' ORDER BY field_name"""
    )
    assert missing == [{"field_name": "income_statement", "reason_code": "source_empty"}]
    cashflow_rows = duckdb_store.read_query(
        "SELECT report_date, cf_from_operating FROM cash_flow WHERE stock_code='600519'"
    )
    assert cashflow_rows[0]["cf_from_operating"] == pytest.approx(1.0)


# ─── schema：retry 去重与 v12 幂等 ─────────────────────────────────────────


def test_sqlite_schema_dedups_retry_rows_before_unique_index(database_paths) -> None:
    sqlite = SQLiteStore(paths=database_paths)
    init_sqlite_schema(sqlite)
    with sqlite.transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS uq_retry_list_request")
        for error in ("older", "newest"):
            conn.execute(
                """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                   VALUES ('600519', 'price_daily', 'tencent', ?, 0, ?, '{}')""",
                [error, datetime.now(UTC).isoformat()],
            )

    init_sqlite_schema(sqlite)

    rows = sqlite.query(
        """SELECT error FROM retry_list
           WHERE stock_code='600519' AND data_type='price_daily' AND adapter='tencent'"""
    )
    assert rows == [{"error": "newest"}]
    indexes = sqlite.query("PRAGMA index_list(retry_list)")
    assert any(row["name"] == "uq_retry_list_request" for row in indexes)


def test_duckdb_v12_funding_drop_only_when_version_not_applied(database_paths) -> None:
    duck = DuckDBStore(paths=database_paths)
    init_duckdb_schema(duck)

    def recreate_legacy_pk_with_row() -> None:
        with duck.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS funding_events")
            conn.execute(
                """CREATE TABLE funding_events (
                    stock_code VARCHAR NOT NULL,
                    event_type VARCHAR NOT NULL,
                    announce_date DATE,
                    list_date DATE,
                    issue_price DOUBLE,
                    issue_shares DOUBLE,
                    raise_funds DOUBLE,
                    raise_funds_net DOUBLE,
                    derived BOOLEAN,
                    source VARCHAR NOT NULL,
                    fetch_time TIMESTAMP NOT NULL,
                    raw_hash VARCHAR NOT NULL,
                    confidence VARCHAR NOT NULL,
                    batch_id VARCHAR NOT NULL,
                    PRIMARY KEY (stock_code, event_type, list_date)
                )"""
            )
            conn.execute(
                """INSERT INTO funding_events
                   (stock_code, event_type, list_date, source, fetch_time, raw_hash, confidence, batch_id)
                   VALUES ('000001', 'ipo', '1991-04-03', 'cninfo_funding',
                           CURRENT_TIMESTAMP, '0' * 64, 'missing', 'b1')"""
            )

    # 版本 12 未记录 + 旧主键 → 必须 DROP 重建
    recreate_legacy_pk_with_row()
    duck.write_query("DELETE FROM schema_migrations WHERE version = 12")
    init_duckdb_schema(duck)
    assert duck.read_query("SELECT COUNT(*) AS c FROM funding_events")[0]["c"] == 0
    has_pk = duck.read_query(
        """SELECT 1 FROM duckdb_constraints()
           WHERE table_name = 'funding_events' AND constraint_type = 'PRIMARY KEY'"""
    )
    assert has_pk == []

    # 版本 12 已记录后即使表再次出现旧主键，也不得再次 DROP（幂等且保数据）
    recreate_legacy_pk_with_row()
    init_duckdb_schema(duck)
    rows = duck.read_query("SELECT COUNT(*) AS c FROM funding_events")
    assert rows[0]["c"] == 1, "version=12 已执行后不得每次启动检查/执行 DROP"
    versions = {row["version"] for row in duck.read_query("SELECT version FROM schema_migrations")}
    assert 12 in versions
