"""ETF 基本面聚合计算域（2026-09-09）

为 ETF 详情提供“成分股公司基本面合计”：
- 业绩合计（归母净利润按报告期求和，单位亿元）
- 市值合计（总股本 × 前复权收盘价，按日聚合后取月末值，单位亿元）
- 全A指数的全体利润增速（各报告期合计同比）
- 全A利润增长的申万一级行业贡献拆解

成分股口径（没有指数公司官方成分表，明确披露代理口径）：
- 申万一级行业 ETF：stock_meta.sw_level1 与跟踪指数行业名完全匹配。
- 宽基/市场 ETF：按最新总市值排名近似（沪深300=前300、中证500=301-800、
  中证1000=801-1800、上证50=前50、中证800=前800 等）。
- 策略 ETF 与无法映射的标的：暂用全A上市池近似，并在 disclaimer 标注。
- 全A指数（伪 ETF 代码 ALL_A）：全A上市股票。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.core.index_dashboard import SW_INDUSTRY_NAMES

ALL_A_CODE = "ALL_A"
_LOOKBACK_YEARS = 10
_LOOKBACK_DAYS = _LOOKBACK_YEARS * 365

# 宽基/市场指数 → (前 N 名, 跳过前 N 名)。None 表示不设上限（全A近似）。
_SIZE_PROXY: dict[str, tuple[int | None, int]] = {
    "000016": (50, 0),
    "000010": (180, 0),
    "000300": (300, 0),
    "000906": (800, 0),
    "000905": (800, 300),
    "000852": (1800, 800),
    "000009": (380, 0),
    "399330": (100, 0),
    "399673": (50, 0),
}

_RANK_SQL = """
    WITH latest_snapshot AS (
        SELECT stock_code, report_date, total_market_cap
        FROM indicator_snapshot
        WHERE report_date = (SELECT MAX(report_date) FROM indicator_snapshot)
    )
    SELECT s.stock_code
    FROM stock_meta s
    JOIN latest_snapshot i ON i.stock_code = s.stock_code
    WHERE s.is_listed IS TRUE
      AND s.total_shares > 0
      AND i.total_market_cap > 0
    ORDER BY i.total_market_cap DESC, s.stock_code
