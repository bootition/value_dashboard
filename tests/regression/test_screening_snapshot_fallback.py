"""更新窗口内的筛选**数据**解耦：快照库兜底（2026-11-20，reports/151）。

## 事故（第一版修错了什么）

第一版把"让更新期间能筛选"做成了**结果回放**（`read_cache` + `_screening_run_key`）。
但键里含**规则体 + 基础池 + 列 + strict + 数据指纹**，于是只有"完全相同的请求
曾经成功过"才命中：

- 用户换个条件 → 新键 → miss；
- 用户第一次在更新窗口里筛 → 从来没成功过 → miss。

两种都 409。服务日志实证：

```
19:29:17  筛选完成: 590 条, 耗时 1406ms          ← curl 那次（键 A）
19:29:42  启动自动更新子进程...
19:42:33  筛选运行被写锁挡住且无上次成功结果      ← 用户 UI 那次（键 B）→ 失败
```

**回放不是解耦**：它只保证"同一个问题再问一次"能用。

## 本文件守住的契约

1. **任意新条件**（从未跑过）+ 主库被写锁 → **200 且有结果**，
   `served_from == "snapshot"`，带 `as_of` / `stale=True` / 中文 `notice`；
2. **快照与真库结果完全一致**：同一条件两边跑，`total`/条数/股票代码集合/逐行
   内容全等（这是本任务最重要的正确性断言）；
3. **快照缺失 + 库被锁 → 如实 409**（不许假装成功，更不许返回空集冒充"没有符合的股票"）；
4. **原子性**：生成中途失败 → 旧快照仍可用、不留半个文件；
5. **幂等**：同一份数据连建两次，表清单与行数一致；
6. **物化范围**只由引擎的引用面决定：`price_daily_raw/qfq`（1750 万行）**不许**进快照。

## 隔离

用 `duckdb_store` / `sqlite_store` fixture（`--basetemp` 下的独立临时库），
**绝不碰** `data/valuedashboard.duckdb`。锁用「让 `read_query` 抛
`DuckDBReadLockedError`」模拟，与真实写进程持锁后的异常类型逐字一致。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage import read_cache, screening_snapshot
from app.core.storage.duckdb_store import DuckDBReadLockedError, DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed

LIQUOR = "000858"
VANKE = "000002"
MAOTAI = "600519"
MINGHANG = "601398"

PERIOD = "2026-09-30"

LOCKED_MESSAGE = (
    'IO Error: Cannot open file "valuedashboard.duckdb": '
    "另一个程序正在使用此文件，进程无法访问。"
)


class LockedDuck:
    """任何 `read_query` 都抛 `DuckDBReadLockedError`（锁的是**文件**，不是某张表）。"""

    def __init__(self, inner: DuckDBStore) -> None:
        self._inner = inner
        self.db_path = inner.db_path
        self.read_attempts = 0

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        self.read_attempts += 1
        raise DuckDBReadLockedError(LOCKED_MESSAGE)

    def read_connection(self):
        self.read_attempts += 1
        raise DuckDBReadLockedError(LOCKED_MESSAGE)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture(autouse=True)
def _clean_read_cache():
    read_cache.clear_cache()
    yield
    read_cache.clear_cache()


def _seed(duck: DuckDBStore) -> None:
    seed = Seed(duck)
    rows = (
        # code, name, csrc_l1, pe_ttm, roe, market_cap, revenue, assets, fcf_margin
        (LIQUOR, "五 粮 液", "制造业", 12.0, 0.22, 5.0e11, 8.0e10, 3.0e11, 0.12),
        (VANKE, "万  科Ａ", "房地产业", 8.0, 0.05, 1.0e11, 4.0e11, 1.5e12, 0.02),
        (MAOTAI, "贵州茅台", "制造业", 30.0, 0.30, 2.0e12, 1.5e11, 3.0e11, 0.45),
        (MINGHANG, "工商银行", "金融业", 5.0, 0.11, 1.8e12, 6.0e11, 4.0e13, 0.30),
    )
    for code, name, csrc_l1, pe, roe, cap, revenue, assets, fcf in rows:
        seed.stock(
            code, name=name, exchange="SSE", csrc_l1=csrc_l1,
            csrc_l2=f"{csrc_l1}子类", is_listed=True,
            is_st=False, is_suspended=False, listing_date="2005-01-04",
            total_shares=1.0e9, circ_shares=8.0e8,
        )
        seed.snapshot(
            code, PERIOD, pe_ttm=pe, pb_mrq=1.5, roe=roe, roa=roe / 2,
            total_market_cap=cap, latest_close=12.0, latest_price_date="2026-11-16",
            ma20=11.0, ttm_dividend_yield=3.2, period_return=0.05,
        )
        seed.balance(code, PERIOD, total_assets=assets, total_liabilities=assets * 0.4,
                     total_equity=assets * 0.6, total_equity_parent=assets * 0.55)
        seed.income(code, PERIOD, revenue=revenue, parent_net_profit=revenue * 0.15,
                    deducted_net_profit=revenue * 0.14,
                    asset_impairment_loss=0.0, credit_impairment_loss=0.0)
        seed.cash_flow(code, PERIOD, cf_from_operating=revenue * 0.2)
        seed.extended(
            code, PERIOD, calculated_at="2026-11-16 00:00:00", source="test", data_version=1,
            fcf_margin=fcf, inventory_turnover=2.0,
        )
        # strict 模式的血缘：每只股票给 `income.revenue` / `balance.total_assets`
        # 各两条审计（旧的一条 + 新的一条），最新一条是 strict。
        for field, value in (("revenue", revenue), ("total_assets", assets)):
            seed.source_audit(
                code, field, report_date=PERIOD, value=value * 0.9, source="eastmoney",
                confidence="approximate", fetch_time="2026-09-01 00:00:00",
                raw_response_hash="a" * 64,
            )
            seed.source_audit(
                code, field, report_date=PERIOD, value=value, source="eastmoney",
                confidence="strict", fetch_time="2026-10-08 00:00:00",
                raw_response_hash="b" * 64,
            )


def _snapshot_app(duck: Any, sqlite: SQLiteStore, rule_json: str) -> FastAPI:
    from app.web.api.screening import router as screening_router

    sqlite.execute(
        """INSERT INTO screening_rules (id, name, version, rule_json, locked_indicators)
           VALUES (?, ?, ?, ?, ?)""",
        [77, "快照兜底用例", 1, rule_json, "{}"],
    )
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.state.startup_readiness = {"ready": True}
    app.include_router(screening_router)

    from fastapi.responses import JSONResponse

    @app.exception_handler(DuckDBReadLockedError)
    async def _handler(_request, _error: DuckDBReadLockedError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "数据正在自动更新中，请稍后刷新", "reason_code": "duckdb_read_locked"},
        )

    return app


#: **从来没跑过**的条件：ROE 高于 20%（快照里恰好两只满足）。
NEVER_RUN_RULE = {
    "conditions": {
        "logic": "AND",
        "rules": [{"field": "roe", "op": ">=", "value": 0.2}],
    },
    "sort": [{"field": "roe", "direction": "desc"}],
    "columns": ["stock_code", "name", "roe"],
}


def _build_snapshot(duck: DuckDBStore) -> dict[str, Any]:
    info = screening_snapshot.build_screening_snapshot(duck, reason="regression")
    assert info is not None, "快照生成失败"
    return info


# ─── 1. 核心：写锁 + 从没跑过的新条件 ⇒ 200 且 served_from=snapshot ──────


def test_new_rule_while_locked_is_served_from_snapshot(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed(duckdb_store)
    info = _build_snapshot(duckdb_store)
    # 主库被锁：任何 read_query 都是 DuckDBReadLockedError
    locked = LockedDuck(duckdb_store)
    app = _snapshot_app(locked, sqlite_store, json.dumps(NEVER_RUN_RULE))

    response = TestClient(app).post(
        "/api/screening/run", json={"rule_id": 77, "rule_version": 1},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["served_from"] == "snapshot"
    assert body["stale"] is True
    assert body["as_of"] == info["built_at"]
    assert body["reason_code"] == "screening_snapshot"
    assert "数据正在自动更新" in body["notice"]
    assert info["built_at"][:10] in body["notice"]
    # 真的有结果，不是空集冒充成功
    assert body["total"] >= 1
    assert {row["stock_code"] for row in body["results"]} == {LIQUOR, MAOTAI}
    # 主库确实一次都没读成功过（锁是真的）
    assert locked.read_attempts > 0
    # 快照口径必须自曝家门
    assert "screening_served_from_snapshot" in body["disclosed_warnings"]
    assert body["readiness_source"] == "snapshot"


def test_snapshot_run_does_not_touch_live_database(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """快照路径不许回写主库：锁窗口里"能读"不能变成"顺手写一下"。"""
    _seed(duckdb_store)
    _build_snapshot(duckdb_store)
    before = duckdb_store.read_query("SELECT COUNT(*) AS n FROM indicator_snapshot")
    app = _snapshot_app(LockedDuck(duckdb_store), sqlite_store, json.dumps(NEVER_RUN_RULE))

    assert TestClient(app).post(
        "/api/screening/run", json={"rule_id": 77, "rule_version": 1},
    ).status_code == 200

    after = duckdb_store.read_query("SELECT COUNT(*) AS n FROM indicator_snapshot")
    assert after == before


# ─── 2. 正确性：快照 vs 真库，结果集必须完全相同 ──────────────────────────

_CONSISTENCY_RULES = {
    "估值+盈利": {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": "between", "value": [0, 20]},
            {"field": "roe", "op": ">=", "value": 0.1},
        ]},
        "sort": [{"field": "pe_ttm", "direction": "asc"}],
        "columns": ["stock_code", "name", "pe_ttm", "roe"],
    },
    "三表科目+派生字段": {
        "conditions": {"logic": "AND", "rules": [
            {"field": "balance.total_assets", "op": ">", "value": 1e11},
            {"field": "income.revenue", "op": ">", "value": 1e10},
            {"field": "cashflow.cf_from_operating", "op": ">", "value": 0},
            {"field": "income.impairment_loss_positive", "op": ">=", "value": 0},
        ]},
        "sort": [{"field": "total_market_cap", "direction": "desc"}],
        "columns": ["stock_code", "total_market_cap", "balance.total_assets"],
    },
    "扩展域+技术指标": {
        "conditions": {"logic": "AND", "rules": [
            {"field": "fcf_margin", "op": ">", "value": 0.05},
            {"field": "ma20", "op": ">", "value": 0},
            {"field": "ttm_dividend_yield", "op": ">", "value": 1},
        ]},
        "sort": [{"field": "fcf_margin", "direction": "desc"}],
        "columns": ["stock_code", "fcf_margin", "ma20", "ttm_dividend_yield"],
    },
    "strict 血缘": {
        "conditions": {"logic": "AND", "rules": [
            {"field": "income.revenue", "op": ">", "value": 1e9},
            {"field": "balance.total_assets", "op": ">", "value": 1e10},
        ]},
        # 排序/结果列也参与 strict 谓词 → 只能引用已播种血缘的字段。
        "sort": [{"field": "income.revenue", "direction": "desc"}],
        "columns": ["stock_code", "income.revenue"],
    },
    "排名列": {
        "conditions": {"logic": "AND", "rules": [
            {"field": "roe", "op": "<=", "value": 2, "rank_mode": "market_rank"},
        ]},
        "sort": [{"field": "roe", "direction": "desc"}],
        "columns": ["stock_code", "roe", "roe_market_rank"],
    },
}


@pytest.mark.parametrize("name", sorted(_CONSISTENCY_RULES))
def test_snapshot_results_identical_to_live_database(
    duckdb_store: DuckDBStore, name: str,
) -> None:
    """同一条件分别在真库与快照上跑 ⇒ `total`/条数/代码集合/逐行内容全等。

    这是本任务最重要的断言：快照只是**换了一条连接**，SQL 一个字都不许变；
    一旦物化漏了某张表/某几列，这里就会立刻红（而不是等用户拿到错结果）。
    """
    from app.core.screening.engine import ScreeningEngine

    _seed(duckdb_store)
    info = _build_snapshot(duckdb_store)
    rule = _CONSISTENCY_RULES[name]

    strict = name.startswith("strict")
    live = ScreeningEngine(duck=duckdb_store, sqlite=None).run(
        rule, strict_only=strict, masked_fields=[],
    )
    store = screening_snapshot.SnapshotStore(
        screening_snapshot.snapshot_path(duckdb_store), info=info,
    )
    cached = ScreeningEngine(duck=store, sqlite=None).run(
        rule, strict_only=strict, masked_fields=[],
    )

    assert cached["total"] == live["total"]
    assert len(cached["results"]) == len(live["results"])
    assert [r["stock_code"] for r in cached["results"]] == [
        r["stock_code"] for r in live["results"]
    ]
    assert cached["results"] == live["results"]
    assert cached["base_pool_size"] == live["base_pool_size"]
    assert cached["data_date"] == live["data_date"]
    assert cached["warnings"] == live["warnings"]
    assert cached["strict_enforced_fields"] == live["strict_enforced_fields"]
    assert live["total"] >= 1, (
        f"用例本身必须真筛出东西，否则一致性是空断言 "
        f"(enforced={live['strict_enforced_fields']} avail={live['strict_availability']} "
        f"exempt={live['strict_exempt_fields']})"
    )


# ─── 3. 快照缺失 + 库被锁 ⇒ 如实 409 ────────────────────────────────────


def test_missing_snapshot_while_locked_is_honest_409(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed(duckdb_store)
    path = screening_snapshot.snapshot_path(duckdb_store)
    assert not path.exists()  # 故意不建快照
    app = _snapshot_app(LockedDuck(duckdb_store), sqlite_store, json.dumps(NEVER_RUN_RULE))

    response = TestClient(app).post(
        "/api/screening/run", json={"rule_id": 77, "rule_version": 1},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "data_update_in_progress"
    assert "数据正在自动更新" in detail["message"]
    assert "快照" in detail["message"]
    # 绝不假绿：没有结果、没有 served_from 标记
    assert "results" not in response.json()


def test_corrupt_snapshot_while_locked_is_honest_409(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """文件在、但不是本模块产的有效快照（无 meta 表）⇒ 当**没有**处理。"""
    _seed(duckdb_store)
    path = screening_snapshot.snapshot_path(duckdb_store)
    path.write_bytes(b"not a duckdb file")
    app = _snapshot_app(LockedDuck(duckdb_store), sqlite_store, json.dumps(NEVER_RUN_RULE))

    response = TestClient(app).post(
        "/api/screening/run", json={"rule_id": 77, "rule_version": 1},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason_code"] == "data_update_in_progress"


# ─── 4. 原子性 / 幂等 / 物化范围 ────────────────────────────────────────


def test_failed_rebuild_keeps_previous_snapshot_and_leaves_no_half_file(
    duckdb_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成中途失败 ⇒ 旧快照原样可用，且不留 `*.tmp-*` 半个文件。"""
    _seed(duckdb_store)
    first = _build_snapshot(duckdb_store)
    path = screening_snapshot.snapshot_path(duckdb_store)
    assert path.exists()

    def _boom(_conn: Any) -> list[tuple[str, str]]:
        raise RuntimeError("模拟物化中途炸掉")

    monkeypatch.setattr(screening_snapshot, "_create_statements", _boom)
    second = screening_snapshot.build_screening_snapshot(duckdb_store, reason="broken")

    assert second is None, "失败必须如实返回 None（不能假装建成了）"
    # 旧快照仍可用，且还是旧的那一份（built_at 未变）
    info = screening_snapshot.read_snapshot_info(path)
    assert info is not None and info["built_at"] == first["built_at"]
    store = screening_snapshot.SnapshotStore(path, info=info)
    assert store.read_query("SELECT COUNT(*) AS n FROM indicator_snapshot")[0]["n"] > 0
    # 不留临时文件
    leftovers = [
        p.name for p in path.parent.iterdir() if ".tmp-" in p.name
    ]
    assert leftovers == []


