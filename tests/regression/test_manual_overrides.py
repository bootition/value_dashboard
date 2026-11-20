from __future__ import annotations

import sqlite3

import pytest

from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed


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

    with pytest.raises(sqlite3.IntegrityError):
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
    with store.transaction() as connection:
        # 留（第 3 类·必须写失败的）：这条 INSERT 的**第二次**调用是
        # `test_duplicate_current_published_override_is_rejected` 的被测行为本身 ——
        # 它要撞的是部分唯一索引 `uq_manual_overrides_published`，而且断言写死了
        # `pytest.raises(sqlite3.IntegrityError)`。`SeedSqlite.override()`
        # 把 UNIQUE 失败**规整**成 `DuplicateSqliteSeed`（故意不是 IntegrityError 子类，
        # 见 `tests/support/seed_sqlite.py` 的 `_insert_many`），换成工厂就等于
        # 改掉断言要的异常类型，所以这一处保留内联写法。
        connection.execute(
            """
            INSERT INTO manual_overrides
                (stock_code, field_name, report_date, original_value,
                 override_value, reason, status)
            VALUES ('600519', 'total_assets', '2025-03-31', 1000, ?, 'test', ?)
            """,
            [value, status],
        )
