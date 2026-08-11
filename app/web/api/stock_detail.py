"""个股详情 API (PRD §14)

提供个股详情页所需的全部数据：
- K线行情 (raw/qfq, 含MA)
- 估值/盈利/成长/安全/股东回报摘要
- 财务趋势 (年度/季度/TTM)
- 自定义指标趋势
- 溯源信息 (报告期/来源/置信度)
- PDF 打开
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import FileResponse

from app.core.pdf.manager import PDFManager

router = APIRouter(prefix="/api/stock", tags=["stock-detail"])

TTM_FLOW_FIELDS = (
    "revenue",
    "cost_of_revenue",
    "operating_profit",
    "net_profit",
    "parent_net_profit",
    "deducted_net_profit",
    "basic_eps",
    "cf_from_operating",
    "cf_from_investing",
    "cf_from_financing",
)
TTM_POINT_FIELDS = (
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_equity_parent",
)


def calculate_ttm_trend(rows: list[dict]) -> list[dict]:
    """Calculate TTM flow values while preserving current-period balance-sheet values."""
    dated_rows: dict[date, dict] = {}
    for row in rows:
        raw_date = row.get("report_date")
        report_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        dated_rows[report_date] = row

    trend: list[dict] = []
    for report_date in sorted(dated_rows):
        current = dated_rows[report_date]
        item: dict = {"report_date": report_date.isoformat()}
        if report_date.month == 12 and report_date.day == 31:
            for field in TTM_FLOW_FIELDS:
                item[field] = current.get(field)
        else:
            annual = dated_rows.get(date(report_date.year - 1, 12, 31))
            prior_period = dated_rows.get(
                date(report_date.year - 1, report_date.month, report_date.day)
            )
            if annual is None or prior_period is None:
                continue
            for field in TTM_FLOW_FIELDS:
                annual_value = annual.get(field)
                current_value = current.get(field)
                prior_value = prior_period.get(field)
                item[field] = (
                    annual_value + current_value - prior_value
                    if annual_value is not None
                    and current_value is not None
                    and prior_value is not None
                    else None
                )

        for field in TTM_POINT_FIELDS:
            item[field] = current.get(field)
        revenue = item.get("revenue")
        cost = item.get("cost_of_revenue")
        item["net_profit"] = (
            item.get("parent_net_profit")
            if item.get("parent_net_profit") is not None
            else item.get("net_profit")
        )
        item["gross_profit"] = (
            revenue - cost if revenue is not None and cost is not None else None
        )
        trend.append(item)
    return trend


def _to_single_quarter(rows: list[dict]) -> list[dict]:
    """Convert cumulative flow statements to standalone quarters, preserving balances.

    P1-A修复（第六轮复审 F1/F2）:
    - F2: 差分必须基于"差分前的累计原值"——单季值 = 本期累计 − 上期累计，
      且供下期差分的是本期累计原值，绝不能用差分后的单季值（否则连续
      三个及以上报告期的 Q3/Q4 系统性错误）。
    - F1: 年内首行不是 Q1 时，累计值无法推导单季值 → fail-closed 置 NULL；
      年内首行即 Q1 时累计值 = 单季值，保留。
    - 季度缺口（如缺 Q2 只剩 Q1/Q3）同样 fail-closed 置 NULL，
      与 calculate_ttm_trend 的缺口处理一致；绝不输出累计值或跨季差值。
    """
    flow_fields = (
        "revenue", "cost_of_revenue", "gross_profit", "operating_profit", "net_profit",
        "parent_net_profit", "deducted_net_profit", "cf_from_operating",
        "cf_from_investing", "cf_from_financing",
    )
    _QUARTER_OF_MONTH = {3: 1, 6: 2, 9: 3, 12: 4}

    def _quarter_index(report_date: object) -> int:
        text = str(report_date)
        if len(text) < 7:
            return 0
        try:
            return _QUARTER_OF_MONTH.get(int(text[5:7]), 0)
        except ValueError:
            return 0

    chronological = list(reversed(rows))
    prior_by_year: dict[str, dict] = {}
    for row in chronological:
        year = str(row.get("report_date", ""))[:4]
        quarter = _quarter_index(row.get("report_date"))
        # F2: 先取差分前的累计原值（下期差分必须用累计值，而非本期单季值）
        cumulative = {field: row.get(field) for field in flow_fields}
        prior = prior_by_year.get(year)
        if prior is not None:
            prior_quarter = prior.get("_quarter", 0)
            if prior_quarter == quarter - 1:
                for field in flow_fields:
                    value = cumulative.get(field)
                    previous = prior.get(field)
                    row[field] = value - previous if value is not None and previous is not None else None
            else:
                # 同一年内季度缺口（或上一行不是紧邻季度）：单季值不可推导
                for field in flow_fields:
                    row[field] = None
        elif quarter != 1:
            # F1: 年内首行不是 Q1（如数据自 Q2 起始）→ 累计值不可推导单季值
            for field in flow_fields:
                row[field] = None
        # else: 年内首行即 Q1 → 累计值 = 单季值，原样保留
        prior_by_year[year] = {**cumulative, "_quarter": quarter}
    return chronological


def build_freshness_metadata(
    financial_date: date | None,
    price_date: date | None,
    calculated_at: datetime | str | None,
    data_version: str | None,
) -> dict:
    """Describe independent financial, price, and snapshot ages (all UTC)."""
    from datetime import timezone
    today = datetime.now(timezone.utc).date()
    calculated_date: date | None = None
    if isinstance(calculated_at, datetime):
        if calculated_at.tzinfo is None:
            calculated_date = calculated_at.date()
        else:
            calculated_date = calculated_at.astimezone(timezone.utc).date()
    elif isinstance(calculated_at, str):
        try:
            calculated_date = datetime.fromisoformat(calculated_at.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except ValueError:
            calculated_date = None
    price_age_days = (today - price_date).days if price_date else None
    financial_age_days = (today - financial_date).days if financial_date else None
    snapshot_age_days = (today - calculated_date).days if calculated_date else None
    ages = (financial_age_days, price_age_days, snapshot_age_days)
    stale_days = max(value for value in ages if value is not None) if any(value is not None for value in ages) else None
    return {
        "financial_effective_date": financial_date.isoformat() if financial_date else None,
        "price_date": price_date.isoformat() if price_date else None,
        "calculated_at": str(calculated_at) if calculated_at is not None else None,
        "data_version": data_version,
        "stale_days": stale_days,
        "price_age_days": price_age_days,
        "financial_age_days": financial_age_days,
        "snapshot_age_days": snapshot_age_days,
        "stale_warning": stale_days is None or stale_days > 7,
    }

# ─── 指标历史能力标志 (PRD §14 SD7: current_only 指标标注) ──────────
# historical_capable=True: 可生成可信历史序列
# historical_capable=False: current_only，只能做当前展示
INDICATOR_HISTORICAL_CAPABLE: dict[str, bool] = {
    # 估值指标: 依赖最新收盘价 → current_only
    "pe_ttm": False, "pb_mrq": False, "ps_ttm": False, "pcf_ttm": False,
    "dividend_yield": False, "total_market_cap": False, "circ_market_cap": False,
    # 盈利能力: 从财务报表计算 → historical_capable
    "roe": True, "roa": True, "gross_margin": True, "net_margin": True,
    "roic": True, "cf_to_net_profit": True,
    # 成长: 需要多期数据 → historical_capable
    "revenue_yoy": True, "net_profit_yoy": True, "deducted_profit_yoy": True,
    "revenue_cagr3": True, "revenue_cagr5": True,
    "net_profit_cagr3": True, "net_profit_cagr5": True,
    "deducted_profit_cagr3": True, "deducted_profit_cagr5": True,
    # 安全: 从资产负债表计算 → historical_capable
    "debt_ratio": True, "current_ratio": True, "quick_ratio": True,
    "interest_bearing_debt": True, "interest_coverage": True, "goodwill_ratio": True,
    # 股东回报: historical_capable
    "payout_ratio": True, "dps": True, "consecutive_div_years": True,
    # 行情: current_only (依赖最新价格)
    "ma5": False, "ma10": False, "ma20": False, "ma60": False,
    "ma120": False, "ma250": False,
    "latest_close": False, "turnover_rate": False,
}


@router.get("/search")
def search_stocks(
    request: Request,
    query: str = Query(min_length=1, max_length=80),
) -> dict:
    """Search the covered listed-stock universe by partial code or Chinese name."""
    term = query.strip()
    if not term:
        raise HTTPException(status_code=400, detail="search query is required")

    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = request.app.state.duck.read_query(
        """SELECT stock_code, name, exchange, csrc_l1
           FROM stock_meta
           WHERE is_listed IS TRUE
             AND (stock_code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')
           ORDER BY
             CASE WHEN stock_code = ? THEN 0 WHEN name = ? THEN 1 ELSE 2 END,
             stock_code
           LIMIT 20""",
        [pattern, pattern, term, term],
    )
    return {"items": rows, "query": term}


@router.get("/{stock_code}/info")
def get_stock_info(stock_code: str, request: Request) -> dict:
    """股票基本信息 (PRD §14 SD1: 代码/名称/拼音/最近收盘价/价格日期)"""
    duck = request.app.state.duck
    rows = duck.read_query(
        "SELECT stock_code, name, pinyin, exchange, listing_date, "
        "is_st, is_suspended, csrc_l1, csrc_l2 "
        "FROM stock_meta WHERE stock_code = ?",
        [stock_code],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="stock not found")

    info = rows[0]

    # 最新收盘价
    price_rows = duck.read_query(
        "SELECT trade_date, close FROM price_daily_raw "
        "WHERE stock_code = ? AND close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1",
        [stock_code],
    )
    if price_rows:
        info["latest_close"] = price_rows[0]["close"]
        info["latest_price_date"] = str(price_rows[0]["trade_date"])
    else:
        info["latest_close"] = None
        info["latest_price_date"] = None

    return info


@router.get("/{stock_code}/kline")
def get_kline(
    stock_code: str,
    request: Request,
    adjust: Literal["raw", "qfq"] = "raw",
    days: int = Query(250, ge=1, le=2000),
    period: Literal["day", "week", "month"] = "day",
) -> dict:
    """K线数据 (PRD §14 SD2: 日/周/月K, 成交量, 均线, raw与qfq切换)

    period=day 保持原行为；period=week/month 服务端读时聚合日线：
    - trade_date 取桶内最后一个真实交易日
    - open/close 取桶内首个/末个真实交易日价格，high/low 取桶内极值，
      volume/turnover 求和
    - turnover_rate 周/月 fail-closed 置 null
    - days 表示返回的 K 线根数（取最新 days 根后升序）
    聚合后复用 _calc_ma 计算均线。
    """
    duck = request.app.state.duck
    table = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
    columns = duck.read_query(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = ?
        """,
        [table],
    )
    turnover_rate_column = (
        "turnover_rate"
        if "turnover_rate" in {row["column_name"] for row in columns}
        else "NULL::DOUBLE AS turnover_rate"
    )

    if period == "day":
        rows = duck.read_query(
            f"""SELECT trade_date, open, high, low, close, volume, turnover,
                       {turnover_rate_column}
                FROM {table}
                WHERE stock_code = ? AND close IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT ?""",
            [stock_code, days],
        )
        if not rows:
            return {"candles": [], "adjust": adjust, "period": period}
        # 按时间正序排列（旧→新）
        rows.reverse()
    else:
        daily_rows = _fetch_daily_for_aggregation(duck, table, stock_code, days, period)
        if not daily_rows:
            return {"candles": [], "adjust": adjust, "period": period}
        rows = _aggregate_candles(daily_rows, period)[-days:]

    # 计算 MA 线
    closes = [r["close"] for r in rows]
    for ma_period in [5, 10, 20, 60, 120, 250]:
        ma_values = _calc_ma(closes, ma_period)
        for i, r in enumerate(rows):
            r[f"ma{ma_period}"] = ma_values[i]

    return {
        "candles": rows,
        "adjust": adjust,
        "period": period,
        "count": len(rows),
    }


