from __future__ import annotations

import pytest

from app.core.dsl.engine import DSLEngine, expand_shorthand
from app.core.dsl.registry import ExpressionRegistry
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

# ─── 2a: 带 @ 后缀的简写不得二次追加 ───────────────────────────────────────

def test_expand_shorthand_never_double_appends_period_suffix() -> None:
    assert expand_shorthand("revenue") == "income.revenue@TTM"
    assert expand_shorthand("total_assets") == "balance.total_assets@LATEST"
    # 带 @ 的 token 不是裸字段：原样保留（parser 随后报缺少表前缀的语法
    # 错误），绝不生成 income.revenue@TTM@TTM 这种被污染表达式。
    assert expand_shorthand("revenue@TTM") == "revenue@TTM"
    assert expand_shorthand("total_assets@LATEST") == "total_assets@LATEST"
    # 完整引用不受影响
    assert expand_shorthand("income.revenue@TTM") == "income.revenue@TTM"
    assert expand_shorthand("balance.total_assets@LATEST") == "balance.total_assets@LATEST"


# ─── 2b: 依赖版本锁定使用最新 published 版本 ───────────────────────────────

def _publish(engine: DSLEngine, name: str, expression: str) -> dict:
    engine.create(name, expression, description=name)
    assert engine.validate(name, 1)["valid"] is True
    assert "error" not in engine.preview_single(name, 1, "600519")
    assert "error" not in engine.preview_sample(name, 1)
    published = engine.publish(name, 1)
    assert published.get("status") == "published"
    return published


def test_dependency_locks_latest_published_when_newer_draft_exists(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    _publish(engine, "base_ind", "balance.total_assets")
    # 同一名称创建新 draft（未发布）：依赖解析必须仍锁旧 published v1。
    engine.create("base_ind", "balance.total_assets")

    assert engine.registry.get("base_ind")["version"] == 1
    assert engine.registry.get("base_ind")["status"] == "published"

    engine.create("dep_ind", "base_ind", description="depends on base")
    outcome = engine.validate("dep_ind", 1)

    assert outcome["valid"] is True
    dependency_rows = sqlite_store.query(
        """SELECT d.depends_on_version
           FROM dsl_dependencies d
           JOIN dsl_expressions e ON e.id = d.expression_id
           JOIN dsl_expressions dep ON dep.id = d.depends_on_id
           WHERE e.name = 'dep_ind' AND dep.name = 'base_ind'"""
    )
    assert dependency_rows == [{"depends_on_version": 1}]


def test_registry_get_without_version_uses_latest_published_semantics(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    _publish(engine, "multi_ver", "balance.total_assets")
    engine.create("multi_ver", "balance.total_assets")  # v2 draft

    assert engine.registry.get("multi_ver")["version"] == 1
    assert engine.registry.get("multi_ver")["status"] == "published"


def test_dependency_without_any_published_version_is_rejected(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    engine.create("unpublished_base", "balance.total_assets")
    engine.create("dep_on_unpublished", "unpublished_base")

    outcome = engine.validate("dep_on_unpublished", 1)

    assert outcome["valid"] is False
    assert any("unpublished_base" in error for error in outcome["errors"])


# ─── 2c: 指标名不得与 FIELD 简写冲突 ───────────────────────────────────────

def test_create_rejects_indicator_name_colliding_with_field_shorthand(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)

    with pytest.raises(ValueError, match="shorthand field name"):
        engine.create("revenue", "balance.total_assets")
    with pytest.raises(ValueError, match="shorthand field name"):
        engine.create("total_assets", "balance.total_assets")


def test_publish_also_rejects_legacy_name_colliding_with_field_shorthand(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    # 绕过 create（模拟历史数据/直接 INSERT）进入可发布状态。
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO dsl_expressions
               (name, version, expression_text, status, description, direction, historical_capable)
               VALUES ('revenue', 1, 'balance.total_assets', 'previewed', 'legacy', 'none', FALSE)"""
        )

    result = engine.publish("revenue", 1)

    assert result == {"error": "expression name conflicts with a shorthand field name"}


def test_registry_dependency_requires_published_version(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    engine.create("draft_base", "balance.total_assets")
    assert engine.validate("draft_base", 1)["valid"] is True

    with pytest.raises(ValueError, match="published expression versions"):
        engine.registry.add_dependency("draft_base", 1, "draft_base", 1)


def test_registry_identifier_validation_is_shared(
    sqlite_store: SQLiteStore,
) -> None:
    registry = ExpressionRegistry(sqlite=sqlite_store)
    with pytest.raises(ValueError, match="shorthand field name"):
        registry.create("revenue", "balance.total_assets")
