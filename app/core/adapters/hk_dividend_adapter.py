"""东财港股分红历史适配器（2026-09-04，总市场分红融资比数据前置）

数据源：akshare `stock_hk_dividend_payout_em`，底层为东方财富
datacenter.eastmoney.com/securities/api/data/v1/get（不是 push2/push2his）。

约束：
- 单股接口，输入 5 位港股代码（如 00941）；
- 源侧硬限速 ≤2 req/s：BaseAdapter rate_limit=0.5s/请求；
- 国内源直连，调用前临时禁用 HTTP(S)_PROXY；
- 港股 IPO/配股/供股等融资事件不在本适配器范围，未采集部分绝不伪造。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from datetime import date
from typing import Any

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

__all__ = ["EastMoneyHKDividendAdapter", "parse_hk_plan_explain"]

# 可选依赖：akshare（模块级导入，便于测试 monkeypatch）
try:
    import akshare as ak

    _AKSHARE_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False

# 当前 akshare 1.18.81 列名 / 用户任务书列名都兼容；
# 分红方案是解析每股 HKD/CNY 股息的唯一机器可读字段。
_COLUMN_ALIASES = {
    "announcement_date": ("最新公告日期", "公告日期"),
    "report_period": ("财政年度", "报告期"),
    "plan_explain": ("分红方案", "方案说明"),
    "ex_date": ("除净日", "除权除息日"),
    "transfer_end_date": ("截至过户日", "股权登记日-股权登记日截止"),
    "dividend_date": ("发放日", "派息日"),
}

_PRIMARY_AMOUNT_RE = re.compile(
    r"(?:每股派息|每股派|每股股息|每股现金股息|每股)"
    r"(人民币|港币|港元)?\s*([0-9]+(?:\.[0-9]+)?)\s*元"
)
_PAREN_CNY_RE = re.compile(r"人民币\s*([0-9]+(?:\.[0-9]+)?)\s*元")
_PAREN_HKD_RE = re.compile(r"(?:港币|港元)\s*([0-9]+(?:\.[0-9]+)?)\s*元")


@contextlib.contextmanager
def _domestic_direct() -> Any:
    """临时禁用 HTTP(S) 代理（国内源直连），退出时恢复。"""
    saved = {
        key: os.environ.get(key)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    for key in saved:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value != value:  # NaN
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> str | None:
    if value is None or value == "" or value != value:  # NaN
        return None
    text = str(value).strip()[:10].replace("/", "-")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _to_text(value: Any) -> str | None:
    if value is None or value != value:  # NaN
        return None
    text = str(value).strip()
    return text or None


def _first_value(row: Any, *keys: str) -> Any:
    """兼容源返回的 dict 或 pandas Series/row。"""
    for key in keys:
        if isinstance(row, dict):
            if key in row:
                return row.get(key)
        else:
            try:
                return row.get(key)
            except (AttributeError, KeyError):
                continue
    return None


def parse_hk_plan_explain(plan_explain: str | None) -> tuple[float | None, float | None]:
    """Parse an Eastmoney HK dividend plan string into (hkd, cny) per share.

    Recognized shapes（不满足时返回 (None, None)，绝不猜数）：
    - 每股派息2.51元(相当于港币2.9003元)         → (2.9003, 2.51)
    - 每股派息港币2.4元                          → (2.4, None)
    - 每股派人民币2.51元(相当于港币2.9003元)     → (2.9003, 2.51)
    - 每股派港币1.582元(相当于人民币1.322元)     → (1.582, 1.322)
    """
    if not plan_explain:
        return None, None
    text = str(plan_explain).strip()
    primary = _PRIMARY_AMOUNT_RE.search(text)
    if primary is None:
        return None, None

    currency = primary.group(1) or "人民币"
    amount = _to_float(primary.group(2))
    if amount is None:
        return None, None

    hkd: float | None = None
    cny: float | None = None
    if currency in {"港币", "港元"}:
        hkd = amount
    else:
        cny = amount

    if hkd is None:
        match = _PAREN_HKD_RE.search(text)
        if match is not None:
            hkd = _to_float(match.group(1))
    if cny is None:
        match = _PAREN_CNY_RE.search(text)
        if match is not None:
            cny = _to_float(match.group(1))
    return hkd, cny


class EastMoneyHKDividendAdapter(BaseAdapter):
    """东财港股分红适配器（data_type=hk_dividends）。

    单股串行抓取；请求间隔由 BaseAdapter._wait_rate_limit() 保证，
    默认 0.5s（即源侧 ≤2 req/s）。
    """

    def __init__(self, rate_limit: float = 0.5) -> None:
        super().__init__(
            name="eastmoney_hk_dividend",  # type: ignore[arg-type]
            supported={"hk_dividends"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result("hk_dividends 需要 stock_codes（港股代码）")
        if not _AKSHARE_AVAILABLE or ak is None:
            return self._make_empty_result("akshare 未安装")

        raw_code = str(request.stock_codes[0]).strip()
        hk_code = raw_code.zfill(5)
        if not re.fullmatch(r"[0-9]{4,5}", hk_code):
            return self._make_empty_result(f"无效港股代码: {raw_code!r}")

        self._wait_rate_limit()
        started = time.monotonic()
        try:
            with _domestic_direct():
                df = ak.stock_hk_dividend_payout_em(symbol=hk_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("港股分红 %s 抓取失败: %s", hk_code, exc)
            return self._make_empty_result(f"{type(exc).__name__}: {exc}")
        finally:
            self.record_response_duration(time.monotonic() - started)

        if df is None or len(df) == 0:
            # 合法空（无分红记录）与网络错误区分：error=None 不触发熔断。
            return self._make_result([], confidence="missing")

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            plan_explain = _to_text(_first_value(row, *_COLUMN_ALIASES["plan_explain"]))
            hkd, cny = parse_hk_plan_explain(plan_explain)
            rows.append({
                "stock_code": hk_code,
                "announcement_date": _to_date(_first_value(
                    row, *_COLUMN_ALIASES["announcement_date"],
                )),
                "report_period": _to_text(_first_value(
                    row, *_COLUMN_ALIASES["report_period"],
                )),
                "plan_explain": plan_explain or "",
                "ex_date": _to_date(_first_value(row, *_COLUMN_ALIASES["ex_date"])),
                "transfer_end_date": _to_text(_first_value(
                    row, *_COLUMN_ALIASES["transfer_end_date"],
                )),
                "dividend_date": _to_date(_first_value(
                    row, *_COLUMN_ALIASES["dividend_date"],
                )),
                "dividend_per_share_hkd": hkd,
                "dividend_per_share_cny": cny,
            })

        raw = df.to_json(orient="records", force_ascii=False)
        return self._make_result(rows, raw_response=raw, confidence="approximate")