def _fetch_daily_for_aggregation(
    duck: object, table: str, stock_code: str, days: int, period: str
) -> list[dict]:
    """取构建最近 days 根周/月 K 线所需的升序日线窗口（带时间边界下界）"""
    max_row = duck.read_query(
        f"SELECT max(trade_date) AS latest FROM {table} "
        "WHERE stock_code = ? AND close IS NOT NULL",
        [stock_code],
    )
    latest = max_row[0]["latest"] if max_row else None
    if latest is None:
        return []
    span_days = days * 7 + 7 if period == "week" else days * 31 + 15
    start = latest - timedelta(days=span_days)
    return duck.read_query(
        f"""SELECT trade_date, open, high, low, close, volume, turnover
            FROM {table}
            WHERE stock_code = ? AND close IS NOT NULL AND trade_date >= ?
            ORDER BY trade_date ASC""",
        [stock_code, start],
    )


def _aggregate_candles(rows: list[dict], period: str) -> list[dict]:
    """将升序日线聚合为周/月 K 线（服务端读时聚合）

    - trade_date: 桶内最后一个真实交易日
    - open/close: 桶内首个/末个真实交易日价格；首日 open 缺失时保持缺失
    - high/low: 桶内极值；volume/turnover: 桶内求和
    - turnover_rate: 周/月聚合 fail-closed 置 None
    """
    bars: list[dict] = []
    current: dict | None = None
    current_key: tuple | None = None

    def _bucket_key(trade_date: date) -> tuple:
        if period == "week":
            iso = trade_date.isocalendar()
            return (iso[0], iso[1])
        return (trade_date.year, trade_date.month)

    for row in rows:
        raw = row["trade_date"]
        trade_date = raw if isinstance(raw, date) else date.fromisoformat(str(raw)[:10])
        key = _bucket_key(trade_date)
        if key != current_key:
            current_key = key
            current = {
                "trade_date": row["trade_date"],
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "turnover": row.get("turnover"),
                "turnover_rate": None,
            }
            bars.append(current)
            continue
        # 合并进当前桶
        current["trade_date"] = row["trade_date"]
        current["close"] = row.get("close")
        current["high"] = _merge_max(current["high"], row.get("high"))
        current["low"] = _merge_min(current["low"], row.get("low"))
        current["volume"] = _merge_sum(current["volume"], row.get("volume"))
        current["turnover"] = _merge_sum(current["turnover"], row.get("turnover"))
    return bars


