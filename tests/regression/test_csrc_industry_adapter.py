"""CSRC 行业分类适配器测试（mock AKShare，不联网）"""

from __future__ import annotations

import pandas as pd

from app.core.adapters.base import FetchRequest
from app.core.adapters.csrc_industry_adapter import CSRCIndustryAdapter


def test_csrc_industry_fetch_returns_current_classification(monkeypatch) -> None:
    history = pd.DataFrame([
        {"最新记录标识": 0, "行业门类": "制造业", "行业大类": "计算机、通信和其他电子设备制造业",
         "变更日期": "2024-01-01"},
        {"最新记录标识": 1, "行业门类": "制造业", "行业大类": "专用设备制造业",
         "变更日期": "2025-06-01"},
    ])

    def fake_change(symbol: str) -> pd.DataFrame:
        assert symbol == "000001"
        return history

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert result.metadata.error is None
    assert result.metadata.confidence == "strict"
    assert result.data == [{
        "stock_code": "000001",
        "csrc_l1": "制造业",
        "csrc_l2": "专用设备制造业",
        "as_of_date": "2025-06-01",
    }]


def test_csrc_industry_fetch_skips_non_latest_records(monkeypatch) -> None:
    history = pd.DataFrame([
        {"最新记录标识": 0, "行业门类": "制造业", "行业大类": "旧大类", "变更日期": "2020-01-01"},
        {"最新记录标识": 0, "行业门类": "金融业", "行业大类": "货币金融服务", "变更日期": "2023-01-01"},
    ])

    def fake_change(symbol: str) -> pd.DataFrame:
        return history

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    # 没有最新记录标识=1 的记录时，退化为取第一条
    assert result.data[0]["csrc_l1"] == "制造业"


def test_csrc_industry_fetch_empty_history_returns_no_data(monkeypatch) -> None:
    def fake_change(symbol: str) -> pd.DataFrame:
        return pd.DataFrame()

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    adapter = CSRCIndustryAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert result.data == []
    assert result.metadata.error is None


def test_csrc_industry_fetch_records_error_per_stock(monkeypatch) -> None:
    def fake_change(symbol: str) -> pd.DataFrame:
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
