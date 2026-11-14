"""Read-only data quality signals for the status API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

DEFAULT_MINIMUM_HISTORY_OBSERVATIONS = 1_300
DEFAULT_MINIMUM_VOLUME_OBSERVATIONS = 1_300
READINESS_CACHE_KEY = "minimum_data_readiness"
READINESS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
_SCREENING_BLOCKING_WARNINGS = {
    "FINANCIAL_SHELL_ROWS",
    "SNAPSHOT_STALE",
    "CODE_IDENTITY_ALIAS",
    "LIVE_SCHEMA_INCOMPATIBLE",
    "LINEAGE_INVALID",
}
# 2026-11-12 契约 §3：发布门禁的硬阻断阈值。违规票 ≤ 阈值时逐股隔离发布，
# 超过阈值才认为"系统性单位错乱"并整批拒绝。
# 正式库 ~5,580 只上市股票 → 阈值 max(50, 55) = 55 只。
GATE_HARD_BLOCK_MIN = 50
GATE_HARD_BLOCK_RATIO = 0.01
# 2026-11-13 P1：绝对阈值在小库/退化库上形同虚设——上市股 <101 只时阈值恒为 50，
# 于是 30 只库里 30 只全违规也只会被逐股隔离、ready=True、发布集合空（等价于
# "永不出新快照"）。补一条与库大小无关的占比判据：**≥1/3 的上市股同时违规**
# 一律视为系统性错乱。分母 3 用整数比较实现，避免浮点边界问题。
GATE_HARD_BLOCK_SHARE_DIVISOR = 3


def checking_data_readiness() -> dict:
    """Return a conservative placeholder while the background check runs."""
    return {
        "ready": False,
        "checking": True,
        "cached": False,
        "stock_count": 0,
        "missing": {},
        "missing_counts": {},
        "schema_compatibility": {"compatible": True, "missing": []},
    }


def read_cached_data_readiness(sqlite: SQLiteStore) -> dict | None:
    """Load the last completed readiness check from the profile SQLite store."""
    try:
        rows = sqlite.query(
            "SELECT value, updated_at FROM data_refresh_state WHERE key = ?",
            [READINESS_CACHE_KEY],
        )
        if not rows:
            return None
        checked_at = datetime.fromisoformat(rows[0]["updated_at"])
        if checked_at.tzinfo is None:
            checked_at = checked_at.astimezone()
        if (datetime.now().astimezone() - checked_at).total_seconds() > READINESS_CACHE_MAX_AGE_SECONDS:
            return None
        value = json.loads(rows[0]["value"])
        if not isinstance(value, dict) or "ready" not in value:
            return None
        value["checking"] = False
        value["cached"] = True
        value["checked_at"] = rows[0]["updated_at"]
        return value
    except Exception:
        return None


def store_cached_data_readiness(sqlite: SQLiteStore, readiness: dict) -> dict:
    """Persist one completed check and return its display-ready representation."""
    checked_at = datetime.now().astimezone().isoformat()
    value = dict(readiness)
    value.pop("checking", None)
    value.pop("cached", None)
    value.pop("checked_at", None)
    sqlite.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        [READINESS_CACHE_KEY, json.dumps(value, ensure_ascii=False, default=str), checked_at],
    )
    value["checking"] = False
    value["cached"] = False
    value["checked_at"] = checked_at
    return value


def screening_readiness(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Return one fail-closed policy decision for every screening entry point."""
    quality = build_data_quality_status_cached(duck, sqlite)
    blocking_warnings = sorted(
        set(quality["warning_codes"]) & _SCREENING_BLOCKING_WARNINGS
    )
    if not _has_complete_trading_calendar(duck, sqlite):
        blocking_warnings.append("TRADING_CALENDAR_UNAVAILABLE")
    return {
        # 2026-08-27：筛选/导出不再被少数股票的数据缺口全局阻断。
        # 只保留真正影响全库/全字段的硬性阻断（schema、lineage 等）。
        "ready": not blocking_warnings,
        "readiness": quality["minimum_data_readiness"],
        "warning_codes": blocking_warnings,
    }

SCREENING_READINESS_CACHE_KEY = "screening_readiness_cache"
SCREENING_READINESS_CACHE_TTL_SECONDS = 600
FULL_DATA_QUALITY_CACHE_KEY = "data_quality_status_cache_v1"


def load_full_data_quality_cache(sqlite: SQLiteStore, fingerprint: str) -> dict | None:
    """Load a cached full data-quality build for an exact input fingerprint."""
    try:
        rows = sqlite.query(
            "SELECT value, updated_at FROM data_refresh_state WHERE key = ?",
            [FULL_DATA_QUALITY_CACHE_KEY],
        )
        if not rows or not rows[0].get("value"):
            return None
        value = json.loads(rows[0]["value"])
        if not isinstance(value, dict) or value.get("fingerprint") != fingerprint:
            return None
        value["cached_full_build"] = True
        value["cached_at"] = rows[0]["updated_at"]
        return value
    except Exception:
        return None


def store_full_data_quality_cache(
    sqlite: SQLiteStore, fingerprint: str, quality: dict,
) -> None:
    value = dict(quality)
    value["fingerprint"] = fingerprint
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            [
                FULL_DATA_QUALITY_CACHE_KEY,
                json.dumps(value, ensure_ascii=False, default=str),
                datetime.now(UTC).isoformat(),
            ],
        )