def _merge_max(a: float | None, b: float | None) -> float | None:
    """取两者较大者，None 视为缺失"""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _merge_min(a: float | None, b: float | None) -> float | None:
    """取两者较小者，None 视为缺失"""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _merge_sum(a: float | None, b: float | None) -> float | None:
    """累加两者，None 视为缺失"""
    if a is None:
        return b
    if b is None:
        return a
    return a + b


@router.get("/{stock_code}/indicators")
def get_indicators(stock_code: str, request: Request) -> dict:
    """指标摘要 (PRD §14 SD3: 估值/盈利/成长/安全/股东回报)"""
    duck = request.app.state.duck
    rows = duck.read_query(
        """SELECT * FROM indicator_snapshot
           WHERE stock_code = ?
           ORDER BY report_date DESC LIMIT 1""",
        [stock_code],
    )

    if not rows:
        raise HTTPException(status_code=404, detail="no indicator data")

    indicators = rows[0]
    freshness = build_freshness_metadata(
        financial_date=indicators.get("report_date"),
        price_date=indicators.get("latest_price_date"),
        calculated_at=indicators.get("calculated_at"),
        data_version=indicators.get("data_version"),
    )

    # P1-4: 服务端权威信任决策；阻断警告存在时遮蔽快照数值，不再伪装为正常研究数据
    from app.core.data_quality import (
        indicator_trust,
        mask_untrusted_values,
        read_warning_codes,
    )

    trust = indicator_trust(read_warning_codes(duck, request.app.state.sqlite))

    # 按类别组织，附带 historical_capable 标志 (PRD §14 SD7)
    def _with_meta(values: dict) -> dict:
        untrusted = set(trust["untrusted_fields"])
        return {
            k: {
                "value": v,
                "historical_capable": INDICATOR_HISTORICAL_CAPABLE.get(k, True),
                "untrusted": trust["untrusted_all"] or k in untrusted,
            }
            for k, v in mask_untrusted_values(values, trust).items()
        }

    summary = {
        "valuation": _with_meta({
            "pe_ttm": indicators.get("pe_ttm"),
            "pb_mrq": indicators.get("pb_mrq"),
            "ps_ttm": indicators.get("ps_ttm"),
            "pcf_ttm": indicators.get("pcf_ttm"),
            "dividend_yield": indicators.get("dividend_yield"),
            "total_market_cap": indicators.get("total_market_cap"),
            "circ_market_cap": indicators.get("circ_market_cap"),
        }),
        "profitability": _with_meta({
            "roe": indicators.get("roe"),
            "roa": indicators.get("roa"),
            "gross_margin": indicators.get("gross_margin"),
            "net_margin": indicators.get("net_margin"),
            "roic": indicators.get("roic"),
            "cf_to_net_profit": indicators.get("cf_to_net_profit"),
        }),
        "growth": _with_meta({
            "revenue_yoy": indicators.get("revenue_yoy"),
            "net_profit_yoy": indicators.get("net_profit_yoy"),
            "deducted_profit_yoy": indicators.get("deducted_profit_yoy"),
            "revenue_cagr3": indicators.get("revenue_cagr3"),
            "revenue_cagr5": indicators.get("revenue_cagr5"),
            "net_profit_cagr3": indicators.get("net_profit_cagr3"),
            "net_profit_cagr5": indicators.get("net_profit_cagr5"),
            "deducted_profit_cagr3": indicators.get("deducted_profit_cagr3"),
            "deducted_profit_cagr5": indicators.get("deducted_profit_cagr5"),
        }),
        "safety": _with_meta({
            "debt_ratio": indicators.get("debt_ratio"),
            "current_ratio": indicators.get("current_ratio"),
            "quick_ratio": indicators.get("quick_ratio"),
            "interest_bearing_debt": indicators.get("interest_bearing_debt"),
            "interest_coverage": indicators.get("interest_coverage"),
            "goodwill_ratio": indicators.get("goodwill_ratio"),
        }),
        "shareholder_return": _with_meta({
            "payout_ratio": indicators.get("payout_ratio"),
            "dps": indicators.get("dps"),
            "consecutive_div_years": indicators.get("consecutive_div_years"),
        }),
    }

    return {
        "indicators": summary,
        "report_date": str(indicators.get("report_date", "")) if indicators.get("report_date") else None,
        "latest_close": None if trust["untrusted_all"] else indicators.get("latest_close"),
        "latest_price_date": str(indicators.get("latest_price_date", "")) if indicators.get("latest_price_date") else None,
        "freshness": freshness,
        "trust": trust,
    }


