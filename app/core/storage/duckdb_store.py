"""DuckDB 连接管理器 - open-per-query 模式

审查问题1修订：Web 进程不持有长期 DuckDB 连接，每个查询打开→执行→关闭。
CLI 写操作通过应用级写锁文件协调。

DuckDB 进程模型：单进程读写，或多进程只读，二者互斥。
- Web 进程：open-per-query，每次查询打开连接、执行、关闭，不长期占据文件锁
- CLI 写操作：抢锁 → 打开写连接 → 执行 → 释放锁
- backup restore：独占操作，前置条件是 Web 进程退出
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import secrets
import threading
import time
import weakref
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from app.core.storage.maintenance import assert_writes_allowed
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv

logger = logging.getLogger(__name__)

# 空/半写锁文件宽限期（秒）：与 update_lock.py 同口径，超过即视为死锁
_STALE_MALFORMED_LOCK_SECONDS = 5.0

# raw_response_archive 热表轮转阈值：任一条件满足即把当前 active 关闭
# 为新分区并另开空表。5GB 来自 2026-09-01 正式库 26GB 大表提交峰值
# 24.5GB 的教训；在该阈值下提交仍保持亚秒级/低内存。
_RAW_ARCHIVE_ROTATE_BYTES = 5 * 1024 * 1024 * 1024
_RAW_ARCHIVE_ROTATE_ROWS = 100_000
_RAW_ARCHIVE_ROTATE_DAYS = 31

_RAW_ARCHIVE_COLUMNS = (
    "raw_response_hash", "source", "fetch_time", "payload",
    "api_version", "integrity_verified", "created_at",
)


def _recreate_raw_archive_view(conn: Any) -> None:
    """把登记表里的分区 UNION 成 `raw_response_archive_all` 视图。

    ⚠️ 2026-11-23 生产事故（更新卡死数小时）：本函数原来**无条件相信登记表**，
    把每一行都拼进 `UNION ALL`。而登记表里有 4 条指向**已被删除的实体表**
    （`raw_response_archive_20260917_044324` 等，表没了但登记行还在），
    于是视图创建失败 → **每一次价格落库都报**
    `Catalog Error: Table with name raw_response_archive_20260917_044324 does not exist`
    → 单只股票价格写入 100% 失败、整轮永远跑不完、日志刷屏几小时。

    修法：**以库里的实体表为准**过滤登记行（`duckdb_tables()`），并打 WARNING
    把死链暴露出来（不静默）。登记表是"记账"，实体表才是"事实"——两者不一致时
    以事实为准，绝不让一条脏记账拖垮整条写路径。
    """
    rows = conn.execute(
        "SELECT partition_table FROM raw_response_archive_partitions "
        "ORDER BY created_at, partition_table"
    ).fetchall()
    if not rows:
        return
    existing = {
        str(r[0]) for r in conn.execute(
            "SELECT table_name FROM duckdb_tables()"
        ).fetchall()
    }
    usable = [str(row[0]) for row in rows if str(row[0]) in existing]
    dangling = [str(row[0]) for row in rows if str(row[0]) not in existing]
    if dangling:
        logger.warning(
            "归档分区登记表有 %d 条指向不存在的实体表，已跳过（视图只用存在的 %d 个）：%s。"
            "这些登记行是脏记账，应清理（否则每次重建视图都要跳一次）",
            len(dangling), len(usable), ", ".join(dangling[:5]),
        )
    if not usable:
        return
    selects = " UNION ALL ".join(
        f"SELECT {', '.join(_RAW_ARCHIVE_COLUMNS)} "
        f'FROM "{name}"'
        for name in usable
    )
    conn.execute(
        f"CREATE OR REPLACE VIEW raw_response_archive_all AS {selects}"
    )


def _rotate_raw_archive_if_needed(conn: Any) -> None:
    row = conn.execute(
        "SELECT COALESCE(row_count, 0), COALESCE(estimated_bytes, 0), created_at "
        "FROM raw_response_archive_partitions "
        "WHERE partition_table = 'raw_response_archive'"
    ).fetchone()
    if row is None:
        return
    count, total_bytes, oldest = row
    now = datetime.now(UTC)
    if oldest is not None and getattr(oldest, "tzinfo", None) is None:
        oldest = oldest.replace(tzinfo=UTC)
    age_days = (now - oldest).days if oldest is not None else 0
    if (
        int(total_bytes or 0) < _RAW_ARCHIVE_ROTATE_BYTES
        and int(count or 0) < _RAW_ARCHIVE_ROTATE_ROWS
        and age_days < _RAW_ARCHIVE_ROTATE_DAYS
    ):
        return
    # 2026-09-04 失控轮转修复：旧实现把 registry 里活跃行的
    # row_count/estimated_bytes/created_at 原样保留（先 UPDATE 一个尚不存在的
    # TS 名再 ON CONFLICT DO NOTHING 插新行），阈值一旦触发（如 5GB）后
    # 每次写入都会再轮转，1 秒内两次轮转同名表直接 Catalog Error
    # 崩溃（当日实锤：416 个单行 TS 表 + auto-update failed）。
    # 修正：先把活跃 registry 行改名为 TS 名并落终值，再插入全新的
    # 计数器归零活跃行；表名冲突时加序号避免同秒重名。
    base = f"raw_response_archive_{now.strftime('%Y%m%d_%H%M%S')}"
    used = {
        r[0] for r in conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE table_name LIKE ?",
            [f"{base}%"],
        ).fetchall()
    }
    partition = base if base not in used else f"{base}_{len(used)}"
    conn.execute(
        f'ALTER TABLE raw_response_archive RENAME TO "{partition}"'
    )
    conn.execute(
        """
        CREATE TABLE raw_response_archive (
            raw_response_hash VARCHAR PRIMARY KEY,
            source            VARCHAR NOT NULL,
            fetch_time        TIMESTAMP NOT NULL,
            payload           BLOB,
            api_version       VARCHAR,
            integrity_verified BOOLEAN DEFAULT FALSE,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # 1) 活跃 registry 行 → 改名为 TS 名并关闭（置真实终值）
    conn.execute(
        """UPDATE raw_response_archive_partitions
           SET partition_table = ?,
               closed_at = ?,
               row_count = ?,
               estimated_bytes = ?
           WHERE partition_table = 'raw_response_archive'""",
        [partition, now, int(count), int(total_bytes)],
    )
    # 2) 若该活跃行历史上缺失（防御），补一条 TS 行
    conn.execute(
        """INSERT INTO raw_response_archive_partitions
           (partition_table, created_at, closed_at, row_count, estimated_bytes)
           SELECT ?, ?, ?, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM raw_response_archive_partitions WHERE partition_table = ?
           )""",
        [partition, now, now, int(count), int(total_bytes), partition],
    )
    # 3) 全新活跃行：计数器归零、created_at=now（无 ON CONFLICT——若已存在
    #    说明状态异常，让事务显式失败好过静默保留脏计数器）
    conn.execute(
        """INSERT INTO raw_response_archive_partitions
           (partition_table, created_at, closed_at, row_count, estimated_bytes)
           VALUES ('raw_response_archive', ?, NULL, 0, 0)""",
        [now],
    )
    _recreate_raw_archive_view(conn)


def archive_raw_response_if_absent(
    conn: Any,
    *,
    raw_response_hash: str,
    source: str,
    fetch_time: Any,
    payload: bytes | None,
    api_version: str | None,
) -> bool:
    """Append a raw response payload unless its content hash already exists.

    Schema v16 splits the archive into a small hot ``raw_response_archive``
    for new writes and ``raw_response_archive_history`` for legacy BLOBs.
    Writers are serialized by the application write lock and DuckDB's
    single-writer rule, so a primary-key probe followed by a plain INSERT is
    race-free and keeps the same first-writer-wins semantics as the old
    ``ON CONFLICT DO NOTHING`` statement.
    """
    _rotate_raw_archive_if_needed(conn)
    existing = conn.execute(
        "SELECT 1 FROM raw_response_archive_all WHERE raw_response_hash = ?",
        [raw_response_hash],
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        """INSERT INTO raw_response_archive
           (raw_response_hash, source, fetch_time, payload, api_version,
            integrity_verified)
           VALUES (?, ?, ?, ?, ?, TRUE)""",
        [raw_response_hash, source, fetch_time, payload, api_version],
    )
    if payload:
        conn.execute(
            """INSERT INTO raw_response_archive_valid_hash (raw_response_hash)
               VALUES (?) ON CONFLICT DO NOTHING""",
            [raw_response_hash],
        )
        conn.execute(
            """UPDATE raw_response_archive_partitions
               SET row_count = COALESCE(row_count, 0) + 1,
                   estimated_bytes = COALESCE(estimated_bytes, 0) + ?
               WHERE partition_table = 'raw_response_archive'""",
            [len(payload)],
        )
    return True





def _is_process_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否仍在运行（Windows 兼容）"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # SYNCHRONIZE access
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


class DuckDBWriteLockError(Exception):
    """DuckDB 写锁获取失败"""


class DuckDBReadLockedError(Exception):
    """外部写进程正占用 DuckDB 文件，当前无法新建只读连接。"""


# =============================================================================
# 索引 ↔ 堆不变量：检测与自愈（2026-11-25，业务概览每轮原生崩溃的真因）
# =============================================================================
#
# ## 现象（副本上实测，见 docs/reports 本轮报告）
# `BusinessOverviewUpdater.update_stock('002185')` 每轮都把更新子进程打死，
# **没有 traceback、没有报告 JSON、job 永久 running**。逐级探针把崩溃点缩到
# `DuckDBStore.transaction()` 的 `conn.commit()`（看门狗每 20ms dump 全线程栈，
# 最后一条样本就是 commit 行），退出码 **0xC0000409**（`__fastfail`；不是 Python
# 异常，`PYTHONFAULTHANDLER=1` + `-X dev` 也拿不到栈，因为 fail-fast 绕过 SEH）。
# commit 里抛出的真正错误是 DuckDB 的内部 FATAL：
#
#     FatalException: FATAL Error: Invalid Input Error:
#     Failed to delete all rows from index. Only deleted 0 out of 200 rows.
#
# ## 它是什么（2026-11-25 定位到具体索引）
# 坏的是 `business_breakdown` 的**二级索引** `idx_business_breakdown_stock_report`
# (stock_code, report_date)：堆里 002185 的 200 行在索引里一条都没有。DELETE 的
# 索引维护在提交时逐条核对，核对不上就抛 FATAL —— 而且 FATAL 之后 DuckDB 的
# 连接状态不可再信，rollback/close 可能再撞一次 fail-fast 直接 abort，所以进程
# "猝死"。触发面是**文件级不变量被破坏**（写库中途被打断 / 双写者 / 拷活动库），
# 不是调用方的 SQL 写法：同一 DDL + 同一批数据在**新建库**上三种 DELETE/INSERT
# 形态全部正常（`.planning/20261125-bo-crash/freshdb.py`）。
#
# ## 怎么修：`reindex_table()` 优先，`rebuild_table()` 兜底
# 索引由堆数据遍历生成 ⇒ 重建索引即可恢复"条目 ↔ 行"逐行对应，且**不动堆数据**：
# 实测 101 万行 `business_breakdown` 重建二级索引 **0.45s**（整表重建 4.2s 且会让
# 追加型库文件的表数据再存一份，因此只当索引重建无效时的兜底）。
#
# ## 为什么不做"检测"
# 二级索引**读不出来**：DuckDB 1.5.5 **会**选 ART 索引（`EXPLAIN ANALYZE` 显示
# `Type: Index Scan`；22.9M 行实测 0.49ms → DROP 后 24.57ms → 重建 0.42ms = 50 倍），
# 但**普通 `EXPLAIN` 在 1.5.5 里不打印扫描类型**，所以"全是 SEQ_SCAN"什么也没证明。
# 何况这批二级索引被删的正当性从来不是"优化器没选索引"，而是**列与真实查询形状
# 不匹配**（我们做的是 JOIN / `IN (子查询)` / 范围扫描，没有 `col = 字面量`），且实测
# 删后真实查询不变慢（最慢 1.43 倍、绝对值 <2ms）；反过来，索引坏了也没有任何
# SQL 能把它读出来 —— `INSERT ... ON CONFLICT DO NOTHING` 探针只查主键 ART，
# 对二级索引**完全盲**（实测探针报 `consistent=True`，紧接着同一笔 DELETE 照样
# FATAL，本轮就是被这个假绿骗过一次）。唯一"测得出来"的动作就是真 DELETE，
# 而那就是猝死点本身。既然重建只要 0.45s（一轮 ~900s），就把检测问题换成
# **幂等重建**：每轮第一次写库前重建一次，代价 0.05%。
_INDEX_INCONSISTENCY_MARKERS: tuple[str, ...] = (
    "Failed to delete all rows from index",
    "Failed to update all rows in index",
    "Failed to insert all rows into index",
    "Failed to delete all rows from index.",
)


def is_index_inconsistency_error(error: BaseException | str) -> bool:
    """是否为"索引 ↔ 堆失联"这一类 DuckDB 内部 FATAL。

    只认 DuckDB 自己那句话：把范围放宽会把并发/IO 错误误判成需要重建表。
    """
    text = error if isinstance(error, str) else str(error)
    return any(marker in text for marker in _INDEX_INCONSISTENCY_MARKERS)


class DuckDBIndexInconsistencyError(RuntimeError):
    """表索引与堆数据失联（DuckDB 内部 FATAL）；必须整表重建才能恢复。"""

    def __init__(self, table: str, detail: str) -> None:
        super().__init__(f"{table}: {detail}")
        self.table = table
        self.detail = detail


def _is_external_file_lock(message: str, *, lock_active: bool = False) -> bool:
    """Windows 下外部 DuckDB 写进程持锁时，新只读连接会直接失败。

    必须区分"外部写进程独占"与"路径/权限等永久性 IO 错误"：两者都可能
    包含 ``Cannot open file``（2026-09-17 实测）。只有应用级更新锁文件表明
    确有外部写进程时，才把泛化消息当成锁竞争；否则继续退避重试并如实抛出。
    """
    specific_lock = (
        "另一个程序正在使用此文件" in message
        or "File is already open in" in message
    )
    return specific_lock or (lock_active and "Cannot open file" in message)


# 进程级连接配置覆盖（2026-11-12 P2）。
#
# 旧实现用 ContextVar 保存 ``memory_limit()`` 的覆盖值——但 ContextVar 不会
# 传播到 ThreadPoolExecutor 的工作线程：更新子进程里主线程设了
# ``update.duckdb_memory_limit``，价格/财务流水线的工作线程却回落到
# ``database.*`` 配置，于是同一数据库文件出现两套 configuration，抛出
# ``Connection Error: Can't open a connection to same database file with a
# different configuration``（2026-11-12 14:01 生产日志实锤一次）。
#
# 变更理由：更新/备份/重建都跑在**专用子进程**里，一个进程同一时刻只有一套
# 覆盖值，进程级全局正是它要的语义，而且所有线程自然共享同一份配置。
# Web 进程**禁止**调用 memory_limit()（见其 docstring），因此不会出现两个
# 请求互相污染覆盖值的情况；仍加锁保护进入/退出的成对读写。
_MEMORY_LIMIT_OVERRIDE: dict[str, str] = {}
_MEMORY_LIMIT_OVERRIDE_LOCK = threading.Lock()


# =============================================================================
# 只读连接复用（2026-11-24，测试提速：全量 34min → 目标 ~18min）
# =============================================================================
#
# ## 为什么要复用
# 本机实测（同一台机器、同一份库文件、`.probe/p2.py`）：
#   * `duckdb.connect(..., read_only=True, config=...)` + `close()`，**每次**都是
#     冷开：**15.4 ms**（小库）/ **49.3 ms**（正式库量级，见任务书实测）
#   * 只要进程内**已经有一条活着的连接**（Database 实例已建、catalog 已加载），
#     再开一条同样配置的连接：**0.22 ms** —— 差 70~200 倍
#   * 打开的连接上直接跑 SELECT：**0.21 ms**
# 也就是说 open-per-query 里 99% 的时间花在「建 Database 实例 + 加载 catalog」，
# 而不是查询本身。把只读连接按线程复用即可把这部分摊掉。
#
# ## 三条硬约束（都是实测出来的，不是推测）
# 1. **同进程 read_only 与读写连接不能共存**（`.probe/p1.py`）：
#      `ConnectionException: Can't open a connection to same database file with a
#       different configuration than existing connections`
#    ⇒ 写路径开始前**必须**先把缓存只读连接关掉（否则 `_connect_writer` 只能靠
#      ~90s 退避硬等）。因此写路径入口先 `invalidate`，拿到写锁前就关闭。
# 2. **跨进程不共享**：本进程只要持有 read_only 句柄，**另一个进程**（自动更新
#    子进程 / CLI）就以读写方式**打不开**这个文件，实测重试 6 次（~3s）全部
#    `IO Error: Cannot open file ... 另一个程序正在使用此文件`，父进程一关就好
#    （`.probe/p3.py`）。
#    ⇒ 这正是 open-per-query 存在的理由：**长期持有只读句柄会挡住自动更新**。
#      所以缓存**默认只在 `VD_ENV=test` 打开**（隔离临时库、无外部写进程），
#      正式/预发布环境保持 open-per-query 原样；要用请显式设
#      `VD_DUCKDB_READ_CONN_TTL=<秒>`（以及 database.duckdb_read_conn_cache_ttl）。
# 3. **DuckDB Python 连接非线程安全** ⇒ 每个线程一条，绝不复用别人的连接。
#
# ## 失效模型
# * **generation 计数器**（每库文件一份，进程级）：任何写路径成功/开始时 +1；
#   缓存条目记下自己出生时的 generation，取用时对不上就丢弃重开。
# * **物理关闭**：写路径进入时把本进程内该库文件的**所有**缓存只读连接关掉
#   （含别的 store 实例的——生产里 `app.state.duck` 与临时 `DuckDBStore(...)`
#   会同时存在），正在跑查询的置 `retired`，由持有线程归还时关闭（绝不从别的
#   线程 close 一个在用连接）。
# * **旁证守卫**：每次取用还核对 ①库文件 `(st_dev, st_ino)` 没变（库被
#   restore 换掉/删掉就重开）；②没有应用级写锁文件（`.value-dashboard.update.lock`
#   / `.duckdb.write.lock` 存在即放弃缓存，走老路径——宁可慢，不许读到更新前的
#   旧页）。
# * **空闲 TTL**（默认 2s）+ 上限 16 条：死线程留下的连接会被下次取用顺手扫掉，
#   不会无限期占着文件句柄。
_READ_CONN_TTL_ENV = "VD_DUCKDB_READ_CONN_TTL"
_DEFAULT_READ_CONN_TTL_TEST = "2.0"
_READ_CONN_IDLE_TTL_SECONDS = 2.0
_READ_CONN_MAX_IDLE = 16
_WRITE_DRAIN_WAIT_SECONDS = 2.0

_READ_CACHE_REGISTRY_LOCK = threading.Lock()
# 每库文件的写纪元：写路径 +1，缓存条目按它对账。
_WRITE_GENERATIONS: dict[str, int] = {}
# 每库文件活跃的读缓存（弱引用：store 被回收后自动出表）。
#
# 粒度选"每个 store 实例一份"：实测按库文件共享并不能再减少只读连接数
# （2026-11-24 的 12 文件样本：共享前后 ro_calls 都是 989）——因为只读连接
# 的数量由"写完再读"的周期数决定，而不是由实例个数决定，写路径的失效本来
# 就是按库文件全局生效的。按实例实现更简单：谁开的连接谁关，close/析构
# 不需要引用计数。
_LIVE_READ_CACHES: dict[str, weakref.WeakSet] = {}


def _cache_key(db_path: Path | str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(db_path)))


