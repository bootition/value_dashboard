"""自选列表 API (PRD §13)

支持: 分组/排序/自定义列/手动保留/移除/来源记录
不支持: 备注/目标价/预警/自动提醒 (PRD §13 WL3)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.core import watchlist_groups as wg
from app.core import watchlist_ops as wo
from app.core.blacklist import blacklisted_code_set
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
    #: 缺省 → 默认分组 `我的自选`（2026-11-26：详情页/筛选结果页的「加入自选」不再
    #: 让用户先想一个分组名，点一下就落到默认组，之后再挪）。
    group_name: str | None = None
    source_rule_id: int | None = None
    source_result_id: str | None = None


class RemoveRequest(BaseModel):
    stock_code: str
    group_name: str | None = None  # None=从所有分组移除


class MoveGroupRequest(BaseModel):
    """单只或批量移动（2026-11-26 补批量：筛选结果/自选表格里勾选若干只再挪）。"""

    to_group: str
    #: 单只口径（旧契约，保留）。
    stock_code: str | None = None
    #: 批量口径。
    stock_codes: list[str] | None = None
    #: 来源分组；None = 从该股票所在的**所有**分组里移动（批量选择时更顺手）。
    from_group: str | None = None
    #: `copy`（默认语义，见下）还是 `move`。
    #:
    #: 2026-11-27（用户裁决）：从**规则分组**往自己的分组里放，必须是**复制**。
    #: 移动会引出一串无解的语义问题：「下一轮筛选要不要把移出去的再移回来？」
    #: 「被规则踢出之后又该怎么办？」——复制则天然没这些问题：
    #: 规则分组永远由规则的运行结果决定，用户自己的分组永远由用户决定，两边互不干扰。
    mode: Literal["copy", "move"] = "copy"


class RefreshGroupRequest(BaseModel):
    group_name: str


class CreateGroupRequest(BaseModel):
    name: str


class RenameGroupRequest(BaseModel):
    new_name: str


def _raise_group_error(error: wg.WatchlistGroupError) -> NoReturn:
    """分组域错误 → HTTP（`detail` 带 `reason_code` + 中文 message，前端直接展示）。"""
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


def _actor_of(request: Request) -> str:
    """这条写操作是谁发起的（`local-ui` / `cli` / `api`）。

    判定实现在 `watchlist_ops.actor_from_headers`（单源）：`screening.py` 的
    `add_to_watchlist` 也写自选行，必须与这里同一个口径。
    """
    return wo.actor_from_headers(request.headers)


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
    #
    # 2026-11-26：分组**存在性**的真相源改成 `watchlist_groups` 注册表。
    # 这里曾经是 `SELECT group_name, COUNT(*) ... GROUP BY group_name`，于是
    # 「刚建好、还没放股票」的分组在侧栏里根本不存在（用户不能自建分组的根因）。
    # 现在注册表里有行就返回，`cnt=0` 的空分组照常出现在侧栏。
    groups = wg.list_groups(sqlite, blocked_codes=blocked_codes)

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

    2026-11-26 契约变化（详情页/筛选结果页的「加入自选」按钮靠它）：
    - `group_name` **可选**，缺省落到默认分组 `我的自选`；
    - 已经在该分组 → **幂等**返回 `created=false`（按钮会连点，报错等于坑用户），
      且**不刷新 `added_at`**（否则列表里本来在下面的那只票每次都跳到顶部）；
    - 分组名在注册表里不存在 → 顺手登记成自建分组（见
      `watchlist_groups.ensure_group_for_write`：宽松但留痕）。
    """
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck
    actor = _actor_of(request)
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
    try:
        group_name = wg.normalize_group_name(req.group_name) or wg.DEFAULT_GROUP_NAME
    except wg.WatchlistGroupError as error:
        _raise_group_error(error)
    created = True
    with sqlite.transaction() as conn:
        # 2026-11-27 自查修正：`ensure_group_for_write` 原来在事务**外**调用，
        # 于是它自己提交一次"登记分组"，add 再失败（如溯源校验 400）就会留下一个
        # **空分组**，而日志里只有一条失败的 add —— 看不出这个空组是那次操作造出来的。
        # 现在把它并进同一事务：要么"分组 + 自选行 + 审计"一起成功，要么一起回滚。
        try:
            group_name = wg.ensure_group_for_write(sqlite, group_name, conn=conn)
        except wg.WatchlistGroupError as error:
            _raise_group_error(error)
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
        existing = conn.execute(
            "SELECT source_rule_id, source_result_id FROM watchlist "
            "WHERE stock_code = ? AND group_name = ?",
            [stock_code, group_name],
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO watchlist (stock_code, group_name, source_rule_id, source_result_id) "
                "VALUES (?, ?, ?, ?)",
                [stock_code, group_name, source_rule_id, req.source_result_id],
            )
        else:
            # 幂等：已存在就不再插、也不动 added_at。只在调用方显式给了来源时补写
            # 溯源（否则重复点一次按钮会把这行的时间戳顶到列表最前面）。
            created = False
            if source_rule_id is not None or req.source_result_id is not None:
                conn.execute(
                    "UPDATE watchlist SET source_rule_id = ?, source_result_id = ? "
                    "WHERE stock_code = ? AND group_name = ?",
                    [source_rule_id, req.source_result_id, stock_code, group_name],
                )
        # 审计（v22）：与数据写**同一事务** —— 上面任何一条 400 抛出去都会连日志一起回滚。
        wo.record(
            sqlite,
            op=wo.OP_ADD,
            stock_codes=[stock_code],
            to_group=group_name,
            detail={
                "created": created,
                "idempotent": not created,
                "source_rule_id": source_rule_id,
                "source_result_id": req.source_result_id,
            },
            actor=actor,
            conn=conn,
        )
    return {
        "status": "ok",
        "stock_code": stock_code,
        "group": group_name,
        "created": created,
        "default_group": wg.DEFAULT_GROUP_NAME,
    }