def build_data_quality_status_cached(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> dict:
    """Incremental full-quality build: reuse the persisted result while inputs unchanged.

    2026-09-03 优化：此前数据状态页每个刷新周期都执行一次约 20-80s 的
    4300 万行 source_audit 全量扫描。现在用与筛选就绪缓存同源的轻量指纹
    判断输入是否变化；更新完成后指纹变化才重算一次，空闲期直接复用缓存。
    """
    fingerprint = screening_readiness_cache_key(duck, sqlite)
    if fingerprint:
        cached = load_full_data_quality_cache(sqlite, fingerprint)
        if cached is not None:
            return cached
    quality = build_data_quality_status(duck, sqlite)
    if fingerprint:
        try:
            store_full_data_quality_cache(sqlite, fingerprint, quality)
        except Exception:
            logger.warning("全量数据质量缓存写入失败(非致命)")
    return quality


def screening_readiness_cache_key(duck: DuckDBStore, sqlite: SQLiteStore) -> str | None:
    """Cheap fingerprint for the global inputs behind screening readiness.

    COUNT(*) is metadata-only in DuckDB and the snapshot aggregate is served
    by zone maps on the production database; this must stay far cheaper than
    the full readiness build it guards.
    """
    try:
        snap = duck.read_query(
            """SELECT MAX(calculated_at) AS c, MAX(latest_price_date) AS p,
                      MAX(report_date) AS r, COUNT(*) AS n
               FROM indicator_snapshot"""
        )[0]
        counts = duck.read_query(
            """SELECT
                 (SELECT COUNT(*) FROM source_audit) AS source_audit_c,
                 (SELECT COUNT(*) FROM source_audit_archive) AS source_audit_archive_c,
                 (SELECT COUNT(*) FROM fetch_batch) AS fetch_batch_c,
                 (SELECT COUNT(*) FROM raw_response_archive_all) AS archive_c,
                 (SELECT COUNT(*) FROM indicator_snapshot) AS snapshot_c"""
        )[0]
        # 财务报表/分红/除权除息会改变 readiness 与筛选结果，必须进入指纹；
        # 只用 COUNT + MAX(date) 聚合，仍比全量 readiness 构建便宜得多。
        financials = duck.read_query(
            """SELECT
                 (SELECT COUNT(*) FROM balance_sheet) AS balance_c,
                 (SELECT MAX(report_date) FROM balance_sheet) AS balance_max,
                 (SELECT COUNT(*) FROM income_statement) AS income_c,
                 (SELECT MAX(report_date) FROM income_statement) AS income_max,
                 (SELECT COUNT(*) FROM cash_flow) AS cashflow_c,
                 (SELECT MAX(report_date) FROM cash_flow) AS cashflow_max"""
        )[0]
        dividends = duck.read_query(
            """SELECT COUNT(*) AS n, MAX(ex_date) AS ex_max,
                      MAX(announcement_date) AS ann_max
               FROM dividends"""
        )[0]
        xdxr = duck.read_query(
            "SELECT COUNT(*) AS n, MAX(event_date) AS event_max FROM xdxr"
        )[0]
        overrides = sqlite.query(
            """SELECT COUNT(*) AS n, MAX(created_at) AS created_max
               FROM manual_overrides
               WHERE status = 'published' AND rolled_back_at IS NULL"""
        )[0]
        calendar = sqlite.query(
            """SELECT COUNT(*) AS n, MIN(trade_date) AS lo, MAX(trade_date) AS hi
               FROM trading_dates"""
        )
        calendar_fp = calendar[0] if calendar else {}
        payload = {
            "snap": snap,
            "counts": counts,
            "financials": financials,
            "dividends": dividends,
            "xdxr": xdxr,
            "published_overrides": overrides,
            "trading_calendar": calendar_fp,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return None


def load_screening_readiness_cache(
    sqlite: SQLiteStore, fingerprint: str, *, allow_stale: bool = False,
) -> dict | None:
    """Return a cached screening decision, or None on miss/fingerprint change.

    Fresh calls reject entries older than ``SCREENING_READINESS_CACHE_TTL_SECONDS``.
    ``allow_stale=True`` still requires the same input fingerprint but permits an
    expired decision so the request path can use stale-while-revalidate instead
    of blocking on another 20s+ full-data-quality scan.
    """
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [SCREENING_READINESS_CACHE_KEY],
        )
        if not rows:
            return None
        payload = json.loads(rows[0]["value"] or "{}")
        if payload.get("fingerprint") != fingerprint:
            return None
        if not allow_stale:
            updated = payload.get("updated_at")
            if updated:
                updated_dt = datetime.fromisoformat(updated)
                if (
                    datetime.now(UTC) - updated_dt
                ).total_seconds() > SCREENING_READINESS_CACHE_TTL_SECONDS:
                    return None
        return payload.get("decision")
    except Exception:
        return None


_screening_readiness_refresh_lock = threading.Lock()
_screening_readiness_refreshing: set[str] = set()


def ensure_screening_readiness_refresh(
    duck: DuckDBStore, sqlite: SQLiteStore, fingerprint: str,
) -> None:
    """Refresh an expired screening decision in a single-flight background thread."""
    key = f"{duck.db_path}|{sqlite.db_path}|{fingerprint}"
    with _screening_readiness_refresh_lock:
        if key in _screening_readiness_refreshing:
            return
        _screening_readiness_refreshing.add(key)
    threading.Thread(
        target=_screening_readiness_refresh_worker,
        args=(duck, sqlite, fingerprint, key),
        name="vd-screening-readiness-refresh",
        daemon=True,
    ).start()


def _screening_readiness_refresh_worker(
    duck: DuckDBStore, sqlite: SQLiteStore, fingerprint: str, key: str,
) -> None:
    try:
        from app.core.storage.update_lock import any_write_lock_active

        if any_write_lock_active(duck.db_path):
            return
        decision = screening_readiness(duck, sqlite)
        store_screening_readiness_cache(sqlite, fingerprint, decision)
    except Exception as error:
        logger.warning("后台 screening readiness 刷新失败: %s", error)
    finally:
        with _screening_readiness_refresh_lock:
            _screening_readiness_refreshing.discard(key)


def store_screening_readiness_cache(
    sqlite: SQLiteStore, fingerprint: str, decision: dict,
) -> None:
    """Persist a completed screening decision atomically (best-effort)."""
    try:
        sqlite.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                             updated_at=excluded.updated_at""",
            [
                SCREENING_READINESS_CACHE_KEY,
                json.dumps({
                    "fingerprint": fingerprint,
                    "decision": decision,
                    "updated_at": datetime.now(UTC).isoformat(),
                }, default=str),
                datetime.now(UTC).isoformat(),
            ],
        )
    except Exception:
        logger.warning("screening readiness cache write failed", exc_info=True)


def warm_screening_readiness_cache(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> dict | None:
    """Compute and persist the screening decision when no fresh cache exists.

    Startup maintenance calls this off the web request path so the first
    user-triggered screening run normally hits the persistent cache instead
    of paying the full data-quality scan inline.
    """
    fingerprint = screening_readiness_cache_key(duck, sqlite)
    if not fingerprint:
        return None
    cached = load_screening_readiness_cache(sqlite, fingerprint)
    if cached is not None:
        return cached
    decision = screening_readiness(duck, sqlite)
    store_screening_readiness_cache(sqlite, fingerprint, decision)
    return decision




# Fraction of a stock's observed window that may lack bars before the
# calendar check fails. Suspensions legitimately produce zero bars on
# calendar days; 2% of a multi-year window covers ordinary suspensions while
# still failing real source gaps (e.g. 000560 qfq 2017-2020 ~12%).
_CALENDAR_GAP_TOLERANCE = 0.02
# Suspensions routinely span several days to a few weeks; any stock whose
# missing-bar count is within this absolute floor passes regardless of window
# size, so small recent windows (new listings, resume-from-suspension) are not
# failed on a handful of quiet days.
_CALENDAR_GAP_ABSOLUTE_FLOOR = 20


def _has_complete_trading_calendar(duck: DuckDBStore, sqlite: SQLiteStore) -> bool:
    """Require every listed stock's raw and QFQ series to match the persisted calendar.

    A global first/last-date check permits an incomplete calendar or an internal
    per-stock gap to pass. Screening indicators read latest_close from raw but
    require adjusted series for technicals, so validate both directions for both
    tables: every calendar date in a stock's observed window has a close, and
    every close date is represented by the calendar.

    Suspensions produce legal zero-bar days, so a per-stock gap fraction up to
    _CALENDAR_GAP_TOLERANCE of its window is accepted. Dates that exist in the
    price tables but not in the calendar remain fail-closed at zero tolerance.
    """
    try:
        calendar_dates = sorted({
            date.fromisoformat(str(row["trade_date"])[:10]).isoformat()
            for row in sqlite.query("SELECT trade_date FROM trading_dates")
        })
        if not calendar_dates:
            return False
        # 2026-08-30 提速：该检查在正式库上约需 60 秒，且筛选每次运行都会
        # 调用。交易日历与价格覆盖不变时结果不会变，因此用输入指纹做持久
        # 缓存；价格更新后仅首次筛选会重算一次。
        try:
            cal_fp = sqlite.query(
                "SELECT COUNT(*) AS c, MIN(trade_date) AS lo, MAX(trade_date) AS hi "
                "FROM trading_dates"
            )[0]
            price_fp = duck.read_query(
                """SELECT
                     (SELECT COUNT(*) FROM price_daily_raw WHERE close IS NOT NULL) AS raw_c,
                     (SELECT MIN(trade_date) FROM price_daily_raw WHERE close IS NOT NULL) AS raw_lo,
                     (SELECT MAX(trade_date) FROM price_daily_raw WHERE close IS NOT NULL) AS raw_hi,
                     (SELECT COUNT(*) FROM price_daily_qfq WHERE close IS NOT NULL) AS qfq_c,
                     (SELECT MIN(trade_date) FROM price_daily_qfq WHERE close IS NOT NULL) AS qfq_lo,
                     (SELECT MAX(trade_date) FROM price_daily_qfq WHERE close IS NOT NULL) AS qfq_hi"""
            )[0]
            import hashlib as _hashlib
            fingerprint = _hashlib.sha256(
                json.dumps({
                    "calendar": cal_fp,
                    "price": price_fp,
                }, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            cached = sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = 'trading_calendar_gate_cache'"
            )
            if cached:
                payload = json.loads(cached[0]["value"] or "{}")
                if payload.get("fingerprint") == fingerprint and "result" in payload:
                    return bool(payload["result"])
        except Exception:
            fingerprint = None
        values = ", ".join("(CAST(? AS DATE))" for _ in calendar_dates)
        missing = duck.read_query(
            f"""
            WITH calendar(trade_date) AS (VALUES {values}),
            raw_windows AS (
                SELECT m.stock_code, MIN(r.trade_date) AS first_date, MAX(r.trade_date) AS last_date
                FROM stock_meta m
                JOIN price_daily_raw r ON r.stock_code = m.stock_code AND r.close IS NOT NULL
                WHERE m.is_listed IS TRUE
                GROUP BY m.stock_code
            ),
            qfq_windows AS (
                SELECT m.stock_code, MIN(q.trade_date) AS first_date, MAX(q.trade_date) AS last_date
                FROM stock_meta m
                JOIN price_daily_qfq q ON q.stock_code = m.stock_code AND q.close IS NOT NULL
                WHERE m.is_listed IS TRUE
                GROUP BY m.stock_code
            ),
            raw_stats AS (
                SELECT w.stock_code, w.first_date, w.last_date,
                       (SELECT COUNT(*) FROM calendar c
                        WHERE c.trade_date BETWEEN w.first_date AND w.last_date) AS window_days,
                       COUNT(r.trade_date) AS have_days
                FROM raw_windows w
                LEFT JOIN calendar c ON c.trade_date BETWEEN w.first_date AND w.last_date
                LEFT JOIN price_daily_raw r
                  ON r.stock_code = w.stock_code AND r.trade_date = c.trade_date AND r.close IS NOT NULL
                GROUP BY w.stock_code, w.first_date, w.last_date
            ),
            qfq_stats AS (
                SELECT w.stock_code, w.first_date, w.last_date,
                       (SELECT COUNT(*) FROM calendar c
                        WHERE c.trade_date BETWEEN w.first_date AND w.last_date) AS window_days,
                       COUNT(q.trade_date) AS have_days
                FROM qfq_windows w
                LEFT JOIN calendar c ON c.trade_date BETWEEN w.first_date AND w.last_date
                LEFT JOIN price_daily_qfq q
                  ON q.stock_code = w.stock_code AND q.trade_date = c.trade_date AND q.close IS NOT NULL
                GROUP BY w.stock_code, w.first_date, w.last_date
            ),
            raw_gap_excess AS (
                SELECT stock_code FROM raw_stats
                WHERE window_days > 0
                  AND have_days < window_days * (1 - {_CALENDAR_GAP_TOLERANCE})
                  AND window_days - have_days > {_CALENDAR_GAP_ABSOLUTE_FLOOR}
            ),
            qfq_gap_excess AS (
                SELECT stock_code FROM qfq_stats
                WHERE window_days > 0
                  AND have_days < window_days * (1 - {_CALENDAR_GAP_TOLERANCE})
                  AND window_days - have_days > {_CALENDAR_GAP_ABSOLUTE_FLOOR}
            ),
            raw_missing_calendar AS (
                SELECT r.stock_code, 'raw' AS source
                FROM price_daily_raw r
                JOIN stock_meta m ON m.stock_code = r.stock_code AND m.is_listed IS TRUE
                LEFT JOIN calendar c ON c.trade_date = r.trade_date
                WHERE r.close IS NOT NULL AND c.trade_date IS NULL
            ),
            qfq_missing_calendar AS (
                SELECT q.stock_code, 'qfq' AS source
                FROM price_daily_qfq q
                JOIN stock_meta m ON m.stock_code = q.stock_code AND m.is_listed IS TRUE
                LEFT JOIN calendar c ON c.trade_date = q.trade_date
                WHERE q.close IS NOT NULL AND c.trade_date IS NULL
            )
            SELECT stock_code FROM raw_gap_excess
            UNION SELECT stock_code FROM qfq_gap_excess
            UNION SELECT stock_code FROM raw_missing_calendar
            UNION SELECT stock_code FROM qfq_missing_calendar
            LIMIT 1
            """,
            calendar_dates,
        )
        try:
            if fingerprint is not None:
                sqlite.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES ('trading_calendar_gate_cache', ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                     updated_at=excluded.updated_at""",
                    [json.dumps({"fingerprint": fingerprint, "result": True}), datetime.now(UTC).isoformat()],
                )
        except Exception:
            pass
        return not missing
    except Exception:
        return False


