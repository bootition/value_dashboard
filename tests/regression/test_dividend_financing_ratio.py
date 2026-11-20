"""分红融资比指标数据前置回归（2026-08-25）。

覆盖累计现金分红（每股股息 × 除权日生效股本链）与累计股权融资
（IPO+增发+配股）两个快照输入字段，以及 FundingUpdater 有界续传
必须作用在“未覆盖子集”而非全市场前缀的断点修复。

播种统一走 `tests/support/seed.py` 的按域工厂：工厂只负责「怎么把行放进去」
（主键、provenance 必填列、日期写法），**金额/股数一律逐字写在调用点上** ——
「放什么」才是这些用例的全部意义。
"""

from __future__ import annotations

import pytest

from app.core.dsl.engine import DSLEngine
from app.core.funding import FundingUpdater
from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed


def _seed_stock(duck: DuckDBStore, code: str = "000001") -> None:
    Seed(duck).stock(code, name=code, exchange="SZSE", is_listed=True)


def _seed_share_chain(duck: DuckDBStore, code: str = "000001") -> None:
    """时点股本链：2020 起 100 股 → 2023 起 200 股（分红按除权日生效的那一档折算）。"""
    Seed(duck).shares(
        code,
        [("2020-01-01", 100.0), ("2023-01-01", 200.0)],
        is_anchor=True,
        verified=True,
        source="fixture",
    )


def _seed_dividends(duck: DuckDBStore, code: str = "000001") -> None:
    """两期现金分红（除权日 + 公告日 + 每股股息逐行不同）。"""
    Seed(duck).dividends(
        code,
        [("2021-06-01", "2021-05-20", 1.0), ("2024-06-01", "2024-05-20", 2.0)],
    )


