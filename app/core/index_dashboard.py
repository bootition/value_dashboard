"""指数看板只读计算域（2026-09-05：多指数 ERP + ETF 分位的共同计算层）

仅读取 index_valuation 与 treasury_yield_curve，不做任何网络/写库操作。
口径（requirements.md）：
- ERP = 1 / PE-TTM − 10 年期国债收益率；PE 为倍、国债为百分数，
  结果以百分点计（如 沪深300 PE 14.57、国债 1.72% → ERP ≈ 5.14）。
- 宽基/红利：乐咕月末序列（pe_metric=ttm）；申万一级行业：sws 日度
  序列（pe_metric=sws_daily，申万日报口径，不宣称 TTM）。
- 分位窗口：最近 10 年（按该指数最新交易日起回溯 3650 天）；样本 <30
  标记 confidence=low；无数据如实 unavailable，不伪造。
- 行业 ERP 无回测验证（backtest_validated=false），宽基为 true。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

# 指数目录：宽基/红利 12 个（乐咕） + 申万一级行业 31 个（sws）
BROAD_INDEX_NAMES: dict[str, str] = {
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

SW_INDUSTRY_NAMES: dict[str, str] = {
    "SW801010": "农林牧渔",
    "SW801030": "基础化工",
    "SW801040": "钢铁",
    "SW801050": "有色金属",
    "SW801080": "电子",
    "SW801880": "汽车",
    "SW801110": "家用电器",
    "SW801120": "食品饮料",
    "SW801130": "纺织服饰",
    "SW801140": "轻工制造",
    "SW801150": "医药生物",
    "SW801160": "公用事业",
    "SW801170": "交通运输",
    "SW801180": "房地产",
    "SW801200": "商贸零售",
    "SW801210": "社会服务",
    "SW801780": "银行",
    "SW801790": "非银金融",
    "SW801230": "综合",
    "SW801710": "建筑材料",
    "SW801720": "建筑装饰",
    "SW801730": "电力设备",
    "SW801890": "机械设备",
    "SW801740": "国防军工",
    "SW801750": "计算机",
    "SW801760": "传媒",
    "SW801770": "通信",
    "SW801950": "煤炭",
    "SW801960": "石油石化",
    "SW801970": "环保",
    "SW801980": "美容护理",
}

TEN_YEAR_DAYS = 3650
MIN_SAMPLES = 30


def index_catalog() -> list[dict[str, Any]]:
    """全量指数目录（宽基 + 申万一级行业），前端卡片墙与对比表共用。"""
    catalog: list[dict[str, Any]] = []
    for code, name in BROAD_INDEX_NAMES.items():
        catalog.append({
            "code": code,
            "name": name,
            "category": "broad",
            "source": "legulegu",
            "cadence": "monthly",
            "backtest_validated": True,
        })
    for code, name in SW_INDUSTRY_NAMES.items():
        catalog.append({
            "code": code,
            "name": name,
            "category": "industry",
            "source": "sws",
            "cadence": "daily",
            "backtest_validated": False,
        })
    return catalog


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        # pandas.Timestamp 是 datetime 子类，也走这里：归一为纯 date
        return date(value.year, value.month, value.day)
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _quantile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位数（q∈[0,1]）；空序列返回 None。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = pos - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _percentile_rank(sorted_values: list[float], value: float) -> float | None:
    """当前值在历史分布中的分位（严格小于当前值的样本占比，百分数 0-100）。"""
    if not sorted_values:
        return None
    below = sum(1 for v in sorted_values if v < value)
    return below / len(sorted_values) * 100.0


def valuation_rows(duck: object, index_code: str) -> list[dict[str, Any]]:
    """取指数估值序列；同日期多源按主源优先去重，按日期升序返回。"""
    raw_rows = duck.read_query(
        """SELECT trade_date, pe_ttm, pe_metric, pb, div_yield, source, extra
           FROM index_valuation
           WHERE index_code = ? AND trade_date IS NOT NULL
           ORDER BY trade_date, source""",
        [index_code],
    )
    preferred = "sws" if index_code.startswith("SW") else "legulegu"
    by_date: dict[date, dict[str, Any]] = {}
    for row in raw_rows:
        trade_date = _as_date(row.get("trade_date"))
        if trade_date is None:
            continue
        existing = by_date.get(trade_date)
        if existing is None or row.get("source") == preferred:
            by_date[trade_date] = row
    return [by_date[d] for d in sorted(by_date)]


def treasury_10y(duck: object) -> list[dict[str, Any]]:
    rows = duck.read_query(
        """SELECT curve_date, yield_pct
           FROM treasury_yield_curve
           WHERE tenor_years = 10 AND yield_pct IS NOT NULL
           ORDER BY curve_date"""
    )
    return rows


def _treasury_lookup(treasury_by_date: dict[date, float], trade_date: date) -> float | None:
    """非交易日对齐：取 <= trade_date 最近一个国债收益率。"""
    for d in sorted(treasury_by_date, reverse=True):
        if d <= trade_date:
            return treasury_by_date[d]
    return None


def compute_erp_series(
    valuations: list[dict[str, Any]],
    treasury_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """ERP 序列：仅 PE>0 且国债可得时计算，缺失日期跳过（不伪造）。"""
    treasury_by_date: dict[date, float] = {}
    for row in treasury_rows:
        d = _as_date(row.get("curve_date"))
        if d is not None and row.get("yield_pct") is not None:
            treasury_by_date[d] = float(row["yield_pct"])

    points: list[dict[str, Any]] = []
    for row in valuations:
        trade_date = _as_date(row.get("trade_date"))
        pe = row.get("pe_ttm")
        if trade_date is None or pe is None or float(pe) <= 0:
            continue
        yield_pct = _treasury_lookup(treasury_by_date, trade_date)
        if yield_pct is None:
            continue
        points.append({
            "trade_date": trade_date.isoformat(),
            "pe": float(pe),
            "treasury_yield": yield_pct,
            "erp": (1.0 / float(pe) * 100.0) - yield_pct,
        })
    return points


def _bands(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    return {
        "p10": _quantile(ordered, 0.10),
        "p20": _quantile(ordered, 0.20),
        "p50": _quantile(ordered, 0.50),
        "p80": _quantile(ordered, 0.80),
        "p90": _quantile(ordered, 0.90),
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def _window(values: list[tuple[date, float]], latest: date) -> list[float]:
    cutoff = latest - timedelta(days=TEN_YEAR_DAYS)
    return [v for d, v in values if cutoff <= d <= latest]


def index_summary(duck: object, code: str) -> dict[str, Any]:
    """单指数概览：最新 PE/PB 及其 10 年分位、ERP 与分位带。"""
    meta = BROAD_INDEX_NAMES.get(code) and {
        "code": code, "name": BROAD_INDEX_NAMES[code], "category": "broad",
        "source": "legulegu", "cadence": "monthly", "backtest_validated": True,
    } or {
        "code": code, "name": SW_INDUSTRY_NAMES.get(code, code), "category": "industry",
        "source": "sws", "cadence": "daily", "backtest_validated": False,
    }
    rows = valuation_rows(duck, code)
    if not rows:
        return {**meta, "status": "unavailable", "samples": 0,
                "latest_date": None, "pe": None, "pb": None}
    latest = rows[-1]
    latest_date = _as_date(latest.get("trade_date"))
    pe_series = [(_as_date(r.get("trade_date")), float(r["pe_ttm"]))
                 for r in rows
                 if r.get("pe_ttm") is not None and _as_date(r.get("trade_date")) is not None]
    pb_series = [(_as_date(r.get("trade_date")), float(r["pb"]))
                 for r in rows
                 if r.get("pb") is not None and _as_date(r.get("trade_date")) is not None]

    assert latest_date is not None
    pe_window = _window(pe_series, latest_date)
    pb_window = _window(pb_series, latest_date)
    samples = len(pe_window)
    confidence = "high" if samples >= 250 else ("low" if samples >= MIN_SAMPLES else "unavailable")

    erp_points = compute_erp_series(rows, treasury_10y(duck))
    erp_window = [p["erp"] for p in erp_points
                  if _as_date(p["trade_date"]) >= latest_date - timedelta(days=TEN_YEAR_DAYS)]
    current_erp = erp_points[-1]["erp"] if erp_points else None

    current_pe = float(latest["pe_ttm"]) if latest.get("pe_ttm") is not None else None
    current_pb = float(latest["pb"]) if latest.get("pb") is not None else None

    return {
        **meta,
        "status": "ok" if current_pe is not None else "partial",
        "samples": samples,
        "confidence": confidence,
        "latest_date": latest_date.isoformat(),
        "pe": current_pe,
        "pe_metric": latest.get("pe_metric"),
        "pe_percentile": _percentile_rank(sorted(pe_window), current_pe) if current_pe is not None and pe_window else None,
        "pb": current_pb,
        "pb_percentile": _percentile_rank(sorted(pb_window), current_pb) if current_pb is not None and pb_window else None,
        "erp": current_erp,
        "erp_percentile": _percentile_rank(sorted(erp_window), current_erp) if current_erp is not None and erp_window else None,
        "erp_bands": _bands(erp_window),
    }


def erp_detail(duck: object, code: str) -> dict[str, Any]:
    """单指数 ERP 详情：序列 + 近 10 年分位带 + 当前值/分位。"""
    summary = index_summary(duck, code)
    rows = valuation_rows(duck, code)
    points = compute_erp_series(rows, treasury_10y(duck))
    latest_date = _as_date(summary.get("latest_date"))
    erp_values: list[float] = []
    if latest_date is not None:
        cutoff = latest_date - timedelta(days=TEN_YEAR_DAYS)
        erp_values = [p["erp"] for p in points if _as_date(p["trade_date"]) >= cutoff]
    current = points[-1]["erp"] if points else None
    return {
        **summary,
        "series": points,
        "erp": current,
        "erp_percentile": _percentile_rank(sorted(erp_values), current) if current is not None and erp_values else None,
        "erp_bands": _bands(erp_values),
        "disclaimer": (
            "ERP 对宽基指数未来一年收益具有历史预测力；对申万一级行业暂无回测验证，仅作观察参考。"
            if summary.get("category") == "industry"
            else "ERP 对宽基指数：ERP 越高表示股票相对国债越便宜，历史规律不等于未来保证。"
        ),
    }


def valuation_detail(duck: object, code: str) -> dict[str, Any]:
    """单指数 PE/PB 序列 + 近 10 年分位带（ETF 分位图与指数详情共用）。"""
    summary = index_summary(duck, code)
    rows = valuation_rows(duck, code)
    latest_date = _as_date(summary.get("latest_date"))
    cutoff = latest_date - timedelta(days=TEN_YEAR_DAYS) if latest_date else None

    pe_points = [
        {"trade_date": str(r["trade_date"]), "value": float(r["pe_ttm"])}
        for r in rows
        if r.get("pe_ttm") is not None and _as_date(r.get("trade_date")) is not None
    ]
    pb_points = [
        {"trade_date": str(r["trade_date"]), "value": float(r["pb"])}
        for r in rows
        if r.get("pb") is not None and _as_date(r.get("trade_date")) is not None
    ]
    pe_window = [p["value"] for p in pe_points if cutoff is None or _as_date(p["trade_date"]) >= cutoff]
    pb_window = [p["value"] for p in pb_points if cutoff is None or _as_date(p["trade_date"]) >= cutoff]
    current_pe = pe_points[-1]["value"] if pe_points else None
    current_pb = pb_points[-1]["value"] if pb_points else None

    return {
        "code": code,
        "name": summary.get("name"),
        "category": summary.get("category"),
        "latest_date": summary.get("latest_date"),
        "pe_series": pe_points,
        "pe_bands": _bands(pe_window),
        "pe_percentile": _percentile_rank(sorted(pe_window), current_pe) if current_pe is not None else None,
        "pb_series": pb_points,
        "pb_bands": _bands(pb_window),
        "pb_percentile": _percentile_rank(sorted(pb_window), current_pb) if current_pb is not None else None,
        "pe_metric": summary.get("pe_metric"),
    }


def erp_compare(duck: object) -> dict[str, Any]:
    """全指数 ERP 对比表（卡片墙数据源）。"""
    items = []
    for item in index_catalog():
        try:
            summary = index_summary(duck, item["code"])
        except Exception as error:  # noqa: BLE001
            summary = {**item, "status": "error", "error": str(error)}
        items.append(summary)
    return {"items": items, "updated_at": None}


def _parse_extra(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}
