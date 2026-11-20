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
from tests.support.seed import Seed

# `source_audit` 与 price_daily_raw/qfq 的**插入**走工厂的按域方法；紧随其后的
# `UPDATE` 一律留在原地：「就地改值后指纹变没变」的论点就在"行集合与时间戳都不动、
# 只有一列被改"上，插入与更新被混进同一条路径就看不见它了。
#
# 唯一例外是 `_insert_price_row()`：它显式走 `join(write_connection())`，因为
# 本文件有一条断言钉着"这一行写下去内容纪元必须 +1"，而那个记账只挂在
# `DuckDBStore.write_connection()` 上。详见该函数的 docstring。

_READY = {"ready": True, "readiness": {"ready": True}, "warning_codes": []}


def _insert_audit_row(duck: DuckDBStore, *, stock_code: str = "000001",
                      confidence: str = "high", raw_hash: str = "a" * 64,
                      batch_id: str = "batch-1") -> None:
    """One source_audit row shaped like the real writer's output."""
    Seed(duck).source_audit(
        stock_code,
        "total_assets",
        report_date="2025-12-31",
        value=100,
        # `source='akshare'`（不是工厂默认的 csmar）与固定 `fetch_time` 逐字传：
        # 本文件钉的是"就地改值后指纹变没变"，改前/改后两次比较必须站在同一行上。
        source="akshare",
        fetch_batch_id=batch_id,
        fetch_time="2026-01-01 00:00:00",
        raw_response_hash=raw_hash,
        confidence=confidence,
        reason_code=None,
        api_version="v1",
    )


def _insert_batch_row(duck: DuckDBStore, *, batch_id: str = "batch-1",
                      data_type: str = "financials", row_count: int = 10) -> None:
    # 留（第 4 类·多站点缺口，本轮不补 API）：`fetch_batch` 全仓 7 处播种、工厂
    # 没有方法；本轮租约禁止扩 `seed.py`，如实登记为下一轮候选。
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
    # 留（第 3 类·隔离归档）：`source_audit_archive` 的写入**就是本文件的论点** ——
    # "归档行也必须被指纹覆盖"。把它搬进工厂等于把"哪一行进了归档"藏起来，
    # 也不再是"整批搬家"的那条构造。
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


