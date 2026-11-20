"""自选列表 API (PRD §13)

支持: 分组/排序/自定义列/手动保留/移除/来源记录
不支持: 备注/目标价/预警/自动提醒 (PRD §13 WL3)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.blacklist import blacklisted_code_set, exclusion_condition
from app.core.screening.defaults import base_pool_defaults
from app.core.storage.duckdb_store import DuckDBReadLockedError

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

logger = logging.getLogger(__name__)

#: `GET /api/watchlist/list` 的 DuckDB 富化状态。
#:
#: 自选列表有**两个数据源**：分组/股票代码只查 SQLite（用户态，永不受
#: 自动更新影响），名称/指标才需要 DuckDB 的 `stock_meta` + `indicator_snapshot`。
#: 写进程持锁时第二层取不到，是**暂时状态**，不是"自选为空"。
#:
#: 2026-11-24 用户实测：自动更新期间整页显示「还没有分组」+「还没有可显示的
#: 自选分组」，而库里《往股东口袋装钱的大老公司》573 行完好——取名字失败
#: 被当成了列表失败。这是最不能接受的一类假象（比报错更糟），因此富化层
#: 一律降级返回，并在响应里显式标注状态。
ENRICHMENT_OK = "ok"
ENRICHMENT_UNAVAILABLE = "unavailable"
#: 写进程持锁的稳定原因码：前端据它说"正在自动更新"，而不是猜一个原因。
ENRICHMENT_READ_LOCKED = "duckdb_read_locked"

# 依赖 indicator_snapshot 的数值字段；信任决策阻断时必须遮蔽
_SNAPSHOT_VALUE_FIELDS = (
    "latest_close",
    "pe_ttm",
    "pb_mrq",
    "roe",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "revenue_yoy",
    "net_profit_yoy",
    "dividend_yield",
    "total_market_cap",
    "circ_market_cap",
)


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


class RefreshGroupRequest(BaseModel):
    group_name: str


def _fetch_stock_info(duck: Any, stock_codes: list[str]) -> dict[str, dict]:
    """DuckDB 富化层：按代码取名称/交易所/行业/上市日 + 最新一期快照指标。

    这是自选列表里**唯一**需要 DuckDB 的步骤，单独成函数是为了让调用点
    能把"富化失败"与"列表失败"分开处理（见 `list_watchlist` 的降级注释）。
    """
    placeholders = ", ".join(["?"] * len(stock_codes))
    stock_info = duck.read_query(
        f"""SELECT m.stock_code, m.name, m.exchange, m.csrc_l1, m.listing_date,
                   s.latest_close, s.pe_ttm, s.pb_mrq, s.roe,
                   s.gross_margin, s.net_margin, s.debt_ratio,
                   s.revenue_yoy, s.net_profit_yoy, s.dividend_yield,
                   s.total_market_cap, s.circ_market_cap
             FROM stock_meta m
             LEFT JOIN LATERAL (
                 SELECT * FROM indicator_snapshot s2
                 WHERE s2.stock_code = m.stock_code
                 ORDER BY s2.report_date DESC LIMIT 1
             ) s ON true
             WHERE m.stock_code IN ({placeholders})""",
        stock_codes,
    )
    return {r["stock_code"]: r for r in stock_info}


def _read_trust(duck: Any, sqlite: Any) -> dict:
    """服务端权威信任决策；读不到告警集时也不许炸掉整个自选页。

    DuckDB 不可用时 `read_warning_codes` 本身也可能失败——这时返回空告警集：
    降级响应里所有指标都是 None，遮蔽是空操作，因此**"没有任何数值"本身**
    就保证了 fail-closed，不存在放行未核验数字的可能。
    """
    from app.core.data_quality import indicator_trust, read_warning_codes

    try:
        return indicator_trust(read_warning_codes(duck, sqlite))
    except Exception as error:  # 见 docstring：宁可少一个横幅，也不要整页 500
        logger.warning("自选列表读取 trust 失败，按空告警集降级: %s", error)
        return indicator_trust([])


@router.get("/list")
def list_watchlist(request: Request, group: str | None = None) -> dict:
    """列出自选股票 (PRD §13: 分组/排序/自定义列/来源记录)

    降级契约（2026-11-24）：**分组与股票行只依赖 SQLite，任何情况下都必须
    返回**；DuckDB 只用来富化名称/指标，取不到就标 `enrichment=unavailable`
    并把名称留空、指标置 null，绝不把本页整体打成 503/空列表。
    """
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck

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

    # 2026-11-22：黑名单股票从自选列表里剔除。**不删除 watchlist 行**——
    # 移出黑名单后原来自选的股票会自动回到列表，避免"加错黑名单顺手毁掉自选"。
    blocked_codes = blacklisted_code_set(sqlite)
    if blocked_codes:
        rows = [row for row in rows if str(row["stock_code"]) not in blocked_codes]

    # 分组列表必须先于空行短路计算：当前过滤分组为空时也必须返回全量分组，
    # 否则前端侧栏分组按钮全部消失、无法切回其他分组（2026-08-14 红队 P2-19）。
    # 计数必须与上面 `rows` 同口径剔除黑名单，否则侧栏显示「3」而表里只有 2 行。
    group_pred, group_params = exclusion_condition("stock_code", blocked_codes)
    groups_where = f" WHERE {group_pred}" if group_pred else ""
    groups = sqlite.query(
        f"SELECT group_name, COUNT(*) as cnt FROM watchlist{groups_where} "
        "GROUP BY group_name ORDER BY group_name",
        group_params,
    )

    if not rows:
        # 没有任何自选行：此时**不需要** DuckDB，富化视为已完成。
        return {
            "items": [], "count": 0, "groups": groups,
            "enrichment": ENRICHMENT_OK, "enrichment_error": None,
        }

    # 关联股票名称和最新指标 —— 这一步失败只影响"显示得全不全"，
    # 不影响"有没有自选"，因此异常一律就地降级（详见模块级常量注释）。
    stock_codes = [r["stock_code"] for r in rows]
    enrichment = ENRICHMENT_OK
    enrichment_error: str | None = None
    try:
        info_map = _fetch_stock_info(duck, stock_codes)
    except DuckDBReadLockedError as error:
        # 自动更新/CLI 写进程持锁：这是已知的**暂时**状态，如实提示"更新中"，
        # 不要把原始 IO 错误抛给用户，更不要把整页打成失败。
        logger.warning("自选列表 DuckDB 富化跳过（写进程持锁）: %s", error)
        info_map = {}
        enrichment = ENRICHMENT_UNAVAILABLE
        enrichment_error = ENRICHMENT_READ_LOCKED
    except Exception as error:  # 降级而非整页失败，原因照实回传
        # 非锁类故障也降级：分组与代码是 SQLite 里的事实，必须照常可见。
        # 真实原因进日志 + `enrichment_error`，不静默吞掉。
        logger.warning("自选列表 DuckDB 富化失败，降级返回: %s", error)
        info_map = {}
        enrichment = ENRICHMENT_UNAVAILABLE
        enrichment_error = f"{type(error).__name__}: {error}"

    items = []
    for row in rows:
        code = row["stock_code"]
        info = info_map.get(code, {})
        items.append({
            "stock_code": code,
            "name": info.get("name", ""),
            "exchange": info.get("exchange", ""),
            "csrc_l1": info.get("csrc_l1"),
            "listing_date": info.get("listing_date"),
            "group_name": row["group_name"],
            "source_rule_id": row.get("source_rule_id"),
            "source_result_id": row.get("source_result_id"),
            "added_at": row.get("added_at"),
            "latest_close": info.get("latest_close"),
            "pe_ttm": info.get("pe_ttm"),
            "pb_mrq": info.get("pb_mrq"),
            "roe": info.get("roe"),
            "gross_margin": info.get("gross_margin"),
            "net_margin": info.get("net_margin"),
            "debt_ratio": info.get("debt_ratio"),
            "revenue_yoy": info.get("revenue_yoy"),
            "net_profit_yoy": info.get("net_profit_yoy"),
            "dividend_yield": info.get("dividend_yield"),
            "total_market_cap": info.get("total_market_cap"),
            "circ_market_cap": info.get("circ_market_cap"),
        })

    # P1-4: 服务端权威信任决策；阻断警告存在时遮蔽快照数值字段
    from app.core.data_quality import mask_untrusted_values

    trust = _read_trust(duck, sqlite)
    untrusted = set(trust["untrusted_fields"])
    for item in items:
        item.update(mask_untrusted_values(
            {field: item[field] for field in _SNAPSHOT_VALUE_FIELDS}, trust
        ))
        item["untrusted_fields"] = (
            list(_SNAPSHOT_VALUE_FIELDS) if trust["untrusted_all"]
            else sorted(untrusted & set(_SNAPSHOT_VALUE_FIELDS))
        )

    # 获取分组列表（已上移至空行短路之前，此处删除原重复查询）

    # reports/76 P1-2: 自动更新窗口标注，前端展示横幅而非误读为不可信。
    # 2026-08-14 红队 P2-1：写窗口判定统一——自动更新锁 OR DuckDB 写锁
    # （维护/回填类 CLI 写操作持 .duckdb.write.lock 而非 update 锁）。
    try:
        from app.core.storage.update_lock import any_write_lock_active

        auto_update_in_progress = any_write_lock_active(duck.db_path)
    except Exception:
        auto_update_in_progress = False

    return {
        "items": items, "count": len(items), "groups": groups, "trust": trust,
        "auto_update_in_progress": auto_update_in_progress,
        "enrichment": enrichment, "enrichment_error": enrichment_error,
    }


@router.post("/add")
def add_to_watchlist(req: AddStockRequest, request: Request) -> dict:
    """手动添加单只股票到自选（无筛选门禁，纯手动保留）。

    与 `screening.add_to_watchlist`（`POST /api/screening/add_to_watchlist`）
    **不是重复实现**：那个端点从已保存的筛选结果批量入自选并先校验可筛选性；
    本端点只校验 6 位代码在股票池内（PRD §13）。
    """
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck
    # L0-6（报告42）: 前后端双重校验——6 位数字代码且存在于股票列表中，
    # 防止垃圾代码进入自选（筛选路径来源的代码天然合法，不受影响）
    stock_code = req.stock_code.strip()
    if not re.fullmatch(r"\d{6}", stock_code):
        raise HTTPException(status_code=400, detail="invalid stock code: 6 digits required")
    known = duck.read_query(
        "SELECT 1 AS found FROM stock_meta WHERE stock_code = ? LIMIT 1",
        [stock_code],
    )
    if not known:
        raise HTTPException(status_code=400, detail="stock code not found in universe")
    # 黑名单股票不许进自选（用户手动敲代码也拦，不只是前端隐藏）
    if stock_code in blacklisted_code_set(sqlite):
        raise HTTPException(
            status_code=400, detail="stock is blacklisted; remove it from the blacklist first"
        )
    with sqlite.transaction() as conn:
        if req.source_result_id is not None:
            result = conn.execute(
                "SELECT rule_id, result_json FROM screening_results WHERE id = ?",
                [req.source_result_id],
            ).fetchone()
            if result is None:
                raise HTTPException(status_code=400, detail="screening result provenance is missing")
            if stock_code not in {row.get("stock_code") for row in json.loads(result["result_json"])}:
                raise HTTPException(status_code=400, detail="stock code is not in the source result")
            if req.source_rule_id is not None and req.source_rule_id != result["rule_id"]:
                raise HTTPException(status_code=400, detail="source rule does not match source result")
            source_rule_id = result["rule_id"]
        elif req.source_rule_id is not None:
            raise HTTPException(status_code=400, detail="source result is required for screening provenance")
        else:
            source_rule_id = None
        conn.execute(
            """INSERT INTO watchlist
               (stock_code, group_name, source_rule_id, source_result_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stock_code, group_name) DO UPDATE SET
                 source_rule_id=excluded.source_rule_id,
                 source_result_id=excluded.source_result_id,
                 added_at=CURRENT_TIMESTAMP""",
            [stock_code, req.group_name, source_rule_id, req.source_result_id],
        )
    return {"status": "ok", "stock_code": stock_code, "group": req.group_name}


@router.post("/refresh")
def refresh_watchlist_group(req: RefreshGroupRequest, request: Request) -> dict:
    """按来源规则重新执行筛选，并刷新当前分组中由该规则产生的自选股票。

    自选列表只记录股票代码和规则来源，不保留当次不可信数值；
    因此该操作支持在数据更新后把整个规则命中的股票替换为最新结果。
    """
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck
    group_name = req.group_name.strip()
    if not group_name:
        raise HTTPException(status_code=400, detail="group_name is required")

    refs = sqlite.query(
        """SELECT source_rule_id, MIN(source_result_id) AS source_result_id
           FROM watchlist
           WHERE group_name = ? AND source_rule_id IS NOT NULL AND source_result_id IS NOT NULL
           GROUP BY source_rule_id""",
        [group_name],
    )
    if not refs:
        raise HTTPException(status_code=400, detail="该分组没有可刷新筛选来源")

    from app.core.screening.engine import ScreeningEngine

    total_added = 0
    total_removed = 0
    refreshed = 0
    for ref in refs:
        rule_id = ref["source_rule_id"]
        old_result_id = ref["source_result_id"]
        rule_rows = sqlite.query(
            "SELECT rule_json, locked_indicators, version FROM screening_rules WHERE id = ?",
            [rule_id],
        )
        if not rule_rows:
            continue
        old_results = sqlite.query(
            "SELECT * FROM screening_results WHERE id = ?",
            [old_result_id],
        )
        if not old_results:
            continue
        rule = json.loads(rule_rows[0]["rule_json"])
        locked = json.loads(rule_rows[0]["locked_indicators"] or "{}")
        old = old_results[0]
        base_pool = json.loads(old.get("base_pool_config") or "{}")
        # 老结果的 base_pool_config 可能缺键：缺键时落到配置
        # `screening.default_*`，不再用写死的 False/False/1（否则用户改配置
        # 对"刷新自选"这条路径永远无效）。
        pool_defaults = base_pool_defaults()
        include_st = bool(base_pool.get("include_st", not pool_defaults["exclude_st"]))
        include_suspended = bool(
            base_pool.get("include_suspended", not pool_defaults["exclude_suspended"])
        )
        min_listing_years = int(
            base_pool.get("min_listing_years", pool_defaults["min_listing_years"])
        )

        engine = ScreeningEngine(duck=duck, sqlite=sqlite)
        try:
            result = engine.run(
                rule=rule,
                include_st=include_st,
                include_suspended=include_suspended,
                min_listing_years=min_listing_years,
                strict_only=False,
                locked_indicators=locked,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        results = result["results"]
        columns = json.loads(old.get("columns_json") or "[]")
        if not columns and results:
            columns = [key for key in results[0] if not key.startswith("_")]

        now = datetime.now(UTC)
        title = f"{group_name} 刷新 {now:%Y-%m-%d %H:%M}"
        with sqlite.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO screening_results
                   (title, note, rule_id, rule_version, data_date,
                    result_json, columns_json, sort_json, confidence_summary, base_pool_config)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    title,
                    "由自选列表按最新数据重新筛选生成",
                    rule_id,
                    rule_rows[0]["version"],
                    result["data_date"] or now.isoformat(),
                    json.dumps(results, ensure_ascii=False, default=str),
                    json.dumps(columns, ensure_ascii=False),
                    json.dumps(rule.get("sort", []), ensure_ascii=False),
                    json.dumps({
                        "total": result["total"],
                        "strict_only": False,
                        "truncated": bool(result.get("truncated")),
                    }, ensure_ascii=False),
                    json.dumps(base_pool, ensure_ascii=False),
                ],
            )
            new_result_id = cursor.lastrowid

            removed = conn.execute(
                "DELETE FROM watchlist WHERE group_name = ? AND source_rule_id = ?",
                [group_name, rule_id],
            ).rowcount
            total_removed += removed

            added = 0
            for row in results:
                code = row.get("stock_code")
                if not code:
                    continue
                conn.execute(
                    """INSERT INTO watchlist
                       (stock_code, group_name, source_rule_id, source_result_id)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(stock_code, group_name) DO UPDATE SET
                         source_rule_id=excluded.source_rule_id,
                         source_result_id=excluded.source_result_id,
                         added_at=CURRENT_TIMESTAMP""",
                    [code, group_name, rule_id, new_result_id],
                )
                added += 1
            total_added += added
        refreshed += 1

    return {
        "status": "ok",
        "group": group_name,
        "refreshed": refreshed,
        "added": total_added,
        "removed": total_removed,
    }


@router.delete("/remove")
def remove_from_watchlist(req: RemoveRequest, request: Request) -> dict:
    """从自选移除 (PRD §13: 手动移除)

    reports/76 P3-6: 返回实际删除行数；无匹配行时 404，避免前端误以为成功。
    """
    sqlite = request.app.state.sqlite
    if req.group_name:
        removed = sqlite.execute(
            "DELETE FROM watchlist WHERE stock_code = ? AND group_name = ?",
            [req.stock_code, req.group_name],
        )
    else:
        removed = sqlite.execute(
            "DELETE FROM watchlist WHERE stock_code = ?",
            [req.stock_code],
        )
    if removed == 0:
        raise HTTPException(status_code=404, detail="watchlist entry not found")
    return {"status": "ok", "removed": req.stock_code, "rows": removed}


@router.post("/move")
def move_group(req: MoveGroupRequest, request: Request) -> dict:
    """移动到其他分组"""
    sqlite = request.app.state.sqlite
    sqlite.execute(
        "UPDATE watchlist SET group_name = ? WHERE stock_code = ? AND group_name = ?",
        [req.to_group, req.stock_code, req.from_group],
    )
    return {"status": "ok", "stock_code": req.stock_code,
            "from": req.from_group, "to": req.to_group}


@router.get("/groups")
def list_groups(request: Request) -> dict:
    """列出所有分组"""
    sqlite = request.app.state.sqlite
    groups = sqlite.query(
        "SELECT group_name, COUNT(*) as cnt "
        "FROM watchlist GROUP BY group_name ORDER BY group_name"
    )
    return {"groups": groups, "count": len(groups)}
