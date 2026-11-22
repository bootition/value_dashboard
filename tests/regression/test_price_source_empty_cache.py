"""源侧"永远没有这只股票的行情"的负缓存（2026-11-22 线上效率事故）。

事故现场：`002122`（ST汇洲）在库里**一根 K 线都没有**，源侧对目标交易日始终
不返回数据；而"raw 缺失"是**硬缺口**（必须可见、不许延迟）→ 它**每轮都被重抓**，
白花约 10 秒/轮（占整轮 5%），且永远不可能成功。

口径（本次实现）：
* **只缓存"库里一行都没有"的股票**；有历史行、只是落后的**不缓存**（源侧可能补发）。
* 按 `(code, target_date)` 记，**TTL 24 小时**；
* 缺口**不隐藏**：报告里单列 `source_empty_cached_count/codes`；
* 缓存读不出来/格式坏 → **原样放行**（宁可多扫一轮，不能漏更新）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from app.core.update_parts.prices_step import (
    SOURCE_EMPTY_CACHE_KEY,
    SOURCE_EMPTY_TTL_SECONDS,
    _apply_source_empty_cache,
    _refresh_source_empty_cache,
)

LOG = logging.getLogger("test.price_cache")


def _seed_stock(duckdb_store, code: str, *, bars: int = 3, latest: str | None = "2026-11-19"):
    """插 `bars` 根日线（bars=0 表示这只股票库里一行都没有）。"""
    if bars <= 0:
        return
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES (?, ?, 'SZSE', TRUE) ON CONFLICT DO NOTHING""",
        [code, f"名称{code}"],
    )
    rows = []
    for i in range(bars):
        day = (datetime.fromisoformat(latest) - timedelta(days=bars - 1 - i)).date()
        rows.append((code, str(day), 10.0))
    for code_, day, close in rows:
        duckdb_store.write_query(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?,?,?)",
            [code_, day, close],
        )


def _stock(code: str, latest: str | None = None) -> dict:
    return {"stock_code": code, "latest_raw_date": latest, "latest_qfq_date": latest}


def test_caches_only_stocks_with_zero_bars(duckdb_store, sqlite_store) -> None:
    """抓完之后：库里一行都没有的进缓存；有历史行的绝不进（源侧可能补发）。"""
    _seed_stock(duckdb_store, "002122", bars=0)      # 无任何行情
    _seed_stock(duckdb_store, "600000", bars=3)      # 有历史行、只是落后

    class Owner:
        duck = duckdb_store
        sqlite = sqlite_store

        @staticmethod
        def _read_refresh_state(key):
            rows = sqlite_store.query("SELECT value FROM data_refresh_state WHERE key=?", [key])
            return str(rows[0]["value"]) if rows else None

        @staticmethod
        def _write_refresh_state(key, value):
            sqlite_store.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                [key, value],
            )

    owner = Owner()
    _refresh_source_empty_cache(
        owner, ["002122", "600000"], "2026-11-20", logger=LOG,
    )
    cache = json.loads(owner._read_refresh_state(SOURCE_EMPTY_CACHE_KEY) or "{}")
    assert "002122" in cache
    assert cache["002122"]["had_bars"] is False
    assert "600000" not in cache          # 有历史行 → 不进缓存


def test_cache_hit_skips_fetch_within_ttl_and_keeps_gap_visible(duckdb_store, sqlite_store) -> None:
    """TTL 内命中：从抓取集合摘掉，但**返回的代码要能进报告**（缺口不被隐藏）。"""
    class Owner:
        @staticmethod
        def _read_refresh_state(key):
            return json.dumps({
                "002122": {
                    "target_date": "2026-11-20",
                    "last_attempt": datetime.now(UTC).isoformat(),
                    "had_bars": False,
                },
            })

    kept, cached = _apply_source_empty_cache(
        Owner(), [_stock("002122"), _stock("600000")], "2026-11-20", logger=LOG,
    )
    assert [s["stock_code"] for s in kept] == ["600000"]
    assert cached == ["002122"]


def test_expired_entry_is_retried(duckdb_store) -> None:
    """超过 TTL → 必须重新抓（不能永久拉黑一只股票）。"""
    old = (datetime.now(UTC) - timedelta(seconds=SOURCE_EMPTY_TTL_SECONDS + 60)).isoformat()

    class Owner:
        @staticmethod
        def _read_refresh_state(key):
            return json.dumps({
                "002122": {"target_date": "2026-11-20", "last_attempt": old, "had_bars": False},
            })

    kept, cached = _apply_source_empty_cache(
        Owner(), [_stock("002122")], "2026-11-20", logger=LOG,
    )
    assert [s["stock_code"] for s in kept] == ["002122"]
    assert cached == []


def test_different_target_date_is_not_cached(duckdb_store) -> None:
    """换了目标交易日 → 缓存不适用（源侧可能已经发出来了）。"""
    class Owner:
        @staticmethod
        def _read_refresh_state(key):
            return json.dumps({
                "002122": {
                    "target_date": "2026-11-19",
                    "last_attempt": datetime.now(UTC).isoformat(),
                    "had_bars": False,
                },
            })

    kept, cached = _apply_source_empty_cache(
        Owner(), [_stock("002122")], "2026-11-20", logger=LOG,
    )
    assert [s["stock_code"] for s in kept] == ["002122"]
    assert cached == []


def test_corrupt_cache_fails_open(duckdb_store) -> None:
    """缓存坏掉 → 原样放行（宁可多扫一轮，也不能因为缓存漏更新）。"""
    class Owner:
        @staticmethod
        def _read_refresh_state(key):
            return "{not json"

    kept, cached = _apply_source_empty_cache(
        Owner(), [_stock("002122")], "2026-11-20", logger=LOG,
    )
    assert [s["stock_code"] for s in kept] == ["002122"]
    assert cached == []


def test_stock_that_got_bars_is_removed_from_cache(duckdb_store, sqlite_store) -> None:
    """一旦有了行情行（哪怕没追平）→ 缓存条目必须被删掉，恢复正常每轮重试。"""
    _seed_stock(duckdb_store, "002122", bars=5, latest="2026-11-19")

    class Owner:
        duck = duckdb_store
        sqlite = sqlite_store

        @staticmethod
        def _read_refresh_state(key):
            rows = sqlite_store.query(
                "SELECT value FROM data_refresh_state WHERE key=?", [key],
            )
            return str(rows[0]["value"]) if rows else None

        @staticmethod
        def _write_refresh_state(key, value):
            sqlite_store.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                [key, value],
            )

    owner = Owner()
    # 先放一条"曾经判过空"的旧缓存（复刻"先空后有行"的时间线）
    owner._write_refresh_state(SOURCE_EMPTY_CACHE_KEY, json.dumps({
        "002122": {
            "target_date": "2026-11-20",
            "last_attempt": datetime.now(UTC).isoformat(),
            "had_bars": False,
        },
    }))
    _refresh_source_empty_cache(owner, ["002122"], "2026-11-20", logger=LOG)
    # 有历史行但没追平 → 既不加缓存，也要把旧条目删掉
    assert "002122" not in _apply_source_empty_cache(
        owner, [_stock("002122")], "2026-11-20", logger=LOG,
    )[1]
