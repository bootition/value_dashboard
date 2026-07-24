"""自选列表 API (PRD §13)

支持: 分组/排序/自定义列/手动保留/移除/来源记录
不支持: 备注/目标价/预警/自动提醒 (PRD §13 WL3)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddStockRequest(BaseModel):
    stock_code: str
    group_name: str = "default"
    source_rule_id: int | None = None
    source_result_id: str | None = None


class RemoveRequest(BaseModel):
    stock_code: str
    group_name: str | None = None  # None=从所有分组移除


class MoveGroupRequest(BaseModel):
    stock_code: str
    from_group: str
    to_group: str


@router.get("/list")
async def list_watchlist(group: str | None = None) -> dict:
    """列出自选股票 (PRD §13: 分组/排序/自定义列/来源记录)"""
    from app.core.storage.sqlite_store import SQLiteStore
    from app.core.storage.duckdb_store import DuckDBStore

    sqlite = SQLiteStore()
    duck = DuckDBStore()

    # 查询自选列表
    if group:
        rows = sqlite.query(
            "SELECT stock_code, group_name, source_rule_id, source_result_id, added_at "
            "FROM watchlist WHERE group_name = ? ORDER BY added_at DESC",
            [group],
        )
    else:
        rows = sqlite.query(
            "SELECT stock_code, group_name, source_rule_id, source_result_id, added_at "
            "FROM watchlist ORDER BY group_name, added_at DESC"
        )

    if not rows:
        return {"items": [], "count": 0, "groups": []}

    # 关联股票名称和最新指标
    stock_codes = [r["stock_code"] for r in rows]
    placeholders = ", ".join(["?"] * len(stock_codes))

    try:
        stock_info = duck.read_query(
            f"""SELECT m.stock_code, m.name, m.exchange, m.sw_level1,
                       s.latest_close, s.pe_ttm, s.pb_mrq, s.roe,
                       s.gross_margin, s.debt_ratio, s.revenue_yoy
                FROM stock_meta m
                LEFT JOIN LATERAL (
                    SELECT * FROM indicator_snapshot s2
                    WHERE s2.stock_code = m.stock_code
                    ORDER BY s2.report_date DESC LIMIT 1
                ) s ON true
                WHERE m.stock_code IN ({placeholders})""",
            stock_codes,
        )
        info_map = {r["stock_code"]: r for r in stock_info}
    except Exception:
        info_map = {}

    # 合并数据
    items = []
    for row in rows:
        code = row["stock_code"]
        info = info_map.get(code, {})
        items.append({
            "stock_code": code,
            "name": info.get("name", ""),
            "exchange": info.get("exchange", ""),
            "sw_level1": info.get("sw_level1"),
            "group_name": row["group_name"],
            "source_rule_id": row.get("source_rule_id"),
            "source_result_id": row.get("source_result_id"),
            "added_at": row.get("added_at"),
            "latest_close": info.get("latest_close"),
            "pe_ttm": info.get("pe_ttm"),
            "pb_mrq": info.get("pb_mrq"),
            "roe": info.get("roe"),
            "gross_margin": info.get("gross_margin"),
            "debt_ratio": info.get("debt_ratio"),
            "revenue_yoy": info.get("revenue_yoy"),
        })

    # 获取分组列表
    groups = sqlite.query(
        "SELECT DISTINCT group_name, COUNT(*) as cnt "
        "FROM watchlist GROUP BY group_name ORDER BY group_name"
    )

    return {"items": items, "count": len(items), "groups": groups}


@router.post("/add")
async def add_to_watchlist(req: AddStockRequest) -> dict:
    """添加股票到自选 (PRD §13: 手动保留)"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO watchlist
               (stock_code, group_name, source_rule_id, source_result_id)
               VALUES (?, ?, ?, ?)""",
            [req.stock_code, req.group_name, req.source_rule_id, req.source_result_id],
        )
    return {"status": "ok", "stock_code": req.stock_code, "group": req.group_name}


@router.delete("/remove")
async def remove_from_watchlist(req: RemoveRequest) -> dict:
    """从自选移除 (PRD §13: 手动移除)"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    if req.group_name:
        sqlite.execute(
            "DELETE FROM watchlist WHERE stock_code = ? AND group_name = ?",
            [req.stock_code, req.group_name],
        )
    else:
        sqlite.execute(
            "DELETE FROM watchlist WHERE stock_code = ?",
            [req.stock_code],
        )
    return {"status": "ok", "removed": req.stock_code}


@router.post("/move")
async def move_group(req: MoveGroupRequest) -> dict:
    """移动到其他分组"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    sqlite.execute(
        "UPDATE watchlist SET group_name = ? WHERE stock_code = ? AND group_name = ?",
        [req.to_group, req.stock_code, req.from_group],
    )
    return {"status": "ok", "stock_code": req.stock_code,
            "from": req.from_group, "to": req.to_group}


@router.get("/groups")
async def list_groups() -> dict:
    """列出所有分组"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    groups = sqlite.query(
        "SELECT group_name, COUNT(*) as cnt "
        "FROM watchlist GROUP BY group_name ORDER BY group_name"
    )
    return {"groups": groups, "count": len(groups)}
