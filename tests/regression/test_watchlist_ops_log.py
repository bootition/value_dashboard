"""自选写操作审计日志（v22 `watchlist_ops`）：七类操作必须「查得出来」。

## 事故（2026-11-24，用户原话）

> 「那是他妈的，我从 ROCE 这个筛选分组里面移动过去的。这是我发现你 bug 的一次实验，
>  你难道查不出这个日志来吗？……居然连我的操作是怎么做的都查不出来，
>  那说明你在做功能的时候，根本就没有细致地检查嘛。」

用户在 UI 里把 7 只股票从「ROCE遗传好公司」**移动**到「我的自选」。事后只剩
「A 组少 7 行 + B 组多 7 行 + 时间戳接近」三条间接证据，靠猜；第一次还猜错了
（把 UTC 当本地时间，误判成"卡了 8 小时"）。根因：自选写操作**零留痕**。

## 本文件锁死的六条

1. 七类写操作（add / copy / move / remove / group_create / group_rename /
   group_delete）各自产生**恰好一条**记录，字段完整（op_label / 股票 / 分组 /
   detail / actor / 时间）。
2. `POST /move` 的 `copy` 与 `move` 是**两个不同的 op**，`detail` 能区分
   （`copied` vs `moved`）。
3. 失败的操作（400/404/409）**不留记录** —— 日志里出现没发生过的操作，比没有日志更糟。
4. `GET /ops` 的 `created_at_utc` 与 `created_at_local` 同时给出，且**固定时间**下
   差值恰好 8 小时（这条就是那次误判的回归测试）。
5. `limit` / `op` / `stock_code` 三个过滤参数都真的生效（含非法参数的 400）。
6. v22 迁移幂等：删掉记账行重跑不报错、不重复、**不丢已有日志行**。

额外钉一条硬要求：**审计写失败不得反向阻断业务写**（try/except + WARNING）。

隔离：全部用 `sqlite_store` / `duckdb_store` fixture（`--basetemp` 下的临时库），
**绝不碰** `data/valuedashboard.sqlite`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import watchlist_groups as wg
from app.core import watchlist_ops as wo
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import SQLITE_SCHEMA_VERSION, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.watchlist import router as watchlist_router
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

SOURCE = "ROCE遗传好公司"
TARGET = wg.DEFAULT_GROUP_NAME
CODES = ["000333", "002415", "600036", "601318", "000001", "000002", "000004"]


def _client(sqlite: SQLiteStore, duck: DuckDBStore | None = None) -> TestClient:
    app = FastAPI()
    app.state.sqlite = sqlite
    if duck is not None:
        app.state.duck = duck
    app.include_router(watchlist_router)
    return TestClient(app)


def _universe(duck: DuckDBStore, codes: list[str]) -> None:
    """`POST /add` 要用 `stock_meta` 校验代码在股票池内。"""
    for code in codes:
        Seed(duck).stock(code, name=f"测试{code}", exchange="SZSE")


def _rows(sqlite: SQLiteStore) -> list[dict[str, Any]]:
    return sqlite.query(
        "SELECT id, op, stock_codes, from_group, to_group, detail_json, actor, created_at "
        "FROM watchlist_ops ORDER BY id"
    )


def _ops(sqlite: SQLiteStore, **params: Any) -> list[dict[str, Any]]:
    client = _client(sqlite)
    resp = client.get("/api/watchlist/ops", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["ops"]


# ── ① 七类操作：一条操作 = 一条记录，字段完整 ────────────────────────


def test_seven_write_paths_each_leave_one_complete_record(
    sqlite_store: SQLiteStore, duckdb_store: DuckDBStore,
) -> None:
    """add / copy / move / remove / group_create / group_rename / group_delete 各一条。"""
    client = _client(sqlite_store, duckdb_store)
    _universe(duckdb_store, CODES)
    seed = SeedSqlite(sqlite_store)
    for code in CODES[:3]:
        seed.watchlist(code, group_name=SOURCE)

    steps = [
        ("add", lambda: client.post(
            "/api/watchlist/add",
            json={"stock_code": CODES[0], "group_name": TARGET},
        )),
        ("copy", lambda: client.post("/api/watchlist/move", json={
            "stock_codes": CODES[:2], "from_group": SOURCE, "to_group": TARGET,
            "mode": "copy",
        })),
        ("move", lambda: client.post("/api/watchlist/move", json={
            "stock_code": CODES[2], "from_group": SOURCE, "to_group": TARGET,
            "mode": "move",
        })),
        ("remove", lambda: client.request("DELETE", "/api/watchlist/remove", json={
            "stock_code": CODES[0], "group_name": TARGET,
        })),
        ("group_create", lambda: client.post(
            "/api/watchlist/groups", json={"name": "重点观察"},
        )),
        ("group_rename", lambda: client.patch(
            "/api/watchlist/groups/重点观察", json={"new_name": "重点观察2"},
        )),
        ("group_delete", lambda: client.delete("/api/watchlist/groups/重点观察2")),
    ]
    for expected_op, call in steps:
        resp = call()
        assert resp.status_code == 200, (expected_op, resp.text)

    rows = _rows(sqlite_store)
    assert [row["op"] for row in rows] == [op for op, _ in steps]

    for row in rows:
        assert row["actor"] in wo.ACTORS, row
        assert row["created_at"], row
        assert json.loads(row["detail_json"] or "{}"), row

    # 股票级操作带代码与分组，分组级操作为 NULL（schema 口径）
    by_op = {row["op"]: row for row in rows}
    assert json.loads(by_op["add"]["stock_codes"]) == [CODES[0]]
    assert by_op["add"]["to_group"] == TARGET
    assert json.loads(by_op["copy"]["stock_codes"]) == CODES[:2]
    assert by_op["copy"]["from_group"] == SOURCE
    assert by_op["copy"]["to_group"] == TARGET
    assert json.loads(by_op["move"]["stock_codes"]) == [CODES[2]]
    assert by_op["remove"]["from_group"] == TARGET
    assert by_op["group_create"]["stock_codes"] is None
    assert by_op["group_create"]["to_group"] == "重点观察"
    assert by_op["group_rename"]["from_group"] == "重点观察"
    assert by_op["group_rename"]["to_group"] == "重点观察2"
    assert by_op["group_delete"]["from_group"] == "重点观察2"

    # 读端点：中文 op_label + 一句话 summary + 双轨时间都在
    ops = _ops(sqlite_store, limit=50)
    assert [item["op"] for item in ops] == [op for op, _ in reversed(steps)]
    for item in ops:
        assert item["op_label"] == wo.OP_LABELS[item["op"]]
        assert item["summary"], item
        assert item["created_at_utc"].endswith("Z"), item
        assert item["created_at_local"], item
    detail = {item["op"]: item["detail"] for item in ops}
    assert detail["add"]["created"] is True
    assert detail["group_rename"]["moved_rows"] == 0
    assert detail["group_delete"]["deleted_rows"] == 0


def test_add_idempotent_second_hit_is_recorded_as_not_created(
    sqlite_store: SQLiteStore, duckdb_store: DuckDBStore,
) -> None:
    """连点两次「加入自选」：两条记录，`created` 分别 true / false（幂等也要留痕）。"""
    client = _client(sqlite_store, duckdb_store)
    _universe(duckdb_store, CODES[:1])
    payload = {"stock_code": CODES[0], "group_name": TARGET}
    assert client.post("/api/watchlist/add", json=payload).status_code == 200
    assert client.post("/api/watchlist/add", json=payload).status_code == 200
    rows = _rows(sqlite_store)
    assert [row["op"] for row in rows] == ["add", "add"]
    details = [json.loads(row["detail_json"]) for row in rows]
    assert details[0]["created"] is True
    assert details[1]["created"] is False
    assert details[1]["idempotent"] is True


def test_actor_header_overrides_script_default(
    sqlite_store: SQLiteStore, duckdb_store: DuckDBStore,
) -> None:
    """来源粗分：脚本（TestClient 的 UA）默认 `api`，显式 `X-VD-Actor` 以它为准。"""
    client = _client(sqlite_store, duckdb_store)
    _universe(duckdb_store, CODES[:1])
    assert client.post(
        "/api/watchlist/add", json={"stock_code": CODES[0]},
    ).status_code == 200
    assert client.post(
        "/api/watchlist/add",
        json={"stock_code": CODES[0]},
        headers={"X-VD-Actor": "cli"},
    ).status_code == 200
    assert [row["actor"] for row in _rows(sqlite_store)] == ["api", "cli"]


# ── ② copy / move 是两个 op，detail 能区分 ───────────────────────────


def test_copy_and_move_are_distinct_ops_with_distinct_detail(
    sqlite_store: SQLiteStore,
) -> None:
    """同一批 7 只：先 copy（原组不动），再 move（原组清空）。日志必须一眼分清。"""
    client = _client(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    for code in CODES:
        seed.watchlist(code, group_name=SOURCE)
    wg.create_group(sqlite_store, "我的观察池")

    assert client.post("/api/watchlist/move", json={
        "stock_codes": CODES, "from_group": SOURCE, "to_group": TARGET, "mode": "copy",
    }).status_code == 200
    assert client.post("/api/watchlist/move", json={
        "stock_codes": CODES, "from_group": SOURCE, "to_group": TARGET, "mode": "move",
    }).status_code == 200

    rows = _rows(sqlite_store)
    assert [row["op"] for row in rows] == ["copy", "move"]
    copy_detail = json.loads(rows[0]["detail_json"])
    move_detail = json.loads(rows[1]["detail_json"])
    assert copy_detail["mode"] == "copy"
    assert copy_detail["copied"] == len(CODES)
    assert copy_detail["moved"] == 0
    assert move_detail["mode"] == "move"
    assert move_detail["moved"] == len(CODES)
    assert "copied" not in move_detail or move_detail.get("copied") in (0, None)
    # 两个 op 的中文标签也不同（前端直接显示，不自己拼）
    assert wo.OP_LABELS["copy"] == "复制到分组"
    assert wo.OP_LABELS["move"] == "移动分组"

    ops = _ops(sqlite_store)
    assert [item["op_label"] for item in ops] == ["移动分组", "复制到分组"]
    # 那条 move 的 summary 一眼能看懂「7 只 从 A 到 B」
    move_summary = ops[0]["summary"]
    assert CODES[0] in move_summary
    assert SOURCE in move_summary
    assert TARGET in move_summary
    assert "7 只" in move_summary


# ── ③ 失败的操作不留记录 ─────────────────────────────────────────────


def test_failed_operations_leave_no_record(
    sqlite_store: SQLiteStore, duckdb_store: DuckDBStore,
) -> None:
    """400/404/409 一律不留痕 —— 「数据成功才记录」的反面。"""
    client = _client(sqlite_store, duckdb_store)
    _universe(duckdb_store, CODES[:2])
    seed = SeedSqlite(sqlite_store)
    seed.watchlist(CODES[0], group_name=SOURCE)
    wg.create_group(sqlite_store, "普通分组")
    before = len(_rows(sqlite_store))

    failures = [
        # add: 代码格式错 / 不在股票池 / 黑名单
        (client.post("/api/watchlist/add", json={"stock_code": "12345"}), 400),
        (client.post("/api/watchlist/add", json={"stock_code": "999999"}), 400),
        # move: 空请求 / 目标组不存在 / 来源组不存在 / 一只都没匹配到
        (client.post("/api/watchlist/move", json={"to_group": TARGET}), 400),
        (client.post("/api/watchlist/move", json={
            "stock_code": CODES[0], "to_group": "不存在的组",
        }), 404),
        (client.post("/api/watchlist/move", json={
            "stock_codes": CODES, "from_group": "不存在的组", "to_group": TARGET,
        }), 404),
        # move 模式：这只股票不在来源组里 → 事务里一行没动 → 提交后 404（不许留痕）
        (client.post("/api/watchlist/move", json={
            "stock_code": CODES[1], "from_group": SOURCE, "to_group": TARGET,
            "mode": "move",
        }), 404),
        # copy 模式：来源组里没有这只票 → 404（在事务内部抛，连日志一起回滚）
        (client.post("/api/watchlist/move", json={
            "stock_code": CODES[1], "from_group": SOURCE, "to_group": TARGET,
            "mode": "copy",
        }), 404),
        # remove: 本来就不在自选
        (client.request(
            "DELETE", "/api/watchlist/remove", json={"stock_code": CODES[1]},
        ), 404),
        # 建组: 重名（含默认组名）/ 空名
        (client.post("/api/watchlist/groups", json={"name": "普通分组"}), 409),
        (client.post("/api/watchlist/groups", json={"name": TARGET}), 409),
        (client.post("/api/watchlist/groups", json={"name": "   "}), 400),
        # 改名: 默认组不许改 / 源组不存在 / 目标名已存在
        (client.patch(f"/api/watchlist/groups/{TARGET}", json={"new_name": "x"}), 400),
        (client.patch("/api/watchlist/groups/不存在的组", json={"new_name": "x"}), 404),
        (client.patch("/api/watchlist/groups/普通分组", json={"new_name": TARGET}), 409),
        # 删组: 默认组不许删 / 组不存在
        (client.delete(f"/api/watchlist/groups/{TARGET}"), 400),
        (client.delete("/api/watchlist/groups/不存在的组"), 404),
        # 刷新: 该分组没有可刷新的规则来源
        (client.post("/api/watchlist/refresh", json={"group_name": "普通分组"}), 400),
    ]
    for response, expected_status in failures:
        assert response.status_code == expected_status, (response.text, expected_status)

    # 改成同名（`unchanged=true`）不写任何数据 → 200 但同样不留痕
    same = client.patch("/api/watchlist/groups/普通分组", json={"new_name": "普通分组"})
    assert same.status_code == 200, same.text
    assert same.json()["unchanged"] is True

    assert _rows(sqlite_store) == []
    assert len(_rows(sqlite_store)) == before == 0
    # 失败之后业务数据也没被改动（400/404 不许半写）
    assert [row["stock_code"] for row in sqlite_store.query(
        "SELECT stock_code FROM watchlist WHERE group_name = ?", [SOURCE],
    )] == [CODES[0]]


def test_audit_write_failure_does_not_block_business_write(
    sqlite_store: SQLiteStore, duckdb_store: DuckDBStore, caplog: pytest.LogCaptureFixture,
) -> None:
    """硬要求：审计是旁路，表没了也只能 WARNING，业务写必须照常成功。"""
    client = _client(sqlite_store, duckdb_store)
    _universe(duckdb_store, CODES[:1])
    sqlite_store.execute("DROP TABLE watchlist_ops")
    wo._ENSURED.clear()

    resp = client.post("/api/watchlist/add", json={"stock_code": CODES[0]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM watchlist WHERE stock_code = ?", [CODES[0]],
    )[0]["c"] == 1
    assert any("自选操作审计写入失败" in record.message for record in caplog.records)

    # 读端点会自愈建表（不会因为一次写失败就永久失去日志能力）
    assert _ops(sqlite_store) == []


# ── ④ UTC / 本地时间双轨：固定时间断言差值 8 小时 ─────────────────────


def test_ops_expose_utc_and_local_time_eight_hours_apart(
    sqlite_store: SQLiteStore,
) -> None:
    """`2026-11-24 05:37:00` UTC（= 13:37 北京）就是那次误判的时刻。"""
    sqlite_store.execute(
        "INSERT INTO watchlist_ops "
        "(op, stock_codes, from_group, to_group, detail_json, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            "move", json.dumps(CODES, ensure_ascii=False), SOURCE, TARGET,
            json.dumps({"mode": "move", "moved": 7, "rows": 7}), "local-ui",
            "2026-11-24 05:37:00",
        ],
    )
    body = _client(sqlite_store).get("/api/watchlist/ops").json()
    assert body["count"] == 1
    item = body["ops"][0]
    assert item["created_at_utc"] == "2026-11-24T05:37:00Z"
    assert item["created_at_local"] == "2026-11-24 13:37:00"
    assert item["created_at_raw"] is None

    utc = datetime.strptime(item["created_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    local = datetime.strptime(item["created_at_local"], "%Y-%m-%d %H:%M:%S")
    assert local - utc == timedelta(hours=8)
    # 同一条记录说的是「7 只 从 ROCE 到 我的自选」（用户原话要的效果）
    assert set(item["stock_codes"]) == set(CODES)
    assert item["from_group"] == SOURCE
    assert item["to_group"] == TARGET
    assert "7 只" in item["summary"]


def test_unparsable_created_at_is_reported_honestly(sqlite_store: SQLiteStore) -> None:
    """时间解析不了 → 两个时间字段给 None + 原文，**不猜"现在"**。"""
    sqlite_store.execute(
        "INSERT INTO watchlist_ops (op, stock_codes, actor, created_at) "
        "VALUES (?, ?, ?, ?)",
        ["add", json.dumps([CODES[0]]), "local-ui", "not-a-timestamp"],
    )
    item = _client(sqlite_store).get("/api/watchlist/ops").json()["ops"][0]
    assert item["created_at_utc"] is None
    assert item["created_at_local"] is None
    assert item["created_at_raw"] == "not-a-timestamp"


# ── ⑤ limit / op / stock_code 过滤 ───────────────────────────────────


def test_ops_filters_by_limit_op_and_stock_code(sqlite_store: SQLiteStore) -> None:
    seed = SeedSqlite(sqlite_store)
    for code in CODES[:4]:
        seed.watchlist(code, group_name=SOURCE)
    sqlite_store.execute(
        "INSERT INTO watchlist (stock_code, group_name) VALUES (?, ?)",
        [CODES[0], TARGET],
    )
    for op, codes in [
        ("add", [CODES[0]]),
        ("copy", [CODES[1], CODES[2]]),
        ("move", [CODES[2]]),
        ("remove", [CODES[3]]),
    ]:
        wo.record(
            sqlite_store, op=op, stock_codes=codes,
            from_group=SOURCE, to_group=TARGET, detail={"rows": 1},
        )

    client = _client(sqlite_store)
    assert client.get("/api/watchlist/ops", params={"limit": 2}).json()["count"] == 2
    only_copy = client.get("/api/watchlist/ops", params={"op": "copy"}).json()
    assert [item["op"] for item in only_copy["ops"]] == ["copy"]
    by_code = client.get(
        "/api/watchlist/ops", params={"stock_code": CODES[2]},
    ).json()
    assert [item["op"] for item in by_code["ops"]] == ["move", "copy"]
    assert client.get(
        "/api/watchlist/ops", params={"op": "copy", "stock_code": CODES[0]},
    ).json()["count"] == 0
    # 默认 limit=50、非法参数 400（拼错的操作名不该静默变空列表）
    assert client.get("/api/watchlist/ops").json()["count"] == 4
    assert client.get("/api/watchlist/ops", params={"op": "nope"}).status_code == 400
    assert client.get(
        "/api/watchlist/ops", params={"stock_code": "abc"},
    ).status_code == 400
    assert client.get("/api/watchlist/ops", params={"limit": 0}).status_code == 422


# ── ⑥ v22 迁移 ───────────────────────────────────────────────────────


def test_v22_migration_creates_table_and_index(sqlite_store: SQLiteStore) -> None:
    assert SQLITE_SCHEMA_VERSION == 22
    versions = {
        int(row["version"]) for row in sqlite_store.query("SELECT version FROM schema_migrations")
    }
    assert 22 in versions
    columns = {
        str(row["name"]) for row in sqlite_store.query("PRAGMA table_info(watchlist_ops)")
    }
    assert {
        "id", "op", "stock_codes", "from_group", "to_group",
        "detail_json", "actor", "created_at",
    } <= columns
    indexes = {
        str(row["name"])
        for row in sqlite_store.query("PRAGMA index_list(watchlist_ops)")
    }
    assert f"idx_{wo.OPS_TABLE}_created_at" in indexes


def test_v22_migration_is_idempotent(sqlite_store: SQLiteStore) -> None:
    """删掉 v22 记账行重跑两次：不报错、不重复记账、**已有的日志行不丢**。"""
    wo.record(sqlite_store, op="add", stock_codes=[CODES[0]], to_group=TARGET)
    assert len(_rows(sqlite_store)) == 1

    sqlite_store.execute("DELETE FROM schema_migrations WHERE version = 22")
    init_sqlite_schema(sqlite_store)
    init_sqlite_schema(sqlite_store)

    recorded = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = 22"
    )[0]["c"]
    assert recorded == 1
    assert len(_rows(sqlite_store)) == 1


# ── 额外记录点：POST /refresh（整组替换） ────────────────────────────


def test_refresh_replaces_rows_and_is_recorded(
    sqlite_store: SQLiteStore,
    duckdb_store: DuckDBStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/refresh` 会整组删旧行插新行 —— 用户没点名，但漏了它这张表就答不上
    「我的 591 行怎么变成 200 行了」。"""
    seed = SeedSqlite(sqlite_store)
    SeedSqlite(sqlite_store).screening_rule("刷新规则", 1, "{}", status="saved")
    rule_id = int(sqlite_store.query("SELECT id FROM screening_rules")[0]["id"])
    sqlite_store.execute(
        "INSERT INTO screening_results "
        "(title, rule_id, rule_version, data_date, result_json, columns_json, sort_json, "
        " confidence_summary, base_pool_config) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["旧结果", rule_id, 1, "2026-11-01", "[]", "[]", "[]", "{}", "{}"],
    )
    result_id = int(sqlite_store.query("SELECT id FROM screening_results")[0]["id"])
    for code in CODES[:2]:
        seed.watchlist(
            code, group_name=SOURCE, source_rule_id=rule_id, source_result_id=result_id,
        )

    from app.core.screening.engine import ScreeningEngine

    def fake_run(self: Any, **_: Any) -> dict[str, Any]:
        return {
            "results": [{"stock_code": CODES[4]}, {"stock_code": CODES[5]}],
            "data_date": "2026-11-24",
            "total": 2,
            "truncated": False,
        }

    monkeypatch.setattr(ScreeningEngine, "run", fake_run)
    resp = _client(sqlite_store, duckdb_store).post(
        "/api/watchlist/refresh", json={"group_name": SOURCE},
    )
    assert resp.status_code == 200, resp.text

    rows = _rows(sqlite_store)
    assert [row["op"] for row in rows] == ["refresh"]
    detail = json.loads(rows[0]["detail_json"])
    assert detail["added"] == 2
    assert detail["removed"] == 2
    assert detail["rule_ids"] == [rule_id]
    # 分组级操作：`stock_codes` 为 NULL（schema 口径），且 summary 能读
    assert rows[0]["stock_codes"] is None
    assert rows[0]["to_group"] == SOURCE
    assert "新增 2 只" in _ops(sqlite_store)[0]["summary"]