def _write_generation(key: str) -> int:
    """当前写纪元（进程级、按库文件）。写路径一发生就 +1。"""
    with _READ_CACHE_REGISTRY_LOCK:
        return _WRITE_GENERATIONS.get(key, 0)


def _bump_write_generation(key: str) -> int:
    with _READ_CACHE_REGISTRY_LOCK:
        value = _WRITE_GENERATIONS.get(key, 0) + 1
        _WRITE_GENERATIONS[key] = value
        return value


def _register_read_cache(key: str, cache: _ReadConnectionCache) -> None:
    with _READ_CACHE_REGISTRY_LOCK:
        _LIVE_READ_CACHES.setdefault(key, weakref.WeakSet()).add(cache)


def _live_read_caches(key: str) -> list[_ReadConnectionCache]:
    with _READ_CACHE_REGISTRY_LOCK:
        registered = _LIVE_READ_CACHES.get(key)
        return list(registered) if registered else []


def _file_identity(db_path: Path) -> tuple[int, int] | None:
    """库文件身份指纹：文件被替换（restore / 重建）后指纹必变。"""
    try:
        info = os.stat(str(db_path))
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _application_write_lock_present(db_path: Path) -> bool:
    """是否有**应用级**写锁文件存在（存在即放弃缓存复用，走 open-per-query）。

    只看文件存在、不判活主：残留锁最多让本进程退化成老路径（慢一点），
    而"以为没人写"却拿着缓存只读句柄，代价是挡死更新子进程 或 读到旧页。
    """
    parent = db_path.parent
    return (
        (parent / ".duckdb.write.lock").exists()
        or (parent / ".value-dashboard.update.lock").exists()
    )