def test_fingerprint_detects_audit_hash_change_via_content_epoch(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """`raw_response_hash` 就地改坏 —— **现在能发现了**（2026-11-19 内容纪元落地后）。

    历史：这里原本是"已登记残余盲区"的边界钉子 —— 断言改 hash 后指纹**不变**，
    因为对 22.4M 行的高基数列做内容摘要太贵（`SUM(hash())` +300ms 以上、
    `bit_xor` 又奇偶敏感，会给出假安全感）。

    内容纪元（`app/core/storage/content_epoch.py`）改变了这笔账：
    **任何写事务都会让纪元 +1**，所以不再需要"按列做摘要"也能发现就地改动。
    钉子按设计报了红（"边界变了…请把本用例改成 must-change"），于是照它说的改成 must-change。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    _insert_audit_row(duckdb_store, raw_hash="a" * 64)
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None

    duckdb_store.write_query(
        "UPDATE source_audit SET raw_response_hash = repeat('b', 32)"
    )
    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after != baseline, (
        "就地改 raw_response_hash 后指纹没变：LINEAGE_INVALID 的 invalid_hash_rows "
        "判据会被旧缓存掩盖"
    )


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
    # 这里用工厂默认事务口即可：本用例要的信号是**行集合/COUNT**（见 docstring
    # 的"覆盖边界"），内容纪元那一路由 `_insert_price_row()` 单独钉。
    Seed(duckdb_store).prices("000002", [("2001-01-02", 5)], volume=1)
    after_raw = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after_raw is not None and after_raw != baseline, (
        "price_daily_raw 新增行后指纹必须变化，否则价格表仍然完全在外层指纹之外"
    )
    assert load_screening_readiness_entry(sqlite_store, after_raw, allow_stale=True) is None

    # ③-b price_daily_qfq 新增一行。
    Seed(duckdb_store).prices("000002", [("2001-01-02", 5)], table="price_daily_qfq", volume=1)
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


# ── 盲区 ③ 的正面收口：价格表「就地重算」必须让指纹变 ──────────────────────
# 以前价格表只在外层指纹里放 COUNT(*)。qfq 历史重算**不新增行**、只改已有行的数值，
# 于是行数不变 → 指纹不变 → 缓存命中 → 对外仍是旧的 ready 结论（假绿）。
# 修法不是"算得更狠"（行级摘要在 1750 万行上 +275ms，基线才 430–510ms），
# 而是**让写入自己记账**：DuckDBStore 每个成功写事务把 content_epoch +1，
# 指纹把这个数字也吃进哈希。


def _insert_price_row(duck: DuckDBStore, *, code: str, trade_date: str, close: float) -> None:
    """插入一行 qfq；**只收敛插入这一半**。

    紧随其后的 `_update_price_close()` 是 `UPDATE`，「就地改值 → 指纹要变」的
    论点正在那条 UPDATE 上，绝不能连它一起搬走。

    这里走 `join(write_connection())` 而不是让工厂自开 `transaction()`：
    本文件的 `test_content_epoch_advances_only_on_writes_not_reads` 断言的正是
    「这一行写下去，内容纪元必须 +1」，而 `DuckDBStore` 只在 `write_connection()`
    成功退出时记账（`transaction()` 不记账）。换成工厂默认事务口会**静默**
    把那条断言的前提抽掉 —— 种子形状一样，记账没了。
    """
    with duck.write_connection() as conn:
        Seed(duck).join(conn).prices(
            code, [(trade_date, close)], table="price_daily_qfq", volume=1000.0
        )


def _update_price_close(duck: DuckDBStore, *, code: str, trade_date: str, close: float) -> None:
    """**不新增行**，只改已有行的数值 —— 这就是"就地重算"的形态。"""
    duck.write_query(
        "UPDATE price_daily_qfq SET close = ? WHERE stock_code = ? AND trade_date = ?",
        [close, code, trade_date],
    )


def test_fingerprint_changes_when_price_row_recomputed_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """qfq 就地重算（行数不变）必须让指纹变化，否则缓存会拿旧结论假装已就绪。"""
    _seed_screenable(duckdb_store, sqlite_store)
    _insert_price_row(duckdb_store, code="600519", trade_date="2024-01-02", close=100.0)

    before = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert before is not None

    # 关键：行数不变、没有任何时间戳字段变化
    _update_price_close(duckdb_store, code="600519", trade_date="2024-01-02", close=88.5)

    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after is not None
    assert after != before, (
        "价格改值后指纹没变：缓存会命中旧的 ready 结论（qfq 就地重算 = 假绿）"
    )


def test_content_epoch_advances_only_on_writes_not_reads(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """读操作绝不许 bump —— 否则指纹永远在变，缓存等于没有。"""
    from app.core.storage import content_epoch

    _seed_screenable(duckdb_store, sqlite_store)
    first = content_epoch.current(sqlite_store.paths.sqlite_path)

    for _ in range(5):
        duckdb_store.read_query("SELECT COUNT(*) AS n FROM price_daily_qfq")

    assert content_epoch.current(sqlite_store.paths.sqlite_path) == first, (
        "只读操作改变了内容纪元：缓存会永远失效"
    )

    _insert_price_row(duckdb_store, code="000002", trade_date="2024-06-05", close=9.9)
    assert content_epoch.current(sqlite_store.paths.sqlite_path) > first, (
        "写操作没有推进内容纪元：就地改动将无法被发现"
    )


def test_content_epoch_bump_is_monotonic_and_reasoned(
    sqlite_store: SQLiteStore,
) -> None:
    """纪元必须单调递增，且记下原因（便于排查"谁把缓存打掉的"）。"""
    import json

    from app.core.storage import content_epoch

    path = sqlite_store.paths.sqlite_path
    start = content_epoch.current(path)
    for index in range(3):
        content_epoch.bump(path, reason=f"probe-{index}")

    assert content_epoch.current(path) == start + 3

    row = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = ?", [content_epoch.CONTENT_EPOCH_KEY]
    )
    payload = json.loads(row[0]["value"])
    assert payload["epoch"] == start + 3
    assert payload["reason"] == "probe-2"


def test_content_epoch_fails_open_when_store_is_unreadable(tmp_path) -> None:
    """记账失败绝不能让业务写入失败：读不到就返回 0，不抛异常。"""
    from app.core.storage import content_epoch

    missing = tmp_path / "nope.sqlite"
    assert content_epoch.current(missing) == 0
    # bump 在表不存在时也不许抛（业务写入路径会吞它，但这里直接验证"不抛"）
    assert content_epoch.bump(missing, reason="probe") is None


def test_content_epoch_advances_on_every_write_path(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """**两条写路径都必须推进内容纪元**（2026-11-23 由测试基建 agent 发现的真洞）。

    `DuckDBStore` 有两条写路径：`write_connection()` 与 `transaction()`。
    内容纪元最初只挂在 `write_connection()` 上，而 `transaction()` 被
    `analyst_forecast` / `archive` / `backfill` / `backup` / `business` 等**生产代码**广泛使用 ——
    也就是说，凡是走 `transaction()` 的就地改写**都不会**让指纹失效，
    "就地改动看不见"的假绿会从这个口子悄悄回来。

    这条用例把两条路径都钉住：任一分支漏了记账，立刻红。
    """
    from app.core.storage import content_epoch

    path = sqlite_store.paths.sqlite_path

    # 路径 1：write_connection()
    before = content_epoch.current(path)
    # 留（第 3 类·写路径论点构造）：这三条 `INSERT` 的论点**就是写路径本身**（哪个口
    # 提交后必须推进内容纪元、回滚的那次必须不推进），所以逐字写成 `conn.execute`，
    # 不换成工厂调用 —— 换了就看不出"是这条连接提交/回滚决定记账"这件事。
    with duckdb_store.write_connection() as conn:
        conn.execute(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume) "
            "VALUES ('000001', '2024-02-01', 10.0, 100.0)"
        )
    after_write_connection = content_epoch.current(path)
    assert after_write_connection > before, (
        "write_connection() 提交后没有推进内容纪元 → 指纹不会失效"
    )

    # 路径 2：transaction()
    # 留（第 3 类·写路径论点构造）：同路径 1 —— 钉的是"`transaction()` 提交也要记账"。
    with duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume) "
            "VALUES ('000002', '2024-02-01', 20.0, 200.0)"
        )
    after_transaction = content_epoch.current(path)
    assert after_transaction > after_write_connection, (
        "transaction() 提交后没有推进内容纪元 → 走这条路径的生产写入会让"
        "「就地改动看不见」的假绿从后门回来"
    )

    # 回滚的那次**不许**推进（没落库就没有内容变化）
    stable = content_epoch.current(path)
    # 留（第 3 类·必须写失败/回滚）：这一条**必须**随事务回滚掉（`pytest.raises` 抛在
    # `with` 内），断言钉的是"没落库就不许推进内容纪元"，所以保留原始写连接写法。
    with pytest.raises(RuntimeError), duckdb_store.transaction() as conn:
        conn.execute(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume) "
            "VALUES ('000003', '2024-02-01', 30.0, 300.0)"
        )
        raise RuntimeError("故意失败，验证回滚不记账")
    assert content_epoch.current(path) == stable, (
        "事务回滚了却推进了内容纪元 → 会让缓存无谓失效"
    )
