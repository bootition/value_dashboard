"""条件级排名口径（方案 C）回归。

背景（2026-11-16 用户裁定）
--------------------------
过去 `/api/screening/indicators` 为 `RANKABLE_INDICATORS` 里**每个**指标铺 6 条
扁平派生字段：

    {指标}_market_rank / _market_percentile
    {指标}_industry_rank / _industry_percentile
    {指标}_sw2_rank / _sw2_percentile

155 个可排名指标 × 6 = **930 条**。ROCE 一个指标就占 21 条（3 窗口 × 6 + 3 主指标），
用户在下拉里搜 "roce" 命中 21 条，主指标被埋住根本无法选择。

而且它与 DSL 已有的 `rank()/rank_industry()/percentile()/zscore()/normalize()`
（`app/core/dsl/codegen.py` 已实现为窗口函数）是**同一功能的两套实现**，
PRD §11.2 本就把排名定位为 DSL 运算。

方案 C：收成**条件行上的一个下拉**。后端在入口把
`{"field": "pe_ttm", "rank_mode": "market_rank"}` 展开成
`{"field": "pe_ttm_market_rank"}`，复用既有的排名 SQL（不写第二套实现）。

本文件钉死四件事：

1. 展开逻辑正确，且**不改变任何排名语义**（新旧写法结果必须逐只一致）；
2. 保存期校验也走同一份展开逻辑（否则又是"能存不能跑/能跑不能存"）；
3. 非法口径必须显式报错，不能静默降级成原始值；
4. `/indicators` 不再下发 930 条排名派生字段。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.screening.engine import (
    RANK_MODE_LABELS,
    RANK_MODES,
    RANK_SUFFIXES,
    expand_rank_modes,
    validate_rule_fields,
)
from app.core.storage.duckdb_store import DuckDBStore


def test_expand_rank_mode_rewrites_field() -> None:
    """`rank_mode` 展开成字段后缀，且原键被移除。"""
    out = expand_rank_modes({
        "logic": "AND",
        "rules": [
            {"field": "pe_ttm", "op": "<=", "value": 15, "rank_mode": "market_rank"},
        ],
    })
    rule = out["rules"][0]
    assert rule["field"] == "pe_ttm_market_rank"
    assert "rank_mode" not in rule, "展开后必须移除 rank_mode，否则会重复展开"


def test_expand_keeps_conditions_without_rank_mode() -> None:
    """没有 rank_mode 的条件原样保留。"""
    out = expand_rank_modes({
        "logic": "AND",
        "rules": [{"field": "roe", "op": ">", "value": 0.15}],
    })
    assert out["rules"][0] == {"field": "roe", "op": ">", "value": 0.15}


def test_expand_handles_nested_groups() -> None:
    """嵌套条件组里的 rank_mode 也要展开。"""
    out = expand_rank_modes({
        "logic": "AND",
        "rules": [
            {"logic": "OR", "rules": [
                {"field": "pb_mrq", "op": "<", "value": 2, "rank_mode": "industry_percentile"},
            ]},
        ],
    })
    inner = out["rules"][0]["rules"][0]
    assert inner["field"] == "pb_mrq_industry_percentile"


def test_every_rank_mode_expands_to_a_known_suffix() -> None:
    """6 个口径必须各自对应 `RANK_SUFFIXES` 里的真实后缀。

    这条防的是"下拉里能选、后端却拼不出字段"的错位。
    """
    assert set(RANK_MODES) == {
        "market_rank", "market_percentile",
        "industry_rank", "industry_percentile",
        "sw2_rank", "sw2_percentile",
    }
    for mode in RANK_MODES:
        assert f"_{mode}" in RANK_SUFFIXES, f"{mode} 不在 RANK_SUFFIXES 里"
        assert mode in RANK_MODE_LABELS, f"{mode} 缺中文标签（PRD §12.1 要求全中文）"


def test_unknown_rank_mode_is_rejected() -> None:
    """未知口径必须报错，不能静默当成原始值。"""
    with pytest.raises(ValueError, match="未知排名口径"):
        expand_rank_modes({
            "logic": "AND",
            "rules": [{"field": "pe_ttm", "rank_mode": "bogus_mode"}],
        })


def test_double_suffix_is_rejected() -> None:
    """字段已带排名后缀又指定 rank_mode → 报错，避免 `xxx_market_rank_market_rank`。"""
    with pytest.raises(ValueError, match="已带排名后缀"):
        expand_rank_modes({
            "logic": "AND",
            "rules": [{"field": "pe_ttm_market_rank", "rank_mode": "market_rank"}],
        })


def test_validate_rule_fields_accepts_rank_mode() -> None:
    """保存期校验必须接受 rank_mode。

    若不展开，校验看到的是基名 `pe_ttm`（合法）而运行期是
    `pe_ttm_market_rank` —— 会重现"能跑不能存"的老问题。
    """
    validate_rule_fields({
        "conditions": {
            "logic": "AND",
            "rules": [
                {"field": "pe_ttm", "op": "<=", "value": 15, "rank_mode": "market_rank"},
            ],
        },
    })


def test_validate_rule_fields_rejects_unknown_rank_mode() -> None:
    with pytest.raises(ValueError, match="未知排名口径"):
        validate_rule_fields({
            "conditions": {
                "logic": "AND",
                "rules": [{"field": "pe_ttm", "rank_mode": "nope"}],
            },
        })


def test_rank_mode_matches_legacy_suffix_condition(
    duckdb_store: DuckDBStore, sqlite_store: Any,
) -> None:
    """**关键等价性**：条件级写法的结果必须与旧的扁平字段写法逐只一致。

    这是方案 C 能安全替代 930 条派生字段的前提——展开只是入口改写，
    排名 SQL（NULLS LAST、csrc_l1 为 NULL 时行业排名返回 NULL）完全没动。
    """
    from app.core.screening.engine import ScreeningEngine

    # 基础池要求 ST/停牌/上市日期齐全（engine._require_known_pool_status），
    # 缺一列就会整体拒绝运行——与排名逻辑无关，这里补齐。
    duckdb_store.write_query(
        "INSERT INTO stock_meta "
        "(stock_code, name, exchange, is_listed, csrc_l1, is_st, is_suspended, listing_date) "
        "VALUES ('000001','A','SZSE',true,'制造业', false, false, CAST('2000-01-01' AS DATE)), "
        "       ('000002','B','SZSE',true,'制造业', false, false, CAST('2000-01-01' AS DATE)), "
        "       ('000003','C','SZSE',true,'制造业', false, false, CAST('2000-01-01' AS DATE))"
    )
    for code, pe in (("000001", 10.0), ("000002", 20.0), ("000003", 30.0)):
        duckdb_store.write_query(
            """INSERT INTO indicator_snapshot
                   (stock_code, report_date, calculated_at, data_version, pe_ttm)
               VALUES (?, CAST('2025-12-31' AS DATE), now(), 'fixture', ?)""",
            [code, pe],
        )

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    new_rule = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": "<=", "value": 2, "rank_mode": "market_rank"},
        ]},
        "columns": ["stock_code"],
    }
    old_rule = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm_market_rank", "op": "<=", "value": 2},
        ]},
        "columns": ["stock_code"],
    }
    # min_listing_years=0：夹具上市日固定，避免基础池年限过滤干扰排名对比
    new_rows = sorted(r["stock_code"] for r in engine.run(new_rule, min_listing_years=0)["results"])
    old_rows = sorted(r["stock_code"] for r in engine.run(old_rule, min_listing_years=0)["results"])
    assert new_rows == old_rows == ["000001", "000002"], "PE 最低的两只"


def test_indicators_endpoint_stops_emitting_rank_fields() -> None:
    """`/indicators` 不得再下发 930 条排名派生字段。

    只断言源码层面的生成块已移除（真正调用端点需要 Request 对象，
    由 test_screening_api_regressions 覆盖）。
    """
    import inspect

    from app.web.api import screening as screening_api

    source = inspect.getsource(screening_api.list_available_indicators)
    # 只检查**生成块**（遍历 RANKABLE_INDICATORS 铺后缀），
    # 不能简单 grep `market_rank`——注释里说明方案 C 时也会出现这个词。
    assert "for col in sorted(RANKABLE_INDICATORS)" not in source, (
        "list_available_indicators 仍在为每个可排名指标铺排名字段"
    )
    assert "rank_modes" in source, "响应体必须下发 rank_modes 供前端渲染下拉"


# ─── 排序与结果列也支持排名口径（方案 C 的另外两块，2026-11-16）────────────


def test_normalize_columns_accepts_plain_and_rank_objects() -> None:
    """结果列支持纯字段名与 `{field, rank_mode}` 两种写法。"""
    from app.core.screening.engine import normalize_columns

    out = normalize_columns([
        "stock_code",
        "pe_ttm",
        {"field": "pe_ttm", "rank_mode": "market_rank"},
        {"field": "roe", "rank_mode": "sw2_percentile"},
    ])
    assert out == [
        "stock_code",
        "pe_ttm",
        "pe_ttm_market_rank",
        "roe_sw2_percentile",
    ]


def test_normalize_columns_rejects_bad_shapes() -> None:
    from app.core.screening.engine import normalize_columns

    with pytest.raises(ValueError, match="结果列必须是数组"):
        normalize_columns({"field": "pe_ttm"})
    with pytest.raises(ValueError, match="必须是字段名或对象"):
        normalize_columns([123])
    with pytest.raises(ValueError, match="缺少 field"):
        normalize_columns([{"rank_mode": "market_rank"}])


def test_validate_rule_fields_accepts_rank_sort_and_columns() -> None:
    """排序与结果列都带排名口径时，保存期校验必须通过（防"能跑不能存"）。"""
    validate_rule_fields({
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "value": 0},
        ]},
        "sort": [{"field": "pe_ttm", "rank_mode": "market_rank", "direction": "asc"}],
        "columns": [
            "stock_code",
            {"field": "pe_ttm", "rank_mode": "market_rank"},
        ],
    })


def test_rank_mode_works_in_conditions_sort_and_columns(
    duckdb_store: DuckDBStore, sqlite_store: Any,
) -> None:
    """三项能力同时使用：条件筛排名、按排名排序、结果列显示排名。

    这是方案 C 的完整验收：用户既要能"筛"，也要能"看"和"排"。
    """
    from app.core.screening.engine import ScreeningEngine

    duckdb_store.write_query(
        "INSERT INTO stock_meta "
        "(stock_code, name, exchange, is_listed, csrc_l1, is_st, is_suspended, listing_date) "
        "VALUES ('000001','A','SZSE',true,'制造业', false, false, CAST('2000-01-01' AS DATE)), "
        "       ('000002','B','SZSE',true,'制造业', false, false, CAST('2000-01-01' AS DATE)), "
        "       ('000003','C','SZSE',true,'制造业', false, false, CAST('2000-01-01' AS DATE))"
    )
    for code, pe in (("000001", 30.0), ("000002", 10.0), ("000003", 20.0)):
        duckdb_store.write_query(
            """INSERT INTO indicator_snapshot
                   (stock_code, report_date, calculated_at, data_version, pe_ttm)
               VALUES (?, CAST('2025-12-31' AS DATE), now(), 'fixture', ?)""",
            [code, pe],
        )

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    rule = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "value": 0, "rank_mode": "market_rank"},
        ]},
        # 按全市场 PE 排名升序：PE 最低的排第一
        "sort": [{"field": "pe_ttm", "rank_mode": "market_rank", "direction": "asc"}],
        "columns": [
            "stock_code",
            {"field": "pe_ttm", "rank_mode": "market_rank"},
        ],
    }
    rows = engine.run(rule, min_listing_years=0)["results"]

    assert [r["stock_code"] for r in rows] == ["000002", "000003", "000001"]
    assert [r["pe_ttm_market_rank"] for r in rows] == [1, 2, 3]