@router.get("/status")
def watchlist_status(request: Request, stock_code: str = Query(...)) -> dict:
    """某只股票的自选状态（详情页按钮三态：不在自选 / 已在某组 / 在多个组）。

    契约：
    - `in_watchlist`：该股票是否在**任何**可见自选分组里（黑名单股票一律 false）；
    - `groups`：所在分组名列表（可能多个）；
    - `default_group`：默认分组名（前端不写死中文字面量）；
    - `blacklisted`：是否在黑名单（前端据此把按钮置灰并说明原因）。
    """
    sqlite = request.app.state.sqlite
    code = (stock_code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="invalid stock code: 6 digits required")
    return wg.stock_status(sqlite, code)


@router.post("/refresh")
def refresh_watchlist_group(req: RefreshGroupRequest, request: Request) -> dict:
    """按来源规则重新执行筛选，并刷新当前分组中由该规则产生的自选股票。

    自选列表只记录股票代码和规则来源，不保留当次不可信数值；
    因此该操作支持在数据更新后把整个规则命中的股票替换为最新结果。
    """
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck
    actor = _actor_of(request)
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

    # 审计（v22）：整组替换是最具破坏性的自选写路径（删旧行 + 插新行），必须留痕，
    # 否则「我的分组怎么从 591 行变成 200 行」永远查不出来。
    if refreshed:
        wo.record(
            sqlite,
            op=wo.OP_REFRESH,
            to_group=group_name,
            detail={
                "refreshed": refreshed,
                "added": total_added,
                "removed": total_removed,
                "rule_ids": [int(ref["source_rule_id"]) for ref in refs],
            },
            actor=actor,
        )
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
    actor = _actor_of(request)
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
    # 审计（v22）：404 分支在上面先抛 —— **没删掉任何行就不许留痕**。
    wo.record(
        sqlite,
        op=wo.OP_REMOVE,
        stock_codes=[req.stock_code],
        from_group=req.group_name,
        detail={
            "rows": removed,
            # None = 从所有分组移除，这个区别决定"它到底是不是还在别的组里"。
            "scope": "group" if req.group_name else "all_groups",
            "group": req.group_name,
        },
        actor=actor,
    )
    return {"status": "ok", "removed": req.stock_code, "rows": removed}


