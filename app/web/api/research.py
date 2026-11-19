"""研究域只读 API（reports/119 §5 D 类，2026-11-14）

补的是两个「**有数据、无出口**」的能力：

1. **时点可见性（as reported / point-in-time）** —— `financial_report_dates`
   369,648 条季度精度公布日此前只有 CLI（`vd data report-dates`）可用，
   用户在 Web 上无法回答「2024-06-30 那天，我能看到的最新一期财报是哪一期、
   数字是多少」。本模块把 `app/core/point_in_time.py` 的判定能力暴露为只读 API。
2. **公告时间线** —— `announcement_registry`（SQLite，22,179 条 / 5,619 只）
   此前只被增量更新用来触发财务刷新，用户看不到「这只票最近发了什么公告」。

三件事必须如实（不得夸大）
--------------------------
- **时点可见只决定「取哪一期」，不还原「当时的数值」**：库里的
  income/balance/cash_flow 每个报告期只存一行（今天的最新重述值），没有历史版本。
  因此 `value_basis` 恒为 `latest_restated`，响应里带 `disclaimer`。
- **可见日取该报告期最早登记的公布日**（同报告期多来源时取 MIN）——回测要的是
  「我第一次能看到它」的日期；未登记公布日的报告期不参与判定，不做推断。
- **公告类别**来自 `app.core.update.classify_announcement`（只 import，不修改），
  只有 financial / dividend / other 三档，是按标题关键词的**内部触发分类**，
  不是交易所一级分类。

边界与已知缺口
--------------
- `announcement_registry` 的 schema 只有 `announcement_id` 主键，**没有**
  `(stock_code, announcement_time)` 索引；`app/core/storage/schema.py` 本轮禁改，
  所以公告查询是按 stock_code 等值的**全表扫描**（22,179 行，实测毫秒级）。
  SQL 已按 `(stock_code, 时间下界)` 的可用索引形态书写，索引补上即会走索引。
- 本模块全部 GET，只读；连接用 `SQLiteStore.connection()`（`mode=ro` + `query_only`）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request

from app.core.financial_period import CN_TZ, cn_today
from app.core.point_in_time import (
    PIT_LIMITATIONS,
    ReportVisibility,
    as_reported_period,
    financial_field_catalog,
    financial_values,
    visible_periods,
)
from app.core.storage.duckdb_store import DuckDBReadLockedError
from app.web.api._ttl_cache import TTLCache
from app.web.api.blacklist import require_visible_stock

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/research", tags=["research"])

#: `fields` 白名单所在的三张财务表（与 `point_in_time.FINANCIAL_VALUE_TABLES` 同源）
DEFAULT_AS_OF_FIELDS: tuple[str, ...] = (
    "revenue",
    "parent_net_profit",
    "total_assets",
    "total_equity_parent",
    "cf_from_operating",
)

MAX_ANNOUNCEMENT_LIMIT = 500
MAX_ANNOUNCEMENT_OFFSET = 100_000
MAX_DAYS = 3_650
#: 汇总接口单次最多解析的公告行数（全表 22,179 行，留足余量并显式披露截断）
SUMMARY_SCAN_LIMIT = 200_000
SUMMARY_CACHE_TTL_SECONDS = 300.0

_STOCK_CODE_PATTERN = re.compile(r"\d{6}")
_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_FREQUENCIES = ("any", "annual", "interim", "quarterly")

_ANNOUNCEMENT_CATEGORIES = ("financial", "dividend", "other")

#: 「当时可见」数值口径的固定免责声明（与 PIT_LIMITATIONS 一起进响应）
VALUE_BASIS = "latest_restated"
VALUE_DISCLAIMER = (
    "数值取自库内该报告期唯一一份记录（今天的最新重述值），"
    "不是当年披露时的原始数字；可见性只决定「取哪一期」，不还原历史数值版本"
)
ANNOUNCEMENT_DISCLAIMER = (
    "只包含已登记进 announcement_registry 的公告（增量更新时按 CNINFO 定期报告/"
    "权益分派类目差分登记），不是全市场全量公告库；category 是按标题关键词的"
    "内部触发分类（financial/dividend/other），不是交易所一级分类"
)

_summary_cache = TTLCache(SUMMARY_CACHE_TTL_SECONDS)
_classifier: Any = None


# ─── 参数校验（全部 400 + reason，不把用户输入拼进 SQL）────────────────


def _require_visible(request: Request, stock_code: str) -> None:
    """黑名单股票在研究域一律 404。

    直接复用 `app/web/api/blacklist.py::require_visible_stock` 的同一个实现
    （同为「判黑名单 → 404 + 结构化 detail」），不在这里再写一份判定，
    否则两处错一个就会出现「详情页屏蔽了、公告照样能翻」的漏洞（`AGENTS R1`）。
    """
    require_visible_stock(request, stock_code)


def _parse_stock_code(code: str) -> str:
    text = (code or "").strip()
    if not _STOCK_CODE_PATTERN.fullmatch(text):
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_stock_code", "message": "code 必须是 6 位数字股票代码"},
        )
    return text


def _parse_iso_date(value: str, *, field: str) -> date:
    text = (value or "").strip()
    if not _ISO_DATE_PATTERN.fullmatch(text):
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_date", "message": f"{field} 必须是 YYYY-MM-DD"},
        )
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_date", "message": f"{field} 不是合法日期: {text}"},
        ) from error


def _parse_frequency(frequency: str) -> str:
    text = (frequency or "any").strip().lower()
    if text not in _FREQUENCIES:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "invalid_frequency",
                "message": f"frequency 只接受 {'/'.join(_FREQUENCIES)}",
            },
        )
    return text


def _parse_fields(raw: str | None) -> tuple[str, ...]:
    """解析 `fields=a,b,c`：去空白、去重保序。

    只做**语法**校验（白名单要读一次 information_schema 才能知道，见
    `_ensure_fields_allowed`）——这样明显的参数错误不必先碰数据库。
    """
    requested: list[str] = []
    for item in (raw or "").split(","):
        name = item.strip()
        if name and name not in requested:
            requested.append(name)
    if not requested:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "fields_required",
                "message": "fields 不能为空，例如 fields=revenue,parent_net_profit",
                "examples": list(DEFAULT_AS_OF_FIELDS),
            },
        )
    return tuple(requested)


def _ensure_fields_allowed(requested: tuple[str, ...], allowed: dict[str, str]) -> None:
    """白名单外的字段一律 400（绝不把用户输入的列名拼进 SQL）。"""
    rejected = [name for name in requested if name not in allowed]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "field_not_allowed",
                "message": (
                    "字段不在 income_statement / balance_sheet / cash_flow 标准列白名单内"
                ),
                "rejected_fields": rejected,
                "allowed_field_count": len(allowed),
                "examples": list(DEFAULT_AS_OF_FIELDS),
            },
        )


@contextmanager
def _read_guard(endpoint: str) -> Iterator[None]:
    """把数据库层异常转成结构化 5xx，不让 traceback 裸奔。"""
    try:
        yield
    except HTTPException:
        raise
    except DuckDBReadLockedError as error:
        logger.warning("%s: DuckDB 被外部写进程占用: %s", endpoint, error)
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "duckdb_read_locked",
                "message": "数据正在更新中，请稍后重试",
            },
        ) from error
    except (duckdb.Error, OSError) as error:
        logger.warning("%s: DuckDB 读取失败: %s", endpoint, error)
        raise HTTPException(
            status_code=503,
            detail={"reason": "database_unavailable", "message": f"DuckDB 读取失败: {error}"},
        ) from error
    except Exception as error:  # SQLite / 解析 / 其它
        logger.exception("%s: 查询失败", endpoint)
        raise HTTPException(
            status_code=500,
            detail={
                "reason": "query_failed",
                "message": f"{type(error).__name__}: {error}",
            },
        ) from error


# ─── 时点可见性 ────────────────────────────────────────────────────────


def _period_payload(period: ReportVisibility, as_of: date) -> dict[str, Any]:
    return {
        "report_date": period.report_date.isoformat(),
        "announce_date": period.announce_date.isoformat(),
        "report_type": period.report_type,
        "source": period.source,
        "days_since_announce": (as_of - period.announce_date).days,
    }


@router.get("/as-reported")
def get_as_reported(
    request: Request,
    code: str = Query(..., description="6 位股票代码"),
    as_of: str = Query(..., description="观察日 YYYY-MM-DD（在该日收盘时能看到什么）"),
    fields: str | None = Query(
        None,
        description="逗号分隔的字段白名单，如 revenue,parent_net_profit",
    ),
    frequency: str = Query("any", description="any/annual/interim/quarterly"),
) -> dict[str, Any]:
    """`as_of` 当天可见的最近一期财务值（「当时可见」口径）。

    可见性判定：`financial_report_dates.announce_date <= as_of`，与
    `app/core/point_in_time.py` 同一套规则；未登记公布日的报告期不参与判定。

    - 404 `no_announce_date_records`：该股没有任何公布日记录（不推断）；
    - 404 `no_visible_period_before_as_of`：有记录但都晚于 `as_of`（附最早公布日）；
    - 404 `visible_period_has_no_financials`：可见期已定位，但三表都没有该期数据；
    - 200 但个别字段为 null：该期有行、该字段本身空值，`missing_fields` 会列出。
    """
    stock_code = _parse_stock_code(code)
    _require_visible(request, stock_code)
    as_of_date = _parse_iso_date(as_of, field="as_of")
    freq = _parse_frequency(frequency)
    wanted = _parse_fields(fields)
    duck = request.app.state.duck

    with _read_guard("research.as_reported"):
        _ensure_fields_allowed(wanted, financial_field_catalog(duck))

    with _read_guard("research.as_reported"):
        period = as_reported_period(duck, stock_code, as_of_date, frequency=freq)

    if period is None:
        with _read_guard("research.as_reported"):
            history = visible_periods(duck, stock_code, frequency=freq)
        if not history:
            raise HTTPException(
                status_code=404,
                detail={
                    "reason": "no_announce_date_records",
                    "message": (
                        "该股票没有公布日记录（financial_report_dates 未登记即不推断可见性）"
                    ),
                    "stock_code": stock_code,
                    "as_of": as_of_date.isoformat(),
                },
            )
        earliest = min(item.announce_date for item in history)
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "no_visible_period_before_as_of",
                "message": "as_of 早于该股最早登记的公布日，当天什么都还看不到",
                "stock_code": stock_code,
                "as_of": as_of_date.isoformat(),
                "earliest_announce_date": earliest.isoformat(),
                "earliest_report_date": min(item.report_date for item in history).isoformat(),
            },
        )

    with _read_guard("research.as_reported"):
        loaded = financial_values(duck, stock_code, period.report_date, wanted)

    if not loaded.tables:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "visible_period_has_no_financials",
                "message": (
                    "可见报告期已定位，但 income_statement/balance_sheet/cash_flow "
                    "都没有该期数据"
                ),
                "stock_code": stock_code,
                "report_date": period.report_date.isoformat(),
                "announce_date": period.announce_date.isoformat(),
            },
        )

    payload = _period_payload(period, as_of_date)
    payload.update(
        {
            "stock_code": stock_code,
            "as_of": as_of_date.isoformat(),
            "frequency": freq,
            "fields": loaded.values,
            "missing_fields": list(loaded.missing_fields),
            "value_tables": list(loaded.tables),
            "value_basis": VALUE_BASIS,
            "value_source": (
                "financial_report_dates.announce_date 定位报告期 + "
                "income_statement/balance_sheet/cash_flow 取数"
            ),
            "disclaimer": VALUE_DISCLAIMER,
            "limitations": list(PIT_LIMITATIONS),
        }
    )
    return payload


@router.get("/visible-periods")
def get_visible_periods(
    request: Request,
    code: str = Query(..., description="6 位股票代码"),
    as_of: str | None = Query(None, description="可选：只看该日已可见的期（YYYY-MM-DD）"),
    frequency: str = Query("any", description="any/annual/interim/quarterly"),
    limit: int | None = Query(None, ge=1, le=1000, description="可选：只取最新 N 期"),
) -> dict[str, Any]:
    """每个报告期「什么日期开始可见」的清单（回测对齐用，按 report_date 升序）。

    可见日 = 该报告期**最早**登记的公布日（同报告期多来源取 MIN）；
    回测时应在 `announce_date` 之后再使用该期数字，之前仍用上一期。
    """
    stock_code = _parse_stock_code(code)
    _require_visible(request, stock_code)
    freq = _parse_frequency(frequency)
    as_of_date = _parse_iso_date(as_of, field="as_of") if as_of else None
    duck = request.app.state.duck

    with _read_guard("research.visible_periods"):
        periods = visible_periods(
            duck, stock_code, as_of=as_of_date, frequency=freq, limit=limit
        )

    if not periods:
        all_periods: list[ReportVisibility] = []
        if as_of_date is not None:
            with _read_guard("research.visible_periods"):
                all_periods = visible_periods(duck, stock_code, frequency=freq)
        reason = (
            "no_visible_period_before_as_of" if all_periods else "no_announce_date_records"
        )
        detail: dict[str, Any] = {
            "reason": reason,
            "message": (
                "该股票没有公布日记录（未登记即不推断可见性）"
                if not all_periods
                else "as_of 之前没有任何已可见的报告期"
            ),
            "stock_code": stock_code,
        }
        if as_of_date is not None:
            detail["as_of"] = as_of_date.isoformat()
        if all_periods:
            detail["earliest_announce_date"] = min(
                item.announce_date for item in all_periods
            ).isoformat()
        raise HTTPException(status_code=404, detail=detail)

    return {
        "stock_code": stock_code,
        "frequency": freq,
        "as_of": as_of_date.isoformat() if as_of_date else None,
        "count": len(periods),
        "periods": [
            {
                "report_date": item.report_date.isoformat(),
                "announce_date": item.announce_date.isoformat(),
                "report_type": item.report_type,
                "source": item.source,
            }
            for item in periods
        ],
        "usage": "回测时 announce_date 之后才允许使用该期数字，之前仍应使用上一期",
        "limitations": list(PIT_LIMITATIONS),
    }


# ─── 公告时间线 ────────────────────────────────────────────────────────


def _classify(title: str | None) -> str:
    """委托给 `app.core.update.classify_announcement`（只 import，不修改）。

    延迟导入并把函数对象缓存起来：`app.core.update` 会连带拉起适配器/数据质量/
    补采域等重模块（≈0.25s），没必要让每个 Web 进程在启动时就付这笔钱。
    """
    global _classifier
    if _classifier is None:
        from app.core.update import classify_announcement

        _classifier = classify_announcement
    return str(_classifier(title))


def _parse_announcement_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # 适配器存的是 UTC（CNINFO announceTime 毫秒时间戳转来的），历史脏数据可能无时区
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _load_announcement_rows(sqlite: Any, stock_code: str, cutoff: date) -> list[dict[str, Any]]:
    """按股票取公告行（SQL 粗筛 + Python 按北京时间自然日精确复核）。

    schema 里 announcement_registry 只有 announcement_id 主键，没有
    (stock_code, announcement_time) 索引；SQL 仍按「可走索引」的等值+范围形态写，
    索引一旦补上即自动生效。粗筛下界取 cutoff-1 天：UTC 前一日 16:00 起即属
    北京时间 cutoff 当天，单按 UTC 日期前缀筛会漏掉北京清晨的公告。
    """
    coarse = (cutoff - timedelta(days=1)).isoformat()
    return sqlite.query(
        """SELECT announcement_id, stock_code, announcement_time, title
           FROM announcement_registry
           WHERE stock_code = ?
             AND announcement_time >= ?
           ORDER BY announcement_time DESC""",
        [stock_code, coarse],
    )


def _in_window(row: dict[str, Any], cutoff: date) -> tuple[datetime | None, date | None]:
    published = _parse_announcement_datetime(row.get("announcement_time"))
    if published is None:
        return None, None
    return published, published.astimezone(CN_TZ).date()


@router.get("/announcements")
def get_announcements(
    request: Request,
    code: str = Query(..., description="6 位股票代码"),
    days: int = Query(365, ge=1, le=MAX_DAYS, description="最近 N 个自然日（北京时间，含今天）"),
    limit: int = Query(200, ge=1, le=MAX_ANNOUNCEMENT_LIMIT, description="本页条数上限"),
    offset: int = Query(0, ge=0, le=MAX_ANNOUNCEMENT_OFFSET, description="分页偏移"),
) -> dict[str, Any]:
    """该股公告时间线（按公告时间**倒序**，带 `category`）。

    `category` 来自 `app.core.update.classify_announcement`：
    financial（定期报告/业绩预告/快报，会触发财务刷新）| dividend（分红除权）| other。
    `total` / `by_category` 统计的是**整个时间窗**，不是当前页。
    """
    stock_code = _parse_stock_code(code)
    _require_visible(request, stock_code)
    sqlite = request.app.state.sqlite
    cutoff = cn_today() - timedelta(days=days - 1)

    with _read_guard("research.announcements"):
        rows = _load_announcement_rows(sqlite, stock_code, cutoff)

    timed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        published, published_date = _in_window(row, cutoff)
        if published is None or published_date is None or published_date < cutoff:
            continue
        title = row.get("title")
        item = {
            "announcement_id": row.get("announcement_id"),
            "stock_code": row.get("stock_code"),
            "title": title,
            "category": _classify(title),
            "announcement_time": published.isoformat(),
            "announcement_date_cn": published_date.isoformat(),
            "announcement_datetime_cn": published.astimezone(CN_TZ).isoformat(),
        }
        timed.append((published, item))

    # 时间倒序（同一时刻按 title 稳定排序，避免分页漂移）
    timed.sort(key=lambda pair: (pair[0], str(pair[1].get("title") or "")), reverse=True)

    by_category = dict.fromkeys(_ANNOUNCEMENT_CATEGORIES, 0)
    for _, item in timed:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1

    total = len(timed)
    page = [item for _, item in timed[offset : offset + limit]]

    return {
        "stock_code": stock_code,
        "days": days,
        "cutoff_date": cutoff.isoformat(),
        "timezone": "Asia/Shanghai",
        "limit": limit,
        "offset": offset,
        "count": len(page),
        "total": total,
        "has_more": offset + len(page) < total,
        "by_category": by_category,
        "items": page,
        "category_source": "app.core.update.classify_announcement",
        "disclaimer": ANNOUNCEMENT_DISCLAIMER,
    }


@router.get("/announcements/summary")
def get_announcements_summary(
    request: Request,
    days: int = Query(90, ge=1, le=MAX_DAYS, description="最近 N 个自然日（北京时间，含今天）"),
) -> dict[str, Any]:
    """全市场公告按 `category` 计数（最近 N 天，进程内 5 分钟缓存）。

    走全表扫描（22,179 行量级）→ 毫秒级；`scanned_rows` 与 `truncated` 如实暴露
    本次实际解析了多少行，超过 `SUMMARY_SCAN_LIMIT` 时 `truncated=true`（不假装全量）。
    """
    sqlite = request.app.state.sqlite
    cutoff = cn_today() - timedelta(days=days - 1)
    cache_key = (str(getattr(sqlite, "db_path", "")), days, cutoff.isoformat())

    def compute() -> dict[str, Any]:
        coarse = (cutoff - timedelta(days=1)).isoformat()
        rows = sqlite.query(
            """SELECT stock_code, announcement_time, title
               FROM announcement_registry
               WHERE announcement_time >= ?
               ORDER BY announcement_time DESC
               LIMIT ?""",
            [coarse, SUMMARY_SCAN_LIMIT],
        )
        by_category = dict.fromkeys(_ANNOUNCEMENT_CATEGORIES, 0)
        stocks: set[str] = set()
        kept = 0
        for row in rows:
            published, published_date = _in_window(row, cutoff)
            if published_date is None or published_date < cutoff:
                continue
            category = _classify(row.get("title"))
            by_category[category] = by_category.get(category, 0) + 1
            stocks.add(str(row.get("stock_code") or ""))
            kept += 1
        return {
            "days": days,
            "cutoff_date": cutoff.isoformat(),
            "timezone": "Asia/Shanghai",
            "window_total": kept,
            "by_category": by_category,
            "scanned_rows": len(rows),
            "scan_limit": SUMMARY_SCAN_LIMIT,
            "truncated": len(rows) >= SUMMARY_SCAN_LIMIT,
            "stocks": len(stocks - {""}),
            "generated_at": datetime.now(UTC).isoformat(),
            "cache_ttl_seconds": SUMMARY_CACHE_TTL_SECONDS,
            "category_source": "app.core.update.classify_announcement",
            "disclaimer": ANNOUNCEMENT_DISCLAIMER,
        }

    # key 含库路径，避免同进程多 app/测试夹具串数据
    with _read_guard("research.announcements_summary"):
        return _summary_cache.get_or_compute(cache_key, compute)
