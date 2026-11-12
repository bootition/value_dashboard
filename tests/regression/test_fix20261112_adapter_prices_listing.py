"""2026-11-12 T1 回归：tencent 预算/超时 + akshare listing_info 股本回填。

对应契约 §1 与 Lead 追加的 P0（上交所清单无股本列 → 腾讯回填）。
全部 monkeypatch，不联网。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

import app.core.adapters.akshare_adapter as akshare_module
import app.core.adapters.tencent_adapter as tencent_module
from app.core.adapters.akshare_adapter import AKShareAdapter
from app.core.adapters.base import FetchRequest
from app.core.adapters.tencent_adapter import TencentAdapter

# ─── tencent：deadline 顺序 / 预算下限 / timeout 下限 ────────────────


class _QuoteResponse:
    def __init__(self, bars: list[list[str]] | None = None) -> None:
        self._bars = bars

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        bars = self._bars if self._bars is not None else [[
            "2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174",
        ]]
        return {"data": {"bj920000": {"qfqday": bars}}}


def test_tencent_deadline_checked_after_rate_limit_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限速等待吃掉预算后不得再发 HTTP 请求。"""
    adapter = TencentAdapter(rate_limit=0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(tencent_module.time, "monotonic", lambda: clock["now"])
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tencent_module.requests.Session,
        "get",
        lambda *args, **kwargs: calls.append(kwargs) or _QuoteResponse(),
    )

    def consume_budget() -> None:
        clock["now"] += 1.0

    monkeypatch.setattr(adapter, "_wait_rate_limit", consume_budget)

    result = adapter.fetch(FetchRequest(
        data_type="price_daily",
        stock_codes=["920000"],
        adjust="qfq",
        extra_params={"deadline_monotonic": 1000.5},
    ))

    assert calls == [], "预算耗尽后不得发起请求"
    assert result.metadata.error_kind == "deadline"
    assert result.metadata.error is None


def test_tencent_remaining_below_half_second_skips_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = TencentAdapter(rate_limit=0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(tencent_module.time, "monotonic", lambda: clock["now"])
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tencent_module.requests.Session,
        "get",
        lambda *args, **kwargs: calls.append(kwargs) or _QuoteResponse(),
    )
    monkeypatch.setattr(adapter, "_wait_rate_limit", lambda: None)

    result = adapter.fetch(FetchRequest(
        data_type="price_daily",
        stock_codes=["920000"],
        adjust="qfq",
        extra_params={"deadline_monotonic": 1000.3},
    ))

    assert calls == [], "剩余预算 <0.5s 时必须直接返回 deadline"
    assert result.metadata.error_kind == "deadline"


def test_tencent_http_timeout_never_below_one_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    class _PagedResponse:
        def __init__(self) -> None:
            self.calls = 0

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            self.calls += 1
            bars = [[
                "2026-07-31", "14.16", "14.58", "15.10", "14.10", "33174",
            ]] if self.calls == 1 else []
            return {"data": {"bj920000": {"qfqday": bars}}}

    responder = _PagedResponse()

    def capture(*args: Any, **kwargs: Any) -> Any:
        seen.append(float(kwargs["timeout"]))
        return responder

    monkeypatch.setattr(tencent_module.requests.Session, "get", capture)
    result = TencentAdapter(rate_limit=0).fetch(FetchRequest(
        data_type="price_daily",
        stock_codes=["920000"],
        adjust="qfq",
        start_date="2026-07-30",
        end_date="2026-07-31",
        extra_params={"deadline_monotonic": time.monotonic() + 5.0},
    ))

    assert seen, "预算充足时必须正常发请求"
    assert all(timeout >= 1.0 for timeout in seen), seen
    assert result.metadata.error_kind == "none"
    assert result.data


# ─── akshare listing_info：上交所股本腾讯回填 ────────────────────────


def _listing_fakes(
    *,
    sh_row: dict[str, Any] | None = None,
    sz_row: dict[str, Any] | None = None,
) -> SimpleNamespace:
    sh_frame = pd.DataFrame(
        [sh_row] if sh_row else [],
        columns=["证券代码", "证券简称", "上市日期"],
    )

    def sh_list(symbol: str) -> pd.DataFrame:
        if symbol == "主板A股":
            return sh_frame
        return pd.DataFrame(columns=["证券代码", "证券简称", "上市日期"])

    sz_frame = pd.DataFrame([sz_row] if sz_row else [])
    return SimpleNamespace(
        stock_info_sh_name_code=sh_list,
        stock_info_sz_name_code=lambda symbol: sz_frame,
        stock_info_bj_name_code=lambda: pd.DataFrame(
            columns=["证券代码", "证券简称", "上市日期", "总股本", "流通股本"]
        ),
        stock_tfp_em=lambda date: pd.DataFrame([{"代码": "000001"}]),
    )