@router.get("/{stock_code}/financial-trend")
def get_financial_trend(
    stock_code: str,
    request: Request,
    period: Literal["annual", "quarterly", "ttm"] = "annual",
    years: int = Query(5, ge=1, le=99),
) -> dict:
    """财务趋势 (PRD §14 SD4-SD6: 年度默认, 可切季度/TTM, 可选1/3/5/10年/全部)

    years=99 表示全部历史数据。
    """
    duck = request.app.state.duck
    limit = 999 if years >= 99 else (years * 4 if period in ("quarterly", "ttm") else years)

    if period == "annual":
        rows = duck.read_query(
            """SELECT bs.report_date,
                       ic.revenue, ic.cost_of_revenue,
                       (ic.revenue - ic.cost_of_revenue) AS gross_profit,
                       ic.operating_profit, ic.net_profit,
                       ic.parent_net_profit, ic.deducted_net_profit,
                       ic.basic_eps,
                       bs.total_assets, bs.total_liabilities, bs.total_equity,
                       bs.total_equity_parent,
                       cf.cf_from_operating, cf.cf_from_investing, cf.cf_from_financing
                FROM balance_sheet bs
                LEFT JOIN income_statement ic
                    ON bs.stock_code = ic.stock_code AND bs.report_date = ic.report_date
                LEFT JOIN cash_flow cf
                    ON bs.stock_code = cf.stock_code AND bs.report_date = cf.report_date
                WHERE bs.stock_code = ?
                  AND EXTRACT(MONTH FROM bs.report_date) = 12
                ORDER BY bs.report_date DESC
                LIMIT ?""",
            [stock_code, limit],
        )
    else:  # quarterly or ttm needs every report period
        rows = duck.read_query(
            """SELECT bs.report_date,
                       ic.revenue, ic.cost_of_revenue,
                       (ic.revenue - ic.cost_of_revenue) AS gross_profit,
                       ic.operating_profit, ic.net_profit,
                       ic.parent_net_profit, ic.deducted_net_profit,
                       ic.basic_eps,
                       bs.total_assets, bs.total_liabilities, bs.total_equity,
                       bs.total_equity_parent,
                       cf.cf_from_operating, cf.cf_from_investing, cf.cf_from_financing
                FROM balance_sheet bs
                LEFT JOIN income_statement ic
                    ON bs.stock_code = ic.stock_code AND bs.report_date = ic.report_date
                LEFT JOIN cash_flow cf
                    ON bs.stock_code = cf.stock_code AND bs.report_date = cf.report_date
                WHERE bs.stock_code = ?
                ORDER BY bs.report_date DESC
                LIMIT ?""",
            [stock_code, limit],
        )

    if not rows:
        return {"trend": [], "period": period}

    _apply_published_overrides(rows, request.app.state.sqlite, stock_code)

    if period == "ttm":
        rows = calculate_ttm_trend(rows)
    elif period == "quarterly":
        rows = _to_single_quarter(rows)

    # 按时间正序
    else:
        rows.reverse()

    # 计算衍生指标
    trend = []
    for r in rows:
        revenue = r.get("revenue")
        parent_net_profit = r.get("parent_net_profit")
        net_profit = parent_net_profit if parent_net_profit is not None else r.get("net_profit")
        total_assets = r.get("total_assets")
        total_equity = r.get("total_equity_parent") if r.get("total_equity_parent") is not None else r.get("total_equity")

        item = {
            "report_date": str(r.get("report_date", "")),
            "revenue": revenue,
            "gross_profit": r.get("gross_profit"),
            "net_profit": net_profit,
            "parent_net_profit": parent_net_profit,
            "deducted_net_profit": r.get("deducted_net_profit"),
            "operating_profit": r.get("operating_profit"),
            "basic_eps": r.get("basic_eps"),
            "total_assets": total_assets,
            "total_liabilities": r.get("total_liabilities"),
            "total_equity": total_equity,
            "cf_from_operating": r.get("cf_from_operating"),
            # 衍生指标
            "gross_margin": (r["gross_profit"] / revenue) if revenue not in (None, 0) and r.get("gross_profit") is not None else None,
            "net_margin": (net_profit / revenue) if revenue not in (None, 0) and net_profit is not None else None,
            "debt_ratio": (r.get("total_liabilities") / total_assets) if total_assets not in (None, 0) and r.get("total_liabilities") is not None else None,
            "roe": (net_profit / total_equity) if total_equity not in (None, 0) and net_profit is not None else None,
        }
        trend.append(item)

    return {
        "trend": trend,
        "period": period,
        "period_semantic": "single_quarter" if period == "quarterly" else period,
        "count": len(trend),
    }


