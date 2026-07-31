"""Tencent quote adapter used as the free BSE QFQ price fallback."""

from __future__ import annotations

import json
from typing import Any

import requests

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

_API_VERSION = "tencent-fqkline-1"
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def _symbol(stock_code: str) -> str | None:
    code = stock_code.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


class TencentAdapter(BaseAdapter):
    """Normalized Tencent daily bars with source bytes retained for lineage."""

    def __init__(self, rate_limit: float = 0.2) -> None:
        super().__init__("tencent", {"price_daily"}, rate_limit)

    def _fetch_price_daily(self, request: FetchRequest) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result("tencent price_daily requires stock_codes")
        if request.adjust not in {"raw", "qfq"}:
            return self._make_empty_result("tencent supports raw and qfq prices only")

        records: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        for stock_code in request.stock_codes:
            symbol = _symbol(stock_code)
            if symbol is None:
                return self._make_empty_result(f"invalid stock code: {stock_code}")
            self._wait_rate_limit()
            suffix = "qfq" if request.adjust == "qfq" else ""
            # Tencent's fqkline API requires dashed dates (2021-01-01);
            # compact dates (20210101) are rejected with "param error".
            start = request.start_date or "1990-01-01"
            end = request.end_date or "2099-12-31"
            try:
                response = requests.get(
                    _KLINE_URL,
                    params={"param": f"{symbol},day,{start},{end},640,{suffix}"},
                    headers=_HEADERS,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                return self._make_empty_result(f"tencent price request failed: {error}")

            item = payload.get("data", {}).get(symbol, {})
            key = "qfqday" if request.adjust == "qfq" else "day"
            bars = item.get(key, [])
            # Tencent emits plain ``day`` for BSE symbols without a recorded
            # adjustment factor. In that case raw and QFQ are identical.
            if request.adjust == "qfq" and not bars:
                bars = item.get("day", [])
            if not bars:
                return self._make_empty_result(f"tencent returned no {request.adjust} bars for {stock_code}")
            responses.append(payload)
            for bar in bars:
                if len(bar) < 6:
                    continue
                try:
                    records.append({
                        "stock_code": stock_code,
                        "trade_date": bar[0],
                        "open": float(bar[1]),
                        "close": float(bar[2]),
                        "high": float(bar[3]),
                        "low": float(bar[4]),
                        # Tencent publishes volume in lots; storage uses shares.
                        "volume": float(bar[5]) * 100,
                        "turnover": None,
                        "turnover_rate": None,
                    })
                except (TypeError, ValueError):
                    continue
        if not records:
            return self._make_empty_result("tencent returned no parseable price bars")
        return self._make_result(
            records,
            raw_response=json.dumps(responses, ensure_ascii=False, separators=(",", ":")),
            confidence="approximate",
            api_version=_API_VERSION,
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not self.can_handle(request):
            return self._make_empty_result(f"tencent does not support {request.data_type}")
        return self._fetch_price_daily(request)
