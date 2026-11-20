from __future__ import annotations

from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import CURRENT_TIMESTAMP, Seed

# 三张表都走 `tests/support/seed.py` 的按域方法（`stocks` / `snapshots` /
# `source_audit` / `balance`）：交易所/上市日/`is_st` 这类"全批同值"由 `**cols` 表达，
# 逐行不同的名称、行业（`csrc_l1`/`csrc_l2`）、估值与审计值仍逐字写在调用点。


def _seed_screening_data(duck: DuckDBStore) -> None:
    seed = Seed(duck)
    # `exchange` / `listing_date` / `is_st` / `is_suspended` 原 SQL 三行逐字相同 → 全批同值；
    # 逐行不同的是名称与细分行业。
    seed.stocks(
        [
            ("000001", "Strict One", {"csrc_l1": "Finance", "csrc_l2": "Bank"}),
            ("000002", "Approx Two", {"csrc_l1": "Finance", "csrc_l2": "Insurance"}),
            ("000003", "Strict Three", {"csrc_l1": "Industry", "csrc_l2": "Machinery"}),
        ],
        exchange="SZSE",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
    )
    seed.snapshots(
        [
            {"stock_code": "000001", "report_date": "2025-12-31", "pe_ttm": 10, "roe": 0.20},
            {"stock_code": "000002", "report_date": "2025-12-31", "pe_ttm": 20, "roe": 0.10},
            {"stock_code": "000003", "report_date": "2025-12-31", "pe_ttm": 30, "roe": 0.30},
        ]
    )
    # 审计行逐行给：`confidence`（strict/approximate）与批次/摘要三行都不同，
    # 没有"全批同值"可收敛。`fetch_time` 原 SQL 是 `CURRENT_TIMESTAMP` → 照迁移守则
    # 传库侧哨兵（固定字面量会让"这条是刚写的"静默变成过去时刻）。
    seed.source_audit(
        "000001", "pe_ttm", report_date="2025-12-31", value=10, source="test",
        fetch_batch_id="a", fetch_time=CURRENT_TIMESTAMP, raw_response_hash="a" * 64,
        confidence="strict",
    )
    seed.source_audit(
        "000002", "pe_ttm", report_date="2025-12-31", value=20, source="test",
        fetch_batch_id="b", fetch_time=CURRENT_TIMESTAMP, raw_response_hash="b" * 64,
        confidence="approximate",
    )
    seed.source_audit(
        "000003", "pe_ttm", report_date="2025-12-31", value=30, source="test",
        fetch_batch_id="c", fetch_time=CURRENT_TIMESTAMP, raw_response_hash="c" * 64,
        confidence="strict",
    )


def test_strict_only_requires_latest_strict_field_audit(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    rule = {
        "conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]},
        "sort": [{"field": "pe_ttm", "direction": "asc"}],
        "columns": ["stock_code", "pe_ttm"],
    }

    permissive = engine.run(rule, min_listing_years=0)
    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert [row["stock_code"] for row in permissive["results"]] == ["000001", "000002", "000003"]
    assert [row["stock_code"] for row in strict["results"]] == ["000001", "000003"]
    assert strict["strict_fields"] == ["pe_ttm"]


def test_strict_only_rejects_audited_value_that_differs_from_snapshot(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    # 留（第 6 类·库侧相对时间）：`fetch_time` 要**比上面那三条审计晚**
    # （`CURRENT_TIMESTAMP + INTERVAL '1 second'`），"谁是最新一条审计"正是本用例的
    # 论点；`sql_now()` 只收 `datetime('now', '±N 单位')`，表达不出
    # `CURRENT_TIMESTAMP + INTERVAL` 这种"相对另一列的库侧增量"。
    duckdb_store.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
           VALUES ('000001', 'pe_ttm', '2025-12-31', 9, 'test', 'new', CURRENT_TIMESTAMP + INTERVAL '1 second', repeat('d', 64), 'strict')"""
    )

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">", "value": 0}]}},
        min_listing_years=0,
        strict_only=True,
    )

    assert "000001" not in [row["stock_code"] for row in result["results"]]


def test_rank_fields_are_generated_for_sort_and_sw_levels(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    result = ScreeningEngine(duck=duckdb_store).run(
        {
            "conditions": {"logic": "AND", "rules": []},
            "sort": [{"field": "pe_ttm_sw2_rank", "direction": "asc"}],
            "columns": [
                "stock_code", "pe_ttm_market_rank", "pe_ttm_industry_rank",
                "pe_ttm_sw1_rank", "pe_ttm_sw2_rank",
            ],
        },
        min_listing_years=0,
    )

    assert result["total"] == 3
    by_code = {row["stock_code"]: row for row in result["results"]}
    assert by_code["000001"]["pe_ttm_market_rank"] == 1
    # sw1_* 是 industry_*（均按 csrc_l1 分区）的历史别名：为兼容已保存
    # 规则仍参与计算，但 /api/screening/indicators 不再暴露这两个后缀。
    assert by_code["000001"]["pe_ttm_sw1_rank"] == by_code["000001"]["pe_ttm_industry_rank"]
    assert by_code["000001"]["pe_ttm_sw2_rank"] == 1


def test_normalized_statement_fields_support_filter_columns_and_ranks(duckdb_store: DuckDBStore) -> None:
    _seed_screening_data(duckdb_store)
    seed = Seed(duckdb_store)
    # 三行的差别只有 `total_assets` 一个数 —— 逐个逐字传，别把 100/200/300 藏进工厂。
    seed.balance("000001", "2025-12-31", total_assets=100)
    seed.balance("000002", "2025-12-31", total_assets=200)
    seed.balance("000003", "2025-12-31", total_assets=300)

    result = ScreeningEngine(duck=duckdb_store).run(
        {
            "conditions": {"logic": "AND", "rules": [
                {"field": "balance.total_assets", "op": ">=", "value": 200},
            ]},
            "sort": [{"field": "balance.total_assets", "direction": "desc"}],
            "columns": ["stock_code", "balance.total_assets", "balance.total_assets_market_rank"],
        },
        min_listing_years=0,
    )

    assert result["results"] == [
        {"stock_code": "000003", "name": "Strict Three", "balance.total_assets": 300.0,
         "balance.total_assets_market_rank": 3},
        {"stock_code": "000002", "name": "Approx Two", "balance.total_assets": 200.0,
         "balance.total_assets_market_rank": 2},
    ]
