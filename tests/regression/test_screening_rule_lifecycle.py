"""筛选规则生命周期：删除守卫只看真溯源锚点 + 原地更新（PUT）。

事故现场（2026-11-25 用户两张截图投诉）：

1. 「自选列表里面明明没有这个运行规则显示的分组，为什么它还删不掉？」
   `DELETE /rules/{id}` 把 `screening_runs`（每跑一次加一行的**运行日志**）
   也算引用 → 规则只要跑过一次就永远删不掉；而文案写「运行结果或自选列表」，
   把用户引去自选列表找一个根本不存在的引用。
2. 「改了点数字后不要自动保存成一条新规则，应该让我自己来命名……搞成一大堆
   V1、V2、V3 我以后怎么分辨？」—— `runScreening()` 草稿脏了就 POST
   `/rules/save`，后端 `MAX(version)+1` 追加一行同名规则。

本文件锁的是后端契约：删除只被 `screening_results` / `watchlist.source_rule_id`
拦住（文案点名哪一种、几条），`screening_runs` 行保留且不挡删除；规则编辑走
`PUT /rules/{id}` 原地升版本，`POST /rules/save` 同名直接 400。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

_RULE_JSON = {
    "conditions": {
        "logic": "AND",
        "rules": [{"field": "pe_ttm", "op": ">", "value": 10}],
    },
    "columns": ["stock_code", "pe_ttm"],
}


def _client(sqlite_store: SQLiteStore, duck: DuckDBStore | None = None) -> TestClient:
    app = FastAPI()
    app.state.sqlite = sqlite_store
    if duck is not None:
        app.state.duck = duck
    app.include_router(router)
    return TestClient(app)


def _save_rule(client: TestClient, name: str) -> dict:
    response = client.post(
        "/api/screening/rules/save",
        json={"name": name, "rule_json": _RULE_JSON},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed_run(sqlite_store: SQLiteStore, rule_id: int, run_id: str = "run-1") -> None:
    SeedSqlite(sqlite_store).screening_run(
        run_id,
        rule_id,
        1,
        result_json="[]",
        columns_json='["stock_code"]',
        sort_json="[]",
        base_pool_config="{}",
        strict_only=False,
        confidence_summary="{}",
        data_date="2025-12-31",
    )


def _seed_saved_result(sqlite_store: SQLiteStore, rule_id: int, title: str) -> int:
    with sqlite_store.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO screening_results
               (title, rule_id, rule_version, data_date, result_json, columns_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [title, rule_id, 1, "2025-12-31", '[{"stock_code":"000001"}]', '["stock_code"]'],
        )
        return int(cursor.lastrowid)


# ── 删除守卫 ────────────────────────────────────────────────────────


def test_rule_with_only_screening_runs_can_be_deleted_and_runs_survive(
    sqlite_store: SQLiteStore,
) -> None:
    """只有运行日志 → **可以删**；删完 runs 行仍在（审计保留）。

    旧守卫把 runs 当引用，这条规则（跑过一次）在任何用户视角里都删不掉。
    """
    client = _client(sqlite_store)
    rule_id = _save_rule(client, "ROCE v1")["rule_id"]
    _seed_run(sqlite_store, rule_id)

    response = client.delete(f"/api/screening/rules/{rule_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "deleted_rule_id": rule_id}
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM screening_rules WHERE id = ?", [rule_id]
    )[0]["c"] == 0
    # 审计日志行原样保留（rule_id 成为历史上的悬空值，这是有意的）
    assert sqlite_store.query(
        "SELECT run_id, rule_id FROM screening_runs WHERE run_id = ?", ["run-1"]
    ) == [{"run_id": "run-1", "rule_id": rule_id}]
    # 关外键只对**那一条连接**生效：新连接照旧拦住真正的溯源断链（不许把
    # 全库的外键检查悄悄关掉）。
    with pytest.raises(sqlite3.IntegrityError), sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO screening_results
               (title, rule_id, rule_version, data_date, result_json, columns_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ["悬空", rule_id, 1, "2025-12-31", "[]", "[]"],
        )


def test_rule_with_saved_results_reports_exact_saved_result_count(
    sqlite_store: SQLiteStore,
) -> None:
    client = _client(sqlite_store)
    rule_id = _save_rule(client, "有结果")["rule_id"]
    _seed_saved_result(sqlite_store, rule_id, "结果 A")
    _seed_saved_result(sqlite_store, rule_id, "结果 B")

    response = client.delete(f"/api/screening/rules/{rule_id}")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "rule_has_provenance_refs"
    assert detail["refs"]["results"] == 2
    assert "2 条已保存的筛选结果" in detail["message"]
    # 只拦结果时不许提"自选记录"（免得用户又去自选列表白找一遍）
    assert "自选记录" not in detail["message"]
    # 旧文案里的"运行结果"指向不明，永久禁用
    assert "运行结果" not in detail["message"]


def test_rule_with_watchlist_refs_reports_exact_watchlist_count(
    sqlite_store: SQLiteStore,
) -> None:
    client = _client(sqlite_store)
    rule_id = _save_rule(client, "有自选")["rule_id"]
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", source_rule_id=rule_id)
    seed.watchlist("600519", source_rule_id=rule_id)

    response = client.delete(f"/api/screening/rules/{rule_id}")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["refs"]["watchlist"] == 2
    assert "2 条自选记录" in detail["message"]
    assert "已保存的筛选结果" not in detail["message"]
    assert "运行结果" not in detail["message"]


def test_rule_with_both_refs_lists_both_counts(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    rule_id = _save_rule(client, "两种都有")["rule_id"]
    result_id = _seed_saved_result(sqlite_store, rule_id, "结果 A")
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", source_rule_id=rule_id, source_result_id=result_id)
    seed.watchlist("600519", source_rule_id=rule_id, source_result_id=result_id)
    seed.watchlist("000858", source_rule_id=rule_id, source_result_id=result_id)

    response = client.delete(f"/api/screening/rules/{rule_id}")

    assert response.status_code == 409, response.text
    message = response.json()["detail"]["message"]
    assert "1 条已保存的筛选结果" in message
    assert "3 条自选记录" in message
    assert "运行结果" not in message


def test_delete_missing_rule_returns_404(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    assert client.delete("/api/screening/rules/999").status_code == 404


# ── 原地更新（PUT）──────────────────────────────────────────────────


def test_put_rule_updates_in_place_and_bumps_version(sqlite_store: SQLiteStore) -> None:
    """同一 id / 同一 name，version +1，rule_json 被替换；库里仍是**一行**。"""
    client = _client(sqlite_store)
    saved = _save_rule(client, "ROCE 遗传好公司")
    rule_id = saved["rule_id"]
    before = client.get(f"/api/screening/rules/{rule_id}").json()
    assert before["version"] == 1

    updated_rule_json = {
        "conditions": {
            "logic": "AND",
            "rules": [{"field": "pe_ttm", "op": "<", "value": 25}],
        },
        "columns": ["stock_code"],
    }
    response = client.put(
        f"/api/screening/rules/{rule_id}",
        json={
            "rule_json": updated_rule_json,
            "locked_indicators": {},
            "expected_version": before["version"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "rule_id": rule_id,
        "version": before["version"] + 1,
    }
    after = client.get(f"/api/screening/rules/{rule_id}").json()
    assert after["id"] == rule_id
    assert after["name"] == before["name"]
    assert after["version"] == before["version"] + 1
    assert after["rule_json"] == updated_rule_json
    # 一套规则只有一行（旧实现这里会变成同名两行 v1/v2）
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM screening_rules")[0]["c"] == 1


def test_put_stale_expected_version_returns_409(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    rule_id = _save_rule(client, "并发规则")["rule_id"]
    body = {"rule_json": _RULE_JSON, "expected_version": 1}
    assert client.put(f"/api/screening/rules/{rule_id}", json=body).status_code == 200

    stale = client.put(f"/api/screening/rules/{rule_id}", json=body)

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["reason_code"] == "concurrent_rule_save_conflict"
    # 冲突时版本没被再次推进
    assert client.get(f"/api/screening/rules/{rule_id}").json()["version"] == 2


def test_put_missing_rule_returns_404(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    response = client.put(
        "/api/screening/rules/999", json={"rule_json": _RULE_JSON}
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "rule not found"


def test_put_validates_rule_fields_like_save(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    rule_id = _save_rule(client, "校验规则")["rule_id"]
    bad = {"conditions": {"logic": "AND", "rules": [{"field": "not_a_field", "op": ">", "value": 1}]}}
    response = client.put(f"/api/screening/rules/{rule_id}", json={"rule_json": bad})
    assert response.status_code == 400, response.text


# ── 新建（POST /rules/save）同名不再堆叠 ────────────────────────────


def test_save_duplicate_name_returns_400_and_keeps_single_row(
    sqlite_store: SQLiteStore,
) -> None:
    client = _client(sqlite_store)
    _save_rule(client, "ROCE 遗传好公司")

    response = client.post(
        "/api/screening/rules/save",
        json={"name": "ROCE 遗传好公司", "rule_json": _RULE_JSON},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "已存在同名规则，请换个名字或直接编辑那一条"
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM screening_rules")[0]["c"] == 1


def test_save_rejects_blank_name(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    response = client.post(
        "/api/screening/rules/save", json={"name": "   ", "rule_json": _RULE_JSON}
    )
    assert response.status_code == 400, response.text


# ── 悬空 rule_id 的读取方：不得 500 ─────────────────────────────────


def test_saving_result_from_run_of_deleted_rule_is_rejected_not_500(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """规则删除后，它留下的运行日志被拿去保存结果 → 400 可读提示，不是 500。

    `screening_results.rule_id` 有外键指向 `screening_rules(id)`；规则已删时
    直接 INSERT 会抛 IntegrityError（500）。这就是"悬空 rule_id 会不会崩"
    的唯一真实读取路径，必须提前拦住。
    """
    seed = Seed(duckdb_store)
    seed.stock(
        "000001", name="One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot("000001", "2025-12-31", pe_ttm=10)
    insert_minimum_screenable_data(duckdb_store)
    client = _client(sqlite_store, duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)

    rule_id = _save_rule(client, "跑一次就删")["rule_id"]
    _seed_run(sqlite_store, rule_id, run_id="run-dangling")
    assert client.delete(f"/api/screening/rules/{rule_id}").status_code == 200

    response = client.post(
        "/api/screening/save",
        json={"title": "历史上的运行结果", "run_id": "run-dangling"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "run rule was deleted"
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM screening_results")[0]["c"] == 0


def test_export_still_works_after_rule_edited_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """原地升版本后，历史结果（记的是旧 rule_version）仍然可以导出。

    `PUT /rules/{id}` 把规则行上的 version 推到 2，而保存结果里记的
    `rule_version = 1` 已经不存在了。导出若还按 `(id, version)` 内侧连接就会
    404/400「缺少规则溯源」—— 那是**新引入的回归**，这里锁住"仍能导出"。
    """
    seed = Seed(duckdb_store)
    seed.stock(
        "000001", name="One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot("000001", "2025-12-31", pe_ttm=10)
    insert_minimum_screenable_data(duckdb_store)
    client = _client(sqlite_store, duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)

    rule_id = _save_rule(client, "导出规则")["rule_id"]
    result_id = _seed_saved_result(sqlite_store, rule_id, "历史结果")
    # 原地升版本：规则行上的 version 变 2，结果里仍是 1
    updated = client.put(
        f"/api/screening/rules/{rule_id}",
        json={"rule_json": _RULE_JSON, "expected_version": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    response = client.post("/api/screening/export_csv", json={"result_id": result_id})

    assert response.status_code == 200, response.text
    assert "000001" in response.json()["csv"]


def test_put_legacy_duplicate_name_rows_do_not_collide_on_version(
    sqlite_store: SQLiteStore,
) -> None:
    """历史遗留的同名两行（正式库 49=v1 / 50=v2）—— 编辑 v1 那行不许撞唯一索引。

    修复前每次"改条件再运行"都会追加一行同名新版本，所以库里已经存在
    `UNIQUE(name, version)` 意义上的兄弟行。原地更新若死按 `current+1`，
    49 从 v1 升到 v2 就会撞上 50（v2）→ 409「并发冲突」永远重试不掉。
    """
    seed = SeedSqlite(sqlite_store)
    seed.screening_rule("ROCE遗传好公司", 1, json.dumps(_RULE_JSON), status="saved")
    seed.screening_rule("ROCE遗传好公司", 2, json.dumps(_RULE_JSON), status="saved")
    client = _client(sqlite_store)
    rows = {row["version"]: row["id"] for row in client.get("/api/screening/rules").json()["rules"]}
    older_id = rows[1]

    response = client.put(
        f"/api/screening/rules/{older_id}",
        json={"rule_json": _RULE_JSON, "expected_version": 1},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "rule_id": older_id, "version": 3}
    versions = {
        row["version"]
        for row in sqlite_store.query("SELECT version FROM screening_rules WHERE name = ?", [
            "ROCE遗传好公司"
        ])
    }
    assert versions == {2, 3}
