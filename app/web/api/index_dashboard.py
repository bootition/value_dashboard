"""指数看板 API（2026-09-05：多指数 ERP + 估值分位，只读）

数据全部来自 index_dashboard 计算域（index_valuation + treasury_yield_curve），
不写库、不抓取；前端「指数」页卡片墙/详情/对比表的数据契约。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.etf_fundamentals import fundamental_detail as etf_fundamental_detail
from app.core.index_dashboard import (
    BROAD_INDEX_NAMES,
    SW_INDUSTRY_NAMES,
    erp_compare,
    erp_detail,
    index_catalog,
    index_detail,
    valuation_detail,
)
from app.core.storage.duckdb_store import DuckDBReadLockedError
from app.web.api._ttl_cache import TTLCache

router = APIRouter(prefix="/api/index", tags=["index-dashboard"])

_CODES = {item["code"] for item in index_catalog()}

# 指数估值按日更新：概览 30s、单指数详情 60s 的进程内 TTL 足够吸收并发
# 尖峰，且不会跨天展示旧值；key 绑定 DuckDB 路径隔离不同 app/测试夹具。
_compare_cache = TTLCache(30.0)
_detail_cache = TTLCache(60.0)
# 指数成分股基本面（业绩/市值合计）只读且全A约 2s，缓存 5 分钟。
_fundamental_cache = TTLCache(300.0)


def _db_key(request: Request) -> str:
    return str(request.app.state.duck.db_path)


def _uncacheable_error_snapshot(result: dict) -> bool:
    """预取整体失败时返回的全量 status=error 快照不应进入正常 TTL。"""
    items = result.get("items")
    return isinstance(items, list) and len(items) > 0 and all(
        item.get("status") == "error" for item in items
    )


def _cached_compare(request: Request, key: tuple[str, str]) -> dict:
    try:
        # reject：全 error 快照不写缓存；有预热旧值时返回旧值（更新窗口内
        # 卡片墙保持可用），无旧值才按原契约返回逐项 error（200）。
        # 2026-09-17 修复：此前是"写入后再 invalidate(keep_stale=False)"，
        # 既覆盖又删除预热快照，导致更新窗口内卡片墙退化为整页错误。
        return _compare_cache.get_or_compute(
            key,
            lambda: erp_compare(request.app.state.duck),
            stale_on=(DuckDBReadLockedError,),
            reject=_uncacheable_error_snapshot,
        )
    except DuckDBReadLockedError as error:
        raise HTTPException(status_code=503, detail="数据正在自动更新中，请稍后重试") from error


def _cached_detail(key: tuple[str, str], factory) -> dict:
    try:
        return _detail_cache.get_or_compute(
            key, factory, stale_on=(DuckDBReadLockedError,),
        )
    except DuckDBReadLockedError as error:
        raise HTTPException(status_code=503, detail="数据正在自动更新中，请稍后重试") from error


def warm_index_read_cache(duck: object) -> None:
    """自动更新前预热指数只读缓存；更新期间旧值仍可展示。"""
    from contextlib import suppress

    with suppress(Exception):
        _cached_compare(
            _FakeRequest(duck), ("overview", str(duck.db_path)),
        )
    for code in _CODES:
        try:
            _cached_detail(
                ("detail", str(duck.db_path), code),
                lambda code=code: index_detail(duck, code),
            )
        except Exception:
            continue


class _FakeRequest:
    def __init__(self, duck: object) -> None:
        from types import SimpleNamespace

        self.app = SimpleNamespace(state=SimpleNamespace(duck=duck))


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
    return _cached_compare(request, ("overview", _db_key(request)))


@router.get("/erp-compare")
async def get_erp_compare(request: Request) -> dict:
    """全指数 ERP 对比表（与 /overview 同源，独立端点便于语义区分）。"""
    return _cached_compare(request, ("overview", _db_key(request)))


@router.get("/{code}/detail")
async def get_detail(request: Request, code: str) -> dict:
    """指数详情页一次请求同时返回 ERP 与 PE/PB 详情（共享取数与计算）。"""
    resolved = _ensure_code(code)
    return _cached_detail(
        ("detail", _db_key(request), resolved),
        lambda: index_detail(request.app.state.duck, resolved),
    )


@router.get("/{code}/erp")
async def get_erp(request: Request, code: str) -> dict:
    """单指数 ERP 详情：序列 + 分位带 + 当前值/分位。"""
    resolved = _ensure_code(code)
    return _cached_detail(
        ("erp", _db_key(request), resolved),
        lambda: erp_detail(request.app.state.duck, resolved),
    )


@router.get("/{code}/valuation")
async def get_valuation(request: Request, code: str) -> dict:
    """单指数 PE/PB 详情：序列 + 分位带（ETF 分位信号图复用）。"""
    resolved = _ensure_code(code)
    return _cached_detail(
        ("valuation", _db_key(request), resolved),
        lambda: valuation_detail(request.app.state.duck, resolved),
    )


@router.get("/{code}/fundamentals")
async def get_fundamentals(request: Request, code: str) -> dict:
    """单指数成分股基本面：业绩/市值/利润增速（2026-09-10 用户要求——
    每个指数详情页都像全A一样建立"市值与利润的关系"）。

    成分口径与 ETF 基本面一致：申万一级行业按 sw_level1 精确匹配，
    宽基按总市值排名近似，无法映射时全A近似（响应内 disclaimer 如实披露）。
    """
    resolved = _ensure_code(code)
    name = BROAD_INDEX_NAMES.get(resolved) or SW_INDUSTRY_NAMES.get(resolved, resolved)
    meta = {
        "etf_code": resolved,
        # 全A伪指数没有跟踪指数；其余指数以自身代码驱动成分解析
        "track_index_code": None if resolved == "ALL_A" else resolved,
        "name": name,
    }
    try:
        return _fundamental_cache.get_or_compute(
            ("fundamentals", _db_key(request), resolved),
            lambda: etf_fundamental_detail(request.app.state.duck, meta),
            stale_on=(DuckDBReadLockedError,),
        )
    except DuckDBReadLockedError as error:
        raise HTTPException(status_code=503, detail="数据库正在更新，请稍后刷新") from error
