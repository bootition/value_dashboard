"""融资事件 + 指数估值低频域回归（数据补全 2026-08-25；v21 扩展 2026-09-05）

覆盖：
- eastmoney_f10 placement_funding：zfmx/pgmx 解析、derived 推算、北交所跳过、空→missing
- cninfo_funding adapter：IPO 要素解析、单位归一（万股→股、万元→元）、空→missing
- index_valuation adapter：乐咕 PE/PB 合并、中证解析、申万日报映射
- FundingUpdater：单股事务原子替换 / 失败保留旧值 / retry/missing 去重
- IndexValuationUpdater：主源+交叉源双写、申万行业组更新、主源失败保旧值
- schema v11/v21 新表新列 + readiness 完全不变
- manager 注册与独立限速（含 ths/sws）
- ThsAdapter：ETF 快照/日线/资料/跟踪分位映射与错误分类
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.cninfo_funding_adapter import CNINFOFundingAdapter
from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
from app.core.adapters.index_valuation_adapter import (
    CSIndexIndexAdapter,
    LeguleguIndexAdapter,
    SwsIndexAdapter,
)
from app.core.adapters.ths_adapter import THS_API_KEY_ENV, ThsAdapter
from app.core.funding import FundingUpdater
from app.core.index_valuation import IndexValuationUpdater
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

# ─── 东财 F10 BonusFinancing fixture（基于 2026-08-25 真实响应精简） ────

BONUS_PAYLOAD = {
    "fhyx": [],
    "lnfhrz": [],
    "zfmx": [
        {"SECUCODE": "000725.SZ", "SECURITY_CODE": "000725", "SECURITY_NAME_ABBR": "京东方A",
         "NOTICE_DATE": "2021-08-19 00:00:00", "ISSUE_NUM": 3650377019,
         "TOTAL_RAISE_FUNDS": None, "ISSUE_PRICE": 5.57, "EVENT_EXPLAIN": None},
        {"SECUCODE": "000725.SZ", "SECURITY_CODE": "000725", "SECURITY_NAME_ABBR": "京东方A",
         "NOTICE_DATE": "2014-04-04 00:00:00", "ISSUE_NUM": 21768095233,
         "TOTAL_RAISE_FUNDS": None, "ISSUE_PRICE": 2.1, "EVENT_EXPLAIN": None},
    ],
    "pgmx": [
        {"SECUCODE": "601398.SH", "SECURITY_CODE": "601398", "SECURITY_NAME_ABBR": "工商银行",
         "NOTICE_DATE": "2010-11-11 00:00:00", "ISSUE_NUM": 11262153213,
         "TOTAL_RAISE_FUNDS": 33673838107, "ISSUE_PRICE": 2.99,
         "EQUITY_RECORD_DATE": "2010-11-15 00:00:00", "EX_DIVIDEND_DATEE": "2010-11-24 00:00:00",
         "EVENT_EXPLAIN": "每10股配0.45股"},
    ],
}

MISSING_PAYLOAD = {"status": 0, "message": "error"}


def _mock_f10_client(payload=None, recorded: list | None = None) -> httpx.Client:
    payload = BONUS_PAYLOAD if payload is None else payload

    def handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(str(request.url))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _result(data: list[dict], *, source: str = "eastmoney_f10",
            error: str | None = None) -> FetchResult:
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


def _seed_stock(duck: DuckDBStore, code: str = "000725") -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES (?, ?, 'SZSE', true)",
        [code, code],
    )


# ─── adapter：eastmoney_f10 placement_funding ─────────────────────────

def test_placement_funding_parses_zfmx_and_pgmx() -> None:
    client = _mock_f10_client()
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type="placement_funding", stock_codes=["000725"]))

    assert result.metadata.error is None
    assert result.metadata.confidence == "approximate"
    placements = [row for row in result.data if row["event_type"] == "a_placement"]
    rights = [row for row in result.data if row["event_type"] == "rights"]
    assert len(placements) == 2
    assert len(rights) == 1
    # zfmx TOTAL_RAISE_FUNDS=null → derived 推算 price×shares
    assert placements[0]["raise_funds"] == pytest.approx(3650377019 * 5.57)
    assert placements[0]["derived"] is True
    assert placements[0]["issue_shares"] == 3650377019
    assert placements[0]["issue_price"] == pytest.approx(5.57)
    assert placements[0]["list_date"] == "2021-08-19"
    # pgmx 使用东财完整募资额，derived=False
    assert rights[0]["raise_funds"] == 33673838107
    assert rights[0]["derived"] is False
    assert rights[0]["list_date"] == "2010-11-24"
    assert rights[0]["issue_price"] == pytest.approx(2.99)
    client.close()


def test_placement_funding_derived_false_when_price_missing() -> None:
    payload = {
        "zfmx": [{"SECURITY_CODE": "000725", "NOTICE_DATE": "2021-08-19 00:00:00",
                  "ISSUE_NUM": 1000, "TOTAL_RAISE_FUNDS": None, "ISSUE_PRICE": None,
                  "EVENT_EXPLAIN": None}],
        "pgmx": [],
    }
    client = _mock_f10_client(payload)
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type="placement_funding", stock_codes=["000725"]))
    assert result.data[0]["raise_funds"] is None, "price/shares 任一缺失不得伪造募资额"
    assert result.data[0]["derived"] is False
    client.close()


def test_placement_funding_bse_skipped_without_request(recorded: list | None = None) -> None:
    recorded = [] if recorded is None else recorded
    client = _mock_f10_client(recorded=recorded)
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type="placement_funding", stock_codes=["832566"]))
    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing", "北交所无东财交叉源=合法缺失"
    client.close()


def test_placement_funding_missing_or_malformed_returns_missing() -> None:
    for payload in (MISSING_PAYLOAD, None):
        client = _mock_f10_client(payload)
        adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
        result = adapter.fetch(FetchRequest(data_type="placement_funding", stock_codes=["999999"]))
        assert result.data == []
        assert result.metadata.error is None
        assert result.metadata.confidence == "missing"
        client.close()


# ─── adapter：cninfo_funding IPO ──────────────────────────────────────

class _FakeAK:
    """离线 mock akshare.stock_ipo_summary_cninfo（600030 真实数据形状）。"""

    def stock_ipo_summary_cninfo(self, symbol: str) -> object:
        import pandas as pd

        if symbol == "999999":
            return pd.DataFrame()
        return pd.DataFrame([{
            "股票代码": "600030", "招股公告日期": "2002-12-13", "中签率公告日": None,
            "每股面值": 1.0, "总发行数量": 40000.0, "发行前每股净资产": 1.67,
            "摊薄发行市盈率": 15.0, "募集资金净额": 175967.3375, "上网发行日期": "2002-12-17",
            "上市日期": "2003-01-06", "发行价格": 4.5, "发行费用总额": 4032.6625,
            "发行后每股净资产": 2.11, "上网发行中签率": 0.282, "主承销商": "广发证券股份有限公司",
        }])


def test_cninfo_funding_adapter_parses_ipo(monkeypatch) -> None:
    import app.core.adapters.cninfo_funding_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAK())
    adapter = CNINFOFundingAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="ipo_funding", stock_codes=["600030"]))

    assert result.metadata.error is None
    assert len(result.data) == 1
    row = result.data[0]
    assert row["event_type"] == "ipo"
    assert row["list_date"] == "2003-01-06"
    assert row["announce_date"] == "2002-12-13"
    assert row["issue_price"] == pytest.approx(4.5)
    # 单位归一：40000 万股 → 4 亿股；175967.3375 万元 → 17.6 亿元
    assert row["issue_shares"] == pytest.approx(40000 * 1e4)
    assert row["raise_funds_net"] == pytest.approx(175967.3375 * 1e4)
    assert row["raise_funds"] is None, "CNINFO 仅净额，总额不伪造"
    assert row["derived"] is False


def test_cninfo_funding_adapter_empty_is_missing(monkeypatch) -> None:
    import app.core.adapters.cninfo_funding_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAK())
    adapter = CNINFOFundingAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="ipo_funding", stock_codes=["999999"]))
    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"


def test_cninfo_funding_adapter_akshare_index_error_is_legal_missing(monkeypatch) -> None:
    """akshare 对无发行记录股票（如部分北交所）抛 IndexError → 必须按合法缺失处理，
    不得触发熔断/登记 retry（数据补全 2026-08-25 实测 832566/430047）。"""

    class _IndexErrorAK:
        def stock_ipo_summary_cninfo(self, symbol: str) -> object:
            raise IndexError("list index out of range")

    import app.core.adapters.cninfo_funding_adapter as mod

    monkeypatch.setattr(mod, "ak", _IndexErrorAK())
    adapter = CNINFOFundingAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="ipo_funding", stock_codes=["832566"]))
    assert result.data == []
    assert result.metadata.error is None, "源无记录必须 error=None（不触发熔断）"
    assert result.metadata.confidence == "missing"


# ─── adapter：index_valuation ─────────────────────────────────────────

class _FakeAKIndex:
    def stock_index_pe_lg(self, symbol: str) -> object:
        import pandas as pd

        return pd.DataFrame([
            {"日期": "2026-08-25", "指数": 3800.0, "等权静态市盈率": 20.0,
             "静态市盈率": 16.0, "静态市盈率中位数": 18.0, "等权滚动市盈率": 19.0,
             "滚动市盈率": 14.57, "滚动市盈率中位数": 16.0},
            {"日期": "2026-08-24", "指数": 3790.0, "等权静态市盈率": 20.1,
             "静态市盈率": 16.1, "静态市盈率中位数": 18.1, "等权滚动市盈率": 19.1,
             "滚动市盈率": 14.59, "滚动市盈率中位数": 16.1},
        ])

    def stock_index_pb_lg(self, symbol: str) -> object:
        import pandas as pd

        return pd.DataFrame([
            {"日期": "2026-08-25", "指数": 3800.0, "市净率": 1.55,
             "加权市净率": 1.60, "市净率中位数": 1.51},
            {"日期": "2026-08-24", "指数": 3790.0, "市净率": 1.54,
             "加权市净率": 1.59, "市净率中位数": 1.50},
        ])

    def index_analysis_daily_sw(self, symbol: str, start_date: str, end_date: str) -> object:
        import pandas as pd

        return pd.DataFrame([
            {"指数代码": "801010.SI", "指数名称": "农林牧渔", "交易日期": "2026-08-25",
             "收盘指数": 2500.0, "成交额": 100.0, "涨跌幅": 1.2, "换手率": 2.1,
             "市盈率": 33.9, "市净率": 2.04, "均价": 10.0, "成交额占比": 0.5,
             "流通市值": 10000.0, "平均流通市值": 100.0, "股息率": 2.1},
        ])

    def stock_zh_index_value_csindex(self, symbol: str) -> object:
        import pandas as pd

        return pd.DataFrame([
            {"日期": "2026-08-25", "指数代码": "300", "指数中文全称": "沪深300指数",
             "指数中文简称": "沪深300", "指数英文全称": "CSI 300 Index", "指数英文简称": "CSI 300",
             "市盈率1": 14.57, "市盈率2": 16.89, "股息率1": 2.55, "股息率2": 2.25},
        ])


def test_legulegu_adapter_maps_pe_ttm(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAKIndex())
    adapter = LeguleguIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="index_valuation", stock_codes=["000300"]))
    assert result.metadata.error is None
    assert len(result.data) == 2
    assert result.data[0]["index_code"] == "000300"
    assert result.data[0]["pe_ttm"] == pytest.approx(14.57)
    assert result.data[0]["pe_metric"] == "ttm"
    assert result.data[0]["trade_date"] == "2026-08-25"
    # v21：乐咕 PE 与 PB 按日期合并
    assert result.data[0]["pb"] == pytest.approx(1.55)


def test_legulegu_pb_failure_is_nonfatal(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    class FakeNoPB:
        def stock_index_pe_lg(self, symbol: str) -> object:
            import pandas as pd
            return pd.DataFrame([
                {"日期": "2026-08-25", "指数": 3800.0, "静态市盈率": 16.0,
                 "静态市盈率中位数": 18.0, "滚动市盈率": 14.57, "滚动市盈率中位数": 16.0},
            ])

    monkeypatch.setattr(mod, "ak", FakeNoPB())
    adapter = LeguleguIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="index_valuation", stock_codes=["000300"]))
    assert result.metadata.error is None
    assert len(result.data) == 1
    assert result.data[0]["pb"] is None, "PB 请求失败不得阻断 PE 主链"


def test_sws_adapter_maps_industry_valuation(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAKIndex())
    adapter = SwsIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(
        data_type="index_valuation", start_date="2026-08-25", end_date="2026-08-25",
    ))
    assert result.metadata.error is None
    assert len(result.data) == 1
    row = result.data[0]
    assert row["index_code"] == "SW801010"
    assert row["pe_metric"] == "sws_daily"
    assert row["pe_ttm"] == pytest.approx(33.9)
    assert row["pb"] == pytest.approx(2.04)
    assert row["div_yield"] == pytest.approx(2.1)
    assert result.metadata.source == "sws"


def test_sws_adapter_non_trading_day_is_missing(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    class FakeEmpty:
        def index_analysis_daily_sw(self, symbol: str, start_date: str, end_date: str) -> object:
            raise KeyError("交易日期")

    monkeypatch.setattr(mod, "ak", FakeEmpty())
    adapter = SwsIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(
        data_type="index_valuation", start_date="2026-09-06", end_date="2026-09-06",
    ))
    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"


def test_csindex_adapter_maps_official_fields(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAKIndex())
    adapter = CSIndexIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="index_valuation", stock_codes=["000300"]))
    assert result.data[0]["pe_ttm"] == pytest.approx(14.57)
    assert result.data[0]["pe_metric"] == "ttm"
    assert result.data[0]["div_yield"] == pytest.approx(2.55)
    assert result.data[0]["pb"] is None


def test_csindex_unsupported_sz_index_is_missing(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAKIndex())
    adapter = CSIndexIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="index_valuation", stock_codes=["399673"]))
    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"


def test_legulegu_unsupported_index_is_missing(monkeypatch) -> None:
    import app.core.adapters.index_valuation_adapter as mod

    monkeypatch.setattr(mod, "ak", _FakeAKIndex())
    adapter = LeguleguIndexAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="index_valuation", stock_codes=["999999"]))
    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"


# ─── FundingUpdater：原子替换 / 保旧值 / retry / missing ───────────────

class _FakeFundingAdapter:
    def __init__(self, *, ipo=None, placement=None, error: str | None = None) -> None:
        self.ipo = ipo
        self.placement = placement
        self.error = error
        self.calls: list[str] = []

    def fetch(self, request: FetchRequest) -> FetchResult:
        self.calls.append(request.data_type)
        if self.error:
            return _result([], error=self.error)
        data = self.ipo if request.data_type == "ipo_funding" else self.placement
        if data is None:
            return _result([], source="eastmoney_f10" if request.data_type == "placement_funding" else "cninfo_funding")
        return _result(data, source="cninfo_funding" if request.data_type == "ipo_funding" else "eastmoney_f10")


def _seed_funding_rows(duck: DuckDBStore, code: str = "000725") -> None:
    duck.write_query(
        """INSERT INTO funding_events
           (stock_code, event_type, list_date, issue_price, issue_shares,
            raise_funds, derived, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES (?, 'a_placement', '2010-01-01', 1.0, 1000.0, 1000.0, false,
                   'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'old-batch')""",
        [code, "0" * 64],
    )


def test_funding_updater_atomic_replace(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    _seed_stock(duckdb_store)
    _seed_funding_rows(duckdb_store)
    ipo = [{
        "stock_code": "000725", "event_type": "ipo", "announce_date": "2003-01-01",
        "list_date": "2003-02-01", "issue_price": 2.0, "issue_shares": 100000000.0,
        "raise_funds": None, "raise_funds_net": 190000000.0, "derived": False,
    }]
    placement = [{
        "stock_code": "000725", "event_type": "a_placement", "announce_date": "2021-08-19",
        "list_date": "2021-08-19", "issue_price": 5.57, "issue_shares": 3650377019.0,
        "raise_funds": 3650377019.0 * 5.57, "raise_funds_net": None, "derived": True,
    }]
    updater = FundingUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter=_FakeFundingAdapter(ipo=ipo, placement=placement),
    )

    report = updater.update_stock("000725")

    assert report["status"] == "success"
    assert report["event_rows"] == 2
    rows = duckdb_store.read_query(
        "SELECT event_type, list_date, source FROM funding_events WHERE stock_code='000725' ORDER BY list_date"
    )
    assert len(rows) == 2
    assert {str(r["event_type"]) for r in rows} == {"ipo", "a_placement"}
    assert {r["source"] for r in rows} == {"cninfo_funding", "eastmoney_f10"}
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM missing_list WHERE stock_code='000725' AND resolved_at IS NULL"
    )[0]["c"] == 0


