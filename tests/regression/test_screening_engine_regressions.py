from __future__ import annotations

import pytest

import app.core.screening.engine as engine_module
from app.core.screening.engine import (
    ScreeningEngine,
    validate_rule_fields,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite, sql_now

# ─── 1a: overrides VALUES / mixed_report_date NOT IN / WHERE 参数顺序 ───────

def test_run_binds_overrides_before_mixed_codes_and_where(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = Seed(duckdb_store)
    seed.stock(
        "000001", name="Override One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot("000001", "2025-12-31", latest_close=10)
    # 报表原值 100，勘误覆盖 200：`> 150` 只可能命中被覆盖后的值。
    seed.balance("000001", "2025-12-31", total_assets=100)
    # `manual_overrides` 住在 SQLite（与 `Seed` 持有的 DuckDBStore 不是同一个库），
    # 所以走 SQLite 侧工厂。`status` 必须逐字写 `published`：工厂默认 `active`，
    # 而部分唯一索引 `uq_manual_overrides_published` 只认 `published`；
    # `created_at` 用 `sql_now()` 保住原 SQL 的库侧 `DEFAULT CURRENT_TIMESTAMP`。
    SeedSqlite(sqlite_store).override(
        "000001", "total_assets", 200,
        report_date="2025-12-31", reason="verified", status="published",
        created_at=sql_now(),
    )
    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        engine, "_load_mixed_report_date_codes", lambda: ["999999"]
    )

    result = engine.run(
        {
            "conditions": {"logic": "AND", "rules": [
                {"field": "balance.total_assets", "op": ">", "value": 150},
            ]},
            "columns": ["stock_code", "balance.total_assets"],
        },
        min_listing_years=0,
    )

    assert result["results"] == [
        {"stock_code": "000001", "name": "Override One", "balance.total_assets": 200.0}
    ]


# ─── 1b: sort 非 dict 保存拒绝 + 运行不 500 ────────────────────────────────

def test_validate_rule_fields_rejects_non_dict_sort() -> None:
    with pytest.raises(ValueError, match="排序项必须是对象"):
        validate_rule_fields({"sort": ["pe_ttm"]})


def test_engine_rejects_non_dict_sort_with_value_error(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="排序项必须是对象"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}, "sort": ["pe_ttm"]},
            min_listing_years=0,
        )


# ─── 1c: 无排序/排序全无效时回退稳定 ORDER BY stock_code ──────────────────

def test_no_sort_uses_stable_stock_code_order(duckdb_store: DuckDBStore) -> None:
    seed = Seed(duckdb_store)
    # 插入顺序故意与代码顺序相反：回退排序必须是 stock_code，不是插入顺序。
    seed.stock(
        "000002", name="Zed", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.stock(
        "000001", name="Alpha", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot("000002", "2025-12-31", pe_ttm=1)
    seed.snapshot("000001", "2025-12-31", pe_ttm=1)

    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        min_listing_years=0,
    )

    assert [row["stock_code"] for row in result["results"]] == ["000001", "000002"]


def test_build_order_falls_back_to_stock_code(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    assert engine._build_order([]) == "ORDER BY stock_code"
    assert engine._build_order([{"field": "not_known", "direction": "desc"}]) == "ORDER BY stock_code"


# ─── 1d: min_listing_years 边界 ────────────────────────────────────────────

@pytest.mark.parametrize("bad_years", [-1, 101, 10**9])
def test_min_listing_years_out_of_range_is_rejected(
    duckdb_store: DuckDBStore, bad_years: int,
) -> None:
    with pytest.raises(ValueError, match="min_listing_years 必须在 0-100 之间"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}},
            min_listing_years=bad_years,
        )


@pytest.mark.parametrize("good_years", [0, 100])
def test_min_listing_years_boundaries_are_accepted(
    duckdb_store: DuckDBStore, good_years: int,
) -> None:
    result = ScreeningEngine(duck=duckdb_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        min_listing_years=good_years,
    )
    assert result["total"] == 0


# ─── 1e: 未知 columns 不再静默丢弃 ─────────────────────────────────────────

def test_build_select_rejects_unknown_columns(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    with pytest.raises(ValueError, match="未知结果字段"):
        engine._build_select(["stock_code", "does_not_exist"])


def test_run_rejects_unknown_result_columns(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="未知结果字段"):
        ScreeningEngine(duck=duckdb_store).run(
            {"conditions": {"logic": "AND", "rules": []}, "columns": ["stock_code", "bogus"]},
            min_listing_years=0,
        )


# ─── 1f: generate_entry_explanation 只解释实际命中项 ───────────────────────

def test_explanation_or_mentions_only_the_matched_branch(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    explanation = engine.generate_entry_explanation(
        {"pe_ttm": 10.0, "roe": 0.2},
        {"logic": "OR", "rules": [
            {"field": "pe_ttm", "op": ">", "value": 100},
            {"field": "roe", "op": ">", "value": 0.1},
        ]},
    )

    assert "roe" in explanation
    assert "pe_ttm" not in explanation


def test_explanation_right_field_uses_both_actual_values(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    explanation = engine.generate_entry_explanation(
        {"pe_ttm": 10.0, "pb_mrq": 5.0},
        {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "right_field": "pb_mrq"},
        ]},
    )

    assert "pe_ttm=10.0000" in explanation
    assert "pb_mrq=5.0000" in explanation


def test_explanation_missing_field_is_conservative_without_fake_actual(
    duckdb_store: DuckDBStore,
) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    explanation = engine.generate_entry_explanation(
        {"stock_code": "000001"},
        {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "value": 0},
        ]},
    )

    assert "未保留该字段实际值" in explanation
    assert "实际:" not in explanation


