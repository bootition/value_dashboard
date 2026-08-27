"""指数估值适配器（数据补全 2026-08-25，沪深300 ERP 数据前置）

主源：乐咕乐股（legulegu.com）`ak.stock_index_pe_lg` —— 全历史 PE-TTM/PB/股息率
      （沪深300 自 2005-04-08 起约 5000+ 交易日，ERP 长历史计算所需）。
交叉源：中证指数官网（csindex.com.cn）`ak.stock_zh_index_value_csindex` ——
      近 20 交易日官方口径（市盈率1=PE-TTM / 股息率1），每日 1 次交叉核验。

语义边界：
- 两个源同日期双行并存（index_valuation PK 含 source），主源优先、交叉披露。
- 均为低频源：单次调用返回全部可得历史，全量更新=每日 1~2 次请求，无风控风险。
- 国内源直连（同 cninfo_funding 的 _domestic_direct 语义）。
- 指数代码归一：000300（沪深300）；symbol 映射见 _SYMBOL 表。
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

__all__ = ["LeguleguIndexAdapter", "CSIndexIndexAdapter"]

# 可选依赖：akshare（模块级导入，便于测试 monkeypatch）
try:
    import akshare as ak

    _AKSHARE_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False

# 指数中文名 → 乐咕接口符号（stock_index_pe_lg 支持）
_LEGULEGU_SYMBOLS: dict[str, str] = {
    "000300": "沪深300",
    "000905": "中证500",
    "000016": "上证50",
}


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


class LeguleguIndexAdapter(BaseAdapter):
    """乐咕乐股指数估值主源（data_type=index_valuation）。

    通过 request.stock_codes[0] 指定指数代码（默认 000300）。
    """

    def __init__(self, rate_limit: float = 1.0, timeout: float = 20.0) -> None:
        super().__init__(
            name="legulegu",  # type: ignore[arg-type]
            supported={"index_valuation"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    def fetch(self, request: FetchRequest) -> FetchResult:
        index_code = (request.stock_codes or ["000300"])[0]
        symbol = _LEGULEGU_SYMBOLS.get(index_code)
        if symbol is None:
            # 不支持的指数=合法缺失：error=None 不触发熔断
            return self._make_result([], confidence="missing")
        if not _AKSHARE_AVAILABLE or ak is None:
            return self._make_empty_result("akshare 未安装")

        try:
            with _domestic_direct():
                df = ak.stock_index_pe_lg(symbol=symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("legulegu 指数 %s 抓取失败: %s", index_code, e)
            return self._make_empty_result(f"{type(e).__name__}: {e}")

        if df is None or len(df) == 0:
            return self._make_result([], confidence="missing")  # 合法缺失：error=None 不触发熔断

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            date_val = _to_date(row.get("日期"))
            if date_val is None:
                continue
            rows.append({
                "index_code": index_code,
                "trade_date": date_val,
                "pe_ttm": _to_float(row.get("滚动市盈率")),  # 滚动市盈率 = PE-TTM
                "pb": None,  # 乐咕 stock_index_pe_lg 无 PB 列，如实置空
                "div_yield": None,
                "extra": {
                    "静态市盈率": _to_float(row.get("静态市盈率")),
                    "等权滚动市盈率": _to_float(row.get("等权滚动市盈率")),
                    "指数点位": _to_float(row.get("指数")),
                },
            })
        if not rows:
            return self._make_empty_result("no_valid_rows", confidence="missing")

        raw = df.to_json(orient="records", force_ascii=False)
        return self._make_result(rows, raw_response=raw, confidence="approximate")


class CSIndexIndexAdapter(BaseAdapter):
    """中证指数官网交叉源（data_type=index_valuation）。

    返回近 20 交易日官方口径估值（市盈率1=PE-TTM / 股息率1=%），
    供 index_valuation 域与主源同日期交叉核验。
    """

    def __init__(self, rate_limit: float = 1.0, timeout: float = 20.0) -> None:
        super().__init__(
            name="csindex",  # type: ignore[arg-type]
            supported={"index_valuation"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    def fetch(self, request: FetchRequest) -> FetchResult:
        index_code = (request.stock_codes or ["000300"])[0]
        # 中证官网接口要求 6 位指数代码（如 000300；截为 300 会 404，2026-08-25 实测）
        csindex_symbol = index_code
        if not _AKSHARE_AVAILABLE or ak is None:
            return self._make_empty_result("akshare 未安装")

        try:
            with _domestic_direct():
                df = ak.stock_zh_index_value_csindex(symbol=csindex_symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("csindex 指数 %s 抓取失败: %s", index_code, e)
            return self._make_empty_result(f"{type(e).__name__}: {e}")

        if df is None or len(df) == 0:
            return self._make_result([], confidence="missing")  # 合法缺失：error=None 不触发熔断

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            date_val = _to_date(row.get("日期"))
            if date_val is None:
                continue
            rows.append({
                "index_code": index_code,
                "trade_date": date_val,
                "pe_ttm": _to_float(row.get("市盈率1")),
                "pb": None,  # 中证官网该接口无 PB 列
                "div_yield": _to_float(row.get("股息率1")),
                "extra": {
                    "市盈率2": _to_float(row.get("市盈率2")),
                    "股息率2": _to_float(row.get("股息率2")),
                    "指数简称": str(row.get("指数中文简称") or ""),
                },
            })
        if not rows:
            return self._make_empty_result("no_valid_rows", confidence="missing")

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
