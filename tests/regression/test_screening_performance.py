"""PRD §19.1 筛选性能验收的可复现隔离基准（发布级红队 P1）。

正式库全市场性能验收脚本（scripts/screening_performance_acceptance.py +
scripts/create_performance_fixture.py）面向目标主机正式 profile；
本测试在 S1 隔离 profile 内构造 5000+ 股票合成夹具，预热后连续运行
10 次，断言 9/10 小于 5 秒（PRD §19.1 合同的可重复执行形态）。
"""

from __future__ import annotations

import hashlib
import time
from datetime import date, timedelta

from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

COMPOSITE_NAME = "performance_value_blend"
COMPOSITE_EXPRESSION = "pe_ttm + pb_mrq"
TARGET_STOCKS = 5000
THRESHOLD_MS = 5000
WARMUP_RUNS = 1
MEASURED_RUNS = 10


def _rule() -> dict:
    """主路径规则：只由 columns 驱动展示列。

    2026-11-19 `a0bc1e3`（reports/143 §10）起"结果排序"整体移出筛选页，
    新品规则 `rule_json` 不再产出 `sort`；`41181e1` 删掉了前端残留的排序入参。
    性能基准测的是这条**当前**路径，所以这里不再构造 `sort`。
    老规则带 `sort` 的兼容性单独由 `test_legacy_rule_with_sort_still_runs` 覆盖。
    """
    return {
        "conditions": {
            "logic": "AND",
            "rules": (
                [{"field": "pe_ttm", "op": ">", "value": 0}]
                + [{"field": "pe_ttm", "op": "is_not_null"} for _ in range(17)]
                + [
                    {"field": COMPOSITE_NAME, "op": "is_not_null"},
                    {"field": "pe_ttm_industry_rank", "op": "<=", "value": TARGET_STOCKS},
                ]
            ),
        },
        "columns": [
            "stock_code", "name", "pe_ttm", "roe", "debt_ratio",
            COMPOSITE_NAME, "pe_ttm_industry_rank",
        ],
    }


def _seed_synthetic_pool(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    count: int = TARGET_STOCKS,
) -> dict:
    """合成股票 + 快照 + 已发布复合指标（20 条件规则所需）。

    `count` 默认造满 5000 只（性能 SLA 用）；legacy 兼容用例只需少量行，
    避免为一条形状断言重复播种 5000 行。
    """
    industries = ["制造业", "金融业", "房地产业", "信息技术业", "批发零售业"]
    listed_date = (date.today() - timedelta(days=365 * 8)).isoformat()
    snapshot_date = "2026-06-30"
    price_date = "2026-07-31"

    meta_rows: list[tuple] = []
    snapshot_rows: list[tuple] = []
    for i in range(count):
        code = f"{600000 + i:06d}"
        industry = industries[i % len(industries)]
        meta_rows.append((
            code, f"stock-{i}", "SZSE", listed_date, True, False, False,
            industry, f"{industry}-sub", 1_000_000 + i, 800_000 + i,
        ))
        snapshot_rows.append((
            code, snapshot_date, 10.0 + (i % 100) / 10, price_date, 10.0 + (i % 100) / 10,
            10.0 + (i % 100) / 10, 0.05 + (i % 50) / 1000, 0.3 + (i % 50) / 100,
        ))

    with duckdb_store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO stock_meta
               (stock_code, name, exchange, listing_date, is_listed, is_st, is_suspended,
                csrc_l1, csrc_l2, total_shares, circ_shares)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            meta_rows,
        )
        connection.executemany(
            """INSERT INTO indicator_snapshot
               (stock_code, report_date, latest_close, latest_price_date, pe_ttm, pb_mrq,
                roe, debt_ratio, calculated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            snapshot_rows,
        )

    content_hash = hashlib.sha256(COMPOSITE_EXPRESSION.encode("utf-8")).hexdigest()
    with sqlite_store.transaction() as connection:
        connection.execute(
            """INSERT INTO dsl_expressions
               (name, version, expression_text, status, description, direction,
                historical_capable, content_hash)
               VALUES (?, 1, ?, 'published', 'PRD 19.1 synthetic benchmark', 'lower_is_better', 0, ?)""",
            [COMPOSITE_NAME, COMPOSITE_EXPRESSION, content_hash],
        )
    return {"name": COMPOSITE_NAME, "version": 1, "content_hash": content_hash}


def test_screening_performance_sla_on_5000_stock_pool(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    composite = _seed_synthetic_pool(duckdb_store, sqlite_store)

    listed = duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM stock_meta WHERE is_listed IS TRUE"
    )[0]["count"]
    assert listed >= 5000

    rule = _rule()
    assert len(rule["conditions"]["rules"]) == 20

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    locked = {composite["name"]: composite}

    for _ in range(WARMUP_RUNS):
        warmup = engine.run(rule, locked_indicators=locked)
    assert warmup["total"] > 0

    durations: list[int] = []
    for _ in range(MEASURED_RUNS):
        start = time.monotonic()
        result = engine.run(rule, locked_indicators=locked)
        durations.append(int((time.monotonic() - start) * 1000))
        assert result["total"] > 0

    passing = sum(duration <= THRESHOLD_MS for duration in durations)
    assert passing >= 9, (
        f"PRD 19.1 SLA failed: {passing}/{MEASURED_RUNS} runs within "
        f"{THRESHOLD_MS}ms; durations_ms={durations}"
    )


def test_legacy_rule_with_sort_still_runs(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """老 rule_json 带 sort 仍必须能跑通（兼容期口径，reports/143 §10.2）。

    2026-11-19 `a0bc1e3` 之后新品规则不再产出 `sort`，但历史已保存规则里还有；
    引擎必须继续按 `sort` 排序，而不是报错或静默丢行。这条用例是上一条
    （columns 驱动的主路径）的对照：**形状兼容**留在这里，不让它污染主基准。
    """
    _seed_synthetic_pool(duckdb_store, sqlite_store, count=3)
    legacy_rule = {
        "conditions": {
            "logic": "AND",
            "rules": [{"field": "pe_ttm", "op": ">", "value": 0}],
        },
        "sort": [{"field": "pe_ttm", "direction": "desc"}],
        "columns": ["stock_code", "pe_ttm"],
    }

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    result = engine.run(legacy_rule)

    assert result["total"] == 3
    pe_values = [row["pe_ttm"] for row in result["results"]]
    assert pe_values == sorted(pe_values, reverse=True), (
        f"legacy sort 未生效；结果顺序={pe_values}"
    )
