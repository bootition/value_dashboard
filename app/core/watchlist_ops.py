"""自选写操作审计日志 —— 「这行数据是怎么变成现在这样的」的唯一答案（SQLite，v22）。

## 为什么需要这张表（2026-11-24 用户原话）

> 「那是他妈的，我从 ROCE 这个筛选分组里面移动过去的。这是我发现你 bug 的一次实验，
>  你难道查不出这个日志来吗？……居然连我的操作是怎么做的都查不出来，
>  那说明你在做功能的时候，根本就没有细致地检查嘛。」

真实经过：用户在 UI 里把 7 只股票从「ROCE遗传好公司」**移动**到「我的自选」。
事后只能靠「ROCE 少 7 行 + 我的自选多 7 行 + 时间戳吻合」去**猜**，还猜错了一次
（把 UTC 当本地时间，误判成"卡了 8 小时"）。根因就是自选**没有任何操作留痕**：
`job_logs` 是自动更新任务的日志，与用户点了什么毫无关系。

所以本模块只做一件事：**每一次自选写操作落一行，字段足以还原用户意图**
（谁、什么时候、哪几只、从哪个分组到哪个分组、结果如何）。读出去时
**UTC 与本地（UTC+8）双轨并列**，不接受调用方自己换算。

## 三条硬约束

1. **失败不阻断业务**（`AGENTS` 的"非致命"惯例）：`record()` 内部 try/except +
   `logger.warning`，审计写不进去（表缺失 / 锁超时）绝不能让用户的移动操作失败。
2. **数据成功才记录**：每个调用点都放在数据写之后；`/move`、`/remove` 这类
   "一只都没匹配到就 404" 的路径，404 分支必须在记录**之前**抛，否则日志里会
   多出一条没发生过的操作。
3. **同一事务**：`record(..., conn=...)` 直接复用业务事务的连接，数据与日志
   要么都提交、要么都回滚。

## 为什么在 SQLite（不写 DuckDB）

用户态数据一律 SQLite：DuckDB 是单写者 + 应用级文件锁，自动更新（5~15 分钟一轮）
持锁时**写不进去** —— 而"记下用户刚做了什么"恰恰最不能挑时间。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.financial_period import CN_TZ

logger = logging.getLogger(__name__)

#: 表名（建表见 `storage/schema.py` 的 v22 迁移；DDL 由下面的常量单源提供）。
OPS_TABLE = "watchlist_ops"

#: `CREATE TABLE` 与索引的**唯一实现**：v22 迁移与本模块的 `ensure_table()` 共用。
#: 分两处各写一份 DDL 是这一带的历史事故源（`watchlist_groups` 就吃过一次）。
CREATE_TABLE_SQL = f"""
    CREATE TABLE IF NOT EXISTS {OPS_TABLE} (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        op           TEXT NOT NULL,
        stock_codes  TEXT,
        from_group   TEXT,
        to_group     TEXT,
        detail_json  TEXT,
        actor        TEXT,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{OPS_TABLE}_created_at "
    f"ON {OPS_TABLE} (created_at)"
)

# ── op 取值与中文标签（前端不写死中文，`op_label` 一律从这里出）─────────────
OP_ADD = "add"
OP_COPY = "copy"
OP_MOVE = "move"
OP_REMOVE = "remove"
OP_GROUP_CREATE = "group_create"
OP_GROUP_RENAME = "group_rename"
OP_GROUP_DELETE = "group_delete"
#: 额外一条：`POST /refresh` 按规则重跑会**整组替换**自选行（删旧的、插新的），
#: 是最具破坏性的自选写路径；用户没点名，但漏了它这张表就答不上"我的 591 行
#: 怎么突然变成 200 行了"。
OP_REFRESH = "refresh"
#: `POST /api/screening/add_to_watchlist`：从已保存的筛选结果批量入自选（**行数最大**的
#: 来源 —— 用户现场那个 591 行的分组就是它一次次累加出来的）。用户没点名这条路径，
#: 但漏了它，这张表就只能回答"谁挪了 7 行"、答不上"这 591 行哪来的"。
OP_SCREENING_SAVE = "screening_save"

