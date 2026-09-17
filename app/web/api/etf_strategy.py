"""ETF 轮动工作台 API（2026-09-05）

- GET 只读：持仓/网格/信号汇总与单 ETF 详情（不写库，不落卖出计划）
- POST 用户录入：ETF 元数据、交易流水、资金流水、设置（总资产等）
数据契约与 ETF 工作台前端一一对应；估值分位复用 /api/index 计算域。
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.etf_fundamentals import fundamental_detail as etf_fundamental_detail
from app.core.etf_reset import bootstrap_portfolio, fresh_start, reset_portfolio
from app.core.etf_strategy import (
    add_cash_flow,
    add_etf_trade,
    get_setting,
    grid_state,
    grid_states_batch,
    load_etf_meta,
    set_setting,
    signal_zone,
    upsert_etf_meta,
)
from app.core.index_dashboard import index_summaries_batch, valuation_detail
from app.core.storage.duckdb_store import DuckDBReadLockedError
from app.web.api._ttl_cache import TTLCache

router = APIRouter(prefix="/api/etf", tags=["etf-strategy"])

# ETF 概览拆成两层（2026-09-17 线上修复）：
# - _market_cache 只缓存 DuckDB 派生的行情/估值/分位快照（外部更新进程持
#   文件锁时可回退旧值，不会让整页 503）；
# - 预算/持仓/流水/设置等 SQLite 派生数据不缓存、每次实时读，保证写完立即
#   可见——此前预算 POST 会整体失效概览缓存，更新窗口内重算必然失败，导致
#   "保存预算 → 整个 ETF 页报错"。
_market_cache = TTLCache(10.0)
# 基本面聚合只读且全A计算约 2s，缓存 5 分钟吸收重复打开详情。
_fundamental_cache = TTLCache(300.0)
# 单 ETF 历史估值序列（DuckDB 派生，绘图用）；明细/流水实时读 SQLite。
_valuation_history_cache = TTLCache(60.0)


def _market_context_key(request: Request) -> tuple[str, str]:
    return (
        str(request.app.state.duck.db_path),
        str(request.app.state.sqlite.db_path),
    )


class MetaIn(BaseModel):
    etf_code: str
    name: str
    category: str = "industry"
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


class BootstrapPositionIn(BaseModel):
    etf_code: str
    name: str
    category: str = "industry"
    track_index_code: str | None = None
    track_index_name: str | None = None
    primary_metric: str = "pe"
    industry_group: str | None = None
    budget: float = 0.0
    step_pct: float = 5.0
    shares: float = 0.0
    price: float | None = None
    trade_date: str | None = None
    fee: float = 0.0


class BootstrapIn(BaseModel):
    total_assets: float | None = None
    positions: list[BootstrapPositionIn]


class ResetIn(BaseModel):
    purge_meta: bool = True


def _track_metric(summary: dict[str, Any], primary_metric: str) -> float | None:
    if primary_metric == "pb":
        return summary.get("pb_percentile")
    return summary.get("pe_percentile")


_UNAVAILABLE_VALUATION: dict[str, Any] = {
    "status": "unavailable",
    "pe_percentile": None, "pb_percentile": None,
    "pe": None, "pb": None, "erp": None, "erp_percentile": None,
    "samples": 0, "latest_date": None,
}

_PERCENTILE_UNSET = object()


def _effective_track_code(meta: dict[str, Any]) -> str | None:
    """ETF 估值所跟踪的指数代码。

    全A指数（ALL_A）没有官方跟踪指数（track_index_code 为空），但它本身
    就是全市场合成指数：回退到同代码的合成指数概览，使其像其他 ETF 一样
    在卡片与详情统计中展示 PE/PB/ERP（2026-09-10 用户要求"全A当作正常
    ETF 研究，其他 ETF 有的市盈率也要算"）。
    """
    track = meta.get("track_index_code")
    if track:
        return str(track)
    if str(meta.get("etf_code")) == "ALL_A":
        return "ALL_A"
    return None


def _latest_closes(duck: object, codes: list[str]) -> dict[str, float]:
    """一次查询取全部 ETF 的最新收盘价，替代逐 ETF 开连接。"""
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    rows = duck.read_query(
        f"""SELECT etf_code, close_price
            FROM etf_daily
            WHERE etf_code IN ({placeholders}) AND close_price IS NOT NULL
            ORDER BY etf_code, trade_date DESC, source""",
        codes,
    )
    closes: dict[str, float] = {}
    for row in rows:
        closes.setdefault(str(row["etf_code"]), float(row["close_price"]))
    return closes


def _ths_pe_percentiles(duck: object, codes: list[str]) -> dict[str, float]:
    """一次查询取多只 ETF 的同花顺跟踪指数 PE 五年分位兜底值。"""
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    rows = duck.read_query(
        f"""SELECT etf_code, track_pe_ttm_five_year_percentile
            FROM etf_daily
            WHERE etf_code IN ({placeholders})
              AND track_pe_ttm_five_year_percentile IS NOT NULL
            ORDER BY etf_code, trade_date DESC""",
        codes,
    )
    percentiles: dict[str, float] = {}
    for row in rows:
        percentiles.setdefault(
            str(row["etf_code"]),
            float(row["track_pe_ttm_five_year_percentile"]),
        )
    return percentiles


def _percentile_for(
    request: Request,
    meta: dict[str, Any],
    valuation: dict[str, Any],
    fallback_percentile: float | None | object = _PERCENTILE_UNSET,
) -> tuple[float | None, str, str]:
    percentile = _track_metric(valuation, meta["primary_metric"])
    percentile_label = "PB分位" if meta["primary_metric"] == "pb" else "PE分位"
    percentile_source = "指数近10年"
    if percentile is None and meta["primary_metric"] == "pe":
        # 港股/中概等无指数估值历史：同花顺跟踪指数 PE-TTM 五年分位兜底。
        # 批量调用方已传入一次查询结果；单只调用方保持旧的一次查询。
        resolved_fallback = fallback_percentile
        if resolved_fallback is _PERCENTILE_UNSET:
            track_rows = request.app.state.duck.read_query(
                """SELECT track_pe_ttm_five_year_percentile FROM etf_daily
                   WHERE etf_code = ? AND track_pe_ttm_five_year_percentile IS NOT NULL
                   ORDER BY trade_date DESC LIMIT 1""",
                [meta["etf_code"]],
            )
            resolved_fallback = (
                float(track_rows[0]["track_pe_ttm_five_year_percentile"])
                if track_rows else None
            )
        if resolved_fallback is not None:
            percentile = resolved_fallback
            percentile_label = "PE分位(同花顺5年)"
            percentile_source = "跟踪指数5年"
    return percentile, percentile_label, percentile_source


def _empty_market_context(meta: dict[str, Any]) -> dict[str, Any]:
    """DuckDB 市场快照缺失时的占位；SQLite 侧网格/预算字段照常展示。"""
    return {
        "current_price": None,
        "valuation": _UNAVAILABLE_VALUATION,
        "percentile": None,
        "percentile_label": "PB分位" if meta["primary_metric"] == "pb" else "PE分位",
        "percentile_source": "指数近10年",
    }


def _compute_market_context(request: Request) -> dict[str, dict[str, Any]]:
    """一次算出全部 ETF 的 DuckDB 市场快照（最新价/估值/主指标分位）。"""
    metas = load_etf_meta(request.app.state.sqlite)
    effective_tracks = {
        str(meta["etf_code"]): _effective_track_code(meta) for meta in metas
    }
    track_codes = {code for code in effective_tracks.values() if code}
    summaries = index_summaries_batch(request.app.state.duck, track_codes)
    prices = _latest_closes(
        request.app.state.duck,
        [str(meta["etf_code"]) for meta in metas],
    )
    valuations: dict[str, dict[str, Any]] = {
        etf_code: (
            summaries.get(track) if track else _UNAVAILABLE_VALUATION
        )
        for etf_code, track in effective_tracks.items()
    }
    fallback_codes = [
        code for code, meta in (
            (str(meta["etf_code"]), meta) for meta in metas
        )
        if meta["primary_metric"] == "pe"
        and _track_metric(valuations[code], "pe") is None
    ]
    ths_percentiles = _ths_pe_percentiles(request.app.state.duck, fallback_codes)
    percentiles: dict[str, tuple[float | None, str, str]] = {
        code: _percentile_for(
            request, meta, valuations[code],
            fallback_percentile=ths_percentiles.get(code),
        )
        for code, meta in (
            (str(meta["etf_code"]), meta) for meta in metas
        )
    }
    return {
        code: {
            "current_price": prices.get(code),
            # 统一走 valuations（含 ALL_A 有效跟踪指数回退），
            # 不再按原始 track_index_code 单独取值（2026-09-10 遗漏点）。
            "valuation": valuations[code],
            "percentile": percentiles[code][0],
            "percentile_label": percentiles[code][1],
            "percentile_source": percentiles[code][2],
        }
        for code in effective_tracks
    }


def _market_context(request: Request) -> dict[str, dict[str, Any]]:
    """DuckDB 市场快照（10s TTL）。外部写进程持锁时回退旧快照而不是报错。"""
    return _market_cache.get_or_compute(
        _market_context_key(request),
        lambda: _compute_market_context(request),
        stale_on=(DuckDBReadLockedError,),
    )


def _item_from_context(
    request: Request,
    meta: dict[str, Any],
    context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """SQLite 网格状态 + DuckDB 市场快照合成单只 ETF 概览项。"""
    code = str(meta["etf_code"])
    resolved = context.get(code) or _empty_market_context(meta)
    state = grid_state(
        request.app.state.sqlite,
        etf_code=code,
        current_price=resolved["current_price"],
        signal=signal_zone(resolved["percentile"]),
        persist_sell_plan=False,
    )
    return {**state, **resolved}


def _compute_overview(request: Request) -> dict[str, Any]:
    metas = load_etf_meta(request.app.state.sqlite)
    context = _market_context(request)
    resolved = {
        str(meta["etf_code"]): (context.get(str(meta["etf_code"])) or _empty_market_context(meta))
        for meta in metas
    }
    states = grid_states_batch(
        request.app.state.sqlite,
        metas=metas,
        prices={code: item["current_price"] for code, item in resolved.items()},
        signals={
            code: signal_zone(item["percentile"]) for code, item in resolved.items()
        },
        persist_sell_plan=False,
    )
    items = [
        {**states[code], **resolved[code]}
        for code in resolved
    ]
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


@router.get("/overview")
async def get_overview(request: Request) -> dict[str, Any]:
    """ETF 工作台汇总：市场快照 10s TTL，预算/持仓等 SQLite 数据实时读。"""
    try:
        return _compute_overview(request)
    except DuckDBReadLockedError as error:
        raise HTTPException(status_code=503, detail="数据正在自动更新中，请稍后重试") from error


@router.get("/reset-preview")
async def get_reset_preview(request: Request) -> dict[str, Any]:
    """重新开始预览：将删除哪些 ETF 操作数据、重建默认观察池。"""
    return fresh_start(request.app.state.sqlite, dry_run=True)


@router.post("/reset")
async def post_reset(request: Request, body: ResetIn) -> dict[str, Any]:
    """清空 ETF 策略数据并回到新用户状态（行情与指数数据保留）。"""
    try:
        report = (
            fresh_start(request.app.state.sqlite, dry_run=False)
            if body.purge_meta
            else reset_portfolio(request.app.state.sqlite, dry_run=False)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _market_cache.invalidate(_market_context_key(request))
    return report


@router.post("/bootstrap")
async def post_bootstrap(request: Request, body: BootstrapIn) -> dict[str, Any]:
    """新用户初始化：策略总资产 + 首批持仓/预算一次录入（原子）。"""
    try:
        report = bootstrap_portfolio(
            request.app.state.sqlite,
            total_assets=body.total_assets,
            positions=[position.model_dump() for position in body.positions],
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _market_cache.invalidate(_market_context_key(request))
    return report


@router.get("/{etf_code}/fundamentals")
async def get_fundamentals(request: Request, etf_code: str) -> dict[str, Any]:
    """成分股业绩合计、市值序列、全A利润增速与行业贡献。"""
    metas = [m for m in load_etf_meta(request.app.state.sqlite) if m["etf_code"] == etf_code]
    if not metas:
        raise HTTPException(status_code=404, detail=f"ETF 未配置: {etf_code}")
    try:
        return _fundamental_cache.get_or_compute(
            (str(request.app.state.duck.db_path), etf_code),
            lambda: etf_fundamental_detail(request.app.state.duck, metas[0]),
            stale_on=(DuckDBReadLockedError,),
        )
    except DuckDBReadLockedError as error:
        raise HTTPException(status_code=503, detail="数据正在自动更新中，请稍后重试") from error


def _compute_detail(request: Request, etf_code: str) -> dict[str, Any]:
    metas = [m for m in load_etf_meta(request.app.state.sqlite) if m["etf_code"] == etf_code]
    if not metas:
        raise HTTPException(status_code=404, detail=f"ETF 未配置: {etf_code}")
    meta = metas[0]
    # 市场快照走可回退缓存；明细/流水/预算实时读 SQLite（保存后立即可见）。
    item = _item_from_context(request, meta, _market_context(request))
    # 历史估值序列统一用有效跟踪指数：ALL_A 回退到全市场合成指数，
    # 既有 PE/PB/ERP 当前值，也有合成历史序列可画分位图（2026-09-10）。
    track_code = _effective_track_code(meta)
    valuation = None
    if track_code:
        valuation = _valuation_history_cache.get_or_compute(
            (str(request.app.state.duck.db_path), track_code),
            lambda track_code=track_code: valuation_detail(
                request.app.state.duck, track_code
            ),
            stale_on=(DuckDBReadLockedError,),
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


@router.get("/{etf_code}/detail")
async def get_detail(request: Request, etf_code: str) -> dict[str, Any]:
    try:
        return _compute_detail(request, etf_code)
    except DuckDBReadLockedError as error:
        raise HTTPException(status_code=503, detail="数据正在自动更新中，请稍后重试") from error


def _fake_request(duck: Any, sqlite: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(duck=duck, sqlite=sqlite)))


def warm_etf_read_cache(duck: Any, sqlite: Any) -> None:
    """在自动更新子进程启动前预热只读缓存，更新期间旧值仍可展示。"""
    from contextlib import suppress

    fake = _fake_request(duck, sqlite)
    with suppress(Exception):
        _market_cache.get_or_compute(
            _market_context_key(fake),
            lambda: _compute_market_context(fake),
            stale_on=(DuckDBReadLockedError,),
        )
    for meta in load_etf_meta(sqlite):
        track_code = _effective_track_code(meta)
        if track_code:
            with suppress(Exception):
                _valuation_history_cache.get_or_compute(
                    (str(duck.db_path), track_code),
                    lambda track_code=track_code: valuation_detail(duck, track_code),
                    stale_on=(DuckDBReadLockedError,),
                )
        with suppress(Exception):
            _fundamental_cache.get_or_compute(
                (str(duck.db_path), str(meta["etf_code"])),
                lambda meta=meta: etf_fundamental_detail(duck, meta),
                stale_on=(DuckDBReadLockedError,),
            )


@router.post("/meta")
async def post_meta(request: Request, body: MetaIn) -> dict[str, Any]:
    payload = body.model_dump()
    if "note" not in body.model_fields_set:
        # 预算弹窗不携带 note；缺省保留原备注，避免保存预算时误清空。
        rows = request.app.state.sqlite.query(
            "SELECT note FROM etf_meta WHERE etf_code = ?", [body.etf_code]
        )
        payload["note"] = rows[0]["note"] if rows else None
    try:
        result = upsert_etf_meta(request.app.state.sqlite, **payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # 跟踪指数/主指标可能变化：市场快照与基本面缓存都失效（保留旧值兜底）。
    _market_cache.invalidate(_market_context_key(request))
    _fundamental_cache.invalidate(
        (str(request.app.state.duck.db_path), body.etf_code),
    )
    return result


@router.post("/trades")
async def post_trade(request: Request, body: TradeIn) -> dict[str, Any]:
    try:
        result = add_etf_trade(request.app.state.sqlite, **body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # 持仓/预算等 SQLite 数据不缓存，概览与详情下次请求实时读取即可。
    return result


@router.post("/cash-flows")
async def post_cash_flow(request: Request, body: CashFlowIn) -> dict[str, Any]:
    try:
        result = add_cash_flow(request.app.state.sqlite, **body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return result


@router.post("/settings")
async def post_setting(request: Request, body: SettingIn) -> dict[str, Any]:
    allowed = {"total_assets"}
    if body.key not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的设置项: {body.key}")
    try:
        value = float(body.value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="设置值必须是数字") from error
    if not math.isfinite(value) or value < 0:
        raise HTTPException(status_code=400, detail="设置值必须是非负有限数") from None
    set_setting(request.app.state.sqlite, body.key, body.value)
    return {"key": body.key, "value": body.value}
