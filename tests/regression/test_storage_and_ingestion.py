from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.backfill import PriceBackfiller
from app.core.config import Config
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import init_duckdb_schema
from app.core.storage.sqlite_store import SQLiteStore
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
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source="akshare_eastmoney",
            fetch_time=datetime.now(timezone.utc),
            raw_response_hash="0" * 64,
            confidence="missing" if error else "approximate",
            error=error,
        ),
    )


def test_old_qfq_schema_is_upgraded_idempotently(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "old.duckdb")
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


def test_qfq_api_supports_schema_before_turnover_rate_migration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "legacy-api.duckdb"
    store = DuckDBStore(db_path)
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
    monkeypatch.setattr(
        Config,
        "_instance",
        Config(
            {
                "database": {
                    "duckdb_path": str(db_path),
                    "sqlite_path": str(tmp_path / "unused.sqlite"),
                }
            }
        ),
    )

    result = asyncio.run(get_kline("600519", adjust="qfq", days=250))

    assert result["count"] == 1
    assert result["candles"][0]["turnover_rate"] is None


def test_stock_refresh_preserves_known_optional_metadata(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """
        INSERT INTO stock_meta
            (stock_code, name, exchange, listing_date, is_st, is_suspended, sw_level1)
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
        SELECT name, listing_date, is_st, is_suspended, sw_level1
        FROM stock_meta WHERE stock_code = '600519'
        """
    )
    assert rows[0]["name"] == "new"
    assert str(rows[0]["listing_date"]) == "2001-08-27"
    assert rows[0]["is_st"] is False
    assert rows[0]["is_suspended"] is True
    assert rows[0]["sw_level1"] == "食品饮料"


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
    assert rows == []
    assert missing == [{"field_name": "balance_sheet", "reason_code": "shell_row"}]
