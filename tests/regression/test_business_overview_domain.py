"""P2 业务概览独立低频域回归（reports/67, reports/68）

覆盖：
- 东财 F10 adapter：真实响应解析、代码映射、空/畸形→missing、独立限速、session 注入
- 单股事务原子替换：成功发布 / 失败保留旧值 / missing 保留旧值
- retry/missing 去重与解决
- schema migration v8 建表 + SQLite missing 去重索引
- readiness 完全不变（无引用、无影响）
- /business-overview API：404 与 missing 语义、构成、历史与溯源
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
from app.core.business import BusinessOverviewUpdater
from app.core.data_quality import minimum_data_readiness
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.stock_detail import router as stock_detail_router

# ─── 东财 F10 fixture（基于 2026-08-10 真实响应结构精简） ─────────────

SURVEY_PAYLOAD = {
    "jbzl": [{
        "SECUCODE": "600519.SH",
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "ORG_NAME": "贵州茅台酒股份有限公司",
        "ORG_PROFILE": "贵州茅台酒股份有限公司主营茅台酒及系列酒的生产与销售。",
        "BUSINESS_SCOPE": "茅台酒及系列酒的生产与销售；食品、饮料、包装材料。",
        "EMP_NUM": 34992,
        "INDUSTRYCSRC1": "制造业-酒、饮料和精制茶制造业",
        "TRADE_MARKET": "上海证券交易所",
    }],
    "fxxg": [],
}

ANALYSIS_PAYLOAD = {
    "zyfw": [{"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
              "BUSINESS_SCOPE": "茅台酒及系列酒的生产与销售。"}],
    "zygcfx": [
        {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
         "REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "1", "ITEM_NAME": "白酒",
         "MAIN_BUSINESS_INCOME": 168774585187.65, "MBI_RATIO": 0.999624,
         "MAIN_BUSINESS_COST": 14805900139.59, "GROSS_RPOFIT_RATIO": 0.912274, "RANK": 1},
        {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
         "REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "1", "ITEM_NAME": "其他(补充)",
         "MAIN_BUSINESS_INCOME": 63517327.14, "MBI_RATIO": 0.000376, "RANK": 2},
        {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
         "REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "茅台酒",
         "MAIN_BUSINESS_INCOME": 146499906480.49, "MBI_RATIO": 0.867695, "RANK": 1},
        {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
         "REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "其他系列酒",
         "MAIN_BUSINESS_INCOME": 22274678707.16, "MBI_RATIO": 0.131929, "RANK": 2},
        {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
         "REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "3", "ITEM_NAME": "国内",
         "MAIN_BUSINESS_INCOME": 165390000000.0, "MBI_RATIO": 0.980000, "RANK": 1},
        {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
         "REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "3", "ITEM_NAME": "国外",
         "MAIN_BUSINESS_INCOME": 3376000000.0, "MBI_RATIO": 0.020000, "RANK": 2},
    ],
    "jyps": [],
}

MISSING_PAYLOAD = {"status": 0, "message": "error"}


def _mock_client(*, survey=None, analysis=None, recorded: list | None = None) -> httpx.Client:
    """构建注入 transport 的 httpx.Client（离线 fixture，不联网）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(str(request.url))
        if "CompanySurvey" in str(request.url):
            payload = survey if survey is not None else SURVEY_PAYLOAD
        else:
            payload = analysis if analysis is not None else ANALYSIS_PAYLOAD
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _result(
    data: list[dict],
    *,
    error: str | None = None,
    raw: bytes | None = None,
) -> FetchResult:
    raw = raw or json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source="eastmoney_f10",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw).hexdigest(),
            confidence="approximate" if error is None else "missing",
            error=error,
        ),
        raw_response=raw,
    )


def _seed_stock(duck: DuckDBStore, code: str = "600519") -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES (?, ?, 'SSE', true)",
        [code, code],
    )


# ─── adapter：解析 / 代码映射 / 限速 / 注入 / 缺失 ────────────────────


