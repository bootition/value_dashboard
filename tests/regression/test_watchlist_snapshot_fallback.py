"""更新期间自选列表必须显示**上一次成功更新的数据**，而不是留白。

## 用户口径（2026-11-27）
> 「我不是说过，在数据更新的时候，你可以先显示之前还缓存留下的数据吗？
>  比如周二的数据正在更新，那就可以先显示周一的数据嘛。为什么一定要留空白呢？」

## 改前
主库被更新子进程写锁挡住时，`_fetch_stock_info` 直接降级 → 名称/指标全 `None`
→ 自选表格一整屏破折号、名称列只能拿代码兜底。

## 改后
同一份查询改打到 `screening_snapshot.duckdb`（上一次成功更新后物化，
`stock_meta` 6,286 行 + `indicator_snapshot` 5,575 行，列齐全，且**不受主库写锁影响**），
响应里 `enrichment="screening_snapshot"` + `enrichment_as_of` 说明是哪一轮的数据。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed_sqlite import SeedSqlite

from .test_watchlist_groups_registry import _client


class _LockedDuck:
    """主库被写进程持锁的鸭子类型（只读查询一律抛 `DuckDBReadLockedError`）。"""

    def __init__(self) -> None:
        self.calls = 0

    def read_query(self, sql: str, params: Any = None) -> list[dict]:
        self.calls += 1
        from app.core.storage.duckdb_store import DuckDBReadLockedError

        raise DuckDBReadLockedError("File is already open in another process")


def test_watchlist_falls_back_to_screening_snapshot(
    sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主库锁住 → 走物化快照，名称与指标必须有值（不许留白）。"""

    client = _client(sqlite_store, _LockedDuck())
    SeedSqlite(sqlite_store).watchlist("600519", group_name="我的自选")

    # 假快照 store：证明 list_watchlist 真的会去开 SnapshotStore 并复用它
    opened: list[Any] = []

    class FakeSnapshotStore:
        as_of = "2026-11-25T19:41:00Z"

        def __init__(self, path: Any, info: Any = None) -> None:
            opened.append(path)

        def read_query(self, sql: str, params: Any = None) -> list[dict]:
            return [{
                "stock_code": "600519", "name": "贵州茅台", "exchange": "SSE",
                "csrc_l1": "制造业", "listing_date": "2001-08-27",
                "latest_close": 1500.0, "pe_ttm": 22.5, "pb_mrq": 8.1,
                "roe": 0.31, "gross_margin": 0.91, "net_margin": 0.52,
                "debt_ratio": 0.18, "revenue_yoy": 0.16, "net_profit_yoy": 0.19,
                "dividend_yield": 0.021, "total_market_cap": 1.9e12,
                "circ_market_cap": 1.9e12,
            }]

    monkeypatch.setattr(
        "app.core.storage.screening_snapshot.SnapshotStore", FakeSnapshotStore,
    )
    monkeypatch.setattr(
        "app.core.storage.screening_snapshot.snapshot_path", lambda duck: "fake.duckdb",
    )

    body = client.get("/api/watchlist/list").json()
    item = body["items"][0]

    assert body["enrichment"] == "screening_snapshot", body.get("enrichment")
    assert body["enrichment_as_of"] == "2026-11-25T19:41:00Z"
    assert opened, "必须真的去开物化快照"
    # 关键：**不许留白**
    assert item["name"] == "贵州茅台", item["name"]
    assert item["pe_ttm"] == 22.5, item["pe_ttm"]
    assert item["roe"] == 0.31, item["roe"]
    assert item["total_market_cap"] == 1.9e12


def test_watchlist_degrades_when_snapshot_also_unavailable(
    sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照也读不到时，仍按"不可用"降级 —— 分组与代码照常可见，不许整页失败。"""
    client = _client(sqlite_store, _LockedDuck())
    SeedSqlite(sqlite_store).watchlist("600519", group_name="我的自选")

    class BoomStore:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("snapshot missing")

    monkeypatch.setattr("app.core.storage.screening_snapshot.SnapshotStore", BoomStore)
    monkeypatch.setattr(
        "app.core.storage.screening_snapshot.snapshot_path", lambda duck: "nope.duckdb",
    )

    body = client.get("/api/watchlist/list").json()
    assert body["enrichment"] == "unavailable"
    assert [i["stock_code"] for i in body["items"]] == ["600519"]
