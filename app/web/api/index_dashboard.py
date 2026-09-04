"""指数看板 API（2026-09-05：多指数 ERP + 估值分位，只读）

数据全部来自 index_dashboard 计算域（index_valuation + treasury_yield_curve），
不写库、不抓取；前端「指数」页卡片墙/详情/对比表的数据契约。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.index_dashboard import (
    erp_compare,
    erp_detail,
    index_catalog,
    valuation_detail,
)

router = APIRouter(prefix="/api/index", tags=["index-dashboard"])

_CODES = {item["code"] for item in index_catalog()}


def _ensure_code(code: str) -> str:
    code = code.strip().upper()
    if code not in _CODES:
        raise HTTPException(status_code=404, detail=f"未收录的指数代码: {code}")
    return code


@router.get("/catalog")
async def get_catalog() -> dict:
    """指数目录（宽基 12 + 申万一级行业 31）。"""
    return {"items": index_catalog()}


@router.get("/overview")
async def get_overview(request: Request) -> dict:
    """卡片墙：每个指数的当前 PE/PB/ERP 与 10 年分位。"""
    return erp_compare(request.app.state.duck)


@router.get("/erp-compare")
async def get_erp_compare(request: Request) -> dict:
    """全指数 ERP 对比表（与 /overview 同源，独立端点便于语义区分）。"""
    return erp_compare(request.app.state.duck)


@router.get("/{code}/erp")
async def get_erp(request: Request, code: str) -> dict:
    """单指数 ERP 详情：序列 + 分位带 + 当前值/分位。"""
    return erp_detail(request.app.state.duck, _ensure_code(code))


@router.get("/{code}/valuation")
async def get_valuation(request: Request, code: str) -> dict:
    """单指数 PE/PB 详情：序列 + 分位带（ETF 分位信号图复用）。"""
    return valuation_detail(request.app.state.duck, _ensure_code(code))
