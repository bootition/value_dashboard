from __future__ import annotations

import pytest

from app.core.dsl.codegen import CodeGen
from app.core.dsl.engine import DSLEngine
from app.core.dsl.parser import parse
from app.core.indicators.calculator import IndicatorCalculator
from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _calculator(duck: DuckDBStore, sqlite: SQLiteStore) -> IndicatorCalculator:
    return IndicatorCalculator(duck=duck, sqlite=sqlite)


@pytest.fixture
def seed_trading_dates(sqlite_store: SQLiteStore):
    sqlite_store.execute("CREATE TABLE IF NOT EXISTS trading_dates (trade_date TEXT PRIMARY KEY)")

    def seed(*dates: str) -> None:
        with sqlite_store.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO trading_dates (trade_date) VALUES (?)",
                [(date,) for date in dates],
            )

    return seed


def test_p0_3_red_nearest_prior_balance_green_same_period_prior_year(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query("""
        INSERT INTO balance_sheet
            (stock_code, report_date, total_assets, total_equity, total_equity_parent)
        VALUES
            ('600519', '2024-12-31', 1000, 500, 500),
            ('600519', '2025-09-30', 2000, 1000, 1000)
    """)
    metrics = _calculator(duckdb_store, sqlite_store)._calc_profitability(
        {'parent_net_profit': 100.0, 'net_profit': 200.0},
        {'report_date': '2025-12-31', 'total_assets': 3000.0, 'total_equity': 1500.0},
        '600519',
    )

    assert metrics['roe'] == pytest.approx(0.1)
    assert metrics['roa'] == pytest.approx(0.1)
    missing_prior = _calculator(duckdb_store, sqlite_store)._calc_profitability(
        {'parent_net_profit': 100.0, 'net_profit': 200.0},
        {'report_date': '2026-12-31', 'total_assets': 3000.0, 'total_equity': 1500.0},
        '600519',
    )
    assert missing_prior['roe'] is None
    assert missing_prior['roa'] is None


def test_p0_3_red_raw_and_gap_compaction_green_qfq_contiguous_metrics(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, seed_trading_dates,
) -> None:
    seed_trading_dates('2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08')
    with duckdb_store.write_connection() as connection:
        connection.executemany(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?)",
            [
                ('600519', '2025-01-02', 100.0), ('600519', '2025-01-03', 50.0),
                ('600519', '2025-01-06', 1.0), ('600519', '2025-01-07', 1.0),
                ('600519', '2025-01-08', 1.0),
            ],
        )
        connection.executemany(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES (?, ?, ?)",
            [
                ('600519', '2025-01-02', 10.0), ('600519', '2025-01-03', 11.0),
                ('600519', '2025-01-06', None), ('600519', '2025-01-07', 12.0),
                ('600519', '2025-01-08', 13.0),
            ],
        )

    metrics = _calculator(duckdb_store, sqlite_store)._calc_technical('600519')

    assert metrics['period_return'] == pytest.approx(1 / 12)
    assert metrics['max_drawdown'] == 0.0
    assert metrics['annualized_volatility'] is None

    duckdb_store.write_query(
        "UPDATE price_daily_qfq SET close = NULL WHERE stock_code = '600519' AND trade_date = '2025-01-08'"
    )
    assert _calculator(duckdb_store, sqlite_store)._calc_technical('600519')['period_return'] is None


def test_p0_3_qfq_metrics_reject_omitted_expected_trading_days(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, seed_trading_dates,
) -> None:
    # The calendar deliberately omits the weekend (Jan 4-5), but includes every
    # trading day between Jan 3 and Jan 10 that the QFQ feed omitted.
    seed_trading_dates(
        '2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07',
        '2025-01-08', '2025-01-09', '2025-01-10',
    )
    with duckdb_store.write_connection() as connection:
        connection.executemany(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?)",
            [('600519', '2025-01-02', 10.0), ('600519', '2025-01-03', 11.0),
             ('600519', '2025-01-10', 12.0)],
        )
        connection.executemany(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES (?, ?, ?)",
            [('600519', '2025-01-02', 10.0), ('600519', '2025-01-03', 11.0),
             ('600519', '2025-01-10', 12.0)],
        )

    metrics = _calculator(duckdb_store, sqlite_store)._calc_technical('600519')

    assert metrics['period_return'] is None
    assert metrics['annualized_volatility'] is None
    assert metrics['max_drawdown'] is None


def test_p0_3_qfq_metrics_accept_calendar_contiguous_weekday_sequence(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, seed_trading_dates,
) -> None:
    seed_trading_dates('2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08')
    with duckdb_store.write_connection() as connection:
        prices = [
            ('600519', '2025-01-02', 10.0), ('600519', '2025-01-03', 11.0),
            ('600519', '2025-01-06', 12.0), ('600519', '2025-01-07', 13.0),
            ('600519', '2025-01-08', 14.0),
        ]
        connection.executemany(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?)", prices,
        )
        connection.executemany(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES (?, ?, ?)", prices,
        )

    metrics = _calculator(duckdb_store, sqlite_store)._calc_technical('600519')

    assert metrics['period_return'] == pytest.approx(0.4)
    assert metrics['max_drawdown'] == 0.0


