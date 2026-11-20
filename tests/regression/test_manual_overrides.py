from __future__ import annotations

import pytest

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed
from tests.support.seed_sqlite import DuplicateSqliteSeed, SeedSqlite


def test_active_override_does_not_change_financials(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_financials(duckdb_store)
    _insert_override(sqlite_store, status="active", value=2000.0)

    financials = IndicatorCalculator(
        duck=duckdb_store,
        sqlite=sqlite_store,
    )._get_latest_financials("600519")

    assert financials["total_assets"] == 1000.0


def test_published_override_changes_financials(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_financials(duckdb_store)
    _insert_override(sqlite_store, status="published", value=2000.0)

    financials = IndicatorCalculator(
        duck=duckdb_store,
        sqlite=sqlite_store,
    )._get_latest_financials("600519")

    assert financials["total_assets"] == 2000.0


def test_duplicate_current_published_override_is_rejected(
    sqlite_store: SQLiteStore,
) -> None:
    _insert_override(sqlite_store, status="published", value=2000.0)

    # 工厂把部分唯一索引 `uq_manual_overrides_published` 的失败**规整**成
    # `DuplicateSqliteSeed`（不是 sqlite3.IntegrityError 子类）：这里要的语义是
    # "第二条 published 被拒"，而不是"sqlite3 的哪一种异常" —— 分类由
    # `test_seed_sqlite_factory.test_active_overrides_coexist_while_published_is_unique`
    # 钉住（含批量 rows：两条 published 整批回滚）。
    with pytest.raises(DuplicateSqliteSeed):
        _insert_override(sqlite_store, status="published", value=3000.0)


def _insert_financials(store: DuckDBStore) -> None:
    with store.write_connection() as connection:
        seed = Seed(store).join(connection)
        # 1000 / 300 / 700、500 / 100、50 都是"覆写前后对比"的基准值，逐字给。
        seed.balance(
            "600519", "2025-03-31", total_assets=1000, total_liabilities=300, total_equity=700
        )
        seed.income("600519", "2025-03-31", revenue=500, parent_net_profit=100)
        # P0-4/5: 最新完整期 = 三表核心字段齐备；快照计算只取完整期
        seed.cash_flow("600519", "2025-03-31", cf_from_operating=50)


def _insert_override(store: SQLiteStore, status: str, value: float) -> None:
    # 业务值（1000 / 'test' / 2025-03-31 / status）逐字给；工厂只补 NOT NULL 的
    # 形状与确定性时间戳。第二次 published 调用由工厂抛 `DuplicateSqliteSeed`。
    SeedSqlite(store).override(
        "600519",
        "total_assets",
        value,
        reason="test",
        report_date="2025-03-31",
        original_value=1000,
        status=status,
    )
