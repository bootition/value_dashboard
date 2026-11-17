
"""港股日行情 + HKD/CNY 汇率适配器（reports/122 §2.7 J12 数据层）。

为什么需要这个模块
------------------
`hk_price_daily` / `fx_rate_daily` 是 A+H 市值拆分与 AH 溢价率/股息率的两个
外部输入。本模块只负责**取数 + 列名归一**，落库、断点续传、单写者锁由
`scripts/fetch_hk_prices.py` / `scripts/fetch_fx_rates.py` 负责。

港股日线（不复权）
------------------
主接口（任务书建议）：

    ak.stock_hk_hist(symbol="01398", period="daily",
                     start_date="20260101", end_date="20261114", adjust="")

东财港股日线。akshare 1.18.81 源码中的返回列：
`日期 / 开盘 / 收盘 / 最高 / 最低 / 成交量 / 成交额 / 振幅 / 涨跌幅 /
涨跌额 / 换手率`。

备用接口（本机实测可用）：

    ak.stock_hk_daily(symbol="01398", adjust="")

新浪港股历史，返回 `date / open / high / low / close / volume / amount`
（实测 4,936 行，覆盖 2006-11 → 2026-11-13）。**本机东财 push2his 域名
2026-11-15 实测 3/3 `RemoteDisconnected`，因此脚本默认 auto 模式：主源失败
自动降级备用源，并在证据 JSON 的 `upstream` 字段如实记录用了哪个源。**
两个源都失败时返回空 rows + error，绝不伪造价格。

汇率（HKD/CNY）
---------------
    ak.currency_boc_sina(symbol="港币", start_date="20000101", end_date="20261114")

新浪-中国银行人民币牌价历史，一次请求返回全部历史。akshare 1.18.81 返回列：
`日期 / 中行汇买价 / 中行钞买价 / 中行钞卖价/汇卖价 / 央行中间价 / 中行折算价`。

⚠️ **单位陷阱**：该接口报价单位是「人民币元 / 100 港元」（实测 2026-11-13
中行折算价 85.604，即 0.85604 元/港元），落库前必须 ÷100。本模块按量级
自动判定（>10 视为每百港元），并在结果里返回每行实际使用的牌价列名。

口径选择：`中行折算价` 优先（PRODUCT_GAPS §1.3 G2 指定唯一口径），缺失时
回退 `央行中间价`；不使用钞买/钞卖价（零售牌价含价差噪音）。
"""

from __future__ import annotations

import logging
import math
import numbers
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "FX_RATE_SOURCE",
    "HKD_CNY_PAIR",
    "HK_PRICE_SOURCE",
    "FxHistoryFetch",
    "HkPriceFetch",
    "fetch_hk_daily",
    "fetch_hkd_cny_history",
    "to_date",
]

# 落库 source 字段（任务书约定）
HK_PRICE_SOURCE = "akshare_hk"
FX_RATE_SOURCE = "boc_sina"
HKD_CNY_PAIR = "HKDCNY"

# akshare 港股日线候选列名（主源中文、备用源英文）
_DATE_COLUMNS = ("日期", "date", "trade_date", "时间")
_CLOSE_COLUMNS = ("收盘", "close", "收盘价", "最新价")

# BOC 牌价列：折算价优先，央行中间价兜底
_FX_MID_COLUMNS = ("中行折算价", "央行中间价")

# 1 港元兑人民币的合理区间（用于识别「每百港元」未除 100 的事故）
_FX_SANE_MIN = 0.5
_FX_SANE_MAX = 2.0


@dataclass(frozen=True)
class HkPriceFetch:
    """单只港股日线抓取结果（归一化后）。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    upstream: str = "none"
    error: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class FxHistoryFetch:
    """HKD/CNY 历史汇率抓取结果（rate 已换算为 元/港元）。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    upstream: str = "none"
    error: str | None = None
    note: str | None = None
    mid_source_counts: dict[str, int] = field(default_factory=dict)


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ImportError as error:  # pragma: no cover - 依赖已随 release 安装
        raise RuntimeError("akshare 未安装，无法抓取港股行情/汇率") from error
    return ak


