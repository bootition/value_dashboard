from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.data_quality import (
    _published_override_stale_snapshot_codes,
    minimum_data_readiness,
    screening_readiness,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def test_empty_database_is_not_screenable(duckdb_store: DuckDBStore) -> None:
    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["stock_count"] == 0


def test_readiness_requires_complete_history_fresh_prices_and_coherent_snapshot(
    duckdb_store: DuckDBStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)

    assert minimum_data_readiness(duckdb_store)["ready"] is True

    duckdb_store.write_query(
        "DELETE FROM price_daily_raw WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '3 years'"
    )
    assert minimum_data_readiness(
        duckdb_store, minimum_volume_observations=1,
    )["missing_counts"] == {"raw_history": 1}

    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_close = 11 WHERE stock_code = '000001'"
    )
    assert minimum_data_readiness(duckdb_store)["missing_counts"] == {"snapshot_price_coherence": 1}


@pytest.mark.parametrize(
    ("changes", "missing_key"),
    [
        (
            [
                "DELETE FROM price_daily_qfq WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '3 years'",
            ],
            "qfq_history",
        ),
        (
            [
                "DELETE FROM price_daily_raw WHERE stock_code = '000001' AND trade_date >= CURRENT_DATE - INTERVAL '8 days'",
                "DELETE FROM price_daily_qfq WHERE stock_code = '000001' AND trade_date >= CURRENT_DATE - INTERVAL '8 days'",
                "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', CURRENT_DATE - INTERVAL '8 days', 10)",
                "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES ('000001', CURRENT_DATE - INTERVAL '8 days', 10)",
                "UPDATE indicator_snapshot SET latest_price_date = CURRENT_DATE - INTERVAL '8 days' WHERE stock_code = '000001'",
                "UPDATE source_audit SET report_date = CURRENT_DATE - INTERVAL '8 days' "
                "WHERE stock_code = '000001' AND field_name = 'latest_close' "
                "AND fetch_batch_id IN (SELECT batch_id FROM fetch_batch "
                "WHERE data_type IN ('price_daily_raw', 'price_daily_qfq'))",
            ],
            "price_freshness",
        ),
        (
            ["DELETE FROM cash_flow WHERE stock_code = '000001' AND report_date = '2025-12-31'"],
            "financial_period",
        ),
    ],
)
def test_readiness_identifies_each_remaining_hard_requirement(
    duckdb_store: DuckDBStore,
    changes: list[str],
    missing_key: str,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    for statement in changes:
        duckdb_store.write_query(statement)

    readiness = minimum_data_readiness(duckdb_store)

    if missing_key == "price_freshness":
        # 价格陈旧属披露项（PRD §6.4 D7: 允许陈旧但显示日期）: 有数据但
        # 超过 7 天无新 bar 视为停牌豁免，不阻断 ready 也不计入缺口。
        assert readiness["ready"] is True
        assert "price_freshness" not in readiness["missing_counts"]
    else:
        assert readiness["ready"] is False
        assert readiness["missing_counts"][missing_key] == 1


def test_readiness_requires_sufficient_nonzero_volume(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query("UPDATE price_daily_raw SET volume = 0 WHERE stock_code = '000001'")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"] == {"meaningful_volume": 1}


@pytest.mark.parametrize(
    ("statement", "missing_key"),
    [
        ("DELETE FROM xdxr WHERE stock_code = '000001'", "corporate_action_dividend_lineage"),
        ("UPDATE stock_meta SET total_shares = NULL WHERE stock_code = '000001'", "share_capital"),
    ],
)
def test_readiness_requires_company_actions_and_share_capital(
    duckdb_store: DuckDBStore, statement: str, missing_key: str,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(statement)

    assert minimum_data_readiness(duckdb_store)["missing_counts"][missing_key] == 1


def test_readiness_rejects_snapshot_older_than_financial_source_input(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
           VALUES ('000001', 'revenue', '2025-12-31', 100, 'test', 'new-financial-source',
                   CURRENT_TIMESTAMP + INTERVAL '1 second', repeat('a', 64), 'strict')"""
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_input_freshness"] == 1


def test_readiness_rejects_materialized_data_without_source_graph(duckdb_store: DuckDBStore) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query("DELETE FROM source_audit WHERE stock_code = '000001'")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["lineage_coverage"] == 1


def test_screening_requires_a_persisted_trading_calendar(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


def test_screening_rejects_a_trading_calendar_gap_for_a_qfq_series(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    assert screening_readiness(duckdb_store, sqlite_store)["ready"] is True

    gap = duckdb_store.read_query(
        """SELECT CAST(trade_date AS VARCHAR) AS trade_date FROM price_daily_qfq
           WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           ORDER BY trade_date DESC LIMIT 1"""
    )[0]["trade_date"]
    sqlite_store.execute("DELETE FROM trading_dates WHERE trade_date = ?", [gap])

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


def test_screening_rejects_an_internal_qfq_gap_on_a_calendar_date(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # Remove a gap larger than the 2% suspension tolerance.
    # 60 natural days ≈ 43 trading days of ~1570 (≈2.7%).
    duckdb_store.write_query(
        """DELETE FROM price_daily_qfq WHERE stock_code = '000001'
           AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           AND trade_date >= CURRENT_DATE - INTERVAL '1 year' - INTERVAL '60 days'"""
    )

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


def test_screening_tolerates_a_small_calendar_gap_as_suspension(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # A single missing bar is within the 2% suspension tolerance.
    duckdb_store.write_query(
        """DELETE FROM price_daily_qfq WHERE stock_code = '000001'
           AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           AND trade_date >= CURRENT_DATE - INTERVAL '1 year' - INTERVAL '2 days'"""
    )

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is True


def test_published_override_after_snapshot_blocks_readiness(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET calculated_at = TIMESTAMP '2000-01-01' WHERE stock_code = '000001'"
    )
    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status, created_at)
           VALUES ('000001', 'total_assets', '2025-12-31', 101, 'correction', 'published',
                   datetime('now', '+1 hour'))"""
    )

    assert sqlite_store.query(
        "SELECT stock_code, created_at FROM manual_overrides WHERE status = 'published'"
    )[0]["stock_code"] == "000001"
    assert _published_override_stale_snapshot_codes(duckdb_store, sqlite_store) == ["000001"]
    readiness = minimum_data_readiness(duckdb_store, sqlite_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_input_freshness"] == 1


def test_readiness_blocks_durable_actions_when_current_data_changes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'ready', 'SZSE', '2020-01-01', FALSE, FALSE)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    assert minimum_data_readiness(duckdb_store)["ready"] is True

    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = {"ready": True}
    app.include_router(router)
    duckdb_store.write_query("UPDATE indicator_snapshot SET latest_close = 11 WHERE stock_code = '000001'")

    response = TestClient(app).post("/api/screening/save", json={"title": "blocked", "run_id": "missing"})

    assert response.status_code == 409
    assert response.json()["detail"]["readiness"]["missing_counts"] == {"snapshot_price_coherence": 1}


def test_readiness_blocks_an_incompatible_live_schema(duckdb_store: DuckDBStore) -> None:
    duckdb_store.execute_script("DROP TABLE raw_response_archive")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert "raw_response_archive.raw_response_hash" in readiness["schema_compatibility"]["missing"]


def test_screening_rejects_a_known_incomplete_startup_state(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = {"ready": False, "missing_counts": {"raw_history": 1}}
    app.include_router(router)

    response = TestClient(app).post(
        "/api/screening/run", json={"rule_id": 1, "rule_version": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "minimum_data_not_ready"


def test_screening_durable_actions_revalidate_current_readiness(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = {"ready": True}
    app.include_router(router)
    client = TestClient(app)

    for path, payload in (
        ("/api/screening/save", {"title": "blocked", "run_id": "missing"}),
        ("/api/screening/export_csv", {"result_id": 1}),
        ("/api/screening/add_to_watchlist", {"stock_codes": ["000001"], "result_id": 1}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["reason_code"] == "minimum_data_not_ready"
