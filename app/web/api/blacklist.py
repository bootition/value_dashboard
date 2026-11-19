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


def require_visible_stock(request: Request, stock_code: str) -> str:
    """FastAPI 依赖：黑名单股票直接 404，绝不返回任何详情数据。

    只做**判黑名单**，不校验代码格式/是否存在（那是各端点原有的 400/404 语义）。
    """
    sqlite = getattr(request.app.state, "sqlite", None)
    if blacklist_core.is_blacklisted(sqlite, stock_code):
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
    try:
        meta_rows = request.app.state.duck.read_query(
            f"""SELECT stock_code, name, exchange, csrc_l1
                FROM stock_meta
                WHERE stock_code IN ({placeholders})""",
            codes,
        )
    except DuckDBReadLockedError as error:
        raise HTTPException(
            status_code=503, detail="数据正在自动更新中，请稍后刷新"
        ) from error
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
    return {"items": items, "count": len(items)}


@router.get("/check/{stock_code}")
def check_blacklist(stock_code: str, request: Request) -> dict:
    """详情页进入时先问一句「我是不是被屏蔽了」。

    单独一个轻量端点（SQLite 主键查）比让前端从 4 个并发请求的 404 里
    反推更清楚，也避免进入屏蔽态时先打一堆必然失败的请求。
    """
    return {
        "stock_code": stock_code.strip(),
        "blacklisted": blacklist_core.is_blacklisted(
            request.app.state.sqlite, stock_code
        ),
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
