"""分红融资比指标数据前置回归（2026-08-25）。

覆盖累计现金分红（每股股息 × 除权日生效股本链）与累计股权融资
（IPO+增发+配股）两个快照输入字段，以及 FundingUpdater 有界续传
必须作用在“未覆盖子集”而非全市场前缀的断点修复。
"""

from __future__ import annotations

import pytest

from app.core.dsl.engine import DSLEngine
from app.core.funding import FundingUpdater
from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _seed_stock(duck: DuckDBStore, code: str = "000001") -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES (?, ?, 'SZSE', true)",
        [code, code],
    )


def _seed_share_chain(duck: DuckDBStore, code: str = "000001") -> None:
    duck.write_query(
        """INSERT INTO share_capital_history
           (stock_code, effective_date, total_shares, is_anchor, verified, source, raw_hash, batch_id)
           VALUES (?, '2020-01-01', 100.0, true, true, 'fixture', ?, 'batch-1'),
                  (?, '2023-01-01', 200.0, true, true, 'fixture', ?, 'batch-1')""",
        [code, "0" * 64, code, "0" * 64],
    )


def _seed_dividends(duck: DuckDBStore, code: str = "000001") -> None:
    duck.write_query(
        """INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES (?, '2021-06-01', '2021-05-20', 1.0),
                  (?, '2024-06-01', '2024-05-20', 2.0)""",
        [code, code],
    )


