from __future__ import annotations

from app.core.dsl.ast_nodes import FIELD_METADATA, INDICATOR_METADATA
from app.core.dsl.codegen import CodeGen
from app.core.dsl.engine import DSLEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_every_normalized_screening_field_has_dsl_metadata() -> None:
    from app.core.screening.engine import NORMALIZED_FIELDS, SNAPSHOT_COLUMNS

    assert NORMALIZED_FIELDS <= set(FIELD_METADATA)
    assert SNAPSHOT_COLUMNS <= set(INDICATOR_METADATA)


def test_dsl_rejects_unknown_standardized_field_and_accepts_ttm_shorthand(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    dsl_engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    unknown = dsl_engine.create("unknown_field", "income.not_a_real_field")
    assert dsl_engine.validate("unknown_field", unknown["version"])["valid"] is False

    valid = dsl_engine.create("ttm_revenue", "revenue")
    outcome = dsl_engine.validate("ttm_revenue", valid["version"])
    assert outcome["valid"] is True
    assert outcome["expanded_expression"] == "income.revenue@TTM"


def test_time_series_codegen_partitions_every_window_by_stock() -> None:
    generated = CodeGen.FUNC_SQL
    for function in ("YoY", "QoQ", "CAGR", "rolling_avg", "rolling_max", "rolling_min", "lag"):
        assert "PARTITION BY stock_code" in generated[function]
