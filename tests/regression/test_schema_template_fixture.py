"""守卫：DuckDB schema 模板复制替代「每测试跑一遍 DDL」之后，**覆盖面没有缩小**。

背景（``.planning/2026-11-19-test-audit/T4_cost.md`` §3.1 / §4 / §5①）：
``init_duckdb_schema()`` 实测中位 0.404 s，被 956 条测试经 ``duckdb_store``
fixture 各跑一遍 ≈ 363 s = 全量 1749 s 的 20.8%。schema 是常量，因此改为
「会话级建一次模板库 + 每测试复制一份」。

本文件守住四件事（每一条都对应 T4 §5① 列出的一个已知坑）：

A. **调用次数**：``init_duckdb_schema`` 从「每测试一次」降到「每会话一次」，
   且 ``duckdb_store`` fixture **体内**不再有 DDL 调用（计数器看不见的回归
   由 AST 守卫补，见 :func:`test_duckdb_store_fixture_body_has_no_ddl_call`）。
B. **隔离性**：每个测试仍拿到一个独立的、可写的真实库；一个测试写进去的东西
   另一个测试看不到，模板本体字节不变（坑 2 / 5 / 7）。
C. **模板 == 真 DDL 的产物**：模板的库结构、``schema_migrations`` 版本集合与
   一次**真跑** ``init_duckdb_schema()`` 的结果一致，所以断言「迁移后状态」的
   11 个文件语义不变（坑 3）。
D. **「从零建库」这条路径继续有人测**：真跑 DDL 的用例留在本文件（坑 4）——
   历史上 v12 ``DROP TABLE funding_events`` 分支是「版本未记录时才执行」，
   改成「每会话一次」后必须仍有真跑路径 + 幂等路径的用例盯着它。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.schema import init_duckdb_schema
from tests.support import schema_template
from tests.support.schema_template import TEMPLATE_DB_FILENAME

#: 本模块里**实际跑过**的「取 ``duckdb_store``」用例名（供 module teardown 对账：
#: 用「跑了多少条」而不是「文件里写了几条」，这样 `-k` 只挑一条跑时账本仍然成立）。
_MATERIALIZING_RUNS: list[str] = []

#: 一次真实 init 建出的表数（实测 49；取宽松下界，只用来发现「模板是空的」）。
_MIN_EXPECTED_TABLES = 40
#: schema_migrations 的终态（实测记录 48 条，最大版本 49）。
_EXPECTED_MAX_MIGRATION_VERSION = 49

#: 上一条隔离性用例留下的痕迹（见 ``test_previous_test_writes_are_invisible_here``）。
_PREVIOUS_WRITES: dict[str, object] = {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _init_from_scratch(paths: DatabasePathSet) -> DuckDBStore:
    """在**空目录**上跑真实 DDL（模拟「从零建库」这条生产路径）。"""
    store = DuckDBStore(paths=paths)
    init_duckdb_schema(store)
    return store


def _table_names(store: DuckDBStore) -> set[str]:
    rows = store.read_query(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
    )
    return {row["table_name"] for row in rows}


def _migration_versions(store: DuckDBStore) -> list[tuple[int, str]]:
    rows = store.read_query(
        "SELECT version, description FROM schema_migrations ORDER BY version"
    )
    return [(int(row["version"]), str(row["description"])) for row in rows]


@pytest.fixture(scope="module", autouse=True)
def _assert_template_built_once_per_session():
    """会话级账本：整个会话只允许建一次模板（= 只跑一次 DDL）。

    放在 module teardown 检查，这样断言看到的是「本模块跑完之后」的全局计数，
    与模块在会话里的先后顺序无关（模板是 session 级，谁先要谁触发，也只触发一次）。
    """
    before = schema_template.snapshot()
    yield
    stats = schema_template.snapshot()
    assert stats["template_builds"] == 1, (
        f"schema 模板在一个会话里被建了 {stats['template_builds']} 次，应当恰好 1 次"
        f"（账本：{stats}）"
    )
    assert stats["init_duckdb_schema_calls"] == 1, (
        "init_duckdb_schema 在一个会话里被调用了 "
        f"{stats['init_duckdb_schema_calls']} 次，应当恰好 1 次（账本：{stats}）"
    )
    copied_here = stats["materializations"] - before["materializations"]
    assert copied_here >= len(_MATERIALIZING_RUNS), (
        f"本模块有 {len(_MATERIALIZING_RUNS)} 条用例各要了一份独立库，"
        f"但只发生了 {copied_here} 次模板复制（账本：{before} → {stats}）"
    )


# ---------------------------------------------------------------------------
# A. 调用次数：每测试 DDL → 每会话一次 DDL + 每测试一次复制
# ---------------------------------------------------------------------------


def test_duckdb_store_fixture_copies_template_without_running_ddl(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    schema_template_path: Path,
    database_paths: DatabasePathSet,
) -> None:
    """``duckdb_store`` 只复制模板：本测试期间 DDL 调用数 == 0，复制数 == 1。

    先声明 ``schema_template_path`` 依赖，保证模板已经用**真的**
    ``init_duckdb_schema`` 建好（否则下面这层替身会把模板建空）。
    """
    ddl_calls: list[object] = []

    def _explode(*args: object, **kwargs: object) -> None:
        ddl_calls.append((args, kwargs))
        raise AssertionError("duckdb_store fixture 不得在测试内重跑 init_duckdb_schema")

    monkeypatch.setattr(schema_template, "init_duckdb_schema", _explode)
    before = schema_template.snapshot()
    store = request.getfixturevalue("duckdb_store")
    after = schema_template.snapshot()

    assert ddl_calls == [], "取 duckdb_store 时触发了真实 DDL"
    assert after["materializations"] - before["materializations"] == 1, (
        f"本测试应恰好复制模板 1 次（账本变化：{before} → {after}）"
    )
    assert after["template_builds"] - before["template_builds"] == 0, (
        "取 duckdb_store 时不得重建模板"
    )
    # 拿到的必须是「一个真库」，不是空文件：schema 在复制件里完整存在。
    assert len(_table_names(store)) >= _MIN_EXPECTED_TABLES
    assert store.db_path == database_paths.duckdb_path
    _MATERIALIZING_RUNS.append("copies_template_without_running_ddl")


def test_duckdb_store_fixture_body_has_no_ddl_call() -> None:
    """AST 守卫：fixture **体内**不允许再出现 ``init_duckdb_schema`` 调用节点。

    计数器只看得见「经过模板构建器」的 DDL；如果将来有人把
    ``init_duckdb_schema(store)`` 加回 fixture（账本不变、测试照样绿），
    这条 AST 断言会先红 —— 它断的是「接线事实」，不是「某行文字」。
    """
    from tests import conftest as tests_conftest
    from tests.support import source_guard

    fixture_object = tests_conftest.duckdb_store
    fixture_func = getattr(fixture_object, "__wrapped__", fixture_object)
    source_guard.assert_calls(
        fixture_func,
        "materialize_duckdb_from_template",
        forbidden=("init_duckdb_schema", "init_all_schema", "execute_script"),
    )


# ---------------------------------------------------------------------------
# B. 隔离性：独立文件、可写、模板本体字节不变
# ---------------------------------------------------------------------------


def test_store_is_independent_writable_copy_and_template_is_untouched(
    duckdb_store: DuckDBStore,
    schema_template_path: Path,
) -> None:
    """复制件可写且独立；写进去的东西**不回流到模板**（模板只读 + 字节不变）。"""
    template = schema_template_path
    assert template.name == TEMPLATE_DB_FILENAME, "模板文件名必须与隔离库不同名"
    assert template.name != duckdb_store.db_path.name, (
        "模板绝不能是任何测试的隔离库（DatabasePathSet 只接受 valuedashboard.duckdb）"
    )
    assert duckdb_store.db_path != template
    assert duckdb_store.db_path.parent != template.parent, "复制件必须在测试自己的 tmp_path 里"

    digest_before = _sha256(template)
    template_mtime = template.stat().st_mtime_ns

    # 复制件**可写**：真写一行 + 一句 DDL（连 schema 都能改，证明不是只读挂载）。
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) "
        "VALUES ('000001', 'template guard', 'SZSE')"
    )
    duckdb_store.write_query("CREATE TABLE template_guard_probe (id INTEGER)")

    assert _sha256(template) == digest_before, "测试写入改掉了模板本体的字节"
    assert template.stat().st_mtime_ns == template_mtime, "测试写入碰了模板文件"
    # 模板原件是只读的：任何「直接写模板」的尝试都在文件系统层失败。
    with pytest.raises(PermissionError):
        template.open("ab").close()

    _PREVIOUS_WRITES["done"] = True
    _PREVIOUS_WRITES["db_path"] = duckdb_store.db_path
    _MATERIALIZING_RUNS.append("independent_writable_copy")


def test_previous_test_writes_are_invisible_here(duckdb_store: DuckDBStore) -> None:
    """上一条用例写进自己库里的行/表，这一条看不到（隔离性未退化）。"""
    _MATERIALIZING_RUNS.append("previous_writes_invisible")
    if _PREVIOUS_WRITES.get("done") is not True:
        pytest.skip("上一隔离性用例未执行（-k/--deselect 过滤），本条没有对照物可断言")
    assert _PREVIOUS_WRITES["db_path"] != duckdb_store.db_path, (
        "两条用例拿到了同一个库路径：每测试独立库的语义已破坏"
    )

    rows = duckdb_store.read_query(
        "SELECT count(*) AS c FROM stock_meta WHERE name = 'template guard'"
    )
    assert rows[0]["c"] == 0, "上一条用例的写入泄漏到了本测试的库"

    tables = _table_names(duckdb_store)
    assert "template_guard_probe" not in tables, "上一条用例的建表泄漏到了本测试的库"

    # 反过来确认「本测试的库确实是完整的 schema 库」，而不是一个空壳。
    assert "price_daily_raw" in tables and "schema_migrations" in tables


# ---------------------------------------------------------------------------
# C. 模板 == 一次真 DDL 的产物（11 个 schema_migrations 文件语义不变的前提）
# ---------------------------------------------------------------------------


def test_template_matches_fresh_ddl_database(
    database_paths: DatabasePathSet,
    schema_template_path: Path,
) -> None:
    """模板与「空目录上真跑一遍 DDL」的库结构/版本集合完全一致。"""
    fresh = _init_from_scratch(database_paths)
    fresh_tables = _table_names(fresh)
    fresh_versions = _migration_versions(fresh)

    # 模板以只读连接打开：只读本身也证明「复制源不会被测试改动」。
    connection = duckdb.connect(str(schema_template_path), read_only=True)
    try:
        template_tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
            ).fetchall()
        }
        template_versions = [
            (int(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT version, description FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    finally:
        connection.close()

    assert fresh_tables, "真 DDL 建出来的库居然是空表集合"
    assert template_tables == fresh_tables, (
        "模板与真 DDL 的表集合不一致："
        f"只出现在模板 {sorted(template_tables - fresh_tables)}；"
        f"只出现在真 DDL {sorted(fresh_tables - template_tables)}"
    )
    assert template_versions == fresh_versions, "模板的 schema_migrations 版本集合与真 DDL 不一致"
    assert len(template_versions) > 0
    assert max(version for version, _ in template_versions) == _EXPECTED_MAX_MIGRATION_VERSION


# ---------------------------------------------------------------------------
# D. 保留「真跑 DDL」路径：从零建库 + 幂等（含 v12 DROP 分支）
# ---------------------------------------------------------------------------


def test_init_duckdb_schema_from_scratch_still_covered(
    database_paths: DatabasePathSet,
) -> None:
    """从零建库仍真跑 DDL：关键表 + 迁移终态 + **v12 已撤销 funding_events 复合主键**。

    v12 分支（``DROP TABLE funding_events``）只在
    ``schema_migrations.version = 12`` 未记录时执行；模板化之后它每会话只跑一次，
    所以「空库上执行它」这条路径必须继续有显式用例 —— 就是这一条。
    """
    store = _init_from_scratch(database_paths)

    tables = _table_names(store)
    for expected in ("funding_events", "stock_meta", "price_daily_qfq", "schema_migrations"):
        assert expected in tables, f"从零建库缺少 {expected}"

    versions = _migration_versions(store)
    assert max(version for version, _ in versions) == _EXPECTED_MAX_MIGRATION_VERSION
    assert any(version == 12 for version, _ in versions), "v12 迁移版本未记录"

    primary_key = store.read_query(
        "SELECT count(*) AS c FROM duckdb_constraints() "
        "WHERE table_name = 'funding_events' AND constraint_type = 'PRIMARY KEY'"
    )
    assert primary_key[0]["c"] == 0, (
        "v12 迁移未生效：funding_events 仍有复合主键 "
        "（同 list_date 多批次增发会被主键吞掉）"
    )


def test_init_duckdb_schema_is_idempotent_on_migrated_database(
    database_paths: DatabasePathSet,
) -> None:
    """迁移后再跑一遍 DDL 必须不报错、不重建（v12 的「版本已记录就不 DROP」）。

    模板化把这条幂等路径从「每条测试都跑」压到「每会话一次」，所以单独补一条：
    第二次 init 拿到的是已经记录了 v12 的库，必须**跳过** DROP 而不是再删表。
    """
    store = _init_from_scratch(database_paths)
    store.write_query(
        """INSERT INTO funding_events
               (stock_code, event_type, list_date, source, fetch_time,
                raw_hash, confidence, batch_id)
           VALUES ('000001', 'refinance', DATE '2025-01-02', 'fixture',
                   CURRENT_TIMESTAMP, 'guard-hash', 'strict', 'guard-batch')"""
    )
    rows_before = store.read_query("SELECT count(*) AS c FROM funding_events")[0]["c"]
    assert rows_before == 1

    init_duckdb_schema(store)  # 第二遍：只允许幂等 DDL，不允许 DROP 掉上面的数据

    rows_after = store.read_query("SELECT count(*) AS c FROM funding_events")[0]["c"]
    assert rows_after == rows_before, (
        "第二次 init_duckdb_schema 重建了 funding_events（v12 的「版本已记录不 DROP」护栏失效）"
    )
    assert max(v for v, _ in _migration_versions(store)) == _EXPECTED_MAX_MIGRATION_VERSION
