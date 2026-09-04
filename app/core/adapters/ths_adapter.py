"""同花顺官方 Financial-API 适配器（2026-09-05 接入，source=ths）

契约源：https://gitee.com/HiThink-Tech/Financial-API（docs/api）
- Base URL: https://fuyao.aicubes.cn；方法全部 GET；Header `X-api-key`。
- 响应信封：{code, message, request_id, data}；code==0 成功；HTTP 恒 200。
- 时间戳毫秒 Asia/Shanghai；thscode 必须带市场后缀（如 510300.SH）。
- API Key 只从环境变量 `HITHINK_FINANCE_API_KEY` 读取，绝不写入配置/日志/git。

语义边界（reports/61 + 2026-08-30 ths-api-research）：
- 免费额度 ~200 万次/月、历史行情 5 年；仅做低频/补充/交叉，不做价格主链。
- 国内源直连（禁用 HTTP(S)_PROXY）。
- 业务错误 code!=0 一律 error（登记 retry），唯 3004（叶子类型不支持，如 LOF
  请求 ETF 行情端点）按合法 missing 处理，不触发熔断。
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

__all__ = ["ThsAdapter", "THS_BASE_URL", "THS_API_KEY_ENV"]

THS_BASE_URL = "https://fuyao.aicubes.cn"
THS_API_KEY_ENV = "HITHINK_FINANCE_API_KEY"

# 业务 code 3004：标的叶子类型不支持该端点（合法无数据，不熔断）
_LEGAL_EMPTY_CODES = frozenset({3004})


@contextlib.contextmanager
def _domestic_direct() -> Any:
    saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def normalize_thscode(code: str) -> str:
    """补全市场后缀：无后缀的 ETF 代码按交易所规则推断（51/56/58→SH，15/16→SZ）。"""
    code = code.strip().upper()
    if "." in code:
        return code
    if code.startswith(("51", "56", "58")):
        return f"{code}.SH"
    if code.startswith(("15", "16")):
        return f"{code}.SZ"
    return code


def _ms_to_date(ms: Any) -> str | None:
    if ms is None or ms == "" or ms != ms:  # NaN
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=UTC).astimezone().date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


class ThsAdapter(BaseAdapter):
    """同花顺官方 Financial-API 适配器（ETF 行情/资料/跟踪指数分位）。"""

    def __init__(self, rate_limit: float = 0.5, timeout: float = 20.0) -> None:
        super().__init__(
            name="ths",  # type: ignore[arg-type]
            supported={
                "etf_daily", "etf_snapshot", "etf_profile", "etf_track_percentile",
            },  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    # ─── 入口 ────────────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        api_key = os.environ.get(THS_API_KEY_ENV, "")
        if not api_key:
            return self._make_empty_result(
                f"环境变量 {THS_API_KEY_ENV} 未设置", confidence="missing",
            )
        thscode = normalize_thscode((request.stock_codes or [""])[0])
        if not thscode:
            return self._make_empty_result("缺少 thscode", confidence="missing")

        try:
            if request.data_type == "etf_daily":
                return self._fetch_daily(thscode, request, api_key)
            if request.data_type == "etf_snapshot":
                return self._fetch_snapshot(thscode, api_key)
            if request.data_type == "etf_profile":
                return self._fetch_profile(thscode, api_key)
            if request.data_type == "etf_track_percentile":
                return self._fetch_track_percentile(thscode, request, api_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("ths %s %s 抓取失败: %s", request.data_type, thscode, e)
            return self._make_empty_result(f"{type(e).__name__}: {e}", confidence="missing")
        return self._make_empty_result(f"不支持的数据类型 {request.data_type}")

    # ─── 底层请求 ────────────────────────────────────────────────────

    def _get(self, path: str, api_key: str, params: dict[str, Any]) -> dict[str, Any]:
        # 用 httpx 而非 requests：akshare 会 monkeypatch requests.Session.request，
        # 导致 trust_env 等关键字不被接受（2026-09-05 实测）。
        import httpx

        with _domestic_direct():
            response = httpx.get(
                THS_BASE_URL + path,
                params=params,
                headers={"X-api-key": api_key},
                timeout=self._timeout,
                trust_env=False,
                follow_redirects=True,
            )
        response.raise_for_status()
        return response.json()

    def _parse_envelope(
        self, path: str, params: dict[str, Any], api_key: str,
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """返回 (data, error, confidence_override)。business error 与网络错误分离。"""
        try:
            payload = self._get(path, api_key, params)
        except Exception as e:  # noqa: BLE001
            return {}, f"{type(e).__name__}: {e}", None
        code = payload.get("code")
        if code != 0:
            if code in _LEGAL_EMPTY_CODES:
                return {}, None, "missing"
            return {}, f"ths code={code}: {payload.get('message')}", None
        return payload.get("data") or {}, None, None

    # ─── 各端点行映射 ────────────────────────────────────────────────

    def _fetch_daily(
        self, thscode: str, request: FetchRequest, api_key: str,
    ) -> FetchResult:
        start_ms, end_ms = self._window_ms(request)
        path = "/api/fund/market/historical"
        params = {"thscode": thscode, "interval": "1d", "start": start_ms, "end": end_ms}
        data, error, conf = self._parse_envelope(path, params, api_key)
        if error:
            return self._make_empty_result(error)
        items = data.get("item") or []
        if not items:
            return self._make_result([], confidence=conf or "missing")
        rows = []
        for item in items:
            trade_date = _ms_to_date(item.get("date_ms"))
            if trade_date is None:
                continue
            rows.append({
                "thscode": thscode,
                "trade_date": trade_date,
                "close_price": item.get("close_price"),
                "open_price": item.get("open_price"),
                "high_price": item.get("high_price"),
                "low_price": item.get("low_price"),
                "volume": item.get("volume"),
                "turnover": item.get("turnover"),
            })
        return self._make_result(rows, raw_response=_payload_json(data))

    def _fetch_snapshot(self, thscode: str, api_key: str) -> FetchResult:
        path = "/api/fund/market/snapshot"
        params = {"thscode": thscode}
        data, error, conf = self._parse_envelope(path, params, api_key)
        if error:
            return self._make_empty_result(error)
        items = data.get("item") or []
        if not items:
            return self._make_result([], confidence=conf or "missing")
        item = items[0]
        return self._make_result([{
            "thscode": item.get("thscode") or thscode,
            "ticker": item.get("ticker"),
            "last_price": item.get("last_price"),
            "prev_price": item.get("prev_price"),
            "open_price": item.get("open_price"),
            "high_price": item.get("high_price"),
            "low_price": item.get("low_price"),
            "price_change": item.get("price_change"),
            "price_change_ratio_pct": item.get("price_change_ratio_pct"),
            "volume": item.get("volume"),
            "turnover": item.get("turnover"),
        }], raw_response=_payload_json(data))

    def _fetch_profile(self, thscode: str, api_key: str) -> FetchResult:
        path = "/api/fund/profile/detail"
        params = {"fund_type": "exchange", "thscode": thscode}
        data, error, conf = self._parse_envelope(path, params, api_key)
        if error:
            return self._make_empty_result(error)
        items = data.get("item") or []
        if not items:
            return self._make_result([], confidence=conf or "missing")
        item = items[0]
        return self._make_result([{
            "thscode": item.get("thscode") or thscode,
            "ticker": item.get("ticker"),
            "fund_name": item.get("fund_name"),
            "estab_date": _ms_to_date(item.get("estab_date")),
            "mgmt_name": item.get("mgmt_name"),
            "manager_name": item.get("manager_name"),
            "fund_scale": item.get("fund_scale"),
            "unit_nav": item.get("unit_nav"),
        }], raw_response=_payload_json(data))

    def _fetch_track_percentile(
        self, thscode: str, request: FetchRequest, api_key: str,
    ) -> FetchResult:
        start_ms, end_ms = self._window_ms(request)
        path = "/api/fund/performance/indicators-historical"
        params = {
            "fund_type": "exchange", "thscode": thscode,
            "start": start_ms, "end": end_ms,
        }
        data, error, conf = self._parse_envelope(path, params, api_key)
        if error:
            return self._make_empty_result(error)
        items = data.get("item") or []
        if not items:
            return self._make_result([], confidence=conf or "missing")
        rows = []
        for item in items:
            trade_date = _ms_to_date(item.get("date_ms"))
            if trade_date is None:
                continue
            rows.append({
                "thscode": thscode,
                "trade_date": trade_date,
                "track_index_pe_ttm_five_year_percentile": item.get(
                    "track_index_pe_ttm_five_year_percentile"
                ),
                "rsi_pct": item.get("rsi_pct"),
                "donchian_channel": item.get("donchian_channel"),
            })
        return self._make_result(rows, raw_response=_payload_json(data))

    def _window_ms(self, request: FetchRequest) -> tuple[int, int]:
        """历史端点窗口：默认近 1 年；thscode 端点最长 5 自然年由调用方约束。"""
        end = date.today()
        start = end - timedelta(days=365)
        if request.start_date:
            with contextlib.suppress(ValueError):
                start = date.fromisoformat(str(request.start_date)[:10])
        if request.end_date:
            with contextlib.suppress(ValueError):
                end = date.fromisoformat(str(request.end_date)[:10])
        end_ms = int(datetime(end.year, end.month, end.day, 23, 59, 59).timestamp() * 1000)
        start_ms = int(datetime(start.year, start.month, start.day).timestamp() * 1000)
        return start_ms, end_ms


def _payload_json(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
