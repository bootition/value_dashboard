"""东财股票回购/注销金额适配器（数据补全 2026-08-26）

数据源：东方财富股票回购列表 `ak.stock_repurchase_em()`，一次返回全市场历史
回购计划与已回购金额，适合低频全量同步，不适合逐股高频请求。

字段映射：
- 已回购金额（元）→ buyback_amount
- 已回购股份数量 → buyback_shares
- 回购开始时间 → start_date
- 最新公告日期 → announce_date
- 实施进度 → progress
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import date
from typing import Any

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

__all__ = ["EastMoneyRepurchaseAdapter"]

try:
    import akshare as ak

    _AKSHARE_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False


@contextlib.contextmanager
def _domestic_direct() -> Any:
    saved = {
        k: os.environ.get(k)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
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


def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value != value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> str | None:
    if value is None or value == "" or value != value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


class EastMoneyRepurchaseAdapter(BaseAdapter):
    """东财股票回购适配器（data_type=buyback_funding）。

    全市场一次性低频接口；调用方负责以全量替换方式写入 buyback_events。
    """

    def __init__(self, rate_limit: float = 0.0, timeout: float = 30.0) -> None:
        super().__init__(
            name="eastmoney_repurchase",  # type: ignore[arg-type]
            supported={"buyback_funding"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not _AKSHARE_AVAILABLE or ak is None:
            return self._make_empty_result("akshare 未安装")

        try:
            with _domestic_direct():
                df = ak.stock_repurchase_em()
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财回购列表抓取失败: %s", exc)
            return self._make_empty_result(f"{type(exc).__name__}: {exc}")

        if df is None or len(df) == 0:
            return self._make_result([], confidence="missing")

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            code = str(row.get("股票代码") or "").zfill(6)
            amount = _to_float(row.get("已回购金额"))
            if not code or amount is None or amount <= 0:
                continue
            rows.append({
                "stock_code": code,
                "event_type": "buyback",
                "start_date": _to_date(row.get("回购起始时间")),
                "announce_date": _to_date(row.get("最新公告日期")),
                "buyback_shares": _to_float(row.get("已回购股份数量")),
                "buyback_amount": amount,
                "progress": str(row.get("实施进度") or ""),
                "source": "eastmoney_repurchase",
                "confidence": "approximate",
            })

        raw = df.to_json(orient="records", force_ascii=False)
        return self._make_result(rows, raw_response=raw, confidence="approximate")
