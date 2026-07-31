from __future__ import annotations

from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore


def _seed_screening_data(duck: DuckDBStore) -> None:
    duck.write_query(
        """INSERT INTO stock_meta
           (stock_code, name, exchange, listing_date, is_st, is_suspended, csrc_l1, csrc_l2)
           VALUES
           ('000001', 'Strict One', 'SZSE', '2020-01-01', false, false, 'Finance', 'Bank'),
           ('000002', 'Approx Two', 'SZSE', '2020-01-01', false, false, 'Finance', 'Insurance'),
           ('000003', 'Strict Three', 'SZSE', '2020-01-01', false, false, 'Industry', 'Machinery')"""
    )
    duck.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm, roe)
           VALUES
           ('000001', '2025-12-31', 10, 0.20),
           ('000002', '2025-12-31', 20, 0.10),
           ('000003', '2025-12-31', 30, 0.30)"""
    )
    duck.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
           VALUES
           ('000001', 'pe_ttm', '2025-12-31', 10, 'test', 'a', CURRENT_TIMESTAMP, repeat('a', 64), 'strict'),
           ('000002', 'pe_ttm', '2025-12-31', 20, 'test', 'b', CURRENT_TIMESTAMP, repeat('b', 64), 'approximate'),
           ('000003', 'pe_ttm', '2025-12-31', 30, 'test', 'c', CURRENT_TIMESTAMP, repeat('c', 64), 'strict')"""
    )


def test_strict_only_requires_latest_strict_field_audit(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    rule = {
        "conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
        "sort": [{"field": "pe_ttm", "direction": "asc"}],
        "columns": ["stock_code", "pe_ttm"],
    }

    permissive = engine.run(rule, min_listing_years=0)
    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert [row["stock_code"] for row in permissive["results"]] == ["000001", "000002", "000003"]
    assert [row["stock_code"] for row in strict["results"]] == ["000001", "000003"]
    assert strict["strict_fields"] == ["pe_ttm"]


def test_strict_only_rejects_audited_value_that_differs_from_snapshot(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
           VALUES ('000001', 'pe_ttm', '2025-12-31', 9, 'test', 'new', CURRENT_TIMESTAMP + INTERVAL '1 second', repeat('d', 64), 'strict')"""
    )

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]}},
        min_listing_years=0,
        strict_only=True,
    )

    assert "000001" not in [row["stock_code"] for row in result["results"]]


def test_rank_fields_are_generated_for_sort_and_sw_levels(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    result = ScreeningEngine(duck=duckdb_store).run(
        {
            "conditions": {"logic": "AND", "rules": []},
            "sort": [{"field": "pe_ttm_sw2_rank", "direction": "asc"}],
            "columns": [
                "stock_code", "pe_ttm_market_rank", "pe_ttm_sw1_rank", "pe_ttm_sw2_rank",
            ],
        },
        min_listing_years=0,
    )

    assert result["total"] == 3
    by_code = {row["stock_code"]: row for row in result["results"]}
    assert by_code["000001"]["pe_ttm_market_rank"] == 1
    assert by_code["000001"]["pe_ttm_sw1_rank"] == 1
    assert by_code["000001"]["pe_ttm_sw2_rank"] == 1


def test_normalized_statement_fields_support_filter_columns_and_ranks(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets)
           VALUES ('000001', '2025-12-31', 100), ('000002', '2025-12-31', 200),
                  ('000003', '2025-12-31', 300)"""
    )

    result = ScreeningEngine(duck=duckdb_store).run(
        {
            "conditions": {"logic": "AND", "rules": [
                {"field": "balance.total_assets", "op": ">=", "value": 200},
            ]},
            "sort": [{"field": "balance.total_assets", "direction": "desc"}],
            "columns": ["stock_code", "balance.total_assets", "balance.total_assets_market_rank"],
        },
        min_listing_years=0,
    )

    assert result["results"] == [
        {"stock_code": "000003", "name": "Strict Three", "balance.total_assets": 300.0,
         "balance.total_assets_market_rank": 3},
        {"stock_code": "000002", "name": "Approx Two", "balance.total_assets": 200.0,
         "balance.total_assets_market_rank": 2},
    ]
