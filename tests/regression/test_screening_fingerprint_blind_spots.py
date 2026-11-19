"""指纹盲区续修（2026-11-19 复审 §13.4/§14.8 遗留 4 个盲区）。

上一轮已修好日历 / 人工覆盖 / 财务三表，但审查实测点名仍有 4 个盲区：

① `source_audit`（正式库 22,421,205 行）**就地翻标记**：旧信号只有
   `COUNT(*) + MAX(fetch_time)/MAX(created_at)`，改某行的 confidence /
   raw_response_hash（后者正是 LINEAGE_INVALID 的 `invalid_hash_rows` 判据）
   行数与时间戳全不变 → 指纹不变 → 缓存命中 → 对外 `ready=True`。
② 归档系（`source_audit_archive` / `fetch_batch`）同理。
③ 价格两表**从未进过外层指纹** → qfq 历史重算外层完全看不见。
④ 超预算时**静默**退回 count-only（payload 里写了 mode，调用方看不出来）。

本文件追加的用例要求：**把上面每一条改坏，必须红**。
"""

from __future__ import annotations

import logging

import pytest

from app.core.data_quality import (
    _CONTENT_CHECKSUM_ROW_BUDGET,
    SCREENING_READINESS_CACHE_KEY,
    fingerprint_degradation,
    load_screening_readiness_entry,
    screening_readiness_cache_key,
    screening_readiness_fingerprint,
    store_screening_readiness_cache,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.regression.test_screening_cache_freshness import _seed_screenable

_READY = {"ready": True, "readiness": {"ready": True}, "warning_codes": []}


def _insert_audit_row(duck: DuckDBStore, *, stock_code: str = "000001",
                      confidence: str = "high", raw_hash: str = "a" * 64,
                      batch_id: str = "batch-1") -> None:
    """One source_audit row shaped like the real writer's output."""
    duck.write_query(
        """INSERT INTO source_audit
               (stock_code, field_name, report_date, value, source, fetch_batch_id,
                fetch_time, raw_response_hash, confidence, reason_code, api_version)
           VALUES (?, 'total_assets', '2025-12-31', 100, 'akshare', ?,
                   TIMESTAMP '2026-01-01 00:00:00', ?, ?, NULL, 'v1')""",
        [stock_code, batch_id, raw_hash, confidence],
    )


def _insert_batch_row(duck: DuckDBStore, *, batch_id: str = "batch-1",
                      data_type: str = "financials", row_count: int = 10) -> None:
    duck.write_query(
        """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time,
                raw_response_hash, row_count, confidence, created_at)
           VALUES (?, ?, 'akshare', 'v1', TIMESTAMP '2026-01-01 00:00:00',
                   ?, ?, 'high', TIMESTAMP '2026-01-01 00:00:00')""",
        [batch_id, data_type, "a" * 64, row_count],
    )


def _insert_archive_row(duck: DuckDBStore, *, stock_code: str = "000001",
                        confidence: str = "high") -> None:
    duck.write_query(
        """INSERT INTO source_audit_archive
               (id, stock_code, field_name, report_date, value, source, fetch_batch_id,
                fetch_time, raw_response_hash, confidence, api_version)
           VALUES (1, ?, 'total_assets', '2025-12-31', 100, 'akshare', 'batch-1',
                   TIMESTAMP '2026-01-01 00:00:00', ?, ?, 'v1')""",
        [stock_code, "a" * 64, confidence],
    )


# ---------------------------------------------------------------------------
# 盲区 ①：source_audit 就地翻标记
# ---------------------------------------------------------------------------
def test_fingerprint_changes_when_source_audit_flag_flipped_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """盲区 ①：`source_audit.confidence` 就地翻转必须让指纹变化。

    旧指纹对此完全透明：COUNT(*)、MAX(fetch_time)、MAX(created_at) 三值全等。
    而 `confidence` 直接决定 `LINEAGE_INVALID` 这类阻断码的判定输入。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    _insert_audit_row(duckdb_store, confidence="high")
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None
    store_screening_readiness_cache(sqlite_store, baseline, _READY)
    legacy_before = duckdb_store.read_query(
        """SELECT COUNT(*) AS n, MAX(fetch_time) AS f, MAX(created_at) AS c
           FROM source_audit"""
    )[0]

    duckdb_store.write_query(
        "UPDATE source_audit SET confidence = 'low' WHERE stock_code = '000001'"
    )

    # 前提校验：这正是旧信号看不见的那种改动（行数与时间戳全等）。
    legacy = duckdb_store.read_query(
        """SELECT COUNT(*) AS n, MAX(fetch_time) AS f, MAX(created_at) AS c
           FROM source_audit"""
    )[0]
    assert legacy == legacy_before, "旧信号 (COUNT, MAX(fetch_time), MAX(created_at)) 必须全等"

    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after is not None
    assert after != baseline, "source_audit 就地翻标记后指纹必须变化，否则就是假绿"
    # 调用方会拿新指纹查缓存：必须 miss，不能把 baseline 时代的 ready=True 交出去。
    assert load_screening_readiness_entry(sqlite_store, after, allow_stale=True) is None
    # 反向验证：失效来自内容指纹，不是记录被删。
    assert load_screening_readiness_entry(sqlite_store, baseline)["decision"] == _READY


def test_fingerprint_audit_lineage_hash_boundary_is_pinned(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """**已登记的残余盲区**：`raw_response_hash` 就地改坏，外层指纹仍看不见。

    为什么没修：`raw_response_hash` 是 64 字符高基数字符串列，对 22.4M 行做
    `SUM(hash(col))` 实测 +300ms 以上、`bit_xor` 虽然便宜但**奇偶敏感**（偶数列
    同值会抵消成 0，等于给了个假安全感）。而 `confidence` 这类**标记列**的
    分布已经进了指纹（见上一个用例），`LINEAGE_INVALID` 的另一半判据
    （`orphan_batch_rows`）由 `fetch_batch` 分布 + `fetch_batch_c` 兜住。

    这条用例把边界**钉住**：真补上内容信号时它会立刻变红，提醒同步更新
    报告里的残余盲区清单（而不是悄悄多了个没人知道的覆盖）。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    _insert_audit_row(duckdb_store, raw_hash="a" * 64)
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None

    duckdb_store.write_query(
        "UPDATE source_audit SET raw_response_hash = repeat('b', 32)"
    )
    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after == baseline, (
        "边界变了：raw_response_hash 现已进入外层指纹 → 请更新 "
        "reports/140 §13.4 的残余盲区清单并把本用例改成 must-change"
    )
    # 但**行数**维度的 lineage 变化仍然可见（删除/归档迁移都会改变 COUNT）。
    duckdb_store.write_query(
        "DELETE FROM source_audit WHERE raw_response_hash = repeat('b', 32)"
    )
    after_delete = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_delete is not None and after_delete != baseline


def test_fingerprint_changes_when_archive_and_batch_flipped_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """盲区 ②：归档表 `source_audit_archive` / `fetch_batch` 就地翻标记。

    这两张表也是 LINEAGE_INVALID（`orphan_batch_rows` / `archive_gap_rows`）的
    判定输入，旧信号同样只有 COUNT + MAX 时间戳。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    _insert_batch_row(duckdb_store, data_type="financials")
    _insert_archive_row(duckdb_store, confidence="high")
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None

    duckdb_store.write_query(
        "UPDATE source_audit_archive SET confidence = 'low' WHERE id = 1"
    )
    after_archive = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_archive is not None and after_archive != baseline, (
        "source_audit_archive 就地翻标记后指纹必须变化"
    )

    duckdb_store.write_query(
        "UPDATE fetch_batch SET data_type = 'dividends' WHERE batch_id = 'batch-1'"
    )
    after_batch = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_batch is not None and after_batch not in {baseline, after_archive}, (
        "fetch_batch 就地改 data_type 后指纹必须变化"
    )


# ---------------------------------------------------------------------------
# 盲区 ③：价格表从未进入外层指纹
# ---------------------------------------------------------------------------
def test_fingerprint_covers_price_tables_row_set(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """盲区 ③：价格两表必须进入**外层**指纹（此前一张都不在）。

    旧状态：外层指纹只数 source_audit / 归档 / 指标快照 / 财务表，价格表只在
    `_has_complete_trading_calendar()` 的**内层**缓存键里出现；外层一命中缓存，
    内层根本没机会跑 → 价格数据怎么变，外层指纹都不动。

    覆盖边界（**诚实登记**，见报告残余盲区）：

    * 覆盖：任一价格表**新增/删除行** → COUNT(*) 变化 → 指纹变化。
    * 不覆盖：`price_daily_qfq.close` **就地重算**（历史复权重算，不新增除权
      记录）。能看见它的内容信号（按年 COUNT + `SUM(round(close*100)::BIGINT)`）
      在正式库实测 +275ms/次，`MAX(trade_date)` +180ms/表 —— 都超出 150ms
      性能红线，因此**没有**进常态路径。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None
    store_screening_readiness_cache(sqlite_store, baseline, _READY)

    # ③-a price_daily_raw 新增一行（旧的 K 线行集合变化）。
    duckdb_store.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
           VALUES ('000002', DATE '2001-01-02', 5, 1)"""
    )
    after_raw = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_raw is not None and after_raw != baseline, (
        "price_daily_raw 新增行后指纹必须变化，否则价格表仍然完全在外层指纹之外"
    )
    assert load_screening_readiness_entry(sqlite_store, after_raw, allow_stale=True) is None

    # ③-b price_daily_qfq 新增一行。
    duckdb_store.write_query(
        """INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume)
           VALUES ('000002', DATE '2001-01-02', 5, 1)"""
    )
    after_qfq = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_qfq is not None and after_qfq not in {baseline, after_raw}, (
        "price_daily_qfq 新增行后指纹必须变化"
    )

    # ③-c 反向验证：失效来自指纹而不是缓存行被删。
    assert load_screening_readiness_entry(sqlite_store, baseline)["decision"] == _READY


# ---------------------------------------------------------------------------
# 盲区 ④：降级必须可见（不静默）
# ---------------------------------------------------------------------------
def test_oversized_content_checksum_degrades_visibly(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """盲区 ④：超预算退回 count-only 时，payload/日志/缓存条目都必须看得出来。

    修复前的行为：`_duck_content_checksums` 只在每张表的子字典里写
    `mode="count-only"`，而 `screening_readiness_cache_key()` **只返回一个
    sha256 字符串** —— 调用方拿到的就是"SHA 变了/没变"，无从判断这次就绪
    判断到底有没有降级。于是盲区随数据增长悄悄回来。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    # 预算压到 0：任何有行的表都必须降级。
    monkeypatch.setattr("app.core.data_quality._CONTENT_CHECKSUM_ROW_BUDGET", 0)
    caplog.set_level(logging.WARNING, logger="app.core.data_quality")

    fingerprint, payload = screening_readiness_fingerprint(duckdb_store, sqlite_store)
    assert fingerprint is not None
    degraded = payload["degraded"]
    assert degraded["active"] is True, "超预算必须显式标 degraded，不能静默退回"
    assert "balance_sheet" in degraded["count_only_tables"]
    assert degraded["reasons"]["balance_sheet"] == "row_budget_exceeded"

    # ① 入口能查到：fingerprint_degradation(指纹) 返回同一条记录。
    assert fingerprint_degradation(fingerprint)["active"] is True
    # ② 日志有 WARNING（运维可见）。
    assert any(
        "降级为 count-only" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "降级必须落 WARNING，不能只在内部 payload 里"
    # ③ 决策条目带出来：缓存 → 读回的 entry 里能看出这条决策是降级算的。
    store_screening_readiness_cache(sqlite_store, fingerprint, _READY)
    entry = load_screening_readiness_entry(sqlite_store, fingerprint)
    assert entry is not None
    assert entry["fingerprint_degraded"]["active"] is True

    # ④ 降级态与正常态的指纹必须不同（不会互相命中缓存行）。
    monkeypatch.setattr(
        "app.core.data_quality._CONTENT_CHECKSUM_ROW_BUDGET",
        _CONTENT_CHECKSUM_ROW_BUDGET,
    )
    healthy = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert healthy is not None and healthy != fingerprint
    assert load_screening_readiness_entry(sqlite_store, healthy) is None


def test_fingerprint_is_stable_across_repeated_calls(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """回归：同一份数据算两次必须完全一致（否则缓存永远不命中）。

    新增的 `bit_xor(hash(...))` 是**多重集**摘要，与行序无关，因此必须稳定。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    _insert_audit_row(duckdb_store)
    _insert_batch_row(duckdb_store)
    _insert_archive_row(duckdb_store)

    first = screening_readiness_cache_key(duckdb_store, sqlite_store)
    second = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert first is not None and first == second

    # 指纹稳定 ⇒ 同一指纹的缓存在有效期内的行为也不变。
    store_screening_readiness_cache(sqlite_store, first, _READY)
    entry = load_screening_readiness_entry(sqlite_store, second)
    assert entry is not None and entry["decision"] == _READY
    assert SCREENING_READINESS_CACHE_KEY == "screening_readiness_cache"