@router.post("/move")
def move_group(req: MoveGroupRequest, request: Request) -> dict:
    """把自选股票移动到其它分组（单只 / 批量，2026-11-26 补批量）。

    调用方有两种：
    - 自选表格「选中若干只 → 移动到分组」→ `{stock_codes: [...], from_group, to_group}`；
    - 旧的单只移动 → `{stock_code, from_group, to_group}`（保持可用）。

    契约：
    - `to_group` 必须**已在注册表里存在**（用户在界面下拉里选，对不上说明前端状态过期
      → 404，明确让用户刷新；这里不替用户建组，否则一个拼错的名字会静默变成新分组）。
    - `from_group` 缺省 = 从该股票所在的**所有**分组移动。
    - 目标分组里已经有同一只股票（唯一索引 `(stock_code, group_name)`）→ 直接删掉
      来源行（等价于"移动完成"），不报 409 让用户卡住。
    - 一只都没匹配到 → 404（与 `DELETE /remove` 同口径，避免前端以为移动成功了）。
    """
    sqlite = request.app.state.sqlite
    actor = _actor_of(request)
    codes = [code.strip() for code in (req.stock_codes or []) if code and code.strip()]
    if req.stock_code and req.stock_code.strip():
        codes.append(req.stock_code.strip())
    # 去重但保持顺序
    ordered: list[str] = []
    for code in codes:
        if code not in ordered:
            ordered.append(code)
    if not ordered:
        raise HTTPException(status_code=400, detail="stock_code or stock_codes is required")
    for code in ordered:
        if not re.fullmatch(r"\d{6}", code):
            raise HTTPException(status_code=400, detail="invalid stock code: 6 digits required")
    try:
        to_group = wg.ensure_group_exists(sqlite, req.to_group)
        from_group = wg.normalize_group_name(req.from_group) if req.from_group else None
    except wg.WatchlistGroupError as error:
        _raise_group_error(error)

    moved = 0
    copied = 0
    skipped_already_in_target = 0
    not_found: list[str] = []
    with sqlite.transaction() as conn:
        if req.mode == "copy":
            # 复制：目标组插一行（**不带任何规则来源** → `source_rule_id`/`source_result_id`
            # 一律 NULL）。这是"用户自己的选择"，因此：
            #   · 规则分组按"替换"刷新时只动它自己那一组，不会波及这里；
            #   · 股票被规则踢出后，这里的副本**照样保留**（这正是用户要的）；
            #   · 删规则不会被这行挡住（规则删除守卫只数带 source_rule_id 的引用）。
            # 黑名单一致性（2026-11-27 自查发现）：`POST /add` 早就拦黑名单，
            # 但复制路径没拦 —— 于是"被隐藏的股票"能通过复制重新出现在自选里。
            # 两者的语义都是"往自选里加"，必须同一口径。
            blocked = blacklisted_code_set(sqlite)
            blacklisted_hits = [code for code in ordered if code in blocked]
            for code in ordered:
                if code in blocked:
                    continue
                if from_group:
                    source = conn.execute(
                        "SELECT 1 FROM watchlist WHERE stock_code = ? AND group_name = ?",
                        [code, from_group],
                    ).fetchone()
                else:
                    source = conn.execute(
                        "SELECT 1 FROM watchlist WHERE stock_code = ?", [code]
                    ).fetchone()
                if source is None:
                    not_found.append(code)
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM watchlist WHERE stock_code = ? AND group_name = ?",
                    [code, to_group],
                ).fetchone()
                if existing is not None:
                    skipped_already_in_target += 1
                    continue
                conn.execute(
                    "INSERT INTO watchlist (stock_code, group_name) VALUES (?, ?)",
                    [code, to_group],
                )
                copied += 1
            if blacklisted_hits and copied == 0 and skipped_already_in_target == 0:
                raise HTTPException(
                    status_code=400,
                    detail="stock is blacklisted; remove it from the blacklist first",
                )
            if copied == 0 and skipped_already_in_target == 0 and not blacklisted_hits:
                raise HTTPException(status_code=404, detail="watchlist entry not found")
            # 审计（v22）：上面两条 400/404 先抛 —> 复制 0 行时不会留下"假操作"。
            wo.record(
                sqlite,
                op=wo.OP_COPY,
                stock_codes=ordered,
                from_group=from_group,
                to_group=to_group,
                detail={
                    "mode": "copy",
                    "copied": copied,
                    "rows": copied,
                    "moved": 0,
                    "already_in_target": skipped_already_in_target,
                    "not_found": not_found,
                    "blacklisted": blacklisted_hits,
                },
                actor=actor,
                conn=conn,
            )
            return {
                "status": "ok",
                "mode": "copy",
                "stock_code": ordered[0] if len(ordered) == 1 else None,
                "stock_codes": ordered,
                "from": from_group,
                "to": to_group,
                "moved": 0,
                "copied": copied,
                "rows": copied,
                "already_in_target": skipped_already_in_target,
                "not_found": not_found,
                "blacklisted": blacklisted_hits,
            }
        for code in ordered:
            if from_group:
                source_rows = conn.execute(
                    "SELECT 1 FROM watchlist WHERE stock_code = ? AND group_name = ?",
                    [code, from_group],
                ).fetchall()
            else:
                source_rows = conn.execute(
                    "SELECT 1 FROM watchlist WHERE stock_code = ?", [code]
                ).fetchall()
            if not source_rows:
                not_found.append(code)
                continue
            existing = conn.execute(
                "SELECT 1 FROM watchlist WHERE stock_code = ? AND group_name = ?",
                [code, to_group],
            ).fetchone()
            if existing is not None:
                # 目标组已有这只票：只清掉*其它*分组里的来源行（唯一索引
                # `(stock_code, group_name)` 不允许两行，直接插会撞键）。
                skipped_already_in_target += 1
            if from_group:
                cursor = conn.execute(
                    "DELETE FROM watchlist WHERE stock_code = ? AND group_name = ?",
                    [code, from_group],
                )
            else:
                # 不留 `to_group` 自己那行：否则"已在目标组"的股票会被顺手删掉。
                cursor = conn.execute(
                    "DELETE FROM watchlist WHERE stock_code = ? AND group_name <> ?",
                    [code, to_group],
                )
            moved += max(0, int(cursor.rowcount))
            if existing is None:
                conn.execute(
                    "INSERT INTO watchlist (stock_code, group_name) VALUES (?, ?)",
                    [code, to_group],
                )
        # 2026-11-27 自查修正：404 原来在事务**提交之后**才抛（"提交后判失败"）。
        # 现在挪进事务内、且在写审计之前 —— 判失败就整笔回滚，既不留"先写了再报错"
        # 的顺序隐患，也不会在日志里留下一条"其实没发生"的 move。
        if moved == 0:
            raise HTTPException(status_code=404, detail="watchlist entry not found")
        # 审计（v22）：与数据写同事务。
        if moved > 0:
            wo.record(
                sqlite,
                op=wo.OP_MOVE,
                stock_codes=ordered,
                from_group=from_group,
                to_group=to_group,
                detail={
                    "mode": "move",
                    "moved": len([code for code in ordered if code not in not_found]),
                    "rows": moved,
                    "already_in_target": skipped_already_in_target,
                    "not_found": not_found,
                    "blacklisted": [],
                },
                actor=actor,
                conn=conn,
            )
    return {
        "status": "ok",
        "mode": "move",
        "stock_code": ordered[0] if len(ordered) == 1 else None,
        "stock_codes": ordered,
        "from": from_group,
        "to": to_group,
        "moved": len([code for code in ordered if code not in not_found]),
        "rows": moved,
        "already_in_target": skipped_already_in_target,
        "not_found": not_found,
    }