def test_raw_technical_metrics_do_not_compact_across_calendar_gaps(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, seed_trading_dates,
) -> None:
    seed_trading_dates('2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08')
    with duckdb_store.write_connection() as connection:
        connection.executemany(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close, volume, turnover_rate) VALUES (?, ?, ?, ?, ?)",
            [('600519', '2025-01-02', 10.0, 100.0, 1.0),
             ('600519', '2025-01-03', 11.0, 100.0, 1.0),
             ('600519', '2025-01-08', 12.0, 1000.0, 9.0)],
        )
        connection.executemany(
            "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES (?, ?, ?)",
            [('600519', '2025-01-02', 10.0), ('600519', '2025-01-03', 11.0), ('600519', '2025-01-08', 12.0)],
        )

    metrics = _calculator(duckdb_store, sqlite_store)._calc_technical('600519')

    assert metrics['ma5'] is None
    assert metrics['avg_volume'] == 1000.0
    assert metrics['turnover_rate'] == 9.0


def test_p0_3_red_wall_clock_dividends_green_snapshot_as_of(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query("""
        INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
        VALUES
            ('600519', '2024-06-30', '2024-06-01', 5.0),
            ('600519', '2025-11-30', '2025-11-01', 1.0),
            ('600519', '2025-12-15', '2026-01-01', 9.0)
    """)
    calculator = _calculator(duckdb_store, sqlite_store)

    summary = calculator._get_dividend_summary('600519', '2025-12-31')

    assert summary['latest_dps'] == 1.0
    assert calculator._calc_dividend_yield('600519', 10.0, '2025-12-31') == 0.1
    assert calculator._calc_consecutive_div_years('600519', '2025-12-31') == 2


def test_p0_4_red_identifier_and_parser_green_safe_boolean_runtime(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    with pytest.raises(ValueError, match='expression name'):
        engine.create('score, injected', 'pe_ttm')
    with pytest.raises(ValueError, match='conflicts'):
        engine.create('pe_ttm', 'pb_mrq')

    ast = parse('pe_ttm > 1 AND pb_mrq > 1 OR pe_ttm < 0')
    duckdb_store.write_query(
        "INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm, pb_mrq) "
        "VALUES ('600519', '2025-12-31', 10, 2)"
    )
    value = duckdb_store.read_query(CodeGen().generate_select(ast, stock_code='600519'))[0]['result']

    assert value is True
    draft = engine.create('window_score', 'CAGR(income.revenue, 3)')
    validation = engine.validate('window_score', draft['version'])
    assert validation['valid'] is False
    assert any('historical planner' in error for error in validation['errors'])
    multi_arg = engine.create('bad_rank', 'rank(pe_ttm, pb_mrq)')
    assert engine.validate('bad_rank', multi_arg['version'])['valid'] is False


def test_p0_4_red_right_field_unproven_green_strict_rejects_it(
    duckdb_store: DuckDBStore,
) -> None:
    duckdb_store.write_query("""
        INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
        VALUES ('000001', 'Strict left only', 'SZSE', '2020-01-01', false, false)
    """)
    duckdb_store.write_query("""
        INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm, pb_mrq)
        VALUES ('000001', '2025-12-31', 10, 5)
    """)
    duckdb_store.write_query("""
        INSERT INTO source_audit
            (stock_code, field_name, report_date, value, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
        VALUES ('000001', 'pe_ttm', '2025-12-31', 10, 'test', 'batch', CURRENT_TIMESTAMP, repeat('a', 64), 'strict')
    """)

    result = ScreeningEngine(duck=duckdb_store).run(
        {'conditions': {'logic': 'AND', 'rules': [
            {'field': 'pe_ttm', 'op': '>', 'right_field': 'pb_mrq'},
        ]}},
        min_listing_years=0,
        strict_only=True,
    )

    assert result['strict_fields'] == ['pb_mrq', 'pe_ttm']
    assert result['results'] == []


def test_p0_4_red_independent_latest_rows_green_rejects_mixed_dates(
    duckdb_store: DuckDBStore,
) -> None:
    duckdb_store.write_query("""
        INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
        VALUES ('000001', 'Mixed dates', 'SZSE', '2020-01-01', false, false)
    """)
    duckdb_store.write_query(
        "INSERT INTO indicator_snapshot (stock_code, report_date, pe_ttm) VALUES ('000001', '2025-12-31', 10)"
    )
    duckdb_store.write_query(
        "INSERT INTO balance_sheet (stock_code, report_date, total_assets) VALUES ('000001', '2026-03-31', 100)"
    )

    result = ScreeningEngine(duck=duckdb_store).run(
        {'conditions': {'logic': 'AND', 'rules': []}}, min_listing_years=0,
    )
    # 2026-08-27 口径：混期股票从本次基础池剔除，而不是中止整轮筛选。
    assert result["total"] == 0
    assert result["base_pool_size"] == 0
    assert result["results"] == []
