from __future__ import annotations

import pytest

from app.core.dsl.engine import DSLEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_draft_must_validate_before_any_preview(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    engine.create("revenue_ttm", "income.revenue@TTM", "营收 TTM", "higher_is_better")

    assert engine.preview_single("revenue_ttm", 1, "600519") == {
        "error": "expression must be validated before single-stock preview"
    }
    assert engine.preview_sample("revenue_ttm", 1) == {
        "error": "expression must complete single-stock preview before sample preview"
    }
    assert engine.publish("revenue_ttm", 1)["error"].startswith("表达式必须完成校验和两次预览")


def test_validated_expression_can_complete_preview_lifecycle(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    engine.create("latest_assets", "balance.total_assets", "总资产", "higher_is_better")

    validated = engine.validate("latest_assets", 1)

    assert validated["status"] == "validated"
    assert engine.publish("latest_assets", 1)["error"].startswith("表达式必须完成校验和两次预览")
    assert engine.preview_sample("latest_assets", 1) == {
        "error": "expression must complete single-stock preview before sample preview"
    }
    assert "error" not in engine.preview_single("latest_assets", 1, "600519")
    preview = engine.preview_sample("latest_assets", 1)
    assert "error" not in preview
    assert engine.registry.get("latest_assets", 1)["status"] == "previewed"


def test_registry_cannot_bypass_or_regress_the_lifecycle(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    engine.create("immutable_assets", "balance.total_assets", "总资产", "higher_is_better")

    with pytest.raises(ValueError, match="invalid DSL lifecycle transition"):
        engine.registry.preview("immutable_assets", 1)

    assert engine.validate("immutable_assets", 1)["status"] == "validated"
    assert "error" not in engine.preview_single("immutable_assets", 1, "600519")
    assert "error" not in engine.preview_sample("immutable_assets", 1)
    assert engine.publish("immutable_assets", 1)["status"] == "published"
    assert engine.validate("immutable_assets", 1) == {
        "error": "expression must be a draft before validation"
    }
    with pytest.raises(ValueError, match="invalid DSL lifecycle transition"):
        engine.registry.publish("immutable_assets", 1)
    with pytest.raises(ValueError, match="dependencies can only be recorded"):
        engine.registry.add_dependency("immutable_assets", 1, "immutable_assets", 1)


def test_historical_dsl_cannot_publish_when_screening_has_no_equivalent_planner(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    created = engine.create("historical_revenue", "income.revenue@TTM", "历史营收", "higher_is_better")
    monkeypatch.setattr(engine.registry, "get", lambda *_args: {
        "status": "previewed", "expression_text": "income.revenue@TTM",
    })

    assert engine.publish(created["name"], created["version"]) == {
        "error": "screening cannot publish historical-period DSL expressions"
    }