@router.get("/groups")
def list_groups(request: Request) -> dict:
    """列出**注册表里的全部分组**（含 0 只的空分组）+ 统计事实。

    `cnt` 保持原语义（行数，黑名单股票同口径剔除）；`runs` / `manual` / `rule_ids`
    沿用上一轮加的只读字段：
    - `runs`：该分组来自几次不同的**已保存筛选结果**（`COUNT(DISTINCT source_result_id)`，
      NULL 不计）——替换确认框要如实说「来自 M 次历史运行」；
    - `manual`：`source_result_id IS NULL` 的手动自选行数（替换永远不动它们）；
    - `rule_ids`：该分组出现的不同 `source_rule_id`（删分组时判断能否连带删规则）。

    2026-11-26：分组**不再由 `GROUP BY watchlist` 派生** —— 那是「不能自建分组」的
    根因（空分组不可能存在）。清单来自 `watchlist_groups` 注册表，读时自愈：
    `watchlist` 里有、注册表里没有的 group_name 会先被补登记。
    """
    sqlite = request.app.state.sqlite
    healed = wg.heal_registry(sqlite)
    groups = wg.list_groups(sqlite, blocked_codes=blacklisted_code_set(sqlite))
    return {
        "groups": groups,
        "count": len(groups),
        "default_group": wg.DEFAULT_GROUP_NAME,
        "healed": healed,
    }


