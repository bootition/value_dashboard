"""港股分红域回归（2026-09-04）

覆盖：
- A+H 映射：600941→00941、人工覆写、归一化、不做危险后缀剥离
- 分红方案解析：HKD/CNY 每股股息、无法识别时如实 None
- eastmoney_hk_dividend 适配器：列名兼容、合法空、网络错误
- HKDividendUpdater：单股原子替换、失败/空响应保留旧值、retry/missing
- AdapterManager 注册与独立 0.5s 限速
- 港股域不触碰 A 股 readiness 相关表
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pandas as pd
import pytest

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.hk_dividend_adapter import (
    EastMoneyHKDividendAdapter,
    parse_hk_plan_explain,
)
from app.core.ah_hk_mapping import build_ah_hk_mapping, normalize_company_name
from app.core.hk_dividends import HKDividendUpdater
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

# ─── 构造器 ──────────────────────────────────────────────────────────

def _seed_stock(
    duck: DuckDBStore,
    code: str,
    name: str,
    *,
    exchange: str = "SSE",
) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES (?, ?, ?, TRUE)
           ON CONFLICT (stock_code) DO UPDATE SET
             name = excluded.name, exchange = excluded.exchange,
             is_listed = TRUE""",
        [code, name, exchange],
    )


def _result(
    data: list[dict],
    *,
    source: str = "eastmoney_hk_dividend",
    error: str | None = None,
    confidence: str = "approximate",
) -> FetchResult:
    raw = hashlib.sha256(
        repr(data).encode("utf-8") if not error else error.encode("utf-8"),
    ).hexdigest().encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source=source,  # type: ignore[arg-type]
            fetch_time=datetime.now(UTC),
            raw_response_hash=raw.decode("ascii"),
            confidence=confidence,  # type: ignore[arg-type]
            error=error,
        ),
        raw_response=raw,
    )