def test_funding_updater_failure_preserves_old_and_records_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_funding_rows(duckdb_store)
    updater = FundingUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter=_FakeFundingAdapter(error="source down"),
    )

    report = updater.update_stock("000725")

    assert report["status"] == "failed"
    assert report["retained"] is True
    rows = duckdb_store.read_query(
        "SELECT event_type FROM funding_events WHERE stock_code='000725'"
    )
    assert rows[0]["event_type"] == "a_placement", "失败必须保留旧值"
    retries = sqlite_store.query(
        "SELECT data_type, adapter FROM retry_list WHERE stock_code='000725'"
    )
    assert {r["data_type"] for r in retries} == {"ipo_funding", "placement_funding"}


def test_funding_updater_missing_records_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "832566")
    updater = FundingUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter=_FakeFundingAdapter(ipo=None, placement=None),  # 北交所无源 → 双 missing
    )
    report = updater.update_stock("832566")
    assert report["status"] == "success"
    assert report["event_rows"] == 0
    missing = sqlite_store.query(
        "SELECT field_name FROM missing_list WHERE stock_code='832566' AND resolved_at IS NULL"
    )
    assert {m["field_name"] for m in missing} == {"ipo_funding", "placement_funding"}


def test_funding_update_all_skips_when_covered(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_funding_rows(duckdb_store)
    updater = FundingUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=_FakeFundingAdapter(),
    )
    report = updater.update_all(max_stocks=5)
    assert report["status"] == "skipped", "全部覆盖时不得重复抓取"
    assert report["reason"] == "all_funding_covered"