def test_eastmoney_f10_adapter_parses_company_profile() -> None:
    client = _mock_client()
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type="company_profile", stock_codes=["600519"]))

    assert result.metadata.error is None
    assert result.metadata.confidence == "approximate"
    assert result.metadata.source == "eastmoney_f10"
    assert len(result.metadata.raw_response_hash) == 64
    assert result.data == [{
        "stock_code": "600519", "code": "600519", "name": "贵州茅台",
        "org_name": "贵州茅台酒股份有限公司",
        "profile": "贵州茅台酒股份有限公司主营茅台酒及系列酒的生产与销售。",
        "scope": "茅台酒及系列酒的生产与销售；食品、饮料、包装材料。",
        "employee_num": 34992,
        "csrc_industry": "制造业-酒、饮料和精制茶制造业",
        "trade_market": "上海证券交易所",
    }]


def test_eastmoney_f10_adapter_parses_business_breakdown() -> None:
    client = _mock_client()
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type="business_breakdown", stock_codes=["600519"]))

    assert result.metadata.error is None
    assert len(result.data) == 6
    product = [row for row in result.data if row["type"] == 1]
    industry = [row for row in result.data if row["type"] == 2]
    region = [row for row in result.data if row["type"] == 3]
    assert len(product) == 2 and len(industry) == 2 and len(region) == 2
    # MBI_RATIO 小数 → 百分比
    assert product[0] == {
        "stock_code": "600519", "report_date": "2025-12-31", "type": 1,
        "item_name": "白酒", "amount": 168774585187.65,
        "ratio": pytest.approx(99.9624), "rank": 1,
    }
    assert region[1]["item_name"] == "国外"
    assert region[1]["ratio"] == pytest.approx(2.0)
    assert {row["report_date"] for row in result.data} == {"2025-12-31"}


def test_eastmoney_f10_adapter_maps_exchange_codes(recorded: list | None = None) -> None:
    recorded = [] if recorded is None else recorded
    client = _mock_client(recorded=recorded)
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)

    for code, expected in (("600519", "SH600519"), ("000001", "SZ000001"),
                           ("300750", "SZ300750"), ("920000", "BJ920000")):
        recorded.clear()
        adapter.fetch(FetchRequest(data_type="company_profile", stock_codes=[code]))
        assert f"code={expected}" in recorded[0], recorded
    recorded.clear()
    client.close()


def test_eastmoney_f10_adapter_accepts_plain_or_prefixed_codes() -> None:
    for code in ("600519", "SH600519", "600519.SH"):
        recorded: list[str] = []
        client = _mock_client(recorded=recorded)
        adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
        result = adapter.fetch(FetchRequest(data_type="company_profile", stock_codes=[code]))
        assert result.metadata.error is None
        assert result.data[0]["code"] == "600519"
        client.close()


def test_eastmoney_f10_adapter_default_rate_limit_at_least_half_second() -> None:
    adapter = EastMoneyF10Adapter()
    assert adapter.rate_limit_interval >= 0.5, "独立实例限速必须至少 0.5s（不高于 2 req/s）"
    assert adapter.name == "eastmoney_f10"
    # 2026-08-25 数据补全：F10 适配器扩展 placement_funding（增发/配股）
    assert adapter.supported_data_types == {
        "company_profile", "business_breakdown", "placement_funding",
    }


def test_eastmoney_f10_adapter_uses_injected_session() -> None:
    client = _mock_client()
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    assert adapter.client is client
    adapter.fetch(FetchRequest(data_type="company_profile", stock_codes=["600519"]))
    assert adapter.client is client, "注入的 session 必须被复用，不得自建"
    adapter.close()  # 不应关闭注入的 session
    client.close()


@pytest.mark.parametrize("data_type", ["company_profile", "business_breakdown"])
def test_eastmoney_f10_adapter_missing_payload_returns_missing(data_type: str) -> None:
    client = _mock_client(survey=MISSING_PAYLOAD, analysis=MISSING_PAYLOAD)
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type=data_type, stock_codes=["999999"]))

    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"
    client.close()