def _close_quietly(conn: Any) -> None:
    with contextlib.suppress(Exception):
        conn.close()


# -- 「裸 connect」守卫 -------------------------------------------------------
# 同进程 read_only 与读写连接不能共存（见上文约束 1）。缓存只读连接一旦存在，
# 本进程里**任何**绕过 store 的裸 `duckdb.connect(path)`（测试自建表、脚本、
# 冷库工具……）都会撞 "different configuration"。与其去逐个改调用点，这里装
# 一个薄包装：裸 connect 进来之前先把该库文件的缓存只读连接**关掉**。
# store 自己开的连接走 `_INTERNAL_CONNECT` 线程本地标记，不触发清理
# （否则每个新线程热身都会把别的线程的缓存打断）。
#
# 2026-11-25（独立审查）：这个包装原来**只关连接、不记账**，于是裸写方的就地
# UPDATE 不会推进 `content_epoch` —— 按纪元键控的内容缓存会回放旧值（假绿）。
# 现在同一个包装里再加一条：裸**可写**连接打开时推进纪元（`reason` 可区分）。
# 2026-11-25 起守卫由 `DuckDBStore.__init__` **无条件**安装（不再只在开启只读
# 连接复用的环境装）：它现在承担的是"内容账"职责，与连接复用开关无关。
_CONNECT_GUARD_LOCK = threading.Lock()
_CONNECT_GUARD_LOCAL = threading.local()
_ORIGINAL_DUCKDB_CONNECT = duckdb.connect
_CONNECT_GUARD_TARGET = _ORIGINAL_DUCKDB_CONNECT
_CONNECT_GUARD_INSTALLED = False
#: 规范化 duckdb 文件路径 → 该库的 sqlite（内容纪元落点）。由 `DuckDBStore.__init__`
#: 登记；裸 connect 要用它才知道"这条 duckdb 的内容账记在哪个 sqlite 里"。
_CONTENT_EPOCH_TARGETS: dict[str, Any] = {}