# ─── IndexValuationUpdater：双源并存 / 主源失败 ───────────────────────

class _FakeIndexAdapter:
    def __init__(self, rows, error: str | None = None, source: str = "legulegu") -> None:
        self.rows = rows
        self.error = error
        self.source = source

    def fetch(self, request: FetchRequest) -> FetchResult:
        if self.error:
            return _result([], source=self.source, error=self.error)
        return _result(self.rows, source=self.source)


def test_index_valuation_updater_writes_both_sources(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    primary_rows = [{
        "index_code": "000300", "trade_date": "2026-08-25", "pe_ttm": 14.57,
        "pe_metric": "ttm", "pb": 1.55, "div_yield": None, "extra": None,
    }]
    cross_rows = [{
        "index_code": "000300", "trade_date": "2026-08-25", "pe_ttm": 14.57,
        "pe_metric": "ttm", "pb": None, "div_yield": 2.55, "extra": None,
    }]
    updater = IndexValuationUpdater(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(updater, "_primary", _FakeIndexAdapter(primary_rows, source="legulegu"))
    monkeypatch.setattr(updater, "_cross", _FakeIndexAdapter(cross_rows, source="csindex"))

    report = updater.update_daily()

    assert report["status"] == "success"
    assert report["indexes"]["000300"]["primary_rows"] == 1
    assert report["indexes"]["000300"]["cross_rows"] == 1
    rows = duckdb_store.read_query(
        "SELECT source, pe_ttm, pe_metric, pb, div_yield FROM index_valuation "
        "WHERE index_code='000300' ORDER BY source"
    )
    assert {r["source"] for r in rows} == {"legulegu", "csindex"}
    by_source = {r["source"]: r for r in rows}
    assert by_source["legulegu"]["pe_ttm"] == pytest.approx(14.57)
    assert by_source["legulegu"]["pe_metric"] == "ttm"
    assert by_source["legulegu"]["pb"] == pytest.approx(1.55)
    assert by_source["csindex"]["div_yield"] == pytest.approx(2.55)


def test_index_valuation_updater_writes_sws_industries(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    sws_rows = [{
        "index_code": "SW801010", "trade_date": "2026-08-25", "pe_ttm": 33.9,
        "pe_metric": "sws_daily", "pb": 2.04, "div_yield": 2.1,
        "extra": '{"index_close":2500.0}',
    }]
    updater = IndexValuationUpdater(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(updater, "_sws", _FakeIndexAdapter(sws_rows, source="sws"))

    report = updater.update_sw_industries(start_date="2026-08-25", end_date="2026-08-25")

    assert report["status"] == "success"
    assert report["rows"] == 1
    rows = duckdb_store.read_query(
        "SELECT source, index_code, pe_metric, pb FROM index_valuation WHERE index_code='SW801010'"
    )
    assert rows[0]["source"] == "sws"
    assert rows[0]["pe_metric"] == "sws_daily"
    assert rows[0]["pb"] == pytest.approx(2.04)


def test_index_valuation_primary_failure_records_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    updater = IndexValuationUpdater(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(updater, "_primary", _FakeIndexAdapter([], error="boom"))
    monkeypatch.setattr(updater, "_cross", _FakeIndexAdapter([]))

    report = updater.update_daily()

    assert report["status"] == "failed"
    assert "primary_error" in report["indexes"]["000300"]
    retries = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM retry_list WHERE data_type='index_valuation'"
    )
    assert retries[0]["c"] == 1


def test_index_valuation_refresh_if_due_throttles_same_day(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    """日节流：当日已刷新则 skip，不再发网络请求（数据补全 2026-08-25）。"""
    updater = IndexValuationUpdater(duck=duckdb_store, sqlite=sqlite_store)
    calls: list[list] = []

    def fake_update_daily(codes=None):
        calls.append(codes or [])
        return {"status": "success", "indexes": {}}

    monkeypatch.setattr(updater, "update_daily", fake_update_daily)
    monkeypatch.setattr(updater, "_sws", _FakeIndexAdapter([], source="sws"))

    first = updater.refresh_if_due()
    second = updater.refresh_if_due()

    assert first["status"] == "success"
    assert second["status"] == "skipped"
    assert second["reason"] == "refreshed_today"
    assert len(calls) == 1, "当日第二次 refresh 不得再发请求"
    assert len(calls[0]) == 12, "refresh 必须覆盖乐咕全部 12 个宽基/红利指数"


def test_index_valuation_refresh_if_due_records_marker(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    updater = IndexValuationUpdater(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(updater, "_primary", _FakeIndexAdapter([], source="legulegu"))
    monkeypatch.setattr(updater, "_cross", _FakeIndexAdapter([], source="csindex"))
    monkeypatch.setattr(updater, "_sws", _FakeIndexAdapter([], source="sws"))

    report = updater.refresh_if_due()

    assert report["status"] == "success"
    rows = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = 'index_valuation_last_refresh'"
    )
    assert rows, "刷新成功后必须记录 marker"


# ─── schema v11 + readiness 不变 ──────────────────────────────────────

def test_schema_v11_creates_new_tables(database_paths) -> None:
    from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema

    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)

    tables = {row["table_name"] for row in duck.read_query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    )}
    assert "funding_events" in tables
    assert "index_valuation" in tables
    columns = {row["column_name"] for row in duck.read_query(
        "SELECT column_name FROM information_schema.columns WHERE table_name='index_valuation'"
    )}
    assert "pe_metric" in columns, "schema v21 必须提供 pe_metric 口径列"
    assert "extra" in columns, "schema v21 必须提供 extra 附加字段列"
    versions = {row["version"] for row in duck.read_query(
        "SELECT version FROM schema_migrations"
    )}
    assert 21 in versions


def test_readiness_unchanged_by_new_domains(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from app.core import data_quality as module

    source = inspect.getsource(module)
    assert "funding_events" not in source
    assert "index_valuation" not in source


# ─── manager 注册与限速 ───────────────────────────────────────────────

def test_manager_registers_new_adapters() -> None:
    from app.core.adapters import manager as manager_module
    from app.core.adapters.manager import AdapterManager

    assert "cninfo_funding" in manager_module.KNOWN_ADAPTERS
    assert "legulegu" in manager_module.KNOWN_ADAPTERS
    assert "csindex" in manager_module.KNOWN_ADAPTERS
    assert "ths" in manager_module.KNOWN_ADAPTERS
    assert "sws" in manager_module.KNOWN_ADAPTERS
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["ipo_funding"] == ["cninfo_funding"]
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["placement_funding"] == ["eastmoney_f10"]
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["index_valuation"] == ["legulegu"]
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["etf_daily"] == ["ths"]
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["etf_snapshot"] == ["ths"]
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["cninfo_funding"] >= 1.5
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["eastmoney_f10"] >= 0.5
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["ths"] >= 0.5
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["sws"] >= 0.5

    adapter_manager = AdapterManager()
    adapter_manager._ensure_initialized()
    assert adapter_manager.get_adapter("cninfo_funding") is not None
    assert adapter_manager.get_adapter("legulegu") is not None
    assert adapter_manager.get_adapter("csindex") is not None
    assert adapter_manager.get_adapter("sws") is not None
    assert adapter_manager.get_adapter("ths") is not None
    ths = adapter_manager.get_adapter("ths")
    assert "etf_daily" in ths.supported_data_types
    assert "etf_track_percentile" in ths.supported_data_types
    f10 = adapter_manager.get_adapter("eastmoney_f10")
    assert "placement_funding" in f10.supported_data_types


# ─── ThsAdapter：同花顺官方 Financial-API（2026-09-05） ───────────────

class _FakeThs:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __call__(self, path: str, api_key: str, params: dict) -> dict:
        return self.payload


def _ths_payload(item: dict | None, *, code: int = 0) -> dict:
    return {"code": code, "message": "success" if code == 0 else "boom",
            "request_id": "x", "data": {"item": [item] if item else []}}


def test_ths_adapter_etf_snapshot_mapping(monkeypatch) -> None:
    monkeypatch.setenv(THS_API_KEY_ENV, "test-key")
    adapter = ThsAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_get", _FakeThs(_ths_payload({
        "thscode": "510300.SH", "ticker": "510300", "last_price": 4.616,
        "prev_price": 4.621, "volume": 841465540, "turnover": 3905673000,
    })))
    result = adapter.fetch(FetchRequest(data_type="etf_snapshot", stock_codes=["510300"]))
    assert result.metadata.error is None
    assert result.data[0]["thscode"] == "510300.SH", "无后缀代码应自动补 SH"
    assert result.data[0]["last_price"] == pytest.approx(4.616)


def test_ths_adapter_etf_track_percentile_mapping(monkeypatch) -> None:
    monkeypatch.setenv(THS_API_KEY_ENV, "test-key")
    adapter = ThsAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_get", _FakeThs(_ths_payload({
        "date_ms": 1753977600000, "rsi_pct": 58.36, "donchian_channel": 0.874,
        "track_index_pe_ttm_five_year_percentile": 69.11,
    })))
    result = adapter.fetch(FetchRequest(
        data_type="etf_track_percentile", stock_codes=["510300.SH"],
        start_date="2025-08-01", end_date="2025-08-02",
    ))
    assert result.metadata.error is None
    row = result.data[0]
    assert row["trade_date"] == "2025-08-01"
    assert row["track_index_pe_ttm_five_year_percentile"] == pytest.approx(69.11)


