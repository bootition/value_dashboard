"""自选分组注册表：用户可自建分组 + 两处「加入自选」（2026-11-26 用户需求）。

用户原话：

> 「现在这个自选分组是不是做得还不太好？除了按照筛选规则分组，我难道不能自己添加进分组吗？
> 有没有办法做到：在详情界面有个『加入自选』，或者我直接在按规则分组的列表里面有个
> 『点击加入自选列表』。……那种『点击加入自选』应该是加入默认自选列表，然后还可以自己
> 移动到自己的分组里。」

根因：分组此前由 `SELECT ... GROUP BY group_name` **派生**，于是**空分组不可能存在**
——用户先建一个分组再往里挪股票这条路在数据模型上根本走不通。

本文件锁死的契约：

1. `watchlist_groups` 表由 v21 迁移建出、**默认组 `我的自选`（kind='default'）真的存在**、
   现有 `watchlist.group_name` 全部回填（有规则来源 → `kind='rule'`，否则 `manual`）；
   迁移**幂等**（删掉记账行重跑不报错、不重复插）。
2. `POST /groups`：建组 / 重名 409 / 空名 400 / 超长 400 / 与默认组重名 409。
3. `GET /groups` 列出注册表全部分组，**0 只的空分组照常列出（cnt=0）**。
4. `PATCH /groups/{name}` 级联改 `watchlist.group_name`。
5. 默认组不许删除（400）、不许改名（400）。
6. `POST /add` 不传 group → 落在默认组；重复加 **幂等** `created=false`。
7. `GET /status` 三种情况（不在自选 / 在一个组 / 在多个组）。
8. `POST /move` 批量口径。
9. 自愈：`watchlist` 里有、注册表里没有的分组名 → `/groups` 自动补登记。

隔离：全部用 `sqlite_store` / `duckdb_store` fixture（`--basetemp` 下的临时库），
**绝不碰** `data/valuedashboard.sqlite`。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import watchlist_groups as wg
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import SQLITE_SCHEMA_VERSION, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.watchlist import router as watchlist_router
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

DEFAULT = "我的自选"


def _client(sqlite: SQLiteStore, duck: DuckDBStore | None = None) -> TestClient:
    app = FastAPI()
    app.state.sqlite = sqlite
    if duck is not None:
        app.state.duck = duck
    app.include_router(watchlist_router)
    return TestClient(app)


def _seed_stock(duck: DuckDBStore, code: str, name: str = "测试公司") -> None:
    Seed(duck).stock(
        code, name=name, exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )


def _registry(sqlite: SQLiteStore) -> dict[str, dict]:
    wg.ensure_table(sqlite)
    return {
        str(row["name"]): row
        for row in sqlite.query("SELECT name, kind, rule_id FROM watchlist_groups")
    }


def _seed_rule(sqlite: SQLiteStore, name: str = "规则") -> int:
    SeedSqlite(sqlite).screening_rule(name, 1, "{}", status="saved")
    return int(sqlite.query("SELECT id FROM screening_rules WHERE name = ?", [name])[0]["id"])


# ── ① v21 迁移：默认组 + 回填 + 幂等 ─────────────────────────────────


def test_v21_creates_default_group_and_backfills_existing_groups(
    sqlite_store: SQLiteStore,
) -> None:
    """迁移口径：默认组一定在；现有分组按「有规则来源 = rule」回填。"""
    registry = _registry(sqlite_store)
    assert registry[DEFAULT]["kind"] == "default"
    versions = {
        int(row["version"])
        for row in sqlite_store.query("SELECT version FROM schema_migrations")
    }
    assert SQLITE_SCHEMA_VERSION == 21
    assert 21 in versions

    rule_id = _seed_rule(sqlite_store, "回填规则")
    seed = SeedSqlite(sqlite_store)
    seed.watchlist(
        "000001", group_name="规则组", source_rule_id=rule_id, source_result_id=None,
    )
    seed.watchlist("000002", group_name="手动组")
    _ = wg.heal_registry(sqlite_store)

    registry = _registry(sqlite_store)
    assert registry["规则组"]["kind"] == "rule"
    assert registry["规则组"]["rule_id"] == rule_id
    assert registry["手动组"]["kind"] == "manual"
    assert registry["手动组"]["rule_id"] is None


def test_v21_migration_is_idempotent(sqlite_store: SQLiteStore) -> None:
    """把 v21 记账行删掉重跑：不报错、不重复插、默认组不重复。"""
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", group_name="重跑组")
    sqlite_store.execute("DELETE FROM schema_migrations WHERE version = 21")
    sqlite_store.execute("DELETE FROM watchlist_groups WHERE name = ?", ["重跑组"])

    init_sqlite_schema(sqlite_store)
    init_sqlite_schema(sqlite_store)  # 再来一次也不能抛（幂等）

    rows = sqlite_store.query(
        "SELECT name, kind, COUNT(*) AS c FROM watchlist_groups GROUP BY name, kind"
    )
    by_name = {str(row["name"]): row for row in rows}
    assert by_name[DEFAULT]["c"] == 1
    assert by_name["重跑组"]["kind"] == "manual"
    migrations = sqlite_store.query(
        "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = 21"
    )
    assert migrations[0]["c"] == 1


# ── ② 建组：重名 / 空名 / 超长 ──────────────────────────────────────


def test_create_group_validation(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)

    created = client.post("/api/watchlist/groups", json={"name": "  重点观察  "})
    assert created.status_code == 200, created.text
    assert created.json()["group"]["group_name"] == "重点观察"
    assert created.json()["group"]["kind"] == "manual"
    # 空分组必须合法存在
    assert created.json()["group"]["cnt"] == 0

    duplicate = client.post("/api/watchlist/groups", json={"name": "重点观察"})
    assert duplicate.status_code == 409
    assert "已经有一个叫" in duplicate.json()["detail"]["message"]

    blank = client.post("/api/watchlist/groups", json={"name": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"]["reason_code"] == "watchlist_group_name_invalid"

    too_long = client.post("/api/watchlist/groups", json={"name": "长" * (wg.MAX_GROUP_NAME_LENGTH + 1)})
    assert too_long.status_code == 400
    assert str(wg.MAX_GROUP_NAME_LENGTH) in too_long.json()["detail"]["message"]

    default_clash = client.post("/api/watchlist/groups", json={"name": DEFAULT})
    assert default_clash.status_code == 409


# ── ③ 空分组能被 /groups 列出（cnt = 0）─────────────────────────────


def test_groups_endpoint_lists_empty_group(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    client = _client(sqlite_store, duckdb_store)
    assert client.post("/api/watchlist/groups", json={"name": "空分组"}).status_code == 200

    body = client.get("/api/watchlist/groups").json()
    names = [group["group_name"] for group in body["groups"]]
    assert "空分组" in names
    assert DEFAULT in names
    empty = next(group for group in body["groups"] if group["group_name"] == "空分组")
    assert empty["cnt"] == 0
    assert empty["kind"] == "manual"
    assert empty["is_default"] is False
    default = next(group for group in body["groups"] if group["group_name"] == DEFAULT)
    assert default["is_default"] is True
    assert default["kind"] == "default"

    # `GET /list` 的 groups 也必须带出空分组（侧栏靠它渲染）
    listing = client.get("/api/watchlist/list").json()
    assert "空分组" in [group["group_name"] for group in listing["groups"]]


# ── ④ 重命名：级联改 watchlist.group_name ───────────────────────────


def test_rename_group_cascades_watchlist_rows(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    seed.watchlist("000001", group_name="旧名字")
    seed.watchlist("000002", group_name="旧名字")
    _ = wg.heal_registry(sqlite_store)

    response = client.patch("/api/watchlist/groups/旧名字", json={"new_name": "新名字"})
    assert response.status_code == 200, response.text
    assert response.json()["moved_rows"] == 2
    groups = [
        str(row["group_name"])
        for row in sqlite_store.query("SELECT DISTINCT group_name FROM watchlist")
    ]
    assert groups == ["新名字"]
    registry = _registry(sqlite_store)
    assert "旧名字" not in registry
    assert registry["新名字"]["kind"] == "manual"

    # 改成一个已存在的名字 → 409；改成默认组名 → 409
    client.post("/api/watchlist/groups", json={"name": "已存在"})
    conflict = client.patch("/api/watchlist/groups/新名字", json={"new_name": "已存在"})
    assert conflict.status_code == 409
    default_conflict = client.patch("/api/watchlist/groups/新名字", json={"new_name": DEFAULT})
    assert default_conflict.status_code == 409
    # 改成空白 → 400；源分组不存在 → 404
    assert client.patch("/api/watchlist/groups/新名字", json={"new_name": "  "}).status_code == 400
    assert client.patch("/api/watchlist/groups/没这个组", json={"new_name": "x"}).status_code == 404


# ── ⑤ 默认组不许删 / 不许改名 ───────────────────────────────────────


def test_default_group_cannot_be_deleted_or_renamed(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)

    deleted = client.delete(f"/api/watchlist/groups/{DEFAULT}")
    assert deleted.status_code == 400
    assert deleted.json()["detail"]["reason_code"] == "watchlist_default_group_protected"

    renamed = client.patch(f"/api/watchlist/groups/{DEFAULT}", json={"new_name": "改名试试"})
    assert renamed.status_code == 400
    assert renamed.json()["detail"]["reason_code"] == "watchlist_default_group_protected"

    # 默认组仍然在（含 0 只的状态）
    registry = _registry(sqlite_store)
    assert registry[DEFAULT]["kind"] == "default"

    # 非默认组可以删，且注册表行一起消失（不留 0 只的幽灵分组）
    client.post("/api/watchlist/groups", json={"name": "可删组"})
    ok = client.delete("/api/watchlist/groups/可删组")
    assert ok.status_code == 200
    assert ok.json()["deleted_rows"] == 0
    assert ok.json()["registry_removed"] is True
    assert "可删组" not in _registry(sqlite_store)


# ── ⑥ add 不传 group → 默认组；重复加幂等 ──────────────────────────


def test_add_without_group_lands_in_default_group_and_is_idempotent(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600519", name="贵州茅台")
    client = _client(sqlite_store, duckdb_store)

    first = client.post("/api/watchlist/add", json={"stock_code": "600519"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True
    assert body["group"] == DEFAULT
    rows = sqlite_store.query(
        "SELECT group_name, source_rule_id FROM watchlist WHERE stock_code = ?",
        ["600519"],
    )
    assert rows == [{"group_name": DEFAULT, "source_rule_id": None}]

    # 连点第二次：不报错、不新增行、不刷新 added_at
    added_at_before = sqlite_store.query(
        "SELECT added_at FROM watchlist WHERE stock_code = ? AND group_name = ?",
        ["600519", DEFAULT],
    )[0]["added_at"]
    second = client.post("/api/watchlist/add", json={"stock_code": "600519"})
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    count = sqlite_store.query("SELECT COUNT(*) AS c FROM watchlist")[0]["c"]
    assert count == 1
    added_at_after = sqlite_store.query(
        "SELECT added_at FROM watchlist WHERE stock_code = ? AND group_name = ?",
        ["600519", DEFAULT],
    )[0]["added_at"]
    assert added_at_before == added_at_after


# ── ⑦ status 端点三种情况 ───────────────────────────────────────────


def test_status_endpoint_covers_not_in_one_and_many_groups(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)

    nobody = client.get("/api/watchlist/status", params={"stock_code": "600519"}).json()
    assert nobody["in_watchlist"] is False
    assert nobody["groups"] == []
    assert nobody["default_group"] == DEFAULT

    seed = SeedSqlite(sqlite_store)
    seed.watchlist("600519", group_name=DEFAULT)
    one = client.get("/api/watchlist/status", params={"stock_code": "600519"}).json()
    assert one["in_watchlist"] is True
    assert one["groups"] == [DEFAULT]

    seed.watchlist("600519", group_name="重点观察")
    many = client.get("/api/watchlist/status", params={"stock_code": "600519"}).json()
    assert many["in_watchlist"] is True
    assert set(many["groups"]) == {DEFAULT, "重点观察"}

    # 代码格式不合法 → 400（不猜）
    assert client.get("/api/watchlist/status", params={"stock_code": "abc"}).status_code == 400


# ── ⑧ 批量 move ────────────────────────────────────────────────────


def test_batch_move_moves_selected_stocks(sqlite_store: SQLiteStore) -> None:
    client = _client(sqlite_store)
    seed = SeedSqlite(sqlite_store)
    for code in ("000001", "000002", "000003"):
        seed.watchlist(code, group_name=DEFAULT)
    seed.watchlist("600519", group_name=DEFAULT)
    client.post("/api/watchlist/groups", json={"name": "重点观察"})

    # 2026-11-27：`/move` 的默认语义已改为**复制**（用户裁决：从规则分组往自己分组
    # 里放不该把原分组删掉）。本用例要测的是"移动"这条路，所以显式传 `mode="move"`。
    response = client.post(
        "/api/watchlist/move",
        json={
            "stock_codes": ["000001", "000002"],
            "from_group": DEFAULT,
            "to_group": "重点观察",
            "mode": "move",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["to"] == "重点观察"
    assert body["rows"] == 2
    assert body["not_found"] == []
    moved = [
        str(row["stock_code"])
        for row in sqlite_store.query(
            "SELECT stock_code FROM watchlist WHERE group_name = ? ORDER BY stock_code",
            ["重点观察"],
        )
    ]
    assert moved == ["000001", "000002"]
    remaining = [
        str(row["stock_code"])
        for row in sqlite_store.query(
            "SELECT stock_code FROM watchlist WHERE group_name = ? ORDER BY stock_code",
            [DEFAULT],
        )
    ]
    assert remaining == ["000003", "600519"]

    # 单只口径仍可用（旧契约）
    single = client.post(
        "/api/watchlist/move",
        json={"stock_code": "000003", "from_group": DEFAULT, "to_group": "重点观察"},
    )
    assert single.status_code == 200
    assert single.json()["stock_code"] == "000003"

    # 目标分组不存在 → 404（不替用户建组）
    missing = client.post(
        "/api/watchlist/move",
        json={"stock_codes": ["600519"], "from_group": DEFAULT, "to_group": "没这个组"},
    )
    assert missing.status_code == 404
    # 一只都没匹配到 → 404
    nothing = client.post(
        "/api/watchlist/move",
        json={"stock_codes": ["999998"], "from_group": DEFAULT, "to_group": "重点观察"},
    )
    assert nothing.status_code == 404


# ── ⑨ 自愈：watchlist 有、注册表没有 ────────────────────────────────


def test_groups_endpoint_heals_unregistered_group(sqlite_store: SQLiteStore) -> None:
    """历史分组（迁移前派生出来的名字）不许在侧栏凭空消失。"""
    client = _client(sqlite_store)
    rule_id = _seed_rule(sqlite_store, "自愈规则")
    seed = SeedSqlite(sqlite_store)
    seed.watchlist(
        "000001", group_name="历史规则组", source_rule_id=rule_id, source_result_id=None,
    )
    seed.watchlist("000002", group_name="历史手动组")
    # 模拟"迁移没跑到"：注册表里先删干净（连默认组一起，验证 ensure 会补回来）
    sqlite_store.execute("DELETE FROM watchlist_groups")

    body = client.get("/api/watchlist/groups").json()
    by_name = {group["group_name"]: group for group in body["groups"]}
    assert by_name["历史规则组"]["kind"] == "rule"
    assert by_name["历史规则组"]["cnt"] == 1
    assert by_name["历史手动组"]["kind"] == "manual"
    # 默认组也一定被补回来（硬要求：`我的自选` 必须真的存在）
    assert by_name[DEFAULT]["kind"] == "default"
    assert {"历史规则组", "历史手动组"} <= set(body["healed"])
    assert _registry(sqlite_store)["历史规则组"]["rule_id"] == rule_id
