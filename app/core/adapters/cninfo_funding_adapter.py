"""CNINFO IPO 首发募资适配器（数据补全 2026-08-25）

数据源：巨潮资讯（CNINFO）IPO 发行汇总，经 akshare 封装
（`ak.stock_ipo_summary_cninfo`，cninfo.com.cn 合规源，reports/61 确认可用）。

语义边界：
- 单股接口，一次返回该股历史上唯一 IPO 发行要素（招股日期/上市日期/
  发行价/发行数量/募集资金净额/发行费用）。
- 募集资金净额缺失时如实 null，不推算、不伪造。
- 单位归一化：发行数量 万股→股（×1e4），金额 万元→元（×1e4）。
- 北交所股票若 CNINFO 无记录 → 合法空（missing），不触发熔断。
- 国内源直连：调用前临时禁用 HTTP(S)_PROXY（本机代理常指向不可达的
  127.0.0.1:10808，直连才是正确路径；限速器保证本域串行，竞态可接受）。
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

__all__ = ["CNINFOFundingAdapter"]

# 可选依赖：akshare（模块级导入，便于测试 monkeypatch）
try:
    import akshare as ak

    _AKSHARE_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False


@contextlib.contextmanager
def _domestic_direct() -> Any:
    """临时禁用 HTTP(S) 代理（国内源直连），退出时恢复。"""
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


class CNINFOFundingAdapter(BaseAdapter):
    """CNINFO IPO 首发募资适配器（data_type=ipo_funding）。

    独立限速实例（默认 1.5s/请求，对齐 cninfo_capital），
    与其他数据源不共享请求间隔，防止慢源拖累价格链。
    """

    def __init__(self, rate_limit: float = 1.5, timeout: float = 20.0) -> None:
        super().__init__(
            name="cninfo_funding",  # type: ignore[arg-type]
            supported={"ipo_funding"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    # ─── 协议方法 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result("ipo_funding 需要 stock_codes")
        code = request.stock_codes[0]
        if not _AKSHARE_AVAILABLE or ak is None:
            return self._make_empty_result("akshare 未安装")

        try:
            with _domestic_direct():
                df = ak.stock_ipo_summary_cninfo(symbol=code)
        except IndexError:
            # akshare 自身缺陷：源无记录（如部分北交所）时 records[0] 越界。
            # 这是"源无记录"的确定性信号 → 合法缺失，不触发熔断、不登记 retry。
            logger.info("CNINFO IPO %s 无发行记录（akshare IndexError，按合法缺失）", code)
            return self._make_result([], confidence="missing")
        except Exception as e:  # noqa: BLE001
            logger.warning("CNINFO IPO %s 抓取失败: %s", code, e)
            return self._make_empty_result(f"{type(e).__name__}: {e}")

        if df is None or len(df) == 0:
            # 合法缺失（如北交所无 IPO 汇总）：error=None 不触发熔断（manager P1-27）
            return self._make_result([], confidence="missing")

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            shares_raw = _to_float(row.get("总发行数量"))
            shares = shares_raw * 1e4 if shares_raw is not None else None  # 万股→股
            net_raw = _to_float(row.get("募集资金净额"))
            net = net_raw * 1e4 if net_raw is not None else None  # 万元→元
            rows.append({
                "stock_code": str(row.get("股票代码") or code).zfill(6),
                "event_type": "ipo",
                "announce_date": _to_date(row.get("招股公告日期")),
                "list_date": _to_date(row.get("上市日期")),
                "issue_price": _to_float(row.get("发行价格")),
                "issue_shares": shares,
                "raise_funds": None,  # CNINFO 仅净额，总额不伪造
                "raise_funds_net": net,
                "derived": False,
                "extra": {
                    "面值": _to_float(row.get("每股面值")),
                    "发行费用总额_万元": _to_float(row.get("发行费用总额")),
                    "摊薄发行市盈率": _to_float(row.get("摊薄发行市盈率")),
                    "主承销商": str(row.get("主承销商") or ""),
                },
            })

        raw = df.to_json(orient="records", force_ascii=False)
        return self._make_result(rows, raw_response=raw, confidence="approximate")


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
    s = str(value)[:10]
    try:
        from datetime import date

        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None
