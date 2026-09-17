"""进程内 TTL 缓存回归：并发同 key 只计算一次，过期/失效后重算。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.web.api._ttl_cache import TTLCache


def test_single_flight_ttl_and_invalidate() -> None:
    cache = TTLCache(ttl_seconds=0.2)
    calls = 0
    active = 0
    max_active = 0
    guard = threading.Lock()

    def factory() -> dict[str, int]:
        nonlocal calls, active, max_active
        with guard:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return {"value": calls}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.get_or_compute("k", factory), range(8)))

    assert calls == 1
    assert max_active == 1
    assert {item["value"] for item in results} == {1}

    time.sleep(0.25)  # TTL 过期后重算（Windows 时钟粒度约 15ms，留足余量）
    assert cache.get_or_compute("k", factory)["value"] == 2

    cache.invalidate("k")
    assert cache.get_or_compute("k", factory)["value"] == 3


def test_clear_during_compute_discards_stale_snapshot() -> None:
    """写入发生在只读计算期间时，不得把旧快照重新塞回缓存。"""
    cache = TTLCache(ttl_seconds=10)
    started = threading.Event()

    def slow_factory() -> dict[str, int]:
        started.set()
        time.sleep(0.05)
        return {"value": 1}

    def fresh_factory() -> dict[str, int]:
        return {"value": 2}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get_or_compute, "k", slow_factory)
        assert started.wait(timeout=1)
        cache.clear()  # 模拟 POST 写入后的失效
        assert first.result(timeout=2)["value"] == 1
        second = pool.submit(cache.get_or_compute, "k", fresh_factory)

    assert second.result(timeout=2)["value"] == 2, "过期快照必须被丢弃并重新计算"


class _LockedError(Exception):
    pass


def test_stale_fallback_when_external_writer_locks_reads() -> None:
    cache = TTLCache(ttl_seconds=0.05)
    assert cache.get_or_compute("k", lambda: {"value": 1})["value"] == 1
    time.sleep(0.06)

    def locked_factory() -> dict[str, int]:
        raise _LockedError("file locked")

    assert cache.get_or_compute("k", locked_factory, stale_on=(_LockedError,))["value"] == 1
    with pytest.raises(_LockedError):
        cache.get_or_compute("missing", locked_factory, stale_on=(_LockedError,))


def test_invalidate_keeps_stale_fallback_for_locked_reads() -> None:
    """线上事故回归（2026-09-17）：写操作 invalidate 后遇到外部写进程持锁，
    必须回退旧快照而不是 503；一旦锁释放，下一次请求必须拿到新值。"""
    cache = TTLCache(ttl_seconds=10)
    assert cache.get_or_compute("k", lambda: {"value": 1})["value"] == 1

    def locked_factory() -> dict[str, int]:
        raise _LockedError("Can't open a connection: file already open in PID 28840")

    cache.invalidate("k")
    assert cache.get_or_compute("k", locked_factory, stale_on=(_LockedError,))["value"] == 1

    # 外部锁结束后重算成功，缓存必须换成新值（旧值只是过期回退，不会被直接命中）。
    assert cache.get_or_compute("k", lambda: {"value": 2})["value"] == 2
    assert cache.get_or_compute("k", lambda: {"value": 3})["value"] == 2


def test_invalidate_can_drop_stale_when_result_is_known_bad() -> None:
    cache = TTLCache(ttl_seconds=10)
    assert cache.get_or_compute("k", lambda: {"value": 1})["value"] == 1

    def locked_factory() -> dict[str, int]:
        raise _LockedError("file locked")

    cache.invalidate("k", keep_stale=False)
    with pytest.raises(_LockedError):
        cache.get_or_compute("k", locked_factory, stale_on=(_LockedError,))


def test_clear_removes_stale_fallback() -> None:
    cache = TTLCache(ttl_seconds=10)
    assert cache.get_or_compute("k", lambda: {"value": 1})["value"] == 1
    cache.clear()

    def locked_factory() -> dict[str, int]:
        raise _LockedError("file locked")

    with pytest.raises(_LockedError):
        cache.get_or_compute("k", locked_factory, stale_on=(_LockedError,))


def test_reject_returns_stale_and_never_caches_bad_value() -> None:
    """reject 语义（2026-09-17 index 卡片墙）：全 error 快照不得覆盖旧快照。"""
    cache = TTLCache(ttl_seconds=10)
    calls = 0

    def bad_factory() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": 0, "status": "error"}

    def is_bad(value: dict[str, object]) -> bool:
        return value.get("status") == "error"

    assert cache.get_or_compute("k", lambda: {"value": 1})["value"] == 1
    cache.invalidate("k")

    fallback = cache.get_or_compute("k", bad_factory, reject=is_bad)
    assert fallback["value"] == 1, "有旧值时必须返回旧值而不是坏快照"
    assert calls == 1

    # 无旧值时保留原契约返回坏快照，但不得写入缓存（下次仍重新计算）。
    no_stale = cache.get_or_compute("fresh", bad_factory, reject=is_bad)
    assert no_stale["status"] == "error"
    assert calls == 2
    cache.get_or_compute("fresh", bad_factory, reject=is_bad)
    assert calls == 3, "被 reject 的坏快照不得进入缓存"
