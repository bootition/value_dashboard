"""股票黑名单 API（用户态数据，存 SQLite）。

产品语义与「为什么只隐藏、不停止抓取」见 `app/core/blacklist.py` 模块 docstring。

本模块同时提供**唯一的读侧守卫** `require_visible_stock`：
`app/web/api/stock_detail.py` 把所有 `/{stock_code}/...` 端点挂在带该依赖的
第二个 router 上，一处声明即覆盖全部单股出口——避免"13 个端点逐个手写检查、
将来新增端点忘写"这类漏网（`AGENTS R1` 一个事实只能有一个实现）。

404 的 `detail` 是**结构体**而不是字符串：前端要能区分
「代码不存在」与「被黑名单屏蔽」，后者要渲染屏蔽态 + 移出按钮（自救入口）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import blacklist as blacklist_core
from app.core import floating_blacklist as floating
from app.core.storage import read_cache
from app.core.storage.duckdb_store import DuckDBReadLockedError

router = APIRouter(prefix="/api/blacklist", tags=["blacklist"])

#: 单股出口被屏蔽时的 404 载荷码（前端 `StockDetailPage` 据此渲染屏蔽态）
BLACKLISTED_DETAIL_CODE = "stock_blacklisted"


class AddRequest(BaseModel):
    stock_code: str
    #: 可选备注；留空也允许加入（产品要求"点一下就加入"）
    reason: str | None = None


class RemoveRequest(BaseModel):
    stock_code: str


class RuleUpsertRequest(BaseModel):
    """新建/更新一条浮动黑名单规则。conditions 与筛选规则同一套结构。"""

    name: str
    conditions: dict[str, Any]
    note: str | None = None
    #: 池参数，目前只支持 {"max_listing_years": N}（次新股口径）
    pool: dict[str, Any] | None = None
    enabled: bool = True


class RuleToggleRequest(BaseModel):
    enabled: bool


def require_visible_stock(request: Request, stock_code: str) -> str:
    """FastAPI 依赖：黑名单股票直接 404，绝不返回任何详情数据。

    只做**判黑名单**，不校验代码格式/是否存在（那是各端点原有的 400/404 语义）。
    """
    sqlite = getattr(request.app.state, "sqlite", None)
    # 浮动拉黑的股票同样要从单股出口消失——否则"黑名单"只挡了一半。
    if blacklist_core.is_blacklisted(sqlite, stock_code) or floating.membership_for(
        sqlite, stock_code
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": BLACKLISTED_DETAIL_CODE,
                "message": "该股票已加入黑名单，内容不再显示",
            },
        )
    return stock_code


@router.get("/list")
def list_blacklist(request: Request) -> dict:
    """黑名单管理页：列出被屏蔽的股票（含名称，便于辨认）。

    这是黑名单股票**唯一**允许出现在研究面之外的出口——没有它，误加了
    黑名单就再也找不到移出的入口（详情页已被屏蔽、搜索也搜不到）。
    """
    sqlite = request.app.state.sqlite
    entries = blacklist_core.list_entries(sqlite)
    if not entries:
        return {"items": [], "count": 0}

    codes = [entry["stock_code"] for entry in entries]
    placeholders = ", ".join("?" for _ in codes)
    duck = request.app.state.duck
    served_from = read_cache.SERVED_FROM_LIVE
    as_of: str | None = None
    try:
        meta_rows = duck.read_query(
            f"""SELECT stock_code, name, exchange, csrc_l1
                FROM stock_meta
                WHERE stock_code IN ({placeholders})""",
            codes,
        )
    except DuckDBReadLockedError:
        # 2026-11-20：写进程持锁时不再 503 —— 黑名单里那几行**代码本身**在 SQLite
        # 里，只有"取名字"需要 DuckDB。名字取不到就退回 `stock_meta` 全表快照；
        # 连快照都没有才如实报错（否则用户会以为黑名单被清空了，那是更糟的假象）。
        snapshot_meta = read_cache.stock_meta_by_code(duck, codes)
        if snapshot_meta is None:
            raise
        meta_rows = list(snapshot_meta.values())
        snapshot = read_cache.stock_meta_snapshot(duck, reload=False)
        served_from = read_cache.SERVED_FROM_CACHE
        as_of = snapshot.as_of if snapshot is not None else None
    meta = {row["stock_code"]: row for row in meta_rows}

    items: list[dict[str, Any]] = []
    for entry in entries:
        code = entry["stock_code"]
        info = meta.get(code, {})
        items.append({
            "stock_code": code,
            "name": info.get("name") or "",
            "exchange": info.get("exchange") or "",
            "csrc_l1": info.get("csrc_l1"),
            "reason": entry.get("reason"),
            "created_at": entry.get("created_at"),
        })
    return {
        "items": items,
        "count": len(items),
        "served_from": served_from,
        "as_of": as_of,
        "stale": served_from == read_cache.SERVED_FROM_CACHE,
    }


@router.get("/check/{stock_code}")
def check_blacklist(stock_code: str, request: Request) -> dict:
    """详情页进入时先问一句「我是不是被屏蔽了」。

    单独一个轻量端点（SQLite 主键查）比让前端从 4 个并发请求的 404 里
    反推更清楚，也避免进入屏蔽态时先打一堆必然失败的请求。

    返回的 `scope` 决定前端渲染哪种屏蔽态：
    - `permanent`：彻底拉黑 → 给「移出黑名单」按钮（可自救）；
    - `floating`：浮动拉黑 → **不给任何手动移出入口**，只告诉他命中了哪几条规则
      并指向规则库（用户裁定：想让它出来就去改规则）。
    """
    sqlite = request.app.state.sqlite
    code = stock_code.strip()
    permanent = blacklist_core.is_blacklisted(sqlite, code)
    hits = [] if permanent else floating.membership_for(sqlite, code)
    return {
        "stock_code": code,
        "blacklisted": bool(permanent or hits),
        "scope": "permanent" if permanent else ("floating" if hits else None),
        "rules": [{"rule_id": hit["rule_id"], "name": hit["name"]} for hit in hits],
    }


@router.post("/add")
def add_blacklist(req: AddRequest, request: Request) -> dict:
    """加入黑名单（幂等）。

    只校验代码格式与"该代码在股票池里"，不要求来源可追溯——产品要求
    「点一下就加入」。已在黑名单里再点一次也不报错，只刷新加入时间。
    """
    sqlite = request.app.state.sqlite
    try:
        code = blacklist_core.normalize_stock_code(req.stock_code)
    except blacklist_core.InvalidStockCodeError as error:
        raise HTTPException(
            status_code=400, detail="invalid stock code: 6 digits required"
        ) from error

    known = request.app.state.duck.read_query(
        "SELECT 1 AS found FROM stock_meta WHERE stock_code = ? LIMIT 1",
        [code],
    )
    if not known:
        raise HTTPException(status_code=400, detail="stock code not found in universe")

    blacklist_core.add_stock(sqlite, code, req.reason)
    return {"status": "ok", "stock_code": code}


@router.delete("/remove")
def remove_blacklist(req: RemoveRequest, request: Request) -> dict:
    """移出黑名单。无匹配行时 404，避免前端误以为移出成功。"""
    sqlite = request.app.state.sqlite
    removed = blacklist_core.remove_stock(sqlite, req.stock_code)
    if removed == 0:
        raise HTTPException(status_code=404, detail="blacklist entry not found")
    return {"status": "ok", "stock_code": req.stock_code.strip(), "removed": removed}


# ─── 浮动黑名单：规则库 ────────────────────────────────────────────
#
# 用户裁定（2026-11-22）：浮动拉黑**完全由规则决定**，前端不提供任何手工
# 「把某只股票浮动拉黑」的入口 —— 要它进浮动名单，就去改规则。
# 手工入口只保留彻底拉黑（上面的 /add）。


@router.get("/rules")
def list_rules(request: Request) -> dict:
    """规则库 + 每条规则的当前名册规模。"""
    sqlite = request.app.state.sqlite
    inserted = floating.ensure_preset_rules(sqlite)
    rules = floating.list_rules(sqlite)
    counts = {
        int(row["rule_id"]): int(row["n"])
        for row in sqlite.query(
            f"SELECT rule_id, COUNT(*) AS n FROM {floating.MEMBERSHIP_TABLE} GROUP BY rule_id"
        )
    }
    for rule in rules:
        rule["matched"] = counts.get(int(rule["id"]), 0)
    return {"rules": rules, "count": len(rules), "preset_seeded": inserted}


@router.post("/rules")
def create_rule(req: RuleUpsertRequest, request: Request) -> dict:
    sqlite = request.app.state.sqlite
    try:
        rule_id = floating.add_rule(
            sqlite,
            name=req.name,
            conditions=req.conditions,
            note=req.note,
            pool=req.pool,
            enabled=req.enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "ok", "rule_id": rule_id}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, req: RuleUpsertRequest, request: Request) -> dict:
    sqlite = request.app.state.sqlite
    try:
        ok = floating.update_rule(
            sqlite,
            rule_id,
            name=req.name,
            conditions=req.conditions,
            note=req.note,
            pool=req.pool,
            enabled=req.enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not ok:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"status": "ok", "rule_id": rule_id}


@router.post("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, req: RuleToggleRequest, request: Request) -> dict:
    """开关一条规则。关闭后**下一次重算**才会把它的成员移出（不即时改库），
    这样「关掉它」和「它自己不再命中」走同一条路径，行为一致。"""
    sqlite = request.app.state.sqlite
    if not floating.update_rule(sqlite, rule_id, enabled=req.enabled):
        raise HTTPException(status_code=404, detail="rule not found")
    return {"status": "ok", "rule_id": rule_id, "enabled": req.enabled}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, request: Request) -> dict:
    sqlite = request.app.state.sqlite
    if floating.delete_rule(sqlite, rule_id) == 0:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"status": "ok", "rule_id": rule_id}


@router.post("/rules/{rule_id}/preview")
def preview_rule(rule_id: int, request: Request) -> dict:
    """**试算**：这条规则现在会命中多少只、前 30 只是谁。

    这是「开规则前先看一眼」的唯一入口 —— 规则比手工黑名单危险得多，
    一条写错的条件可能一次拉黑半个市场。试算只读，不写名册。
    """
    sqlite = request.app.state.sqlite
    rules = [rule for rule in floating.list_rules(sqlite) if int(rule["id"]) == rule_id]
    if not rules:
        raise HTTPException(status_code=404, detail="rule not found")
    try:
        codes = floating.evaluate_rule(
            request.app.state.duck, sqlite, rules[0]
        )
    except floating.RuleTooBroadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - 规则字段非法等，如实回报
        raise HTTPException(status_code=400, detail=str(error)) from error

    sample = sorted(codes)[:30]
    names = {}
    if sample:
        placeholders = ", ".join("?" for _ in sample)
        for row in request.app.state.duck.read_query(
            f"SELECT stock_code, name FROM stock_meta WHERE stock_code IN ({placeholders})",
            sample,
        ):
            names[row["stock_code"]] = row["name"]
    return {
        "rule_id": rule_id,
        "matched": len(codes),
        "sample": [{"stock_code": code, "name": names.get(code, "")} for code in sample],
    }


# ─── 浮动黑名单：名册 / 进出事件 / 手动重算 ────────────────────────


@router.get("/floating")
def list_floating(request: Request, limit: int = 300) -> dict:
    """当前浮动名册（按股票聚合，带「被哪几条规则拉黑」）。"""
    sqlite = request.app.state.sqlite
    codes = floating.floating_codes(sqlite)
    total = len(codes)
    shown = codes[: max(1, min(int(limit), 2000))]
    names: dict[str, str] = {}
    if shown:
        placeholders = ", ".join("?" for _ in shown)
        for row in request.app.state.duck.read_query(
            f"SELECT stock_code, name FROM stock_meta WHERE stock_code IN ({placeholders})",
            shown,
        ):
            names[row["stock_code"]] = row["name"]
    items = []
    for code in shown:
        hits = floating.membership_for(sqlite, code)
        items.append({
            "stock_code": code,
            "name": names.get(code, ""),
            "rules": [{"rule_id": hit["rule_id"], "name": hit["name"]} for hit in hits],
        })
    return {
        "items": items,
        "count": total,
        "returned": len(items),
        "last_run": floating.latest_run(sqlite),
    }


@router.get("/floating/events")
def list_floating_events(request: Request, limit: int = 100) -> dict:
    """有进有出：最近的新增/移出事件（倒序）。"""
    sqlite = request.app.state.sqlite
    rows = floating.recent_events(sqlite, limit=max(1, min(int(limit), 500)))
    codes = [row["stock_code"] for row in rows if row.get("stock_code")]
    names: dict[str, str] = {}
    if codes:
        unique = sorted(set(codes))
        placeholders = ", ".join("?" for _ in unique)
        for row in request.app.state.duck.read_query(
            f"SELECT stock_code, name FROM stock_meta WHERE stock_code IN ({placeholders})",
            unique,
        ):
            names[row["stock_code"]] = row["name"]
    for row in rows:
        row["stock_name"] = names.get(row["stock_code"], "")
    return {"events": rows, "count": len(rows)}


@router.post("/floating/refresh")
def refresh_floating(request: Request) -> dict:
    """手动重算一次（平时由自动更新最后一步跑，这里给「刚改完规则想立刻看到效果」用）。"""
    try:
        return floating.refresh(duck=request.app.state.duck, sqlite=request.app.state.sqlite)
    except Exception as error:  # noqa: BLE001 - 如实回报，不吞
        raise HTTPException(status_code=503, detail=str(error)) from error
