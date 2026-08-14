from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.backfill import PriceBackfiller
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.schema import init_duckdb_schema
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater
from app.web.api.stock_detail import get_kline


class StaticAdapterManager:
    def __init__(self, results: dict[str, FetchResult]) -> None:
        self._results = results

    def fetch(self, request: FetchRequest) -> FetchResult:
        key = request.data_type if request.data_type != "price_daily" else request.adjust
        return self._results[key]


def _result(
    data: list[dict[str, str | float | None]],
    error: str | None = None,
) -> FetchResult:
    raw_response = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source="akshare_eastmoney",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
            confidence="missing" if error else "approximate",
            error=error,
        ),
        raw_response=raw_response,
    )


def test_old_qfq_schema_is_upgraded_idempotently(database_paths: DatabasePathSet) -> None:
    store = DuckDBStore(paths=database_paths)
    store.write_query(
        """
        CREATE TABLE price_daily_qfq (
            stock_code VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            close DOUBLE,
            PRIMARY KEY (stock_code, trade_date)
        )
        """
    )

    init_duckdb_schema(store)
    init_duckdb_schema(store)

    columns = store.read_query(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'price_daily_qfq'
        ORDER BY ordinal_position
        """
    )
    assert "turnover_rate" in {row["column_name"] for row in columns}


def test_income_statement_legacy_schema_merges_without_an_upsert_constraint(
    database_paths: DatabasePathSet,
) -> None:
    store = DuckDBStore(paths=database_paths)
    store.write_query(
        """CREATE TABLE income_statement (
               stock_code VARCHAR NOT NULL,
               report_date DATE NOT NULL,
               revenue DOUBLE,
               parent_net_profit DOUBLE
           )"""
    )

    initializer = DataInitializer.__new__(DataInitializer)
    with store.transaction() as connection:
        initializer._upsert_financial_row(
            connection,
            "income_statement",
            "000001",
            {"REPORT_DATE": "2025-03-31", "OPERATE_INCOME": 1.0, "PARENT_NETPROFIT": 2.0},
        )

    assert store.read_query(
        "SELECT revenue, parent_net_profit FROM income_statement"
    ) == [{"revenue": 1.0, "parent_net_profit": 2.0}]


def test_qfq_api_supports_schema_before_turnover_rate_migration(
    database_paths: DatabasePathSet,
) -> None:
    store = DuckDBStore(paths=database_paths)
    store.execute_script(
        """
        CREATE TABLE price_daily_qfq (
            stock_code VARCHAR,
            trade_date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            turnover DOUBLE
        );
        INSERT INTO price_daily_qfq VALUES
            ('600519', '2026-07-17', 1400, 1510, 1390, 1500, 100, 150000);
        """
    )
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(duck=store))}
    )

    result = get_kline("600519", request=request, adjust="qfq", days=250)

    assert result["count"] == 1
    assert result["candles"][0]["turnover_rate"] is None


def test_stock_refresh_preserves_known_optional_metadata(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """
        INSERT INTO stock_meta
            (stock_code, name, exchange, listing_date, is_st, is_suspended, csrc_l1)
        VALUES ('600519', 'old', 'SSE', '2001-08-27', FALSE, TRUE, '食品饮料')
        """
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = StaticAdapterManager(
        {"stock_list": _result([{"stock_code": "600519", "name": "new", "exchange": "SSE"}])}
    )
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer._batch_id = "test-batch"

    initializer._fetch_stock_universe()

    rows = duckdb_store.read_query(
        """
        SELECT name, listing_date, is_st, is_suspended, csrc_l1
        FROM stock_meta WHERE stock_code = '600519'
        """
    )
    assert rows[0]["name"] == "new"
    assert str(rows[0]["listing_date"]) == "2001-08-27"
    assert rows[0]["is_st"] is False
    assert rows[0]["is_suspended"] is True
    assert rows[0]["csrc_l1"] == "食品饮料"


def test_stock_refresh_marks_absent_historical_codes_not_listed(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'current', 'SZSE', TRUE), ('000003', 'historical', 'SZSE', TRUE)"""
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = StaticAdapterManager(
        {"stock_list": _result([{"stock_code": "000001", "name": "current", "exchange": "SZSE"}])}
    )
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer._batch_id = "test-batch"

    initializer._fetch_stock_universe()

    assert duckdb_store.read_query(
        "SELECT stock_code, is_listed FROM stock_meta ORDER BY stock_code"
    ) == [
        {"stock_code": "000001", "is_listed": True},
        {"stock_code": "000003", "is_listed": False},
    ]


def test_partial_stock_refresh_does_not_delist_an_unreturned_exchange(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000001', 'current', 'SZSE', TRUE), ('600001', 'sse', 'SSE', TRUE)"""
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = StaticAdapterManager(
        {"stock_list": _result([{"stock_code": "000001", "name": "current", "exchange": "SZSE"}])}
    )
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer._batch_id = "test-batch"

    initializer._fetch_stock_universe()

    assert duckdb_store.read_query(
        "SELECT stock_code, is_listed FROM stock_meta ORDER BY stock_code"
    ) == [
        {"stock_code": "000001", "is_listed": True},
        {"stock_code": "600001", "is_listed": True},
    ]


def test_listing_info_is_persisted_without_defaulting_unknown_status(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange)
           VALUES ('600519', '贵州茅台', 'SSE')"""
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = StaticAdapterManager(
        {
            "listing_info": _result(
                [{
                    "stock_code": "600519",
                    "listing_date": "2001-08-27",
                    "is_st": False,
                    "is_suspended": None,
                    "total_shares": 1_000_000,
                    "circ_shares": 800_000,
                }]
            )
        }
    )
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer._batch_id = "test-batch"

    report = initializer._fetch_listing_info()

    row = duckdb_store.read_query(
        """SELECT listing_date, is_st, is_suspended, total_shares, circ_shares
           FROM stock_meta WHERE stock_code = '600519'"""
    )[0]
    missing = sqlite_store.query(
        """SELECT reason_code FROM missing_list
           WHERE stock_code = '600519' AND field_name = 'listing_info'"""
    )
    assert report == {"status": "partial", "count": 1, "missing": 1}
    assert str(row["listing_date"]) == "2001-08-27"
    assert row["is_st"] is False
    assert row["is_suspended"] is None
    assert row["total_shares"] == 1_000_000
    assert row["circ_shares"] == 800_000
    assert missing == [{"reason_code": "source_incomplete"}]


def test_full_init_skips_indicator_publication_when_prerequisites_are_not_ready(
    monkeypatch,
) -> None:
    initializer = DataInitializer.__new__(DataInitializer)
    initializer._batch_id = "test-batch"
    initializer._log_job_start = lambda _job_type: 1
    initializer._log_job_finish = lambda *_args: None
    initializer._fetch_stock_universe = lambda: {"status": "success"}
    initializer._fetch_listing_info = lambda: {"status": "success"}
    initializer._fetch_trading_dates = lambda: {"status": "success"}
    initializer._fetch_sw_industry = lambda: {"status": "success"}
    initializer._fetch_daily_prices = lambda *, years: {"status": "skipped", "reason": "no_stocks"}
    initializer._fetch_financial_statements = lambda: {"status": "success"}

    class UnexpectedCalculator:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("indicator calculator must not run without ready prerequisites")

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", UnexpectedCalculator)

    report = initializer.run_full_init()

    assert report["steps"]["indicators"] == {
        "status": "skipped",
        "reason": "prerequisites_not_ready",
        "daily_prices_status": "skipped",
        "financials_status": "success",
    }


def test_non_bse_qfq_failure_preserves_old_prices_and_records_retry(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """
        INSERT INTO stock_meta (stock_code, name, exchange, listing_date)
        VALUES ('600519', '贵州茅台', 'SSE', '2001-08-27')
        """
    )
    duckdb_store.write_query(
        """
        INSERT INTO price_daily_raw (stock_code, trade_date, close)
        VALUES ('600519', '2025-01-02', 10.0)
        """
    )
    backfiller = PriceBackfiller.__new__(PriceBackfiller)
    backfiller.adapter_mgr = StaticAdapterManager(
        {
            "raw": _result([{"trade_date": "2026-01-02", "close": 20.0}]),
            "qfq": _result([], error="qfq unavailable"),
        }
    )
    backfiller.duck = duckdb_store
    backfiller.sqlite = sqlite_store
    backfiller._batch_id = "test-batch"

    report = backfiller._backfill_prices(skip_if_complete=False, max_stocks=1)

    prices = duckdb_store.read_query(
        "SELECT trade_date, close FROM price_daily_raw WHERE stock_code = '600519'"
    )
    retry = sqlite_store.query(
        "SELECT data_type, error, extra_json FROM retry_list WHERE stock_code = '600519'"
    )
    assert report["failed"] == 1
    assert len(prices) == 1
    assert str(prices[0]["trade_date"]) == "2025-01-02"
    assert retry == [
        {
            "data_type": "price_daily",
            "error": "qfq unavailable",
            "extra_json": '{"adjust": "qfq"}',
        }
    ]


def test_replenish_only_refetches_missing_current_stock_inputs(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
               VALUES ('000001', 'ready', 'SZSE', TRUE)"""
        )
        connection.execute(
            """INSERT INTO balance_sheet
               (stock_code, report_date, total_assets, total_liabilities, total_equity)
               VALUES ('000001', '2025-03-31', 100, 20, 80)"""
        )
        connection.execute(
            """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
               VALUES ('000001', '2025-03-31', 100, 10)"""
        )
        connection.execute(
            """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
               VALUES ('000001', '2025-03-31', 10)"""
        )
        connection.execute(
            """INSERT INTO price_daily_raw (stock_code, trade_date, close)
               VALUES ('000001', '2025-03-31', 10)"""
        )
        connection.execute(
            """INSERT INTO price_daily_qfq (stock_code, trade_date, close)
               VALUES ('000001', '2025-03-31', 10)"""
        )
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES ('000002', 'missing', 'SZSE', TRUE), ('000003', 'historical', 'SZSE', FALSE)"""
    )
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store
    requested: list[tuple[str, str]] = []

    def refetch(stock_code: str, data_type: str) -> dict[str, str]:
        requested.append((stock_code, data_type))
        return {"status": "success"}

    monkeypatch.setattr(updater, "refetch_one", refetch)

    report = updater.replenish_missing_core_data()

    assert report == {
        "status": "success",
        "targeted": 1,
        "completed": 1,
        "failed": 0,
        "failed_codes": [],
    }
    assert requested == [
        ("000002", "balance_sheet"),
        ("000002", "income_statement"),
        ("000002", "cash_flow"),
        ("000002", "price_daily"),
    ]


def test_initializer_qfq_failure_preserves_old_prices_and_records_retry(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """
        INSERT INTO stock_meta (stock_code, name, exchange, listing_date)
        VALUES ('600519', '贵州茅台', 'SSE', '2001-08-27')
        """
    )
    duckdb_store.write_query(
        """
        INSERT INTO price_daily_raw (stock_code, trade_date, close)
        VALUES ('600519', '2025-01-02', 10.0)
        """
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = StaticAdapterManager(
        {
            "raw": _result([{"trade_date": "2026-01-02", "close": 20.0}]),
            "qfq": _result([], error="qfq unavailable"),
        }
    )
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer._batch_id = "test-batch"

    report = initializer._fetch_daily_prices(years=1)

    prices = duckdb_store.read_query(
        "SELECT trade_date, close FROM price_daily_raw WHERE stock_code = '600519'"
    )
    retry = sqlite_store.query(
        "SELECT data_type, error, extra_json FROM retry_list WHERE stock_code = '600519'"
    )
    assert report["failed"] == 1
    assert len(prices) == 1
    assert str(prices[0]["trade_date"]) == "2025-01-02"
    assert retry == [
        {
            "data_type": "price_daily",
            "error": "qfq unavailable",
            "extra_json": '{"adjust": "qfq"}',
        }
    ]


def test_financial_completeness_gate_rejects_shell_rows() -> None:
    assert DataInitializer._financial_row_is_complete(
        "balance_sheet",
        {"TOTAL_ASSETS": 1000, "TOTAL_LIABILITIES": 300, "TOTAL_EQUITY": 700},
    )
    assert not DataInitializer._financial_row_is_complete(
        "balance_sheet",
        {"debt_ratio": 0.3, "current_ratio": 2.0},
    )
    assert not DataInitializer._financial_row_is_complete(
        "income_statement",
        {"PARENT_NETPROFIT": 100, "BASIC_EPS": 1.0},
    )
    assert DataInitializer._financial_row_is_complete(
        "balance_sheet",
        {
            "total_assets": 1000,
            "total_liabilities": 300,
            "total_equity_parent": 700,
        },
    )
    assert DataInitializer._financial_row_is_complete(
        "income_statement",
        {"TOTAL_OPERATE_INCOME": 1000, "PARENT_NETPROFIT": 100},
    )


def test_initializer_rejects_financial_shell_rows(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """
        INSERT INTO stock_meta (stock_code, name, exchange, listing_date)
        VALUES ('600519', '贵州茅台', 'SSE', '2001-08-27')
        """
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.adapter_mgr = StaticAdapterManager(
        {
            "balance_sheet": _result(
                [{"REPORT_DATE": "2026-03-31", "debt_ratio": 0.3}]
            ),
            "income_statement": _result([]),
            "cash_flow": _result([]),
        }
    )
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer._batch_id = "test-batch"

    report = initializer._fetch_financial_statements()

    rows = duckdb_store.read_query(
        "SELECT stock_code FROM balance_sheet WHERE stock_code = '600519'"
    )
    missing = sqlite_store.query(
        """
        SELECT field_name, reason_code FROM missing_list
        WHERE stock_code = '600519' AND field_name = 'balance_sheet'
        """
    )
    assert report["balance_sheet"] == 0
    assert report["status"] == "failed"
    assert report["complete_stocks"] == 0
    assert rows == []
    assert missing == [{"field_name": "balance_sheet", "reason_code": "shell_row"}]