# ── 额外记录点：POST /api/screening/add_to_watchlist ──────────────────


def test_screening_save_is_recorded(
    sqlite_store: SQLiteStore, duckdb_store: DuckDBStore,
) -> None:
    """`add_to_watchlist` 是自选行数最大的来源（现场 591 行就是它累加的）。

    这条路径不在 `watchlist.py` 里，用户没点名 —— 但漏了它，「这 591 行哪来的」
    仍然只能靠猜，所以一并留痕（op=`screening_save`）。
    """
    from app.web.api.screening import router as screening_router
    from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data

    Seed(duckdb_store).stock(
        "000001", name="One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    Seed(duckdb_store).snapshot("000001", "2025-12-31", pe_ttm=10)
    # 顺序不能反：日历是照 `price_daily_qfq` 的真实日期灌的，
    # 先灌日历等于灌了个空表 → 筛可筛性检查报 TRADING_CALENDAR_UNAVAILABLE。
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    SeedSqlite(sqlite_store).screening_rule("大老公司", 1, "{}", status="saved")
    rule_id = int(sqlite_store.query("SELECT id FROM screening_rules")[0]["id"])
    sqlite_store.execute(
        """INSERT INTO screening_results
           (title, rule_id, rule_version, data_date, result_json, columns_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ["结果", rule_id, 1, "2025-12-31", json.dumps([{"stock_code": "000001"}]), '["stock_code"]'],
    )
    result_id = int(sqlite_store.query("SELECT id FROM screening_results")[0]["id"])

    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(screening_router)
    resp = TestClient(app).post("/api/screening/add_to_watchlist", json={
        "stock_codes": ["000001"], "group": "往股东口袋装钱的大老公司",
        "result_id": result_id, "replace_group": False,
    })
    assert resp.status_code == 200, resp.text

    rows = _rows(sqlite_store)
    assert [row["op"] for row in rows] == ["screening_save"]
    assert json.loads(rows[0]["stock_codes"]) == ["000001"]
    assert rows[0]["to_group"] == "往股东口袋装钱的大老公司"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["result_id"] == result_id
    assert detail["inserted"] == 1
    assert detail["replace_group"] is False
    assert "累加" in _ops(sqlite_store)[0]["summary"]


def test_delete_group_is_atomic_no_ghost_group(sqlite_store) -> None:
    """删分组必须**原子**：不许留下"0 只但还在侧栏"的幽灵分组（2026-11-27 自查）。

    改前：`DELETE FROM watchlist`（事务1）→ `delete_registry_group`（事务2），
    中间失败会删掉行却留下注册表行。现在两步共用同一个事务。
    """
    from app.web.api import watchlist as watchlist_api

    client = _client(sqlite_store)  # type: ignore[name-defined]
    group = "原子性测试组"
    client.post("/api/watchlist/groups", json={"name": group})
    sqlite_store.execute(
        "INSERT OR IGNORE INTO watchlist (stock_code, group_name) VALUES ('600519', ?)",
        [group],
    )

    # 让删注册表那一步抛错（模拟第二个事务失败）
    original = watchlist_api.wg.delete_registry_group

    def boom(sqlite, name, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("模拟第二段失败")

    watchlist_api.wg.delete_registry_group = boom
    try:
        # 最小测试 app 没挂异常处理器，TestClient 会把异常原样抛出 —— 这正是我们要的：
        # 无论是 500 还是直接抛，**事务都必须回滚**
        client.delete(f"/api/watchlist/groups/{group}")
    except RuntimeError:
        pass
    finally:
        watchlist_api.wg.delete_registry_group = original
    # 关键：整个事务回滚 —— 行还在、注册表行也还在，不出现幽灵分组
    rows = sqlite_store.query(
        "SELECT COUNT(*) AS n FROM watchlist WHERE group_name = ?", [group]
    )[0]["n"]
    registered = sqlite_store.query(
        "SELECT COUNT(*) AS n FROM watchlist_groups WHERE name = ?", [group]
    )[0]["n"]
    assert int(rows) == 1, "事务回滚后 watchlist 行必须还在"
    assert int(registered) == 1, "注册表行也必须还在（否则就是幽灵分组）"


def test_add_failure_does_not_leave_ghost_group(duckdb_store, sqlite_store) -> None:
    """`POST /add` 失败时不许留下空分组（2026-11-27 自查）。

    改前：`ensure_group_for_write` 在事务**外**先提交一次"登记分组"，
    add 再失败（如溯源校验 400）就会留下一个**空分组**，
    而日志里只有一条失败的 add —— 看不出这个空组是那次操作造出来的。
    """
    # `/add` 要用 DuckDB 校验股票在池内 → 必须给 client 一个带股票的 duck
    from tests.support.seed import Seed

    Seed(duckdb_store).stock(
        "600519", name="贵州茅台", exchange="SSE",
        listing_date="2001-08-27", is_st=False, is_suspended=False,
    )
    client = _client(sqlite_store, duckdb_store)  # type: ignore[name-defined]
    ghost = "不该出现的空组"

    # 用"结果里没有这只股票"触发 400：add 会在事务内校验 provenance
    from tests.support.seed_sqlite import SeedSqlite

    SeedSqlite(sqlite_store).screening_rule("规则A", 1, "{}", status="saved")
    rule_id = int(sqlite_store.query(
        "SELECT id FROM screening_rules WHERE name = '规则A'"
    )[0]["id"])
    # 空结果集 → 里面没有 600519 → add 必然 400
    sqlite_store.execute(
        "INSERT INTO screening_results "
        "(title, rule_id, rule_version, data_date, result_json, columns_json) "
        "VALUES ('结果A', ?, 1, '2026-11-24', '[]', '[]')",
        [rule_id],
    )
    result_id = int(sqlite_store.query(
        "SELECT id FROM screening_results ORDER BY id DESC LIMIT 1"
    )[0]["id"])

    resp = client.post("/api/watchlist/add", json={
        "stock_code": "600519",
        "group_name": ghost,
        "source_result_id": str(result_id),   # 契约是字符串；结果里没有 600519 → 400
    })
    assert resp.status_code == 400, resp.text

    registered = sqlite_store.query(
        "SELECT COUNT(*) AS n FROM watchlist_groups WHERE name = ?", [ghost]
    )[0]["n"]
    assert int(registered) == 0, (
        "add 失败后不许留下空分组 —— 分组登记必须与自选行在同一个事务里"
    )
