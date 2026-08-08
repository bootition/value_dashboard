"""数据状态页 API (PRD §15)

只读展示：更新时间、覆盖状态、回填状态、重试/缺失摘要。
不提供写操作（PRD DS3）。

写竞争降级：自动更新持有 DuckDB 写锁期间，逐股提交间隙读查询仍会被
串行化拖慢（实测 summary 可达 60s+）。这里在写锁存在时直接返回最近一次
成功计算的结果缓存并标注 stale=true，避免界面被读阻塞；轮询不再重复触发
全量聚合。缓存 60s 有效，无锁时每 60s 重算一次。
"""

from __future__ import annotations

import time
import json
import threading

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/data-status", tags=["data-status"])

_SUMMARY_TTL_SECONDS = 60
_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
_SUMMARY_CACHE_LOCK = threading.Lock()


def _update_write_lock_active(duck) -> bool:
    """自动更新写锁存在即认为正在写入（保守：宁愿命中缓存也不要卡读）。"""
    try:
        from app.core.storage.update_lock import _owner_is_dead

        lock_path = duck.db_path.parent / ".value-dashboard.update.lock"
        return lock_path.exists() and not _owner_is_dead(lock_path)
    except OSError:
        return False


def _summary_cache_key(request: Request) -> str:
    app = getattr(request, "app", request)
    return f"{app.state.duck.db_path}|{app.state.sqlite.db_path}"


def _cached_summary(request: Request, *, allow_stale: bool = False) -> dict | None:
    with _SUMMARY_CACHE_LOCK:
        entry = _SUMMARY_CACHE.get(_summary_cache_key(request))
        if not entry:
            return None
        data = entry.get("data")
        if data is None:
            return None
        at = float(entry["at"])
        if not allow_stale and (time.monotonic() - at) > _SUMMARY_TTL_SECONDS:
            return None
        return dict(data) if isinstance(data, dict) else None


def _store_summary(request: Request, summary: dict) -> None:
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[_summary_cache_key(request)] = {
            "at": time.monotonic(),
            "data": dict(summary),
        }


@router.get("/summary")
def get_summary(request: Request) -> dict:
    """数据状态摘要 (PRD §15 DS2)"""
    # 自动更新写锁期间：直接返回最近一次成功缓存（stale=true）。
    # 避免每个轮询请求阻塞在 DuckDB 写锁上（曾实测 60s+）。
    startup_readiness = getattr(request.app.state, "startup_readiness", None)
    write_lock_active = _update_write_lock_active(request.app.state.duck)
    cached = _cached_summary(request, allow_stale=write_lock_active)
    if (
        cached is not None
        and cached.get("checking")
        and isinstance(startup_readiness, dict)
        and not startup_readiness.get("checking")
    ):
        cached = None
    if write_lock_active:
        if cached is not None:
            cached["stale"] = True
            cached["stale_reason"] = "auto_update_active"
            return cached
        summary = _build_summary_fresh(request)
        _store_summary(request, summary)
        return summary
    if cached is not None:
        return cached
    summary = _build_summary_fresh(request)
    _store_summary(request, summary)
    return summary


