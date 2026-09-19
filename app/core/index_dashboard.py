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
import time
from bisect import bisect_right
from datetime import date, timedelta
from typing import Any

from app.core.storage.duckdb_store import DuckDBReadLockedError

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

ALL_A_INDEX_CODE = "ALL_A"


def index_catalog() -> list[dict[str, Any]]:
    """全量指数目录（宽基 + 申万一级行业），前端卡片墙与对比表共用。"""
    catalog: list[dict[str, Any]] = [
        {
            "code": ALL_A_INDEX_CODE,
            "name": "全A指数",
            "category": "broad",
            "source": "synthetic_all_a",
            "cadence": "daily",
            "backtest_validated": False,
        },
    ]
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


def _dedupe_valuation_rows(raw_rows: list[dict[str, Any]], index_code: str) -> list[dict[str, Any]]:
    """同日期多源按主源优先去重，按日期升序返回。"""
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


def valuation_rows(duck: object, index_code: str) -> list[dict[str, Any]]:
    """取指数估值序列；同日期多源按主源优先去重，按日期升序返回。"""
    raw_rows = duck.read_query(
        """SELECT trade_date, pe_ttm, pe_metric, pb, div_yield, source, extra
           FROM index_valuation
           WHERE index_code = ? AND trade_date IS NOT NULL
           ORDER BY trade_date, source""",
        [index_code],
    )
    return _dedupe_valuation_rows(raw_rows, index_code)


