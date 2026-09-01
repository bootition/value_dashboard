"""P0-4/P0-5 回归: 快照/筛选/发布门禁以"最新完整三表期"为统一判定。

报表存在晚于完整期但缺核心字段的新行 = 数据源未就绪（PRD §7.7），
不阻断；快照期与完整期不一致才是阻断项。readiness 门禁与筛选引擎
必须共用同一判定，消除 "ready=true 但筛选全部失败" 的假阳性。
"""

from __future__ import annotations

from app.core.data_quality import minimum_data_readiness
from app.core.indicators.calculator import IndicatorCalculator
from app.core.screening.engine import ScreeningEngine
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data


def _seed_pool(duckdb_store, sqlite_store) -> None:
    """One listed stock with a complete 2025-12-31 period and published snapshot."""
    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)


def _insert_pending_incomplete_period(duckdb_store) -> None:
    """三表都有 2026-03-31 行，但 balance.total_liabilities 为 NULL（源未就绪）。"""
    duckdb_store.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets, total_liabilities)
           VALUES ('000001', '2026-03-31', 200, NULL)"""
    )
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
           VALUES ('000001', '2026-03-31', 200, 20)"""
    )
    duckdb_store.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES ('000001', '2026-03-31', 2)"""
    )


def _insert_complete_newer_period(duckdb_store) -> None:
    """三表在 2026-03-31 的核心字段齐备（真正的完整期推进）。"""
    duckdb_store.write_query(
        """INSERT INTO balance_sheet
               (stock_code, report_date, total_assets, total_liabilities, total_equity)
           VALUES ('000001', '2026-03-31', 200, 40, 160)"""
    )
    duckdb_store.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
           VALUES ('000001', '2026-03-31', 200, 20)"""
    )
    duckdb_store.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES ('000001', '2026-03-31', 2)"""
    )


def _run_screening(duckdb_store) -> dict:
    return ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        min_listing_years=0,
    )


def test_pending_incomplete_period_does_not_block_screening_or_readiness(
    duckdb_store, sqlite_store,
) -> None:
    """603435 场景: 新期缺字段 → 快照停留在完整期，筛选必须可用。"""
    _seed_pool(duckdb_store, sqlite_store)
    _insert_pending_incomplete_period(duckdb_store)

    result = _run_screening(duckdb_store)

    assert result["total"] == 1
    assert result["results"][0]["stock_code"] == "000001"

    quality = minimum_data_readiness(duckdb_store, sqlite_store)
    assert quality["ready"] is True
    assert quality["missing_counts"].get("pending_financial_period") == 1
    assert "snapshot_period_alignment" not in quality["missing"]
    assert quality["disclosure_missing_counts"]["pending_financial_period"] == 1


def test_snapshot_behind_complete_period_blocks_screening_and_readiness(
    duckdb_store, sqlite_store,
) -> None:
    """完整期已推进但快照未重算 → 门禁阻断、引擎剔除该股并空跑。

    2026-08-27 口径：引擎不再因个别混期股票中止全市场筛选，
    而是把混期代码从本次基础池排除（其他股票照常可用）。
    """
    _seed_pool(duckdb_store, sqlite_store)
    _insert_complete_newer_period(duckdb_store)

    result = _run_screening(duckdb_store)
    assert result["total"] == 0
    assert result["base_pool_size"] == 0
    assert result["results"] == []

    quality = minimum_data_readiness(duckdb_store, sqlite_store)
    assert quality["ready"] is False
    assert quality["missing_counts"]["snapshot_period_alignment"] == 1


def test_readiness_discloses_alignment_stock_codes(duckdb_store, sqlite_store) -> None:
    _seed_pool(duckdb_store, sqlite_store)
    _insert_complete_newer_period(duckdb_store)

    quality = minimum_data_readiness(duckdb_store, sqlite_store)

    assert quality["missing"]["snapshot_period_alignment"] == ["000001"]


def test_compute_all_for_stock_uses_latest_complete_period(
    duckdb_store, sqlite_store,
) -> None:
    """新期缺字段时，指标计算回退到完整期，不混入部分新数据。"""
    _seed_pool(duckdb_store, sqlite_store)
    _insert_pending_incomplete_period(duckdb_store)

    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    result = calculator.compute_all_for_stock("000001")

    assert str(result["report_date"])[:10] == "2025-12-31"


def test_snapshot_publish_proceeds_at_complete_period_despite_pending_rows(
    duckdb_store, sqlite_store,
) -> None:
    """部分新财务不再永久阻断全量快照发布（P0-5）。"""
    _seed_pool(duckdb_store, sqlite_store)
    _insert_pending_incomplete_period(duckdb_store)

    report = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store).compute_snapshot_for_all()

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT report_date FROM indicator_snapshot WHERE stock_code = '000001'"
    )
    assert str(rows[0]["report_date"])[:10] == "2025-12-31"


def test_snapshot_publish_moves_to_new_complete_period(duckdb_store, sqlite_store) -> None:
    """完整期推进后重算 → 快照发布到新完整期，随后筛选可用。"""
    _seed_pool(duckdb_store, sqlite_store)
    _insert_complete_newer_period(duckdb_store)

    report = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store).compute_snapshot_for_all()

    assert report["status"] == "success"
    rows = duckdb_store.read_query(
        "SELECT report_date FROM indicator_snapshot WHERE stock_code = '000001'"
    )
    assert str(rows[0]["report_date"])[:10] == "2026-03-31"

    result = _run_screening(duckdb_store)
    assert result["total"] == 1
