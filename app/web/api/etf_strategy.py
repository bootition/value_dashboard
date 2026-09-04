"""ETF 轮动工作台 API（2026-09-05）

- GET 只读：持仓/网格/信号汇总与单 ETF 详情（不写库，不落卖出计划）
- POST 用户录入：ETF 元数据、交易流水、资金流水、设置（总资产等）
数据契约与 ETF 工作台前端一一对应；估值分位复用 /api/index 计算域。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.etf_strategy import (
    add_cash_flow,
    add_etf_trade,
    get_setting,
    grid_state,
    latest_close,
    load_etf_meta,
    set_setting,
    signal_zone,
    upsert_etf_meta,
)
from app.core.index_dashboard import index_summary, valuation_detail

router = APIRouter(prefix="/api/etf", tags=["etf-strategy"])


class MetaIn(BaseModel):
    etf_code: str
    name: str
    track_index_code: str | None = None
    track_index_name: str | None = None
    primary_metric: str = "pe"
    industry_group: str | None = None
    budget: float = 0.0
    step_pct: float = 5.0
    enabled: bool = True
    note: str | None = None


class TradeIn(BaseModel):
    etf_code: str
    trade_date: str
    direction: str
    price: float
    shares: float
    amount: float | None = None
    fee: float = 0.0
    note: str | None = None


class CashFlowIn(BaseModel):
    flow_date: str
    direction: str
    amount: float
    note: str | None = None


class SettingIn(BaseModel):
    key: str
    value: str


def _track_metric(summary: dict[str, Any], primary_metric: str) -> float | None:
    if primary_metric == "pb":
        return summary.get("pb_percentile")
    return summary.get("pe_percentile")


def _overview_item(
    request: Request, meta: dict[str, Any],
) -> dict[str, Any]:
    code = meta["etf_code"]
    track_code = meta["track_index_code"]
    valuation: dict[str, Any] = (
        index_summary(request.app.state.duck, track_code)
        if track_code
        else {"status": "unavailable", "pe_percentile": None, "pb_percentile": None,
              "pe": None, "pb": None, "erp": None, "erp_percentile": None,
              "samples": 0, "latest_date": None}
    )
    percentile = _track_metric(valuation, meta["primary_metric"])
    percentile_label = "PB分位" if meta["primary_metric"] == "pb" else "PE分位"
    percentile_source = "指数近10年"
    if percentile is None and meta["primary_metric"] == "pe":
        # 港股/中概等无指数估值历史：同花顺跟踪指数 PE-TTM 五年分位兜底
        track_rows = request.app.state.duck.read_query(
            """SELECT track_pe_ttm_five_year_percentile FROM etf_daily
               WHERE etf_code = ? AND track_pe_ttm_five_year_percentile IS NOT NULL
               ORDER BY trade_date DESC LIMIT 1""",
            [code],
        )
        if track_rows:
            percentile = float(track_rows[0]["track_pe_ttm_five_year_percentile"])
            percentile_label = "PE分位(同花顺5年)"
            percentile_source = "跟踪指数5年"
    signal = signal_zone(percentile)
    current_price = latest_close(request.app.state.duck, code)
    state = grid_state(
        request.app.state.sqlite,
        etf_code=code,
        current_price=current_price,
        signal=signal,
        persist_sell_plan=False,
    )
    return {
        **state,
        "valuation": valuation,
        "percentile": percentile,
        "percentile_label": percentile_label,
        "percentile_source": percentile_source,
    }


@router.get("/overview")
async def get_overview(request: Request) -> dict[str, Any]:
    metas = load_etf_meta(request.app.state.sqlite)
    items = [_overview_item(request, meta) for meta in metas]
    cash_rows = request.app.state.sqlite.query(
        """SELECT direction, COALESCE(SUM(amount), 0) AS amount
           FROM etf_cash_flows GROUP BY direction"""
    )
    cash = {row["direction"]: row["amount"] for row in cash_rows}
    net_in = float(cash.get("in", 0)) - float(cash.get("out", 0))
    market_value = sum(
        float(item.get("market_value") or 0) for item in items
    )
    unrealized = sum(
        float(item.get("unrealized_pnl") or 0) for item in items
    )
    realized = sum(
        float(item.get("position", {}).get("realized_pnl") or 0) for item in items
    )
    return {
        "items": items,
        "total_assets": get_setting(request.app.state.sqlite, "total_assets"),
        "cash_net_in": round(net_in, 2),
        "market_value": round(market_value, 2),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": round(realized, 2),
    }


@router.get("/{etf_code}/detail")
async def get_detail(request: Request, etf_code: str) -> dict[str, Any]:
    metas = [m for m in load_etf_meta(request.app.state.sqlite) if m["etf_code"] == etf_code]
    if not metas:
        raise HTTPException(status_code=404, detail=f"ETF 未配置: {etf_code}")
    meta = metas[0]
    item = _overview_item(request, meta)
    track_code = meta["track_index_code"]
    valuation = (
        valuation_detail(request.app.state.duck, track_code) if track_code else None
    )
    trades = request.app.state.sqlite.query(
        """SELECT id, etf_code, trade_date, direction, price, shares, amount, fee, note
           FROM etf_trades WHERE etf_code = ? ORDER BY trade_date DESC, id DESC""",
        [etf_code],
    )
    cash_flows = request.app.state.sqlite.query(
        """SELECT id, flow_date, direction, amount, note
           FROM etf_cash_flows ORDER BY flow_date DESC, id DESC"""
    )
    return {
        **item,
        "track_valuation": valuation,
        "trades": trades,
        "cash_flows": cash_flows,
        "settings": {
            "total_assets": get_setting(request.app.state.sqlite, "total_assets"),
            "budget": meta["budget"],
            "step_pct": meta["step_pct"],
        },
    }


@router.post("/meta")
async def post_meta(request: Request, body: MetaIn) -> dict[str, Any]:
    try:
        return upsert_etf_meta(request.app.state.sqlite, **body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/trades")
async def post_trade(request: Request, body: TradeIn) -> dict[str, Any]:
    try:
        return add_etf_trade(request.app.state.sqlite, **body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/cash-flows")
async def post_cash_flow(request: Request, body: CashFlowIn) -> dict[str, Any]:
    try:
        return add_cash_flow(request.app.state.sqlite, **body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/settings")
async def post_setting(request: Request, body: SettingIn) -> dict[str, Any]:
    allowed = {"total_assets"}
    if body.key not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的设置项: {body.key}")
    set_setting(request.app.state.sqlite, body.key, body.value)
    return {"key": body.key, "value": body.value}