def test_explanation_unmatched_or_branch_is_not_listed(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    explanation = engine.generate_entry_explanation(
        {"pe_ttm": 10.0, "dividend_yield": 0.02},
        {"logic": "AND", "rules": [
            {
                "logic": "OR",
                "rules": [
                    {"field": "pe_ttm", "op": "<", "value": 1},
                    {"field": "dividend_yield", "op": ">", "value": 0.01},
                ],
            },
        ]},
    )

    assert "dividend_yield" in explanation
    assert "pe_ttm" not in explanation


# ─── 1g: 嵌套深度最多 3 层（与前端 max-depth=3 对齐）─────────────────────

def _group(*rules: dict) -> dict:
    return {"logic": "AND", "rules": list(rules)}


def test_build_where_accepts_three_group_levels_and_rejects_four(
    duckdb_store: DuckDBStore,
) -> None:
    engine = ScreeningEngine(duck=duckdb_store)  # 仅测 WHERE 构建，不执行 SQL
    leaf = {"field": "pe_ttm", "op": ">", "value": 0}
    three = _group(_group(_group(leaf)))

    where, params = engine._build_where(three)
    assert "pe_ttm" in where
    assert params == [0]

    four = _group(_group(_group(_group(leaf))))
    with pytest.raises(ValueError, match="规则嵌套超过3层限制"):
        engine._build_where(four)


def test_validate_rule_fields_aligns_depth_limit_with_engine() -> None:
    leaf = {"field": "pe_ttm", "op": ">", "value": 0}
    validate_rule_fields({"conditions": _group(_group(_group(leaf)))})
    with pytest.raises(ValueError, match="规则嵌套超过3层限制"):
        validate_rule_fields({"conditions": _group(_group(_group(_group(leaf))))})


# ─── P3: statement overrides 只加载本次参与股票/报告期，并有上限 ───────────

def test_published_statement_overrides_filtered_to_participating_pairs(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE", is_listed=True)
    seed.stock("000002", name="Two", exchange="SZSE", is_listed=True)
    # 000001 最新期 2025-12-31、000002 最新期 2025-09-30：只有「参与本期」的
    # 勘误才准加载，000001 的 2025-09-30 是过期期 → 必须被过滤掉。
    seed.snapshot("000001", "2025-12-31")
    seed.snapshot("000002", "2025-09-30")
    # 三行覆盖「参与本期 / 过期期 / 他人」三种组合，必须同进同退 → `join()` 进调用点事务。
    with sqlite_store.transaction() as conn:
        overrides = SeedSqlite(sqlite_store).join(conn)
        overrides.override(
            "000001", "total_assets", 200,
            report_date="2025-12-31", reason="relevant", status="published",
            created_at=sql_now(),
        )
        overrides.override(
            "000001", "total_assets", 999,
            report_date="2025-09-30", reason="stale", status="published",
            created_at=sql_now(),
        )
        overrides.override(
            "000002", "total_assets", 300,
            report_date="2025-09-30", reason="other", status="published",
            created_at=sql_now(),
        )

    overrides = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)._published_statement_overrides(
        "m.is_listed IS TRUE", []
    )

    assert overrides == [
        ("000001", "2025-12-31", "total_assets", 200.0),
        ("000002", "2025-09-30", "total_assets", 300.0),
    ]


def test_published_statement_overrides_exceeding_cap_raises(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = Seed(duckdb_store)
    seed.stock("000001", name="One", exchange="SZSE", is_listed=True)
    seed.snapshot("000001", "2025-12-31")
    # 两行都参与 → 造「超上限」场景；必须同进同退 → `join()` 进调用点事务。
    with sqlite_store.transaction() as conn:
        overrides = SeedSqlite(sqlite_store).join(conn)
        overrides.override(
            "000001", "total_assets", 200,
            report_date="2025-12-31", reason="one", status="published",
            created_at=sql_now(),
        )
        overrides.override(
            "000001", "revenue", 300,
            report_date="2025-12-31", reason="two", status="published",
            created_at=sql_now(),
        )
    monkeypatch.setattr(engine_module, "MAX_OVERRIDE_VALUES", 1)

    with pytest.raises(ValueError, match="overrides exceed"):
        ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)._published_statement_overrides(
            "m.is_listed IS TRUE", []
        )
