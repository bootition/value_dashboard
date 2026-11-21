"""筛选探针批量化 / 内容纪元缓存 —— 不许假绿（2026-11-21，`.planning/2026-screening-latency`）。

事故背景：正式库 `read_query()` / `SQLiteStore.query()` 都是 open-per-query（每条
~80ms / ~30ms），一次筛选要开 6-8 条连接，"开连接"就吃掉 1 秒左右；另有
`manual_overrides`（当时空表）13-14 条分块查询每次白跑 ~430ms。改造做了四件事：

1. `_read_scope()`：一次 `run()` 内的多条 DuckDB 探针共用**一条**只读连接；
2. 黑名单 + "有没有已发布覆写"探针共用**一条** SQLite 连接；
3. `_published_statement_overrides()`：没有 published 覆写 → 直接返回 `[]`；
4. `_load_mixed_report_date_codes()`：按**内容令牌**（`content_epoch` + 廉价内容
   指纹）记住结果。

本文件锁的是"**快但不错**"这一半 —— 每条优化都必须能被证明"不会少算、不会
拿旧数据冒充新数据"：

- 批量化（1/2）不许改变任何一条查询的返回语义；
- 短路（3）在空表时与原实现恒等，有覆写时一行不少；
- 缓存（4）**令牌一变必须重算**，**令牌取不到必须不缓存**（"不知道数据变没变"
  必须等价于"当它变了"，否则就是假绿）。
"""

from __future__ import annotations

import contextlib

import pytest

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