@pytest.mark.parametrize("data_type", ["company_profile", "business_breakdown"])
def test_eastmoney_f10_adapter_malformed_response_returns_missing(data_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json{{{")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)
    result = adapter.fetch(FetchRequest(data_type=data_type, stock_codes=["600519"]))

    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"
    client.close()


def test_eastmoney_f10_empty_profile_and_invalid_report_date_are_missing() -> None:
    survey = {"jbzl": [{"SECURITY_CODE": "920000", "ORG_PROFILE": None, "BUSINESS_SCOPE": None}]}
    analysis = {
        "zygcfx": [{
            "SECURITY_CODE": "920000", "REPORT_DATE": None, "MAINOP_TYPE": "1",
            "ITEM_NAME": "空记录", "MBI_RATIO": 1,
        }],
    }
    client = _mock_client(survey=survey, analysis=analysis)
    adapter = EastMoneyF10Adapter(rate_limit=0, session=client)

    for data_type in ("company_profile", "business_breakdown"):
        result = adapter.fetch(FetchRequest(data_type=data_type, stock_codes=["920000"]))
        assert result.data == []
        assert result.metadata.confidence == "missing"
        assert result.metadata.error is None
    client.close()


def test_eastmoney_f10_adapter_requires_stock_codes() -> None:
    adapter = EastMoneyF10Adapter(rate_limit=0)
    for data_type in ("company_profile", "business_breakdown"):
        result = adapter.fetch(FetchRequest(data_type=data_type))
        assert result.data == []
        assert result.metadata.confidence == "missing"
        assert result.metadata.error is None


# ─── updater：原子替换 / 保旧值 / retry / missing ─────────────────────


class _FakeF10Adapter:
    def __init__(self, *, profile=None, breakdown=None, error: str | None = None) -> None:
        self.profile = profile
        self.breakdown = breakdown
        self.error = error
        self.calls: list[str] = []

    def fetch(self, request: FetchRequest) -> FetchResult:
        self.calls.append(request.data_type)
        if self.error:
            return _result([], error=self.error)
        data_type = request.data_type
        data = self.profile if data_type == "company_profile" else self.breakdown
        if data is None:
            return _result([], raw=b"{}")
        return _result(data)


def _seed_business_rows(duck: DuckDBStore, code: str = "600519") -> None:
    duck.write_query(
        """INSERT INTO company_profile
           (stock_code, code, name, profile, scope, employee_num,
            source, fetch_time, raw_hash, confidence, batch_id)
           VALUES (?, ?, '旧名', '旧简介', '旧范围', 1, 'eastmoney_f10',
                   CURRENT_TIMESTAMP, ?, 'approximate', 'old-batch')""",
        [code, code, "0" * 64],
    )
    duck.write_query(
        """INSERT INTO business_breakdown
           (stock_code, report_date, type, item_name, amount, ratio, rank,
            source, fetch_time, raw_hash, confidence, batch_id)
           VALUES (?, '2024-12-31', 1, '旧产品', 100.0, 50.0, 1, 'eastmoney_f10',
                   CURRENT_TIMESTAMP, ?, 'approximate', 'old-batch')""",
        [code, "0" * 64],
    )


def test_updater_atomic_replace_publishes_new_data(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_business_rows(duckdb_store)
    profile = [{
        "stock_code": "600519", "code": "600519", "name": "贵州茅台",
        "org_name": "贵州茅台酒股份有限公司", "profile": "新简介", "scope": "新范围",
        "employee_num": 34992, "csrc_industry": "制造业", "trade_market": "上交所",
    }]
    breakdown = [{
        "stock_code": "600519", "report_date": "2025-12-31", "type": 1,
        "item_name": "白酒", "amount": 168774585187.65, "ratio": 99.9624, "rank": 1,
    }]
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter=_FakeF10Adapter(profile=profile, breakdown=breakdown),
    )

    report = updater.update_stock("600519")

    assert report["status"] == "success"
    profile_rows = duckdb_store.read_query(
        "SELECT name, profile, scope, employee_num, source, batch_id FROM company_profile WHERE stock_code='600519'"
    )
    assert profile_rows[0]["name"] == "贵州茅台"
    assert profile_rows[0]["profile"] == "新简介"
    assert profile_rows[0]["batch_id"] == report["batch_id"]
    breakdown_rows = duckdb_store.read_query(
        "SELECT report_date, item_name, ratio FROM business_breakdown WHERE stock_code='600519'"
    )
    # 旧报告期保留（2024-12-31），新报告期发布（2025-12-31）
    assert {str(row["report_date"]) for row in breakdown_rows} == {"2024-12-31", "2025-12-31"}
    # 失败时记录的 missing 已被解决
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM missing_list WHERE stock_code='600519' AND resolved_at IS NULL"
    )[0]["c"] == 0


def test_updater_failure_preserves_old_values_and_records_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_business_rows(duckdb_store)
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter=_FakeF10Adapter(error="source down"),
    )

    report = updater.update_stock("600519")

    assert report["status"] == "failed"
    assert report["retained"] is True
    profile_rows = duckdb_store.read_query(
        "SELECT name, profile FROM company_profile WHERE stock_code='600519'"
    )
    assert profile_rows[0]["name"] == "旧名"
    assert profile_rows[0]["profile"] == "旧简介"
    breakdown_rows = duckdb_store.read_query(
        "SELECT item_name FROM business_breakdown WHERE stock_code='600519'"
    )
    assert breakdown_rows[0]["item_name"] == "旧产品"
    retries = sqlite_store.query(
        "SELECT stock_code, data_type, adapter, error FROM retry_list "
        "WHERE stock_code='600519' ORDER BY data_type"
    )
    assert {row["data_type"] for row in retries} == {"company_profile", "business_breakdown"}
    assert all(row["adapter"] == "eastmoney_f10" for row in retries)
    assert all("source down" in (row["error"] or "") for row in retries)


def test_updater_missing_keeps_old_values_and_records_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_business_rows(duckdb_store)
    # 模拟北交所等真实缺失：profile 有值、breakdown 为空（无错误）
    profile = [{
        "stock_code": "920000", "code": "920000", "name": "贝特瑞",
        "org_name": "贝特瑞新材料集团股份有限公司", "profile": "新简介", "scope": "新范围",
        "employee_num": 1000, "csrc_industry": "制造业", "trade_market": "北交所",
    }]
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter=_FakeF10Adapter(profile=profile, breakdown=None),
    )

    report = updater.update_stock("920000")

    assert report["status"] == "success"
    assert report["profile_rows"] == 1
    assert report["breakdown_rows"] == 0
    missing = sqlite_store.query(
        "SELECT field_name, reason_code FROM missing_list "
        "WHERE stock_code='920000' AND resolved_at IS NULL"
    )
    assert missing == [{"field_name": "business_breakdown", "reason_code": "source_empty"}]
    # 已发布的数据可读
    assert duckdb_store.read_query(
        "SELECT name FROM company_profile WHERE stock_code='920000'"
    )[0]["name"] == "贝特瑞"


def test_updater_retry_and_missing_dedup_and_resolve(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    failing = _FakeF10Adapter(error="boom")
    updater = BusinessOverviewUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter=failing)

    updater.update_stock("600519")
    updater.update_stock("600519")

    retries = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM retry_list WHERE stock_code='600519' AND data_type='company_profile'"
    )
    assert retries[0]["c"] == 1, "retry_list 必须按请求去重"

    # missing 去重：连续两次空结果只保留一条未解决
    empty = _FakeF10Adapter(profile=None, breakdown=None)
    updater = BusinessOverviewUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter=empty)
    updater.update_stock("600519")
    updater.update_stock("600519")
    missing = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM missing_list "
        "WHERE stock_code='600519' AND field_name='company_profile' AND resolved_at IS NULL"
    )
    assert missing[0]["c"] == 1

    # 数据到达后 missing 解决、retry 清空
    good = _FakeF10Adapter(
        profile=[{"stock_code": "600519", "code": "600519", "name": "贵州茅台"}],
        breakdown=[{
            "stock_code": "600519", "report_date": "2025-12-31", "type": 1,
            "item_name": "白酒", "amount": 100.0, "ratio": 100.0, "rank": 1,
        }],
    )
    updater = BusinessOverviewUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter=good)
    updater.update_stock("600519")
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM missing_list "
        "WHERE stock_code='600519' AND resolved_at IS NULL"
    )[0]["c"] == 0
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM retry_list WHERE stock_code='600519'"
    )[0]["c"] == 0


