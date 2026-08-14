"""P0-1 回归: CSRC 适配器不得覆盖 CNINFO 公告/分红适配器。

AdapterManager 以适配器 name 为字典键；此前 CSRCIndustryAdapter 也注册为
"cninfo"，覆盖了支持 announcements/dividends 的 CNINFOAdapter，导致
PRD 自动公告发现与分红严格来源失效。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.manager import AdapterManager


def test_cninfo_and_csrc_adapters_coexist_under_distinct_names() -> None:
    manager = AdapterManager()
    manager._ensure_initialized()

    names = set(manager.available_adapters)
    assert "cninfo" in names
    assert "cninfo_csrc" in names

    announcements = manager.get_adapter("cninfo")
    csrc = manager.get_adapter("cninfo_csrc")
    assert announcements is not None
    assert csrc is not None
    assert {"announcements", "dividends"} <= set(announcements.supported_data_types)
    assert csrc.supported_data_types == {"csrc_industry"}


def test_announcements_still_route_to_cninfo(monkeypatch) -> None:
    manager = AdapterManager()
    manager._ensure_initialized()
    adapter = manager.get_adapter("cninfo")
    assert adapter is not None

    captured: dict[str, str] = {}

    def fake_fetch(self, request):
        captured["data_type"] = request.data_type
        return FetchResult(
            data=[{
                "announcement_id": "notice-1",
                "stock_code": "000001",
                "title": "2026年半年度报告",
                "announcement_time": "2026-07-28T00:00:00Z",
            }],
            metadata=SourceMetadata(
                source="cninfo", fetch_time=datetime.now(UTC),
                raw_response_hash="a" * 64, confidence="strict",
            ),
            raw_response=b"{}",
        )

    monkeypatch.setattr(type(adapter), "fetch", fake_fetch)

    result = manager.fetch(FetchRequest(
        data_type="announcements", start_date="2026-07-01", end_date="2026-07-31",
    ))

    assert captured["data_type"] == "announcements"
    assert result.metadata.error is None
    assert result.data[0]["announcement_id"] == "notice-1"


def test_csrc_industry_routes_to_cninfo_csrc(monkeypatch) -> None:
    import pandas as pd

    manager = AdapterManager()
    manager._ensure_initialized()
    adapter = manager.get_adapter("cninfo_csrc")
    assert adapter is not None

    def fake_change(symbol: str) -> pd.DataFrame:
        assert symbol == "000001"
        return pd.DataFrame([
            {"最新记录标识": 1, "行业门类": "制造业", "行业大类": "专用设备制造业",
             "变更日期": "2025-06-01"},
        ])

    monkeypatch.setattr("akshare.stock_industry_change_cninfo", fake_change)

    result = manager.fetch(FetchRequest(data_type="csrc_industry", stock_codes=["000001"]))

    assert result.metadata.error is None
    assert result.data == [{
        "stock_code": "000001", "csrc_l1": "制造业",
        "csrc_l2": "专用设备制造业", "as_of_date": "2025-06-01",
    }]


def test_csrc_industry_priority_names_the_dedicated_adapter() -> None:
    from app.core.adapters import manager as manager_module

    assert manager_module.DEFAULT_ADAPTER_PRIORITY["csrc_industry"] == ["cninfo_csrc"]
    assert "cninfo_csrc" in manager_module.KNOWN_ADAPTERS
    assert manager_module.build_adapter_priority(None)["csrc_industry"] == ["cninfo_csrc"]