def _build_summary_fresh(request: Request) -> dict:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite

    from app.core.data_quality import build_data_quality_status

    startup_readiness = getattr(request.app.state, "startup_readiness", None)
    if isinstance(startup_readiness, dict) and startup_readiness.get("checking"):
        return {
            "data_quality": {
                "minimum_data_readiness": dict(startup_readiness),
                "dates": {},
                "dividends": {},
                "lineage": {},
                "code_identity": {},
                "operations": {},
                "warning_codes": [],
            },
            "minimum_data_readiness": dict(startup_readiness),
            "checking": True,
        }

    try:
        summary: dict = {"data_quality": build_data_quality_status(duck, sqlite)}
    except Exception as error:
        raise HTTPException(status_code=503, detail="data quality status is unavailable") from error
    errors: list[str] = []
    # Status is a live read; startup state is only a historical initialization result.
    summary["minimum_data_readiness"] = summary["data_quality"]["minimum_data_readiness"]

    # 股票覆盖
    try:
        row = duck.read_query("SELECT COUNT(*) as cnt FROM stock_meta WHERE is_listed IS TRUE")
        summary["stock_count"] = row[0]["cnt"]
    except Exception:
        summary["stock_count"] = 0
        errors.append("stock_count")

    # 价格覆盖
    try:
        row = duck.read_query(
            """SELECT COUNT(DISTINCT price.stock_code) as cnt FROM price_daily_raw price
               JOIN stock_meta stock ON stock.stock_code = price.stock_code
               WHERE stock.is_listed IS TRUE"""
        )
        summary["price_raw_count"] = row[0]["cnt"]
    except Exception:
        summary["price_raw_count"] = 0
        errors.append("price_raw_count")

    try:
        row = duck.read_query(
            """SELECT COUNT(DISTINCT price.stock_code) as cnt FROM price_daily_qfq price
               JOIN stock_meta stock ON stock.stock_code = price.stock_code
               WHERE stock.is_listed IS TRUE"""
        )
        summary["price_qfq_count"] = row[0]["cnt"]
    except Exception:
        summary["price_qfq_count"] = 0
        errors.append("price_qfq_count")

    try:
        target_rows = sqlite.query(
            """SELECT MAX(trade_date) AS target_date FROM trading_dates
               WHERE trade_date <= CASE
                 WHEN time('now', 'localtime') < '15:30:00'
                 THEN date('now', 'localtime', '-1 day')
                 ELSE date('now', 'localtime')
               END"""
        )
        target_date = target_rows[0]["target_date"] if target_rows else None
        freshness = duck.read_query(
            """WITH raw AS (
                 SELECT stock_code, MAX(trade_date) AS latest FROM price_daily_raw GROUP BY stock_code
               ), qfq AS (
                 SELECT stock_code, MAX(trade_date) AS latest FROM price_daily_qfq GROUP BY stock_code
               )
               SELECT COUNT(*) AS active_count,
                      COUNT(*) FILTER (WHERE raw.latest >= ?) AS raw_current_count,
                      COUNT(*) FILTER (WHERE qfq.latest >= ?) AS qfq_current_count,
                      COUNT(*) FILTER (WHERE raw.latest >= ? AND qfq.latest >= ?) AS complete_count
               FROM stock_meta stock
               LEFT JOIN raw ON raw.stock_code = stock.stock_code
               LEFT JOIN qfq ON qfq.stock_code = stock.stock_code
               WHERE stock.is_listed IS TRUE
                 AND COALESCE(stock.is_suspended, FALSE) IS FALSE""",
            [target_date, target_date, target_date, target_date],
        )[0]
        summary["price_freshness"] = {"target_date": target_date, **freshness}
    except Exception:
        summary["price_freshness"] = None
        errors.append("price_freshness")

    # 价格回填状态 (PRD §6.1 D4: 上市以来全部可得数据)
    try:
        row = duck.read_query(
            "SELECT MIN(trade_date) as earliest, MAX(trade_date) as latest, "
            "COUNT(DISTINCT stock_code) as stocks, COUNT(*) as total_rows "
            "FROM price_daily_raw"
        )
        # 真实回填缺口: 对比每只股票的最早价格日 vs 上市日
        gap_row = duck.read_query(
            """
            SELECT
                COUNT(CASE WHEN p.earliest_price IS NULL THEN 1 END) as no_price,
                COUNT(CASE WHEN p.earliest_price IS NOT NULL
                             AND s.listing_date IS NOT NULL
                             AND p.earliest_price > s.listing_date + INTERVAL '30 days' THEN 1 END) as incomplete,
                COUNT(CASE WHEN p.earliest_price IS NOT NULL
                             AND s.listing_date IS NULL THEN 1 END) as unknown_listing_date,
                COUNT(CASE WHEN p.earliest_price IS NOT NULL
                             AND s.listing_date IS NOT NULL
                             AND p.earliest_price <= s.listing_date + INTERVAL '30 days' THEN 1 END) as complete
            FROM stock_meta s
            LEFT JOIN (
                SELECT stock_code, MIN(trade_date) as earliest_price
                FROM price_daily_raw GROUP BY stock_code
            ) p ON s.stock_code = p.stock_code
            WHERE s.is_listed IS TRUE
            """
        )
        summary["price_backfill"] = {
            "earliest_date": str(row[0]["earliest"]) if row[0]["earliest"] else None,
            "latest_date": str(row[0]["latest"]) if row[0]["latest"] else None,
            "stock_count": row[0]["stocks"],
            "total_rows": row[0]["total_rows"],
            "gap": {
                "no_price": gap_row[0]["no_price"],
                "incomplete": gap_row[0]["incomplete"],
                "unknown_listing_date": gap_row[0]["unknown_listing_date"],
                "complete": gap_row[0]["complete"],
            },
        }
    except Exception:
        summary["price_backfill"] = None
        errors.append("price_backfill")

    # 分红回填状态 (PRD §6.4: 分红/送股/转增/配股)
    try:
        div_row = duck.read_query(
            """
            SELECT
                COUNT(*) as total_rows,
                COUNT(DISTINCT stock_code) as stocks,
                MIN(ex_date) as earliest,
                MAX(ex_date) as latest,
                COUNT(stock_dividend) as has_stock_div,
                COUNT(transfer_share) as has_transfer,
                COUNT(rights_issue) as has_rights
            FROM dividends
            """
        )
        summary["dividends"] = {
            "total_rows": div_row[0]["total_rows"],
            "stocks": div_row[0]["stocks"],
            "earliest": str(div_row[0]["earliest"]) if div_row[0]["earliest"] else None,
            "latest": str(div_row[0]["latest"]) if div_row[0]["latest"] else None,
            "stock_dividend_filled": div_row[0]["has_stock_div"],
            "transfer_share_filled": div_row[0]["has_transfer"],
            "rights_issue_filled": div_row[0]["has_rights"],
        }
    except Exception:
        summary["dividends"] = None
        errors.append("dividends")

    # xdxr 状态
    try:
        xdxr_row = duck.read_query(
            "SELECT COUNT(*) as total_rows, COUNT(DISTINCT stock_code) as stocks, "
            "MIN(event_date) as earliest, MAX(event_date) as latest "
            "FROM xdxr"
        )
        summary["xdxr"] = {
            "total_rows": xdxr_row[0]["total_rows"],
            "stocks": xdxr_row[0]["stocks"],
            "earliest": str(xdxr_row[0]["earliest"]) if xdxr_row[0]["earliest"] else None,
            "latest": str(xdxr_row[0]["latest"]) if xdxr_row[0]["latest"] else None,
        }
    except Exception:
        summary["xdxr"] = None
        errors.append("xdxr")

    # 财务覆盖
    for table in ["balance_sheet", "income_statement", "cash_flow"]:
        try:
            row = duck.read_query(
                f"SELECT COUNT(DISTINCT stock_code) as cnt, "
                f"MIN(report_date) as earliest, MAX(report_date) as latest "
                f"FROM {table}"
            )
            summary[f"{table}_count"] = row[0]["cnt"]
            summary[f"{table}_range"] = {
                "earliest": str(row[0]["earliest"]) if row[0]["earliest"] else None,
                "latest": str(row[0]["latest"]) if row[0]["latest"] else None,
            }
        except Exception:
            summary[f"{table}_count"] = 0
            summary[f"{table}_range"] = None
            errors.append(table)

    # 指标快照覆盖
    try:
        row = duck.read_query(
            "SELECT COUNT(*) as cnt, MIN(report_date) as earliest, MAX(report_date) as latest "
            "FROM indicator_snapshot"
        )
        summary["indicator_snapshot_count"] = row[0]["cnt"]
        summary["indicator_snapshot_range"] = {
            "earliest": str(row[0]["earliest"]) if row[0]["earliest"] else None,
            "latest": str(row[0]["latest"]) if row[0]["latest"] else None,
        }
    except Exception:
        summary["indicator_snapshot_count"] = 0
        summary["indicator_snapshot_range"] = None
        errors.append("indicator_snapshot")

    # 最近更新时间
    try:
        row = sqlite.query(
            "SELECT finished_at, job_type, status FROM job_logs "
            "WHERE status='success' ORDER BY finished_at DESC LIMIT 5"
        )
        summary["recent_jobs"] = row
        summary["last_update"] = row[0]["finished_at"] if row else None
    except Exception:
        summary["recent_jobs"] = []
        summary["last_update"] = None
        errors.append("recent_jobs")

    # 重试/缺失摘要
    try:
        row = sqlite.query("SELECT COUNT(*) as cnt FROM retry_list")
        summary["retry_count"] = row[0]["cnt"]
    except Exception:
        summary["retry_count"] = 0
        errors.append("retry_count")

    try:
        row = sqlite.query(
            "SELECT COUNT(*) as cnt FROM missing_list WHERE resolved_at IS NULL"
        )
        summary["missing_count"] = row[0]["cnt"]
    except Exception:
        summary["missing_count"] = 0
        errors.append("missing_count")

    # PDF解析失败任务摘要
    try:
        row = sqlite.query(
            "SELECT COUNT(*) as cnt, "
            "COUNT(CASE WHEN status='pending' THEN 1 END) as pending "
            "FROM pdf_tasks"
        )
        summary["pdf_tasks"] = row[0] if row else {"cnt": 0, "pending": 0}
    except Exception:
        summary["pdf_tasks"] = {"cnt": 0, "pending": 0}
        errors.append("pdf_tasks")

    # 备份摘要
    try:
        row = sqlite.query(
            "SELECT COUNT(*) as cnt, "
            "MAX(created_at) as latest, "
            "SUM(CASE WHEN type='full' THEN 1 ELSE 0 END) as full_count "
            "FROM backup_registry"
        )
        summary["backup"] = row[0] if row else {"cnt": 0, "latest": None, "full_count": 0}
    except Exception:
        summary["backup"] = {"cnt": 0, "latest": None, "full_count": 0}
        errors.append("backup")

    # CSRC 行业覆盖
    try:
        row = duck.read_query(
            "SELECT COUNT(*) as cnt FROM stock_meta WHERE csrc_l1 IS NOT NULL"
        )
        summary["csrc_industry_count"] = row[0]["cnt"]
    except Exception:
        summary["csrc_industry_count"] = 0
        errors.append("csrc_industry_count")

    # PRD §6.4/§15: 各数据域最新日期（股本/上市名单/除权/CSRC 刷新）
    try:
        meta_row = duck.read_query(
            """SELECT
                   COUNT(*) AS total,
                   COUNT(total_shares) AS with_shares,
                   COUNT(circ_shares) AS with_circ_shares,
                   MAX(updated_at) AS latest_updated
               FROM stock_meta"""
        )
        summary["share_capital"] = {
            "latest_updated": str(meta_row[0]["latest_updated"]) if meta_row[0]["latest_updated"] else None,
            "with_shares": meta_row[0]["with_shares"],
            "with_circ_shares": meta_row[0]["with_circ_shares"],
        }
    except Exception:
        summary["share_capital"] = None
        errors.append("share_capital")

    try:
        refresh_rows = {
            row["key"]: row["value"]
            for row in sqlite.query(
                """SELECT key, value FROM data_refresh_state
                   WHERE key IN ('stock_list_last_refresh', 'listing_info_last_refresh',
                                  'csrc_industry_last_refresh', 'price_update_last_rate')"""
            )
        }
        summary["listing_info"] = {
            "stock_list_refreshed_at": refresh_rows.get("stock_list_last_refresh"),
            "listing_info_refreshed_at": refresh_rows.get("listing_info_last_refresh"),
        }
        summary["csrc_industry_refresh"] = {
            "last_refresh": refresh_rows.get("csrc_industry_last_refresh"),
        }
        rate_value = refresh_rows.get("price_update_last_rate")
        summary["price_update_rate"] = json.loads(rate_value) if rate_value else None
    except Exception:
        summary["listing_info"] = None
        summary["csrc_industry_refresh"] = None
        summary["price_update_rate"] = None
        errors.append("data_refresh_state")

    if errors:
        raise HTTPException(status_code=503, detail={"error": "data status is partially unavailable", "fields": errors})

    return summary