def grouped_valuation_rows(duck: object) -> dict[str, list[dict[str, Any]]]:
    """一次读全表并按 index_code 分组、去重，供概览/ETF 批计算复用。

    全表当前约 12 万行；比 43 个指数各开一次连接、各扫一遍表快一个
    数量级，且调用方只需补一次 treasury 全量读取。
    """
    raw_rows = duck.read_query(
        """SELECT index_code, trade_date, pe_ttm, pe_metric, pb, div_yield, source, extra
           FROM index_valuation
           WHERE trade_date IS NOT NULL
           ORDER BY index_code, trade_date, source"""
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        code = row.get("index_code")
        if code is None:
            continue
        grouped.setdefault(str(code), []).append(row)
    return {
        code: _dedupe_valuation_rows(rows, code)
        for code, rows in grouped.items()
    }


def treasury_10y(duck: object) -> list[dict[str, Any]]:
    rows = duck.read_query(
        """SELECT curve_date, yield_pct
           FROM treasury_yield_curve
           WHERE tenor_years = 10 AND yield_pct IS NOT NULL
           ORDER BY curve_date"""
    )
    return rows


def _treasury_curve(
    treasury_rows: list[dict[str, Any]],
) -> tuple[list[date], list[float]]:
    """把国债行压成有序日期/收益率两个平行数组（每个请求批内只建一次）。"""
    by_date: dict[date, float] = {}
    for row in treasury_rows:
        d = _as_date(row.get("curve_date"))
        if d is not None and row.get("yield_pct") is not None:
            by_date[d] = float(row["yield_pct"])
    dates = sorted(by_date)
    return dates, [by_date[d] for d in dates]


def _treasury_lookup(
    treasury_dates: list[date],
    treasury_yields: list[float],
    trade_date: date,
) -> float | None:
    """非交易日对齐：取 <= trade_date 最近一个国债收益率（二分）。"""
    idx = bisect_right(treasury_dates, trade_date) - 1
    return treasury_yields[idx] if idx >= 0 else None


def compute_erp_series(
    valuations: list[dict[str, Any]],
    treasury_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """ERP 序列：仅 PE>0 且国债可得时计算，缺失日期跳过（不伪造）。"""
    treasury_dates, treasury_yields = _treasury_curve(treasury_rows)

    points: list[dict[str, Any]] = []
    for row in valuations:
        trade_date = _as_date(row.get("trade_date"))
        pe = row.get("pe_ttm")
        if trade_date is None or pe is None or float(pe) <= 0:
            continue
        yield_pct = _treasury_lookup(treasury_dates, treasury_yields, trade_date)
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


def _profit_sum(duck: object, report_date: date) -> float | None:
    rows = duck.read_query(
        """SELECT SUM(parent_net_profit) AS total
           FROM income_statement
           WHERE report_date = ? AND parent_net_profit IS NOT NULL""",
        [report_date.isoformat()],
    )
    value = rows[0].get("total") if rows else None
    return float(value) if value is not None else None


# 全A合成估值序列的进程内缓存：批次/详情/概览都会用到，重算一次约秒级，
# 缓存 30 分钟并以（库路径 + 价格最新交易日）为失效键（自动更新落地后自动
# 重建）。2026-09-17 修复：此前只用 cap_day 作键，两个不同库（正式/测试
# 夹具）日期相同会互相串数据。
_ALL_A_SERIES_TTL_SEC = 1800.0
_all_a_series_memo: dict[Any, dict[str, Any]] = {}
# 全A当前市值/净资产快照：两个全表窗口查询，单独 memo（同库隔离）。
_ALL_A_SNAPSHOT_TTL_SEC = 1800.0
_all_a_snapshot_memo: dict[Any, dict[str, Any]] = {}
_MEMO_MAX_ENTRIES = 8


def _duck_identity(duck: object) -> str:
    """进程内 memo 的跨库隔离键：优先真实路径，测试桩退回对象 id。"""
    db_path = getattr(duck, "db_path", None)
    return str(db_path) if db_path else f"obj:{id(duck)}"


def _remember_memo(memo: dict[Any, dict[str, Any]], key: Any, value: dict[str, Any]) -> None:
    memo[key] = value
    if len(memo) > _MEMO_MAX_ENTRIES:
        oldest = min(memo, key=lambda item: memo[item]["built_at"])
        memo.pop(oldest, None)


def _all_a_valuation_series(duck: object) -> list[dict[str, Any]]:
    """全A合成估值月度序列：[{trade_date, pe_ttm, pb}]，按月末最后交易日采样。

    口径（如实披露，不伪装官方指数）：
    - 市值：price_daily_qfq 收盘价 × **交易日当时股本**（share_capital_history ASOF），逐日全市场 —— D23 已修
      加总后取每月最后交易日；
    - PE：市值 / 归母净利润 TTM（按 report_date 对齐最新报告期；
      income_statement 无披露日字段，未做披露滞后调整）；
    - PB：市值 / 归母净资产（同样按 report_date 对齐）。
    """
    cap_day_row = duck.read_query("SELECT MAX(trade_date) AS d FROM price_daily_qfq")
    cap_day = _as_date(cap_day_row[0].get("d")) if cap_day_row else None
    memo_key = (_duck_identity(duck), str(cap_day))
    cached = _all_a_series_memo.get(memo_key)
    if cached is not None and time.monotonic() - cached["built_at"] < _ALL_A_SERIES_TTL_SEC:
        return cached["series"]

    cutoff = (date.today() - timedelta(days=TEN_YEAR_DAYS + 400)).isoformat()
    # 逐股 point-in-time 对齐（ASOF）：每个月末，每只股票用"该日或之前
    # 最新一期"财报计算 TTM 与净资产，避免按统一报告期对齐时覆盖率随
    # 披露节奏抖动（旧实现 PB 与当前概览差约 10%）。
    cap_rows = duck.read_query(
        """WITH me AS (
               SELECT trade_date FROM price_daily_qfq
               WHERE trade_date >= ?
               GROUP BY trade_date
               QUALIFY ROW_NUMBER() OVER (
                   PARTITION BY STRFTIME(trade_date, '%Y-%m') ORDER BY trade_date DESC
               ) = 1
           ),
           px AS (
               SELECT me.trade_date, p.stock_code,
                      p.close * COALESCE(sh.total_shares, s.total_shares) AS cap
               FROM me
               JOIN price_daily_qfq p ON p.trade_date = me.trade_date
               -- D23 修复：用交易日**当时**股本，不用当前股本
               ASOF LEFT JOIN share_capital_history sh
                 ON p.stock_code = sh.stock_code AND p.trade_date >= sh.effective_date
               LEFT JOIN stock_meta s ON s.stock_code = p.stock_code
               WHERE p.close IS NOT NULL
                 AND COALESCE(sh.total_shares, s.total_shares) > 0
           ),
           prof AS (
               SELECT stock_code, report_date, SUM(parent_net_profit) AS profit
               FROM income_statement
               WHERE parent_net_profit IS NOT NULL
               GROUP BY stock_code, report_date
           ),
           ttm AS (
               SELECT c.stock_code, c.report_date,
                      c.profit + y.profit - p.profit AS ttm_profit
               FROM prof c
               JOIN prof y ON y.stock_code = c.stock_code
                   AND y.report_date = MAKE_DATE(EXTRACT(YEAR FROM c.report_date)::INT - 1, 12, 31)
               JOIN prof p ON p.stock_code = c.stock_code
                   AND p.report_date = MAKE_DATE(
                       EXTRACT(YEAR FROM c.report_date)::INT - 1,
                       EXTRACT(MONTH FROM c.report_date)::INT,
                       EXTRACT(DAY FROM c.report_date)::INT)
               WHERE NOT (EXTRACT(MONTH FROM c.report_date) = 12 AND EXTRACT(DAY FROM c.report_date) = 31)
               UNION ALL
               SELECT stock_code, report_date, profit FROM prof
               WHERE EXTRACT(MONTH FROM report_date) = 12 AND EXTRACT(DAY FROM report_date) = 31
           ),
           equ AS (
               SELECT stock_code, report_date, SUM(total_equity_parent) AS equity
               FROM balance_sheet
               WHERE total_equity_parent IS NOT NULL AND total_equity_parent > 0
               GROUP BY stock_code, report_date
           )
           SELECT px.trade_date AS trade_date,
                  SUM(px.cap) AS total_cap,
                  SUM(ttm.ttm_profit) AS total_ttm,
                  SUM(equ.equity) AS total_equity
           FROM px
           ASOF LEFT JOIN ttm
               ON ttm.stock_code = px.stock_code AND ttm.report_date <= px.trade_date
           ASOF LEFT JOIN equ
               ON equ.stock_code = px.stock_code AND equ.report_date <= px.trade_date
           GROUP BY px.trade_date
           ORDER BY px.trade_date""",
        [cutoff],
    )
    series: list[dict[str, Any]] = []
    for row in cap_rows:
        trade_day = _as_date(row.get("trade_date"))
        cap = float(row["total_cap"] or 0)
        ttm = row.get("total_ttm")
        equity = row.get("total_equity")
        if trade_day is None or cap <= 0:
            continue
        pe = cap / float(ttm) if ttm is not None and float(ttm) > 0 else None
        pb = cap / float(equity) if equity is not None and float(equity) > 0 else None
        if pe is None and pb is None:
            continue
        series.append({"trade_date": trade_day.isoformat(), "pe_ttm": pe, "pb": pb})

    _remember_memo(_all_a_series_memo, memo_key, {
        "built_at": time.monotonic(),
        "series": series,
    })
    return series


def _all_a_market_snapshot(duck: object) -> dict[str, Any]:
    """全A当前总市值/净资产快照（两个全表窗口查询，30 分钟 memo）。

    2026-09-17：此前每次概览重算都跑这两条 19.7GB 全表窗口查询；价格
    最新交易日或最新报告期变化时自动重建，并按库路径隔离。
    """
    cap_day_row = duck.read_query("SELECT MAX(trade_date) AS d FROM price_daily_qfq")
    cap_day = _as_date(cap_day_row[0].get("d")) if cap_day_row else None
    equity_day_row = duck.read_query("SELECT MAX(report_date) AS d FROM balance_sheet")
    equity_day = _as_date(equity_day_row[0].get("d")) if equity_day_row else None
    memo_key = (_duck_identity(duck), str(cap_day), str(equity_day))
    cached = _all_a_snapshot_memo.get(memo_key)
    if cached is not None and time.monotonic() - cached["built_at"] < _ALL_A_SNAPSHOT_TTL_SEC:
        return cached
    # 2026-09-10 修复：市值此前取自 indicator_snapshot（财报期快照，可滞后
    # 数月），导致全A合成 PE/PB 的市值端"不更新"。改用 price_daily_qfq
    # 最新收盘价 × stock_meta.total_shares 逐股取最新，与 ETF 基本面口径一致。
    cap_rows = duck.read_query(
        """SELECT SUM(latest_cap) AS total_cap, MAX(trade_date) AS cap_date
           FROM (
               SELECT p.stock_code,
                      p.close * COALESCE(sh.total_shares, s.total_shares) AS latest_cap, p.trade_date,
                      ROW_NUMBER() OVER (PARTITION BY p.stock_code ORDER BY p.trade_date DESC) AS rn
               FROM price_daily_qfq p
               -- D23 修复：用交易日**当时**股本，不用当前股本
               ASOF LEFT JOIN share_capital_history sh
                 ON p.stock_code = sh.stock_code AND p.trade_date >= sh.effective_date
               LEFT JOIN stock_meta s ON s.stock_code = p.stock_code
               WHERE p.close IS NOT NULL
                 AND COALESCE(sh.total_shares, s.total_shares) > 0
           )
           WHERE rn = 1"""
    )
    equity_rows = duck.read_query(
        """SELECT SUM(total_equity_parent) AS total_equity
           FROM (
               SELECT stock_code, total_equity_parent,
                      ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
               FROM balance_sheet
               WHERE total_equity_parent IS NOT NULL AND total_equity_parent > 0
           )
           WHERE rn = 1"""
    )
    snapshot = {
        "built_at": time.monotonic(),
        "total_cap": float(cap_rows[0]["total_cap"] or 0) if cap_rows else 0.0,
        "cap_date": _as_date(cap_rows[0].get("cap_date")) if cap_rows else None,
        "total_equity": float(equity_rows[0]["total_equity"] or 0) if equity_rows else 0.0,
    }
    _remember_memo(_all_a_snapshot_memo, memo_key, snapshot)
    return snapshot


def _all_a_index_summary(duck: object) -> dict[str, Any]:
    """全A指数合成概览：全市场总市值 / 归母净利润 TTM 作为 PE 口径。"""
    meta = {
        "code": ALL_A_INDEX_CODE,
        "name": "全A指数",
        "category": "broad",
        "source": "synthetic_all_a",
        "cadence": "daily",
        "backtest_validated": False,
    }
    snapshot = _all_a_market_snapshot(duck)
    total_cap = snapshot["total_cap"]
    cap_date = snapshot["cap_date"]
    total_equity = snapshot["total_equity"]
    date_rows = duck.read_query("SELECT MAX(report_date) AS d FROM income_statement")
    latest_date = _as_date(date_rows[0].get("d")) if date_rows else None
    ttm_profit: float | None = None
    if latest_date is not None:
        latest_profit = _profit_sum(duck, latest_date)
        if latest_date.month == 12 and latest_date.day == 31:
            ttm_profit = latest_profit
        elif latest_profit is not None:
            previous_year_end = duck.read_query(
                """SELECT MAX(report_date) AS d FROM income_statement
                   WHERE EXTRACT(YEAR FROM report_date) = ?
                     AND EXTRACT(MONTH FROM report_date) = 12
                     AND EXTRACT(DAY FROM report_date) = 31""",
                [latest_date.year - 1],
            )[0].get("d")
            year_end_profit = _profit_sum(duck, _as_date(previous_year_end)) if previous_year_end else None
            prior_same_date = date(latest_date.year - 1, latest_date.month, latest_date.day)
            prior_same_profit = _profit_sum(duck, prior_same_date)
            if year_end_profit is not None and prior_same_profit is not None:
                ttm_profit = latest_profit + year_end_profit - prior_same_profit
    pe = total_cap / ttm_profit if total_cap > 0 and (ttm_profit or 0) > 0 else None
    pb = total_cap / total_equity if total_cap > 0 and total_equity > 0 else None
    erp: float | None = None
    if pe is not None and pe > 0:
        treasury_rows = treasury_10y(duck)
        latest_yield = float(treasury_rows[-1]["yield_pct"]) if treasury_rows else None
        if latest_yield is not None:
            erp = (1.0 / pe * 100.0) - latest_yield
    asof = cap_date or latest_date
    # 分位：基于合成月度序列的近10年窗口（2026-09-10 用户要求"像其他
    # ETF 一样有图有分位"，不再只给单点数值）。
    pe_pct = pb_pct = erp_pct = None
    erp_bands = _bands([])
    samples = 0
    try:
        series_rows = _all_a_valuation_series(duck)
        samples = len(series_rows)
        if asof is not None and series_rows:
            cutoff_day = asof - timedelta(days=TEN_YEAR_DAYS)
            pe_window = [
                float(r["pe_ttm"]) for r in series_rows
                if r.get("pe_ttm") is not None and (_as_date(r["trade_date"]) or date.min) >= cutoff_day
            ]
            pb_window = [
                float(r["pb"]) for r in series_rows
                if r.get("pb") is not None and (_as_date(r["trade_date"]) or date.min) >= cutoff_day
            ]
            erp_window_points = [
                p["erp"] for p in compute_erp_series(series_rows, treasury_10y(duck))
                if (_as_date(p["trade_date"]) or date.min) >= cutoff_day
            ]
            pe_pct = _percentile_rank(sorted(pe_window), pe) if pe is not None and pe_window else None
            pb_pct = _percentile_rank(sorted(pb_window), pb) if pb is not None and pb_window else None
            erp_pct = (
                _percentile_rank(sorted(erp_window_points), erp)
                if erp is not None and erp_window_points else None
            )
            erp_bands = _bands(erp_window_points)
    except Exception:  # noqa: BLE001 - 序列异常时退化为单点概览，不拖垮概览页
        samples = 1 if pe is not None else 0
    # latest_date 采用市值截止日期（估值"截至日"），利润口径日期单列披露，
    # 避免财报期日期（如 2026-06-30）让用户误判数据未更新。
    return {
        **meta,
        "status": "ok" if pe is not None else "partial",
        "samples": samples,
        "confidence": "unavailable",
        "latest_date": asof.isoformat() if asof else None,
        "profit_date": latest_date.isoformat() if latest_date else None,
        "pe": pe,
        "pe_metric": "全A归母TTM(合成)",
        "pe_percentile": pe_pct,
        "pb": pb,
        "pb_percentile": pb_pct,
        "erp": erp,
        "erp_percentile": erp_pct,
        "erp_bands": erp_bands,
    }


def index_summary(
    duck: object,
    code: str,
    *,
    valuations: list[dict[str, Any]] | None = None,
    treasury_rows: list[dict[str, Any]] | None = None,
    erp_points: list[dict[str, Any]] | None = None,
    include_erp: bool = True,
) -> dict[str, Any]:
    """单指数概览：最新 PE/PB 及其 10 年分位、ERP 与分位带。

    批计算调用方可传入已取好的 ``valuations`` / ``treasury_rows`` /
    ``erp_points``，避免每个指数重复开连接和重复算 ERP；只取 PE/PB 的
    调用方可用 ``include_erp=False`` 省掉国债对齐整段计算。
    """
    if code == ALL_A_INDEX_CODE:
        return _all_a_index_summary(duck)
    meta = BROAD_INDEX_NAMES.get(code) and {
        "code": code, "name": BROAD_INDEX_NAMES[code], "category": "broad",
        "source": "legulegu", "cadence": "monthly", "backtest_validated": True,
    } or {
        "code": code, "name": SW_INDUSTRY_NAMES.get(code, code), "category": "industry",
        "source": "sws", "cadence": "daily", "backtest_validated": False,
    }
    rows = valuations if valuations is not None else valuation_rows(duck, code)
    if not rows:
        empty_bands = _bands([])
        return {
            **meta, "status": "unavailable", "samples": 0,
            "confidence": "unavailable", "latest_date": None,
            "pe": None, "pe_metric": None, "pe_percentile": None,
            "pb": None, "pb_percentile": None,
            "erp": None, "erp_percentile": None, "erp_bands": empty_bands,
        }
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

    if include_erp:
        if erp_points is None:
            treasury = treasury_rows if treasury_rows is not None else treasury_10y(duck)
            erp_points = compute_erp_series(rows, treasury)
        erp_window = [p["erp"] for p in erp_points
                      if _as_date(p["trade_date"]) >= latest_date - timedelta(days=TEN_YEAR_DAYS)]
        current_erp = erp_points[-1]["erp"] if erp_points else None
    else:
        erp_window = []
        current_erp = None

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


def erp_detail(
    duck: object,
    code: str,
    *,
    valuations: list[dict[str, Any]] | None = None,
    treasury_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """单指数 ERP 详情：序列 + 近 10 年分位带 + 当前值/分位。"""
    rows = valuations if valuations is not None else valuation_rows(duck, code)
    treasury = treasury_rows if treasury_rows is not None else treasury_10y(duck)
    points = compute_erp_series(rows, treasury)
    summary = index_summary(
        duck, code,
        valuations=rows, treasury_rows=treasury, erp_points=points,
    )
    if code == ALL_A_INDEX_CODE:
        cap_asof = summary.get("latest_date") or "未知"
        profit_asof = summary.get("profit_date") or "未知"
        # 2026-09-10：返回合成 ERP 月度序列（此前为空数组导致详情页无图）。
        points = compute_erp_series(_all_a_valuation_series(duck), treasury)
        return {
            **summary,
            "series": points,
            "disclaimer": (
                "全A指数为全市场合成口径：PE=全A总市值/归母净利润TTM，"
                "PB=全A总市值/归母净资产，ERP=1/PE−10年国债收益率。"
                f"市值截至 {cap_asof}，利润为 {profit_asof} 报告期 TTM；"
                "历史序列为月末采样、按报告期对齐（未按披露日滞后调整）。"
            ),
        }
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


def valuation_detail(
    duck: object,
    code: str,
    *,
    valuations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """单指数 PE/PB 序列 + 近 10 年分位带（ETF 分位图与指数详情共用）。"""
    if valuations is not None:
        rows = valuations
    elif code == ALL_A_INDEX_CODE:
        # 全A无官方估值序列表数据，用合成月度序列（2026-09-10）。
        rows = _all_a_valuation_series(duck)
    else:
        rows = valuation_rows(duck, code)
    summary = index_summary(duck, code, valuations=rows, include_erp=False)
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
    """全指数 ERP 对比表（卡片墙数据源）。

    批预取本身失败时保持旧语义：返回 200 + 每个指数 status=error，
    而不是让整页 500（前端卡片墙可以逐项显示故障原因）。

    例外：DuckDBReadLockedError（外部更新/CLI 写进程持锁）必须向上抛——
    2026-09-17 修复：此前被吞成全 error 快照，API 层 stale 回退永远不
    触发，且 error 快照会覆盖并删除预热旧值，更新窗口内卡片墙整页退化。
    """
    try:
        grouped = grouped_valuation_rows(duck)
        treasury_rows = treasury_10y(duck)
    except DuckDBReadLockedError:
        raise
    except Exception as error:  # noqa: BLE001
        return {
            "items": [
                {**item, "status": "error", "error": str(error)}
                for item in index_catalog()
            ],
            "updated_at": None,
        }
    items = []
    for item in index_catalog():
        try:
            summary = index_summary(
                duck, item["code"],
                valuations=grouped.get(item["code"], []),
                treasury_rows=treasury_rows,
            )
        except DuckDBReadLockedError:
            raise
        except Exception as error:  # noqa: BLE001
            summary = {**item, "status": "error", "error": str(error)}
        items.append(summary)
    return {"items": items, "updated_at": None}


def index_summaries_batch(duck: object, codes: list[str] | set[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """一次取数批量计算多个指数概览（ETF 工作台等复用）。

    对 41 个 ETF 的跟踪指数只做 2 次 DuckDB 查询（index_valuation 全表
    + treasury 全表），而不是每个 ETF 各查 2+ 次。
    """
    wanted = {str(code) for code in codes}
    if not wanted:
        return {}
    grouped = grouped_valuation_rows(duck)
    treasury_rows = treasury_10y(duck)
    return {
        code: index_summary(
            duck, code,
            valuations=grouped.get(code, []),
            treasury_rows=treasury_rows,
        )
        for code in wanted
    }


def index_detail(duck: object, code: str) -> dict[str, Any]:
    """指数详情页一次响应：ERP 详情 + PE/PB 详情，共享同一批取数与 ERP 计算。"""
    # 全A在 index_valuation 表无官方序列，改用合成月度序列，
    # 否则合并详情里 PE/PB 图数据为空（2026-09-10 实测发现）。
    rows = (
        _all_a_valuation_series(duck)
        if code == ALL_A_INDEX_CODE
        else valuation_rows(duck, code)
    )
    treasury_rows = treasury_10y(duck)
    return {
        "code": code,
        "erp": erp_detail(duck, code, valuations=rows, treasury_rows=treasury_rows),
        "valuation": valuation_detail(duck, code, valuations=rows),
    }


def _parse_extra(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}
