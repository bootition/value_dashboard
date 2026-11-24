"""自选分组注册表 —— 「分组存在」的唯一真源（用户态数据，存 SQLite）。

## 为什么需要这张表（2026-11-26 用户原话）

> 「现在这个自选分组是不是做得还不太好？除了按照筛选规则分组，我难道不能自己添加进
> 分组吗？……那种『点击加入自选』应该是加入默认自选列表，然后还可以自己移动到自己的
> 分组里。」

改造前分组是 `SELECT ... GROUP BY group_name` **派生**出来的，于是**空分组不可能存在**：
用户想「先建一个分组、再把股票挪进去」时，那个分组在建好那一刻就必须已经有股票，否则
侧栏里根本看不到它。这不是 UI 问题，是数据模型缺一张表。

现在 `watchlist_groups` 是分组存在性的唯一真源：
- 注册表里有行就**存在**，哪怕 `watchlist` 里 0 行（空分组合法）；
- `watchlist.group_name` 只是「这只股票当前在哪个分组」的**外键式**记录，
  不再承担「有哪些分组」的语义。

## 与旧数据的关系（自愈）

历史行（迁移前由 GROUP BY 派生出来的分组名、以及 `POST /api/screening/add_to_watchlist`
直接写进来的规则分组名）可能没有注册表行。`heal_registry()` 在读路径上把它们补登记：

- 该分组名出现过 `source_rule_id` → `kind='rule'`，恰好一个规则时把 `rule_id` 记下来；
- 否则 `kind='manual'`。

这一步刻意做成**读时自愈**而不是「只在迁移里做一次」：`screening.add_to_watchlist`
与 `watchlist.add` 都会写新的分组名，靠一次迁移兜不住。

## 默认分组

`我的自选`（`kind='default'`）：`add` 不传分组时落这里；**不可删除、不可改名**
（见 `delete_group()` / `rename_group()` 的守卫）。它是用户「点一下就加进去」的落点，
误删会让详情页/筛选结果页的「加入自选」失效，所以比其它分组多一层保护。

统计口径（`cnt` / `runs` / `manual` / `rule_ids`）只在 `_stats_by_group()` 里算一次，
`GET /list` 与 `GET /groups` 共用，不允许第二处再写一份 —— `AGENTS R1`。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from app.core.blacklist import exclusion_condition

logger = logging.getLogger(__name__)

#: 注册表物理表名（建表见 `storage/schema.py` 的 v21 迁移）。
GROUPS_TABLE = "watchlist_groups"

#: 默认分组名（用户可见、可写；`add` 不传分组时的落点）。
DEFAULT_GROUP_NAME = "我的自选"

#: 分组类型。`default` 只有 `我的自选` 一个；`rule` 来自筛选规则写入；`manual` 是自建。
KIND_DEFAULT = "default"
KIND_MANUAL = "manual"
KIND_RULE = "rule"
GROUP_KINDS = frozenset({KIND_DEFAULT, KIND_MANUAL, KIND_RULE})

#: 分组名长度上限。侧栏一行要塞下 250px 宽的名字 + 计数，超过 40 字就只剩省略号，
#: 所以直接在写入口拒绝，而不是让用户建出一个自己都认不出来的分组。
MAX_GROUP_NAME_LENGTH = 40

#: 控制字符与换行：分组名是单行展示文本，混进 `\n` 会把侧栏撑歪。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: 已经跑过建表 DDL 的库路径（进程级；理由见 `ensure_table()` 的注释）。
_ENSURED: set[str] = set()


class WatchlistGroupError(Exception):
    """分组写操作被拒绝（API 层直接映射成 HTTP 响应）。

    `detail` 是**结构化 detail**：`{reason_code, message}`。
    `message` 必须是中文 —— 前端 `friendlyErrorMessage()` 会把后端的中文主句原样
    展示给用户（`describeDetail()` 的优先级 1），于是「重名」「默认组不许删」这类
    原因能直接落到 toast 里，而不是掉进泛化的「请求参数有误」。
    """

    status_code = 400
    reason_code = "watchlist_group_invalid"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if reason_code is not None:
            self.reason_code = reason_code

    @property
    def detail(self) -> dict[str, Any]:
        return {"reason_code": self.reason_code, "message": self.message}


class GroupNameInvalid(WatchlistGroupError):
    status_code = 400
    reason_code = "watchlist_group_name_invalid"


class DefaultGroupProtected(WatchlistGroupError):
    status_code = 400
    reason_code = "watchlist_default_group_protected"


class GroupNotFound(WatchlistGroupError):
    status_code = 404
    reason_code = "watchlist_group_not_found"


class GroupConflict(WatchlistGroupError):
    status_code = 409
    reason_code = "watchlist_group_exists"


# ─── 建表与自愈 ────────────────────────────────────────────────────


def ensure_table(sqlite: Any) -> None:
    """建表 + 保证默认分组存在（幂等，可在任意进程里重复调用）。

    与 v21 迁移**同一份 DDL** 的兜底：迁移只在启动时跑，而这个模块会被
    `GET /list` 这类读路径调用；表不存在时抛 `no such table` 比"分组列表为空"
    更难排查，所以这里先建后查。

    每个库路径只真正执行一次（`_ENSURED`）：`GET /list` 是前端轮询的热路径，
    每次都开一个写事务做 `CREATE TABLE IF NOT EXISTS` 会在自动更新的同时白白
    抢 SQLite 写锁。表结构是进程级常量，缓存它的代价只是"进程启动后第一次"。
    """
    key = str(getattr(sqlite, "db_path", "")) or "default"
    if key in _ENSURED:
        return
    sqlite.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GROUPS_TABLE} (
            name       TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,
            rule_id    INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # 迁移前就被写成 'default' 的历史手动分组没有独立含义（旧前端的内部 key），
    # 但用户可能已经往里放了股票：登记成 manual，让它在侧栏可见、可改名、可删。
    # 刻意**不**合并进默认组 —— 回填只搬结构不搬数据，改名留给用户自己决定。
    sqlite.execute(
        f"INSERT OR IGNORE INTO {GROUPS_TABLE} (name, kind, rule_id, created_at) "
        "VALUES (?, ?, NULL, CURRENT_TIMESTAMP)",
        [DEFAULT_GROUP_NAME, KIND_DEFAULT],
    )
    _ENSURED.add(key)


def normalize_group_name(raw: str | None) -> str:
    """去空白 + 折叠内部连续空白（分组名是展示键，`a  b` 与 `a b` 不该是两个组）。"""
    text = (raw or "").replace("\u3000", " ")
    text = _CONTROL_CHARS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_new_name(raw: str | None, *, allow_default: bool = False) -> str:
    """建组/改名的名字校验（空名、超长、控制字符、默认组重名）。"""
    name = normalize_group_name(raw)
    if not name:
        raise GroupNameInvalid("分组名不能为空，请输入一个名字（如「重点观察」）。")
    if len(name) > MAX_GROUP_NAME_LENGTH:
        raise GroupNameInvalid(
            f"分组名最多 {MAX_GROUP_NAME_LENGTH} 个字，当前 {len(name)} 个字；请缩短后再试。"
        )
    if not allow_default and name == DEFAULT_GROUP_NAME:
        raise GroupConflict(
            f"「{DEFAULT_GROUP_NAME}」是默认分组，不能重复创建，也不能用作其它分组的名字。"
        )
    return name


def registry_rows(sqlite: Any) -> list[dict]:
    """注册表原始行（按默认组优先、再按登记时间）。"""
    ensure_table(sqlite)
    return sqlite.query(
        f"SELECT name, kind, rule_id, created_at FROM {GROUPS_TABLE} "
        "ORDER BY CASE WHEN kind = ? THEN 0 ELSE 1 END, created_at, name",
        [KIND_DEFAULT],
    )


def registry_kinds(sqlite: Any) -> dict[str, str]:
    """`{分组名: kind}`（存在性判断的唯一入口）。"""
    return {str(row["name"]): str(row["kind"]) for row in registry_rows(sqlite)}


def group_kind(sqlite: Any, name: str) -> str | None:
    """分组类型；`None` = 注册表里没有这个分组。"""
    key = normalize_group_name(name)
    if not key:
        return None
    return registry_kinds(sqlite).get(key)


def is_default_group(sqlite: Any, name: str) -> bool:
    """是否默认分组（名字与 kind 双判：迁移把名字钉死，kind 兜住改名历史）。"""
    key = normalize_group_name(name)
    if not key:
        return False
    if key == DEFAULT_GROUP_NAME:
        return True
    return group_kind(sqlite, key) == KIND_DEFAULT


def register_group(
    sqlite: Any,
    name: str,
    kind: str,
    *,
    rule_id: int | None = None,
    conn: Any = None,
) -> bool:
    """登记一个分组；已存在则**不动**（返回 False）。

    幂等：`INSERT OR IGNORE`。`conn` 用来把登记并入调用方的事务（`add` 要保证
    「登记分组」与「写自选行」同生共死）。
    """
    key = normalize_group_name(name)
    if not key:
        return False
    if kind not in GROUP_KINDS:
        raise ValueError(f"unknown watchlist group kind: {kind}")
    ensure_table(sqlite)
    sql = (
        f"INSERT OR IGNORE INTO {GROUPS_TABLE} (name, kind, rule_id, created_at) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP)"
    )
    params = [key, kind, rule_id]
    if conn is not None:
        return conn.execute(sql, params).rowcount > 0
    return sqlite.execute(sql, params) > 0


def heal_registry(sqlite: Any) -> list[str]:
    """把 `watchlist` 里有、注册表里没有的 `group_name` 补登记（读时自愈）。

    返回本次补登记的分组名。全程 `INSERT OR IGNORE`，并发下重复执行也不会重复插。
    """
    ensure_table(sqlite)
    orphan_rows = sqlite.query(
        f"""
        SELECT w.group_name AS group_name,
               COUNT(DISTINCT w.source_rule_id) AS rule_count,
               MIN(w.source_rule_id) AS single_rule_id
          FROM watchlist w
         WHERE w.group_name IS NOT NULL
           AND TRIM(w.group_name) <> ''
           AND w.group_name NOT IN (SELECT name FROM {GROUPS_TABLE})
         GROUP BY w.group_name
        """
    )
    healed: list[str] = []
    for row in orphan_rows:
        name = normalize_group_name(row["group_name"])
        if not name:
            continue
        rule_count = int(row["rule_count"] or 0)
        kind = KIND_RULE if rule_count > 0 else KIND_MANUAL
        rule_id = int(row["single_rule_id"]) if rule_count == 1 and row["single_rule_id"] else None
        if register_group(sqlite, name, kind, rule_id=rule_id):
            healed.append(name)
    if healed:
        logger.info("自选分组注册表自愈：补登记 %d 个分组 %s", len(healed), healed)
    return healed


# ─── 统计口径（唯一实现）────────────────────────────────────────────


def _stats_by_group(
    sqlite: Any, blocked_codes: Iterable[str] | None = None
) -> tuple[dict[str, dict], dict[str, list[int]]]:
    """按分组算 `cnt` / `runs` / `manual`（与 `GET /list` 的行口径完全一致）。

    `blocked_codes`：黑名单股票在自选列表里被隐藏（历史行不删），侧栏计数必须
    同口径剔除，否则会出现「侧栏 3 只、表里 2 行」。
    """
    blocked = sorted({str(code) for code in (blocked_codes or ())})
    where, params = exclusion_condition("stock_code", blocked)
    where_sql = f" WHERE {where}" if where else ""
    rows = sqlite.query(
        "SELECT group_name, COUNT(*) AS cnt, "
        "COUNT(DISTINCT source_result_id) AS runs, "
        "SUM(CASE WHEN source_result_id IS NULL THEN 1 ELSE 0 END) AS manual "
        f"FROM watchlist{where_sql} GROUP BY group_name",
        params,
    )
    stats = {
        str(row["group_name"]): {
            "cnt": int(row["cnt"] or 0),
            "runs": int(row["runs"] or 0),
            "manual": int(row["manual"] or 0),
        }
        for row in rows
    }
    rule_rows = sqlite.query(
        "SELECT group_name, source_rule_id FROM watchlist "
        f"WHERE source_rule_id IS NOT NULL{(' AND ' + where) if where else ''} "
        "GROUP BY group_name, source_rule_id ORDER BY group_name, source_rule_id",
        params,
    )
    rules: dict[str, list[int]] = {}
    for row in rule_rows:
        rules.setdefault(str(row["group_name"]), []).append(int(row["source_rule_id"]))
    return stats, rules


def list_groups(
    sqlite: Any, *, blocked_codes: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """注册表里的**全部分组**（含 0 只的空分组）+ 统计口径，一张表说清。

    这是 `GET /api/watchlist/groups` 与 `GET /api/watchlist/list -> groups` 的
    唯一实现：调用方不许再自己 `GROUP BY watchlist` 当分组真相源。
    """
    heal_registry(sqlite)
    stats, rules = _stats_by_group(sqlite, blocked_codes)
    groups: list[dict[str, Any]] = []
    for row in registry_rows(sqlite):
        name = str(row["name"])
        stat = stats.get(name, {"cnt": 0, "runs": 0, "manual": 0})
        groups.append({
            "group_name": name,
            "kind": str(row["kind"]),
            "rule_id": int(row["rule_id"]) if row["rule_id"] is not None else None,
            "cnt": stat["cnt"],
            "runs": stat["runs"],
            "manual": stat["manual"],
            "rule_ids": rules.get(name, []),
            "is_default": str(row["kind"]) == KIND_DEFAULT,
        })
    return groups


def default_group_name() -> str:
    """默认分组名（前端/测试取值的唯一来源，别在别处再写一遍中文字面量）。"""
    return DEFAULT_GROUP_NAME


# ─── 写操作 ────────────────────────────────────────────────────────


def create_group(sqlite: Any, raw_name: str | None) -> dict[str, Any]:
    """新建自建分组（`kind='manual'`）。重名 → 409；空名/超长 → 400。"""
    name = validate_new_name(raw_name)
    ensure_table(sqlite)
    if group_kind(sqlite, name) is not None:
        raise GroupConflict(f"已经有一个叫「{name}」的分组了，请换一个名字。")
    register_group(sqlite, name, KIND_MANUAL)
    return {
        "status": "ok",
        "group": {
            "group_name": name,
            "kind": KIND_MANUAL,
            "rule_id": None,
            "cnt": 0,
            "runs": 0,
            "manual": 0,
            "rule_ids": [],
            "is_default": False,
        },
    }


def rename_group(sqlite: Any, old_name: str, new_raw_name: str | None) -> dict[str, Any]:
    """重命名分组：注册表行与 `watchlist.group_name` **同一事务**一起改。

    默认分组拒绝（不可改名）→ 400；目标名已存在 → 409；分组不存在 → 404。
    """
    old = normalize_group_name(old_name)
    if not old:
        raise GroupNameInvalid("要改名的分组不能为空。")
    ensure_table(sqlite)
    kind = group_kind(sqlite, old)
    if kind is None:
        raise GroupNotFound(f"找不到分组「{old}」，可能已被删除，请刷新后重试。")
    if kind == KIND_DEFAULT or old == DEFAULT_GROUP_NAME:
        raise DefaultGroupProtected(
            f"「{DEFAULT_GROUP_NAME}」是默认分组，不能改名；可以新建一个分组再移动股票。"
        )
    new = validate_new_name(new_raw_name)
    if new == old:
        return {
            "status": "ok",
            "group": old,
            "new_name": new,
            "moved_rows": 0,
            "unchanged": True,
        }
    if group_kind(sqlite, new) is not None:
        raise GroupConflict(f"已经有一个叫「{new}」的分组了，请换一个名字。")
    with sqlite.transaction() as conn:
        conn.execute(
            f"UPDATE {GROUPS_TABLE} SET name = ? WHERE name = ?",
            [new, old],
        )
        cursor = conn.execute(
            "UPDATE watchlist SET group_name = ? WHERE group_name = ?",
            [new, old],
        )
        moved = max(0, int(cursor.rowcount))
    logger.info("自选分组改名：%s → %s（%d 行）", old, new, moved)
    return {
        "status": "ok",
        "group": old,
        "new_name": new,
        "moved_rows": moved,
        "unchanged": False,
    }


def delete_registry_group(sqlite: Any, name: str, *, conn: Any = None) -> bool:
    """删注册表行（`DELETE /groups/{name}` 在删完 watchlist 行之后调用）。

    返回是否真的删掉了注册表行。**只删注册表行，不动 watchlist**
    —— 顺序由调用方控制（先删分组行，再让规则守卫统计剩余引用）。

    `conn=`：传进来就用调用方的事务。2026-11-27 自查发现：
    `DELETE /groups` 原来是"删 watchlist 行（一个事务）→ 删注册表行（另一个事务）"，
    中间失败会留下**"0 只但还在侧栏"的幽灵分组**。现在两步共用同一个事务。
    """
    key = normalize_group_name(name)
    if not key:
        return False
    ensure_table(sqlite)
    if conn is not None:
        return conn.execute(f"DELETE FROM {GROUPS_TABLE} WHERE name = ?", [key]).rowcount > 0
    return sqlite.execute(f"DELETE FROM {GROUPS_TABLE} WHERE name = ?", [key]) > 0


# ─── 单只/批量状态 ─────────────────────────────────────────────────


def stock_status(sqlite: Any, stock_code: str) -> dict[str, Any]:
    """某只股票当前在哪些自选分组里（详情页按钮三态的唯一事实来源）。

    黑名单股票**不算**自选：列表里本来就看不到它（`list_watchlist` 剔除），
    若这里算进去，详情页会显示「已在自选」而自选页找不到它。
    """
    ensure_table(sqlite)
    from app.core.blacklist import blacklisted_code_set

    blocked = blacklisted_code_set(sqlite)
    rows = sqlite.query(
        "SELECT group_name FROM watchlist WHERE stock_code = ? ORDER BY group_name",
        [stock_code],
    )
    registry = registry_kinds(sqlite)
    groups: list[str] = []
    for row in rows:
        name = str(row["group_name"] or "")
        if not name:
            continue
        if stock_code in blocked:
            continue
        if name not in registry:
            # 未登记的历史分组：登记后再计入（与 `/groups` 同一自愈口径）。
            register_group(sqlite, name, KIND_MANUAL)
            registry[name] = KIND_MANUAL
        if name not in groups:
            groups.append(name)
    return {
        "stock_code": stock_code,
        "in_watchlist": bool(groups),
        "groups": groups,
        "default_group": DEFAULT_GROUP_NAME,
        "blacklisted": stock_code in blocked,
    }


def ensure_group_exists(sqlite: Any, name: str) -> str:
    """把调用方给的分组名归一成注册表里存在的名字；不存在 → 404。

    `move` / `rename` 这类「目标必须已存在」的操作走这里：用户是在下拉框里选分组，
    名字对不上说明前端状态过期了，应当明确告知而不是替用户建一个空分组。
    """
    key = normalize_group_name(name)
    if not key:
        raise GroupNameInvalid("请选择目标分组。")
    if group_kind(sqlite, key) is None:
        raise GroupNotFound(f"找不到分组「{key}」，可能已被删除，请刷新后重试。")
    return key


def ensure_group_for_write(sqlite: Any, name: str, *, conn: Any = None) -> str:
    """`add` 用的宽松口径：名字存在就直接用，不存在就**登记成自建分组**。

    为什么不拒绝：`POST /api/watchlist/add` 从一开始就接受任意分组名（历史脚本、
    旧前端、`screening.add_to_watchlist` 的规则分组名都走这条），突然 400 会把
    已经能用的调用方打红。宽松不等于不留痕 —— 每个写进去的名字都会在注册表里
    留下 `kind='manual'` 的一行，随后可改名、可删、可显示为 0 只的空分组。
    """
    key = normalize_group_name(name)
    if not key:
        key = DEFAULT_GROUP_NAME
    if len(key) > MAX_GROUP_NAME_LENGTH:
        raise GroupNameInvalid(
            f"分组名最多 {MAX_GROUP_NAME_LENGTH} 个字，当前 {len(key)} 个字；请缩短后再试。"
        )
    if group_kind(sqlite, key) is None:
        register_group(sqlite, key, KIND_MANUAL, conn=conn)
    return key
