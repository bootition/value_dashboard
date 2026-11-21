"""筛选探针批量化 / 内容纪元缓存 —— 不许假绿（2026-11-21，`.planning/2026-screening-latency`）。

事故背景：正式库 `read_query()` / `SQLiteStore.query()` 都是 open-per-query（每条
~80ms / ~30ms），一次筛选要开 6-8 条连接，"开连接"就吃掉 1 秒左右；另有
`manual_overrides`（当时空表）13-14 条分块查询每次白跑 ~430ms。改造做了四件事：

1. `_read_scope()`：一次 `run()` 内的多条 DuckDB 探针共用**一条**只读连接；
2. 黑名单 + "有没有已发布覆写"探针共用**一条** SQLite 连接；
3. `_published_statement_overrides()`：没有 published 覆写 → 直接返回 `[]`；
4. `_load_mixed_report_date_codes()`：按**内容令牌**（`content_epoch` + 内容摘要）记住结果。

2026-11-25（独立审查证伪）：这条缓存的令牌原来只有"行数 + 最大报告期"，**看不见
就地改值** —— 绕过 store 的裸 `duckdb.connect()` 只 UPDATE 一个 `revenue`
（行数/最大期/纪元全不变）就能让它回放旧名单，筛选静默多算 1 只。本文件 3b 节
逐字复刻了那个构造，并分别钉住两条防线（同进程裸写记账 / 跨进程裸写靠内容摘要）。

本文件锁的是"**快但不错**"这一半 —— 每条优化都必须能被证明"不会少算、不会
拿旧数据冒充新数据"：

- 批量化（1/2）不许改变任何一条查询的返回语义；
- 短路（3）在空表时与原实现恒等，有覆写时一行不少；
- 缓存（4）**令牌一变必须重算**，**令牌取不到必须不缓存**（"不知道数据变没变"
  必须等价于"当它变了"，否则就是假绿）。
"""

from __future__ import annotations

import contextlib
import subprocess
import sys

import duckdb
import pytest

from app.core.screening import engine as engine_module
from app.core.screening.engine import ScreeningEngine, _BatchedReader
from app.core.storage import content_epoch, read_cache
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite, sql_now


@pytest.fixture(autouse=True)
def _clean_content_cache():
    read_cache.clear_cache()
    yield
    read_cache.clear_cache()


# ─── 1. 批量化：同一条连接上的结果必须与 store 逐行逐字一致 ────────────────

def test_batched_reader_matches_store_read_query(duckdb_store: DuckDBStore) -> None:
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE")
    seed.snapshot("000001", "2025-12-31", latest_close=10)

    sql = "SELECT stock_code, latest_close FROM indicator_snapshot WHERE latest_close > ?"
    params = [1]

    with duckdb_store.read_connection() as conn:
        batched = _BatchedReader(conn).read_query(sql, params)

    assert batched == duckdb_store.read_query(sql, params)
    assert batched == [{"stock_code": "000001", "latest_close": 10.0}]


# ─── 2. 空表短路：没有 published 覆写时一行不少，且不再打分块查询 ──────────