"""


def _all_codes(duck: object) -> list[str]:
    rows = duck.read_query(
        """SELECT stock_code FROM stock_meta
           WHERE is_listed IS TRUE AND total_shares > 0
           ORDER BY stock_code"""
    )
    return [str(row["stock_code"]) for row in rows]


def _sw_codes(duck: object, industry_name: str) -> list[str]:
    rows = duck.read_query(
        """SELECT stock_code FROM stock_meta
           WHERE is_listed IS TRUE AND sw_level1 = ?
           ORDER BY stock_code""",
        [industry_name],
    )
    return [str(row["stock_code"]) for row in rows]


def _size_proxy_codes(duck: object, index_code: str) -> list[str]:
    rows = duck.read_query(_RANK_SQL)
    codes = [str(row["stock_code"]) for row in rows]
    take, skip = _SIZE_PROXY.get(index_code, (300, 0))
    if take is None:
        return codes
    return codes[skip:skip + take]


def resolve_constituents(duck: object, meta: dict[str, Any]) -> dict[str, Any]:
    """返回成分股代码与口径说明。"""
    etf_code = str(meta.get("etf_code") or "")
    track_code = meta.get("track_index_code")
    if etf_code == ALL_A_CODE:
        return {
            "codes": _all_codes(duck),
            "method": "全A上市股票",
            "disclaimer": "全A指数为全市场 A 股伪 ETF，不含退市/长期停牌公司；成分按全部已上市且披露股本的公司统计。",
        }
    industry_name = SW_INDUSTRY_NAMES.get(track_code or "")
    if track_code and industry_name:
        codes = _sw_codes(duck, industry_name)
        if codes:
            return {
                "codes": codes,
                "method": f"申万一级行业（{industry_name}）",
                "disclaimer": "成分股按 stock_meta.sw_level1 与跟踪指数行业名匹配，非指数公司官方成分表。",
            }
    if track_code in _SIZE_PROXY:
        return {
            "codes": _size_proxy_codes(duck, track_code),
            "method": "规模排名近似",
            "disclaimer": "暂无官方成分表，按最新总市值排名近似成分（沪深300=前300等）；策略/红利类暂用全A近似。",
        }
    return {
        "codes": _all_codes(duck),
        "method": "全A近似",
        "disclaimer": "该 ETF 暂无可映射的成分股口径，使用全A上市池近似，仅作观察参考。",
    }


def _where_codes(codes: list[str]) -> str:
    return ", ".join("?" for _ in codes)


def market_cap_series(duck: object, codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    rows = duck.read_query(
        f"""SELECT p.trade_date AS trade_date,
                   SUM(p.close * COALESCE(sh.total_shares, s.total_shares)) AS total_market_cap,
                   COUNT(DISTINCT p.stock_code) AS companies
            FROM price_daily_qfq p
            -- D23 修复（2026-09-19）：市值必须用**当时股本**，不是当前股本。
            -- 旧实现 JOIN stock_meta（当前总股本）配历史价格，会把增发高估、回购低估。
            -- 改用 ASOF JOIN share_capital_history 取「报告/交易日当时」的股本；
            -- 极少数无股本历史的老股回退到当前股本（COALESCE），如实兜底不丢数据。
            ASOF LEFT JOIN share_capital_history sh
              ON p.stock_code = sh.stock_code AND p.trade_date >= sh.effective_date
            LEFT JOIN stock_meta s ON s.stock_code = p.stock_code
            WHERE p.stock_code IN ({_where_codes(codes)})
              AND p.trade_date >= ?
              AND p.close IS NOT NULL
              AND COALESCE(sh.total_shares, s.total_shares) > 0
            GROUP BY p.trade_date
            ORDER BY p.trade_date""",
        [*codes, cutoff],
    )
    # 只保留月末最后交易日，控制前端点数
    monthly: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_date = str(row["trade_date"])[:10]
        month = trade_date[:7]
        current = monthly.get(month)
        if current is None or trade_date > str(current["trade_date"]):
            monthly[month] = {
                "trade_date": trade_date,
                "value": float(row["total_market_cap"]) / 1e8,
                "companies": int(row["companies"]),
            }
    return [monthly[key] for key in sorted(monthly)]


def earnings_series(duck: object, codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    rows = duck.read_query(
        f"""SELECT i.report_date AS report_date,
                   SUM(i.parent_net_profit) AS total_profit,
                   COUNT(DISTINCT i.stock_code) AS companies
            FROM income_statement i
            WHERE i.stock_code IN ({_where_codes(codes)})
              AND i.report_date >= ?
              AND i.parent_net_profit IS NOT NULL
            GROUP BY i.report_date
            ORDER BY i.report_date""",
        [*codes, cutoff],
    )
    return [
        {
            "report_date": str(row["report_date"])[:10],
            "value": float(row["total_profit"]) / 1e8,
            "companies": int(row["companies"]),
        }
        for row in rows
        if str(row["report_date"])[5:10] in {"03-31", "06-30", "09-30", "12-31"}
    ]


def profit_growth_series(earnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """归母净利润合计同比（今年同期 vs 去年同期）。"""
    by_date = {item["report_date"]: item["value"] for item in earnings}
    points: list[dict[str, Any]] = []
    for item in earnings:
        current = item["report_date"]
        previous = f"{int(current[:4]) - 1}{current[4:]}"
        base = by_date.get(previous)
        if base is None or base == 0:
            continue
        points.append({
            "report_date": current,
            "value": (item["value"] / base - 1) * 100.0,
        })
    return points


def _previous_quarter(report_date: str) -> str:
    year = int(report_date[:4])
    month_day = report_date[4:]
    if month_day == "-03-31":
        return f"{year - 1}-12-31"
    if month_day == "-06-30":
        return f"{year}-03-31"
    if month_day == "-09-30":
        return f"{year}-06-30"
    return f"{year}-09-30"


def profit_growth_qoq(earnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """归母净利润合计环比（本季度 vs 上一季度）。"""
    by_date = {item["report_date"]: item["value"] for item in earnings}
    points: list[dict[str, Any]] = []
    for item in earnings:
        previous = _previous_quarter(item["report_date"])
        base = by_date.get(previous)
        if base is None or base == 0:
            continue
        points.append({
            "report_date": item["report_date"],
            "value": (item["value"] / base - 1) * 100.0,
        })
    return points


def industry_contribution_between(
    duck: object,
    current_date: str,
    previous_date: str,
) -> list[dict[str, Any]]:
    rows = duck.read_query(
        """SELECT s.sw_level1 AS industry, i.report_date AS report_date,
                  SUM(i.parent_net_profit) AS total_profit
           FROM income_statement i
           JOIN stock_meta s ON s.stock_code = i.stock_code
           WHERE i.report_date IN (?, ?)
             AND i.parent_net_profit IS NOT NULL
             AND s.sw_level1 IS NOT NULL
           GROUP BY s.sw_level1, i.report_date""",
        [previous_date, current_date],
    )
    current: dict[str, float] = {}
    previous: dict[str, float] = {}
    for row in rows:
        report_date = str(row["report_date"])[:10]
        target = current if report_date == current_date else previous
        target[str(row["industry"])] = float(row["total_profit"]) / 1e8
    total_delta = sum(current.values()) - sum(previous.values())
    if total_delta == 0:
        return []
    items = []
    for industry in current.keys() | previous.keys():
        delta = current.get(industry, 0.0) - previous.get(industry, 0.0)
        items.append({
            "industry": industry,
            "current": current.get(industry, 0.0),
            "delta": delta,
            "contribution_pct": delta / total_delta * 100.0,
        })
    items.sort(key=lambda item: abs(item["delta"]), reverse=True)
    top = items[:15]
    if len(items) > 15:
        rest_delta = sum(item["delta"] for item in items[15:])
        top.append({
            "industry": "其他行业合计",
            "current": sum(item["current"] for item in items[15:]),
            "delta": rest_delta,
            "contribution_pct": rest_delta / total_delta * 100.0,
        })
    return top


def industry_contribution(
    duck: object,
    earnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """行业利润增长贡献（同比，默认口径，兼容旧调用方）。"""
    if len(earnings) < 2:
        return []
    current_date = earnings[-1]["report_date"]
    previous_date = f"{int(current_date[:4]) - 1}{current_date[4:]}"
    if previous_date < earnings[0]["report_date"]:
        return []
    return industry_contribution_between(duck, current_date, previous_date)


def industry_contribution_qoq(
    duck: object,
    earnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """行业利润增长贡献（环比：最新报告期 vs 上一季度）。"""
    if len(earnings) < 2:
        return []
    current_date = earnings[-1]["report_date"]
    previous_date = _previous_quarter(current_date)
    if previous_date < earnings[0]["report_date"]:
        return []
    return industry_contribution_between(duck, current_date, previous_date)


def fundamental_detail(duck: object, meta: dict[str, Any]) -> dict[str, Any]:
    resolved = resolve_constituents(duck, meta)
    codes = resolved["codes"]
    earnings = earnings_series(duck, codes)
    market_cap = market_cap_series(duck, codes)
    growth = profit_growth_series(earnings)
    growth_qoq = profit_growth_qoq(earnings)
    is_all_a = str(meta.get("etf_code")) == ALL_A_CODE
    contribution = industry_contribution(duck, earnings) if is_all_a else []
    contribution_qoq = industry_contribution_qoq(duck, earnings) if is_all_a else []
    latest_profit = earnings[-1]["value"] if earnings else None
    latest_cap = market_cap[-1]["value"] if market_cap else None
    return {
        "etf_code": str(meta.get("etf_code")),
        "name": str(meta.get("name")),
        "method": resolved["method"],
        "companies": len(codes),
        "latest_profit": latest_profit,
        "latest_market_cap": latest_cap,
        "earnings": earnings,
        "market_cap": market_cap,
        "profit_growth": growth,
        "profit_growth_qoq": growth_qoq,
        "industry_contribution": contribution,
        "industry_contribution_qoq": contribution_qoq,
        "disclaimer": resolved["disclaimer"],
    }
