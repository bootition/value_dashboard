"""事故出处：reports/117 / 2026-11-12。

2026-11-12 T1 回归：适配器错误分类（akshare 分红 / cninfo 截断 / baostock / F10）。

契约：.planning/2026-11-12-update-log-review/contracts.md §1。
全部离线（akshare 用假模块、CNINFO 用 httpx.MockTransport、baostock 用假 bs）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pandas as pd
import pytest
import yaml

from app.core.adapters.akshare_adapter import AKShareAdapter
from app.core.adapters.baostock_adapter import (
    BaoStockAdapter,
    _to_baostock_code,
)
from app.core.adapters.base import FetchRequest
from app.core.adapters.cninfo_adapter import CNINFOAdapter
from app.core.adapters.czb_mof_adapter import TreasuryMofAdapter
from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
from app.core.adapters.manager import AdapterManager

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _manager_with(adapters: dict[str, Any]) -> AdapterManager:
    manager = AdapterManager()
    manager._initialized = True
    manager._adapters.update(adapters)
    return manager


# ─── akshare 分红：上游 KeyError = 合法空 ─────────────────────────────


def _dividend_frame(**overrides: Any) -> pd.DataFrame:
    row: dict[str, Any] = {
        "实施方案公告日期": "2026-06-22",
        "分红类型": "年度分红",
        "送股比例": 1.0,
        "转增比例": 2.0,
        "派息比例": 6.0,
        "股权登记日": "2026-06-25",
        "除权日": "2026-06-26",
        "派息日": "2026-06-30",
        "股份到账日": None,
        "实施方案分红说明": "10送1转2派6元(含税)",
        "报告时间": "2025年报",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_akshare_dividend_keyerror_is_legal_empty_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 5：akshare 上游 KeyError('实施方案公告日期') → 合法空、无 error、不熔断。"""
    import app.core.adapters.akshare_adapter as module

    def empty_records(symbol: str) -> pd.DataFrame:
        raise KeyError("实施方案公告日期")

    monkeypatch.setattr(module, "ak", SimpleNamespace(stock_dividend_cninfo=empty_records))
    adapter = AKShareAdapter(rate_limit=0)

    result = adapter.fetch(FetchRequest(data_type="dividends", stock_codes=["688835"]))

    assert result.data == []
    assert result.metadata.error is None, "合法空不得写 error（否则旧调用方会进 retry）"
    assert result.metadata.error_kind == "legal_empty"
    assert "688835" in (result.metadata.reason or "")

    manager = _manager_with({"akshare_eastmoney": adapter})
    managed = manager.fetch_with_sources(
        FetchRequest(data_type="dividends", stock_codes=["688835"]),
        ["akshare_eastmoney"],
    )
    assert managed.metadata.error is None
    assert managed.metadata.error_kind == "legal_empty"
    assert manager._circuit_breaker == {}


def test_akshare_dividend_unexpected_keyerror_remains_source_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.adapters.akshare_adapter as module

    def broken(symbol: str) -> pd.DataFrame:
        raise KeyError("records")

    monkeypatch.setattr(module, "ak", SimpleNamespace(stock_dividend_cninfo=broken))
    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="dividends", stock_codes=["600519"])
    )

    assert result.data == []
    assert result.metadata.error is not None
    assert result.metadata.error_kind == "source_unavailable"