def test_no_published_overrides_short_circuits_without_chunk_queries(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE", is_listed=True)
    seed.snapshot("000001", "2025-12-31")
    # 一条 active（未发布）覆写：不在 `published` 口径里，不能算数。
    with sqlite_store.transaction() as conn:
        SeedSqlite(sqlite_store).join(conn).override(
            "000001", "total_assets", 200,
            report_date="2025-12-31", reason="pending", status="active",
            created_at=sql_now(),
        )

    statements: list[str] = []
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    original_query = engine.sqlite.query

    def spy(sql: str, params: list | None = None):
        statements.append(" ".join(sql.split()))
        return original_query(sql, params)

    engine.sqlite.query = spy  # type: ignore[method-assign]

    assert engine._published_statement_overrides("m.is_listed IS TRUE", []) == []
    # 只允许那条 `LIMIT 1` 探针；**绝不允许**出现按 (代码, 报告期) 分块的对查询。
    assert len(statements) == 1
    assert "LIMIT 1" in statements[0]
    assert "OR (stock_code = ?" not in statements[0]


def test_published_override_still_returned_when_present(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """短路不能把"有覆写"也短路掉：有 published 覆写时照旧返回。"""
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE", is_listed=True)
    seed.snapshot("000001", "2025-12-31")
    with sqlite_store.transaction() as conn:
        SeedSqlite(sqlite_store).join(conn).override(
            "000001", "total_assets", 200,
            report_date="2025-12-31", reason="verified", status="published",
            created_at=sql_now(),
        )

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    assert engine._published_statement_overrides("m.is_listed IS TRUE", []) == [
        ("000001", "2025-12-31", "total_assets", 200.0)
    ]


# ─── 3. 内容纪元缓存：令牌变 → 必须重算；令牌取不到 → 必须不缓存 ───────────

def test_serve_by_content_recomputes_when_token_changes(
    duckdb_store: DuckDBStore,
) -> None:
    calls: list[int] = []

    def factory() -> str:
        calls.append(len(calls))
        return f"value-{len(calls)}"

    first = read_cache.serve_by_content(duckdb_store, ("k",), (1, "a"), factory)
    again = read_cache.serve_by_content(duckdb_store, ("k",), (1, "a"), factory)
    moved = read_cache.serve_by_content(duckdb_store, ("k",), (2, "a"), factory)

    assert first == again == "value-1"
    assert moved == "value-2"
    assert len(calls) == 2


@pytest.mark.parametrize("token", [None, (None,), (0, None)])
def test_serve_by_content_never_caches_without_token(
    duckdb_store: DuckDBStore, token
) -> None:
    """"不知道数据变没变"必须等价于"当它变了"：令牌缺项一律现算。"""
    calls: list[int] = []

    def factory() -> int:
        calls.append(1)
        return len(calls)

    read_cache.serve_by_content(duckdb_store, ("k",), token, factory)
    read_cache.serve_by_content(duckdb_store, ("k",), token, factory)

    assert len(calls) == 2


def test_mixed_report_date_probe_recomputes_after_content_epoch_bump(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纪元 +1（= 库里写过东西）→ 混合报告期判定必须重算，不许回放旧名单。"""
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE", is_listed=True)
    seed.snapshot("000001", "2025-12-31", latest_close=10)

    content_epoch.bump(sqlite_store.paths.sqlite_path, reason="test")
    calls: list[int] = []
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    def fake_mismatches(duck) -> list[dict]:
        calls.append(1)
        return [{"stock_code": "000001"}]

    monkeypatch.setattr(
        "app.core.data_quality.snapshot_period_mismatches", fake_mismatches
    )

    assert engine._load_mixed_report_date_codes() == ["000001"]
    # 同一纪元：命中缓存，不再重算。
    assert engine._load_mixed_report_date_codes() == ["000001"]
    assert len(calls) == 1

    # 数据变了一次（纪元 +1）→ 必须重算。
    content_epoch.bump(sqlite_store.paths.sqlite_path, reason="test-write")
    assert engine._load_mixed_report_date_codes() == ["000001"]
    assert len(calls) == 2


def test_mixed_report_date_probe_does_not_cache_without_content_epoch(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纪元读不到（=0）时**一次都不许缓存**：宁可慢，也不许假绿。"""
    monkeypatch.setattr(read_cache, "content_epoch_token", lambda sqlite: 0)
    calls: list[int] = []
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    def fake_mismatches(duck) -> list[dict]:
        calls.append(1)
        return []

    monkeypatch.setattr(
        "app.core.data_quality.snapshot_period_mismatches", fake_mismatches
    )

    engine._load_mixed_report_date_codes()
    engine._load_mixed_report_date_codes()

    assert len(calls) == 2


# ─── 3b. 裸写就地改**一个值**：两条防线都必须让缓存失效（2026-11-25 审查构造）──
#
# 独立审查原文：「防线完全依赖'所有写都走 DuckDBStore'；仓库里存在裸
# `duckdb.connect(path)` 的写脚本，`_guarded_duckdb_connect` 只关缓存连接、
# 不 bump epoch。现有 11 条测试没有覆盖这个场景。」
#
# 审查的实际构造（本节的用例逐字复刻它的形状）：
#   UPDATE income_statement SET revenue=123 …（**只改一个值**）
#   → 行数没变、MAX(report_date) 没变、content_epoch 没变（裸 connect 不记账）
#   → 缓存回放旧名单 → 真值多出 1 只被剔除的股票 → 筛选**多算 1 只**，全程静默。


def _seed_one_value_away_from_mismatch(
    duckdb_store: DuckDBStore, *, mismatched: tuple[str, ...] = ("000001", "000003")
) -> None:
    """`mismatched` 里的股票真不一致；`000002` 只差 `revenue` **一个值**。

    000002 的证据形状（整条缺陷的关键，别改成"加一行/加一期"）：
    * 2025-12-31 三表齐全 → 完整期 = 快照期 → 一致；
    * 2026-03-31 三表**都有行**，但 `income_statement.revenue IS NULL`
      → 该期不构成完整期，`cp.complete_date` 仍是 2025-12-31 → 仍一致；
    * 把 revenue 改成任意非空值 → 2026-03-31 变成完整期 → 快照期与之不一致。
    整个过程**行数、MAX(report_date)、content_epoch 三样都不变** —— 旧指纹全瞎。
    """
    # 整段绑在**一条**写连接上：几十条 INSERT 各自提交（每条一次 WAL fsync）要
    # 2s+；绑一条连接只提交一次，语义不变（`Seed(..., conn=...)` 的既定用法）。
    with duckdb_store.write_connection() as conn:
        seed = Seed(duckdb_store, conn=conn)
        for code in (*mismatched, "000002"):
            seed.stock(code, name=f"N{code}", exchange="SZSE", is_listed=True,
                       is_st=False, is_suspended=False, listing_date="2020-01-01")
            seed.snapshot(code, "2025-12-31", pe_ttm=8, latest_close=10)
        for code in mismatched:
            for period in ("2025-09-30", "2025-12-31"):
                seed.balance(code, period, total_assets=100, total_liabilities=20,
                             total_equity=80)
                seed.income(code, period, revenue=100, parent_net_profit=10)
                seed.cash_flow(code, period, cf_from_operating=10)
            # 2025-12-31 的 revenue 抹成 NULL（行还在）→ 该期不构成完整期
            conn.execute(
                "UPDATE income_statement SET revenue = NULL "
                "WHERE stock_code = ? AND report_date = '2025-12-31'",
                [code],
            )
        for period in ("2025-12-31", "2026-03-31"):
            seed.balance("000002", period, total_assets=100, total_liabilities=20,
                         total_equity=80)
            seed.cash_flow("000002", period, cf_from_operating=10)
        seed.income("000002", "2025-12-31", revenue=100, parent_net_profit=10)
        seed.income("000002", "2026-03-31", revenue=None, parent_net_profit=10)


def _external_single_value_update(db_path) -> None:
    """在**另一个进程**里裸 `duckdb.connect()` 就地改一个值。

    为什么必须真起子进程：`_guarded_duckdb_connect` 只在本进程生效，而仓库里的
    裸写脚本（`scripts/rebuild_duckdb.py` / `scripts/repair_*.py` 等）都是**独立
    进程** —— 它们既关不掉本进程的只读缓存，也不会推进本进程看到的账本。
    这正是审查构造的真实形状，也是"去掉内容摘要必须变红"的变异靶子。
    """
    code = (
        "import duckdb\n"
        f"conn = duckdb.connect({str(db_path)!r})\n"
        "conn.execute(\"UPDATE income_statement SET revenue = 123 \"\n"
        "             \"WHERE stock_code = '000002' \"\n"
        "             \"AND report_date = '2026-03-31'\")\n"
        "conn.close()\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_batched_reader_rejects_multi_statement_select(duckdb_store: DuckDBStore) -> None:
    """`_BatchedReader` 必须与 `read_query()` 同样拒绝多语句（审查点名的语义差异）。

    实测差异：`SELECT 1 AS a; SELECT 2 AS b` 会被执行并返回**最后一条**
    （`[{'b': 2}]`），而 store 抛 `ValueError`。`_read_scope()` 拿的是
    `read_connection()`，写锁回退窗口里它可能是读写连接 —— 多语句就等于多出一条
    没人审过的可执行语句。
    """
    with duckdb_store.read_connection() as conn:
        with pytest.raises(ValueError, match="exactly one SELECT"):
            _BatchedReader(conn).read_query("SELECT 1 AS a; SELECT 2 AS b")
        # 单条 SELECT 照常可用（校验不能把正常路径搞坏）
        assert _BatchedReader(conn).read_query("SELECT 1 AS a") == [{"a": 1}]

    # 与 store 逐字一致（同一条 SQL 两边都拒绝）
    with pytest.raises(ValueError, match="exactly one SELECT"):
        duckdb_store.read_query("SELECT 1 AS a; SELECT 2 AS b")


def test_content_fingerprint_is_content_sensitive_not_just_counts_and_max(
    duckdb_store: DuckDBStore,
) -> None:
    """就地改**一个值**：内容摘要必变，而"行数 + 最大报告期"必不变。

    这条直接钉住旧指纹的盲区：把 `digest_*` 退回"行数 + 最大期"（变异）→ 必红。
    """
    _seed_one_value_away_from_mismatch(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)

    before = engine._mixed_report_content_fingerprint()
    assert before is not None
    cheap_before = [item for item in before if item[0].startswith(("n_", "max_"))]
    digest_before = [item for item in before if item[0].startswith("digest_")]
    assert len(digest_before) == 5, before  # 5 张表各一份内容摘要

    with duckdb_store.write_connection() as conn:
        conn.execute(
            "UPDATE income_statement SET revenue = 123 "
            "WHERE stock_code = '000002' AND report_date = '2026-03-31'"
        )

    after = engine._mixed_report_content_fingerprint()
    assert after is not None
    assert [item for item in after if item[0].startswith(("n_", "max_"))] == cheap_before, (
        "行数/最大期本来就不该变 —— 这正是旧指纹抓不到就地改值的原因"
    )
    assert [item for item in after if item[0].startswith("digest_")] != digest_before, (
        "就地改了一个值但内容摘要没变 → 缓存会回放假名单 → 筛选静默多算/少算"
    )


def test_bare_connect_single_value_update_bumps_epoch_and_recomputes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """防线 1：**进程内**裸 `duckdb.connect()` 也是可写连接 → 打开即推进纪元。

    审查实测旧行为：裸 connect 只关缓存只读连接、**不 bump 纪元**，于是按纪元
    键控的缓存原样回放。现在同一个包装里也要记账。
    """
    _seed_one_value_away_from_mismatch(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    assert engine._load_mixed_report_date_codes() == ["000001", "000003"]
    assert engine._load_mixed_report_date_codes() == ["000001", "000003"]  # 命中缓存

    before_epoch = content_epoch.current(sqlite_store.paths.sqlite_path)
    connection = duckdb.connect(str(duckdb_store.db_path))
    try:
        connection.execute(
            "UPDATE income_statement SET revenue = 123 "
            "WHERE stock_code = '000002' AND report_date = '2026-03-31'"
        )
    finally:
        connection.close()

    assert content_epoch.current(sqlite_store.paths.sqlite_path) > before_epoch, (
        "裸可写 connect 没有推进内容纪元：第一层防线还是缺口"
    )
    assert engine._load_mixed_report_date_codes() == ["000001", "000002", "000003"]


def test_external_script_single_value_update_recomputes_without_epoch_bump(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """防线 2（主防线）：**另一个进程**的裸写不记账 → 只有内容摘要能发现。

    与上一条的分工：上一条钉"同进程裸写要记账"，这一条钉"跨进程裸写即使纪元
    完全没动，也必须靠内容摘要重算"。把 `SUM(hash(...))` 退回"行数 + 最大期"
    （变异）→ 这条必红。
    """
    _seed_one_value_away_from_mismatch(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    assert engine._load_mixed_report_date_codes() == ["000001", "000003"]
    assert engine._load_mixed_report_date_codes() == ["000001", "000003"]  # 命中缓存

    # 先还掉本进程的只读句柄：跨进程写脚本运行时，本进程本来就不该持着它。
    duckdb_store.close()
    before_epoch = content_epoch.current(sqlite_store.paths.sqlite_path)
    _external_single_value_update(duckdb_store.db_path)

    assert content_epoch.current(sqlite_store.paths.sqlite_path) == before_epoch, (
        "另一个进程的裸写本来就不记账 —— 这条用例的前提被打破了"
    )
    assert engine._load_mixed_report_date_codes() == ["000001", "000002", "000003"], (
        "内容摘要没抓住就地改值 → 缓存回放旧名单 → 筛选多算 1 只"
    )


def test_cached_empty_list_recomputed_when_truth_turns_non_empty(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """反向：缓存的是**空名单**，真值变成 1 只 → 同样必须重算（不许回放 []）。"""
    _seed_one_value_away_from_mismatch(duckdb_store, mismatched=())
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    assert engine._load_mixed_report_date_codes() == []
    assert engine._load_mixed_report_date_codes() == []  # 缓存命中空名单

    duckdb_store.close()
    before_epoch = content_epoch.current(sqlite_store.paths.sqlite_path)
    _external_single_value_update(duckdb_store.db_path)

    assert content_epoch.current(sqlite_store.paths.sqlite_path) == before_epoch
    assert engine._load_mixed_report_date_codes() == ["000002"], (
        "缓存了 [] 就永远返回 [] → 本该剔除的股票留在池里，筛选多算"
    )


def test_mixed_report_date_probe_does_not_cache_without_fingerprint(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兜底原则：指纹**算不出**（`None`）→ 一次都不许缓存，直接现算。"""
    content_epoch.bump(sqlite_store.paths.sqlite_path, reason="seed")
    calls: list[int] = []
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)

    def fake_mismatches(duck) -> list[dict]:
        calls.append(1)
        return []

    monkeypatch.setattr(engine, "_mixed_report_content_fingerprint", lambda: None)
    monkeypatch.setattr(
        "app.core.data_quality.snapshot_period_mismatches", fake_mismatches
    )

    engine._load_mixed_report_date_codes()
    engine._load_mixed_report_date_codes()

    assert len(calls) == 2


def test_content_fingerprint_returns_none_when_over_budget(
    duckdb_store: DuckDBStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兜底原则：指纹**太慢**（超预算）→ 返回 `None` → 调用方不缓存。

    预算只对"纯查询成本"生效（`_reader` 存在时）；直连调用（`_reader is None`）
    的开连接开销不算在内容摘要头上。
    """
    engine = ScreeningEngine(duck=duckdb_store)
    monkeypatch.setattr(engine_module, "_CONTENT_FINGERPRINT_WARMED", True)
    monkeypatch.setattr(
        engine, "_read_query", lambda sql, params=None: [{"digest_meta": "1"}]
    )
    monkeypatch.setattr(engine, "_reader", object())

    monkeypatch.setattr(engine_module, "MAX_CONTENT_FINGERPRINT_MS", 0.0)
    assert engine._mixed_report_content_fingerprint() is None

    monkeypatch.setattr(engine_module, "MAX_CONTENT_FINGERPRINT_MS", 1e9)
    assert engine._mixed_report_content_fingerprint() == (("digest_meta", "1"),)

    # 算不出（例如 DuckDB 没有 `hash()`、列缺失）→ 同样是 `None`（不缓存）
    def boom(sql: str, params=None):
        raise RuntimeError("hash() 不可用")

    monkeypatch.setattr(engine, "_read_query", boom)
    assert engine._mixed_report_content_fingerprint() is None


# ─── 4. 批量化不改变 run() 的结果集（逐股逐列）──────────────────────────
def test_run_results_identical_with_and_without_read_scope(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把批量化换成"每条查询各开一条连接"，结果集必须逐股逐列相同。"""
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE", is_listed=True,
               is_st=False, is_suspended=False, listing_date="2020-01-01")
    seed.stock("000002", name="Two", exchange="SZSE", is_listed=True,
               is_st=False, is_suspended=False, listing_date="2020-01-01")
    seed.snapshot("000001", "2025-12-31", latest_close=10, pe_ttm=8)
    seed.snapshot("000002", "2025-12-31", latest_close=20, pe_ttm=30)

    rule = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": "<", "value": 15},
        ]},
        "sort": [{"field": "pe_ttm", "direction": "asc"}],
        "columns": ["stock_code", "pe_ttm"],
    }
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    batched = engine.run(dict(rule), min_listing_years=0)

    def unbatched_run():
        plain = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
        # 让 `_read_scope()` 退化成"每条查询各开一条连接"
        monkeypatch.setattr(
            plain, "_read_scope", lambda: _NullScope(plain)
        )
        return plain.run(dict(rule), min_listing_years=0)

    plain_result = unbatched_run()

    assert batched["total"] == plain_result["total"] == 1
    assert batched["base_pool_size"] == plain_result["base_pool_size"] == 2
    assert batched["data_date"] == plain_result["data_date"]
    assert batched["results"] == plain_result["results"]
    assert batched["results"] == [{"stock_code": "000001", "name": "One", "pe_ttm": 8.0}]


class _NullScope:
    """`_read_scope()` 的空实现：不共用连接，全部退回 open-per-query。"""

    def __init__(self, engine: ScreeningEngine) -> None:
        self._engine = engine

    def __enter__(self):
        self._engine._reader = None
        return None

    def __exit__(self, *exc_info) -> bool:
        self._engine._reader = None
        return False


# ─── 5. 耗时断言（确定性版本）：一次 run() 开了几条只读连接 ──────────────────
#
# 为什么不直接断言墙钟：CI/共享机器上毫秒数会漂，那种断言只能靠"放宽阈值"存活。
# 而这次提速的**机制**是确定的 —— 正式库 open-per-query 每条连接 ~80ms，
# 改造前一次 run() 要开 6-8 条，改造后固定 2 条（探针一批 + 主查询一批）。
# 于是把"耗时"断言成"开连接次数"：把批量化去掉，这条必红。

def test_run_opens_fewer_read_connections_than_open_per_query(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确定性机制断言（不看墙钟）：批量化必须把 N 次 `read_query()` 变成 2 批次。

    为什么用"调用次数"而不是"墙钟毫秒"或"开连接次数"：毫秒会随机器漂；
    而测试档 `VD_ENV=test` 默认**开着**只读连接复用（`duck.read_cache`），
    开连接次数在测试里恒为 0，量不到东西。生产档没有复用，每调一次
    `read_query()` 就是一次 ~80ms 的开连接 —— 所以"`read_query()` 被调用了几次"
    正是这次提速的直接代理指标。
    """
    from app.core.storage import duckdb_store as duckdb_store_module

    seed = Seed(duckdb_store)
    for code in ("000001", "000002", "000003"):
        seed.stock(code, name=f"N{code}", exchange="SZSE", is_listed=True,
                   is_st=False, is_suspended=False, listing_date="2020-01-01")
        seed.snapshot(code, "2025-12-31", latest_close=10, pe_ttm=8)
        seed.balance(code, "2025-12-31", total_assets=1_000_000_000)

    rule = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": "<", "value": 15},
            {"field": "balance.total_assets", "op": ">", "value": 1},
        ]},
        "columns": ["stock_code", "pe_ttm"],
    }

    calls = {"read_query": 0, "read_connection": 0}
    original_read_query = duckdb_store_module.DuckDBStore.read_query
    original_read_connection = duckdb_store_module.DuckDBStore.read_connection

    def spy_read_query(self, sql, params=None):
        calls["read_query"] += 1
        return original_read_query(self, sql, params)

    @contextlib.contextmanager
    def spy_read_connection(self):
        calls["read_connection"] += 1
        with original_read_connection(self) as conn:
            yield conn

    monkeypatch.setattr(duckdb_store_module.DuckDBStore, "read_query", spy_read_query)
    monkeypatch.setattr(
        duckdb_store_module.DuckDBStore, "read_connection", spy_read_connection
    )

    def measure(disable_batching: bool) -> dict:
        engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
        if disable_batching:
            # 「变异」：把批量读作用域换成空实现 = 每条探针各开一条连接
            monkeypatch.setattr(engine, "_read_scope", lambda: _NullScope(engine))
        calls["read_query"] = calls["read_connection"] = 0
        # masked_fields=[] 复刻 Web 路径（遮蔽清单由 API 侧传进来，引擎不再现算，
        # 那 3 条 asset_health 探针不属于本次优化的范围，别混进计数）。
        result = engine.run(dict(rule), min_listing_years=0, masked_fields=[])
        result["_calls"] = dict(calls)
        return result

    batched = measure(disable_batching=False)
    plain = measure(disable_batching=True)

    assert batched["total"] == plain["total"] == 3
    assert batched["results"] == plain["results"]
    # 未批量化：每次探针都单独走 `read_query()`（改造前实测 5-7 次）
    assert plain["_calls"]["read_query"] >= 5, plain["_calls"]
    assert plain["_calls"]["read_connection"] == 0, plain["_calls"]
    # 批量化：`read_query()` 一次都不用，改成固定 2 个批次
    assert batched["_calls"]["read_query"] == 0, batched["_calls"]
    assert batched["_calls"]["read_connection"] == 2, batched["_calls"]
