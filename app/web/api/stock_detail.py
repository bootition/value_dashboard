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

from datetime import date, datetime

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import FileResponse
from typing import Literal

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
        item["net_profit"] = item.get("parent_net_profit") or item.get("net_profit")
        item["gross_profit"] = (
            revenue - cost if revenue is not None and cost is not None else None
        )
        trend.append(item)
    return trend


def build_freshness_metadata(
    financial_date: date | None,
    price_date: date | None,
    calculated_at: datetime | str | None,
    data_version: str | None,
) -> dict:
    """Describe the dates behind an indicator and flag material staleness."""
    stale_days = (
        (price_date - financial_date).days
        if financial_date is not None and price_date is not None
        else None
    )
    return {
        "financial_effective_date": financial_date.isoformat() if financial_date else None,
        "price_date": price_date.isoformat() if price_date else None,
        "calculated_at": str(calculated_at) if calculated_at is not None else None,
        "data_version": data_version,
        "stale_days": stale_days,
        "stale_warning": stale_days is None or stale_days > 365,
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


@router.get("/{stock_code}/info")
async def get_stock_info(stock_code: str, request: Request) -> dict:
    """股票基本信息 (PRD §14 SD1: 代码/名称/拼音/最近收盘价/价格日期)"""
    duck = request.app.state.duck
    rows = duck.read_query(
        "SELECT stock_code, name, pinyin, exchange, listing_date, "
        "is_st, is_suspended, sw_level1, sw_level2 "
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
async def get_kline(
    stock_code: str,
    request: Request,
    adjust: Literal["raw", "qfq"] = "raw",
    days: int = Query(250, ge=1, le=2000),
) -> dict:
    """K线数据 (PRD §14 SD2: 日K/成交量/均线/raw与qfq切换)"""
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
        return {"candles": [], "adjust": adjust}

    # 按时间正序排列（旧→新）
    rows.reverse()

    # 计算 MA 线
    closes = [r["close"] for r in rows]
    for period in [5, 10, 20, 60, 120, 250]:
        ma_values = _calc_ma(closes, period)
        for i, r in enumerate(rows):
            r[f"ma{period}"] = ma_values[i]

    return {
        "candles": rows,
        "adjust": adjust,
        "count": len(rows),
    }


@router.get("/{stock_code}/indicators")
async def get_indicators(stock_code: str, request: Request) -> dict:
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

    # 按类别组织，附带 historical_capable 标志 (PRD §14 SD7)
    def _with_meta(values: dict) -> dict:
        return {k: {"value": v, "historical_capable": INDICATOR_HISTORICAL_CAPABLE.get(k, True)}
                for k, v in values.items()}

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
        "latest_close": indicators.get("latest_close"),
        "latest_price_date": str(indicators.get("latest_price_date", "")) if indicators.get("latest_price_date") else None,
        "freshness": freshness,
    }


@router.get("/{stock_code}/financial-trend")
async def get_financial_trend(
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

    if period == "ttm":
        rows = calculate_ttm_trend(rows)

    # 按时间正序
    else:
        rows.reverse()

    # 计算衍生指标
    trend = []
    for r in rows:
        revenue = r.get("revenue")
        net_profit = r.get("parent_net_profit") or r.get("net_profit")
        total_assets = r.get("total_assets")
        total_equity = r.get("total_equity_parent") or r.get("total_equity")

        item = {
            "report_date": str(r.get("report_date", "")),
            "revenue": revenue,
            "gross_profit": r.get("gross_profit"),
            "net_profit": net_profit,
            "deducted_net_profit": r.get("deducted_net_profit"),
            "operating_profit": r.get("operating_profit"),
            "basic_eps": r.get("basic_eps"),
            "total_assets": total_assets,
            "total_liabilities": r.get("total_liabilities"),
            "total_equity": total_equity,
            "cf_from_operating": r.get("cf_from_operating"),
            # 衍生指标
            "gross_margin": (r["gross_profit"] / revenue) if revenue and revenue != 0 and r.get("gross_profit") else None,
            "net_margin": (net_profit / revenue) if revenue and revenue != 0 and net_profit else None,
            "debt_ratio": (r.get("total_liabilities") / total_assets) if total_assets and total_assets != 0 and r.get("total_liabilities") else None,
            "roe": (net_profit / total_equity) if total_equity and total_equity != 0 and net_profit else None,
        }
        trend.append(item)

    return {"trend": trend, "period": period, "count": len(trend)}


@router.get("/{stock_code}/source-audit")
async def get_source_audit(
    stock_code: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """溯源信息 (PRD §14 SD8-SD10: 报告期/来源/置信度/公式)

    M4-问题5修复: 补充生效日期/数据版本/公式/as_reported差异
    """
    duck = request.app.state.duck

    # 关键字段溯源 (补充 effective_date/data_version/formula)
    audit_rows = duck.read_query(
        """SELECT field_name, report_date, value, source,
                  fetch_time, confidence, reason_code, is_override,
                  api_version,
                  report_date AS effective_date,
                  'latest_restated' AS data_version,
                  '' AS formula,
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
        if row.get("field_name") in formula_map:
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
async def get_custom_trend(
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
        net_profit = r.get("parent_net_profit") or r.get("net_profit")
        total_assets = r.get("total_assets")
        total_equity = r.get("total_equity_parent") or r.get("total_equity")

        for f in valid_fields:
            if f in CUSTOM_TREND_FIELDS:
                item[f] = r.get(f)
            elif f == "gross_margin":
                gp = r.get("_gross_profit")
                item[f] = (gp / revenue) if gp and revenue and revenue != 0 else None
            elif f == "net_margin":
                item[f] = (net_profit / revenue) if net_profit and revenue and revenue != 0 else None
            elif f == "debt_ratio":
                tl = r.get("total_liabilities")
                item[f] = (tl / total_assets) if tl and total_assets and total_assets != 0 else None
            elif f == "roe":
                item[f] = (net_profit / total_equity) if net_profit and total_equity and total_equity != 0 else None
        trend.append(item)

    return {"trend": trend, "fields": valid_fields, "count": len(trend)}


@router.get("/{stock_code}/available-fields")
async def get_available_fields() -> dict:
    """列出可用于自定义趋势的字段"""
    return {"fields": list(CUSTOM_TREND_FIELDS.keys()) + ["gross_margin", "net_margin", "debt_ratio", "roe"]}


# ─── M4-问题3: PDF 浏览器打开（最小实现） ──────────────────────────

@router.get("/{stock_code}/pdf/{filename}", response_model=None)
async def serve_pdf(
    stock_code: str,
    filename: str,
    request: Request,
) -> FileResponse | dict:
    """打开已恢复到本地的 PDF (PRD §14 SD9)

    M8-问题2修复: 热数据找不到时检查冷归档, 返回归档位置与恢复指引 (PRD §18.2 AR6)
    P0#14修复: 防止路径遍历攻击 (stock_code/filename 只允许字母数字/下划线/点)
    """
    import re

    # P0#14修复: 白名单验证 stock_code 和 filename, 防止路径遍历
    # stock_code: 6位数字; filename: 字母数字+下划线+点+连字符, 以.pdf结尾
    if not re.match(r"^\d{6}$", stock_code):
        return {"error": "Invalid stock code"}
    if not re.match(r"^[a-zA-Z0-9_.\-]+\.pdf$", filename, re.IGNORECASE):
        return {"error": "Invalid filename"}
    # 额外检查: 不允许 ".." 或 "/" 或 "\" 在 filename 中
    if ".." in filename or "/" in filename or "\\" in filename:
        return {"error": "Invalid filename"}

    cfg = request.app.state.config
    pdf_dir = cfg.project_root / "data" / "pdf" / stock_code
    pdf_path = (pdf_dir / filename).resolve()

    # 二次验证: 确保解析后的路径仍在 pdf_dir 内
    try:
        pdf_path.relative_to(pdf_dir.resolve())
    except ValueError:
        return {"error": "Path traversal detected"}

    if pdf_path.exists():
        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            filename=filename,
        )

    # M8-问题2: 检查冷归档 (PRD §18.2 AR6: 显示归档位置与恢复指引)
    from app.core.pdf.manager import PDFManager
    mgr = PDFManager(sqlite=request.app.state.sqlite)
    if mgr.is_in_archive(stock_code, filename):
        archive_path = cfg.project_root / "data" / "archive_pdf" / stock_code / filename
        return {
            "error": "PDF is in cold archive",
            "archive_location": str(archive_path),
            "recovery_instruction": f"请通过 CLI 恢复: vd data restore_pdf {stock_code} {filename}",
            "stock_code": stock_code,
            "filename": filename,
        }

    return {
        "error": "PDF not found",
        "path": str(pdf_path),
        "hint": "请先通过 CLI 下载 PDF: vd data download_pdf <stock_code>",
    }


@router.get("/{stock_code}/pdf-list")
async def list_pdfs(stock_code: str, request: Request) -> dict:
    """列出已下载的 PDF 文件"""
    cfg = request.app.state.config
    pdf_dir = cfg.project_root / "data" / "pdf" / stock_code

    if not pdf_dir.exists():
        return {"files": [], "count": 0}

    files = []
    for f in pdf_dir.glob("*.pdf"):
        files.append({
            "filename": f.name,
            "size_bytes": f.stat().st_size,
        })
    return {"files": files, "count": len(files)}
