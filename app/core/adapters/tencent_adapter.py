"""Tencent quote adapter used as the free BSE QFQ price fallback."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import requests

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

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

# ─── 成交量单位（reports/131 §9.2 P0-1）─────────────────────────────────
# 腾讯 `day`/`qfqday` 的 bar[5]：主板/创业板为**手数**（×100 得股），
# 科创板（688/689）**已经是股数**（不可再乘）。旧实现无条件 ×100，导致
# 2026-07-31 价格主链切到腾讯后科创板成交量被放大 100 倍（688111 金山办公
# 07-30 12,675,928 股 → 07-31 2,873,692,500 股、换手 619%）。
_VOLUME_LOTS_TO_SHARES = 100.0
#: 科创板（含 CDR 689）前缀：腾讯对这两个板块直接返回股数。
_STAR_PREFIXES: tuple[str, ...] = ("688", "689")
#: 换手率上限护栏：bar 成交量 / 流通股本 > 5（即换手 > 500%）在物理上不可能。
#: 触发即说明"源单位与假设不符"，一律按**未乘 100**兜底（宁可少算，不可放大
#: 100 倍污染下游指标），并记 warning 供后续定位。
_MAX_PLAUSIBLE_TURNOVER = 5.0

#: 流通股本解析器：传入股票代码，返回流通股本（股）或 None。
CircSharesProvider = Callable[[str], float | None]

#: 进程内缓存的 `stock_meta.circ_shares` 快照（键 = DuckDB 绝对路径）。
#: 只在成功读取后写入；失败不缓存，避免"库晚于首次抓取创建"时永久失效。
_CIRC_SHARES_CACHE: dict[str, dict[str, float]] = {}
_CIRC_SHARES_CACHE_LOCK = threading.Lock()


def _is_star_market(stock_code: str) -> bool:
    """科创板（688）与科创板 CDR（689）。"""
    return stock_code.startswith(_STAR_PREFIXES)


def _load_circ_shares_from_store() -> dict[str, float]:
    """一次性读取当前档位 `stock_meta.circ_shares`（只读，best-effort）。

    量级自检需要流通股本，但适配器不持有数据库句柄：这里按需打开一次只读
    连接把全市场流通股本读进内存（约 5.5k 行，微秒级查询），之后按库路径
    进程内缓存。任何异常（库文件不存在 / 测试档未建表 / 写锁竞争）都退化
    为"无股本信息"，绝不能因为护栏打断抓取。
    """
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.path_policy import resolve_and_validate_paths

    paths = resolve_and_validate_paths()
    key = str(paths.duckdb_path)
    with _CIRC_SHARES_CACHE_LOCK:
        cached = _CIRC_SHARES_CACHE.get(key)
    if cached is not None:
        return cached
    rows = DuckDBStore(paths=paths).read_query(
        "SELECT stock_code, circ_shares FROM stock_meta "
        "WHERE circ_shares IS NOT NULL AND circ_shares > 0"
    )
    mapping = {str(row["stock_code"]): float(row["circ_shares"]) for row in rows}
    with _CIRC_SHARES_CACHE_LOCK:
        _CIRC_SHARES_CACHE[key] = mapping
    return mapping


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

    def __init__(
        self,
        rate_limit: float = 0.2,
        circ_shares_provider: CircSharesProvider | None = None,
    ) -> None:
        super().__init__("tencent", {"price_daily"}, rate_limit)
        # Tencent is an HTTP source. Reusing per-thread sessions keeps TLS
        # connections alive across the thousands of raw+qfq requests of a
        # full-market update; sessionless requests.get would pay a new
        # handshake + connection setup on every single request.
        self._session_local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        # 量级自检用的流通股本：可注入（测试），默认从当前档位 DuckDB 懒加载。
        self._circ_shares_provider = circ_shares_provider
        self._circ_shares_map: dict[str, float] | None = None
        self._circ_shares_lock = threading.Lock()

    def _circ_shares(self, stock_code: str) -> float | None:
        """流通股本（股）；未知返回 None（护栏自动退化为只看代码前缀）。"""
        if self._circ_shares_provider is not None:
            try:
                value = self._circ_shares_provider(stock_code)
            except Exception:  # noqa: BLE001 - 护栏不得打断抓取
                logger.debug("circulating-share provider failed for %s", stock_code, exc_info=True)
                return None
            return float(value) if value else None
        if self._circ_shares_map is None:
            with self._circ_shares_lock:
                if self._circ_shares_map is None:
                    try:
                        self._circ_shares_map = _load_circ_shares_from_store()
                    except Exception:  # noqa: BLE001 - 护栏不得打断抓取
                        logger.debug("circulating-share lookup unavailable", exc_info=True)
                        self._circ_shares_map = {}
        return self._circ_shares_map.get(stock_code)

    def _bar_volume(self, stock_code: str, trade_date: str, raw_volume: float) -> float:
        """腾讯 bar[5] → 股（单位归一 + 量级自检）。

        判据两层：
        1. 代码前缀：688/689（科创板）腾讯已是股数，不乘；其余板块按手数 ×100。
        2. 量级自检：若归一后的成交量 / 流通股本 > 5（换手 > 500%，物理上不可能），
           说明源单位与假设不符（例如主板/创业板未来也改成股数），记 warning 并
           按**未乘 100**兜底。流通股本未知时跳过第 2 层。
        """
        volume = raw_volume if _is_star_market(stock_code) else raw_volume * _VOLUME_LOTS_TO_SHARES
        circ = self._circ_shares(stock_code)
        if circ is not None and circ > 0 and volume / circ > _MAX_PLAUSIBLE_TURNOVER:
            logger.warning(
                "tencent volume unit guard: %s %s bar[5]=%.4f → 归一后 %s 股，"
                "流通股本 %s 股（换手 %.1f%% > %.0f%% 物理上限），按未乘 100 兜底"
                "（源很可能已直接返回股数）；请核对腾讯 %s 的单位口径",
                stock_code,
                trade_date,
                raw_volume,
                f"{volume:,.0f}",
                f"{circ:,.0f}",
                volume / circ * 100,
                _MAX_PLAUSIBLE_TURNOVER * 100,
                "day/qfqday",
            )
            return raw_volume
        return volume

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
                            # 腾讯 day/qfqday 的 bar[5]：主板/创业板为手数
                            # （×100 得股），科创板 688/689 已是股数（不再乘）。
                            # 另带量级自检兜底，详见 _bar_volume()。
                            "volume": self._bar_volume(stock_code, bar[0], float(bar[5])),
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