def to_date(value: Any) -> date | None:
    """把 akshare 返回的日期（str / datetime / pandas Timestamp / epoch ms）归一。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        # 8 位整数是 YYYYMMDD；10/13 位是 epoch 秒/毫秒
        if numeric >= 19_000_101 and numeric <= 21_001_231 and float(numeric).is_integer():
            text = str(int(numeric))
            try:
                return datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                return None
        if numeric > 1e11:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    head = text[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _pick_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in columns:
            return name
    return None


def _normalize_daily_frame(
    frame: Any,
    *,
    start: date | None,
    end: date | None,
    upstream: str,
) -> list[dict[str, Any]]:
    """DataFrame → [{trade_date, close}]，过滤空值/非正价格/区间外日期。"""
    if frame is None or getattr(frame, "empty", True):
        return []
    columns = [str(column) for column in frame.columns]
    date_column = _pick_column(columns, _DATE_COLUMNS)
    close_column = _pick_column(columns, _CLOSE_COLUMNS)
    if date_column is None or close_column is None:
        raise ValueError(
            f"{upstream} 返回列不含日期/收盘列: {columns}"
        )
    rows_by_date: dict[date, dict[str, Any]] = {}
    for raw_date, raw_close in zip(
        frame[date_column].tolist(), frame[close_column].tolist(), strict=False,
    ):
        trade_date = to_date(raw_date)
        close = _finite_float(raw_close)
        if trade_date is None or close is None or close <= 0:
            continue
        if start is not None and trade_date < start:
            continue
        if end is not None and trade_date > end:
            continue
        rows_by_date[trade_date] = {
            "trade_date": trade_date,
            "close": close,
            "upstream": upstream,
        }
    return [rows_by_date[key] for key in sorted(rows_by_date)]


_TENCENT_HK_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
_TENCENT_MAX_BARS = 1500


def _fetch_tencent_hk_daily(
    code: str,
    *,
    start: date | None,
    end: date | None,
) -> list[dict[str, Any]]:
    """腾讯港股日线（reports/121 增补的**主源**）。

    为什么加它
    ----------
    2026-11-15 实测：`ak.stock_hk_hist`（东财 push2his）在本机 3/3 断连；
    `ak.stock_hk_daily`（新浪）可用但**每只返回全历史、约 50 秒/只**——
    203 只要跑近 3 小时，不可接受。腾讯 `hkfqkline` 单请求 0.36 秒返回
    1,500 根日线（约 6 年），203 只约 2 分钟。

    口径限制（必须知道）
    -------------------
    腾讯港股只提供 `qfq`（前复权）序列，不支持不复权。**前复权序列的最后一根
    等于真实收盘价**，因此用它算"当前"市值/AH 溢价/H 股息率是正确的；但历史
    某一天的 qfq 值 ≠ 当天真实价，不能拿它算历史时点市值。落库时 source 记
    `tencent_qfq`，下游按 source 区分。
    """
    response = httpx.get(
        _TENCENT_HK_KLINE,
        params={"param": f"hk{code},day,,,{_TENCENT_MAX_BARS},qfq"},
        timeout=30.0,
        headers={"User-Agent": "Mozilla/5.0"},
        # 代理路由**刻意保持 httpx 默认**（trust_env=True）：原先靠临时清空进程环境变量
        # （`_domestic_direct`）实现直连，reports/136 §5.1 实测它从未生效，该包装器已删除；
        # 是否改为 trust_env=False 属独立决策，需真机探针验证，见 reports/137 §3.3。
    )
    response.raise_for_status()
    payload = response.json()
    block = (payload.get("data") or {}).get(f"hk{code}") if isinstance(payload.get("data"), dict) else None
    if not isinstance(block, dict):
        return []
    raw_rows = block.get("qfqday") or block.get("day") or []
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        trade_date = to_date(item[0])
        close = _finite_float(item[2])
        if trade_date is None or close is None or close <= 0:
            continue
        if start is not None and trade_date < start:
            continue
        if end is not None and trade_date > end:
            continue
        rows.append({"trade_date": trade_date, "close": close, "upstream": "tencent_qfq"})
    rows.sort(key=lambda row: row["trade_date"])
    return rows


def fetch_hk_daily(
    hk_code: str,
    *,
    start_date: Any = None,
    end_date: Any = None,
    prefer: str = "auto",
    ak_module: Any = None,
) -> HkPriceFetch:
    """抓取单只港股不复权日线。

    Parameters
    ----------
    hk_code:
        5 位港股代码（如 ``01398``）。
    start_date / end_date:
        ``date`` / ``YYYY-MM-DD`` / ``YYYYMMDD``；None 表示不限。
    prefer:
        ``auto``（主源失败自动降级新浪）、``eastmoney``（只用主源）、
        ``sina``（直接走备用源，适合本机东财域名被断的情况）。
    ak_module:
        测试注入用；默认 lazily import akshare。

    Returns
    -------
    HkPriceFetch
        rows 为按日期升序的 ``{trade_date, close, upstream}``；拿不到时 rows 为空
        且 error 非空。**不抓取、不返回任何伪造价格。**
    """
    code = str(hk_code).strip().zfill(5)
    start = to_date(start_date)
    end = to_date(end_date)
    ak = ak_module or _import_akshare()

    if prefer not in {"auto", "tencent", "eastmoney", "sina"}:
        raise ValueError(f"prefer 必须是 auto/tencent/eastmoney/sina，收到 {prefer!r}")

    # 2026-11-15：腾讯作为 auto 的首选——东财 push2his 本机断连、新浪 50s/只。
    tencent_error: str | None = None
    if prefer in {"auto", "tencent"}:
        try:
            rows = _fetch_tencent_hk_daily(code, start=start, end=end)
        except Exception as error:  # noqa: BLE001
            tencent_error = f"{type(error).__name__}: {error}"
            rows = []
        if rows:
            return HkPriceFetch(rows=rows, upstream="tencent_qfq")
        if tencent_error is None:
            tencent_error = "empty_dataframe"

    if prefer == "tencent":
        return HkPriceFetch(
            rows=[], upstream="tencent_qfq",
            error=f"tencent_hk_kline: {tencent_error or 'empty_dataframe'}",
        )

    primary_error: str | None = None
    if prefer in {"auto", "eastmoney"}:
        try:
            frame = ak.stock_hk_hist(
                symbol=code,
                period="daily",
                start_date=(start or date(1990, 1, 1)).strftime("%Y%m%d"),
                end_date=(end or date.today()).strftime("%Y%m%d"),
                adjust="",
            )
            rows = _normalize_daily_frame(
                frame, start=start, end=end, upstream="eastmoney_stock_hk_hist",
            )
        except Exception as error:  # noqa: BLE001 - 网络/解析失败都要降级
            primary_error = f"{type(error).__name__}: {error}"
            rows = []
        if rows:
            return HkPriceFetch(
                rows=rows, upstream="eastmoney_stock_hk_hist",
            )
        if primary_error is None:
            primary_error = "empty_dataframe"

    if prefer == "eastmoney":
        return HkPriceFetch(
            rows=[], upstream="eastmoney_stock_hk_hist",
            error=f"eastmoney_stock_hk_hist: {primary_error or 'empty_dataframe'}",
        )

    sina_error: str | None = None
    try:
        frame = ak.stock_hk_daily(symbol=code, adjust="")
        rows = _normalize_daily_frame(
            frame, start=start, end=end, upstream="sina_stock_hk_daily",
        )
    except Exception as error:  # noqa: BLE001
        sina_error = f"{type(error).__name__}: {error}"
        rows = []
    if rows:
        note = None
        if primary_error is not None:
            note = f"eastmoney 主源失败，已降级新浪: {primary_error}"
        return HkPriceFetch(
            rows=rows, upstream="sina_stock_hk_daily", note=note,
        )
    if sina_error is None:
        sina_error = "empty_dataframe"
    return HkPriceFetch(
        rows=[],
        upstream="none",
        error=(
            f"all_sources_failed: tencent={tencent_error or 'not_tried'}; "
            f"eastmoney={primary_error or 'not_tried'}; sina={sina_error}"
        ),
    )


def _normalize_fx_frame(
    frame: Any,
    *,
    start: date | None,
    end: date | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """BOC 牌价 DataFrame → [{trade_date, rate, mid_source}]（rate 已是元/港元）。"""
    if frame is None or getattr(frame, "empty", True):
        return [], {}
    columns = [str(column) for column in frame.columns]
    date_column = _pick_column(columns, _DATE_COLUMNS)
    if date_column is None:
        raise ValueError(f"currency_boc_sina 返回列不含日期列: {columns}")
    value_columns = [name for name in _FX_MID_COLUMNS if name in columns]
    if not value_columns:
        raise ValueError(
            f"currency_boc_sina 返回列不含中行折算价/央行中间价: {columns}"
        )

    rows_by_date: dict[date, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for raw_date, *raw_values in zip(
        frame[date_column].tolist(),
        *(frame[name].tolist() for name in value_columns),
        strict=False,
    ):
        trade_date = to_date(raw_date)
        if trade_date is None:
            continue
        if start is not None and trade_date < start:
            continue
        if end is not None and trade_date > end:
            continue
        quote: float | None = None
        used_column: str | None = None
        for name, raw_value in zip(value_columns, raw_values, strict=False):
            value = _finite_float(raw_value)
            if value is not None and value > 0:
                quote = value
                used_column = name
                break
        if quote is None or used_column is None:
            continue
        # 接口单位是「人民币元 / 100 港元」；按量级同时兼容已经归一化的版本。
        rate = quote / 100.0 if quote > 10 else quote
        if not (_FX_SANE_MIN <= rate <= _FX_SANE_MAX):
            logger.warning(
                "汇率 %s 数值异常（原始 %s / %s），已跳过", trade_date, quote, used_column,
            )
            continue
        rows_by_date[trade_date] = {
            "trade_date": trade_date,
            "rate": rate,
            "mid_source": used_column,
        }
        counts[used_column] = counts.get(used_column, 0) + 1
    return [rows_by_date[key] for key in sorted(rows_by_date)], counts


def fetch_hkd_cny_history(
    *,
    start_date: Any = None,
    end_date: Any = None,
    ak_module: Any = None,
) -> FxHistoryFetch:
    """抓取 HKD/CNY 中行牌价历史（一次请求返回全部区间）。

    ``rate`` 已换算成 **人民币元 / 1 港元**（≈0.91），可直接写
    ``fx_rate_daily.rate``；``mid_source`` 记录用了「中行折算价」还是
    「央行中间价」。
    """
    start = to_date(start_date) or date(2000, 1, 1)
    end = to_date(end_date) or date.today()
    if end < start:
        return FxHistoryFetch(
            rows=[], upstream="boc_sina",
            error=f"end_date {end} 早于 start_date {start}",
        )
    try:
        ak = ak_module or _import_akshare()
        frame = ak.currency_boc_sina(
            symbol="港币",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        rows, counts = _normalize_fx_frame(frame, start=start, end=end)
    except Exception as error:  # noqa: BLE001
        return FxHistoryFetch(
            rows=[], upstream="boc_sina",
            error=f"{type(error).__name__}: {error}",
        )
    if not rows:
        return FxHistoryFetch(
            rows=[], upstream="boc_sina", error="empty_dataframe",
            mid_source_counts=counts,
        )
    rates = sorted(row["rate"] for row in rows)
    median = rates[len(rates) // 2]
    if not (_FX_SANE_MIN <= median <= _FX_SANE_MAX):
        return FxHistoryFetch(
            rows=[], upstream="boc_sina",
            error=f"median_rate_out_of_range: {median}",
            mid_source_counts=counts,
        )
    return FxHistoryFetch(
        rows=rows, upstream="boc_sina", mid_source_counts=counts,
    )
