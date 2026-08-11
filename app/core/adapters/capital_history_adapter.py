"""历史总股本链适配器（P4，reports/68 §3）

主链：CNINFO p_stock2215（akshare stock_share_change_cninfo）
- 半年报/年报期末总股本锚点 + 变动事件（变动日期/变动原因）
- 总股本单位为万股，适配器统一换算为股
- 20 股事件样本探测（2026-08-10）全量可得，事件类型覆盖送转、增发、回购、
  期权行权、限制性股票、可转债转股、A+H 发行

交叉校验：东财 F10 RPT_F10_EH_EQUITY（akshare stock_zh_a_gbjg_em）
- 仅事件记录（变动日期/变动原因），无定期锚点；偶发风控返回空（需重试冷却）
- 只作交叉校验，绝不单独决定历史总股本（探测已确认其记录集与主链不一致）
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Any

import akshare as ak

from app.core.adapters.base import (
    BaseAdapter,
    FetchRequest,
    FetchResult,
)

logger = logging.getLogger(__name__)

__all__ = ["CapitalHistoryAdapter"]

WAN_TO_SHARE = 10_000.0  # CNINFO 总股本单位：万股 → 股


def _parse_date(value: Any) -> str | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


class CapitalHistoryAdapter(BaseAdapter):
    """历史总股本主链适配器（cninfo_capital）。

    fetch(data_type="share_capital_history", stock_codes=[code])：
    - 默认返回 CNINFO 主链记录（锚点 + 事件）
    - extra_params={"cross_source": "eastmoney"} 时返回东财 F10 交叉校验事件
    """

    def __init__(self, rate_limit: float = 1.5, timeout: float = 30.0) -> None:
        super().__init__(
            name="cninfo_capital",  # type: ignore[arg-type]
            supported={"share_capital_history"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout
        self._cross_rate_limit = 0.6  # 东财交叉源独立限速
        self._rate_limit_lock = threading.Lock()
        self._last_request_times: dict[str, float] = {}

    # ─── 协议方法 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        if request.data_type != "share_capital_history":
            return self._make_empty_result(
                reason=f"历史股本适配器不支持的数据类型: {request.data_type}",
                confidence="missing",
            )
        codes = request.stock_codes
        if not codes:
            return self._make_empty_result(
                reason="share_capital_history 需要股票代码列表（逐股查询接口）",
                confidence="missing",
            )

        cross = bool(request.extra_params.get("cross_source") == "eastmoney")
        all_data: list[dict[str, Any]] = []
        errors: list[str] = []
        for code in codes:
            try:
                if cross:
                    self._wait_rate_limit_interval(self._cross_rate_limit)
                    rows = self._fetch_eastmoney(code)
                else:
                    self._wait_rate_limit_interval(self._rate_limit)
                    rows = self._fetch_cninfo(code)
                all_data.extend(rows)
            except Exception as error:
                logger.warning("查询 %s 历史股本%s异常: %s",
                               code, "（东财交叉）" if cross else "", error)
                errors.append(f"{code}: {error}")

        if errors:
            return self._make_result(
                data=all_data,
                raw_response=None,
                confidence="approximate",
                error="; ".join(errors),
                api_version="v1",
            )
        if not all_data:
            return self._make_empty_result(
                reason="share_capital_history 无可用数据（空响应或源不可用）",
                confidence="missing",
            )
        return self._make_result(
            data=all_data,
            raw_response=None,
            confidence="strict",
            api_version="v1",
        )

    def _wait_rate_limit_interval(self, interval: float) -> None:
        with self._rate_limit_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_times.get(interval, 0.0)
            if elapsed < interval:
                time.sleep(interval - elapsed)
            self._last_request_times[interval] = time.monotonic()

    # ─── CNINFO 主链 ──────────────────────────────────────────────

    def _fetch_cninfo(self, stock_code: str) -> list[dict[str, Any]]:
        """CNINFO p_stock2215：锚点（半年报/年报期末）+ 变动事件。"""
        df = ak.stock_share_change_cninfo(symbol=stock_code)
        rows: list[dict[str, Any]] = []
        if df is None or df.empty:
            return rows
        for _, record in df.iterrows():
            effective = _parse_date(record.get("变动日期"))
            if not effective:
                continue
            try:
                total = float(record.get("总股本"))
            except (TypeError, ValueError):
                continue
            if total <= 0:
                continue
            reason = str(record.get("变动原因简称") or "").strip()
            rows.append({
                "stock_code": stock_code,
                "effective_date": effective,
                "total_shares": total * WAN_TO_SHARE,
                "change_reason": reason or None,
                "is_anchor": not reason,  # 无变动原因 → 定期报告期末锚点
            })
        rows.sort(key=lambda r: r["effective_date"])
        return rows

    # ─── 东财 F10 交叉校验 ────────────────────────────────────────

    def _fetch_eastmoney(self, stock_code: str) -> list[dict[str, Any]]:
        """东财 RPT_F10_EH_EQUITY：变动事件（仅交叉校验用）。

        偶发风控返回空 DataFrame：按缺失处理（不抛错），由回填逻辑保留主链。
        """
        df = ak.stock_zh_a_gbjg_em(symbol=stock_code)
        rows: list[dict[str, Any]] = []
        if df is None or df.empty:
            return rows
        for _, record in df.iterrows():
            effective = _parse_date(record.get("变动日期"))
            if not effective:
                continue
            try:
                total = float(record.get("总股本"))
            except (TypeError, ValueError):
                continue
            if total <= 0:
                continue
            reason = str(record.get("变动原因") or "").strip()
            rows.append({
                "stock_code": stock_code,
                "effective_date": effective,
                "total_shares": total,
                "change_reason": reason or None,
                "is_anchor": False,
                "cross_source": "eastmoney_f10",
            })
        rows.sort(key=lambda r: r["effective_date"])
        return rows