def test_akshare_dividend_missing_ex_date_is_per_symbol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 6：缺列 → missing_columns + per_symbol_data_error，且不写假行。"""
    import app.core.adapters.akshare_adapter as module

    frame = _dividend_frame()
    frame = frame.drop(columns=["除权日"])
    monkeypatch.setattr(
        module, "ak", SimpleNamespace(stock_dividend_cninfo=lambda symbol: frame),
    )

    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="dividends", stock_codes=["600519"])
    )

    assert result.data == [], "缺 ex_date 的批次不得写入任何行"
    assert result.metadata.error_kind == "per_symbol_data_error"
    assert result.metadata.error is not None
    assert "ex_date" in result.metadata.missing_columns
    assert result.metadata.confidence == "missing"


def test_akshare_dividend_rows_with_null_ex_date_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.adapters.akshare_adapter as module

    frame = pd.concat(
        [_dividend_frame(), _dividend_frame(除权日=None)], ignore_index=True,
    )
    monkeypatch.setattr(
        module, "ak", SimpleNamespace(stock_dividend_cninfo=lambda symbol: frame),
    )

    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="dividends", stock_codes=["600519"])
    )

    assert len(result.data) == 1
    assert result.data[0]["ex_date"] == "2026-06-26"
    assert all(row.get("ex_date") for row in result.data), "不得补假除权日"
    assert "dropped_rows_without_ex_date" in (result.metadata.reason or "")


def test_akshare_dividend_network_error_stays_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.adapters.akshare_adapter as module

    def broken(symbol: str) -> pd.DataFrame:
        raise ConnectionError("network down")

    monkeypatch.setattr(module, "ak", SimpleNamespace(stock_dividend_cninfo=broken))
    result = AKShareAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="dividends", stock_codes=["600519"])
    )

    assert result.metadata.error_kind == "source_unavailable"
    assert result.metadata.error is not None and "network down" in result.metadata.error


# ─── CNINFO 公告截断 ──────────────────────────────────────────────────

_ANN_RAW = {
    "secCode": "000001",
    "secName": "平安银行",
    "announcementId": "ann-1",
    "announcementTitle": "2026年半年度报告",
    "announcementTime": 1785964800000,
    "adjunctUrl": "",
}


def _cninfo_client(total: int) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"announcements": [_ANN_RAW] * 30, "totalRecordNum": total},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _cninfo_adapter(monkeypatch: pytest.MonkeyPatch, client: httpx.Client) -> CNINFOAdapter:
    adapter = CNINFOAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_resolve_org_id", lambda code: "org-1")
    monkeypatch.setattr(adapter, "_get_client", lambda: client)
    return adapter


def test_cninfo_truncation_returns_partial_rows_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 7：单日窗口截断 → 返回 150 行 + truncated，不抛异常、不计熔断。"""
    client = _cninfo_client(total=3719)
    adapter = _cninfo_adapter(monkeypatch, client)

    result = adapter.fetch(FetchRequest(
        data_type="announcements",
        stock_codes=["000001"],
        start_date="2026-11-12",
        end_date="2026-11-12",
        extra_params={"max_pages": 5},
    ))

    assert len(result.data) == 150, "已抓到的 150 条不得丢弃"
    assert result.metadata.truncated is True
    assert result.metadata.error_kind == "truncated_ok"
    assert result.metadata.error is None
    assert result.metadata.confidence != "strict"
    assert "total=3719" in (result.metadata.reason or "")
    assert "fetched=150" in (result.metadata.reason or "")

    manager = _manager_with({"cninfo": adapter})
    managed = manager.fetch_with_sources(
        FetchRequest(
            data_type="announcements",
            stock_codes=["000001"],
            start_date="2026-11-12",
            end_date="2026-11-12",
            extra_params={"max_pages": 5},
        ),
        ["cninfo"],
    )
    assert len(managed.data) == 150
    assert managed.metadata.error_kind == "truncated_ok"
    assert manager._circuit_breaker == {}
    client.close()


def test_cninfo_multi_day_truncation_splits_by_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """general 窗口跨度 >1 天 → 复用日期二分，每日子窗口仍受 max_pages 限。"""
    client = _cninfo_client(total=3719)
    adapter = _cninfo_adapter(monkeypatch, client)

    result = adapter.fetch(FetchRequest(
        data_type="announcements",
        stock_codes=["000001"],
        start_date="2026-11-10",
        end_date="2026-11-12",
        extra_params={"max_pages": 5},
    ))

    assert len(result.data) == 450, "按日二分后应覆盖 3 个单日窗口（各 150 条）"
    assert result.metadata.truncated is True
    assert result.metadata.error_kind == "truncated_ok"
    client.close()


def test_cninfo_dividends_is_source_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = CNINFOAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="dividends", stock_codes=["000001"]))

    assert result.data == []
    assert result.metadata.error_kind == "source_unsupported"
    assert result.metadata.reason
    assert "unsupported" in result.metadata.reason


def test_cninfo_finalize_empty_with_errors_is_not_strict() -> None:
    adapter = CNINFOAdapter(rate_limit=0)
    result = adapter._finalize_result(
        data=[], label="announcements", stock_count=1, errors=["page 1 failed"],
        error_kind="source_unavailable", reason="page 1 failed",
    )

    assert result.metadata.confidence == "missing"
    assert result.metadata.error is not None