@router.post("/groups")
def create_group(req: CreateGroupRequest, request: Request) -> dict:
    """新建自建分组（`kind='manual'`）。

    契约：重名（含默认组名）→ 409；空名/超长（>40 字）→ 400；成功 → 200 +
    分组对象（`cnt=0`，因为空分组本来就该存在）。
    """
    sqlite = request.app.state.sqlite
    try:
        created = wg.create_group(sqlite, req.name)
    except wg.WatchlistGroupError as error:
        _raise_group_error(error)
    # 审计（v22）：`create_group` 自己带事务、拿不到连接，所以只能成功之后补记。
    wo.record(
        sqlite,
        op=wo.OP_GROUP_CREATE,
        to_group=str(created["group"]["group_name"]),
        detail={"kind": created["group"].get("kind"), "cnt": 0},
        actor=_actor_of(request),
    )
    return created


@router.patch("/groups/{group_name}")
def rename_group(group_name: str, req: RenameGroupRequest, request: Request) -> dict:
    """重命名分组（`watchlist.group_name` 一起改）。

    默认分组拒绝（400，不可改名）；目标名已存在 → 409；源分组不存在 → 404；
    新名非法 → 400。注册表行与自选行在**同一事务**里改，不会出现半改状态。
    """
    sqlite = request.app.state.sqlite
    try:
        renamed = wg.rename_group(sqlite, group_name, req.new_name)
    except wg.WatchlistGroupError as error:
        _raise_group_error(error)
    # 审计（v22）：新旧名字 + 跟着改名的行数，一条就够还原「7 行去哪了」。
    # `unchanged=True`（新名与旧名相同）没有写任何数据，不留痕 —— 与 404 不留痕同口径。
    if not renamed.get("unchanged"):
        wo.record(
            sqlite,
            op=wo.OP_GROUP_RENAME,
            from_group=str(renamed["group"]),
            to_group=str(renamed["new_name"]),
            detail={"moved_rows": int(renamed.get("moved_rows") or 0)},
            actor=_actor_of(request),
        )
    return renamed