def _register_content_epoch_target(db_path: Any, sqlite_path: Any) -> None:
    with _CONNECT_GUARD_LOCK:
        _CONTENT_EPOCH_TARGETS[_cache_key(db_path)] = sqlite_path


def _install_connect_guard() -> None:
    """装一次就够（进程级）。"""
    global _CONNECT_GUARD_INSTALLED, _CONNECT_GUARD_TARGET
    with _CONNECT_GUARD_LOCK:
        if _CONNECT_GUARD_INSTALLED:
            return
        if duckdb.connect is not _ORIGINAL_DUCKDB_CONNECT:
            # 安装前已被别人（测试）换过：照旧串下去，别把人家的探针弄丢。
            _CONNECT_GUARD_TARGET = duckdb.connect
        _CONNECT_GUARD_INSTALLED = True
        duckdb.connect = _guarded_duckdb_connect  # type: ignore[assignment]


def _connect_read_only(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """`duckdb.connect(database, read_only=False, config=None)` 的只读判定。"""
    if "read_only" in kwargs:
        return bool(kwargs["read_only"])
    return bool(args[0]) if args else False


def _note_bare_writable_connect(
    database: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """裸**可写**连接打开时，立刻推进内容纪元（让按纪元键控的缓存全部失效）。

    2026-11-25（独立审查证伪"缓存不会假绿"）：`content_epoch` 原先只在
    `write_connection()` / `transaction()` 成功提交后 +1，于是
    **绕过 store 的裸 `duckdb.connect(path)` 写**（仓库里确实有这种脚本）既关了
    缓存只读连接、又能就地改值而不留任何痕迹。审查构造实测：只
    `UPDATE income_statement SET revenue=123`（行数 / 最大报告期 / 纪元全不变）
    → 混合报告期探针回放旧名单 → 筛选静默多算 1 只。

    为什么只能"**打开即记账**"，做不到"看到写语句才记账"：`duckdb.connect()`
    返回 C 扩展对象 `DuckDBPyConnection`，`execute/executemany/sql/append` 都是
    只读属性，挂不上钩子；包一层 Python 代理会改变 `isinstance(...)`、
    `.cursor()`、`with conn`、链式 `conn.execute(...).fetchall()` 等一大批语义，
    而仓库里有 20+ 处裸 connect 用法各异 —— 代理的回归风险远大于收益。
    于是取保守解：**只要有人以可写方式裸开这条库，就当内容可能变了**。
    多失效的代价只是一次重算；少失效的代价是假绿。

    精度边界（必须明说）：
    * 覆盖**本进程内**的裸写（守卫只在装了它的进程里生效）；
    * 跨进程写脚本（自动更新子进程 / `python scripts/xxx.py`）不在覆盖范围 ——
      抓它们靠的是**内容摘要**（`ScreeningEngine._mixed_report_content_fingerprint`，
      对判定读到的列做 `SUM(hash(...))`），那才是主防线；
    * 记账发生在连接**打开时**（早于任何语句），所以它只能缩短"写前缓存"的
      窗口，不能单独构成证明 —— 与内容摘要一起才闭环。
    """
    if _connect_read_only(args, kwargs):
        return
    if not isinstance(database, (str, os.PathLike)):
        return
    target = str(database)
    if not target.strip() or target == ":memory:":
        return
    with _CONNECT_GUARD_LOCK:
        sqlite_path = _CONTENT_EPOCH_TARGETS.get(_cache_key(database))
    if sqlite_path is None:
        return
    from app.core.storage import content_epoch

    content_epoch.bump(sqlite_path, reason="bare_duckdb_connect")


def _guarded_duckdb_connect(database: Any = ":memory:", *args: Any, **kwargs: Any) -> Any:
    if not getattr(_CONNECT_GUARD_LOCAL, "internal", False):
        with contextlib.suppress(Exception):
            for cache in _live_read_caches(_cache_key(database)):
                cache.invalidate()
        # 关缓存连接只是"别把写挡住"；能让内容缓存不假绿的是这行记账。
        with contextlib.suppress(Exception):
            _note_bare_writable_connect(database, args, kwargs)
    return _CONNECT_GUARD_TARGET(database, *args, **kwargs)


def _store_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
    """store 自己开连接：不触发裸 connect 守卫的缓存清理。"""
    _CONNECT_GUARD_LOCAL.internal = True
    try:
        return duckdb.connect(database, *args, **kwargs)
    finally:
        _CONNECT_GUARD_LOCAL.internal = False


class _CachedReadConnection:
    """一条线程私有、可复用的只读连接。"""

    __slots__ = (
        "conn", "generation", "identity", "in_use", "last_used", "retired", "thread_id",
    )

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        generation: int,
        identity: tuple[int, int] | None,
    ) -> None:
        self.conn = conn
        self.generation = generation
        self.identity = identity
        self.thread_id = threading.get_ident()
        self.in_use = True
        self.last_used = time.monotonic()
        self.retired = False


class _ReadConnectionCache:
    """一个 store 实例的只读连接缓存：**一线程一条**，不跨线程共享连接。

    * 取用（``take``）：只有"同一线程 + 同 generation + 同库文件 + 无写锁 + 未过 TTL"
      才复用，否则关掉重开 —— 任何一条不满足都退回 open-per-query 语义。
    * 归还（``release``）：仍在用/纪元没变则留在缓存里等下次用；写了就关掉。
    * 失效（``invalidate``）：写路径调用。空闲的直接关；在用的打 ``retired`` 标记，
      由持有线程归还时关闭（**绝不**从别的线程 close 一个正在跑的连接）。
    """

    __slots__ = (
        "__weakref__", "_closed", "_condition", "_db_path", "_entries", "_lock", "_ttl",
    )

    def __init__(self, *, db_path: Path, ttl_seconds: float) -> None:
        self._db_path = db_path
        self._ttl = max(0.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._entries: dict[int, _CachedReadConnection] = {}
        self._closed = False

    # -- 读路径 ---------------------------------------------------------------
    def take(self, *, generation: int) -> _CachedReadConnection | None:
        """取当前线程的缓存连接；不可用则关掉旧的返回 ``None``。"""
        thread_id = threading.get_ident()
        now = time.monotonic()
        closing: list[Any] = []
        reused: _CachedReadConnection | None = None
        with self._lock:
            if self._closed:
                return None
            self._sweep_locked(now, closing)
            entry = self._entries.get(thread_id)
            if entry is not None and not entry.in_use:
                if (
                    not entry.retired
                    and entry.generation == generation
                    and (now - entry.last_used) <= self._ttl
                    and not _application_write_lock_present(self._db_path)
                    and _file_identity(self._db_path) == entry.identity
                ):
                    entry.in_use = True
                    entry.last_used = now
                    reused = entry
                else:
                    del self._entries[thread_id]
                    closing.append(entry.conn)
        for conn in closing:
            _close_quietly(conn)
        return reused

    def adopt(self, conn: duckdb.DuckDBPyConnection, *, generation: int) -> _CachedReadConnection | None:
        """把刚打开的只读连接登记进缓存（只在调用线程里用）。"""
        identity = _file_identity(self._db_path)
        if identity is None or _application_write_lock_present(self._db_path):
            return None
        entry = _CachedReadConnection(conn, generation=generation, identity=identity)
        closing: list[Any] = []
        with self._lock:
            if self._closed:
                return None
            previous = self._entries.get(entry.thread_id)
            if previous is not None:
                closing.append(previous.conn)
            self._entries[entry.thread_id] = entry
        for stale in closing:
            _close_quietly(stale)
        return entry

    def release(self, entry: _CachedReadConnection, *, generation: int) -> None:
        """归还连接：这一轮写过了、或缓存已关，就直接关闭。"""
        close_now = False
        with self._lock:
            current = self._entries.get(entry.thread_id)
            if (
                self._closed
                or entry.retired
                or current is not entry
                or entry.generation != generation
            ):
                if current is entry:
                    del self._entries[entry.thread_id]
                close_now = True
            else:
                entry.in_use = False
                entry.last_used = time.monotonic()
            self._condition.notify_all()
        if close_now:
            _close_quietly(entry.conn)

    def discard(self, entry: _CachedReadConnection) -> None:
        """语句抛错后丢弃这条连接（可能已不可用），下次重开。"""
        with self._lock:
            if self._entries.get(entry.thread_id) is entry:
                del self._entries[entry.thread_id]
            self._condition.notify_all()
        _close_quietly(entry.conn)

    # -- 失效 / 关闭 ----------------------------------------------------------
    def invalidate(self, *, wait_seconds: float = 0.0) -> None:
        """写路径调用：关闭空闲连接，在用的打 retired 标记（归还时关闭）。"""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        closing: list[Any] = []
        with self._lock:
            for thread_id, entry in list(self._entries.items()):
                if entry.in_use:
                    entry.retired = True
                else:
                    del self._entries[thread_id]
                    closing.append(entry.conn)
            while wait_seconds > 0 and any(e.in_use for e in self._entries.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
                for thread_id, entry in list(self._entries.items()):
                    if not entry.in_use:
                        del self._entries[thread_id]
                        closing.append(entry.conn)
        for conn in closing:
            _close_quietly(conn)

    def close(self) -> None:
        closing: list[Any] = []
        with self._lock:
            self._closed = True
            closing = [entry.conn for entry in self._entries.values()]
            self._entries.clear()
            self._condition.notify_all()
        for conn in closing:
            _close_quietly(conn)

    def _sweep_locked(self, now: float, closing: list[Any]) -> None:
        """顺手回收：空闲过 TTL 的、以及超出上限后最旧的那些。"""
        for thread_id, entry in list(self._entries.items()):
            if not entry.in_use and (now - entry.last_used) > self._ttl:
                del self._entries[thread_id]
                closing.append(entry.conn)
        idle = [e for e in self._entries.values() if not e.in_use]
        if len(idle) > _READ_CONN_MAX_IDLE:
            idle.sort(key=lambda item: item.last_used)
            for entry in idle[: len(idle) - _READ_CONN_MAX_IDLE]:
                if self._entries.get(entry.thread_id) is entry:
                    del self._entries[entry.thread_id]
                    closing.append(entry.conn)


@atexit.register
def _close_all_read_caches() -> None:
    """解释器退出兜底：所有还活着的缓存只读连接都关掉。"""
    with contextlib.suppress(Exception):
        with _READ_CACHE_REGISTRY_LOCK:
            caches = [cache for group in _LIVE_READ_CACHES.values() for cache in list(group)]
            _LIVE_READ_CACHES.clear()
        for cache in caches:
            with contextlib.suppress(Exception):
                cache.close()


class DuckDBStore:
    """DuckDB 连接管理器

    - read_query(): open-per-query 读操作，无需锁
    - write_transaction(): 获取写锁后执行写操作
    """

    def __init__(self, *, paths: DatabasePathSet) -> None:
        validated = paths.validate()
        self._path_set = validated
        self._db_path = validated.duckdb_path
        self._path_key = _cache_key(self._db_path)
        self._lock_path = self._db_path.parent / ".duckdb.write.lock"
        self._memory_limit = self._load_memory_limit()
        # DuckDB 不允许同一进程内对同一文件同时存在不同配置的连接。
        # Web 进程既处理普通查询、也会执行后台 summary 全量扫描，因此这里
        # 必须为所有连接提供一套固定的默认配置（memory/threads/insertion
        # order 三项都固定），并禁止 Web 路径再使用 memory_limit() 制造差异
        # 配置。不同配置并发会触发 ConnectionException，普通请求重试约 28s
        # 后 500（2026-09-02 start.log 实锤）。
        self._threads = self._load_database_setting("duckdb_threads", "2")
        self._preserve_insertion_order = self._load_database_setting(
            "duckdb_preserve_insertion_order", "false"
        )

        # 「裸 connect」守卫（2026-11-25 起**无条件**安装）：
        # * 关缓存只读连接 —— 没有缓存时是空操作；
        # * 裸可写连接推进内容纪元 —— 与连接复用开关无关，正式环境同样需要
        #   （否则进程内裸写方会让按纪元键控的内容缓存回放假值）。
        # 先登记"这条 duckdb 的账记在哪个 sqlite"，再装包装。
        _register_content_epoch_target(self._db_path, self._path_set.sqlite_path)
        _install_connect_guard()

        # 只读连接缓存（见模块内「只读连接复用」一节）。`None` = 保持
        # open-per-query 老行为。
        self._read_cache = self._build_read_cache()

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._revalidate()

    def _build_read_cache(self) -> _ReadConnectionCache | None:
        """按环境/配置决定要不要复用只读连接；默认只有 test 环境开。

        正式/预发布**默认不开**：本进程长期持有只读句柄会让自动更新子进程
        以读写方式打不开库文件（实测 3s 内 6 次 `Cannot open file` 全失败），
        那是比"测试慢 16 分钟"严重得多的事故。要在别的环境试，显式设
        `VD_DUCKDB_READ_CONN_TTL=<秒>`。
        """
        ttl = self._load_read_cache_ttl()
        if ttl <= 0:
            return None
        cache = _ReadConnectionCache(db_path=self._db_path, ttl_seconds=ttl)
        _register_read_cache(self._path_key, cache)
        return cache

    def _load_read_cache_ttl(self) -> float:
        raw = os.environ.get(_READ_CONN_TTL_ENV)
        if raw is None:
            default = (
                _DEFAULT_READ_CONN_TTL_TEST
                if self._path_set.env is VdEnv.TEST
                else "0"
            )
            raw = self._load_database_setting("duckdb_read_conn_cache_ttl", default)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    def close(self) -> None:
        """关闭本 store 缓存的只读连接（幂等；不影响之后新建连接）。"""
        cache = getattr(self, "_read_cache", None)
        if cache is not None:
            cache.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    @staticmethod
    def _load_memory_limit() -> str:
        """Read memory limit: env override > user config > conservative fallback.

        VD_DUCKDB_MEMORY_LIMIT is an operational escape hatch for one-off
        maintenance jobs (backup/export/rebuild) that legitimately need a
        larger limit than the daily service default.
        """
        import os
        env_value = os.environ.get("VD_DUCKDB_MEMORY_LIMIT")
        if env_value:
            return env_value
        return DuckDBStore._load_database_setting("duckdb_memory_limit", "14GB")

    @staticmethod
    def _load_database_setting(name: str, default: str) -> str:
        """Load one database.* DuckDB connection setting with a safe fallback."""
        try:
            import importlib
            config_module = importlib.import_module("app.core.config")
            database_cfg = config_module.Config.current().get_value("database", {})
            value = database_cfg.get(name) if isinstance(database_cfg, dict) else None
            if value is not None:
                return str(value)
        except Exception:
            pass
        return default

    def _connection_config(self) -> dict[str, str]:
        with _MEMORY_LIMIT_OVERRIDE_LOCK:
            override = dict(_MEMORY_LIMIT_OVERRIDE)
        return {
            "memory_limit": override.get("memory_limit") or self._memory_limit,
            "threads": override.get("threads") or self._threads,
            "preserve_insertion_order": (
                override.get("preserve_insertion_order") or self._preserve_insertion_order
            ),
        }

    @contextlib.contextmanager
    def memory_limit(
        self,
        value: str,
        *,
        threads: int | None = None,
        preserve_insertion_order: bool | None = None,
    ) -> Iterator[None]:
        """Temporarily override new connection settings **process-wide**.

        2026-11-12 P2：覆盖值从 ContextVar 改为进程级全局，主线程设置的预算
        对 ThreadPoolExecutor 工作线程同样生效；否则工作线程连接会回落到
        ``database.*`` 配置并触发 DuckDB different-configuration 错误。

        仅供专用子进程/CLI 维护任务使用（如自动更新、备份、重建）。
        **禁止在 Web 服务进程内使用**：DuckDB 不允许同一进程对同一文件
        同时存在不同配置的连接，后台扫描与普通请求并发时会触发
        different-configuration 错误。Web 后台任务必须使用 database.*
        统一配置。

        大扫描在低内存预算下需要同步调低并行度并关闭 insertion order，
        否则会以 OOM 失败：线程越多，每个线程持有的哈希/排序缓存越大。
        """
        override = {"memory_limit": value}
        if threads is not None:
            override["threads"] = str(int(threads))
        if preserve_insertion_order is not None:
            override["preserve_insertion_order"] = (
                "true" if preserve_insertion_order else "false"
            )
        # 进程级全局：主线程设置的预算必须对 ThreadPoolExecutor 工作线程
        # 同样生效，否则同一文件会因配置不同而连接失败（见模块顶部说明）。
        previous = self._swap_override(override)
        try:
            yield
        finally:
            self._swap_override(previous)

    @staticmethod
    def _swap_override(value: dict[str, str]) -> dict[str, str]:
        """Atomically replace the process-wide connection override; return previous."""
        with _MEMORY_LIMIT_OVERRIDE_LOCK:
            previous = dict(_MEMORY_LIMIT_OVERRIDE)
            _MEMORY_LIMIT_OVERRIDE.clear()
            _MEMORY_LIMIT_OVERRIDE.update(value)
            return previous

    def _revalidate(self) -> None:
        validated = self._path_set.validate()
        if validated.duckdb_path != self._db_path:
            raise PathIsolationError("DuckDB path identity changed after validation")

    @contextlib.contextmanager
    def read_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """打开一个只读查询连接，用完即关。

        open-per-query 模式：不持有长期连接，避免阻塞 CLI/更新子进程的写操作
        （跨进程：本进程持有只读句柄时，外部写进程**打不开**库文件，见模块内
        「只读连接复用」实测）。

        竞态（reports/76 P1-1）：Windows 上写线程持事务期间，另一线程
        connect 可能短暂失败 "file already open"（同进程亦可能，DuckDB
        平台特性）。固定 5×0.5s 重试在批量写事务（数秒级）前必然耗尽。
        此处改为指数退避（0.25s 起，3s 封顶，共 12 次，约 28s 窗口），
        写锁活跃时另加 2s 基础等待，骑跨批量写事务而非让调用方 500。
        """
        conn, _ = self._open_read_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _open_read_connection(self) -> tuple[duckdb.DuckDBPyConnection, bool]:
        """打开一条读查询连接，返回 ``(连接, 是否真正的 read_only)``。

        第二个返回值给只读连接缓存用：回退出来的**读写**连接绝不能进缓存
        （否则本进程会长期握着读写句柄，把外部写进程挡在门外）。
        """
        self._revalidate()
        # 2026-08-14 红队 P3：库文件不存在是永久性条件（首次启动的
        # 最小初始化会先建库），不进入竞态重试；立即失败让调用方
        # 决定建库路径，避免 read_only 打开缺失文件空耗 ~28s 退避窗口。
        if not self._db_path.exists():
            raise FileNotFoundError(f"database file not found: {self._db_path}")
        last_error: Exception | None = None
        lock_active = False
        lock_owned_here = False
        try:
            from app.core.storage.update_lock import (
                update_lock_active,
                update_lock_owned_by_current_process,
            )

            lock_active = update_lock_active(self._db_path)
            lock_owned_here = update_lock_owned_by_current_process(self._db_path)
        except Exception:
            lock_active = False
            lock_owned_here = False
        attempts = 12
        allow_same_process_rw = False
        for attempt in range(attempts):
            try:
                # 2026-08-14 红队 P2-2：读连接显式 read_only，读进程不参与
                # 文件锁竞争（DuckDB 允许单写者 + 多只读并发）。
                # 例外：本进程已持有读写连接时 DuckDB 禁止同文件不同配置
                # （"different configuration"）——文件锁本就由本进程持有，
                # 回退同配置读写连接（同进程多连接共享锁，安全）。
                conn = _store_connect(
                    str(self._db_path),
                    read_only=not allow_same_process_rw,
                    config=self._connection_config(),
                )
            except duckdb.ConnectionException as error:
                last_error = error
                if "different configuration" in str(error):
                    allow_same_process_rw = True
            except Exception as error:
                last_error = error
                if _is_external_file_lock(str(error), lock_active=lock_active):
                    if lock_owned_here or allow_same_process_rw:
                        # 本进程就是更新属主：这是自己的写事务与并发读连接
                        # 争用同一个 DuckDB 文件锁，退避重试即可，不能按
                        # "外部写进程持锁"立刻失败（2026-10-20 修复）。
                        last_error = error
                    else:
                        # 外部写进程（auto-update/CLI）持锁时 Windows 不允许
                        # 新建只读连接；与其空转 ~28s 后 500，不如立即抛专用
                        # 异常，让上层返回缓存旧值或 503 提示。
                        raise DuckDBReadLockedError(str(error)) from error
            else:
                return conn, not allow_same_process_rw
            base_delay = 0.25 * (2 ** min(attempt, 4))
            delay = min(base_delay, 3.0)
            if lock_active and not lock_owned_here and attempt == 0:
                delay += 2.0
            time.sleep(delay)
        assert last_error is not None
        raise last_error

    @contextlib.contextmanager
    def _query_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """``read_query`` 用的连接：优先复用本线程的缓存只读连接。

        语义与 ``read_connection()`` 等价 —— 拿不到可复用的连接就走
        open-per-query（含"本进程有写连接时回退读写连接"、外部持锁抛
        ``DuckDBReadLockedError`` 等全部原有分支），一行不变。
        """
        cache = self._read_cache
        entry = None if cache is None else cache.take(generation=_write_generation(self._path_key))
        if entry is not None:
            try:
                yield entry.conn
            except BaseException:
                # 语句抛错 → 连接状态可疑，直接丢弃重开（读查询无副作用，
                # 但绝不复用一条报过错的连接）。
                cache.discard(entry)
                raise
            cache.release(entry, generation=_write_generation(self._path_key))
            return
        conn, read_only = self._open_read_connection()
        adopted = (
            cache.adopt(conn, generation=_write_generation(self._path_key))
            if cache is not None and read_only
            else None
        )
        if adopted is None:
            try:
                yield conn
            finally:
                conn.close()
            return
        try:
            yield conn
        except BaseException:
            cache.discard(adopted)
            raise
        cache.release(adopted, generation=_write_generation(self._path_key))

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        """执行单条只读查询，返回字典列表。"""
        with self._query_connection() as conn:
            statements = conn.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("read_query accepts exactly one SELECT statement")
            cursor = conn.execute(sql, params or [])
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def _invalidate_read_caches(self, *, wait_seconds: float = 0.0) -> int:
        """写路径专用：让本进程内**所有**该库文件的缓存只读连接失效并关闭。

        为什么要跨 store：生产里 `app.state.duck` 是长期实例，但各处适配器
        还会临时 `DuckDBStore(paths=...)`；测试里 fixture store 与临时 store
        也并存。只要**任意一个**实例还留着 read_only 句柄，同进程的写连接就
        会撞 "different configuration"，所以按库文件（而不是按实例）清。
        """
        generation = _bump_write_generation(self._path_key)
        for cache in _live_read_caches(self._path_key):
            cache.invalidate(wait_seconds=wait_seconds)
        return generation

    @contextlib.contextmanager
    def _write_lock(self, timeout: float = 30.0) -> Iterator[None]:
        """获取应用级写锁文件（Windows 兼容），超时则抛出异常

        使用 O_CREAT | O_EXCL 原子性创建锁文件。如果文件已存在，
        检查持有者 PID 是否仍存活——若已退出则接管锁，否则等待。

        A dead PID is reclaimed, but an apparently live lock is never expired by
        age: long-running writes must retain serialization. The owner token
        prevents a former holder from removing a lock acquired after replacement.
        """
        self._revalidate()
        assert_writes_allowed(self._db_path)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout

        while True:
            try:
                # 原子性创建：文件已存在则抛 FileExistsError
                fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o644,
                )
                try:
                    token = secrets.token_urlsafe(24)
                    os.write(fd, f"pid={os.getpid()}\ntime={time.time()}\ntoken={token}\n".encode())
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                # 锁文件已存在，检查持有者是否还活着
                try:
                    content = self._lock_path.read_text()
                    lines = content.strip().split("\n")
                    holder_pid = int(lines[0].split("=")[1])
                    if not _is_process_alive(holder_pid):
                        # 持有者已退出，清理僵尸锁并重试
                        self._lock_path.unlink(missing_ok=True)
                        continue
                except (ValueError, IndexError, OSError):
                    # 锁文件格式损坏：可能是"刚创建、尚未写入"的获取竞态
                    # （2026-08-14 红队 P3-9）。新鲜损坏锁视为获取进行中，
                    # 等待重试；超过宽限期才清理，避免双写。
                    try:
                        age = time.time() - self._lock_path.stat().st_mtime
                    except OSError:
                        age = float("inf")
                    if age > _STALE_MALFORMED_LOCK_SECONDS:
                        self._lock_path.unlink(missing_ok=True)
                        continue

                if time.monotonic() > deadline:
                    raise DuckDBWriteLockError(
                        f"无法获取 DuckDB 写锁，可能正在执行其他写操作。"
                        f"请稍后重试或检查 {self._lock_path}"
                    ) from None
                time.sleep(0.5)

        try:
            yield
        finally:
            try:
                # Never let an old holder remove a replacement lock.
                if f"token={token}" in self._lock_path.read_text(encoding="utf-8"):
                    self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @contextlib.contextmanager
    def write_connection(self, timeout: float = 30.0) -> Iterator[duckdb.DuckDBPyConnection]:
        """获取写锁后打开写连接，用完关闭并释放锁。

        2026-08-14 红队 P2-3：锁已到手后 connect 仍可能瞬时失败
        （残留句柄/文件系统抖动），与读侧对齐做有界重试再抛出。
        """
        # 2026-11-24：拿写锁**之前**先让缓存只读连接失效并关闭。同进程
        # read_only + 读写连接共享一个库文件时 DuckDB 直接抛
        # "different configuration"（实测），不先关就只能靠 _connect_writer
        # 的 ~90s 退避硬等。返回值给 finally 对账用。
        self._invalidate_read_caches(wait_seconds=_WRITE_DRAIN_WAIT_SECONDS)
        with self._write_lock(timeout):
            self._revalidate()
            conn = self._connect_writer()
            committed = False
            try:
                yield conn
                committed = True
            finally:
                conn.close()
                # 写期间别的线程可能又建了缓存只读连接（它读到的是写前的页），
                # 收尾再失效一次：generation 也一并 +1，双保险。
                self._invalidate_read_caches()
                # 2026-11-19：写事务成功提交后把「内容纪元」+1（O(1)，一次 sqlite 写）。
                # 目的是让**就地 UPDATE**（行数/最大时间都不变）也能让筛选就绪指纹失效——
                # 价格表 1750 万行 × 2，做行级 checksum 要 +275ms（基线才 430–510ms），
                # 而写入自己记账是 O(1)。详见 app/core/storage/content_epoch.py 的模块文档。
                # 注意：只在**成功**路径记账；读操作完全不经过这里，所以不会误伤缓存。
                if committed:
                    with contextlib.suppress(Exception):
                        from app.core.storage import content_epoch

                        content_epoch.bump(
                            self._path_set.sqlite_path, reason="duckdb_write"
                        )

    def _connect_writer(self) -> duckdb.DuckDBPyConnection:
        """Open a read-write connection with bounded retry (caller holds the write lock).

        同进程 Web 读线程在写锁建立前可能已持有 read_only 连接；Windows
        DuckDB 此时报 "different configuration"，写连接需要等读连接关闭。
        旧窗口约 10s，全市场价格更新时曾让 000534 写入失败；延长到约
        90s，覆盖长查询/慢请求，失败后再交给上层 retry。
        """
        last_error: Exception | None = None
        delays = (0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0)
        started = time.monotonic()
        for attempt in range(len(delays) + 1):
            try:
                return _store_connect(
                    str(self._db_path), config=self._connection_config(),
                )
            except Exception as error:
                last_error = error
                if attempt < len(delays):
                    # 2026-11-22（独立性能审查实测）：这段退避原来**一个字都不打**，
                    # 于是"另一个进程持只读句柄"时写路径会**静默卡住**（实测 52.65s
                    # 才成功），日志里看不出任何原因。现在每次重试都留 WARNING：
                    # 已等待时长 + 下一次等待 + 原始错误关键字。只加日志，不改退避曲线。
                    logger.warning(
                        "写连接被占用，等待后重试（第 %d/%d 次，已等 %.1fs，"
                        "下次再等 %.1fs）: %s",
                        attempt + 1, len(delays),
                        time.monotonic() - started, delays[attempt],
                        str(error)[:160],
                    )
                    time.sleep(delays[attempt])
        assert last_error is not None
        raise last_error

    @contextlib.contextmanager
    def transaction(self, timeout: float = 30.0) -> Iterator[duckdb.DuckDBPyConnection]:
        """Run all statements on one connection and roll back unless commit succeeds."""
        # 与 write_connection 同理：拿写锁之前先关掉缓存只读连接。
        self._invalidate_read_caches(wait_seconds=_WRITE_DRAIN_WAIT_SECONDS)
        with self._write_lock(timeout):
            self._revalidate()
            conn = self._connect_writer()
            committed = False
            try:
                # 2026-08-14 红队 P3：begin() 移入 try，失败时连接由
                # finally 的 rollback/close 兜底，不再依赖 GC。
                conn.begin()
                yield conn
                conn.commit()
                committed = True
            finally:
                if not committed:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                conn.close()
                # 收尾失效（含写期间新建的读连接），generation +1。
                self._invalidate_read_caches()
                # 2026-11-23 修（由测试基建 agent 发现）：这是**第二条写路径** ——
                # `write_connection()` 提交后会推进内容纪元，这里原先**没有**，
                # 于是所有走 `transaction()` 的生产写入（analyst_forecast / archive /
                # backfill / backup / business …）都**不会**让筛选就绪指纹失效，
                # 等于把"就地改动看不见"的假绿从这个口子放了回来。
                # 与 `write_connection()` 对齐：只在**提交成功**后推进一次。
                if committed:
                    with contextlib.suppress(Exception):
                        from app.core.storage import content_epoch

                        content_epoch.bump(
                            self._path_set.sqlite_path, reason="duckdb_transaction"
                        )

    def _assert_simple_identifier(self, name: str) -> str:
        """表名/索引名只允许 `[A-Za-z_][A-Za-z0-9_]*`：下面这些 DDL 是拼进 SQL 的。"""
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"非法表名: {name!r}")
        return name

    def reindex_table(self, table: str, *, timeout: float = 300.0) -> dict[str, Any]:
        """重建 `table` 的**全部二级索引**（DROP + 按 `duckdb_indexes()` 里的 SQL 重建）。

        ⚠️ **2026-11-22 起：库里已经没有二级索引可重建了**（schema v50 把 30 个
        二级索引全删且不再建，只保留主键 ART）。所以本方法对正常库是**安全 no-op**；
        它只在"从 v49 及更早的备份恢复、且索引确实不一致"这种历史场景下才有事可做。
        **不要**因为它存在就去调它 —— 重建一个本就该没有的索引等于撤销 v50 的判决。

        这是修 `DuckDBIndexInconsistencyError` 的首选手段：只重写索引、不动堆数据，
        实测 `business_breakdown`（101 万行）**0.45s**（整表重建要 4.2s 且会让
        追加型库文件的表数据再存一份）。主键 ART 由 `PRIMARY KEY(...)` 约束持有，
        不能也不会在这里重建（约束索引无法 DROP）。

        DDL 在 DuckDB 里是事务性的：中途失败整体回滚，旧索引原样保留。
        """
        table = self._assert_simple_identifier(table)
        started = time.perf_counter()
        rebuilt: list[str] = []
        with self.write_connection(timeout=timeout) as conn:
            index_rows = conn.execute(
                "SELECT index_name, sql FROM duckdb_indexes() WHERE table_name = ?", [table],
            ).fetchall()
            # ⚠️ 索引 DDL **不能**包在显式事务里：DROP+CREATE 同事务实测报
            # `INTERNAL Error: BoundIndex::CreateDeltaIndex is not supported for
            # this index type`（DuckDB 1.5.5）。所以逐条语句自动提交。
            # 代价：不是原子的。因此先把 DDL 读进内存，CREATE 失败时用这份 SQL
            # 再补一次，尽力避免"索引被删掉没建回来"。
            for index_name, index_sql in index_rows:
                conn.execute(f"DROP INDEX {self._assert_simple_identifier(str(index_name))}")
                try:
                    conn.execute(str(index_sql))
                except Exception:
                    logger.warning("重建索引 %s 失败，用捕获的 DDL 重试一次", index_name)
                    conn.execute(str(index_sql))
                rebuilt.append(str(index_name))
        return {
            "table": table,
            "indexes_rebuilt": rebuilt,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

    def rebuild_table(self, table: str, *, timeout: float = 600.0) -> dict[str, Any]:
        """整表重建：从堆数据重建 heap + 全部索引，恢复索引 ↔ 堆不变量。

        唯一能修 `DuckDBIndexInconsistencyError` 的手段（见模块内长注释）。
        在一个事务里：建同 DDL 新表 → `INSERT ... SELECT` 全量搬 → 删旧表 →
        改名 → 按 `duckdb_indexes()` 里记录的 SQL 重建二级索引。
        任何一步失败整体回滚，旧表原样保留（DDL 在 DuckDB 里是事务性的）。

        注意：主键/唯一约束的 ART 索引由 DDL 里的 `PRIMARY KEY(...)` 自动重建，
        因此**不需要**也不允许手工 `CREATE INDEX` 主键。
        """
        table = self._assert_simple_identifier(table)
        started = time.perf_counter()
        with self.write_connection(timeout=timeout) as conn:
            ddl_row = conn.execute(
                "SELECT sql FROM duckdb_tables() WHERE table_name = ?", [table],
            ).fetchone()
            if not ddl_row or not ddl_row[0]:
                raise ValueError(f"找不到表 {table} 的 DDL，拒绝重建")
            ddl = str(ddl_row[0])
            body = ddl[ddl.index("("):]
            index_sql = [
                str(row[0])
                for row in conn.execute(
                    "SELECT sql FROM duckdb_indexes() WHERE table_name = ?", [table],
                ).fetchall()
                if row[0]
            ]
            rows_before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            staging = f"{table}__vd_rebuild_{secrets.token_hex(4)}"
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(f"CREATE TABLE {staging} {body}")
                conn.execute(f"INSERT INTO {staging} SELECT * FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {staging} RENAME TO {table}")
                for sql in index_sql:
                    conn.execute(sql)
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            rows_after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        return {
            "table": table,
            "rows_before": int(rows_before),
            "rows_after": int(rows_after),
            "indexes_rebuilt": len(index_sql),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

    def write_query(self, sql: str, params: list[Any] | None = None) -> None:
        """执行写操作（自动获取写锁）"""
        with self.write_connection() as conn:
            conn.execute(sql, params or [])

    def execute_script(self, script: str) -> None:
        """执行多语句 SQL 脚本（用于 schema 初始化）"""
        with self.write_connection() as conn:
            conn.execute(script)

    @property
    def db_path(self) -> Path:
        return self._db_path
