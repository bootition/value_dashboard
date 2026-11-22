"""内容纪元（content epoch）：让"就地 UPDATE"在写下的那一刻就被记账。

为什么需要它
------------
筛选就绪缓存靠**指纹**判断"数据变没变"。指纹对"就地 UPDATE 已有行"
（行数不变、最大时间不变）天生不敏感 —— `reports/140` §13.4 修掉了日历/财务表，
但**价格表**（`price_daily_raw` / `price_daily_qfq`，各 1750 万行）仍是盲区：
qfq 历史重算只改已有行的数值、不新增行，指纹看不出来 → 缓存命中 → 假绿。

为什么不是"再加一个 checksum"
------------------------------
实测（28GB 正式库，`read_only`）：价格表的内容信号 ——
`MAX(trade_date)` 单表 +180ms、按年分桶 COUNT +204ms、
`COUNT+SUM(round(close*100))`（唯一能看就地重算的）**+275ms**，
而整个指纹基线是 430–510ms。加 275ms 等于 +60%，会把"筛选就绪缓存"的收益抹光。

所以正解不是"算得更狠"，而是**让写入自己记账**：
每次 DuckDB 写事务提交成功后把纪元 +1（O(1)，一次 sqlite 写），
指纹里带上这个数字 —— 就地重算也会让指纹变化，缓存自然失效。

纪律
----
- **读操作绝不 bump**（否则缓存永远失效 = 等于没有缓存）。
  写路径的 bump 点只有**三处**，且三处都**必须** bump —— 少一处就等于把那一条
  写路径的"就地改动"重新变成无痕，筛选缓存从那个口子假绿回去：
  1. `duckdb_store.DuckDBStore.write_connection()` **成功退出**时
     （`reason="duckdb_write"`）——主写入路径；
  2. `duckdb_store.DuckDBStore.transaction()` **提交成功**时
     （`reason="duckdb_transaction"`）—— analyst_forecast / archive / backfill /
     backup / business 等生产写入全走这条路；它当初**漏了** bump，于是这些写入
     对指纹完全不可见（补记的现场说明就在该函数里）；
  3. **裸 `duckdb.connect(path)` 以可写方式打开**时
     （`duckdb_store._note_bare_writable_connect`，`reason="bare_duckdb_connect"`）
     —— 仓库里确有绕过 store 的脚本：它们既关掉缓存只读连接、又能就地改值，
     不记账就一点痕迹都不留（独立审查构造实测：只 `UPDATE` 一行，行数/最大报告期
     都不变 → 探针回放旧名单 → 筛选静默多算一只）。之所以取"打开即记账"而不是
     "看到写语句才记账"，见该函数的 docstring（`DuckDBPyConnection` 的
     `execute/executemany/sql/append` 是只读属性，挂不上钩子）。
  为什么必须三处都记账：指纹（行数 / 最大时间 / 内容摘要）对"就地 UPDATE"天生
  不敏感，**任何一条写路径漏记账 = 缓存命中旧值 = 假绿**（返回的名单是旧的，
  但没有任何状态显示它旧）；反过来多见几次 bump 的代价只是多算一次。
  跨进程写（自动更新子进程 / `python scripts/xxx.py`）不靠这里，靠
  `ScreeningEngine._mixed_report_content_fingerprint` 的内容摘要兜底。
- **fail-open**：记账失败绝不能让业务写入失败（它只是缓存新鲜度的提示）。
  取不到纪元时 `current()` 返回 0，指纹退化成"不含纪元"，与旧行为等价。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

#: 存在 sqlite `data_refresh_state` 里的键（复用它，**不新增表 = 零 schema 迁移**）。
CONTENT_EPOCH_KEY = "content_epoch"

#: 纪元所在表（由 `init_sqlite_schema` 建）。
_STATE_TABLE = "data_refresh_state"


def current(sqlite_path: Path | str) -> int:
    """读当前纪元；读不到（表还没建 / 文件不存在 / 任何错误）一律返回 0。"""
    try:
        with sqlite3.connect(str(sqlite_path), timeout=5.0) as conn:
            row = conn.execute(
                f"SELECT value FROM {_STATE_TABLE} WHERE key = ?", (CONTENT_EPOCH_KEY,)
            ).fetchone()
    except sqlite3.Error:
        return 0
    if not row or row[0] is None:
        return 0
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return 0
    # 兼容"直接存了个数字"的写法
    value = payload.get("epoch", 0) if isinstance(payload, dict) else payload
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def bump(sqlite_path: Path | str, reason: str = "") -> int | None:
    """把纪元 +1 并记下原因；返回新纪元，失败返回 None（**不抛异常**）。

    用 `BEGIN IMMEDIATE` 拿写锁，避免两个写者读到同一个旧值再各自 +1（丢计数）。
    """
    try:
        conn = sqlite3.connect(str(sqlite_path), timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT value FROM {_STATE_TABLE} WHERE key = ?", (CONTENT_EPOCH_KEY,)
        ).fetchone()
        previous = 0
        if row and row[0] is not None:
            try:
                payload = json.loads(row[0])
                previous = int(
                    payload.get("epoch", 0) if isinstance(payload, dict) else payload
                )
            except (TypeError, ValueError):
                previous = 0
        epoch = max(0, previous) + 1
        conn.execute(
            f"INSERT INTO {_STATE_TABLE} (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            f"ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (CONTENT_EPOCH_KEY, json.dumps({"epoch": epoch, "reason": reason}, ensure_ascii=False)),
        )
        conn.commit()
        return epoch
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        return None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