def _apply_published_overrides(rows: list[dict], sqlite, stock_code: str) -> None:
    """Apply the same current corrections used by the indicator calculator."""
    by_date = {str(row.get("report_date", ""))[:10]: row for row in rows}
    for override in sqlite.query(
        """SELECT field_name, report_date, override_value FROM manual_overrides
           WHERE stock_code = ? AND status = 'published' AND rolled_back_at IS NULL""",
        [stock_code],
    ):
        target = by_date.get(str(override.get("report_date") or "")[:10])
        if target is not None and override["field_name"] in target:
            target[override["field_name"]] = override["override_value"]


@router.get("/{stock_code}/business-overview")
def get_business_overview(stock_code: str, request: Request) -> dict:
    """业务概览 (reports/67 独立低频域: 公司资料 + 主营构成 + 溯源)

    - profile: 公司简介 / 经营范围 / 行业 / 员工数 + 来源 / 抓取时间 / 哈希 / 批次
    - breakdown: 最近报告期按产品/行业/地区构成 + 历史可得数据
    - provenance: 来源、抓取时间、raw hash、置信度、batch
    - 未知股票 → 404；已知股票但无业务数据（含北交所缺失）→ 显式 missing，不造值
    """
    duck = request.app.state.duck
    exists = duck.read_query(
        "SELECT 1 FROM stock_meta WHERE stock_code = ?", [stock_code]
    )
    if not exists:
        raise HTTPException(status_code=404, detail="stock not found")

    profile_rows = duck.read_query(
        """SELECT code, name, org_name, profile, scope, employee_num,
                  csrc_industry, trade_market, source, fetch_time, raw_hash,
                  confidence, batch_id
           FROM company_profile WHERE stock_code = ?""",
        [stock_code],
    )
    breakdown_rows = duck.read_query(
        """SELECT report_date, type, item_name, amount, ratio, rank,
                  source, fetch_time, raw_hash, confidence, batch_id
           FROM business_breakdown
           WHERE stock_code = ?
           ORDER BY report_date DESC, type, rank""",
        [stock_code],
    )

    type_labels = {1: "产品", 2: "行业", 3: "地区"}

    def _provenance(row: dict) -> dict:
        return {
            "source": row.get("source"),
            "fetch_time": str(row.get("fetch_time")) if row.get("fetch_time") else None,
            "raw_hash": row.get("raw_hash"),
            "confidence": row.get("confidence"),
            "batch_id": row.get("batch_id"),
        }

    profile = None
    if profile_rows:
        row = profile_rows[0]
        profile = {
            "status": "ok",
            "code": row.get("code"),
            "name": row.get("name"),
            "org_name": row.get("org_name"),
            "profile": row.get("profile"),
            "scope": row.get("scope"),
            "employee_num": row.get("employee_num"),
            "csrc_industry": row.get("csrc_industry"),
            "trade_market": row.get("trade_market"),
        }
        profile["provenance"] = _provenance(row)
    else:
        profile = {"status": "missing"}

    latest_report_date = None
    if breakdown_rows:
        latest_report_date = str(breakdown_rows[0]["report_date"])[:10]

    composition: dict[str, list[dict]] = {}
    history: list[dict] = []
    breakdown_provenance = None
    for row in breakdown_rows:
        report_date = str(row["report_date"])[:10]
        breakdown_type = int(row["type"] or 0)
        item = {
            "report_date": report_date,
            "type": breakdown_type,
            "type_label": type_labels.get(breakdown_type, str(breakdown_type)),
            "item_name": row.get("item_name"),
            "amount": row.get("amount"),
            "ratio": row.get("ratio"),
            "rank": row.get("rank"),
        }
        history.append(item)
        if report_date == latest_report_date:
            composition.setdefault(str(breakdown_type), []).append(item)
        if breakdown_provenance is None:
            breakdown_provenance = _provenance(row)

    return {
        "stock_code": stock_code,
        "profile": profile,
        "breakdown": {
            "status": "ok" if breakdown_rows else "missing",
            "latest_report_date": latest_report_date,
            "composition": composition,
            "history": history,
            "provenance": breakdown_provenance,
        },
        "provenance": {
            "profile": profile.get("provenance") if profile else None,
            "breakdown": breakdown_provenance,
        },
    }


