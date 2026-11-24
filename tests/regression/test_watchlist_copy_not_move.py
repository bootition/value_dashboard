"""从规则分组往自己的分组里放，必须是**复制**，不是移动（2026-11-27 用户裁决）。

用户原话：
> 「从规则筛选的分组移动到自选分组，我希望是复制，而不是移动。不然的话，如果下一次
>  我点击按照新的数据筛选，你是该把移出去的再移回来？还是如果它被踢出去了，你又该怎么做？」

移动会引出两个无解的语义问题：
1. 下一轮筛选（"替换"模式）要不要把被移出去的股票**移回来**？
2. 股票后来被规则**踢出**结果集，它是该跟着消失，还是留在用户自己的分组里？

复制则天然没有这些问题：**规则分组永远由规则的运行结果决定，用户的分组永远由用户决定。**

本用例钉住四条：
1. `mode="copy"`（**默认**）：原分组行必须还在；
2. 目标组新增的行是**纯手动行**（`source_rule_id` / `source_result_id` 均为 NULL）
   —— 因此规则"替换"刷新、以及删规则，都不会碰它；
3. 股票被规则踢出后，副本照样保留（这就是用户要的）；
4. `mode="move"` 仍保留旧语义（只有显式传才生效）。
"""

from __future__ import annotations

from typing import Any

from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed_sqlite import SeedSqlite

from .test_watchlist_groups_registry import _client

SOURCE = "规则分组"
TARGET = "我的观察池"


def _rows(sqlite: SQLiteStore, group: str) -> list[dict[str, Any]]:
    return sqlite.query(
        "SELECT stock_code, source_rule_id, source_result_id FROM watchlist "
        "WHERE group_name = ? ORDER BY stock_code",
        [group],
    )


def _setup(sqlite_store: SQLiteStore) -> list[str]:
    client = _client(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    codes = ["000001", "000002", "000003"]
    for code in codes:
        seed.watchlist(code, group_name=SOURCE)
    client.post("/api/watchlist/groups", json={"name": TARGET})
    return codes


def test_copy_keeps_source_rows_and_writes_manual_rows(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    codes = _setup(sqlite_store)
    before = _rows(sqlite_store, SOURCE)
    assert [row["stock_code"] for row in before] == codes

    resp = client.post("/api/watchlist/move", json={
        "stock_codes": codes,
        "from_group": SOURCE,
        "to_group": TARGET,
        # 不传 mode → 默认 copy
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "copy", body
    assert body["copied"] == len(codes), body
    assert body["moved"] == 0, body

    # ① 原分组一行都不许少（这是"复制"的定义）
    assert _rows(sqlite_store, SOURCE) == before
    # ② 副本是纯手动行：不带任何规则来源
    copied = _rows(sqlite_store, TARGET)
    assert [row["stock_code"] for row in copied] == codes
    assert all(row["source_rule_id"] is None for row in copied), copied
    assert all(row["source_result_id"] is None for row in copied), copied


def test_copy_survives_stock_being_kicked_out_of_rule(sqlite_store: SQLiteStore) -> None:
    """规则重新筛选后把这批股票踢出去 → 用户分组里的副本必须保留。"""
    client = _client(sqlite_store)
    codes = _setup(sqlite_store)
    client.post("/api/watchlist/move", json={
        "stock_codes": codes, "from_group": SOURCE, "to_group": TARGET,
    })

    # 等价于"规则换了一批结果、旧股票被踢出"
    sqlite_store.execute("DELETE FROM watchlist WHERE group_name = ?", [SOURCE])

    assert _rows(sqlite_store, SOURCE) == []
    assert len(_rows(sqlite_store, TARGET)) == len(codes), (
        "股票被规则踢出后，用户分组里的副本必须保留 —— 这是复制语义的全部意义"
    )


def test_move_mode_still_available_when_explicit(sqlite_store: SQLiteStore) -> None:
    """`mode="move"` 保留旧语义（只有显式传才生效，默认不再是移动）。"""
    client = _client(sqlite_store)
    codes = _setup(sqlite_store)
    resp = client.post("/api/watchlist/move", json={
        "stock_codes": codes, "from_group": SOURCE,
        "to_group": TARGET, "mode": "move",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "move"
    assert _rows(sqlite_store, SOURCE) == []
    assert len(_rows(sqlite_store, TARGET)) == len(codes)


def test_copy_twice_is_idempotent(sqlite_store: SQLiteStore) -> None:
    """重复复制同一批 → 不报错、不重复插（`already_in_target` 计数）。"""
    client = _client(sqlite_store)
    codes = _setup(sqlite_store)
    payload = {"stock_codes": codes, "from_group": SOURCE, "to_group": TARGET}
    client.post("/api/watchlist/move", json=payload)
    second = client.post("/api/watchlist/move", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["copied"] == 0
    assert second.json()["already_in_target"] == len(codes)
    assert len(_rows(sqlite_store, TARGET)) == len(codes)