# 2026-11-12 契约 §3：股本完整性地板的唯一 SQL。
# 违规 = NOT(total>0 AND circ>0 AND circ<=total)，且不满足
# 「上市 ≤90 天且 total/circ 皆为 NULL」的新股豁免。
_SHARE_CAPITAL_FLOOR_SQL = """
    SELECT stock_code
    FROM stock_meta
    WHERE is_listed IS TRUE
      AND NOT (
          listing_date IS NOT NULL
          AND listing_date >= CURRENT_DATE - INTERVAL '90 days'
          AND total_shares IS NULL AND circ_shares IS NULL
       )
      AND NOT (
          total_shares IS NOT NULL AND total_shares > 0
          AND circ_shares IS NOT NULL AND circ_shares > 0
          AND circ_shares <= total_shares
      )
    ORDER BY stock_code
"""


def _strict_share_capital_gap_codes(duck: DuckDBStore) -> list[str]:
    """Raw share-capital floor query; raises so callers can fail closed."""
    rows = duck.read_query(_SHARE_CAPITAL_FLOOR_SQL)
    return [str(row["stock_code"]) for row in rows]


def listed_share_capital_gap_codes(duck: DuckDBStore) -> list[str]:
    """List every listed stock that breaks the share-capital integrity floor.

    **单一口径（契约 §3）**：发布门禁、minimum_data_readiness 的
    ``has_share_capital``、以及 update.py 的每日定向补抓全部复用本函数，
    禁止在别处复制这份 SQL。

    永不抛异常：查询失败时记录 warning 并返回 ``[]``，避免一次瞬时读错误
    把每日补抓流程整体打断。需要 fail-closed 的调用方（发布门禁）请使用
    :func:`_strict_share_capital_gap_codes`，它会把异常抛给上层。
    """
    try:
        return _strict_share_capital_gap_codes(duck)
    except Exception as error:
        logger.warning("股本完整性检查失败，缺口列表按空处理: %s", error)
        return []


def _listed_stock_count(duck: DuckDBStore) -> int:
    rows = duck.read_query("SELECT COUNT(*) AS cnt FROM stock_meta WHERE is_listed IS TRUE")
    return int(rows[0]["cnt"]) if rows else 0


def gate_hard_block_threshold(listed_count: int) -> int:
    """``max(50, int(listed_count * 0.01))`` — see contract §3."""
    return max(GATE_HARD_BLOCK_MIN, int(listed_count * GATE_HARD_BLOCK_RATIO))


def gate_hard_blocks_by_share(violation_count: int, listed_count: int) -> bool:
    """「≥1/3 上市股同时违规」→ 系统性错乱（与库大小无关，小库也能触发）。

    用整数比较 ``violation_count * 3 >= listed_count`` 实现"≥1/3"，
    而不是 ``violation_count >= int(listed_count / 3)``：后者在 20 只库里
    会把 6 只（30%）误判成系统性（``int(20/3) == 6``），与"≥1/3"不符。
    空库（``listed_count <= 0``）或零违规一律不触发。
    """
    if listed_count <= 0 or violation_count <= 0:
        return False
    return violation_count * GATE_HARD_BLOCK_SHARE_DIVISOR >= listed_count


def share_capital_hard_block(violation_count: int, listed_count: int) -> bool:
    """发布门禁与 readiness 共用的硬阻断判定（契约 §3 唯一实现）。

    命中任一即整批拒绝（hard_block）：
    * ``violation_count > gate_hard_block_threshold(listed_count)``
      —— 系统性规模（正式库 5,580 只 → 55 只）；
    * ``violation_count >= listed_count / 3``
      —— 占比兜底，小库/退化库上同样能识别"整库都被隔离"。
    ``snapshot_publish_gate`` 与 ``minimum_data_readiness`` 都必须调用本函数，
    禁止各写一份判定（否则同一状态会得出两个相反结论）。
    """
    return (
        violation_count > gate_hard_block_threshold(listed_count)
        or gate_hard_blocks_by_share(violation_count, listed_count)
    )


def share_capital_violations(duck: DuckDBStore) -> list[str]:
    """Deprecated alias — kept so existing callers keep one implementation (§3)."""
    return listed_share_capital_gap_codes(duck)


