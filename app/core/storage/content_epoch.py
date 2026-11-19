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
  bump 点只在 `DuckDBStore.write_connection()` 成功退出时，别处不许调。
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
