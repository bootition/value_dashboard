"""Tencent quote adapter used as the free BSE QFQ price fallback."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import requests

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

_API_VERSION = "tencent-fqkline-2"
# newfqkline/get is the current endpoint; the legacy fqkline/get returns empty
# day arrays for BSE symbols on non-trading days.
_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
# Tencent returns at most this many bars per request (most recent in window).
_MAX_BARS_PER_REQUEST = 640
# 2026-11-12 P1：剩余预算 <0.5s 时不再发请求（0.1s 超时必然假失败）；
# 实际 RTT 实测 0.17~0.25s，因此 HTTP timeout 下限不得低于 1s。
_MIN_REQUEST_BUDGET_SECONDS = 0.5
_MIN_HTTP_TIMEOUT_SECONDS = 1.0
_MAX_HTTP_TIMEOUT_SECONDS = 30.0


def _symbol(stock_code: str) -> str | None:
    """标准 6 位代码 → 腾讯行情符号。

    2026-09-20 修复：原实现对非 6/0/3 开头的一律回落到 `bj`，
    导致 **ETF 代码被映射到北交所**（510300 → bj510300、159915 → bj159915），
    抓取静默失败。补齐交易所上市基金/债券的映射：
    - 5 开头 → 上交所（510xxx/512xxx 等 ETF、5xxxxx 国债）
    - 1 开头 → 深交所（159xxx/16xxxx ETF、1xxxxx 债券）
    - 4/8/9 开头 → 北交所
    """
    code = stock_code.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    # 精确到两位前缀，避免 920xxx（北交所）被误判成 90xxxx（沪市B股）
    if code.startswith(("60", "68", "90", "5")):
        return f"sh{code}"          # 沪市股票/科创板/沪B/沪市基金债券
    if code.startswith(("92", "43", "83", "87", "88")):
        return f"bj{code}"          # 北交所
    if code.startswith(("0", "1", "2", "3")):
        return f"sz{code}"          # 深市股票/深市基金债券/深B
    return f"sh{code}" 


class TencentAdapter(BaseAdapter):
    """Normalized Tencent daily bars with source bytes retained for lineage."""

    def __init__(self, rate_limit: float = 0.2) -> None:
        super().__init__("tencent", {"price_daily"}, rate_limit)
        # Tencent is an HTTP source. Reusing per-thread sessions keeps TLS
        # connections alive across the thousands of raw+qfq requests of a
        # full-market update; sessionless requests.get would pay a new
        # handshake + connection setup on every single request.
        self._session_local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is not None:
            return session
        session = requests.Session()
        session.headers.update(_HEADERS)
        # No automatic retries: the update layer owns timeout/retry policy.
        session.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=2, pool_maxsize=8, max_retries=0,
        ))
        session.mount("http://", requests.adapters.HTTPAdapter(
            pool_connections=2, pool_maxsize=8, max_retries=0,
        ))
        self._session_local.session = session
        with self._sessions_lock:
            self._sessions.append(session)
        return session

    def close(self) -> None:
        """Release reusable HTTP sessions after a long-running update.

        Also clear the calling thread's thread-local session so a later fetch
        on a reused manager creates a fresh session instead of reusing a closed
        one (AdapterManager.close resets _initialized and re-registers fresh
        adapters, but explicit close() must stay safe for direct callers too).
        """
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        with contextlib.suppress(AttributeError):
            del self._session_local.session

    def _deadline_result(
        self,
        reason: str,
        records: list[dict[str, Any]],
        responses: list[dict[str, Any]],
    ) -> FetchResult:
        """预算耗尽：不发新请求，不把 deadline 伪装成源故障/成功。"""
        return self._make_result(
            data=records,
            raw_response=(
                json.dumps(responses, ensure_ascii=False, separators=(",", ":"))
                if responses else None
            ),
            confidence="approximate" if records else "missing",
            error=None,
            error_kind="deadline",
            reason=reason,
            api_version=_API_VERSION,
        )

    def _fetch_price_daily(self, request: FetchRequest) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result(
                "tencent price_daily requires stock_codes",
                error_kind="per_symbol_data_error",
            )
        if request.adjust not in {"raw", "qfq"}:
            return self._make_empty_result(
                "tencent supports raw and qfq prices only",
                error_kind="per_symbol_data_error",
            )

        records: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        truncated: list[str] = []
        for stock_code in request.stock_codes:
            symbol = _symbol(stock_code)
            if symbol is None:
                return self._make_empty_result(
                    f"invalid stock code: {stock_code}",
                    error_kind="per_symbol_data_error",
                )
            # Tencent's fqkline API requires dashed dates (2021-01-01).
            start = request.start_date or "1990-01-01"
            end = request.end_date or "2099-12-31"
            # Page backwards: each request returns the most recent 640 bars in
            # the window, so repeatedly narrow the window until the start is
            # reached or the source has no older data.
            window_end = end
            pages = 0
            while window_end >= start and pages < 12:
                # 2026-11-12 P1：先排队限速再检查预算——限速等待可能吃掉
                # 剩余预算，旧顺序会在预算耗尽后仍然发出 timeout=0.1s 的请求。
                self._wait_rate_limit()
                if request.deadline_exceeded():
                    return self._deadline_result(
                        f"tencent price budget exhausted before request: {stock_code}",
                        records,
                        responses,
                    )
                remaining = request.remaining_seconds(_MAX_HTTP_TIMEOUT_SECONDS)
                if remaining < _MIN_REQUEST_BUDGET_SECONDS:
                    return self._deadline_result(
                        f"tencent price budget too small ({remaining:.2f}s < "
                        f"{_MIN_REQUEST_BUDGET_SECONDS}s): {stock_code}",
                        records,
                        responses,
                    )
                suffix = "qfq" if request.adjust == "qfq" else ""
                request_started = time.monotonic()
                try:
                    response = self._session().get(
                        _KLINE_URL,
                        params={"param": f"{symbol},day,{start},{window_end},{_MAX_BARS_PER_REQUEST},{suffix}"},
                        timeout=max(
                            _MIN_HTTP_TIMEOUT_SECONDS,
                            min(_MAX_HTTP_TIMEOUT_SECONDS, remaining),
                        ),
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (requests.RequestException, ValueError) as error:
                    return self._make_empty_result(
                        f"tencent price request failed: {error}",
                        error_kind="source_unavailable",
                    )
                finally:
                    self.record_response_duration(time.monotonic() - request_started)

                item = payload.get("data", {}).get(symbol, {})
                key = "qfqday" if request.adjust == "qfq" else "day"
                bars = item.get(key, [])
                # Tencent emits plain ``day`` for BSE symbols without a recorded
                # adjustment factor. In that case raw and QFQ are identical.
                if request.adjust == "qfq" and not bars:
                    bars = item.get("day", [])
                if not bars:
                    if pages == 0:
                        return self._make_empty_result(
                            f"tencent returned no {request.adjust} bars for {stock_code}"
                        )
                    break
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
                oldest = min(bar[0] for bar in bars)
                if oldest <= start:
                    break
                # Move the window end just before the oldest bar received.
                window_end = (datetime.strptime(oldest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                pages += 1

            if request.start_date and pages >= 12 and window_end >= start:
                # Reached the pagination cap before covering the explicitly
                # requested start_date: the returned rows are a truncated
                # window and must not look like a complete success.
                truncated.append(
                    f"{stock_code}: pagination limit reached before covering start_date {start}"
                )

        # Tencent's endpoint ignores start_date: every request returns the most
        # recent 640 bars ending at the window end. Drop bars older than the
        # requested start so incremental callers persist only the missing rows
        # (a handful per day) instead of re-writing ~2.6 years every time.
        if request.start_date:
            records = [row for row in records if row["trade_date"] >= request.start_date]
        error = (
            "tencent price truncated: " + "; ".join(truncated)
            if truncated else None
        )
        if not records:
            return self._make_empty_result(
                "tencent returned no parseable price bars"
                + (f"; {error}" if error else ""),
                error_kind="truncated_ok" if truncated else "source_unavailable",
            )
        return self._make_result(
            records,
            raw_response=json.dumps(responses, ensure_ascii=False, separators=(",", ":")),
            confidence="approximate",
            error=error,
            error_kind="truncated_ok" if truncated else "none",
            reason=error,
            truncated=bool(truncated),
            api_version=_API_VERSION,
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not self.can_handle(request):
            return self._make_legal_empty_result(
                f"tencent does not support {request.data_type}",
                error_kind="source_unsupported",
            )
        return self._fetch_price_daily(request)
