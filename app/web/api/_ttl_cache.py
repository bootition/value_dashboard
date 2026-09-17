"""进程内 TTL 缓存（线程安全 + 单飞），供只读 API 响应复用。

只读数据域（指数估值/ERP）按日更新，短 TTL 缓存即可同时解决：
- 并发请求重复扫描全表导致的 CPU/内存尖峰；
- 同 key 并发时只有一个线程计算（single-flight），其余等待复用结果。

key 应包含数据库路径等租户边界信息，避免同进程多 app/测试夹具串数据。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class TTLCache:
    """固定 TTL 的进程内缓存；值不可变使用（调用方负责不修改缓存对象）。"""

    def __init__(self, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._guard = threading.Lock()
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._compute_locks: dict[Any, threading.Lock] = {}
        self._generations: dict[Any, int] = {}

    def _generation_of(self, key: Any) -> int:
        return self._generations.get(key, 0)

    def get_or_compute(
        self,
        key: Any,
        factory: Callable[[], Any],
        *,
        stale_on: tuple[type[Exception], ...] = (),
        reject: Callable[[Any], bool] | None = None,
    ) -> Any:
        now = time.monotonic()
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None and now - entry[0] <= self._ttl:
                return entry[1]
            stale = entry
            generation = self._generation_of(key)
            lock = self._compute_locks.setdefault(key, threading.Lock())
            self._generations.setdefault(key, generation)

        with lock:
            now = time.monotonic()
            with self._guard:
                entry = self._entries.get(key)
                if entry is not None and now - entry[0] <= self._ttl:
                    return entry[1]
                stale = entry
                generation = self._generation_of(key)
            try:
                value = factory()
            except Exception as error:
                with self._guard:
                    self._compute_locks.pop(key, None)
                # 外部写进程持锁时返回过期旧值，而不是 500；无旧值才向上抛。
                if stale_on and stale is not None and isinstance(error, stale_on):
                    return stale[1]
                raise
            if reject is not None and reject(value):
                # 计算结果被判定为不可用（如全 error 快照）：不写入缓存，
                # 有旧值优先返回旧值（stale-while-error），无旧值仍按调用方
                # 原语义返回本次结果（保持逐项 error 的 200 契约）。
                with self._guard:
                    self._compute_locks.pop(key, None)
                if stale is not None:
                    return stale[1]
                return value
            with self._guard:
                # 计算期间该 key 发生过 invalidate/clear（例如 POST 写入）时
                # 丢弃旧快照结果，等下一次请求重新计算。
                if generation == self._generation_of(key):
                    self._entries[key] = (time.monotonic(), value)
                self._compute_locks.pop(key, None)
            return value

    def invalidate(self, key: Any, *, keep_stale: bool = True) -> None:
        """失效一个 key：下次请求必定尝试重算，但默认保留旧值作为回退快照。

        为什么保留：自动更新子进程持 DuckDB 文件锁期间（Windows 下外部写
        进程独占文件），任何重算都会抛 DuckDBReadLockedError。若这里直接
        删除旧值，写操作（如 ETF 预算）之后紧随的读请求就失去了 stale_on
        回退，页面从"可用旧快照"退化成 503/500（2026-09-17 线上实锤）。
        保留的旧值时间戳被回拨到已过期，正常路径绝不会直接命中它。

        keep_stale=False 用于已知结果本身不可用的场景（例如全 error 快照），
        此时宁可直接报错也不把坏结果当回退。
        """
        with self._guard:
            self._generations[key] = self._generation_of(key) + 1
            entry = self._entries.get(key)
            if entry is None:
                return
            if keep_stale:
                self._entries[key] = (entry[0] - self._ttl - 1.0, entry[1])
            else:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._guard:
            for key in list(self._generations):
                self._generations[key] = self._generation_of(key) + 1
            self._entries.clear()