def test_ths_adapter_business_error_is_error_and_3004_is_missing(monkeypatch) -> None:
    monkeypatch.setenv(THS_API_KEY_ENV, "test-key")
    adapter = ThsAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_get", _FakeThs(
        {"code": 2001, "message": "bad key", "request_id": "x", "data": None}
    ))
    result = adapter.fetch(FetchRequest(data_type="etf_snapshot", stock_codes=["510300.SH"]))
    assert result.data == []
    assert result.metadata.error is not None
    assert "2001" in result.metadata.error

    monkeypatch.setattr(adapter, "_get", _FakeThs(
        {"code": 3004, "message": "unsupported leaf", "request_id": "x", "data": None}
    ))
    result = adapter.fetch(FetchRequest(data_type="etf_snapshot", stock_codes=["510300.SH"]))
    assert result.data == []
    assert result.metadata.error is None, "3004 是不支持类型的合法缺失"
    assert result.metadata.confidence == "missing"


def test_ths_adapter_missing_key_is_error_without_network(monkeypatch) -> None:
    monkeypatch.delenv(THS_API_KEY_ENV, raising=False)
    adapter = ThsAdapter(rate_limit=0)
    called: list[str] = []
    monkeypatch.setattr(adapter, "_get", _FakeThs({"code": 0}))
    result = adapter.fetch(FetchRequest(data_type="etf_snapshot", stock_codes=["510300.SH"]))
    assert called == []
    assert result.metadata.error is not None
    assert "未设置" in result.metadata.error