@router.get("/auto-update")
def get_auto_update_status(request: Request) -> dict:
    """自动更新状态（PRD §15 只读展示）"""
    sqlite = request.app.state.sqlite
    from app.core.auto_update import AutoUpdateController

    try:
        controller = AutoUpdateController(duck=request.app.state.duck, sqlite=sqlite)
        return controller.persisted_status()
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"auto update status is unavailable: {error}") from error


@router.get("/retry-list")
def get_retry_list(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict:
    """重试列表摘要"""
    sqlite = request.app.state.sqlite
    try:
        rows = sqlite.query(
            "SELECT stock_code, data_type, adapter, error, retry_count "
            "FROM retry_list LIMIT ?",
            [limit],
        )
        return {"count": len(rows), "items": rows}
    except Exception as error:
        raise HTTPException(status_code=503, detail="retry list is unavailable") from error


@router.get("/missing-list")
def get_missing_list(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict:
    """缺失列表摘要"""
    sqlite = request.app.state.sqlite
    try:
        rows = sqlite.query(
            "SELECT stock_code, field_name, reason_code "
            "FROM missing_list WHERE resolved_at IS NULL LIMIT ?",
            [limit],
        )
        return {"count": len(rows), "items": rows}
    except Exception as error:
        raise HTTPException(status_code=503, detail="missing list is unavailable") from error