def _install_listing_fakes(
    monkeypatch: pytest.MonkeyPatch,
    quotes: dict[str, dict[str, Any]],
    *,
    sh_row: dict[str, Any] | None = None,
    sz_row: dict[str, Any] | None = None,
) -> list[list[str]]:
    monkeypatch.setattr(
        akshare_module, "ak", _listing_fakes(sh_row=sh_row, sz_row=sz_row),
    )
    calls: list[list[str]] = []

    def fake_quote(codes: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
        calls.append(list(codes))
        return {code: quotes[code] for code in codes if code in quotes}, ""

    monkeypatch.setattr(akshare_module, "_fetch_tencent_share_capital", fake_quote)
    return calls


def test_listing_info_backfills_sse_shares_from_tencent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_listing_fakes(
        monkeypatch,
        quotes={"603468": {"name": "ST Test", "total": 412221700, "circ": 39988574}},
        sh_row={"证券代码": "603468", "证券简称": "*ST Test", "上市日期": "2026-05-01"},
    )
    adapter = AKShareAdapter(rate_limit=0)

    result = adapter.fetch(FetchRequest(data_type="listing_info", stock_codes=["603468"]))

    assert calls == [["603468"]], "缺少股本的沪市股票必须触发一次回填"
    row = result.data[0]
    assert row["total_shares"] == 412221700
    assert row["circ_shares"] == 39988574
    assert row["shares_source"] == "tencent"
    assert "shares_backfilled_from_tencent=1" in (result.metadata.reason or "")
    assert result.metadata.error is None


def test_listing_info_rejects_tencent_name_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_listing_fakes(
        monkeypatch,
        quotes={"603468": {"name": "别的公司", "total": 412221700, "circ": 39988574}},
        sh_row={"证券代码": "603468", "证券简称": "测试股份", "上市日期": "2026-05-01"},
    )
    adapter = AKShareAdapter(rate_limit=0)

    result = adapter.fetch(FetchRequest(data_type="listing_info", stock_codes=["603468"]))

    row = result.data[0]
    assert row["total_shares"] is None
    assert row["circ_shares"] is None
    assert row["shares_source"] is None
    assert "name_mismatch" in (result.metadata.reason or "")


def test_listing_info_rejects_circ_greater_than_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_listing_fakes(
        monkeypatch,
        quotes={"603468": {"name": "测试股份", "total": 100, "circ": 200}},
        sh_row={"证券代码": "603468", "证券简称": "测试股份", "上市日期": "2026-05-01"},
    )
    adapter = AKShareAdapter(rate_limit=0)

    result = adapter.fetch(FetchRequest(data_type="listing_info", stock_codes=["603468"]))

    row = result.data[0]
    assert row["total_shares"] is None
    assert row["circ_shares"] is None
    assert "circ_exceeds_total" in (result.metadata.reason or "")


def test_listing_info_does_not_query_tencent_when_exchange_shares_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_listing_fakes(
        monkeypatch,
        quotes={"000001": {"name": "平安银行", "total": 1, "circ": 1}},
        sz_row={
            "A股代码": "000001",
            "A股简称": "平安银行",
            "A股上市日期": "1991-04-03",
            "A股总股本": 19_405_918_198,
            "A股流通股本": 19_405_918_198,
        },
    )
    adapter = AKShareAdapter(rate_limit=0)

    result = adapter.fetch(FetchRequest(data_type="listing_info", stock_codes=["000001"]))

    assert calls == [], "交易所清单已有完整股本时不得调用腾讯"
    row = result.data[0]
    assert row["total_shares"] == 19_405_918_198
    assert row["shares_source"] == "exchange"


def test_tencent_share_capital_parser_uses_fields_72_and_73(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [""] * 80
    parts[1] = "贵州茅台"
    parts[72] = "26039033"
    parts[73] = "400010000"
    text = 'v_sh688828="' + "~".join(parts) + '";'

    class _ShareResponse:
        encoding = ""

        def __init__(self) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _ShareResponse()

    monkeypatch.setattr(akshare_module.requests, "get", fake_get)

    quotes, _raw = akshare_module._fetch_tencent_share_capital(["688828"])

    assert captured["url"] == "https://qt.gtimg.cn/q=sh688828"
    assert quotes["688828"] == {
        "name": "贵州茅台",
        "total": 400010000,
        "circ": 26039033,
    }
    assert captured["kwargs"]["timeout"] > 0


def test_tencent_quote_symbol_handles_bse_and_b_shares() -> None:
    assert akshare_module._tencent_quote_symbol("920201") == "bj920201"
    assert akshare_module._tencent_quote_symbol("900948") == "sh900948"
    assert akshare_module._tencent_quote_symbol("200429") == "sz200429"
    assert akshare_module._tencent_quote_symbol("688828") == "sh688828"
    assert akshare_module._tencent_quote_symbol("301688") == "sz301688"