def snapshot_publish_gate(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Quality gate consulted before an indicator snapshot is published.

    2026-11-12 P0（契约 §3/§4）：此前本门禁是 fail-closed 的"一票否决"——
    任一只上市股票股本不满足 ``total>0 AND circ>0 AND circ<=total`` 就
    ``ready=False``，三个发布调用点随即整批拒绝，于是 26 只股本缺失票
    （其中 5 只新股在 11-03~11-10 陆续越过 90 天豁免线）让全市场 5,580 只
    快照自 10-30 起再没发布过。

    新语义（分级）：
    - ``violation_count > threshold``：怀疑系统性单位错乱 → ``hard_block``，
      整批拒绝（保留上一代快照）；
    - 否则 ``ready=True, hard_block=False``，违规票进 ``quarantined_codes``
      逐股隔离，其余正常发布；
    - 检查本身抛异常 → ``ready=False, hard_block=True``（fail-closed）。

    唯一口径：违规集合来自 :func:`listed_share_capital_gap_codes`（含 90 天
    新股豁免），readiness 的 ``has_share_capital`` 复用同一函数；本函数使用
    其严格版本，以便把"查询失败"与"没有违规"区分开。
    """
    try:
        violations = _strict_share_capital_gap_codes(duck)
        listed_count = _listed_stock_count(duck)
    except Exception as error:
        # 门禁自身不可用必须 fail-closed：宁可保留上一代快照，也不发布
        # 未经校验收割的快照。
        logger.error("股本完整性门禁不可用，整批拒绝发布(fail-closed): %s", error)
        return {
            "ready": False,
            "reason": "share_capital_check_unavailable",
            "error": str(error),
            "hard_block": True,
            "quarantined_codes": [],
            "violations": [],
            "violation_count": 0,
            "listed_count": 0,
            "threshold": max(GATE_HARD_BLOCK_MIN, 0),
            "hard_block_rule": "check_unavailable",
        }
    threshold = gate_hard_block_threshold(listed_count)
    violation_count = len(violations)
    # 2026-11-13 P1：绝对阈值 OR 占比判据（≥1/3），唯一实现见
    # share_capital_hard_block。占比判据让 30 只的小库也能识别"整库被隔离"。
    over_threshold = violation_count > threshold
    over_share = gate_hard_blocks_by_share(violation_count, listed_count)
    hard_block = over_threshold or over_share
    hard_block_rule = (
        "threshold" if over_threshold else "share" if over_share else None
    )
    if hard_block:
        logger.error(
            "股本完整性违规 %s 只（上市 %s 只，绝对阈值 %s 只，命中=%s），"
            "疑似系统性单位错乱，整批拒绝发布",
            violation_count,
            listed_count,
            threshold,
            hard_block_rule,
        )
    elif violations:
        logger.warning(
            "股本完整性违规 %s 只（上市 %s 只，阈值 %s 只）→ 逐股隔离发布: %s",
            len(violations),
            listed_count,
            threshold,
            violations[:20],
        )
    return {
        "ready": not hard_block,
        "reason": "share_capital_integrity" if hard_block else None,
        "hard_block": hard_block,
        # 命中哪条规则：threshold（绝对阈值）/ share（≥1/3 占比）/ None。
        "hard_block_rule": hard_block_rule,
        "quarantined_codes": list(violations),
        "violations": violations[:20],
        "violation_count": violation_count,
        "listed_count": listed_count,
        "threshold": threshold,
    }


def actionable_missing_count(sqlite: SQLiteStore) -> int:
    """Single source of truth for the actionable ``missing_list`` count (§3).

    口径 = ``resolved_at IS NULL`` 且"可行动"（复用 web 层的
    ``_actionable_missing_condition()``）。2026-11-12 P1：此前 CLI 用
    ``SELECT COUNT(*) FROM missing_list``（含已解决行）报 11,823，而状态页
    用可行动口径报 162，同一时刻两个数字。

    异常向上抛出（不返回 0）：把"查询坏了"伪装成"没有缺口"是假绿。
    """
    # 惰性导入：app.web.api.data_status 反向依赖本模块，模块级导入会成环。
    from app.web.api.data_status import _actionable_missing_condition

    condition, disabled = _actionable_missing_condition()
    rows = sqlite.query(
        f"SELECT COUNT(*) AS cnt FROM missing_list "
        f"WHERE resolved_at IS NULL AND {condition}",
        disabled,
    )
    return int(rows[0]["cnt"]) if rows else 0



def snapshot_period_mismatches(duck: DuckDBStore) -> list[dict]:
    """Return listed stocks whose published snapshot period is not the latest
    complete three-statement period.

    The latest complete period is the newest report_date where balance_sheet,
    income_statement, and cash_flow all carry their core non-null fields.
    Newer statement rows with missing core fields are a data-source pending
    state (PRD §7.7: "数据源尚未就绪时保留旧值"), not a screening period.

    This is the single shared judgment for both the readiness gate
    (minimum_data_readiness → snapshot_period_alignment) and the screening
    engine (_reject_mixed_report_dates), so a partial source update can never
    pass one and silently block the other.
    """
    return duck.read_query(
        """
        SELECT m.stock_code,
               s.report_date AS snapshot_date,
               cp.complete_date
        FROM stock_meta m
        LEFT JOIN LATERAL (
            SELECT report_date FROM indicator_snapshot
            WHERE stock_code = m.stock_code
            ORDER BY report_date DESC LIMIT 1
        ) s ON true
        LEFT JOIN LATERAL (
            SELECT MAX(bs.report_date) AS complete_date
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.stock_code = m.stock_code
              AND bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
              AND cf.cf_from_operating IS NOT NULL
        ) cp ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_date FROM balance_sheet WHERE stock_code = m.stock_code
        ) lb ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_date FROM income_statement WHERE stock_code = m.stock_code
        ) li ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_date FROM cash_flow WHERE stock_code = m.stock_code
        ) lc ON true
        WHERE m.is_listed IS TRUE
          AND s.report_date IS NOT NULL
          AND (
              -- 完整期存在但与快照期不一致（任一方向）
              (cp.complete_date IS NOT NULL AND s.report_date <> cp.complete_date)
              -- 报表有比快照更新的行，但从未构成完整期（无法回算/校验口径）
              OR (cp.complete_date IS NULL
                  AND (
                      lb.max_date IS NOT NULL AND lb.max_date > s.report_date
                      OR li.max_date IS NOT NULL AND li.max_date > s.report_date
                      OR lc.max_date IS NOT NULL AND lc.max_date > s.report_date
                  ))
          )
        ORDER BY m.stock_code
        """
    )


def minimum_data_readiness(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    minimum_history_observations: int = DEFAULT_MINIMUM_HISTORY_OBSERVATIONS,
    minimum_volume_observations: int = DEFAULT_MINIMUM_VOLUME_OBSERVATIONS,
) -> dict:
    """Evaluate the current, per-stock screening floor without mutating data."""
    if minimum_history_observations < 1 or minimum_volume_observations < 1:
        raise ValueError("readiness observation thresholds must be positive")
    compatibility = _live_schema_compatibility(duck)
    if not compatibility["compatible"]:
        missing = {"schema_compatibility": compatibility["missing"][:20]}
        return {
            "ready": False,
            "stock_count": 0,
            "missing": missing,
            "missing_counts": {"schema_compatibility": len(compatibility["missing"])},
            "schema_compatibility": compatibility,
        }

    rows = duck.read_query(
        """
        WITH raw AS (
            SELECT stock_code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                   COUNT(*) AS observation_count,
                   COUNT(*) FILTER (WHERE volume IS NOT NULL AND volume > 0) AS volume_count
            FROM price_daily_raw WHERE close IS NOT NULL GROUP BY stock_code
        ), qfq AS (
            SELECT stock_code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                   COUNT(*) AS observation_count
            FROM price_daily_qfq WHERE close IS NOT NULL GROUP BY stock_code
        ), complete_financials AS (
            SELECT bs.stock_code, MAX(bs.report_date) AS report_date,
                   arg_max(bs.capital_adequacy_ratio, bs.report_date) AS capital_adequacy_ratio,
                   arg_max(bs.non_performing_loan_ratio, bs.report_date) AS non_performing_loan_ratio,
                   arg_max(bs.provision_coverage_ratio, bs.report_date) AS provision_coverage_ratio,
                   arg_max(bs.risk_coverage_ratio, bs.report_date) AS risk_coverage_ratio
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
               AND cf.cf_from_operating IS NOT NULL
            GROUP BY bs.stock_code
        )
        SELECT m.stock_code,
            m.listing_date,
            m.total_shares,
            m.circ_shares,
            m.listing_date >= CURRENT_DATE - INTERVAL '90 days' AS is_recent_listing,
            raw.last_date AS raw_last_date,
            qfq.last_date AS qfq_last_date,
            complete_financials.report_date AS financial_report_date,
            m.listing_date IS NOT NULL AND m.is_st IS NOT NULL AND m.is_suspended IS NOT NULL
                AS has_pool_metadata,
            -- 新股（上市不足 90 天）仍在积累历史与成交量，属于市场真实状态，
            -- 不阻断筛选（披露项），因此对这些检查豁免。
            (m.listing_date >= CURRENT_DATE - INTERVAL '90 days'
             OR (raw.first_date IS NOT NULL
                 AND raw.first_date <= GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years') + INTERVAL '30 days'
                 AND raw.observation_count >= LEAST(?, GREATEST(1, CAST(CEIL(
                     date_diff('day', GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years'), CURRENT_DATE) * 0.45
                 ) AS INTEGER)))))
                AS has_raw_history,
            (m.listing_date >= CURRENT_DATE - INTERVAL '90 days'
             OR (qfq.first_date IS NOT NULL
                 AND qfq.first_date <= GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years') + INTERVAL '30 days'
                 AND qfq.observation_count >= LEAST(?, GREATEST(1, CAST(CEIL(
                     date_diff('day', GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years'), CURRENT_DATE) * 0.45
                 ) AS INTEGER)))))
                AS has_qfq_history,
            -- 价格允许陈旧但必须显示日期（PRD §6.4 D7）：已有数据但超过
            -- 7 天无新 bar 的股票视为停牌，新鲜度缺口属披露项，不阻断。
            -- 完全没有数据的股票仍为缺口（数据损坏类）。
            (raw.last_date IS NOT NULL
             AND (raw.last_date < CURRENT_DATE - INTERVAL '7 days'
                  OR raw.last_date >= CURRENT_DATE - INTERVAL '7 days'
                     AND qfq.last_date >= CURRENT_DATE - INTERVAL '7 days'))
                AS has_fresh_prices,
            (m.listing_date >= CURRENT_DATE - INTERVAL '90 days'
             OR raw.volume_count >= LEAST(?, GREATEST(1, CAST(CEIL(
                 date_diff('day', GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years'), CURRENT_DATE) * 0.45
             ) AS INTEGER)))) AS has_meaningful_volume,
            complete_financials.report_date >= CURRENT_DATE - INTERVAL '18 months'
                AS has_complete_financial_period,
            EXISTS (SELECT 1 FROM xdxr action WHERE action.stock_code = m.stock_code)
              AND EXISTS (
                SELECT 1 FROM dividends dividend
                WHERE dividend.stock_code = m.stock_code
                  AND dividend.announcement_date IS NOT NULL
              ) AS has_corporate_action_or_dividend_lineage,
            CASE
                WHEN lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%银行%'
                  OR lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%证券%'
                THEN complete_financials.report_date IS NOT NULL
                ELSE TRUE
            END AS has_sector_financials,
            -- 银行/券商监管字段（资本充足率/不良贷款率/拨备覆盖率/风险覆盖率）：
            -- 免费结构化 API 不可得，所有者口径为"保持 NULL，不伪造"（STATUS 缺口#4），
            -- 因此该缺口属披露项而非阻断项（disclosure_keys 处理）。
            CASE
                WHEN lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%银行%'
                THEN complete_financials.capital_adequacy_ratio IS NOT NULL
                  AND complete_financials.non_performing_loan_ratio IS NOT NULL
                  AND complete_financials.provision_coverage_ratio IS NOT NULL
                WHEN lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%证券%'
                THEN complete_financials.risk_coverage_ratio IS NOT NULL
                ELSE TRUE
            END AS has_regulatory_fields,
            EXISTS (
                SELECT 1 FROM indicator_snapshot snapshot
                WHERE snapshot.stock_code = m.stock_code
                  AND snapshot.report_date = complete_financials.report_date
                  AND snapshot.calculated_at IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM source_audit audit
                    WHERE audit.stock_code = m.stock_code
                      AND audit.report_date = complete_financials.report_date
                      AND audit.field_name IN (
                          'total_assets', 'total_liabilities', 'total_equity', 'total_equity_parent',
                          'revenue', 'parent_net_profit', 'cf_from_operating'
                      )
                       AND audit.fetch_time > snapshot.calculated_at
                   )
            ) AS has_current_snapshot_inputs,
            EXISTS (
                SELECT 1 FROM indicator_snapshot snapshot
                JOIN price_daily_raw current_raw
                  ON current_raw.stock_code = snapshot.stock_code
                 AND current_raw.trade_date = raw.last_date
                WHERE snapshot.stock_code = m.stock_code
                  AND snapshot.report_date = complete_financials.report_date
                  AND snapshot.latest_price_date = raw.last_date
                  AND snapshot.latest_close IS NOT NULL
                  AND current_raw.close IS NOT NULL
                  AND ABS(snapshot.latest_close - current_raw.close)
                      <= GREATEST(ABS(current_raw.close), 1.0) * 0.000001
            ) AS has_coherent_snapshot,
            -- P0-4/5: 快照期必须等于最新完整三表期（与筛选引擎共用判定）。
            -- 若任一张报表存在晚于完整期的新行，属"数据源未就绪"的披露项
            -- （PRD §7.7），不阻断；快照期与完整期不一致才是阻断项。
            (complete_financials.report_date IS NOT NULL AND (
                lb.max_balance IS NOT NULL AND lb.max_balance > complete_financials.report_date
                OR li.max_income IS NOT NULL AND li.max_income > complete_financials.report_date
                OR lc.max_cashflow IS NOT NULL AND lc.max_cashflow > complete_financials.report_date
            )) AS has_pending_incomplete_period
        FROM stock_meta m
        LEFT JOIN raw ON raw.stock_code = m.stock_code
        LEFT JOIN qfq ON qfq.stock_code = m.stock_code
        LEFT JOIN complete_financials ON complete_financials.stock_code = m.stock_code
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_balance FROM balance_sheet WHERE stock_code = m.stock_code
        ) lb ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_income FROM income_statement WHERE stock_code = m.stock_code
        ) li ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_cashflow FROM cash_flow WHERE stock_code = m.stock_code
        ) lc ON true
        WHERE m.is_listed IS TRUE
        ORDER BY m.stock_code
        """,
        [
            minimum_history_observations,
            minimum_history_observations,
            minimum_volume_observations,
        ],
    )
    issues: dict[str, list[str]] = {
        "pool_metadata": [], "raw_history": [], "qfq_history": [],
        "price_freshness": [], "meaningful_volume": [], "financial_period": [],
        "snapshot_price_coherence": [], "share_capital": [],
        "corporate_action_dividend_lineage": [],
        "sector_financials": [], "regulatory_fields": [], "snapshot_input_freshness": [],
        "lineage_coverage": [], "snapshot_period_alignment": [],
        "pending_financial_period": [],
    }
    for row in rows:
        for key, column in (
            ("pool_metadata", "has_pool_metadata"),
            ("raw_history", "has_raw_history"),
            ("qfq_history", "has_qfq_history"),
            ("price_freshness", "has_fresh_prices"),
            ("meaningful_volume", "has_meaningful_volume"),
            ("financial_period", "has_complete_financial_period"),
            ("corporate_action_dividend_lineage", "has_corporate_action_or_dividend_lineage"),
            ("sector_financials", "has_sector_financials"),
            ("regulatory_fields", "has_regulatory_fields"),
            ("snapshot_input_freshness", "has_current_snapshot_inputs"),
            ("snapshot_price_coherence", "has_coherent_snapshot"),
        ):
            if not row[column]:
                issues[key].append(row["stock_code"])
        # has_pending_incomplete_period 为真才是不利状态（列语义与其余相反）
        if row.get("has_pending_incomplete_period"):
            issues["pending_financial_period"].append(row["stock_code"])
    issues["lineage_coverage"].extend(_missing_lineage_coverage(duck, rows))
    # 股本完整性走唯一真相函数（契约 §3）：发布门禁与 readiness 同源，
    # 90 天新股豁免已在该函数内实现，此处不得再写第二份判定。
    issues["share_capital"] = listed_share_capital_gap_codes(duck)
    # P0-4/5: 快照期与最新完整三表期不一致是阻断项（与筛选引擎共用判定），
    # 消除 "ready=true 但筛选全部失败" 的假阳性。
    issues["snapshot_period_alignment"] = [
        row["stock_code"] for row in snapshot_period_mismatches(duck)
    ]
    if sqlite is not None:
        stale_override_codes = _published_override_stale_snapshot_codes(duck, sqlite)
        issues["snapshot_input_freshness"].extend(stale_override_codes)
    for key, codes in issues.items():
        issues[key] = list(dict.fromkeys(codes))
    # 披露性缺口（市场真实状态，非数据损坏）：公司从未分红/无除权事件是
    # 合法事实，筛选指标对其正确返回空值；报表存在晚于完整期的新行是
    # "数据源尚未就绪"（PRD §7.7），快照按完整期计算；银行/券商监管字段
    # 免费源不可得（所有者口径：保持 NULL，不伪造）——均不阻断筛选，
    # 但保留计数供 UI 披露。
    disclosure_keys = {
        "corporate_action_dividend_lineage",
        "pending_financial_period",
        "regulatory_fields",
    }
    recent_listing_disclosure_keys = {
        "price_freshness",
        "financial_period",
        "snapshot_input_freshness",
        "snapshot_price_coherence",
    }
    recent_codes = {
        row["stock_code"] for row in rows
        if row.get("listing_date") is not None and row.get("is_recent_listing")
    }
    missing = {key: codes[:20] for key, codes in issues.items() if codes}
    blocking: dict[str, list[str]] = {}
    for key, codes in issues.items():
        if key in disclosure_keys:
            continue
        # share_capital 走「阈值政策」而非「一票否决」，见下方统一判定。
        if key == "share_capital":
            continue
        blocking_codes = []
        for code in codes:
            # 新品 90 天豁免已在唯一真相函数 / 各列 SQL 内判定，此处不再复制口径。
            if key in recent_listing_disclosure_keys and code in recent_codes:
                continue
            blocking_codes.append(code)
        if blocking_codes:
            blocking[key] = blocking_codes
    # 2026-11-13 P1：readiness 与发布门禁必须同一政策（阈值唯一实现在
    # gate_hard_block_threshold）。
    # 为什么不能"任一缺口即 ready=False"：少量股本缺口在发布侧是被逐股隔离、
    # 其余股票照常发布的；若 readiness 对同一状态判 ready=False，/api/ready
    # 就会 503 假红（同一状态两个相反结论）。只有缺口数超过硬阻断阈值
    # （=怀疑系统性单位错乱、发布侧整批拒绝）才在这里阻断。
    # 未超阈值时缺失计数仍在 missing_counts["share_capital"] 里如实披露。
    share_capital_gaps = issues.get("share_capital") or []
    share_capital_threshold = gate_hard_block_threshold(len(rows))
    # 与 snapshot_publish_gate 同一判定：绝对阈值 OR ≥1/3 占比。两边必须
    # 同源，否则同一状态会出现"发布侧整批拒绝 / readiness=ready"或反之。
    share_capital_blocking = share_capital_hard_block(
        len(share_capital_gaps), len(rows),
    )
    if share_capital_blocking:
        blocking["share_capital"] = share_capital_gaps
    return {
        "ready": bool(rows) and not blocking,
        "stock_count": len(rows),
        "missing": missing,
        "missing_counts": {key: len(codes) for key, codes in issues.items() if codes},
        "disclosure_missing_counts": {
            key: len(issues[key]) for key in disclosure_keys if issues.get(key)
        },
        # 股本缺口的阈值政策如实披露：多少个缺口、阈值多少、是否据此阻断。
        "share_capital_policy": {
            "gap_count": len(share_capital_gaps),
            "threshold": share_capital_threshold,
            "blocking": share_capital_blocking,
            "quarantined": not share_capital_blocking and bool(share_capital_gaps),
        },
        "schema_compatibility": compatibility,
    }


def _missing_lineage_coverage(duck: DuckDBStore, readiness_rows: list[dict]) -> list[str]:
    """Fail closed if materialized inputs or snapshots lost their source graph.

    2026-08-30 提速：原实现把约 957 万条 evidence 行全部拉回 Python 再逐股
    判空，正式库单次约 25 秒。改为在 DuckDB 内先按每只股票所需的
    raw/qfq 最新交易日、完整财务报告期裁剪 evidence，再聚合出缺失股票。
    语义与原实现一致（新股/停牌股仍豁免），结果只返回缺失代码。
    """
    if not readiness_rows:
        return []
    rows = duck.read_query(
        """
        WITH valid_hashes AS (
            SELECT raw_response_hash FROM raw_response_archive_valid_hash
            UNION ALL
            -- 兼容旧测试/维护脚本直接 INSERT raw_response_archive 的路径；
            -- 只回退扫描小型 active 热表，绝不回退到 26GB 的 history 大表。
            SELECT a.raw_response_hash FROM raw_response_archive a
            WHERE a.payload IS NOT NULL
              AND OCTET_LENGTH(a.payload) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM raw_response_archive_valid_hash v
                  WHERE v.raw_response_hash = a.raw_response_hash
              )
        ), raw AS (
            SELECT stock_code, MAX(trade_date) AS last_date
            FROM price_daily_raw WHERE close IS NOT NULL GROUP BY stock_code
        ), qfq AS (
            SELECT stock_code, MAX(trade_date) AS last_date
            FROM price_daily_qfq WHERE close IS NOT NULL GROUP BY stock_code
        ), complete_financials AS (
            SELECT bs.stock_code, MAX(bs.report_date) AS report_date
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
              AND cf.cf_from_operating IS NOT NULL
            GROUP BY bs.stock_code
        ), base AS (
            SELECT m.stock_code,
                   m.listing_date,
                   raw.last_date AS raw_last_date,
                   qfq.last_date AS qfq_last_date,
                   complete_financials.report_date AS financial_report_date
            FROM stock_meta m
            LEFT JOIN raw ON raw.stock_code = m.stock_code
            LEFT JOIN qfq ON qfq.stock_code = m.stock_code
            LEFT JOIN complete_financials ON complete_financials.stock_code = m.stock_code
            WHERE m.is_listed IS TRUE
        ), evidence AS (
            SELECT base.stock_code,
                   audit.report_date,
                   audit.field_name,
                   batch.data_type
            FROM base
            JOIN source_audit audit ON audit.stock_code = base.stock_code
            JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
            JOIN valid_hashes raw
              ON raw.raw_response_hash = audit.raw_response_hash
              AND (
                  (audit.field_name = 'latest_close'
                   AND audit.report_date IN (base.raw_last_date, base.qfq_last_date))
                  OR (
                      audit.field_name IN (
                          'total_assets', 'total_liabilities', 'total_equity',
                          'total_equity_parent', 'revenue', 'parent_net_profit',
                          'cf_from_operating'
                      )
                      AND audit.report_date = base.financial_report_date
                      AND batch.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                  )
                  OR (
                      batch.data_type = 'indicator_snapshot'
                      AND audit.report_date = base.financial_report_date
                  )
              )
        ), flags AS (
            SELECT base.stock_code,
                   base.listing_date,
                   base.raw_last_date,
                   base.qfq_last_date,
                   base.financial_report_date,
                   BOOL_OR(
                       evidence.data_type = 'price_daily_raw'
                       AND evidence.field_name = 'latest_close'
                       AND evidence.report_date = base.raw_last_date
                   ) AS raw_price_ok,
                   BOOL_OR(
                       evidence.data_type = 'price_daily_qfq'
                       AND evidence.field_name = 'latest_close'
                       AND evidence.report_date = base.qfq_last_date
                   ) AS qfq_price_ok,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'total_assets'
                   ) AS has_total_assets,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'total_liabilities'
                   ) AS has_total_liabilities,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'revenue'
                   ) AS has_revenue,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'parent_net_profit'
                   ) AS has_parent_net_profit,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'cf_from_operating'
                   ) AS has_cf_from_operating,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name IN ('total_equity', 'total_equity_parent')
                   ) AS has_equity,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type = 'indicator_snapshot'
                   ) AS snapshot_ok
            FROM base
            LEFT JOIN evidence ON evidence.stock_code = base.stock_code
            GROUP BY base.stock_code, base.listing_date, base.raw_last_date,
                     base.qfq_last_date, base.financial_report_date
        )
        SELECT flags.stock_code
        FROM flags
        WHERE NOT (
            COALESCE(flags.raw_price_ok, FALSE)
            AND COALESCE(flags.qfq_price_ok, FALSE)
            AND COALESCE(flags.has_total_assets, FALSE)
            AND COALESCE(flags.has_total_liabilities, FALSE)
            AND COALESCE(flags.has_revenue, FALSE)
            AND COALESCE(flags.has_parent_net_profit, FALSE)
            AND COALESCE(flags.has_cf_from_operating, FALSE)
            AND COALESCE(flags.has_equity, FALSE)
            AND COALESCE(flags.snapshot_ok, FALSE)
        )
          -- 新股（上市不足 90 天）与停牌股（最近 bar 超过 7 天前，无法抓取
          -- 新数据）的原始响应仍在积累/冻结中，属披露项，不阻断筛选。
          AND (flags.listing_date IS NULL
               OR flags.listing_date < CURRENT_DATE - INTERVAL '90 days')
          AND (flags.raw_last_date IS NULL
               OR flags.raw_last_date >= CURRENT_DATE - INTERVAL '7 days')
        ORDER BY flags.stock_code
        """
    )
    return [row["stock_code"] for row in rows]


_ARCHIVE_HASH_CACHE_TTL_SECONDS = 60.0
_archive_hash_mismatch_cache: dict[str, tuple[float, int]] = {}


def _archive_hash_mismatch_rows(
    duck: DuckDBStore, *, force_refresh: bool = False
) -> tuple[int, bool]:
    """Count archived payloads failing SHA-256 verification, cached briefly.

    Returns (mismatch_rows, cache_hit). Rows written by the adapters are
    hashed before insert and carry integrity_verified=TRUE (schema v7), so a
    cold scan only re-checks the rows that have not been marked verified yet.
    This keeps the fail-closed check while avoiding a ~40s read of all 6GB+
    of archived payloads on every process start. Failures are never cached.
    """
    cache_key = str(duck.db_path)
    now = time.monotonic()
    if not force_refresh:
        cached = _archive_hash_mismatch_cache.get(cache_key)
        if cached is not None and now - cached[0] < _ARCHIVE_HASH_CACHE_TTL_SECONDS:
            return cached[1], True
    try:
        try:
            rows = duck.read_query(
                "SELECT raw_response_hash, payload FROM raw_response_archive_all "
                "WHERE payload IS NOT NULL AND COALESCE(integrity_verified, FALSE) = FALSE"
            )
        except Exception:
            # 兼容尚无 integrity_verified 列的旧测试库/历史 schema。
            rows = duck.read_query(
                "SELECT raw_response_hash, payload FROM raw_response_archive_all WHERE payload IS NOT NULL"
            )
        mismatches = sum(
            1
            for archive in rows
            if not isinstance(archive["payload"], bytes)
            or hashlib.sha256(archive["payload"]).hexdigest() != archive["raw_response_hash"]
        )
    except Exception:
        return 1, False
    _archive_hash_mismatch_cache[cache_key] = (now, mismatches)
    return mismatches, False


# Warnings that invalidate every value read from indicator_snapshot.
_SNAPSHOT_BLOCKING_WARNINGS = frozenset({
    "FINANCIAL_SHELL_ROWS",
    "SNAPSHOT_STALE",
    "LINEAGE_INVALID",
    "CODE_IDENTITY_ALIAS",
    "LIVE_SCHEMA_INCOMPATIBLE",
})

# Warnings that invalidate only dividend-derived indicator fields.
_DIVIDEND_BLOCKING_WARNINGS = frozenset({"DIVIDEND_DATES_UNVERIFIED"})

# P3-3 修复（reports/73）：国债/股息率域字段与前端 data-quality.ts 对齐，
# DIVIDEND_DATES_UNVERIFIED 时一并遮蔽。
DIVIDEND_INDICATOR_FIELDS = (
    "consecutive_div_years",
    "cumulative_dividend_amount",
    "dividend_financing_ratio_pct",
    "dividend_yield",
    "dps",
    "payout_ratio",
    "ttm_dividend_yield",
    "div_yield_spread_0p25y",
    "div_yield_spread_0p5y",
    "div_yield_spread_1y",
    "div_yield_spread_2y",
    "div_yield_spread_3y",
    "div_yield_spread_5y",
    "div_yield_spread_7y",
    "div_yield_spread_10y",
    "div_yield_spread_30y",
)


def indicator_trust(warning_codes: list[str]) -> dict:
    """Server-authoritative trust decision for snapshot-derived indicator values.

    Mirrors the frontend isIndicatorUntrusted policy: snapshot-dependent
    warnings invalidate every indicator field, dividend warnings invalidate
    only dividend fields, and operational warnings invalidate no numeric
    field. Read-only endpoints mask the affected values instead of serving
    them as normal research data.
    """
    codes = set(warning_codes)
    return {
        "warning_codes": list(warning_codes),
        "untrusted_all": bool(codes & _SNAPSHOT_BLOCKING_WARNINGS),
        "untrusted_fields": (
            sorted(DIVIDEND_INDICATOR_FIELDS)
            if codes & _DIVIDEND_BLOCKING_WARNINGS
            else []
        ),
    }


def mask_untrusted_values(values: dict, trust: dict) -> dict:
    """Null the snapshot-derived values the server cannot vouch for (fail closed)."""
    if trust["untrusted_all"]:
        return {key: None for key in values}
    untrusted = set(trust["untrusted_fields"])
    return {
        key: (None if key in untrusted else value)
        for key, value in values.items()
    }


_WARNING_CODES_CACHE_TTL_SECONDS = 30.0
_WARNING_CODES_STATE_KEY = "warning_codes_cache"
_warning_codes_cache: dict[str, tuple[float, list[str]]] = {}
# reports/76 P1-2 增强：TTL 过期后不阻塞请求——返回 stale 结果并后台单飞重建
_warning_codes_refresh_lock = threading.Lock()
_warning_codes_refreshing: set[str] = set()


def _build_warning_codes_low_memory(duck: DuckDBStore, sqlite: SQLiteStore) -> list[str]:
    """Full quality build under the process-uniform DuckDB budget.

    The full lineage/archive scan is the only operation in this service that
    approaches the multi-GiB DuckDB limit. Connection settings are fixed by
    database.* (8GB / 2 threads / preserve_insertion_order=false on the
    formal profile). This function must NOT call memory_limit(): a different
    configuration in the same web process would make ordinary K-line/detail
    queries fail with different-configuration errors. Hot read paths never run
    this synchronously in formal mode.
    """
    return list(build_data_quality_status_cached(duck, sqlite)["warning_codes"])


def _persisted_warning_codes(sqlite: SQLiteStore) -> list[str]:
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [_WARNING_CODES_STATE_KEY],
        )
        if rows and rows[0].get("value"):
            value = json.loads(rows[0]["value"])
            if isinstance(value, list):
                return [str(item) for item in value]
    except Exception:
        pass
    return []


def _persist_warning_codes(sqlite: SQLiteStore, codes: list[str]) -> None:
    try:
        with sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [_WARNING_CODES_STATE_KEY, json.dumps(codes, ensure_ascii=False),
                 datetime.now(UTC).isoformat()],
            )
    except Exception:
        logger.warning("持久化 warning_codes 缓存失败", exc_info=True)


def read_warning_codes(duck: DuckDBStore, sqlite: SQLiteStore) -> list[str]:
    """Return quality warning codes for read-only display, cached briefly.

    The full status build re-verifies readiness across the whole pool; hot
    read paths (stock indicators, watchlist) reuse the process-local result
    instead of rebuilding it per request. Fail closed: a build failure
    reports LINEAGE_INVALID, and failures are never cached.

    Write-lock awareness (reports/76 P1-2): while the auto-update writer
    holds the DuckDB file, the full build (3,280 万行 source_audit 扫描 +
    全市场一致性子查询) would take 40~60s or fail with file-open races.
    During that window we serve the last idle-time result when present and
    otherwise skip the rebuild entirely (return []); callers detect the
    window via update_lock_active and label responses with
    auto_update_in_progress instead of blocking. Integrity is preserved:
    screening is separately gated, and the next idle build restores exact
    codes within one TTL.

    Stale-while-revalidate: an expired cache returns the previous result
    immediately and rebuilds in a single-flight background thread, so hot
    paths never wait for a 20~60s full-universe scan.
    """
    cache_key = f"{duck.db_path}|{sqlite.db_path}"
    now = time.monotonic()
    cached = _warning_codes_cache.get(cache_key)
    if cached is not None and now - cached[0] < _WARNING_CODES_CACHE_TTL_SECONDS:
        return list(cached[1])
    try:
        from app.core.storage.update_lock import update_lock_active

        if update_lock_active(duck.db_path):
            return list(cached[1]) if cached is not None else _persisted_warning_codes(sqlite)
    except Exception:
        pass
    if cached is not None:
        _ensure_warning_codes_refresh(duck, sqlite, cache_key)
        return list(cached[1])
    # S1 测试进程需要确定性同步结果；正式服务第一次请求绝不执行 40-80s
    # 的全库扫描——用持久化缓存兜底并触发后台单飞刷新。
    if os.environ.get("VD_ENV") == "test":
        try:
            warning_codes = _build_warning_codes_low_memory(duck, sqlite)
        except Exception:
            return ["LINEAGE_INVALID"]
        _warning_codes_cache[cache_key] = (now, warning_codes)
        return list(warning_codes)
    persisted = _persisted_warning_codes(sqlite)
    _ensure_warning_codes_refresh(duck, sqlite, cache_key)
    return persisted


def _ensure_warning_codes_refresh(duck: DuckDBStore, sqlite: SQLiteStore, key: str) -> None:
    """Start a single-flight background rebuild; never blocks the request."""
    with _warning_codes_refresh_lock:
        if key in _warning_codes_refreshing:
            return
        _warning_codes_refreshing.add(key)
    threading.Thread(
        target=_warning_codes_refresh_worker,
        args=(duck, sqlite, key),
        name="vd-warning-codes-refresh",
        daemon=True,
    ).start()


def _warning_codes_refresh_worker(duck: DuckDBStore, sqlite: SQLiteStore, key: str) -> None:
    try:
        from app.core.storage.update_lock import update_lock_active

        if update_lock_active(duck.db_path):
            return
        warning_codes = _build_warning_codes_low_memory(duck, sqlite)
        _warning_codes_cache[key] = (time.monotonic(), warning_codes)
        _persist_warning_codes(sqlite, warning_codes)
    except Exception as error:
        logger.warning("后台 warning codes 刷新失败: %s", error)
    finally:
        with _warning_codes_refresh_lock:
            _warning_codes_refreshing.discard(key)


def build_data_quality_status(
    duck: DuckDBStore, sqlite: SQLiteStore, *, force_archive_hash_recheck: bool = False
) -> dict:
    """Return machine-readable freshness, completeness, lineage, and operation warnings."""
    readiness = minimum_data_readiness(duck, sqlite)
    if not readiness["schema_compatibility"]["compatible"]:
        return _incompatible_schema_status(readiness, sqlite)

    dates = _data_dates(duck)
    dividends = duck.read_query(
        """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND ex_date >= DATE '2005-01-01'
                  AND (
                    COALESCE(dividend_per_share, 0) != 0
                    OR COALESCE(stock_dividend, 0) != 0
                    OR COALESCE(transfer_share, 0) != 0
                    OR COALESCE(rights_issue, 0) != 0
                  )
            ) AS active_missing_announcement_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND ex_date < DATE '2005-01-01'
                  AND (
                    COALESCE(dividend_per_share, 0) != 0
                    OR COALESCE(stock_dividend, 0) != 0
                    OR COALESCE(transfer_share, 0) != 0
                    OR COALESCE(rights_issue, 0) != 0
                  )
            ) AS legacy_missing_announcement_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND (
                      (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
                      OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
                  )
            ) AS unverified_period_end_rows
        FROM dividends
        """
    )[0]
    try:
        from app.core.source_audit_archive import read_archive_state
        source_audit_archive_state = read_archive_state(sqlite) or {
            "archived_rows": 0, "note": "not_archived_yet",
        }
    except Exception:
        source_audit_archive_state = {"archived_rows": 0, "note": "unavailable"}
    lineage = duck.read_query(
        """
        WITH valid_hashes AS (
            SELECT raw_response_hash FROM raw_response_archive_valid_hash
            UNION ALL
            -- 兼容旧测试/维护脚本直接 INSERT raw_response_archive 的路径；
            -- 只回退扫描小型 active 热表，绝不回退到 26GB 的 history 大表。
            SELECT a.raw_response_hash FROM raw_response_archive a
            WHERE a.payload IS NOT NULL
              AND OCTET_LENGTH(a.payload) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM raw_response_archive_valid_hash v
                  WHERE v.raw_response_hash = a.raw_response_hash
              )
        ), batch_hash AS (
            -- 2026-11-12 契约 §3 扩展：归档缺口必须区分「真缺口」与「历史事实」。
            -- 判据只有一条：**摘要形态**决定它是不是 legacy。
            --   现行溯源约定是 64 位内容寻址 SHA256；**32 位十六进制字节串本身
            --   就是 MD5 时代摘要的形态，与哪个适配器写的无关**。
            --   现代批次（64 位 SHA256）：摘要即内容寻址，缺原始字节归档 =
            --   真 lineage 断裂 → 计入 batch_archive_gap_rows → LINEAGE_INVALID 阻断。
            --   legacy 批次（32 位 MD5 形态，2026-09-19~20 那批历史回填）：导入时
            --   从未落原始字节，这是已由 STATUS 披露的历史事实，不是今天的数据损坏
            --   → 只计入 legacy_archive_gap_batches 如实披露，不得触发 LINEAGE_INVALID。
            -- 2026-11-12 二次裁决：曾用 `adapter_version LIKE 'csmar-%'` 白名单保守兜底，
            -- 被正式库证伪——同一战役里还有 6 个东财批次（eastmoney-f10-xjllb/1、
            -- eastmoney-f10-zcfzb/1、em-datacenter-mainfinadata），32 位 MD5 且无归档。
            -- 白名单会把它们误判成 lineage 断裂，故收敛为纯形态判定（唯一表达式，
            -- source_audit 侧与 fetch_batch 侧共用同一口径）。
            SELECT b.batch_id,
                   b.raw_response_hash,
                   COALESCE(
                       LENGTH(b.raw_response_hash) = 32
                       AND regexp_matches(lower(b.raw_response_hash), '^[0-9a-f]{32}$'),
                       FALSE
                   ) AS is_legacy_digest,
                   NOT EXISTS (
                       SELECT 1 FROM valid_hashes archive
                       WHERE archive.raw_response_hash = b.raw_response_hash
                   ) AS archive_gap
            FROM fetch_batch b
        ), audit_hash AS (
            -- legacy 形态只看该行自身的摘要字节串，与它属于哪个批次无关。
            SELECT s.raw_response_hash,
                   LENGTH(s.raw_response_hash) AS hash_len,
                   COALESCE(
                       LENGTH(s.raw_response_hash) = 32
                       AND regexp_matches(lower(s.raw_response_hash), '^[0-9a-f]{32}$'),
                       FALSE
                   ) AS is_legacy_digest
            FROM source_audit s
        )
        SELECT
            (SELECT COUNT(*) FROM audit_hash
             WHERE hash_len != 64 AND NOT is_legacy_digest) AS invalid_hash_rows,
            (SELECT COUNT(*) FROM audit_hash WHERE is_legacy_digest) AS legacy_hash_rows,
            (SELECT COUNT(*) FROM source_audit s
             WHERE s.fetch_batch_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM fetch_batch f WHERE f.batch_id = s.fetch_batch_id
                )) AS orphan_batch_rows,
            (SELECT COUNT(*) FROM source_audit s
             WHERE LENGTH(s.raw_response_hash) = 64
               AND NOT EXISTS (
                   SELECT 1 FROM valid_hashes archive
                   WHERE archive.raw_response_hash = s.raw_response_hash
               )) AS audit_archive_gap_rows,
            (SELECT COUNT(*) FROM source_audit s
             WHERE EXISTS (
                 -- 2026-10-30：active 热表直接检查空 payload。不能用
                 -- raw_response_archive_all 的 storage 列：轮转/重建后的
                 -- 合并视图不保证带 storage，曾导致 summary 每 5 分钟 503。
                 SELECT 1 FROM raw_response_archive archive
                 WHERE archive.raw_response_hash = s.raw_response_hash
                   AND (archive.payload IS NULL OR OCTET_LENGTH(archive.payload) = 0)
             )) AS empty_archive_payload_rows,
            (SELECT COUNT(*) FROM batch_hash
             WHERE archive_gap AND NOT is_legacy_digest) AS batch_archive_gap_rows,
            (SELECT COUNT(*) FROM batch_hash
             WHERE archive_gap AND is_legacy_digest) AS legacy_archive_gap_batches
        """
    )[0]
    lineage["archive_gap_rows"] = (
        lineage["audit_archive_gap_rows"]
        + lineage["batch_archive_gap_rows"]
        + lineage["empty_archive_payload_rows"]
    )
    # 注：legacy_archive_gap_batches（32 位 MD5 摘要的历史回填批次从未落原始字节，
    # 与适配器无关）故意不计入 archive_gap_rows —— 它是已披露的历史事实，不是
    # 今天的数据损坏。真缺口（64 位内容寻址批次缺归档）仍走 batch_archive_gap_rows
    # → LINEAGE_INVALID 阻断。
    lineage["source_audit_archive"] = source_audit_archive_state
    # integrity_verified 标记由写路径在插入前校验；冷启动只重算未标记行，
    # 避免把 6GB+ 归档 BLOB 全部读回。未标记行仍逐条验证，验证失败继续
    # 报 LINEAGE_INVALID（fail closed）。
    lineage["hash_mismatch_rows"], hash_check_cached = _archive_hash_mismatch_rows(
        duck, force_refresh=force_archive_hash_recheck
    )
    lineage["archive_hash_check"] = {
        "cached": hash_check_cached,
        "ttl_seconds": int(_ARCHIVE_HASH_CACHE_TTL_SECONDS),
    }
    code_identity = duck.read_query(
        """
        WITH listed_codes AS (
            SELECT stock_code,
                   regexp_replace(upper(trim(stock_code)), '^(SH|SZ|BJ)|\\.(SH|SS|SZ|BJ)$', '', 'g') AS normalized_code
            FROM stock_meta WHERE is_listed IS TRUE
        )
        SELECT COUNT(DISTINCT raw.stock_code) AS raw_alias_rows
        FROM price_daily_raw raw
        JOIN listed_codes meta
          ON regexp_replace(upper(trim(raw.stock_code)), '^(SH|SZ|BJ)|\\.(SH|SS|SZ|BJ)$', '', 'g') = meta.normalized_code
         AND raw.stock_code != meta.stock_code
        """
    )[0]
    operations = {
        "unpublished_overrides": sqlite.query(
            """
            SELECT COUNT(*) AS count FROM manual_overrides
            WHERE status != 'published' AND rolled_back_at IS NULL
            """
        )[0]["count"],
        # 仅当 running job 悬挂（超过 2 小时未结束）才算 stale：
        # 正常的自动更新运行时 job 处于 running 是预期状态，
        # 若一律计数会误报 STALE_RUNNING_JOBS 警告。
        "running_jobs": sqlite.query(
            """SELECT COUNT(*) AS count FROM job_logs
               WHERE status = 'running' AND started_at < ?""",
            [(datetime.now(UTC) - timedelta(hours=2)).isoformat()],
        )[0]["count"],
    }

    warning_codes: list[str] = []
    balance_dates = dates["balance_sheet"]
    income_dates = dates["income_statement"]
    if (
        balance_dates["latest_record"] != balance_dates["latest_complete"]
        or income_dates["latest_record"] != income_dates["latest_complete"]
    ):
        warning_codes.append("FINANCIAL_SHELL_ROWS")
    if _is_stale(dates["price"], dates["indicator_snapshot"]["latest_price_date"], days=7):
        warning_codes.append("SNAPSHOT_STALE")
    if dividends["active_missing_announcement_rows"]:
        warning_codes.append("DIVIDEND_DATES_UNVERIFIED")
    if (
        lineage["invalid_hash_rows"]
        or lineage["orphan_batch_rows"]
        or lineage["archive_gap_rows"]
        or lineage["hash_mismatch_rows"]
    ):
        warning_codes.append("LINEAGE_INVALID")
    if code_identity["raw_alias_rows"]:
        warning_codes.append("CODE_IDENTITY_ALIAS")
    if operations["unpublished_overrides"]:
        warning_codes.append("UNPUBLISHED_OVERRIDES")
    if operations["running_jobs"]:
        warning_codes.append("STALE_RUNNING_JOBS")
    if not readiness["ready"]:
        warning_codes.append("MINIMUM_DATA_NOT_READY")

    # 2026-11-14（reports/119）：把人工审查结论固化成的 12 条资产健康规则挂进
    # 同一个状态载荷。这里只跑**廉价规则**（正式库实测 <2s），贵重规则（归档
    # 交叉核验 ~31s）留给 `vd data doctor`；预算耗尽/未启用的规则在 asset_health
    # 里如实标 skipped，不会被静默丢弃。
    #
    # 纪律：本模块的 code **不进** `_SNAPSHOT_BLOCKING_WARNINGS` /
    # `_SCREENING_BLOCKING_WARNINGS` —— 它们只披露，不遮蔽指标、不让筛选 409。
    # 升级为阻断型必须另开裁决。
    asset_health = _asset_health_summary(duck, sqlite)
    for code in asset_health.get("p1_codes", []):
        if code not in warning_codes:
            warning_codes.append(code)

    return {
        "minimum_data_readiness": readiness,
        "dates": dates,
        "dividends": dividends,
        "lineage": lineage,
        "code_identity": code_identity,
        "operations": operations,
        "warning_codes": warning_codes,
        "asset_health": asset_health,
    }


def _asset_health_summary(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Run the cheap asset-health rules for the status payload (never raises).

    体检本身坏掉时返回一个显式不可用的摘要，而不是让整个数据状态页 503：
    状态页的主要价值（日期/lineage/warning_codes）不应被新加的披露项拖垮。
    """
    try:
        from app.core.quality.asset_health import run_asset_health, summarize_asset_health

        payload = run_asset_health(duck, sqlite, allow_expensive=False)
        return summarize_asset_health(payload)
    except Exception:
        logger.warning("资产健康检查失败（数据状态降级，不阻断）", exc_info=True)
        return {
            "generated_at": None,
            "run_id": None,
            "level_counts": {},
            "code_counts": {},
            "p1_codes": [],
            "finding_count": 0,
            "findings": [],
            "truncated": 0,
            "rules": [],
            "skipped": [],
            "errors": [{"code": "ASSET_HEALTH_UNAVAILABLE", "error": "status build failed"}],
            "elapsed_ms": None,
        }


