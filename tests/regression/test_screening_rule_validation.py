from __future__ import annotations

import pytest

from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore


@pytest.mark.parametrize(
    "conditions",
    [
        {"logic": "OR 1=1 --", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
        {"logic": "AND", "rules": [{"field": "unknown; DROP TABLE stock_meta", "op": ">", "value": 0}]},
        {"logic": "AND", "rules": [{"field": "pe_ttm", "op": "bogus", "value": 0}]},
        {"logic": "AND", "rules": [{"field": "pe_ttm", "op": "between", "value": [0]}]},
    ],
)
def test_invalid_screening_rules_fail_closed(duckdb_store: DuckDBStore, conditions: dict) -> None:
    with pytest.raises(ValueError):
        ScreeningEngine(duck=duckdb_store).run({"conditions": conditions}, min_listing_years=0)


def test_default_pool_rejects_unknown_suspension_status(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Unknown status', 'SZSE', '2020-01-01', false, NULL)"""
    )

    with pytest.raises(ValueError, match="base pool metadata is incomplete"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}}, min_listing_years=0
        )


def test_historical_unknown_metadata_does_not_block_current_pool(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'Current', 'SZSE', TRUE), ('000002', 'Historical', 'SZSE', FALSE)"""
    )

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        include_st=True,
        include_suspended=True,
        min_listing_years=0,
    )

    assert result["base_pool_size"] == 1


def test_listing_age_rejects_unknown_current_listing_date(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_st, is_suspended)
           VALUES ('000001', 'Unknown date', 'SZSE', false, false)"""
    )

    with pytest.raises(ValueError, match="base pool metadata is incomplete"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}}, min_listing_years=1
        )


def test_condition_can_compare_two_known_numeric_fields(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Compare', 'SZSE', '2020-01-01', false, false)"""
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm, pb_mrq)
           VALUES ('000001', '2025-12-31', 10, 5)"""
    )

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "right_field": "pb_mrq"},
        ]}},
        min_listing_years=0,
    )

    assert result["total"] == 1


def test_condition_rejects_metadata_to_metric_comparison(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="不兼容"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": [
                {"field": "name", "op": "=", "right_field": "pe_ttm"},
            ]}},
            min_listing_years=0,
        )


def test_condition_rejects_resource_exhausting_in_list(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)

    with pytest.raises(ValueError, match="最多允许"):
        engine._build_condition({"field": "pe_ttm", "op": "in", "value": list(range(1001))})
