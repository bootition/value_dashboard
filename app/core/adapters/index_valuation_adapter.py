"""指数估值适配器（2026-08-25 建域；2026-09-05 v21 扩展为多指数四源）

主源一 乐咕乐股（legulegu.com）`ak.stock_index_pe_lg` / `ak.stock_index_pb_lg`：
  12 个宽基/红利指数月末 PE/PB 序列（2005 年至今约 250+ 点，上游 2026-09
  由日度改为月度，如实披露）。PE 口径：滚动市盈率 = PE-TTM（pe_metric=ttm）。
主源二 申万研究（swsresearch）`ak.index_analysis_daily_sw(symbol="一级行业")`：
  31 个申万一级行业日度 PE/PB/股息率（2006 年至今可回填）。申万日报"市盈率"
  上游未注明 TTM/静态，落库 pe_metric=sws_daily，置信度 approximate，不混称 TTM。
交叉源 中证官网（csindex.com.cn）`ak.stock_zh_index_value_csindex`：
  近 20 交易日官方口径（市盈率1=PE-TTM / 股息率1=%），仅中证/上证系指数代码。

语义边界：
- 同指数同日期按 source 多行并存（index_valuation PK 含 source）。
- 均为低频源：乐咕单指数 2 次请求 / 申万单窗口 1 次请求，无风控风险。
- 国内源直连（_domestic_direct：本机 10808 代理常不可达，reports/61 纪律）。
- 指数代码归一：宽基用 6 位数字（000300）；申万行业用 SW+6 位（SW801010）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

__all__ = [
    "LeguleguIndexAdapter",
    "SwsIndexAdapter",
    "CSIndexIndexAdapter",
    "LEGULEGU_INDEX_CODES",
    "CSINDEX_COVERED_CODES",
]

# 可选依赖：akshare（模块级导入，便于测试 monkeypatch）
try:
    import akshare as ak

    _AKSHARE_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False

# 指数代码 → 乐咕接口符号（stock_index_pe_lg / stock_index_pb_lg 支持）
_LEGULEGU_SYMBOLS: dict[str, str] = {
    "000016": "上证50",
    "000300": "沪深300",
    "000009": "上证380",
    "399673": "创业板50",
    "000905": "中证500",
    "000010": "上证180",
    "399324": "深证红利",
    "399330": "深证100",
    "000852": "中证1000",
    "000015": "上证红利",
    "000903": "中证100",
    "000906": "中证800",
}

LEGULEGU_INDEX_CODES: tuple[str, ...] = tuple(_LEGULEGU_SYMBOLS)

# 中证官网交叉源仅覆盖中证/上证系指数（深证 399xxx 会 404，不做交叉）
CSINDEX_COVERED_CODES: frozenset[str] = frozenset({
    "000016", "000300", "000009", "000905", "000010",
    "000852", "000015", "000903", "000906",
})


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


def _cn_today() -> date:
    return date.today()


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
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _compact_extra(mapping: dict[str, Any]) -> str | None:
    """把附加字段序列化为 JSON 字符串；无附加字段时返回 None。"""
    cleaned = {k: v for k, v in mapping.items() if v is not None}
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), default=str)


class LeguleguIndexAdapter(BaseAdapter):
    """乐咕乐股指数估值主源（data_type=index_valuation）。

    通过 request.stock_codes[0] 指定指数代码（默认 000300）。
    PE（滚动市盈率=TTM）与 PB 各一次请求，按日期合并为单行；
    PB 失败不阻断 PE 落库（pb 保持 NULL，错误写入日志）。
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

        self._wait_rate_limit()
        try:
            with _domestic_direct():
                pe_df = ak.stock_index_pe_lg(symbol=symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("legulegu 指数 %s PE 抓取失败: %s", index_code, e)
            return self._make_empty_result(f"{type(e).__name__}: {e}")

        if pe_df is None or len(pe_df) == 0:
            return self._make_result([], confidence="missing")  # 合法缺失：error=None 不触发熔断

        # PB 为补充请求：失败不阻断 PE 主链（单独排队限速，避免同指数双请求连发）
        pb_by_date: dict[str, dict[str, Any]] = {}
        pb_raw: str | None = None
        try:
            self._wait_rate_limit()
            with _domestic_direct():
                pb_df = ak.stock_index_pb_lg(symbol=symbol)
            if pb_df is not None and len(pb_df) > 0:
                pb_raw = pb_df.to_json(orient="records", force_ascii=False)
                for _, row in pb_df.iterrows():
                    d = _to_date(row.get("日期"))
                    if d is not None:
                        pb_by_date[d] = {
                            "pb": _to_float(row.get("市净率")),
                            "pb_weighted": _to_float(row.get("加权市净率")),
                            "pb_median": _to_float(row.get("市净率中位数")),
                        }
        except Exception as e:  # noqa: BLE001
            logger.warning("legulegu 指数 %s PB 抓取失败(非致命): %s", index_code, e)

        rows: list[dict[str, Any]] = []
        for _, row in pe_df.iterrows():
            trade_date = _to_date(row.get("日期"))
            if trade_date is None:
                continue
            pb_extra = pb_by_date.get(trade_date, {})
            rows.append({
                "index_code": index_code,
                "trade_date": trade_date,
                "pe_ttm": _to_float(row.get("滚动市盈率")),  # 滚动市盈率 = PE-TTM
                "pe_metric": "ttm",
                "pb": pb_extra.get("pb"),
                "div_yield": None,
                "extra": _compact_extra({
                    "index_close": _to_float(row.get("指数")),
                    "static_pe": _to_float(row.get("静态市盈率")),
                    "weighted_ttm_pe": _to_float(row.get("加权滚动市盈率")),
                    "median_ttm_pe": _to_float(row.get("滚动市盈率中位数")),
                    "median_static_pe": _to_float(row.get("静态市盈率中位数")),
                    "pb_weighted": pb_extra.get("pb_weighted"),
                    "pb_median": pb_extra.get("pb_median"),
                }),
            })
        if not rows:
            return self._make_empty_result("no_valid_rows", confidence="missing")

        raw = json.dumps(
            {
                "pe": pe_df.to_json(orient="records", force_ascii=False),
                "pb": pb_raw,
            },
            ensure_ascii=False,
        )
        return self._make_result(rows, raw_response=raw, confidence="approximate")


class SwsIndexAdapter(BaseAdapter):
    """申万研究指数分析日报适配器（data_type=index_valuation，source=sws）。

    直连 swsresearch API 并按 page_size=50000 分页（akshare 原接口固定 50/页，
    2026-09-05 实测全量 2372 页×约15s≈10小时；50000/页时仅 3 页，
    3 并发实测墙钟约 56s，单页最长约 55s）。请求日期无数据按合法 missing
    处理，不触发熔断；其余异常按 error 返回。
    """

    _URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/"
    _PAGE_SIZE = 50000
    _MAX_WORKERS = 3

    def __init__(self, rate_limit: float = 0.5, timeout: float = 90.0) -> None:
        super().__init__(
            name="sws",  # type: ignore[arg-type]
            supported={"index_valuation"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    def _request_json(self, params: dict[str, Any]) -> dict[str, Any]:
        """单页请求（独立方法便于测试 monkeypatch）。"""
        import httpx

        with _domestic_direct():
            response = httpx.get(
                self._URL,
                params=params,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
                    )
                },
                timeout=self._timeout,
                trust_env=False,
                verify=False,  # swsresearch 证书链不完整，akshare 原实现同样 verify=False
            )
        response.raise_for_status()
        return response.json()

    def _fetch_pages(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        def params_for(page: int) -> dict[str, Any]:
            return {
                "page": str(page),
                "page_size": str(self._PAGE_SIZE),
                "index_type": "一级行业",
                "start_date": f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}",
                "end_date": f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}",
                "type": "DAY",
                "swindexcode": "all",
            }

        # 首页顺带拿到 count，据此确定总页数；剩余页并发抓取。
        # 服务器单页 50000 行约 26-55s（2026-09-05 实测），3 并发墙钟约 56s。
        self._wait_rate_limit()
        first_payload = self._request_json(params_for(1))
        first_data = first_payload.get("data") or {}
        first_rows = first_data.get("results") or []
        count = int(first_data.get("count") or len(first_rows))
        total_pages = max(1, (count + self._PAGE_SIZE - 1) // self._PAGE_SIZE)

        pages: dict[int, list[dict[str, Any]]] = {1: first_rows}
        if total_pages > 1:
            def fetch_page(page: int) -> tuple[int, list[dict[str, Any]]]:
                self._wait_rate_limit()
                payload = self._request_json(params_for(page))
                data = payload.get("data") or {}
                return page, data.get("results") or []

            with ThreadPoolExecutor(max_workers=min(self._MAX_WORKERS, total_pages - 1)) as pool:
                for page, rows in pool.map(fetch_page, range(2, total_pages + 1)):
                    pages[page] = rows

        rows: list[dict[str, Any]] = []
        for page in sorted(pages):
            rows.extend(pages[page])
        return rows

    def fetch(self, request: FetchRequest) -> FetchResult:
        start_date = (request.start_date or (_cn_today() - timedelta(days=30)).isoformat()).replace("-", "")
        end_date = (request.end_date or _cn_today().isoformat()).replace("-", "")
        try:
            raw_rows = self._fetch_pages(start_date, end_date)
        except Exception as e:  # noqa: BLE001
            logger.warning("sws 行业指数 %s~%s 抓取失败: %s", start_date, end_date, e)
            return self._make_empty_result(f"{type(e).__name__}: {e}")

        if not raw_rows:
            # 非交易日/无数据窗口 = 合法缺失
            return self._make_result([], confidence="missing")

        rows: list[dict[str, Any]] = []
        for row in raw_rows:
            raw_code = str(row.get("swindexcode", "")).split(".")[0]
            trade_date = _to_date(row.get("bargaindate"))
            if not raw_code or trade_date is None:
                continue
            rows.append({
                "index_code": f"SW{raw_code}",
                "trade_date": trade_date,
                "pe_ttm": _to_float(row.get("pe")),
                "pe_metric": "sws_daily",  # 申万日报口径，上游未注明 TTM/静态
                "pb": _to_float(row.get("pb")),
                "div_yield": _to_float(row.get("dp")),
                "extra": _compact_extra({
                    "index_close": _to_float(row.get("closeindex")),
                    "pct_change": _to_float(row.get("markup")),
                    "turnover_rate": _to_float(row.get("turnoverrate")),
                    "amount_yi": _to_float(row.get("bargainamount")),
                    "float_mv_yi": _to_float(row.get("negotiablesharesum1")),
                }),
            })
        if not rows:
            return self._make_empty_result("no_valid_rows", confidence="missing")

        raw = json.dumps(raw_rows, ensure_ascii=False, default=str)
        return self._make_result(rows, raw_response=raw, confidence="approximate")


class CSIndexIndexAdapter(BaseAdapter):
    """中证指数官网交叉源（data_type=index_valuation）。

    返回近 20 交易日官方口径估值（市盈率1=PE-TTM / 股息率1=%），
    供 index_valuation 域与主源同日期交叉核验；该接口无 PB。
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
        if index_code not in CSINDEX_COVERED_CODES:
            # 非中证/上证系指数（如深证 399xxx）无交叉源：合法缺失，不触发熔断
            return self._make_result([], confidence="missing")
        # 中证官网接口要求 6 位指数代码（如 000300；截为 300 会 404，2026-08-25 实测）
        csindex_symbol = index_code
        if not _AKSHARE_AVAILABLE or ak is None:
            return self._make_empty_result("akshare 未安装")

        self._wait_rate_limit()
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
                "pe_metric": "ttm",
                "pb": None,  # 中证官网该接口无 PB 列
                "div_yield": _to_float(row.get("股息率1")),
                "extra": _compact_extra({
                    "pe2": _to_float(row.get("市盈率2")),
                    "div_yield2": _to_float(row.get("股息率2")),
                    "index_short_name": str(row.get("指数中文简称") or "") or None,
                }),
            })
        if not rows:
            return self._make_empty_result("no_valid_rows", confidence="missing")

        raw = df.to_json(orient="records", force_ascii=False)
        return self._make_result(rows, raw_response=raw, confidence="approximate")