@router.delete("/groups/{group_name}")
def delete_group(
    group_name: str,
    request: Request,
    delete_rule: bool = Query(False),
) -> dict:
    """删除整个自选分组（可选同时删除该分组唯一的来源规则）。

    2026-11-25 用户实测投诉：自选分组只能一只一只删，没有「删除分组」；
    同型事故现场是「同一分组重复保存到自选」把两次运行累加成 591 行，
    用户想整体重来却没有入口。

    契约：
    - **分组行一定删除**（这是用户点的主操作），`deleted_rows` 是真实删除行数；
      分组不存在 → 404，不假装成功。
    - `delete_rule=True`：仅当该分组只出现**一个**非空 `source_rule_id` 且规则
      仍存在时，才尝试删规则；多个不同规则 → 不猜、只删分组，`skipped_rules`
      逐条说明原因。
    - 规则删除复用 `screening.delete_rule_with_guards`（同一套守卫：被已保存
      结果或**其它分组**的自选记录引用时拒绝）。守卫拒绝时分组删除仍成功，
      但响应 `rule_deleted=False` + `rule_delete_reason` + `skipped_rules`
      如实说明「哪类引用、几条」，绝不让用户以为规则也删了。
    """
    sqlite = request.app.state.sqlite
    actor = _actor_of(request)
    name = wg.normalize_group_name(group_name)
    if not name:
        raise HTTPException(status_code=400, detail="group_name is required")
    # 默认分组保护（先于存在性判断：默认组即使现在 0 只也必须回 400，不能说"不存在"）。
    if wg.is_default_group(sqlite, name):
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "watchlist_default_group_protected",
                "message": (
                    f"「{wg.DEFAULT_GROUP_NAME}」是默认分组，不能删除；"
                    "可以删掉里面的股票，或新建分组后把股票移过去。"
                ),
            },
        )
    # 2026-11-26：分组的真相源是注册表。历史分组（迁移前由 GROUP BY 派生）可能
    # 只有 watchlist 行没有注册表行，先自愈再判断存在性，否则用户会在自己看得见的
    # 分组上收到 404。
    wg.heal_registry(sqlite)
    in_registry = wg.group_kind(sqlite, name) is not None
    summary = sqlite.query(
        "SELECT COUNT(*) AS cnt FROM watchlist WHERE group_name = ?",
        [name],
    )[0]
    if not in_registry and int(summary["cnt"] or 0) == 0:
        raise HTTPException(status_code=404, detail="watchlist group not found")
    rule_ids = [
        int(row["source_rule_id"])
        for row in sqlite.query(
            "SELECT DISTINCT source_rule_id FROM watchlist "
            "WHERE group_name = ? AND source_rule_id IS NOT NULL "
            "ORDER BY source_rule_id",
            [name],
        )
    ]

    # 先删分组行，再让规则守卫统计引用：这样 `watchlist` 引用只剩**其它分组**
    # 的残留，不会把本次正在删除的分组自己算成"规则删不掉"。
    # 2026-11-27 自查修正：这两步原来各自开事务（`sqlite.execute` 一次、`wg.delete_registry_group`
    # 又一次），中间失败会留下 **"0 只但还在侧栏"的幽灵分组**。现在放进同一个事务：
    # 要么都成、要么都不成。
    with sqlite.transaction() as conn:
        deleted_rows = int(conn.execute(
            "DELETE FROM watchlist WHERE group_name = ?", [name]
        ).rowcount or 0)
        # 注册表行一起删：不删的话这个分组会变成"0 只但永远在侧栏"的幽灵分组
        # （空分组合法 ≠ 用户点了删除还留着）。`deleted_rows` 仍是 watchlist 真实行数。
        registry_removed = wg.delete_registry_group(sqlite, name, conn=conn)
    response: dict[str, Any] = {
        "status": "ok",
        "group": name,
        "deleted_rows": deleted_rows,
        "registry_removed": registry_removed,
        "delete_rule_requested": bool(delete_rule),
        "rule_deleted": False,
        "deleted_rule_id": None,
        "rule_delete_status": "not_requested",
        "rule_delete_reason": None,
        "skipped_rules": [],
    }

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        """审计 + 返回：`delete_group` 有 5 个成功出口，日志只能在**唯一的**出口写。"""
        wo.record(
            sqlite,
            op=wo.OP_GROUP_DELETE,
            from_group=name,
            to_group=name,
            detail={
                "deleted_rows": int(payload.get("deleted_rows") or 0),
                "registry_removed": bool(payload.get("registry_removed")),
                "delete_rule_requested": bool(payload.get("delete_rule_requested")),
                "rule_deleted": bool(payload.get("rule_deleted")),
                "deleted_rule_id": payload.get("deleted_rule_id"),
                "rule_delete_status": payload.get("rule_delete_status"),
                "rule_delete_reason": payload.get("rule_delete_reason"),
                "skipped_rule_ids": [
                    int(item["rule_id"]) for item in payload.get("skipped_rules") or []
                ],
                "source_rule_ids": rule_ids,
            },
            actor=actor,
        )
        return payload

    if not delete_rule:
        return finish(response)
    if not rule_ids:
        response["rule_delete_status"] = "no_rule"
        response["rule_delete_reason"] = "该分组没有关联筛选规则，本次只删除了分组。"
        return finish(response)
    if len(rule_ids) > 1:
        message = (
            f"该分组来自 {len(rule_ids)} 个不同筛选规则（"
            + "、".join(str(rule_id) for rule_id in rule_ids)
            + "），为避免误删，本次只删除分组、未删除任何规则。"
        )
        response["rule_delete_status"] = "skipped"
        response["rule_delete_reason"] = message
        response["skipped_rules"] = [
            {
                "rule_id": rule_id,
                "reason_code": "multiple_source_rules",
                "message": message,
            }
            for rule_id in rule_ids
        ]
        return finish(response)

    # 局部导入避免 `watchlist` ↔ `screening` 模块级循环依赖；同时保证删分组
    # 走的确实是 `DELETE /rules/{id}` 的同一套守卫（不是第二份实现）。
    from app.web.api.screening import delete_rule_with_guards

    rule_id = rule_ids[0]
    try:
        deleted_rule = delete_rule_with_guards(sqlite, rule_id)
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        reason_code = str(detail.get("reason_code") or "rule_delete_failed")
        if error.status_code == 404:
            reason_code = "rule_not_found"
            message = "该分组记录的筛选规则已不存在，本次只删除了分组。"
        else:
            message = str(detail.get("message") or "规则没有删除，原因未知。")
        response["rule_delete_status"] = "skipped"
        response["rule_delete_reason"] = message
        response["skipped_rules"] = [{
            "rule_id": rule_id,
            "reason_code": reason_code,
            "message": message,
            # 守卫原样带回的引用计数：results / watchlist / runs（几条）。
            "refs": detail.get("refs"),
        }]
        return finish(response)

    response["rule_deleted"] = True
    response["deleted_rule_id"] = deleted_rule["deleted_rule_id"]
    response["rule_delete_status"] = "deleted"
    return finish(response)