def test_update_config_uses_get_value_api(monkeypatch) -> None:
    from app.core.config import Config

    class FakeConfig:
        def get_value(self, key: str, default=None):
            return {"update": {"business_overview_max_stocks_per_run": 100}}.get(key, default)

    monkeypatch.setattr(Config, "current", classmethod(lambda cls: FakeConfig()))
    assert BusinessOverviewUpdater._load_config(
        "business_overview_max_stocks_per_run", default=20,
    ) == 100


def test_updater_requires_database_profile(monkeypatch) -> None:
    from app.core.storage.path_policy import PathIsolationError
    for name in ("VD_ENV", "VD_FORMAL_ACK", "VD_DUCKDB_PATH", "VD_SQLITE_PATH",
                 "VD_TEST_RUN_ROOT", "VD_STAGING_ROOT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PathIsolationError):
        BusinessOverviewUpdater()


def test_bounded_auto_refresh_continues_with_missing_stocks(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    for code in ("600001", "600002", "600003"):
        _seed_stock(duckdb_store, code)
    adapter = _FakeF10Adapter(
        profile=[{"stock_code": "ignored", "code": "ignored", "name": "公司"}],
        breakdown=[{
            "stock_code": "ignored", "report_date": "2025-12-31", "type": 1,
            "item_name": "产品", "amount": 100.0, "ratio": 100.0, "rank": 1,
        }],
    )
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=adapter,
    )
    monkeypatch.setattr(
        updater,
        "_load_config",
        lambda key, default: {
            "business_overview_auto_enabled": True,
            "business_overview_refresh_interval_days": 30,
            "business_overview_max_stocks_per_run": 1,
        }.get(key, default),
    )

    first = updater.refresh_if_due()
    second = updater.refresh_if_due()

    assert first["targeted"] == 1
    assert second["targeted"] == 1
    covered = duckdb_store.read_query(
        "SELECT stock_code FROM company_profile ORDER BY stock_code"
    )
    assert [row["stock_code"] for row in covered] == ["600001", "600002"]


def test_update_all_bounded_cursor_skips_covered_prefix(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """max_stocks>0 的 update_all 必须从未覆盖子集续传，不能重复刷前缀。"""
    for code in ("600001", "600002", "600003"):
        _seed_stock(duckdb_store, code)
    adapter = _FakeF10Adapter(
        profile=[{"stock_code": "ignored", "code": "ignored", "name": "公司"}],
        breakdown=[{
            "stock_code": "ignored", "report_date": "2025-12-31", "type": 1,
            "item_name": "产品", "amount": 100.0, "ratio": 100.0, "rank": 1,
        }],
    )
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=adapter,
    )

    first = updater.update_all(max_stocks=1)
    second = updater.update_all(max_stocks=1)

    assert first["targeted"] == 1
    assert second["targeted"] == 1
    rows = duckdb_store.read_query(
        "SELECT stock_code FROM company_profile ORDER BY stock_code"
    )
    assert [row["stock_code"] for row in rows] == ["600001", "600002"]


def test_due_stock_codes_include_stale_rows(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_business_rows(duckdb_store)
    stale = datetime.now(UTC) - timedelta(days=31)
    duckdb_store.write_query(
        "UPDATE company_profile SET fetch_time = ? WHERE stock_code = '600519'",
        [stale],
    )
    duckdb_store.write_query(
        "UPDATE business_breakdown SET fetch_time = ? WHERE stock_code = '600519'",
        [stale],
    )

    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=_FakeF10Adapter(),
    )
    assert updater._due_stock_codes(30) == ["600519"]


# ─── schema migration v8 + readiness 完全不变 ─────────────────────────


def test_duckdb_migration_v8_and_sqlite_missing_index(
    database_paths,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)

    tables = {row["table_name"] for row in duck.read_query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    )}
    assert "company_profile" in tables
    assert "business_breakdown" in tables
    migration = duck.read_query(
        "SELECT description FROM schema_migrations WHERE version = 8"
    )
    assert migration and "business overview" in migration[0]["description"].lower()

    index = sqlite.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_missing_list_stock_field_open'"
    )
    assert index, "missing_list 未解决条目去重索引必须存在"
    migration_sqlite = sqlite.query(
        "SELECT description FROM schema_migrations WHERE version = 15"
    )
    assert migration_sqlite and "missing_list" in migration_sqlite[0]["description"].lower()


