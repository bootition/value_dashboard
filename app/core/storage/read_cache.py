"""写进程持锁时的只读回退层（2026-11-20，用户实测「更新中几乎全员不可用」）。

## 为什么需要这一层

`DuckDBStore.read_connection()` 是 **open-per-query**：每个 `read_query()` 新开一个只读
连接。Windows 上外部写进程（自动更新子进程 / CLI 回填）独占 DuckDB 文件句柄时，新只读
连接直接 `IOException`，`duckdb_store` 立刻抛 `DuckDBReadLockedError`。于是**整个只读面
在更新期间全部 503**——包括「个股搜索」这种只读一张 6,285 行小表的功能。

用户的原话是：*「就算是慢了几天的数据，那我就不能打开看了吗？」* —— 说得对。
「数据正在更新」不等于「数据不可读」：文件里的行一直好着，缺的只是一个**已经打开**
的连接或一份**进程内副本**。

## 两种机制（别混用）

1. **全表快照**（`stock_meta_snapshot`）：`stock_meta` 只有 6,285 行，整表进内存
   完全无压力。锁住时用它照常搜索/取名称，语义由 `search_stock_meta()` 在内存里复刻。
2. **上次成功值**（`serve` / `serve_block`）：大表（日线、财务报表）不能整表缓存，
   但**单次响应**只有几 KB。按 `(库路径, 端点, 参数)` 记住上一次成功的结果，
   锁住时返回它并标注 `as_of`。

## 铁律：陈旧必须可被认出来

- `served_from`：`"live"` = 本次请求真的读了库；`"cache"` = 不是本次读的。
- `as_of`：那份数据**是什么时候读到的**（ISO8601，UTC）。
- `stale`：是否已经无法确认新鲜（即"因为锁住而回退"）。
- **读不到且没有旧值 → 如实抛错**（`DuckDBReadLockedError` 继续上抛，由
  `main.py` 的全局 handler 转 503）。这一层**只管把"有旧值"的情况救活**，
  绝不把"真的读不到"变成 200 假绿。
- 需要"降级为空块"的调用方用 `serve_block()`：空块**必须**带
  `data_freshness.degraded = True` + `reason_code`，与正常返回可区分。

## 配置

TTL 只是"多久重新读一次库"，不是"多久后数据算错"。更新窗口内 TTL 过期也不会报错，
只会把 `stale=True` 打到响应里。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.storage.duckdb_store import DuckDBReadLockedError

logger = logging.getLogger(__name__)

#: `served_from` 取值。前端据它决定是否显示"这是更新前的数据"横幅。
SERVED_FROM_LIVE = "live"
SERVED_FROM_CACHE = "cache"

#: 写进程持锁的稳定原因码（与 `main.py` 全局 handler 的 `reason_code` 同值）。
REASON_READ_LOCKED = "duckdb_read_locked"
#: 无旧值可回退时的降级原因码。
REASON_NO_SNAPSHOT = "no_cached_snapshot"

STALE_NOTICE = "数据正在自动更新中，这是上次成功读取的数据"
UNAVAILABLE_NOTICE = "数据正在自动更新中，本块暂时读不到上次成功的数据"

#: `stock_meta` 全表快照列。与 `GET /api/stock/{code}/info` 的 SELECT 同源
#: （多取 `pinyin` 之外的 `is_listed` 供搜索过滤），别在这里漏列再让详情去补查。
STOCK_META_COLUMNS: tuple[str, ...] = (
    "stock_code",
    "name",
    "pinyin",
    "exchange",
    "listing_date",
    "is_st",
    "is_suspended",
    "csrc_l1",
    "csrc_l2",
    "is_listed",
)

#: 搜索命中行返回的列，与 SQL 版 `SELECT stock_code, name, exchange, csrc_l1` 逐字一致。
SEARCH_RESULT_COLUMNS: tuple[str, ...] = ("stock_code", "name", "exchange", "csrc_l1")

DEFAULT_SNAPSHOT_TTL_SECONDS = 300.0
#: 进程内"上次成功值"表容量上限。超出按插入顺序丢最早的（只丢旧值，不影响正确性）。
MAX_LAST_GOOD_ENTRIES = 4096
#: 快照表容量上限（正常只有 1 条；测试里每个用例一个临时库，必须封顶）。
MAX_SNAPSHOT_ENTRIES = 32

# DuckDB 的 RE2 里 `\s` 只覆盖 ASCII 空白，`\p{Zs}` 不含制表符，两者并集
# `[\s\p{Zs}]+` 才同时覆盖 空格/制表/全角空格 U+3000/NBSP —— 这条正则是
# `app/web/api/stock_detail.py::_NAME_SPACE_RE` 的**内存版等价物**。
#
# Python 的 `str` 模式 `\s` 已经包含全部 Unicode 空格分隔符（`\p{Zs}`）与制表符，
# 因此内存侧只需 `\s+`；写 `\p{Zs}` 反而会因为 Python `re` 不认识该语法而报错。
# 等价性由 `tests/regression/test_cache_parity.py` 拿同一批样本双向核对。
_SPACE_RUN = re.compile(r"\s+")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_name(value: str | None) -> str:
    """去掉全部空白（含全角空格/NBSP/制表），与 SQL 侧 `regexp_replace(...,'g')` 同义。"""
    return _SPACE_RUN.sub("", value or "")


def _store_key(duck: Any) -> str:
    """缓存租户边界：同一进程里不同库（测试夹具/多 app）绝不串数据。"""
    path = getattr(duck, "db_path", None)
    return str(path) if path is not None else f"<anonymous:{id(duck)}>"


@dataclass(frozen=True)
class _Entry:
    value: Any
    as_of: str
    monotonic: float


# ─── 1. stock_meta 全表快照 ──────────────────────────────────────────


@dataclass(frozen=True)
class StockMetaSnapshot:
    """某一时刻 `stock_meta` 的完整副本（不可变使用）。"""

    rows: tuple[dict[str, Any], ...]
    as_of: str
    monotonic: float
    by_code: dict[str, dict[str, Any]]


_snapshot_guard = threading.Lock()
_snapshots: dict[str, StockMetaSnapshot] = {}
_snapshot_load_locks: dict[str, threading.Lock] = {}
#: 后台预热节流：每个库多久才允许再排一次预热线程。
_warm_last: dict[str, float] = {}


def _load_snapshot_rows(duck: Any) -> list[dict[str, Any]]:
    columns = ", ".join(STOCK_META_COLUMNS)
    return duck.read_query(f"SELECT {columns} FROM stock_meta")


def refresh_stock_meta_snapshot(duck: Any) -> StockMetaSnapshot:
    """同步重读 `stock_meta` 全表。失败时**如实抛出**（不吞异常）。

    单飞：同库并发调用只有一个线程真读，其余等它读完复用结果——避免更新窗口内
    几十个搜索请求同时去打一个已经锁住的文件。
    """
    key = _store_key(duck)
    with _snapshot_guard:
        load_lock = _snapshot_load_locks.setdefault(key, threading.Lock())
    with load_lock:
        rows = _load_snapshot_rows(duck)
        snapshot = StockMetaSnapshot(
            rows=tuple(rows),
            as_of=_now_iso(),
            monotonic=time.monotonic(),
            by_code={str(row["stock_code"]): row for row in rows if row.get("stock_code")},
        )
        with _snapshot_guard:
            _snapshots[key] = snapshot
            if len(_snapshots) > MAX_SNAPSHOT_ENTRIES:
                oldest = next(iter(_snapshots))
                _snapshots.pop(oldest, None)
                _snapshot_load_locks.pop(oldest, None)
                _warm_last.pop(oldest, None)
        return snapshot


def stock_meta_snapshot(
    duck: Any,
    *,
    ttl: float = DEFAULT_SNAPSHOT_TTL_SECONDS,
    reload: bool = True,
) -> StockMetaSnapshot | None:
    """取当前库的 `stock_meta` 快照；没有旧值又读不到时返回 `None`。

    - TTL 内直接命中（**请求路径不快照全表**：6,285 行只在 TTL 过期时读一次）；
    - TTL 过期 → 尝试重读；DuckDB 被持锁 / 读失败 → 回落到**旧快照**（不抛错）；
    - 从来没有成功过 → `None`，调用方据此如实报错。

    `reload=False` 只读内存、绝不打库（锁窗口内的兜底路径用这个）。
    """
    key = _store_key(duck)
    with _snapshot_guard:
        snapshot = _snapshots.get(key)
    if snapshot is not None and time.monotonic() - snapshot.monotonic <= ttl:
        return snapshot
    if not reload:
        return snapshot
    try:
        return refresh_stock_meta_snapshot(duck)
    except DuckDBReadLockedError as error:
        # 锁窗口：有旧值就用旧值（并会被标注 stale），没有就交给调用方如实报错。
        logger.info("stock_meta 快照刷新跳过（写进程持锁）: %s", error)
        return snapshot
    except Exception as error:  # noqa: BLE001 - 非锁类故障也不该让搜索整页 500
        if snapshot is not None:
            logger.warning("stock_meta 快照刷新失败，沿用旧副本: %s", error)
            return snapshot
        raise


def warm_stock_read_cache(duck: Any) -> None:
    """自动更新**开始前**预热 `stock_meta` 快照（失败仅记日志，不阻断更新）。

    与 `app/web/api/index_dashboard.py::warm_index_read_cache()` /
    `etf_strategy.py::warm_etf_read_cache()` 同一角色：在写锁落下之前把只读数据
    抄进进程内存，锁窗口期内全靠这份副本服务用户。
    """
    try:
        snapshot = refresh_stock_meta_snapshot(duck)
    except Exception as error:  # noqa: BLE001 - 预热失败绝不能挡住更新
        logger.warning("stock_meta 只读快照预热失败(非致命): %s", error)
        return
    logger.info("stock_meta 只读快照预热完成: %d 行 @ %s", len(snapshot.rows), snapshot.as_of)


def schedule_stock_meta_refresh(
    duck: Any, *, ttl: float = DEFAULT_SNAPSHOT_TTL_SECONDS
) -> None:
    """在当前快照过期后排一次**后台**预热，让请求路径不承担全表读的延迟。

    为什么不在请求里同步读：搜索本身只查回 20 行，为了兜底而每次多扫 6,285 行
    是把便宜路径变贵（"不要在请求路径上做重活"）。后台线程失败一律吞掉——
    它只是提前量，真正的兜底仍由锁窗口内的 `served_from="cache"` 标注。
    """
    key = _store_key(duck)
    now = time.monotonic()
    with _snapshot_guard:
        snapshot = _snapshots.get(key)
        if snapshot is not None and now - snapshot.monotonic <= ttl:
            return
        last = _warm_last.get(key)
        if last is not None and now - last <= ttl:
            return
        _warm_last[key] = now

    def _worker() -> None:
        try:
            refresh_stock_meta_snapshot(duck)
        except Exception as error:  # noqa: BLE001 - 预热线程不允许影响任何请求
            logger.debug("stock_meta 后台预热跳过: %s", error)

    threading.Thread(
        target=_worker, name="stock-meta-warm", daemon=True
    ).start()


# ─── 2. 内存侧搜索（复刻 SQL 语义） ─────────────────────────────────


def search_stock_meta(
    snapshot: StockMetaSnapshot,
    term: str,
    blacklisted: Iterable[str] = (),
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """在快照上复刻 `search_stocks()` 的 SQL 语义。

    逐条对齐（改任何一条都要同步改 `stock_detail.search_stocks`）：

    1. `is_listed IS TRUE` 才进结果；
    2. 代码：`stock_code LIKE '%<escaped term>%'` —— 转义后等价于**字面量子串**匹配
       （`%`/`_` 是字面量，不做通配符，与 `ESCAPE '\\'` 同义）；
    3. 名称：`regexp_replace(name,'[\\s\\p{Zs}]+','','g') LIKE '%<归一化 term>%'`；
    4. 黑名单：`exclusion_condition('stock_code', ...)` 的 `NOT IN` 等价于逐行剔除；
    5. `ORDER BY CASE 代码精确(0) / 名称精确(1) / 其它(2) END, stock_code`；
    6. `LIMIT 20`（排序**之后**才截断）。
    """
    blocked = frozenset(str(code) for code in blacklisted)
    normalized_term = normalize_name(term)
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for row in snapshot.rows:
        code = str(row.get("stock_code") or "")
        if not code or code in blocked:
            continue
        if not bool(row.get("is_listed")):
            continue
        name = normalize_name(row.get("name"))
        if term not in code and normalized_term not in name:
            continue
        if code == term:
            rank = 0
        elif name == normalized_term:
            rank = 1
        else:
            rank = 2
        matches.append((rank, code, row))
    matches.sort(key=lambda item: (item[0], item[1]))
    return [
        {column: row.get(column) for column in SEARCH_RESULT_COLUMNS}
        for _rank, _code, row in matches[:limit]
    ]


def stock_meta_by_code(duck: Any, codes: Sequence[str]) -> dict[str, dict[str, Any]] | None:
    """按代码从快照取 `stock_meta` 行；无快照返回 `None`（调用方必须如实报错）。"""
    snapshot = stock_meta_snapshot(duck)
    if snapshot is None:
        return None
    found: dict[str, dict[str, Any]] = {}
    for code in codes:
        row = snapshot.by_code.get(str(code))
        if row is not None:
            found[str(code)] = row
    return found


# ─── 3. 「上次成功值」响应级回退 ─────────────────────────────────────


_last_good: dict[tuple[Any, ...], _Entry] = {}
_last_good_guard = threading.Lock()


def freshness_meta(
    entry: _Entry | None,
    *,
    live: bool,
    stale: bool,
    reason_code: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """统一的陈旧标记。`as_of` 只在非 live 时有意义（live 就是"刚刚")。"""
    return {
        "served_from": SERVED_FROM_LIVE if live else SERVED_FROM_CACHE,
        "as_of": entry.as_of if entry is not None else None,
        "stale": stale,
        "reason_code": reason_code,
        "notice": notice,
    }


def entry_with_as_of(as_of: str) -> _Entry:
    """构造一个只带时间戳的条目（测试与预热器用，避免外部碰私有 `_Entry`）。"""
    return _Entry(value=None, as_of=as_of, monotonic=time.monotonic())


def remember(duck: Any, key: Sequence[Any], value: Any) -> _Entry:
    """登记一份"上次成功值"。预热器/测试用；正常请求走 `serve()` 自动登记。"""
    return _remember((_store_key(duck), *key), value)


def entry_for_snapshot(snapshot: StockMetaSnapshot) -> _Entry:
    """把全表快照包装成 `freshness_meta()` 认识的时间戳载体。"""
    return _Entry(value=snapshot.rows, as_of=snapshot.as_of, monotonic=snapshot.monotonic)


def _remember(key: tuple[Any, ...], value: Any) -> _Entry:
    entry = _Entry(value=value, as_of=_now_iso(), monotonic=time.monotonic())
    with _last_good_guard:
        _last_good[key] = entry
        while len(_last_good) > MAX_LAST_GOOD_ENTRIES:
            oldest = next(iter(_last_good))
            _last_good.pop(oldest, None)
    return entry


def serve(
    duck: Any,
    key: Sequence[Any],
    factory: Callable[[], Any],
) -> tuple[Any, dict[str, Any]]:
    """调 `factory()` 读库；写进程持锁时返回**该 key 上一次成功值**。

    返回 `(值, 陈旧标记)`。无旧值 → `DuckDBReadLockedError` 继续上抛。
    """
    full_key = (_store_key(duck), *key)
    with _last_good_guard:
        stale_entry = _last_good.get(full_key)
    try:
        value = factory()
    except DuckDBReadLockedError:
        if stale_entry is None:
            raise
        logger.info("DuckDB 持锁，回退上次成功值: %s @ %s", key, stale_entry.as_of)
        return stale_entry.value, freshness_meta(
            stale_entry, live=False, stale=True,
            reason_code=REASON_READ_LOCKED, notice=STALE_NOTICE,
        )
    entry = _remember(full_key, value)
    return value, freshness_meta(entry, live=True, stale=False)


def serve_block(
    duck: Any,
    key: Sequence[Any],
    factory: Callable[[], dict[str, Any]],
    *,
    empty: dict[str, Any],
) -> dict[str, Any]:
    """`serve()` 的"详情页各块"版本：无旧值时**降级成空块**而不是整页失败。

    为什么允许空块：详情页是十几个块拼出来的，任何一块取不到就 500/503 会让用户
    **连公司名都看不到**（用户实测正是如此）。降级块带 `data_freshness.degraded`
    与 `reason_code`，前端可以明确说"这块没取到"，与"这块本来就空"可区分。
    """
    try:
        payload, meta = serve(duck, key, factory)
    except DuckDBReadLockedError as error:
        logger.warning("DuckDB 持锁且无上次成功值，降级为空块: %s (%s)", key, error)
        payload = {name: value for name, value in empty.items()}
        meta = freshness_meta(
            None, live=False, stale=True,
            reason_code=REASON_NO_SNAPSHOT, notice=UNAVAILABLE_NOTICE,
        )
        payload["data_freshness"] = {**meta, "degraded": True}
        return payload
    payload["data_freshness"] = {**meta, "degraded": False}
    return payload


def clear_cache() -> None:
    """清空全部缓存（测试用；生产路径不该调用）。"""
    with _snapshot_guard:
        _snapshots.clear()
        _snapshot_load_locks.clear()
        _warm_last.clear()
    with _last_good_guard:
        _last_good.clear()
    with _content_guard:
        _content_entries.clear()


# ─── 4. 内容纪元键控的派生值缓存（2026-11-21，筛选探针）─────────────────
#
# 背景（`.planning/2026-screening-latency`）：正式库 `read_query()` 是
# open-per-query —— 每次查询要新开一条只读连接，实测**每次 ~75-85ms**（同一条
# 连接上后续查询只有 0.2-0.8ms）。于是筛选引擎里那条"混合报告期"探针
# （`snapshot_period_mismatches`，本身 ~90ms）实际要花 ~180ms，而且**每次运行
# 都重算一遍**，即使数据一个字都没变。
#
# 这一层只做一件事：把"只由库内容决定"的派生值按**内容令牌**记住。
#
# 三条铁律（假绿防线）：
# 1. **令牌取不到就不缓存**：`content_epoch` 读不到（返回 0：库还没记账 /
#    状态表没建 / 任何异常）时 `serve_by_content()` 直接现算，绝不写缓存。
#    "不知道数据变没变"必须等价于"当它变了"。
# 2. **令牌必须对内容敏感**：主令牌是 `content_epoch`（每个成功的 DuckDB 写
#    事务 +1，就地 UPDATE 也会变，见 `app/core/storage/content_epoch.py`），
#    调用方还会再拼一段**自己的**内容摘要。2026-11-25 独立审查证伪了旧的
#    "行数 / 最大报告期"廉价指纹：绕过 store 的裸 `duckdb.connect()` 就地改
#    **一个值**，行数、最大期、纪元全不变 → 回放假结果。现在的契约是：
#    调用方给的必须是**真正的内容摘要**（如 `SUM(hash(<判定读到的列>))`），
#    算不出/太慢就传 `None` → 不缓存；配套地 `_guarded_duckdb_connect` 在裸
#    可写连接打开时也推进纪元（同进程裸写不记账的那个口子）。
# 3. **库身份进键**：同一进程里测试夹具/多 app 的不同库绝不串数据，
#    与 `serve()` 的 `_store_key()` 同口径。

#: 内容键控缓存条目上限（正常只有个位数；测试里每个用例一个临时库，必须封顶）。
MAX_CONTENT_ENTRIES = 256


@dataclass(frozen=True)
class _ContentEntry:
    value: Any
    token: tuple[Any, ...]
    as_of: str
    monotonic: float


_content_guard = threading.Lock()
_content_entries: dict[tuple[Any, ...], _ContentEntry] = {}


def content_epoch_token(sqlite: Any) -> int:
    """当前库的内容纪元；**取不到一律返回 0**（= 调用方不得缓存）。

    `0` 在 `content_epoch.current()` 里的语义正是"读不到"，所以这里直接把
    `<=0` 归一成 `0`，让"不知道"与"没变化"永远不可混淆。
    """
    path = getattr(getattr(sqlite, "paths", None), "sqlite_path", None)
    if path is None:
        return 0
    from app.core.storage import content_epoch

    try:
        return max(0, int(content_epoch.current(path)))
    except Exception:  # noqa: BLE001 - 令牌读不到只影响命中率，绝不阻断筛选
        logger.debug("读取 content_epoch 失败，本次不缓存派生值", exc_info=True)
        return 0


def serve_by_content(
    duck: Any,
    key: Sequence[Any],
    token: Sequence[Any] | None,
    factory: Callable[[], Any],
) -> Any:
    """按**内容令牌**回放 `factory()` 的结果；令牌不可用/不一致就重算并覆盖。

    与 `serve()` 的区别（别混用）：`serve()` 是"写锁期间的陈旧回退"，允许返回
    旧值并标注 `stale=True`；这里是**纯加速**，命中与否都不改变新鲜度语义 ——
    令牌不一致就现算，没有"降级回放"这回事，所以调用方拿到的值永远等价于
    现算值。
    """
    if token is None or not all(part is not None for part in token):
        return factory()
    store_key = _store_key(duck)
    full_key = (store_key, *key)
    frozen = tuple(token)
    with _content_guard:
        entry = _content_entries.get(full_key)
        if entry is not None and entry.token == frozen:
            return entry.value
    value = factory()
    entry = _ContentEntry(value=value, token=frozen, as_of=_now_iso(), monotonic=time.monotonic())
    with _content_guard:
        _content_entries[full_key] = entry
        while len(_content_entries) > MAX_CONTENT_ENTRIES:
            _content_entries.pop(next(iter(_content_entries)), None)
    return value
