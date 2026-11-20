from __future__ import annotations

import pytest

from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import Seed


@pytest.mark.parametrize(
    "conditions",
    [
        {"logic": "OR 1=1 --", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
        {"logic": "AND", "rules": [{"field": "unknown; DROP TABLE stock_meta", "op": ">", "value": 0}]},
        {"logic": "AND", "rules": [{"field": "pe_ttm", "op": "bogus", "value": 0}]},
        {"logic": "AND", "rules": [{"field": "pe_ttm", "op": "between", "value": [0]}]},
    ],
)
def test_invalid_screening_rules_fail_closed(duckdb_store: DuckDBStore, conditions: dict) -> None:
    with pytest.raises(ValueError):
        ScreeningEngine(duck=duckdb_store).run({"conditions": conditions}, min_listing_years=0)


def test_default_pool_rejects_unknown_suspension_status(duckdb_store: DuckDBStore) -> None:
    # `stock_meta` → `Seed.stock()`；`is_suspended=None` 是**故意**的
    # （"停牌状态未知"正是本用例要拒绝的基础池元数据），所以显式写 None。
    Seed(duckdb_store).stock(
        "000001", name="Unknown status", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=None,
    )

    with pytest.raises(ValueError, match="base pool metadata is incomplete"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}}, min_listing_years=0
        )


def test_historical_unknown_metadata_does_not_block_current_pool(duckdb_store: DuckDBStore) -> None:
    # 两行 `stock_meta` → `Seed.stocks()` 批量（一次事务、一次 executemany）；
    # `is_listed` 逐行不同 → 第三项"该行独有的列"。
    Seed(duckdb_store).stocks([
        ("000001", "Current", {"is_listed": True}),
        ("000002", "Historical", {"is_listed": False}),
    ])

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        include_st=True,
        include_suspended=True,
        min_listing_years=0,
    )

    assert result["base_pool_size"] == 1


def test_listing_age_rejects_unknown_current_listing_date(duckdb_store: DuckDBStore) -> None:
    # `stock_meta` → `Seed.stock()`；`listing_date` **故意不写**（NULL = 上市日未知）。
    Seed(duckdb_store).stock(
        "000001", name="Unknown date", exchange="SZSE", is_st=False, is_suspended=False,
    )

    with pytest.raises(ValueError, match="base pool metadata is incomplete"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}}, min_listing_years=1
        )


def test_condition_can_compare_two_known_numeric_fields(duckdb_store: DuckDBStore) -> None:
    seed = Seed(duckdb_store)
    seed.stock(
        "000001", name="Compare", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot("000001", "2025-12-31", pe_ttm=10, pb_mrq=5)

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "right_field": "pb_mrq"},
        ]}},
        min_listing_years=0,
    )

    assert result["total"] == 1


def test_condition_rejects_metadata_to_metric_comparison(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="不兼容"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": [
                {"field": "name", "op": "=", "right_field": "pe_ttm"},
            ]}},
            min_listing_years=0,
        )


def test_condition_rejects_resource_exhausting_in_list(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)

    with pytest.raises(ValueError, match="最多允许"):
        engine._build_condition({"field": "pe_ttm", "op": "in", "value": list(range(1001))})
