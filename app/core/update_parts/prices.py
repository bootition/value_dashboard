"""全历史价格替换的攒批缓冲（reports/140 §2.5 拆分的第 1 块）。

## 这个模块是什么

`IncrementalUpdater` 写全历史价格时走"攒批后一个事务写 N 只"。**攒批判据本身**
（只数 / 行数 / 距上次 add 的空闲秒数）与抓取、写库、DuckDB 全都无关——它是纯内存
数据结构 + 三条阈值判断。原先内联在 `app/core/update.py`（6,600 行的 god class）里，
现在独立成模块，让"什么时候落盘"这一件事可以单独读、单独测。

## 边界（本模块不做什么）

- **不 import `app.core.update`**（会成循环依赖），也不 import 任何 store / adapter：
  本类只持有内存里的 tuple 与三个阈值，落盘时机由调用方判断。
- **不做 I/O**：`add` / `should_flush` / `drain` 都不写库、不打日志、不抛业务异常。
- **行为冻结**：阈值语义、`time.monotonic()` 判据、`drain()` 的重置行为都与搬移前
  逐字一致（本次是搬移，不是重构）。这套语义的回归网是
  `tests/regression/test_price_batch_flush.py` 与
  `tests/regression/test_price_full_history_batch.py`。

## 兼容关系

`app/core/update.py` 仍以 `_PriceReplaceBuffer` 这个名字导入本类
（`from app.core.update_parts.prices import PriceReplaceBuffer as _PriceReplaceBuffer`），
因此既有的 `from app.core.update import _PriceReplaceBuffer` 调用点与测试**无需改动**。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

#: 单调时钟来源：默认用 ``time.monotonic``，可注入（见 ``update.py`` 的兼容壳）。
Clock = Callable[[], float]

__all__ = ["Clock", "PriceReplaceBuffer"]


class PriceReplaceBuffer:
    """Bounded buffer of full-history price responses awaiting one grouped write.

    2026-11-12 P0（性能）：全历史响应走"逐股独立事务"时，正式库 17M 行表上
    一份 DELETE+INSERT 就是一次 WAL 提交，日志里 144 处 ≥10s 全静默、共
    96.5 分钟。改为攒批后一个事务写 N 只，提交次数下降一个量级。

    2026-11-13 P1：上一版的时间判据是"从缓冲开张算起 5 秒"，而全历史抓取
    单只耗时 28~635 秒 —— 每次 add 完就立刻超时，生产 13 次落盘里 12 次
    N=1，攒批名存实亡（此时"整批事务"等价一股一事务，持写锁时长 = 503 时长）。
    现在的判据：

    * ``len >= max_stocks`` 或 ``rows >= max_rows`` → 立刻落盘（硬上限）；
    * 否则只在"距**上一次 add** 已空闲 ``max_seconds``"**且已攒到 ≥2 只** 时
      落盘 —— 即流水线真的没再送新数据来（快收尾/空窗），而不是"开张久了"；
    * 只有 1 只时永不按时限落盘，由调用方在流水线排空（``finally`` flush）时
      一次性写掉，避免证据上出现"批量=1 只"。

    注意行数据本身早已由抓取侧持有，本缓冲只多持有一小段时间，
    且有只数/行数硬上限，不会把整表读进内存。
    """

    def __init__(
        self,
        *,
        max_stocks: int,
        max_rows: int,
        max_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.max_stocks = max(1, int(max_stocks))
        self.max_rows = max(1, int(max_rows))
        self.max_seconds = max(0.001, float(max_seconds))
        # 时钟可注入：搬移前本类直接用 ``app.core.update`` 模块里的 ``time.monotonic()``，
        # 而 ``test_price_batch_flush.py`` 用 ``monkeypatch.setattr(update_module, "time", ...)``
        # 控制时间。``update.py`` 的兼容壳因此把时钟源仍指向 ``update.time``，
        # 保证 monkeypatch 语义与搬移前逐字一致。
        self._clock: Clock = clock
        self._items: list[tuple[str, Any, Any, bool, bool]] = []
        self._rows = 0
        self._last_add_at = self._clock()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def rows(self) -> int:
        return self._rows

    def add(self, item: tuple[str, Any, Any, bool, bool]) -> None:
        self._last_add_at = self._clock()
        self._items.append(item)
        _, raw_result, qfq_result, raw_full, qfq_full = item
        if raw_full and raw_result is not None:
            self._rows += len(raw_result.data or ())
        if qfq_full and qfq_result is not None:
            self._rows += len(qfq_result.data or ())

    def should_flush(self) -> bool:
        if not self._items:
            return False
        if len(self._items) >= self.max_stocks:
            return True
        if self._rows >= self.max_rows:
            return True
        # 时间判据：只看"距上次 add 的空闲时长"，且至少攒到 2 只。
        # 单只时永不按时限 flush（否则慢抓取下永远只能 N=1）。
        if len(self._items) < 2:
            return False
        return (self._clock() - self._last_add_at) >= self.max_seconds

    def drain(self) -> list[tuple[str, Any, Any, bool, bool]]:
        items = self._items
        self._items = []
        self._rows = 0
        self._last_add_at = self._clock()
        return items