def test_cumulative_dividend_amount_uses_share_capital_as_of(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_share_chain(duckdb_store)
    _seed_dividends(duckdb_store)

    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    amount = calc._get_cumulative_dividend_amount("000001")

    # 2021 年按 100 股、2024 年按 200 股折算
    assert amount == pytest.approx(100.0 * 1.0 + 200.0 * 2.0)


def test_cumulative_dividend_amount_fails_closed_when_share_chain_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_dividends(duckdb_store)  # 没有股本链

    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_dividend_amount("000001") is None


def test_cumulative_dividend_amount_zero_when_no_dividends(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_dividend_amount("999999") == 0.0


def test_cumulative_financing_amount_sums_ipo_placement_and_rights(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO funding_events
           (stock_code, event_type, list_date, issue_price, issue_shares,
            raise_funds, raise_funds_net, derived, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES
            ('000001', 'ipo', '1991-04-03', 40.0, 675000.0, NULL, 27000000.0, false, 'cninfo_funding', CURRENT_TIMESTAMP, ?, 'approximate', 'b1'),
            ('000001', 'a_placement', '2010-09-16', 18.26, 379580000.0, 6931130800.0, NULL, true, 'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'b1'),
            ('000001', 'rights', '1993-05-24', 16.0, 20205000.0, 323280000.0, NULL, false, 'eastmoney_f10', CURRENT_TIMESTAMP, ?, 'approximate', 'b1') """,
        ["0" * 64, "0" * 64, "0" * 64],
    )

    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    amount = calc._get_cumulative_financing_amount("000001")

    assert amount == pytest.approx(27000000.0 + 6931130800.0 + 323280000.0)


def test_cumulative_financing_amount_none_when_uncovered_or_missing_amount(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    # 尚未采集：无事件 → 不得当作 0
    assert calc._get_cumulative_financing_amount("000001") is None

    # 已覆盖但某事件金额缺失 → 整值 fail-closed
    duckdb_store.write_query(
        """INSERT INTO funding_events
           (stock_code, event_type, list_date, issue_price, issue_shares,
            raise_funds, raise_funds_net, derived, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES
            ('000002', 'ipo', '1991-04-03', NULL, NULL, NULL, NULL, false, 'cninfo_funding', CURRENT_TIMESTAMP, ?, 'missing', 'b2')""",
        ["0" * 64],
    )
    assert calc._get_cumulative_financing_amount("000002") is None


def test_funding_update_all_advances_past_covered_prefix(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """有界续传断点：前 N 只已覆盖时，max_stocks 必须从未覆盖子集继续推进。"""
    _seed_stock(duckdb_store, "000001")
    _seed_stock(duckdb_store, "000002")
    duckdb_store.write_query(
        """INSERT INTO funding_events
           (stock_code, event_type, list_date, raise_funds, derived, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('000001', 'ipo', '1991-04-03', 1.0, false, 'cninfo_funding', CURRENT_TIMESTAMP, ?, 'approximate', 'b3')""",
        ["0" * 64],
    )

    class _RecordingAdapter:
        def __init__(self) -> None:
            self.codes: list[str] = []

        def fetch(self, request):
            from datetime import UTC, datetime

            from app.core.adapters.base import FetchResult, SourceMetadata

            self.codes.append(request.stock_codes[0])
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="cninfo_funding",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash="0" * 64,
                    confidence="missing",
                ),
            )

    adapter = _RecordingAdapter()
    updater = FundingUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter=adapter)
    report = updater.update_all(max_stocks=1)

    assert report["status"] == "success", report
    assert list(dict.fromkeys(adapter.codes)) == ["000002"], "必须跳过已覆盖前缀并推进到下一只未覆盖股票"


def test_dsl_can_reference_dividend_financing_inputs(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    engine = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    engine.create(
        "dividend_financing_ratio",
        "cumulative_dividend_amount / cumulative_financing_amount",
        "广义分红（现金分红口径，回购注销待补）/融资",
        "higher_is_better",
    )
    validated = engine.validate("dividend_financing_ratio", 1)
    assert validated["valid"] is True, validated
    assert validated["unit"] == "ratio"


def test_screening_executes_published_dividend_financing_ratio(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from app.core.screening.engine import ScreeningEngine

    duckdb_store.write_query(
        """INSERT INTO stock_meta
           (stock_code, name, exchange, listing_date, is_st, is_suspended, is_listed)
           VALUES
            ('000001', 'One', 'SZSE', '2020-01-01', false, false, true),
            ('000002', 'Two', 'SZSE', '2020-01-01', false, false, true)"""
    )
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
           (stock_code, report_date, cumulative_dividend_amount, cumulative_financing_amount)
           VALUES
            ('000001', '2025-12-31', 200.0, 100.0),
            ('000002', '2025-12-31', 50.0, 100.0)"""
    )
    content_hash = "divfin-hash"
    sqlite_store.execute(
        """INSERT INTO dsl_expressions
           (name, version, expression_text, status, description, direction, historical_capable, content_hash)
           VALUES (?, ?, ?, 'published', ?, ?, ?, ?)""",
        [
            "dividend_financing_ratio", 1,
            "cumulative_dividend_amount / cumulative_financing_amount",
            "分红融资比", "higher_is_better", False, content_hash,
        ],
    )

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    rule = {
        "conditions": {
            "logic": "AND",
            "rules": [{"field": "dividend_financing_ratio", "op": ">", "value": 1}],
        },
        "columns": ["stock_code", "dividend_financing_ratio"],
    }
    result = engine.run(
        rule,
        include_st=True,
        include_suspended=True,
        min_listing_years=0,
        locked_indicators={"dividend_financing_ratio": {"version": 1, "content_hash": content_hash}},
    )

    assert result["total"] == 1
    assert result["results"][0]["stock_code"] == "000001"
    assert result["results"][0]["dividend_financing_ratio"] == 2.0


def test_cumulative_dividend_includes_buyback_amount(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    _seed_share_chain(duckdb_store)
    _seed_dividends(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO buyback_events
           (stock_code, start_date, announce_date, buyback_shares,
            buyback_amount, progress, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('000001', '2024-01-01', '2024-12-01', 100.0, 1234.5,
                   '实施中', 'eastmoney_repurchase', CURRENT_TIMESTAMP, ?, 'approximate', 'bb1')""",
        ["0" * 64],
    )
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_dividend_amount("000001") == pytest.approx(
        100.0 * 1.0 + 200.0 * 2.0 + 1234.5
    )


def test_buyback_updater_atomic_replace(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    from datetime import UTC, datetime

    from app.core.adapters.base import FetchResult, SourceMetadata
    from app.core.buyback import BuybackUpdater

    class _FakeBuybackAdapter:
        def __init__(self, rows) -> None:
            self.rows = rows

        def fetch(self, request):
            raw = b"[]"
            return FetchResult(
                data=self.rows,
                metadata=SourceMetadata(
                    source="eastmoney_repurchase",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash="0" * 64,
                    confidence="approximate",
                ),
                raw_response=raw,
            )

    rows = [
        {
            "stock_code": "000001", "event_type": "buyback",
            "start_date": "2024-01-01", "announce_date": "2024-12-01",
            "buyback_shares": 100.0, "buyback_amount": 1234.5,
            "progress": "实施中", "source": "eastmoney_repurchase",
            "confidence": "approximate",
        }
    ]
    updater = BuybackUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter=_FakeBuybackAdapter(rows))
    report = updater.refresh_all()
    assert report["status"] == "success"
    assert report["event_rows"] == 1
    assert duckdb_store.read_query("SELECT COUNT(*) c FROM buyback_events")[0]["c"] == 1
    status = updater.status_report()
    assert status["stocks"] == 1
    assert status["total_amount"] == 1234.5


def test_cumulative_financing_ignores_buyback_event_type(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    # 即使 funding_events 中混入 buyback 事件，融资侧也只应统计 IPO/增发/配股
    duckdb_store.write_query(
        """INSERT INTO funding_events
           (stock_code, event_type, list_date, issue_price, issue_shares,
            raise_funds, raise_funds_net, derived, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES
            ('000001', 'ipo', '1991-04-03', 1.0, 100.0, NULL, 100.0, false, 'cninfo_funding', CURRENT_TIMESTAMP, ?, 'approximate', 'b1'),
            ('000001', 'buyback', '2024-01-01', NULL, NULL, 999999.0, NULL, false, 'eastmoney_repurchase', CURRENT_TIMESTAMP, ?, 'approximate', 'b1')""",
        ["0" * 64, "0" * 64],
    )
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_financing_amount("000001") == 100.0


def test_cumulative_financing_derives_from_price_times_shares_when_amounts_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO funding_events
           (stock_code, event_type, list_date, issue_price, issue_shares,
            raise_funds, raise_funds_net, derived, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('000001', 'ipo', '1991-04-03', 10.0, 100.0, NULL, NULL, false, 'cninfo_funding', CURRENT_TIMESTAMP, ?, 'approximate', 'b9')""",
        ["0" * 64],
    )
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_financing_amount("000001") == pytest.approx(1000.0)