def _live_schema_compatibility(duck: DuckDBStore) -> dict:
    """Report whether the live analytical schema can support the quality contract."""
    required = {
        "stock_meta": {"stock_code", "exchange", "listing_date", "is_listed", "is_st", "is_suspended"},
        "price_daily_raw": {"stock_code", "trade_date", "close"},
        "price_daily_qfq": {"stock_code", "trade_date", "close"},
        "balance_sheet": {"stock_code", "report_date", "total_assets", "total_liabilities", "total_equity", "total_equity_parent"},
        "income_statement": {"stock_code", "report_date", "revenue", "parent_net_profit"},
        "cash_flow": {"stock_code", "report_date", "cf_from_operating"},
        "indicator_snapshot": {"stock_code", "report_date", "latest_close", "latest_price_date", "calculated_at"},
        "dividends": {"stock_code", "announcement_date", "dividend_per_share", "stock_dividend", "transfer_share", "rights_issue"},
        "source_audit": {"stock_code", "fetch_batch_id", "raw_response_hash"},
        "fetch_batch": {"batch_id"},
        "raw_response_archive": {"raw_response_hash"},
    }
    rows = duck.read_query(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'"
    )
    available: dict[str, set[str]] = {}
    for row in rows:
        available.setdefault(row["table_name"], set()).add(row["column_name"])
    missing = [
        f"{table}.{column}"
        for table, columns in required.items()
        for column in sorted(columns - available.get(table, set()))
    ]
    return {"compatible": not missing, "missing": missing}