def test_cumulative_dividend_amount_uses_a_share_circ_shares(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store)
    duckdb_store.write_query(
        "UPDATE stock_meta SET total_shares = 1000000, circ_shares = 200 WHERE stock_code = '000001'"
    )
    _seed_dividends(duckdb_store)

    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    amount = calc._get_cumulative_dividend_amount("000001")

    # A股口径按 circ_shares（A股流通股本）折算，total_shares 中含 H 股也不能混入。
    assert amount == pytest.approx(200.0 * 1.0 + 200.0 * 2.0)


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
    seed = Seed(duckdb_store)
    # `**cols` 只给全批同值，而 raise_funds / raise_funds_net / source / derived
    # 逐行不同 → 用行的第四项（逐行 cols）把三笔事件放进**一次**调用。
    # IPO 只剩募资净额、增发/配股只有募资总额 —— 这个口径差异正是本用例的意义。
    seed.fundings(
        "000001",
        [
            (
                "ipo", "1991-04-03", 675000.0,
                {
                    "issue_price": 40.0, "raise_funds": None,
                    "raise_funds_net": 27000000.0, "derived": False,
                    "source": "cninfo_funding", "confidence": "approximate",
                },
            ),
            (
                "a_placement", "2010-09-16", 379580000.0,
                {
                    "issue_price": 18.26, "raise_funds": 6931130800.0,
                    "raise_funds_net": None, "derived": True,
                    "source": "eastmoney_f10", "confidence": "approximate",
                },
            ),
            (
                "rights", "1993-05-24", 20205000.0,
                {
                    "issue_price": 16.0, "raise_funds": 323280000.0,
                    "raise_funds_net": None, "derived": False,
                    "source": "eastmoney_f10", "confidence": "approximate",
                },
            ),
        ],
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
    Seed(duckdb_store).fundings(
        "000002", [("ipo", "1991-04-03", None)],
        issue_price=None, raise_funds=None, raise_funds_net=None, derived=False,
        source="cninfo_funding", confidence="missing",
    )
    assert calc._get_cumulative_financing_amount("000002") is None


def test_funding_update_all_advances_past_covered_prefix(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """有界续传断点：前 N 只已覆盖时，max_stocks 必须从未覆盖子集继续推进。"""
    _seed_stock(duckdb_store, "000001")
    _seed_stock(duckdb_store, "000002")
    # 「已覆盖」的唯一判据是 funding_events 里有该股一行；1.0 只是占位金额。
    Seed(duckdb_store).fundings(
        "000001", [("ipo", "1991-04-03", None)],
        raise_funds=1.0, derived=False, source="cninfo_funding", confidence="approximate",
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

    seed = Seed(duckdb_store)
    # 两只各持一期快照：分红 200/50、融资都是 100 → 比值 2.0 / 0.5，只有一只过 >1。
    for code, name, dividend in (("000001", "One", 200.0), ("000002", "Two", 50.0)):
        seed.stock(
            code, name=name, exchange="SZSE", listing_date="2020-01-01",
            is_st=False, is_suspended=False, is_listed=True,
        )
        seed.snapshot(
            code, "2025-12-31",
            cumulative_dividend_amount=dividend, cumulative_financing_amount=100.0,
        )
    content_hash = "divfin-hash"
    # 需要 SQLite 侧播种器：dsl_expressions 与 Seed 持有的 DuckDBStore 不同库
    # （见 seed.py 模块 docstring「尚未覆盖」），故保留内联 SQL。
    # 留（第 4 类·多站点缺口）：`content_hash` 由本用例现算 `hashlib.sha256(expression)`
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
    # 回购注销金额进入「广义分红」：已回购金额 1234.5 必须原样加上去。
    Seed(duckdb_store).buyback(
        "000001", "2024-01-01", 100.0, announce_date="2024-12-01",
        buyback_amount=1234.5, progress="实施中",
        source="eastmoney_repurchase", confidence="approximate",
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
    seed = Seed(duckdb_store)
    seed.fundings(
        "000001", [("ipo", "1991-04-03", 100.0)],
        issue_price=1.0, raise_funds=None, raise_funds_net=100.0, derived=False,
        source="cninfo_funding", confidence="approximate",
    )
    seed.fundings(
        "000001", [("buyback", "2024-01-01", None)],
        issue_price=None, raise_funds=999999.0, raise_funds_net=None, derived=False,
        source="eastmoney_repurchase", confidence="approximate",
    )
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_financing_amount("000001") == 100.0


def test_cumulative_financing_derives_from_price_times_shares_when_amounts_missing(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    Seed(duckdb_store).fundings(
        "000001", [("ipo", "1991-04-03", 100.0)],
        issue_price=10.0, raise_funds=None, raise_funds_net=None, derived=False,
        source="cninfo_funding", confidence="approximate",
    )
    calc = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    assert calc._get_cumulative_financing_amount("000001") == pytest.approx(1000.0)


def test_buyback_updater_retains_existing_events_on_empty_source(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """全量替换语义不得在源空响应时清空既有回购事件。"""
    from datetime import UTC, datetime

    from app.core.adapters.base import FetchResult, SourceMetadata
    from app.core.buyback import BuybackUpdater

    Seed(duckdb_store).buyback(
        "000001", "2024-01-01", 100.0, announce_date="2024-12-01",
        buyback_amount=1234.5, progress="实施中",
        source="eastmoney_repurchase", confidence="approximate",
    )

    class EmptyBuybackAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="eastmoney_repurchase",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash="0" * 64,
                    confidence="missing",
                ),
                raw_response=b"[]",
            )

    updater = BuybackUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=EmptyBuybackAdapter(),
    )
    report = updater.refresh_all()

    assert report["status"] == "failed"
    assert report["reason"] == "source_empty"
    assert report["retained"] is True
    rows = duckdb_store.read_query(
        "SELECT stock_code, buyback_amount FROM buyback_events"
    )
    assert rows == [{"stock_code": "000001", "buyback_amount": 1234.5}]
