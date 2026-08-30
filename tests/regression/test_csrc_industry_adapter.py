"""CSRC 行业分类适配器测试（mock AKShare，不联网）"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.core.adapters.base import FetchRequest
from app.core.adapters.csrc_industry_adapter import CSRCIndustryAdapter


def _row(standard: str, l1: str, l2: str, as_of: str) -> dict:
    return {
        "分类标准": standard,
        "行业门类": l1,
        "行业大类": l2,
        "变更日期": as_of,
    }


def test_csrc_industry_fetch_returns_latest_csrc_classification(monkeypatch) -> None:
    calls: list[dict] = []
    history = pd.DataFrame([
        _row("证监会行业分类标准（2012）", "制造业", "旧大类", "2020-01-01"),
        _row("巨潮行业分类标准", "信息技术", "光电子器件", "2026-01-01"),
        _row("申银万国行业分类标准", "电子", "半导体", "2026-06-01"),
        _row("证监会行业分类标准（2012）", "制造业", "专用设备制造业", "2025-06-01"),
    ])

    def fake_change(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        calls.append({"symbol": symbol, "start_date": start_date, "end_date": end_date})
        return history

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert calls == [{
        "symbol": "000001",
        "start_date": "19900101",
        "end_date": date.today().strftime("%Y%m%d"),
    }]
    assert result.metadata.error is None
    assert result.data == [{
        "stock_code": "000001",
        "csrc_l1": "制造业",
        "csrc_l2": "专用设备制造业",
        "as_of_date": "2025-06-01",
    }]


def test_csrc_industry_fetch_skips_non_csrc_standards(monkeypatch) -> None:
    history = pd.DataFrame([
        _row("巨潮行业分类标准", "金融", "银行", "2023-01-01"),
        _row("申银万国行业分类标准", "银行", "银行", "2021-01-01"),
    ])

    def fake_change(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        return history

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert result.data == []
    assert result.metadata.error is None


def test_csrc_industry_fetch_queries_through_today(monkeypatch) -> None:
    def fake_change(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        assert end_date == date.today().strftime("%Y%m%d")
        return pd.DataFrame()

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))
    assert result.data == []


def test_csrc_industry_fetch_empty_history_returns_no_data(monkeypatch) -> None:
    def fake_change(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame()

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert result.data == []
    assert result.metadata.error is None


def test_csrc_industry_fetch_records_error_per_stock(monkeypatch) -> None:
    def fake_change(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise ConnectionError("network down")

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert result.data == []
    assert "000001" in (result.metadata.error or "")


def test_csrc_industry_fetch_requires_stock_codes() -> None:
    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry"))

    assert result.data == []
    assert "需要股票代码" in (result.metadata.error or "")