@router.get("/{stock_code}/treasury-comparison")
def get_treasury_comparison(
    stock_code: str,
    request: Request,
    tenor: float = Query(10.0),
    limit: int = Query(250, ge=1, le=1000),
) -> dict:
    """国债比较 (reports/68 P3): TTM 已实施股息率与所选期限国债收益率

    - series: 按该股价格日降序取最新 limit 天，每天返回 TTM 已实施股息率、
      对齐国债收益率（≤价格日最近点、最大陈旧 5 自然日）、利差与对齐信息。
    - 曲线缺失/陈旧/股息率不可算时该天对应字段为 null 并带 reason。
    - 未知股票 404；股票存在但无曲线数据 → series 逐点 reason=curve_missing。
    """
    from app.core.adapters.czb_mof_adapter import KEY_TENORS
    from app.core.treasury import MAX_STALENESS_DAYS, TreasuryCurveUpdater
    # P3-3 修复（reports/73）：与服务端信任遮蔽模型一致——
    # DIVIDEND_DATES_UNVERIFIED 或快照级警告生效时遮蔽 TTM 股息率/利差。
    from app.core.data_quality import (
        indicator_trust,
        mask_untrusted_values,
        read_warning_codes,
    )

    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    exists = duck.read_query(
        "SELECT 1 FROM stock_meta WHERE stock_code = ?", [stock_code]
    )
    if not exists:
        raise HTTPException(status_code=404, detail="stock not found")
    if tenor not in KEY_TENORS:
        raise HTTPException(status_code=422, detail=f"不支持的期限: {tenor}")

    trust = indicator_trust(read_warning_codes(duck, sqlite))
    masked = mask_untrusted_values(
        {"ttm_dividend_yield": 0.0, "div_yield_spread_10y": 0.0}, trust
    )
    # 遮蔽仅影响股息率/利差字段值；curve_yield（国债基准本身）不受分红可信度影响
    div_trusted = masked.get("ttm_dividend_yield") is not None

    price_rows = duck.read_query(
        """SELECT trade_date, close FROM price_daily_raw
           WHERE stock_code = ? AND close IS NOT NULL
           ORDER BY trade_date DESC LIMIT ?""",
        [stock_code, limit],
    )
    if not price_rows:
        return {
            "stock_code": stock_code,
            "tenor": tenor,
            "tenors_available": list(KEY_TENORS),
            "series": [],
            "missing": True,
            "provenance": None,
        }

    curve_align = TreasuryCurveUpdater(duck=duck, sqlite=sqlite)
    series: list[dict] = []
    for row in price_rows:
        price_date = str(row["trade_date"])[:10]
        close = float(row["close"]) if row.get("close") is not None else None
        item: dict = {
            "price_date": price_date,
            "ttm_div_yield": None,
            "curve_yield": None,
            "spread": None,
            "curve_date": None,
            "staleness_days": None,
            "reason": None,
        }
        if close is None or close <= 0:
            item["reason"] = "no_price"
            series.append(item)
            continue
        div_rows = duck.read_query(
            """SELECT SUM(dividend_per_share) AS dps
               FROM dividends
               WHERE stock_code = ?
                 AND dividend_per_share IS NOT NULL
                 AND dividend_per_share > 0
                 AND announcement_date IS NOT NULL
                 AND announcement_date <= CAST(? AS DATE)
                 AND ex_date <= CAST(? AS DATE)
                 AND ex_date >= CAST(? AS DATE) - INTERVAL '1 year'""",
            [stock_code, price_date, price_date, price_date],
        )
        ttm_dps = div_rows[0].get("dps") if div_rows else None
        ttm_div_yield = (float(ttm_dps) / close) * 100.0 if ttm_dps and close > 0 else None
        item["ttm_div_yield"] = ttm_div_yield

        align = curve_align.align(price_date, tenor)
        item["curve_date"] = align["curve_date"]
        item["staleness_days"] = align["staleness_days"]
        if align["status"] != "ok" or align["yield_pct"] is None:
            item["reason"] = align["reason"]
            series.append(item)
            continue
        item["curve_yield"] = align["yield_pct"]
        # P3-3 修复（reports/73）：股息率/利差按信任遮蔽（曲线基准不受影响）
        if not div_trusted:
            item["reason"] = "dividend_untrusted"
        elif ttm_div_yield is None:
            item["reason"] = "no_dividend"
        else:
            item["spread"] = ttm_div_yield - align["yield_pct"]
        series.append(item)

    # 响应级信任标注（P3-3）：快照级警告（untrusted_all）遮蔽全部字段
    if not div_trusted:
        mask_reason = "dividend_untrusted" if trust["untrusted_fields"] else "snapshot_untrusted"
        for item in series:
            if item.get("spread") is not None or item.get("ttm_div_yield") is not None:
                item["ttm_div_yield"] = None
                item["spread"] = None
                item["reason"] = mask_reason

    # 溯源（曲线侧最新一批）
    curve_rows = duck.read_query(
        """SELECT source, fetch_time, raw_hash, batch_id, confidence
           FROM treasury_yield_curve
           WHERE tenor_years = ?
           ORDER BY curve_date DESC LIMIT 1""",
        [tenor],
    )
    provenance = None
    if curve_rows:
        row = curve_rows[0]
        provenance = {
            "source": row.get("source"),
            "fetch_time": str(row.get("fetch_time")) if row.get("fetch_time") else None,
            "raw_hash": row.get("raw_hash"),
            "batch_id": row.get("batch_id"),
            "confidence": row.get("confidence"),
        }

    return {
        "stock_code": stock_code,
        "tenor": tenor,
        "tenors_available": list(KEY_TENORS),
        "max_staleness_days": MAX_STALENESS_DAYS,
        "series": series,
        "missing": all(item.get("reason") is not None for item in series),
        "provenance": provenance,
        "trust": {
            "dividend_trusted": div_trusted,
            "warning_codes": trust["warning_codes"],
            "untrusted_all": trust["untrusted_all"],
        },
    }


@router.get("/{stock_code}/research-statistics")
def get_research_statistics(
    stock_code: str,
    request: Request,
    metric: Literal["pe_ttm", "pb_mrq", "ttm_dividend_yield", "spread_10y"] = "pe_ttm",
    window_years: Literal[1, 3, 5, 10, 99] = 10,
) -> dict:
    """历史研究统计 (reports/68 P5/P4): 序列 + 窗口聚合统计

    - series: 按价格日升序的研究序列（最新重述回看口径，不用于回测）
    - statistics: 各窗口聚合（样本数/分位带/μσ/z-score/覆盖/原因码）
    - 股本缺失日 PE/PB 为 null；窗口不足最小样本或覆盖<90% → reason
    """
    from app.core.statistics import (
        COVERAGE_THRESHOLD_PCT,
        StatisticsBuilder,
        WINDOW_MIN_SAMPLES,
    )

    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    exists = duck.read_query(
        "SELECT 1 FROM stock_meta WHERE stock_code = ?", [stock_code]
    )
    if not exists:
        raise HTTPException(status_code=404, detail="stock not found")

    builder = StatisticsBuilder(duck=duck, sqlite=sqlite)
    series = builder.build_series(stock_code)
    statistics: dict[str, Any] = {}
    for window in (1, 3, 5, 10, 99):
        stats = builder.window_stats(
            series, metric, window, WINDOW_MIN_SAMPLES[window],
        )
        statistics[f"{window}y"] = stats
    return {
        "stock_code": stock_code,
        "metric": metric,
        "window_years": window_years,
        "series": series,
        "statistics": statistics,
        "coverage_threshold_pct": COVERAGE_THRESHOLD_PCT,
        # P4-13 修复（reports/73）：标注实时计算口径——详情页统计为实时构建，
        # 与筛选使用的已发布统计域（research_statistics 表）可能存在时差，
        # 发布域过期时两者分位/z-score 可能不一致。
        "computed": "realtime",
        "disclaimer": "最新重述回看口径：历史日使用该日对应报告期当前最新重述财务值与历史有效总股本，不代表当时市场可见信息，不用于回测",
    }