def _published_override_stale_snapshot_codes(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> list[str]:
    """Return stocks whose materialized snapshot predates a published correction."""
    overrides = sqlite.query(
        """SELECT DISTINCT stock_code FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL"""
    )
    if not overrides:
        return []
    codes = [row["stock_code"] for row in overrides]
    slots = ", ".join("?" for _ in codes)
    snapshots = duck.read_query(
        f"""SELECT stock_code, MAX(calculated_at) AS calculated_at
             FROM indicator_snapshot WHERE stock_code IN ({slots}) GROUP BY stock_code""",
        codes,
    )
    snapshot_by_code = {row["stock_code"]: row["calculated_at"] for row in snapshots}
    stale: list[str] = []
    for override in sqlite.query(
        """SELECT stock_code, MAX(created_at) AS published_at FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL GROUP BY stock_code"""
    ):
        calculated_at = snapshot_by_code.get(override["stock_code"])
        if calculated_at is None or _as_datetime(override["published_at"]) > _as_datetime(calculated_at):
            stale.append(override["stock_code"])
    return stale


def _as_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _incompatible_schema_status(readiness: dict, sqlite: SQLiteStore) -> dict:
    """Keep the status API truthful instead of querying a known-incompatible database."""
    operations = {
        "unpublished_overrides": sqlite.query(
            "SELECT COUNT(*) AS count FROM manual_overrides WHERE status != 'published' AND rolled_back_at IS NULL"
        )[0]["count"],
        "running_jobs": sqlite.query(
            "SELECT COUNT(*) AS count FROM job_logs "
            "WHERE status = 'running' AND started_at < ?",
            [(datetime.now(UTC) - timedelta(hours=2)).isoformat()],
        )[0]["count"],
    }
    return {
        "minimum_data_readiness": readiness,
        "dates": {
            "price": None,
            "balance_sheet": {"latest_record": None, "latest_complete": None},
            "income_statement": {"latest_record": None, "latest_complete": None},
            "cash_flow": {"latest_record": None, "latest_complete": None},
            "indicator_snapshot": {"latest_complete": None, "calculated_at": None, "latest_price_date": None},
        },
        "dividends": {"total_rows": 0, "active_missing_announcement_rows": 0, "unverified_period_end_rows": 0},
        "lineage": {
            "invalid_hash_rows": 0, "orphan_batch_rows": 0,
            "audit_archive_gap_rows": 0, "batch_archive_gap_rows": 0, "archive_gap_rows": 0,
            # 2026-11-12：legacy 摘要披露项，schema 不兼容时同样给 0，保持载荷形状一致。
            "legacy_hash_rows": 0, "legacy_archive_gap_batches": 0,
        },
        "code_identity": {"raw_alias_rows": 0},
        "operations": operations,
        "warning_codes": ["LIVE_SCHEMA_INCOMPATIBLE", "MINIMUM_DATA_NOT_READY"],
        # 载荷形状与正常构建保持一致：live schema 不兼容时体检无法进行，
        # 如实披露 unavailable，而不是留一个键让前端判断 undefined。
        "asset_health": {
            "generated_at": None,
            "run_id": None,
            "level_counts": {},
            "code_counts": {},
            "p1_codes": [],
            "finding_count": 0,
            "findings": [],
            "truncated": 0,
            "rules": [],
            "skipped": [{"code": "ALL", "reason": "live_schema_incompatible"}],
            "errors": [],
            "elapsed_ms": None,
        },
    }


def _data_dates(duck: DuckDBStore) -> dict:
    price = duck.read_query("SELECT MAX(trade_date) AS value FROM price_daily_raw")[0]["value"]
    balance = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (
                WHERE total_assets IS NOT NULL
                  AND total_liabilities IS NOT NULL
                  AND COALESCE(total_equity_parent, total_equity) IS NOT NULL
            ) AS latest_complete
        FROM balance_sheet
        """
    )[0]
    income = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (
                WHERE revenue IS NOT NULL AND parent_net_profit IS NOT NULL
            ) AS latest_complete
        FROM income_statement
        """
    )[0]
    cash_flow = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (WHERE cf_from_operating IS NOT NULL) AS latest_complete
        FROM cash_flow
        """
    )[0]
    snapshot_columns = duck.read_query(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'indicator_snapshot'
        """
    )
    if "calculated_at" in {row["column_name"] for row in snapshot_columns}:
        snapshot = duck.read_query(
            """
            SELECT
                MAX(report_date) AS latest_complete,
                MAX(calculated_at) AS calculated_at,
                MAX(latest_price_date) AS latest_price_date
            FROM indicator_snapshot
            """
        )[0]
    else:
        snapshot = {
            "latest_complete": duck.read_query(
                "SELECT MAX(report_date) AS value FROM indicator_snapshot"
            )[0]["value"],
            "calculated_at": None,
            "latest_price_date": None,
        }
    return {
        "price": _date_string(price),
        "balance_sheet": _stringify_dates(balance),
        "income_statement": _stringify_dates(income),
        "cash_flow": _stringify_dates(cash_flow),
        "indicator_snapshot": _stringify_dates(snapshot),
    }


def _stringify_dates(values: dict) -> dict:
    return {key: _date_string(value) for key, value in values.items()}


def _date_string(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _is_stale(latest: str | None, earlier: str | None, days: int = 365) -> bool:
    if latest is None or earlier is None:
        return True
    latest_date = date.fromisoformat(latest[:10])
    earlier_date = date.fromisoformat(earlier[:10])
    return (latest_date - earlier_date).days > days