def test_default_dividend_chain_has_no_cninfo() -> None:
    from app.core.adapters.manager import DEFAULT_ADAPTER_PRIORITY

    config = yaml.safe_load((_REPO_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    dividends = config["adapters"]["primary"]["dividends"]

    assert "cninfo" not in dividends
    assert dividends == ["akshare_eastmoney", "baostock"]
    assert DEFAULT_ADAPTER_PRIORITY["dividends"] == ["akshare_eastmoney", "baostock"]


# ─── baostock：900948 不是北交所，skip 不计失败 ───────────────────────


def test_baostock_code_mapping_distinguishes_bse_and_b_shares() -> None:
    # 验收 8：900948 是沪市 B 股，不得判北交所；920xxx 才是北交所
    assert _to_baostock_code("900948") == ("sh.900948", None)
    assert _to_baostock_code("200429") == ("sz.200429", None)
    assert _to_baostock_code("920201") == (None, "bse_unsupported")
    assert _to_baostock_code("830799") == (None, "bse_unsupported")
    assert _to_baostock_code("430047") == (None, "bse_unsupported")
    assert _to_baostock_code("600519") == ("sh.600519", None)
    assert _to_baostock_code("000001") == ("sz.000001", None)


class _FakeBS:
    def __init__(self) -> None:
        self.queried: list[str] = []

    def login(self) -> Any:
        return SimpleNamespace(error_code="0", error_msg="ok")

    def logout(self) -> None:
        return None

    def query_history_k_data_plus(self, code: str, *args: Any, **kwargs: Any) -> Any:
        self.queried.append(code)
        raise AssertionError(f"BSE/B股路径不得发起行情查询: {code}")


class _ErrorRS:
    error_code = "1"
    error_msg = "no data"
    fields: list[str] | None = None


def test_baostock_bse_batch_is_skipped_without_source_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.adapters.baostock_adapter as module

    fake_bs = _FakeBS()
    monkeypatch.setattr(module, "bs", fake_bs)
    adapter = BaoStockAdapter(rate_limit=0)
    manager = _manager_with({"baostock": adapter})

    results = [
        manager.fetch_with_sources(
            FetchRequest(data_type="price_daily", stock_codes=["920201"]),
            ["baostock"],
        )
        for _ in range(5)
    ]

    assert fake_bs.queried == []
    assert all(result.metadata.error_kind == "source_unsupported" for result in results)
    assert all(result.metadata.error is None for result in results)
    assert results[0].metadata.skipped_codes == {"920201": "bse_unsupported"}
    assert manager._circuit_breaker == {}, "BSE skip 不得计熔断"


def test_baostock_b_share_query_failure_is_disclosed_as_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.adapters.baostock_adapter as module

    class _BS(_FakeBS):
        def query_history_k_data_plus(self, code: str, *args: Any, **kwargs: Any) -> Any:
            self.queried.append(code)
            return _ErrorRS()

    fake_bs = _BS()
    monkeypatch.setattr(module, "bs", fake_bs)
    adapter = BaoStockAdapter(rate_limit=0)

    result = adapter.fetch(FetchRequest(
        data_type="price_daily", stock_codes=["900948"],
    ))

    assert fake_bs.queried == ["sh.900948"], "900948 必须按沪市 B 股尝试"
    assert result.data == []
    assert result.metadata.error is None, "skip 不得拼进 error 字符串"
    assert result.metadata.error_kind == "source_unsupported"
    assert result.metadata.skipped_codes == {"900948": "b_share"}
    assert "900948" in (result.metadata.reason or "")


# ─── F10 / CZB：空 vs 畸形 ────────────────────────────────────────────


def test_eastmoney_missing_result_reason_is_not_none(caplog: Any) -> None:
    """验收 9：_missing_result 写 reason + legal_empty，日志不得出现字面 None。"""
    adapter = EastMoneyF10Adapter(rate_limit=0)
    with caplog.at_level(logging.INFO, logger="app.core.adapters.eastmoney_f10_adapter"):
        result = adapter._missing_result("company_profile 合法空: 源无该股资料")

    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.error_kind == "legal_empty"
    assert result.metadata.reason == "company_profile 合法空: 源无该股资料"
    assert "company_profile 合法空" in caplog.text
    assert "None" not in caplog.text


def test_eastmoney_legal_empty_vs_malformed_response() -> None:
    def legal_client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"jbzl": []}),
        ))

    def malformed_client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not-json{{{"),
        ))

    legal_adapter = EastMoneyF10Adapter(rate_limit=0, session=legal_client())
    legal = legal_adapter.fetch(FetchRequest(
        data_type="company_profile", stock_codes=["920000"],
    ))
    assert legal.metadata.error is None
    assert legal.metadata.error_kind == "legal_empty"
    legal_adapter.close()

    bad_adapter = EastMoneyF10Adapter(rate_limit=0, session=malformed_client())
    bad = bad_adapter.fetch(FetchRequest(
        data_type="company_profile", stock_codes=["920000"],
    ))
    assert bad.metadata.error_kind == "source_unavailable"
    assert bad.metadata.error is not None
    bad_adapter.close()


def test_treasury_missing_result_reason_is_not_none() -> None:
    adapter = TreasuryMofAdapter(rate_limit=0)
    result = adapter._missing_result("history 模式需要 tenor")

    assert result.metadata.error is None
    assert result.metadata.error_kind == "legal_empty"
    assert result.metadata.reason == "history 模式需要 tenor"


def test_treasury_legal_empty_vs_malformed_response() -> None:
    def client_for(content: bytes) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content),
        ))

    legal_adapter = TreasuryMofAdapter(rate_limit=0, session=client_for(b"[]"))
    legal = legal_adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": "2026-08-07"},
    ))
    assert legal.metadata.error is None
    assert legal.metadata.error_kind == "legal_empty"
    legal_adapter.close()

    bad_adapter = TreasuryMofAdapter(rate_limit=0, session=client_for(b"not-json{{{"))
    bad = bad_adapter.fetch(FetchRequest(
        data_type="treasury_yield_curve",
        extra_params={"mode": "daily", "work_time": "2026-08-07"},
    ))
    assert bad.metadata.error_kind == "source_unavailable"
    bad_adapter.close()