@router.get("/{stock_code}/source-audit")
def get_source_audit(
    stock_code: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """溯源信息 (PRD §14 SD8-SD10: 报告期/来源/置信度/公式)

    M4-问题5修复: 补充生效日期/数据版本/公式/as_reported差异
    """
    duck = request.app.state.duck

    # Return stored lineage metadata; legacy rows remain visibly incomplete.
    audit_rows = duck.read_query(
        """SELECT field_name, report_date, value, source,
                  fetch_time, confidence, reason_code, is_override,
                  api_version,
                   effective_date,
                   data_version,
                   formula,
                  NULL AS as_reported_value,
                  NULL AS latest_restated_diff
           FROM source_audit
           WHERE stock_code = ?
           ORDER BY fetch_time DESC
           LIMIT ?""",
        [stock_code, limit],
    )

    # M4-问题5: 为已知指标添加公式描述
    formula_map = {
        "pe_ttm": "total_market_cap / TTM(parent_net_profit)",
        "pb_mrq": "total_market_cap / total_equity_parent (MRQ)",
        "ps_ttm": "total_market_cap / TTM(revenue)",
        "pcf_ttm": "total_market_cap / TTM(cf_from_operating)",
        "roe": "TTM(parent_net_profit) / total_equity_parent",
        "roa": "TTM(net_profit) / total_assets",
        "gross_margin": "(revenue - cost_of_revenue) / revenue",
        "net_margin": "net_profit / revenue",
        "debt_ratio": "total_liabilities / total_assets",
        "current_ratio": "total_current_assets / total_current_liabilities",
        "quick_ratio": "(total_current_assets - inventory) / total_current_liabilities",
        "dividend_yield": "annual_dps / latest_close",
    }
    for row in audit_rows:
        if not row.get("formula") and row.get("field_name") in formula_map:
            row["formula"] = formula_map[row["field_name"]]

    # 批次级溯源
    batch_rows = duck.read_query(
        """SELECT batch_id, data_type, source, fetch_time,
                  row_count, confidence
           FROM fetch_batch
           WHERE batch_id IN (
               SELECT DISTINCT fetch_batch_id FROM source_audit
               WHERE stock_code = ?
           )
           ORDER BY fetch_time DESC
           LIMIT ?""",
        [stock_code, limit],
    )

    return {
        "field_audit": audit_rows,
        "batch_audit": batch_rows,
    }


def _calc_ma(closes: list[float | None], period: int) -> list[float | None]:
    """计算移动平均线"""
    result: list[float | None] = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            window = closes[i - period + 1 : i + 1]
            valid = [v for v in window if v is not None]
            if valid:
                result.append(sum(valid) / len(valid))
            else:
                result.append(None)
    return result


# ─── M4-问题2: 自定义指标趋势 ────────────────────────────────────

# 可用于自定义趋势的标准化财务字段
CUSTOM_TREND_FIELDS: dict[str, str] = {
    # 利润表
    "revenue": "ic.revenue", "cost_of_revenue": "ic.cost_of_revenue",
    "operating_profit": "ic.operating_profit", "net_profit": "ic.net_profit",
    "parent_net_profit": "ic.parent_net_profit", "deducted_net_profit": "ic.deducted_net_profit",
    "basic_eps": "ic.basic_eps", "selling_expenses": "ic.selling_expenses",
    "administrative_expenses": "ic.administrative_expenses",
    "financial_expenses": "ic.financial_expenses", "rd_expenses": "ic.rd_expenses",
    # 资产负债表
    "total_assets": "bs.total_assets", "total_liabilities": "bs.total_liabilities",
    "total_equity": "bs.total_equity", "total_equity_parent": "bs.total_equity_parent",
    "monetary_funds": "bs.monetary_funds", "inventory": "bs.inventory",
    "accounts_receivable": "bs.accounts_receivable", "goodwill": "bs.goodwill",
    "paid_in_capital": "bs.paid_in_capital",
    # 现金流量表
    "cf_from_operating": "cf.cf_from_operating",
    "cf_from_investing": "cf.cf_from_investing",
    "cf_from_financing": "cf.cf_from_financing",
}


@router.get("/{stock_code}/custom-trend")
def get_custom_trend(
    stock_code: str,
    request: Request,
    fields: str = Query("revenue,parent_net_profit,gross_margin,roe"),
    years: int = Query(5, ge=1, le=99),
) -> dict:
    """自定义指标趋势 (PRD §14 SD4: 自定义数值指标视图)

    用户可选择任意标准化财务字段查看趋势。
    """
    duck = request.app.state.duck
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    limit = 999 if years >= 99 else years

    # 构建查询字段（只允许白名单中的字段，防注入）
    select_parts: list[str] = ["bs.report_date"]
    valid_fields: list[str] = []
    for f in field_list:
        if f in CUSTOM_TREND_FIELDS:
            select_parts.append(f"{CUSTOM_TREND_FIELDS[f]} AS {f}")
            valid_fields.append(f)
        elif f in ("gross_margin", "net_margin", "debt_ratio", "roe"):
            # 衍生指标
            if f == "gross_margin":
                select_parts.append("(ic.revenue - ic.cost_of_revenue) AS _gross_profit")
            elif f == "net_margin":
                select_parts.append("NULL AS _net_margin_placeholder")
            elif f == "debt_ratio":
                select_parts.append("NULL AS _debt_ratio_placeholder")
            elif f == "roe":
                select_parts.append("NULL AS _roe_placeholder")
            valid_fields.append(f)

    if not valid_fields:
        return {"trend": [], "fields": []}

    sql = f"""SELECT {', '.join(select_parts)}
              FROM balance_sheet bs
              LEFT JOIN income_statement ic
                  ON bs.stock_code = ic.stock_code AND bs.report_date = ic.report_date
              LEFT JOIN cash_flow cf
                  ON bs.stock_code = cf.stock_code AND bs.report_date = cf.report_date
              WHERE bs.stock_code = ?
                AND EXTRACT(MONTH FROM bs.report_date) = 12
              ORDER BY bs.report_date DESC
              LIMIT ?"""

    rows = duck.read_query(sql, [stock_code, limit])
    if not rows:
        return {"trend": [], "fields": valid_fields}

    rows.reverse()
    trend = []
    for r in rows:
        item: dict = {"report_date": str(r.get("report_date", ""))}
        revenue = r.get("revenue")
        parent_net_profit = r.get("parent_net_profit")
        net_profit = parent_net_profit if parent_net_profit is not None else r.get("net_profit")
        total_assets = r.get("total_assets")
        total_equity = r.get("total_equity_parent") if r.get("total_equity_parent") is not None else r.get("total_equity")

        for f in valid_fields:
            if f in CUSTOM_TREND_FIELDS:
                item[f] = r.get(f)
            elif f == "gross_margin":
                gp = r.get("_gross_profit")
                item[f] = (gp / revenue) if gp is not None and revenue not in (None, 0) else None
            elif f == "net_margin":
                item[f] = (net_profit / revenue) if net_profit is not None and revenue not in (None, 0) else None
            elif f == "debt_ratio":
                tl = r.get("total_liabilities")
                item[f] = (tl / total_assets) if tl is not None and total_assets not in (None, 0) else None
            elif f == "roe":
                item[f] = (net_profit / total_equity) if net_profit is not None and total_equity not in (None, 0) else None
        trend.append(item)

    return {"trend": trend, "fields": valid_fields, "count": len(trend)}


@router.get("/{stock_code}/available-fields")
def get_available_fields() -> dict:
    """列出可用于自定义趋势的字段"""
    return {"fields": list(CUSTOM_TREND_FIELDS.keys()) + ["gross_margin", "net_margin", "debt_ratio", "roe"]}


# ─── M4-问题3: PDF 浏览器打开（最小实现） ──────────────────────────

@router.get("/{stock_code}/pdf/{filename}", response_model=None)
def serve_pdf(
    stock_code: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """打开已恢复到本地的 PDF (PRD §14 SD9)

    M8-问题2修复: 热数据找不到时检查冷归档, 返回归档位置与恢复指引 (PRD §18.2 AR6)
    P0#14修复: 防止路径遍历攻击 (stock_code/filename 只允许字母数字/下划线/点)
    """
    import re

    # P0#14修复: 白名单验证 stock_code 和 filename, 防止路径遍历
    # stock_code: 6位数字; filename: 字母数字+下划线+点+连字符, 以.pdf结尾
    if not re.match(r"^\d{6}$", stock_code):
        raise HTTPException(status_code=400, detail="Invalid stock code")
    if not re.match(r"^[a-zA-Z0-9_.\-]+\.pdf$", filename, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid filename")
    # 额外检查: 不允许 ".." 或 "/" 或 "\" 在 filename 中
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    pdf_dir = PDFManager(sqlite=request.app.state.sqlite).hot_dir / stock_code
    pdf_path = (pdf_dir / filename).resolve()

    # 二次验证: 确保解析后的路径仍在 pdf_dir 内
    try:
        pdf_path.relative_to(pdf_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal detected")

    if pdf_path.exists():
        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            filename=filename,
        )

    # M8-问题2: 检查冷归档 (PRD §18.2 AR6: 显示归档位置与恢复指引)
    mgr = PDFManager(sqlite=request.app.state.sqlite)
    if mgr.is_in_archive(stock_code, filename):
        archived = next(
            (item for item in mgr.list_archived_pdfs(stock_code) if item["filename"] == filename),
            {},
        )
        raise HTTPException(status_code=409, detail={
            "error": "PDF is in cold archive",
            "recovery_instruction": f"请通过 CLI 恢复: vd data restore_pdf {stock_code} {filename}",
            "stock_code": stock_code,
            "filename": filename,
            # C13修复(报告41): 绝对路径脱敏——只暴露逻辑归档位置，不泄露本机路径
            "archive_path": f"archive/{stock_code}/{filename}",
            "checksum": archived.get("checksum"),
            "integrity_verified": archived.get("integrity_verified", False),
        })

    raise HTTPException(status_code=404, detail={
        "error": "PDF not found",
        # C13修复(报告41): 逻辑路径而非本机绝对路径
        "path": f"pdf/{stock_code}/{filename}",
        "hint": "请先通过 CLI 下载 PDF: vd data download_pdf <stock_code>",
    })


@router.get("/{stock_code}/pdf-list")
def list_pdfs(stock_code: str, request: Request) -> dict:
    """列出已下载的 PDF 文件"""
    import re

    if not re.fullmatch(r"\d{6}", stock_code):
        raise HTTPException(status_code=400, detail="Invalid stock code")
    pdf_dir = PDFManager(sqlite=request.app.state.sqlite)._stock_dir(stock_code)

    files = []
    if pdf_dir.exists():
        for f in pdf_dir.glob("*.pdf"):
            files.append({"filename": f.name, "size_bytes": f.stat().st_size, "archived": False})
    mgr = PDFManager(sqlite=request.app.state.sqlite)
    hot_names = {item["filename"] for item in files}
    files.extend(item for item in mgr.list_archived_pdfs(stock_code) if item["filename"] not in hot_names)
    return {"files": files, "count": len(files)}