def test_readiness_unchanged_by_business_domain(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    before = minimum_data_readiness(duckdb_store, sqlite_store)
    assert before["schema_compatibility"]["compatible"] is True

    duckdb_store.write_query(
        """INSERT INTO company_profile
           (stock_code, code, name, profile, scope, employee_num,
            source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('600519', '600519', '贵州茅台', '简介', '范围', 34992,
                   'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'b')""",
        ["0" * 64],
    )
    duckdb_store.write_query(
        """INSERT INTO business_breakdown
           (stock_code, report_date, type, item_name, amount, ratio, rank,
            source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('600519', '2025-12-31', 1, '白酒', 100.0, 99.9, 1,
                   'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'b')""",
        ["0" * 64],
    )

    after = minimum_data_readiness(duckdb_store, sqlite_store)
    assert after == before, "业务概览数据不得改变 A 股 readiness"


def test_readiness_code_has_no_business_domain_reference() -> None:
    from app.core import data_quality as module
    source = inspect.getsource(module)
    assert "company_profile" not in source
    assert "business_breakdown" not in source


# ─── /business-overview API ───────────────────────────────────────────


def _api_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    return TestClient(app)


def test_business_overview_api_unknown_stock_is_404(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    client = _api_client(duckdb_store, sqlite_store)
    response = client.get("/api/stock/000001/business-overview")
    assert response.status_code == 404


def test_business_overview_api_returns_composition_history_and_provenance(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO company_profile
           (stock_code, code, name, profile, scope, employee_num, csrc_industry, trade_market,
            source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('600519', '600519', '贵州茅台', '茅台酒生产销售', '经营范围', 34992,
                   '制造业-酒', '上海证券交易所', 'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'b1')""",
        ["a" * 64],
    )
    for type_, item, amount, ratio, rank in (
        (1, "白酒", 168774585187.65, 99.96, 1),
        (2, "茅台酒", 146499906480.49, 86.77, 1),
        (3, "国内", 165390000000.0, 98.0, 1),
    ):
        duckdb_store.write_query(
            """INSERT INTO business_breakdown
               (stock_code, report_date, type, item_name, amount, ratio, rank,
                source, fetch_time, raw_hash, confidence, batch_id)
               VALUES ('600519', '2025-12-31', ?, ?, ?, ?, ?,
                       'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'b1')""",
            [type_, item, amount, ratio, rank, "a" * 64],
        )

    client = _api_client(duckdb_store, sqlite_store)
    response = client.get("/api/stock/600519/business-overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stock_code"] == "600519"
    assert payload["profile"]["status"] == "ok"
    assert payload["profile"]["name"] == "贵州茅台"
    assert payload["profile"]["employee_num"] == 34992
    assert payload["profile"]["provenance"]["source"] == "eastmoney_f10"
    assert payload["profile"]["provenance"]["confidence"] == "approximate"
    assert payload["profile"]["provenance"]["batch_id"] == "b1"
    assert payload["breakdown"]["status"] == "ok"
    assert payload["breakdown"]["latest_report_date"] == "2025-12-31"
    assert set(payload["breakdown"]["composition"].keys()) == {"1", "2", "3"}
    assert payload["breakdown"]["composition"]["1"][0]["item_name"] == "白酒"
    assert payload["breakdown"]["composition"]["3"][0]["ratio"] == 98.0
    assert len(payload["breakdown"]["history"]) == 3
    assert payload["breakdown"]["provenance"]["source"] == "eastmoney_f10"
    assert payload["provenance"]["profile"]["batch_id"] == "b1"


def test_business_overview_api_missing_semantics_for_known_stock(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/stock/600519/business-overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"] == {"status": "missing"}
    assert payload["breakdown"]["status"] == "missing"
    assert payload["breakdown"]["history"] == []
    assert payload["provenance"]["profile"] is None
    assert payload["provenance"]["breakdown"] is None


# ─── 东财 F10 独立实例注册（manager 层） ──────────────────────────────


def test_manager_registers_independent_eastmoney_f10() -> None:
    from app.core.adapters import manager as manager_module
    from app.core.adapters.manager import AdapterManager

    assert "eastmoney_f10" in manager_module.KNOWN_ADAPTERS
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["eastmoney_f10"] >= 0.5
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["company_profile"] == ["eastmoney_f10"]
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["business_breakdown"] == ["eastmoney_f10"]

    adapter_manager = AdapterManager()
    adapter_manager._ensure_initialized()
    adapter = adapter_manager.get_adapter("eastmoney_f10")
    assert adapter is not None
    assert adapter.rate_limit_interval >= 0.5
    # 2026-08-25 数据补全：F10 适配器扩展 placement_funding（增发/配股）
    assert adapter.supported_data_types == {
        "company_profile", "business_breakdown", "placement_funding",
    }


def test_updater_defaults_to_manager_for_independent_circuit_breaker(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from app.core.adapters.manager import AdapterManager

    updater = BusinessOverviewUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert isinstance(updater.adapter, AdapterManager)


def test_update_many_concurrent_fetch_serial_persist(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    for code in ("600001", "600002", "600003", "600004"):
        _seed_stock(duckdb_store, code)
    adapter = _FakeF10Adapter(
        profile=[{"stock_code": "ignored", "code": "ignored", "name": "公司"}],
        breakdown=[{
            "stock_code": "ignored", "report_date": "2025-12-31", "type": 1,
            "item_name": "产品", "amount": 100.0, "ratio": 100.0, "rank": 1,
        }],
    )
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=adapter,
    )
    monkeypatch.setattr(
        updater, "_load_config",
        lambda key, default: {"business_overview_concurrency": 4}.get(key, default),
    )

    report = updater.update_many(["600001", "600002", "600003", "600004"])

    assert report["status"] == "success"
    assert report["succeeded"] == 4
    rows = duckdb_store.read_query(
        "SELECT stock_code FROM company_profile ORDER BY stock_code"
    )
    assert [row["stock_code"] for row in rows] == ["600001", "600002", "600003", "600004"]
    # 两类请求均已发出，且结果被逐股原子持久化
    assert sorted(adapter.calls) == ["business_breakdown"] * 4 + ["company_profile"] * 4


def test_due_stock_codes_skip_recently_confirmed_source_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch,
) -> None:
    _seed_stock(duckdb_store)
    updater = BusinessOverviewUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=_FakeF10Adapter(),
    )
    monkeypatch.setattr(
        updater, "_load_config",
        lambda key, default: {"business_overview_missing_retry_days": 7}.get(key, default),
    )
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO missing_list (stock_code, field_name, reason_code)
               VALUES (?, 'company_profile', 'source_empty'),
                      (?, 'business_breakdown', 'source_empty')""",
            ["600519", "600519"],
        )

    assert updater._due_stock_codes(30) == []