@router.get("/ops")
def list_watchlist_ops(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    op: str | None = Query(None),
    stock_code: str | None = Query(None),
) -> dict:
    """最近的**用户操作**记录（v22 `watchlist_ops`）：这行数据是怎么变成现在这样的。

    为什么要这个端点（2026-11-24 用户投诉）：

    > 「我从 ROCE 这个筛选分组里面移动过去的……你难道查不出这个日志来吗？」

    当时只能靠「ROCE 少 7 行 + 我的自选多 7 行 + 时间戳吻合」去猜，而且第一次还猜错
    了（把 UTC 当本地时间，误判成"卡了 8 小时"）。所以本端点**时间一律双轨**：

    - `created_at_utc`：`2026-11-24T05:37:00Z`（库里的真值，`CURRENT_TIMESTAMP` 是 UTC）；
    - `created_at_local`：`2026-11-24 13:37:00`（北京时间的同一时刻，+8 小时）。

    契约：
    - `limit`：默认 50，1~500；按时间倒序（= id 倒序）。
    - `op`：`add/copy/move/remove/group_create/group_rename/group_delete/refresh`；
      不在这个集合里 → 400（拼错的操作名不该静默返回空列表）。
    - `stock_code`：1~6 位数字，按"这条操作涉及这只股票"过滤；非数字 → 400。
    - 每条带 `op_label`（中文）与 `summary`（一句话，措辞由后端单源给出）。
    - `actor`：`local-ui` / `cli` / `api`（来源粗分，见 `_actor_of`）。
    """
    sqlite = request.app.state.sqlite
    op_key = (op or "").strip()
    if op_key and op_key not in wo.OP_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown op: {op_key}（可选：{'/'.join(wo.OP_LABELS)}）",
        )
    code = (stock_code or "").strip()
    if code and not re.fullmatch(r"\d{1,6}", code):
        raise HTTPException(status_code=400, detail="invalid stock code filter: digits only")
    return wo.list_ops(sqlite, limit=limit, op=op_key or None, stock_code=code or None)
