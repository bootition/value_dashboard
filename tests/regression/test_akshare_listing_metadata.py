from __future__ import annotations

import pandas as pd
from types import SimpleNamespace

from app.core.adapters.akshare_adapter import AKShareAdapter
from app.core.adapters.base import FetchRequest


def test_listing_info_uses_exchange_lists_and_current_suspension_data(monkeypatch) -> None:
    import app.core.adapters.akshare_adapter as module

    sh_calls = 0

    def sh_list(symbol: str) -> pd.DataFrame:
        nonlocal sh_calls
        sh_calls += 1
        if sh_calls == 1:
            return pd.DataFrame([{
                "\u8bc1\u5238\u4ee3\u7801": "600001",
                "\u8bc1\u5238\u7b80\u79f0": "*ST Test",
                "\u4e0a\u5e02\u65e5\u671f": "2001-01-01",
            }])
        return pd.DataFrame(columns=["\u8bc1\u5238\u4ee3\u7801", "\u8bc1\u5238\u7b80\u79f0", "\u4e0a\u5e02\u65e5\u671f"])

    fake_akshare = SimpleNamespace(
        stock_info_sh_name_code=sh_list,
        stock_info_sz_name_code=lambda symbol: pd.DataFrame([
            {
                "A\u80a1\u4ee3\u7801": "000001",
                "A\u80a1\u7b80\u79f0": "Ping An",
                "A\u80a1\u4e0a\u5e02\u65e5\u671f": "1991-04-03",
                "A\u80a1\u603b\u80a1\u672c": 1_000_000,
                "A\u80a1\u6d41\u901a\u80a1\u672c": 800_000,
            }
        ]),
        stock_info_bj_name_code=lambda: pd.DataFrame(
            columns=["\u8bc1\u5238\u4ee3\u7801", "\u8bc1\u5238\u7b80\u79f0", "\u4e0a\u5e02\u65e5\u671f", "\u603b\u80a1\u672c", "\u6d41\u901a\u80a1\u672c"]
        ),
        stock_tfp_em=lambda date: pd.DataFrame([{"\u4ee3\u7801": "000001"}]),
    )
    monkeypatch.setattr(module, "ak", fake_akshare)

    adapter = AKShareAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="listing_info", stock_codes=["600001", "000001", "000002"]))

    by_code = {row["stock_code"]: row for row in result.data}
    assert by_code["600001"] == {
        "stock_code": "600001",
        "name": "*ST Test",
        "listing_date": "2001-01-01",
        "is_st": True,
        "is_suspended": False,
        "pinyin": by_code["600001"]["pinyin"],
        "total_shares": None,
        "circ_shares": None,
    }
    assert by_code["000001"]["listing_date"] == "1991-04-03"
    assert by_code["000001"]["is_st"] is False
    assert by_code["000001"]["is_suspended"] is True
    assert by_code["000001"]["total_shares"] == 1_000_000
    assert by_code["000001"]["circ_shares"] == 800_000
    assert by_code["000002"]["listing_date"] is None
    assert by_code["000002"]["is_st"] is None
    assert by_code["000002"]["is_suspended"] is False


def test_dividend_ratios_are_normalized_to_per_share_fields(monkeypatch) -> None:
    import pandas as pd

    import app.core.adapters.akshare_adapter as module

    fake_akshare = SimpleNamespace(
        stock_dividend_cninfo=lambda symbol: pd.DataFrame([{
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
        }]),
    )
    monkeypatch.setattr(module, "ak", fake_akshare)

    adapter = AKShareAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="dividends", stock_codes=["600519"]))

    assert result.metadata.error is None
    row = result.data[0]
    assert row["ex_date"] == "2026-06-26"
    assert row["announcement_date"] == "2026-06-22"
    assert row["dividend_per_share"] == 0.6  # 每10股派6元 → 每股0.6元
    assert row["stock_dividend"] == 0.1  # 每10股送1股 → 每股0.1股
    assert row["transfer_share"] == 0.2  # 每10股转增2股 → 每股0.2股
    assert row["rights_issue"] is None
