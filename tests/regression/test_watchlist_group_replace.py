"""自选分组的「替换 / 合并」与「删除整个分组」（2026-11-25 用户实测）。

事故现场（正式库实测）：

1. 自选分组「往股东口袋装钱的大老公司」= 591 行，来自两次运行：
   `source_result_id=10`（2026-09-16 旧结果）220 行 +
   `source_result_id=12`（2026-11-23 新结果）371 行。
   自选里 63 只当前市值已 < 100 亿（最小 48.2 亿）—— 全是那批旧结果。
   根因：`POST /api/screening/add_to_watchlist` 只 `ON CONFLICT DO UPDATE`，
   从不删除「这次结果里已经没有」的旧行，而前端同一规则每次都用同一个分组名。
2. 自选页没有「删除整个分组」，用户只能一只一只删。

本文件锁死两条契约：

- `replace_group=True` 只删「该分组里由**其它筛选结果**带进来」的行；
  `source_result_id IS NULL` 的用户手动自选**永不删除**。
  默认 `replace_group=False` 保持旧语义（累加），既有脚本/调用方不回归。
- `DELETE /api/watchlist/groups/{name}` 删分组并返回真实 `deleted_rows`；
  `delete_rule=True` 时复用 `screening.delete_rule_with_guards` 的同一套守卫
  （被已保存结果 / 其它分组自选引用时不许删），并如实回报原因与条数；
  分组来自多个规则时不猜、只删分组并返回 `skipped_rules`。
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.watchlist import router as watchlist_router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite


def _client(sqlite: SQLiteStore, duck: DuckDBStore | None = None) -> TestClient:
    """只挂 watchlist 路由：删分组不读 DuckDB，规则删除走 screening 同一实现。"""
    app = FastAPI()
    app.state.sqlite = sqlite
    if duck is not None:
        app.state.duck = duck
    app.include_router(watchlist_router)
    return TestClient(app)


def _screening_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    """`add_to_watchlist` 需要 `_require_current_screenability` 与 duck state。"""
    from app.web.api.screening import router as screening_router

    insert_matching_trading_calendar(duck, sqlite)
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(screening_router)
    return TestClient(app)


def _make_screenable(duck: DuckDBStore) -> None:
    Seed(duck).stock(
        "000001", name="One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    Seed(duck).snapshot("000001", "2025-12-31", pe_ttm=10)
    insert_minimum_screenable_data(duck)


def _seed_rule(sqlite: SQLiteStore, name: str) -> int:
    SeedSqlite(sqlite).screening_rule(name, 1, "{}", status="saved")
    return int(sqlite.query("SELECT id FROM screening_rules WHERE name = ?", [name])[0]["id"])


def _seed_result(
    sqlite: SQLiteStore,
    rule_id: int,
    codes: list[str],
    title: str = "result",
) -> int:
    rows = [{"stock_code": code, "name": code} for code in codes]
    with sqlite.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO screening_results
               (title, rule_id, rule_version, data_date, result_json, columns_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [title, rule_id, 1, "2025-12-31", json.dumps(rows), '["stock_code"]'],
        )
        return int(cursor.lastrowid)


def _watchlist_rows(sqlite: SQLiteStore, group_name: str) -> list[dict]:
    return sqlite.query(
        "SELECT stock_code, source_rule_id, source_result_id FROM watchlist"
        " WHERE group_name = ? ORDER BY stock_code",
        [group_name],
    )


# ── A. 保存到自选：替换 / 合并 ──────────────────────────────────────


def test_replace_group_removes_other_results_but_keeps_manual_rows(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """用户路径：同一规则重跑 → 选「替换」→ 旧结果行消失、手动行保留。"""
    _make_screenable(duckdb_store)
    rule_id = _seed_rule(sqlite_store, "往股东口袋装钱的大老公司")
    old_result = _seed_result(sqlite_store, rule_id, ["000001"], title="旧结果")
    new_result = _seed_result(sqlite_store, rule_id, ["000003", "000004"], title="新结果")

    seed = SeedSqlite(sqlite_store)
    group = "往股东口袋装钱的大老公司"
    seed.watchlist("000001", group_name=group, source_rule_id=rule_id, source_result_id=old_result)
    # 本次结果已有一行（同 result_id）：替换后必须保留并更新 added_at。
    seed.watchlist("000003", group_name=group, source_rule_id=rule_id, source_result_id=new_result)
    # 用户手动自选：没有筛选来源，任何模式下都不许删。
    seed.watchlist("600519", group_name=group)
    # 别的分组里同一旧结果的股票：删分组/替换都只影响目标分组。
    seed.watchlist("000001", group_name="别的分组", source_rule_id=rule_id, source_result_id=old_result)

    client = _screening_client(duckdb_store, sqlite_store)
    response = client.post(
        "/api/screening/add_to_watchlist",
        json={
            "stock_codes": ["000003", "000004"],
            "group": group,
            "result_id": new_result,
            "replace_group": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] == 1, body
    assert body["added"] == 2
    assert body["inserted"] == 1
    assert body["updated"] == 1
    assert body["manual_rows_preserved"] == 1
    assert body["replaced"] is True

    rows = _watchlist_rows(sqlite_store, group)
    assert rows == [
        {"stock_code": "000003", "source_rule_id": rule_id, "source_result_id": new_result},
        {"stock_code": "000004", "source_rule_id": rule_id, "source_result_id": new_result},
        {"stock_code": "600519", "source_rule_id": None, "source_result_id": None},
    ]
    # 其它分组原样保留
    assert _watchlist_rows(sqlite_store, "别的分组") == [
        {"stock_code": "000001", "source_rule_id": rule_id, "source_result_id": old_result},
    ]


def test_default_merge_keeps_previous_rows_for_backward_compatibility(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """默认 `replace_group=False`：与改动前一致（累加），旧结果行不被删。"""
    _make_screenable(duckdb_store)
    rule_id = _seed_rule(sqlite_store, "累加规则")
    old_result = _seed_result(sqlite_store, rule_id, ["000001"], title="旧结果")
    new_result = _seed_result(sqlite_store, rule_id, ["000003"], title="新结果")
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", group_name="累加组", source_rule_id=rule_id, source_result_id=old_result)
    seed.watchlist("600519", group_name="累加组")

    client = _screening_client(duckdb_store, sqlite_store)
    response = client.post(
        "/api/screening/add_to_watchlist",
        json={"stock_codes": ["000003"], "group": "累加组", "result_id": new_result},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] == 0
    assert body["replaced"] is False
    assert body["inserted"] == 1
    assert {row["stock_code"] for row in _watchlist_rows(sqlite_store, "累加组")} == {
        "000001", "000003", "600519",
    }


# ── B. 删除整个自选分组 ─────────────────────────────────────────────


def test_delete_group_removes_only_that_group(sqlite_store: SQLiteStore) -> None:
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", group_name="要删的分组")
    seed.watchlist("000002", group_name="要删的分组")
    seed.watchlist("600519", group_name="保留的分组")

    client = _client(sqlite_store)
    response = client.delete("/api/watchlist/groups/要删的分组")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_rows"] == 2
    assert body["rule_deleted"] is False
    assert body["rule_delete_status"] == "not_requested"
    assert _watchlist_rows(sqlite_store, "要删的分组") == []
    assert [row["stock_code"] for row in _watchlist_rows(sqlite_store, "保留的分组")] == ["600519"]


def test_delete_missing_group_returns_404(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    assert client.delete("/api/watchlist/groups/不存在").status_code == 404


def test_delete_group_with_rule_blocked_by_saved_results_reports_reason(
    sqlite_store: SQLiteStore,
) -> None:
    """规则被已保存结果引用 → 分组照删，规则不删，且如实说「几条结果」。"""
    rule_id = _seed_rule(sqlite_store, "被结果引用的规则")
    result_id = _seed_result(sqlite_store, rule_id, ["000001"])
    seed = SeedSqlite(sqlite_store)
    seed.watchlist(
        "000001", group_name="有结果的组", source_rule_id=rule_id, source_result_id=result_id,
    )

    client = _client(sqlite_store)
    response = client.delete("/api/watchlist/groups/有结果的组?delete_rule=true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_rows"] == 1
    assert body["rule_deleted"] is False
    assert body["rule_delete_status"] == "skipped"
    assert body["rule_delete_reason"]
    assert "1 条已保存的筛选结果" in body["rule_delete_reason"]
    skipped = body["skipped_rules"]
    assert len(skipped) == 1
    assert skipped[0]["rule_id"] == rule_id
    assert skipped[0]["reason_code"] == "rule_has_provenance_refs"
    # refs 如实带回条数（用户能知道是哪类引用、几条）
    assert skipped[0]["refs"]["results"] == 1
    assert skipped[0]["refs"]["watchlist"] == 0
    # 分组删了、规则还在
    assert _watchlist_rows(sqlite_store, "有结果的组") == []
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM screening_rules WHERE id = ?", [rule_id]
    )[0]["c"] == 1


def test_delete_group_with_rule_deletes_rule_when_unreferenced(
    sqlite_store: SQLiteStore,
) -> None:
    rule_id = _seed_rule(sqlite_store, "没人引用的规则")
    SeedSqlite(sqlite_store).watchlist("000001", group_name="干净组", source_rule_id=rule_id)

    client = _client(sqlite_store)
    response = client.delete("/api/watchlist/groups/干净组?delete_rule=true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_rows"] == 1
    assert body["rule_deleted"] is True
    assert body["deleted_rule_id"] == rule_id
    assert body["rule_delete_status"] == "deleted"
    assert _watchlist_rows(sqlite_store, "干净组") == []
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM screening_rules WHERE id = ?", [rule_id]
    )[0]["c"] == 0


def test_delete_group_with_rule_referenced_by_other_group_is_blocked(
    sqlite_store: SQLiteStore,
) -> None:
    """删分组后自选引用应按**其它分组**的残留计算，不许把自己算成可删。"""
    rule_id = _seed_rule(sqlite_store, "还被别的组引用")
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", group_name="要删的组", source_rule_id=rule_id)
    seed.watchlist("600519", group_name="别的组", source_rule_id=rule_id)

    client = _client(sqlite_store)
    body = client.delete("/api/watchlist/groups/要删的组?delete_rule=true").json()
    assert body["deleted_rows"] == 1
    assert body["rule_deleted"] is False
    assert body["skipped_rules"][0]["reason_code"] == "rule_has_provenance_refs"
    assert body["skipped_rules"][0]["refs"]["watchlist"] == 1
    assert "1 条自选记录" in body["rule_delete_reason"]
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM screening_rules WHERE id = ?", [rule_id]
    )[0]["c"] == 1


def test_delete_group_with_multiple_source_rules_never_guesses(
    sqlite_store: SQLiteStore,
) -> None:
    first_rule = _seed_rule(sqlite_store, "规则一")
    second_rule = _seed_rule(sqlite_store, "规则二")
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", group_name="混合组", source_rule_id=first_rule)
    seed.watchlist("000002", group_name="混合组", source_rule_id=second_rule)

    client = _client(sqlite_store)
    response = client.delete("/api/watchlist/groups/混合组?delete_rule=true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_rows"] == 2
    assert body["rule_deleted"] is False
    assert body["rule_delete_status"] == "skipped"
    assert "2 个不同筛选规则" in body["rule_delete_reason"]
    assert {item["rule_id"] for item in body["skipped_rules"]} == {first_rule, second_rule}
    assert {item["reason_code"] for item in body["skipped_rules"]} == {"multiple_source_rules"}
    # 一条规则都不许删
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM screening_rules")[0]["c"] == 2
    assert _watchlist_rows(sqlite_store, "混合组") == []


def test_groups_endpoint_exposes_runs_manual_and_rule_ids(sqlite_store: SQLiteStore) -> None:
    """前端确认框/删分组对话框依赖这三个字段，不能只给 cnt。"""
    rule_id = _seed_rule(sqlite_store, "分组事实")
    first_result = _seed_result(sqlite_store, rule_id, ["000001"])
    second_result = _seed_result(sqlite_store, rule_id, ["000002"])
    seed = SeedSqlite(sqlite_store)
    seed.watchlist(
        "000001", group_name="事实组", source_rule_id=rule_id, source_result_id=first_result,
    )
    seed.watchlist(
        "000002", group_name="事实组", source_rule_id=rule_id, source_result_id=second_result,
    )
    seed.watchlist("600519", group_name="事实组")

    client = _client(sqlite_store)
    body = client.get("/api/watchlist/groups").json()
    group = next(item for item in body["groups"] if item["group_name"] == "事实组")
    assert group["cnt"] == 3
    assert group["runs"] == 2
    assert group["manual"] == 1
    assert group["rule_ids"] == [rule_id]