OP_LABELS: dict[str, str] = {
    OP_ADD: "加入自选",
    OP_COPY: "复制到分组",
    OP_MOVE: "移动分组",
    OP_REMOVE: "移出自选",
    OP_GROUP_CREATE: "新建分组",
    OP_GROUP_RENAME: "重命名分组",
    OP_GROUP_DELETE: "删除分组",
    OP_REFRESH: "按规则刷新分组",
    OP_SCREENING_SAVE: "保存筛选结果到自选",
}

# ── actor：目前只有本地单用户，按"来源"粗分 ────────────────────────────────
#: 允许的取值。浏览器 UI → `local-ui`；脚本/第三方（curl、python-requests）→ `api`；
#: 自称 CLI 的调用方（或显式 `X-VD-Actor: cli`）→ `cli`。
ACTORS = frozenset({"local-ui", "cli", "api"})
DEFAULT_ACTOR = "local-ui"

#: 显式声明来源的请求头（脚本/CLI 用它自报家门，避免审计里全是 `local-ui`）。
ACTOR_HEADER = "x-vd-actor"
#: `User-Agent` 里出现这些片段的都是脚本而不是浏览器（「按来源粗分」的全部实现）。
_SCRIPT_UA_HINTS = ("python", "curl", "wget", "httpx", "requests", "testclient")


def actor_from_headers(headers: Any) -> str:
    """这条写操作是谁发起的（`local-ui` / `cli` / `api`）—— **唯一实现**。

    目前只有本地单用户，所以只做粗分：浏览器 UI = `local-ui`；脚本 = `api`；
    调用方显式带 `X-VD-Actor: cli|api|local-ui` 时以它为准。**不猜得更细**
    —— 编造一个"看起来精确"的来源，和把 UTC 当本地时间是同一类错误。

    入参是 `starlette.datastructures.Headers`（`.get()` 即可），
    这样 `watchlist.py` 与 `screening.py` 共用同一份判定，不必各写一遍。
    """
    declared = (headers.get(ACTOR_HEADER) or "").strip().lower()
    if declared in ACTORS:
        return declared
    user_agent = (headers.get("user-agent") or "").lower()
    if any(hint in user_agent for hint in _SCRIPT_UA_HINTS):
        return "api"
    return DEFAULT_ACTOR

#: 表是否已经在本进程里确认过（`GET /ops` 是会被点开的热路径，不必每次建表）。
_ENSURED: set[str] = set()

#: `created_at` 由 SQLite `CURRENT_TIMESTAMP` 写入 → 形如 `YYYY-MM-DD HH:MM:SS` 的 **UTC**。
_SQLITE_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")

#: `stock_codes` 多于一屏时只列前两只 + 总数，让人一眼看懂而不是读一串数字。
_PREVIEW_CODES = 2


def ensure_table(sqlite: Any) -> None:
    """建表 + 索引（幂等，可在任意进程里重复调用；每个库路径只真正执行一次）。

    与 v21 的 `watchlist_groups.ensure_table()` 同一取舍：迁移只在启动时跑，
    而读端点随时可能被调用；表缺失时抛 `no such table` 比"操作记录为空"更难排查。
    """
    key = str(getattr(sqlite, "db_path", "")) or "default"
    if key in _ENSURED:
        return
    sqlite.execute(CREATE_TABLE_SQL)
    sqlite.execute(CREATE_INDEX_SQL)
    _ENSURED.add(key)


# ─── 写 ──────────────────────────────────────────────────────────────