class _FakeHKAdapter:
    """最小 fake adapter：按港股代码返回预设结果。"""

    def __init__(
        self,
        by_hk: dict[str, FetchResult] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        self.by_hk = by_hk or {}
        self.error = error
        self.calls: list[str] = []

    def fetch(self, request: FetchRequest) -> FetchResult:
        hk_code = request.stock_codes[0]
        self.calls.append(hk_code)
        if self.error:
            return _result([], error=self.error, confidence="missing")
        return self.by_hk.get(hk_code, _result([], confidence="missing"))


# ─── A+H 映射 ───────────────────────────────────────────────────────

def test_schema_v20_creates_hk_dividends_table(
    duckdb_store: DuckDBStore,
) -> None:
    from app.core.storage.schema import DUCKDB_SCHEMA_VERSION

    assert DUCKDB_SCHEMA_VERSION == 20
    columns = {
        row["column_name"]
        for row in duckdb_store.read_query(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'hk_dividends'",
        )
    }
    assert {
        "stock_code", "ex_date", "announcement_date", "report_period",
        "plan_explain", "dividend_per_share_hkd", "dividend_per_share_cny",
        "transfer_end_date", "dividend_date", "source", "fetch_time",
        "raw_response_hash", "confidence", "batch_id",
    } <= columns
    versions = duckdb_store.read_query(
        "SELECT MAX(version) AS v FROM schema_migrations",
    )[0]["v"]
    assert versions == 20


def test_ah_hk_mapping_maps_china_mobile_and_manual_override(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_stock(duckdb_store, "601375", "中原证券")

    report = build_ah_hk_mapping(duckdb_store)

    assert report["status"] == "ok"
    assert report["mapped_stocks"] == 2
    assert report["mapping"]["600941"] == {
        "hk_code": "00941",
        "hk_name": "中国移动",
        "a_name": "中国移动",
        "match_type": "exact_name",
    }
    assert report["mapping"]["601375"] == {
        "hk_code": "01375",
        "hk_name": "中州证券",
        "a_name": "中原证券",
        "match_type": "manual_override",
    }


def test_ah_hk_mapping_normalizes_full_width_and_whitespace(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_stock(duckdb_store, "000002", "万  科Ａ", exchange="SZSE")

    assert normalize_company_name("万  科Ａ") == "万科A"
    report = build_ah_hk_mapping(
        duckdb_store,
        ah_spot_rows=[
            {"hk_code": "02202", "hk_name": "万科企业"},
            {"hk_code": "00941", "hk_name": " 中国移动 "},
        ],
    )
    # 02202 走人工覆写；00941 仅做归一化精确匹配，不猜后缀。
    assert report["mapping"]["000002"]["hk_code"] == "02202"
    assert "600941" not in report["mapping"]


def test_ah_hk_mapping_does_not_strip_ambiguous_suffixes(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_stock(duckdb_store, "600036", "招商银行")
    _seed_stock(duckdb_store, "600999", "招商证券")

    report = build_ah_hk_mapping(
        duckdb_store,
        ah_spot_rows=[
            {"hk_code": "03968", "hk_name": "招商银行"},
            {"hk_code": "06099", "hk_name": "招商证券"},
        ],
    )

    assert report["mapping"]["600036"]["hk_code"] == "03968"
    assert report["mapping"]["600999"]["hk_code"] == "06099"


# ─── 分红方案解析 ─────────────────────────────────────────────────────

def test_parse_hk_plan_explain_cny_with_hkd_equivalent() -> None:
    assert parse_hk_plan_explain("每股派息2.51元(相当于港币2.9003元)") == (
        pytest.approx(2.9003), pytest.approx(2.51),
    )


def test_parse_hk_plan_explain_hkd_only() -> None:
    assert parse_hk_plan_explain("每股派息港币2.4元") == (
        pytest.approx(2.4), None,
    )


def test_parse_hk_plan_explain_actual_eastmoney_variants() -> None:
    assert parse_hk_plan_explain(
        "每股派人民币2.51元(相当于港币2.9003元)",
    ) == (pytest.approx(2.9003), pytest.approx(2.51))
    assert parse_hk_plan_explain(
        "每股派港币1.582元(相当于人民币1.322元)",
    ) == (pytest.approx(1.582), pytest.approx(1.322))


def test_parse_hk_plan_explain_unrecognized_is_none() -> None:
    assert parse_hk_plan_explain("每10股派息1.5元") == (None, None)
    assert parse_hk_plan_explain("") == (None, None)
    assert parse_hk_plan_explain(None) == (None, None)


# ─── 适配器 ─────────────────────────────────────────────────────────

class _FakeAK:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
        self.calls.append(symbol)
        return pd.DataFrame([
            {
                "最新公告日期": "2026-08-13",
                "财政年度": "2026",
                "分红方案": "每股派人民币2.51元(相当于港币2.9003元)",
                "分配类型": "中期分红",
                "除净日": "2026-08-25",
                "截至过户日": "2026/08/27-2026/08/31",
                "发放日": "2026-09-30",
            },
            {
                "最新公告日期": "2025-05-21",
                "财政年度": "2025",
                "分红方案": "每股派息港币2.4元",
                "分配类型": "未确定",
                "除净日": "2025-06-05",
                "截至过户日": "2025/06/09-2025/06/11",
                "发放日": "2025-06-24",
            },
        ])


def test_hk_dividend_adapter_parses_current_column_names(monkeypatch) -> None:
    import app.core.adapters.hk_dividend_adapter as mod

    fake = _FakeAK()
    monkeypatch.setattr(mod, "ak", fake)
    adapter = EastMoneyHKDividendAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(
        data_type="hk_dividends", stock_codes=["00941"],
    ))

    assert result.metadata.error is None
    assert result.metadata.confidence == "approximate"
    assert result.metadata.source == "eastmoney_hk_dividend"
    assert fake.calls == ["00941"]
    assert len(result.data) == 2
    first, second = result.data
    assert first["stock_code"] == "00941"
    assert first["announcement_date"] == "2026-08-13"
    assert first["report_period"] == "2026"
    assert first["ex_date"] == "2026-08-25"
    assert first["transfer_end_date"] == "2026/08/27-2026/08/31"
    assert first["dividend_date"] == "2026-09-30"
    assert first["dividend_per_share_hkd"] == pytest.approx(2.9003)
    assert first["dividend_per_share_cny"] == pytest.approx(2.51)
    assert second["dividend_per_share_hkd"] == pytest.approx(2.4)
    assert second["dividend_per_share_cny"] is None
    assert len(result.metadata.raw_response_hash) == 64


def test_hk_dividend_adapter_empty_is_legal_missing(monkeypatch) -> None:
    import app.core.adapters.hk_dividend_adapter as mod

    class _EmptyAK:
        def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr(mod, "ak", _EmptyAK())
    result = EastMoneyHKDividendAdapter(rate_limit=0).fetch(FetchRequest(
        data_type="hk_dividends", stock_codes=["00941"],
    ))

    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"


def test_hk_dividend_adapter_network_error_is_error(monkeypatch) -> None:
    import app.core.adapters.hk_dividend_adapter as mod

    class _BrokenAK:
        def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
            raise ConnectionError("network down")

    monkeypatch.setattr(mod, "ak", _BrokenAK())
    result = EastMoneyHKDividendAdapter(rate_limit=0).fetch(FetchRequest(
        data_type="hk_dividends", stock_codes=["00941"],
    ))

    assert result.data == []
    assert "network down" in (result.metadata.error or "")


# ─── Updater：原子替换 / 保旧值 / retry / missing ────────────────────

def _hk_rows() -> list[dict]:
    return [
        {
            "stock_code": "00941",
            "announcement_date": "2026-08-13",
            "report_period": "2026",
            "plan_explain": "每股派人民币2.51元(相当于港币2.9003元)",
            "ex_date": "2026-08-25",
            "transfer_end_date": "2026/08/27-2026/08/31",
            "dividend_date": "2026-09-30",
            "dividend_per_share_hkd": 2.9003,
            "dividend_per_share_cny": 2.51,
        },
    ]


def _seed_hk_dividend_row(duck: DuckDBStore) -> None:
    duck.write_query(
        """INSERT INTO hk_dividends
           (stock_code, ex_date, announcement_date, report_period, plan_explain,
            dividend_per_share_hkd, dividend_per_share_cny, transfer_end_date,
            dividend_date, source, fetch_time, raw_response_hash, confidence, batch_id)
           VALUES ('00941', '2004-06-10', '2004-03-19', '2003', '每股派港币0.2元',
                   0.2, NULL, '2004/06/14-2004/06/16', '2004-06-21',
                   'eastmoney_hk_dividend', CURRENT_TIMESTAMP, ?, 'approximate', 'old-batch')""",
        ["0" * 64],
    )


def test_hk_dividend_updater_atomic_replace_without_touching_readiness(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_hk_dividend_row(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
           (stock_code, report_date, latest_close, calculated_at)
           VALUES ('600941', '2025-12-31', 1.0, CURRENT_TIMESTAMP)""",
    )
    fake = _FakeHKAdapter(by_hk={"00941": _result(_hk_rows())})
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )
    mapping = {"600941": {"hk_code": "00941"}}

    report = updater.update_stock("600941", mapping=mapping)

    assert report["status"] == "success"
    assert report["event_rows"] == 1
    assert fake.calls == ["00941"]
    rows = duckdb_store.read_query(
        "SELECT stock_code, plan_explain, batch_id, source FROM hk_dividends"
    )
    assert len(rows) == 1
    assert rows[0]["batch_id"] != "old-batch"
    assert rows[0]["source"] == "eastmoney_hk_dividend"
    # 独立低频域：不触碰 A 股 readiness 输入表
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS n FROM stock_meta WHERE stock_code='600941'",
    )[0]["n"] == 1
    assert duckdb_store.read_query(
        """SELECT latest_close FROM indicator_snapshot
           WHERE stock_code='600941' AND report_date='2025-12-31'""",
    )[0]["latest_close"] == 1.0


def test_hk_dividend_updater_error_keeps_old_and_records_retry(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_hk_dividend_row(duckdb_store)
    fake = _FakeHKAdapter(error="network down")
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )

    report = updater.update_stock("600941", mapping={"600941": {"hk_code": "00941"}})

    assert report["status"] == "failed"
    assert report["retained"] is True
    assert duckdb_store.read_query("SELECT COUNT(*) AS n FROM hk_dividends")[0]["n"] == 1
    retry = sqlite_store.query(
        "SELECT stock_code, data_type, adapter FROM retry_list WHERE data_type='hk_dividends'",
    )
    assert retry and retry[0]["stock_code"] == "600941"
    assert retry[0]["adapter"] == "eastmoney_hk_dividend"


def test_hk_dividend_updater_empty_keeps_old_and_records_missing(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_hk_dividend_row(duckdb_store)
    fake = _FakeHKAdapter(by_hk={"00941": _result([], confidence="missing")})
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )

    report = updater.update_stock("600941", mapping={"600941": {"hk_code": "00941"}})

    assert report["status"] == "success"
    assert report["retained"] is True
    assert duckdb_store.read_query("SELECT COUNT(*) AS n FROM hk_dividends")[0]["n"] == 1
    missing = sqlite_store.query(
        """SELECT stock_code, field_name, reason_code FROM missing_list
           WHERE field_name='hk_dividends' AND resolved_at IS NULL""",
    )
    assert missing and missing[0]["stock_code"] == "600941"
    assert missing[0]["reason_code"] == "source_empty"


# ─── Manager 注册与限速 ───────────────────────────────────────────────

def test_manager_registers_hk_dividend_adapter_with_half_second_limit() -> None:
    from app.core.adapters import manager as manager_module
    from app.core.adapters.manager import AdapterManager

    assert "eastmoney_hk_dividend" in manager_module.KNOWN_ADAPTERS
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["hk_dividends"] == [
        "eastmoney_hk_dividend",
    ]
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["eastmoney_hk_dividend"] == 0.5

    manager = AdapterManager()
    manager._ensure_initialized()
    adapter = manager.get_adapter("eastmoney_hk_dividend")
    assert adapter is not None
    assert adapter.rate_limit_interval == 0.5
    assert adapter.supported_data_types == {"hk_dividends"}
