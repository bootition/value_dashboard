"""数据状态页 API (PRD §15)

只读展示：更新时间、覆盖状态、回填状态、重试/缺失摘要。
不提供写操作（PRD DS3）。

写竞争降级：自动更新持有 DuckDB 写锁期间，逐股提交间隙读查询仍会被
串行化拖慢（实测 summary 可达 60s+）。这里在写锁存在时直接返回最近一次
成功计算的结果缓存并标注 stale=true，避免界面被读阻塞；轮询不再重复触发
全量聚合。缓存 60s 有效，无锁时每 60s 重算一次。
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-status", tags=["data-status"])

_SUMMARY_TTL_SECONDS = 60

# 当前免费源确实无法补齐的字段：不在缺失列表/计数中继续展示为“待处理”，
# 保留在库中留档；将来接入公告 PDF 解析或付费源时再从这里移除。
_DISABLED_MISSING_FIELDS = {
    "balance.core_tier1_capital_adequacy_ratio",
    "balance.tier1_capital_adequacy_ratio",
    "balance.capital_adequacy_ratio",
    "balance.non_performing_loan_ratio",
    "balance.provision_coverage_ratio",
    "balance.risk_coverage_ratio",
}

_ACTIONABLE_MISSING_SQL = """(
    field_name NOT IN ({disabled_placeholders})
    AND NOT (
        field_name = 'placement_funding'
        AND substr(stock_code, 1, 1) IN ('4', '8', '9')
    )
)"""
_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
_SUMMARY_CACHE_LOCK = threading.Lock()
_SUMMARY_REFRESH_LOCK = threading.Lock()
_SUMMARY_REFRESHING: set[str] = set()

# 指标快照重算任务（页面按钮触发；pending 股票由数据修复脚本登记）
_INDICATOR_RECOMPUTE_KEY = "snapshot_recompute_pending"
_INDICATOR_RECOMPUTE_STATE: dict[str, object] = {
    "status": "idle",
    "pending_codes": [],
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_INDICATOR_RECOMPUTE_LOCK = threading.Lock()


def _pending_snapshot_codes(sqlite) -> list[str]:
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [_INDICATOR_RECOMPUTE_KEY],
        )
        if not rows:
            return []
        value = json.loads(rows[0]["value"])
        return [str(code) for code in value if str(code).strip()]
    except Exception:
        return []


def _set_pending_snapshot_codes(sqlite, codes: list[str]) -> None:
    unique = sorted(set(str(code).strip() for code in codes if str(code).strip()))
    sqlite.execute(
        "INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        [_INDICATOR_RECOMPUTE_KEY, json.dumps(unique, ensure_ascii=False)],
    )


def _clear_pending_snapshot_codes(sqlite) -> None:
    with contextlib.suppress(Exception):
        sqlite.execute("DELETE FROM data_refresh_state WHERE key = ?", [_INDICATOR_RECOMPUTE_KEY])


def _recompute_worker(state, duck, sqlite, codes: list[str] | None) -> None:
    try:
        from app.core.indicators.calculator import IndicatorCalculator
        from app.core.storage.update_lock import UpdateLockError, exclusive_update

        # 2026-08-28 提速计划：指标重算与自动更新互斥。复用同一把跨进程
        # 增量更新锁，任何一方先持锁，另一方（自动更新或手动重算）立即让路，
        # 避免 DuckDB 写连接竞争把价格更新拖到 5-8 只/分。
        try:
            with exclusive_update(duck.db_path):
                calc = IndicatorCalculator(duck=duck, sqlite=sqlite)
                result = (
                    calc.compute_snapshot_for_codes(codes)
                    if codes
                    else calc.compute_snapshot_for_all()
                )
        except UpdateLockError:
            result = {"status": "skipped", "reason": "another_update_running"}
        state["result"] = result
        if result.get("status") == "success":
            state["status"] = "success"
            if codes:
                _clear_pending_snapshot_codes(sqlite)
        elif result.get("status") == "skipped":
            state["status"] = "skipped"
            state["error"] = "自动更新正在运行，本次指标重算已让路"
        else:
            state["status"] = "failed"
            state["error"] = result.get("reason") or result.get("gate")
    except Exception as error:
        state["error"] = str(error)
        state["status"] = "failed"
    finally:
        state["finished_at"] = datetime.now(UTC).isoformat()


def _update_write_lock_active(duck) -> bool:
    """任意写锁存在即认为正在写入（保守：宁愿命中缓存也不要卡读）。

    2026-08-14 红队 P2-1：此前只查 `.value-dashboard.update.lock`，
    维护/回填/发布类 CLI 写操作（.duckdb.write.lock）窗口被误判为空闲，
    stale 缓存降级与筛选快照口径标注双双失效。现统一为
    any_write_lock_active（update 锁 OR duckdb 写锁）。
    """
    try:
        from app.core.storage.update_lock import any_write_lock_active

        return any_write_lock_active(duck.db_path)
    except OSError:
        return False


def _summary_cache_key(request: Request) -> str:
    app = getattr(request, "app", request)
    return f"{app.state.duck.db_path}|{app.state.sqlite.db_path}"


def _cached_summary(request: Request, *, allow_stale: bool = False) -> tuple[dict | None, float | None]:
    with _SUMMARY_CACHE_LOCK:
        entry = _SUMMARY_CACHE.get(_summary_cache_key(request))
        if not entry:
            return None, None
        data = entry.get("data")
        if data is None:
            return None, None
        at = float(entry["at"])
        age = time.monotonic() - at
        if not allow_stale and age > _SUMMARY_TTL_SECONDS:
            return None, age
        return (dict(data) if isinstance(data, dict) else None), age


def _store_summary(request: Request, summary: dict) -> None:
    stored = dict(summary)
    stored.setdefault("summary_as_of", datetime.now(UTC).isoformat())
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[_summary_cache_key(request)] = {
            "at": time.monotonic(),
            "data": stored,
        }


def _actionable_missing_condition() -> tuple[str, list[str]]:
    placeholders = ", ".join("?" for _ in _DISABLED_MISSING_FIELDS)
    return (
        _ACTIONABLE_MISSING_SQL.replace("{disabled_placeholders}", placeholders),
        list(_DISABLED_MISSING_FIELDS),
    )


@router.get("/indicator-recompute")
def get_indicator_recompute(request: Request) -> dict:
    """查询指标快照重算任务状态与待处理股票。"""
    with _INDICATOR_RECOMPUTE_LOCK:
        state = dict(_INDICATOR_RECOMPUTE_STATE)
    state["pending_codes"] = _pending_snapshot_codes(request.app.state.sqlite)
    return state


@router.post("/indicator-recompute")
def start_indicator_recompute(request: Request) -> dict:
    """启动指标快照重算任务（有 pending 股票时只算 pending，否则全量）。"""
    try:
        stage_rows = request.app.state.sqlite.query(
            "SELECT current_stage FROM auto_update_state WHERE id = 1"
        )
        if stage_rows and stage_rows[0].get("current_stage") == "running":
            raise HTTPException(status_code=409, detail="自动更新正在运行，请等待更新完成后再重算指标快照")
        from app.core.storage.update_lock import update_lock_active

        # 跨进程防护：CLI/另一服务进程持锁时，持久化 current_stage 可能尚未同步，
        # 以锁文件为准直接拒绝，避免重算线程创建后才发现冲突。
        if update_lock_active(request.app.state.duck.db_path):
            raise HTTPException(status_code=409, detail="另一更新任务正在运行，请稍后再重算指标快照")
    except HTTPException:
        raise
    except Exception:
        pass
    with _INDICATOR_RECOMPUTE_LOCK:
        if _INDICATOR_RECOMPUTE_STATE.get("status") == "running":
            return dict(_INDICATOR_RECOMPUTE_STATE)
        codes = _pending_snapshot_codes(request.app.state.sqlite) or None
        _INDICATOR_RECOMPUTE_STATE.update({
            "status": "running",
            "pending_codes": codes or [],
            "result": None,
            "error": None,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
        })
        state = dict(_INDICATOR_RECOMPUTE_STATE)
    threading.Thread(
        target=_recompute_worker,
        args=(_INDICATOR_RECOMPUTE_STATE, request.app.state.duck, request.app.state.sqlite, codes),
        name="vd-indicator-recompute",
        daemon=True,
    ).start()
    return state


@router.get("/summary")
def get_summary(request: Request) -> dict:
    """数据状态摘要 (PRD §15 DS2)，stale-while-revalidate。

    同步全量构建（build_data_quality_status）在正式库上需 20s+，
    绝不能阻塞请求线程（曾致前端 15s 超时）。任何情况下都立即返回
    最近缓存（含过期值）或轻量占位，刷新由单例后台线程完成。
    """
    startup_readiness = getattr(request.app.state, "startup_readiness", None)
    write_lock_active = _update_write_lock_active(request.app.state.duck)
    cached, age = _cached_summary(request, allow_stale=True)
    if (
        cached is not None
        and cached.get("checking")
        and isinstance(startup_readiness, dict)
        and not startup_readiness.get("checking")
    ):
        cached = None
    if cached is None:
        cached = _lightweight_summary(request)
        _store_summary(request, cached)
        age = 0.0
    else:
        cached = dict(cached)
        if write_lock_active:
            cached["stale"] = True
            cached["stale_reason"] = "auto_update_active"
            cached["updating"] = True
    needs_refresh = (
        (age is None or age > _SUMMARY_TTL_SECONDS or bool(cached.get("checking")))
        and not write_lock_active
    )
    if needs_refresh:
        _ensure_summary_refresh(request)
    return cached


def _ensure_summary_refresh(request: Request) -> None:
    """Start a single-flight background refresh; never blocks the request."""
    key = _summary_cache_key(request)
    with _SUMMARY_REFRESH_LOCK:
        if key in _SUMMARY_REFRESHING:
            return
        _SUMMARY_REFRESHING.add(key)
    threading.Thread(
        target=_refresh_summary_worker,
        args=(request.app.state, key),
        name="vd-summary-refresh",
        daemon=True,
    ).start()


def _refresh_summary_worker(state, key: str) -> None:
    """Rebuild the summary off the request path and publish it atomically.

    Skips while the auto-update writer lock is active: the background build
    would otherwise open the DuckDB file concurrently with the update's own
    connections (Windows race, see duckdb_store.read_connection), and a stale
    cache is exactly what the writer-active contract already promises.
    """
    try:
        if _update_write_lock_active(state.duck):
            return
        # 后台全量核对是 DuckDB 内存消耗大户。给它单独设置 10GB 预算，
        # 避免把服务默认 14GB 配额全部占用后，并发进入的 K 线/详情等
        # 轻查询触发 OutOfMemoryException（2026-09-02 start.log 实锤）。
        with state.duck.memory_limit("10GB"):
            summary = _build_summary_from_state(state)
        summary.setdefault("summary_as_of", datetime.now(UTC).isoformat())
        with _SUMMARY_CACHE_LOCK:
            _SUMMARY_CACHE[key] = {"at": time.monotonic(), "data": dict(summary)}
    except Exception as error:
        logger.warning("后台 summary 刷新失败: %s", error)
    finally:
        with _SUMMARY_REFRESH_LOCK:
            _SUMMARY_REFRESHING.discard(key)


def _lightweight_summary(request: Request) -> dict:
    """Return a minimal summary when the writer lock is active and no cache exists.

    Mirrors the checking placeholder shape so the frontend renders
    "正在核对数据/自动更新中" instead of timing out. Carries the last
    successful job time so the page never falls back to "尚未初始化".
    """
    startup_readiness = getattr(request.app.state, "startup_readiness", None)
    if not isinstance(startup_readiness, dict):
        from app.core.data_quality import checking_data_readiness

        startup_readiness = checking_data_readiness()
    readiness = dict(startup_readiness)
    last_update: str | None = None
    try:
        rows = request.app.state.sqlite.query(
            "SELECT finished_at FROM job_logs "
            "WHERE status = 'success' ORDER BY finished_at DESC LIMIT 1"
        )
        if rows and rows[0].get("finished_at"):
            last_update = str(rows[0]["finished_at"])
    except Exception:
        last_update = None
    return {
        "data_quality": {
            "minimum_data_readiness": readiness,
            "dates": {},
            "dividends": {},
            "lineage": {},
            "code_identity": {},
            "operations": {},
            "warning_codes": [],
        },
        "minimum_data_readiness": readiness,
        "stock_count": 0,
        "price_raw_count": 0,
        "price_qfq_count": 0,
        "retry_count": 0,
        "missing_count": 0,
        "last_update": last_update,
        "recent_jobs": [],
        "stale": True,
        "stale_reason": "auto_update_active_no_cache",
        "checking": True,
    }


def _build_summary_from_state(state) -> dict:
    """Full summary build off the request path (background refresh)."""
    duck = state.duck
    sqlite = state.sqlite

    from app.core.data_quality import build_data_quality_status

    startup_readiness = getattr(state, "startup_readiness", None)
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

    # 最近一次更新执行时间（success/partial/failed 都记录），
    # 避免 partial 更新实际推进了数据、但 UI 仍显示几天前的“成功时间”。
    try:
        row = sqlite.query(
            "SELECT finished_at, job_type, status FROM job_logs "
            "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 5"
        )
        summary["recent_jobs"] = row
        summary["last_update"] = row[0]["finished_at"] if row else None
        summary["last_update_status"] = row[0]["status"] if row else None
    except Exception:
        summary["recent_jobs"] = []
        summary["last_update"] = None
        summary["last_update_status"] = None
        errors.append("recent_jobs")

    # 最近一次完全成功的完整更新（仍保留，用于区分 partial/failed）。
    try:
        success_row = sqlite.query(
            "SELECT finished_at FROM job_logs "
            "WHERE status='success' ORDER BY finished_at DESC LIMIT 1"
        )
        summary["last_full_success_at"] = success_row[0]["finished_at"] if success_row else None
    except Exception:
        summary["last_full_success_at"] = None
        errors.append("last_full_success_at")

    # 重试/缺失摘要
    try:
        row = sqlite.query("SELECT COUNT(*) as cnt FROM retry_list")
        summary["retry_count"] = row[0]["cnt"]
    except Exception:
        summary["retry_count"] = 0
        errors.append("retry_count")

    try:
        condition, disabled = _actionable_missing_condition()
        row = sqlite.query(
            f"SELECT COUNT(*) as cnt FROM missing_list "
            f"WHERE resolved_at IS NULL AND {condition}",
            disabled,
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

    try:
        table_exists = sqlite.query(
            "SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = 'auto_update_state'"
        )
        if not table_exists:
            return {
                "state": "enabled", "enabled": True, "paused": False,
                "current_stage": "idle", "progress": {}, "last_error": None,
                "last_success_at": None, "last_result": None,
                "last_skip_reason": None, "updated_at": None,
            }
        rows = sqlite.query(
            """SELECT state, paused, current_stage, progress_json, last_error,
                      last_success_at, updated_at
               FROM auto_update_state WHERE id = 1"""
        )
        if not rows:
            return {
                "state": "enabled", "enabled": True, "paused": False,
                "current_stage": "idle", "progress": {}, "last_error": None,
                "last_success_at": None, "last_result": None,
                "last_skip_reason": None, "updated_at": None,
            }
        row = rows[0]
        try:
            progress = json.loads(row.get("progress_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            progress = {}
        state = row.get("state") or "enabled"
        paused = bool(row.get("paused"))
        return {
            "state": "paused" if paused and state == "enabled" else state,
            "enabled": state != "disabled",
            "paused": paused,
            "current_stage": row.get("current_stage") or "idle",
            "progress": progress,
            "last_error": row.get("last_error"),
            "last_success_at": row.get("last_success_at"),
            "last_result": progress.get("status"),
            "last_skip_reason": progress.get("reason"),
            "updated_at": row.get("updated_at"),
        }
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"auto update status is unavailable: {error}") from error


@router.get("/retry-list")
def get_retry_list(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    data_type: str | None = Query(None, description="按 data_type 过滤（如 treasury_yield_curve）"),
) -> dict:
    """重试列表摘要（D-4：支持按数据域过滤，区分国债/股本等低频域条目）"""
    sqlite = request.app.state.sqlite
    try:
        if data_type:
            rows = sqlite.query(
                "SELECT stock_code, data_type, adapter, error, retry_count "
                "FROM retry_list WHERE data_type = ? LIMIT ?",
                [data_type, limit],
            )
        else:
            rows = sqlite.query(
                "SELECT stock_code, data_type, adapter, error, retry_count "
                "FROM retry_list LIMIT ?",
                [limit],
            )
        return {"count": len(rows), "items": rows}
    except Exception as error:
        raise HTTPException(status_code=503, detail="retry list is unavailable") from error


@router.get("/missing-list")
def get_missing_list(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    field_prefix: str | None = Query(None, description="按 field_name 前缀过滤（如 treasury_curve）"),
) -> dict:
    """缺失列表摘要（D-4：支持按字段前缀过滤）"""
    sqlite = request.app.state.sqlite
    try:
        condition, disabled = _actionable_missing_condition()
        if field_prefix:
            rows = sqlite.query(
                f"SELECT stock_code, field_name, reason_code "
                f"FROM missing_list WHERE resolved_at IS NULL "
                f"AND field_name LIKE ? AND {condition} LIMIT ?",
                [f"{field_prefix}%", *disabled, limit],
            )
            total = sqlite.query(
                f"SELECT COUNT(*) AS c FROM missing_list "
                f"WHERE resolved_at IS NULL AND field_name LIKE ? AND {condition}",
                [f"{field_prefix}%", *disabled],
            )
        else:
            rows = sqlite.query(
                f"SELECT stock_code, field_name, reason_code "
                f"FROM missing_list WHERE resolved_at IS NULL "
                f"AND {condition} LIMIT ?",
                [*disabled, limit],
            )
            total = sqlite.query(
                f"SELECT COUNT(*) AS c FROM missing_list "
                f"WHERE resolved_at IS NULL AND {condition}",
                disabled,
            )
        count = total[0]["c"] if total else len(rows)
        return {"count": count, "items": rows}
    except Exception as error:
        raise HTTPException(status_code=503, detail="missing list is unavailable") from error