def record(
    sqlite: Any,
    *,
    op: str,
    stock_codes: Iterable[str] | None = None,
    from_group: str | None = None,
    to_group: str | None = None,
    detail: Mapping[str, Any] | None = None,
    actor: str = DEFAULT_ACTOR,
    conn: Any = None,
) -> int | None:
    """写一条自选操作审计。返回新行 id；失败返回 `None`（**绝不上抛**）。

    `conn`：业务事务连接。传了就与数据写同事务（推荐）；不传则自己开一个事务
    —— 用于 `watchlist_groups` 那种"内部自带事务、拿不到连接"的调用点。

    ⚠️ 这里的 except 是**有意的**：审计是旁路，不能反过来把用户的移动操作弄失败。
    失败留下 `WARNING` 日志（含 op 与被忽略的原因），不是静默吞掉。
    """
    codes = [str(code) for code in stock_codes] if stock_codes is not None else None
    params = [
        str(op),
        json.dumps(codes, ensure_ascii=False) if codes is not None else None,
        from_group,
        to_group,
        json.dumps(dict(detail or {}), ensure_ascii=False, default=str),
        actor if actor in ACTORS else DEFAULT_ACTOR,
    ]
    sql = (
        f"INSERT INTO {OPS_TABLE} "
        "(op, stock_codes, from_group, to_group, detail_json, actor) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    try:
        if conn is not None:
            return int(conn.execute(sql, params).lastrowid or 0)
        with sqlite.transaction() as own_conn:
            return int(own_conn.execute(sql, params).lastrowid or 0)
    except Exception:  # noqa: BLE001 — 审计是旁路，任何异常都不许反向阻断业务写
        logger.warning(
            "自选操作审计写入失败（已忽略，不影响业务写）: op=%s from=%r to=%r codes=%r",
            op,
            from_group,
            to_group,
            codes,
            exc_info=True,
        )
        return None


# ─── 读 ──────────────────────────────────────────────────────────────


def list_ops(
    sqlite: Any,
    *,
    limit: int = 50,
    op: str | None = None,
    stock_code: str | None = None,
) -> dict[str, Any]:
    """最近的操作（按 id 倒序 = 时间倒序）。

    `stock_code` 是"包含这只股票"的过滤：在 JSON 文本上做子串匹配
    （代码永远是 6 位数字，不存在一只代码是另一只的子串），因此不依赖
    SQLite 的 JSON1 扩展是否编译进来。
    """
    ensure_table(sqlite)
    where: list[str] = []
    params: list[Any] = []
    if op:
        where.append("op = ?")
        params.append(op)
    code = (stock_code or "").strip()
    if code:
        where.append("stock_codes LIKE ?")
        params.append(f"%{code}%")
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    rows = sqlite.query(
        f"SELECT id, op, stock_codes, from_group, to_group, detail_json, actor, created_at "
        f"FROM {OPS_TABLE}{where_sql} ORDER BY id DESC LIMIT ?",
        [*params, max(1, int(limit))],
    )
    ops = [serialize(row) for row in rows]
    return {"ops": ops, "count": len(ops)}


def serialize(row: Mapping[str, Any]) -> dict[str, Any]:
    """一行 → API 对象（`op_label` / `summary` / UTC + 本地时间都在这里定稿）。"""
    op = str(row.get("op") or "")
    codes = _load_codes(row.get("stock_codes"))
    detail = _load_detail(row.get("detail_json"))
    from_group = row.get("from_group")
    to_group = row.get("to_group")
    created = _parse_created_at(row.get("created_at"))
    payload: dict[str, Any] = {
        "id": int(row.get("id") or 0),
        "op": op,
        "op_label": OP_LABELS.get(op, op or "未知操作"),
        "stock_codes": codes,
        "from_group": from_group,
        "to_group": to_group,
        "detail": detail,
        "actor": str(row.get("actor") or DEFAULT_ACTOR),
        # 时间**双轨并列**：2026-11-24 那次误判就是因为只有一列 UTC 没有标签。
        "created_at_utc": None,
        "created_at_local": None,
        "created_at_raw": None if created is not None else str(row.get("created_at") or ""),
        "summary": summarize(
            op, codes, from_group, to_group, detail, created_at=created,
        ),
    }
    if created is not None:
        payload["created_at_utc"] = created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload["created_at_local"] = created.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        logger.warning("自选操作日志的 created_at 无法解析：%r（id=%r）",
                       row.get("created_at"), row.get("id"))
    return payload


def summarize(
    op: str,
    codes: list[str],
    from_group: Any,
    to_group: Any,
    detail: Mapping[str, Any],
    *,
    created_at: datetime | None = None,
) -> str:
    """一句话中文描述 —— 前端直接显示，不再自己拼（措辞单源，`AGENTS R1`）。"""
    who = _codes_text(codes)
    src = _group_text(from_group)
    dst = _group_text(to_group)
    if op == OP_ADD:
        state = "已存在（幂等）" if detail.get("idempotent") else "新建"
        return f"{who} 加入「{dst}」（{state}）"
    if op == OP_COPY:
        return (
            f"{who} {_source_text(src)}复制到「{dst}」"
            f"（新复制 {detail.get('copied', 0)} 只"
            f"，已在目标组 {detail.get('already_in_target', 0)} 只"
            f"，未找到 {len(detail.get('not_found') or [])} 只"
            f"，黑名单跳过 {len(detail.get('blacklisted') or [])} 只）"
        )
    if op == OP_MOVE:
        return (
            f"{who} {_source_text(src)}移动到「{dst}」"
            f"（移动 {detail.get('moved', 0)} 只 / {detail.get('rows', 0)} 行"
            f"，已在目标组 {detail.get('already_in_target', 0)} 只"
            f"，未找到 {len(detail.get('not_found') or [])} 只）"
        )
    if op == OP_REMOVE:
        where = f"从「{dst}」" if dst else "从所有分组"
        return f"{who} {where}移出自选（{detail.get('rows', 0)} 行）"
    if op == OP_GROUP_CREATE:
        return f"新建分组「{dst}」"
    if op == OP_GROUP_RENAME:
        return f"重命名分组：{src} → 「{dst}」（{detail.get('moved_rows', 0)} 行跟着改名）"
    if op == OP_GROUP_DELETE:
        rule_note = "含连带删规则" if detail.get("rule_deleted") else "未删规则"
        return (
            f"删除分组「{dst or src}」"
            f"（删除 {detail.get('deleted_rows', 0)} 行，{rule_note}）"
        )
    if op == OP_REFRESH:
        return (
            f"按规则重跑刷新分组「{dst or src}」"
            f"（新增 {detail.get('added', 0)} 只 / 移除 {detail.get('removed', 0)} 只）"
        )
    if op == OP_SCREENING_SAVE:
        mode = "替换" if detail.get("replace_group") else "累加"
        return (
            f"筛选结果（#{detail.get('result_id')}）以「{mode}」方式保存 {len(codes)} 只到「{dst}」"
            f"（新插入 {detail.get('inserted', 0)} 只 / 覆盖 {detail.get('updated', 0)} 只"
            f" / 替换删掉 {detail.get('removed', 0)} 行）"
        )
    return f"{OP_LABELS.get(op, op)}：{who}"


# ─── 内部工具 ────────────────────────────────────────────────────────


def _codes_text(codes: list[str]) -> str:
    if not codes:
        return "分组"
    if len(codes) <= _PREVIEW_CODES:
        return "、".join(codes)
    head = "、".join(codes[:_PREVIEW_CODES])
    return f"{head} 等 {len(codes)} 只"


def _group_text(name: Any) -> str:
    text = str(name or "").strip()
    return text


def _source_text(from_group: str) -> str:
    """来源分组的措辞（None = 从该股票所在的所有分组里搬）。"""
    return f"从「{from_group}」" if from_group else "从所在分组"


def _load_codes(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("自选操作日志的 stock_codes 不是合法 JSON：%r", raw)
        return []
    if isinstance(parsed, list):
        return [str(code) for code in parsed]
    return []


def _load_detail(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("自选操作日志的 detail_json 不是合法 JSON：%r", raw)
        return {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _parse_created_at(raw: Any) -> datetime | None:
    """`CURRENT_TIMESTAMP` 写的是 **UTC** 的 naive 文本 → 补上 tzinfo=UTC。

    解析不了返回 `None`（由调用方把原文一并带出并打 WARNING），**绝不猜成"现在"**
    —— 猜出来的时间是这次误判的同类错误：看起来像证据，其实是编的。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in _SQLITE_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