def test_failed_replace_keeps_previous_snapshot(
    duckdb_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rename 阶段失败（旧快照正被读开）⇒ 旧快照仍能用，临时文件被清掉。"""
    _seed(duckdb_store)
    first = _build_snapshot(duckdb_store)
    path = screening_snapshot.snapshot_path(duckdb_store)

    def _no_replace(_src, _dst) -> None:
        raise PermissionError("模拟 Windows 上旧快照被占用")

    monkeypatch.setattr(screening_snapshot.os, "replace", _no_replace)
    assert screening_snapshot.build_screening_snapshot(duckdb_store) is None

    info = screening_snapshot.read_snapshot_info(path)
    assert info is not None and info["built_at"] == first["built_at"]
    assert [p.name for p in path.parent.iterdir() if ".tmp-" in p.name] == []


def test_snapshot_build_is_idempotent(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    first = _build_snapshot(duckdb_store)
    second = _build_snapshot(duckdb_store)

    assert first["row_counts"] == second["row_counts"]
    assert set(first["row_counts"]) == set(second["row_counts"])
    info = screening_snapshot.read_snapshot_info(screening_snapshot.snapshot_path(duckdb_store))
    assert info is not None and info["row_counts"] == second["row_counts"]


def test_snapshot_materializes_only_screening_tables(duckdb_store: DuckDBStore) -> None:
    """物化范围只由引擎的引用面决定：1750 万行的 `price_daily_*` **不许**进快照。"""
    _seed(duckdb_store)
    info = _build_snapshot(duckdb_store)
    tables = set(info["row_counts"])

    assert {"stock_meta", "indicator_snapshot"} <= tables
    # 三表 + 扩展域只在用到时才有，但绝不能出现行情大表
    assert not any(table.startswith("price_daily") for table in tables)
    assert "price_daily_raw" not in tables and "price_daily_qfq" not in tables
    # 快照里确实没有行情大表
    store = screening_snapshot.SnapshotStore(
        screening_snapshot.snapshot_path(duckdb_store), info=info,
    )
    present = {
        row["table_name"] for row in store.read_query(
            "SELECT table_name FROM information_schema.tables"
        )
    }
    assert "price_daily_raw" not in present
    assert "price_daily_qfq" not in present


def test_ensure_skips_rebuild_when_snapshot_is_fresh(duckdb_store: DuckDBStore) -> None:
    """服务启动补建必须**幂等**：新鲜时一次重建都不许做（`built_at` 不变）。"""
    _seed(duckdb_store)
    first = _build_snapshot(duckdb_store)

    again = screening_snapshot.ensure_screening_snapshot(duckdb_store)

    assert again is not None and again["built_at"] == first["built_at"]


def test_ensure_rebuilds_when_snapshot_is_stale(duckdb_store: DuckDBStore) -> None:
    _seed(duckdb_store)
    _build_snapshot(duckdb_store)

    rebuilt = screening_snapshot.ensure_screening_snapshot(
        duckdb_store, max_age_seconds=0.0, reason="startup_backfill",
    )

    assert rebuilt is not None
    assert rebuilt["built_by"] == "startup_backfill"
