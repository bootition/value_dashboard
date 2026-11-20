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
from tests.support.seed import CURRENT_TIMESTAMP, Seed


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
    seed = Seed(duckdb_store)
    seed.stocks(
        [("000001", "A"), ("000002", "B"), ("000003", "C")],
        is_listed=True,
        csrc_l1="制造业",
        is_st=False,
        is_suspended=False,
        listing_date="2000-01-01",
    )
    seed.snapshots([
        {
            "stock_code": code,
            "report_date": "2025-12-31",
            # `now()` 就是「库侧此刻」：换哨兵而不是写字面量（迁移守则）。
            "calculated_at": CURRENT_TIMESTAMP,
            "data_version": "fixture",
            "pe_ttm": pe,
        }
        for code, pe in (("000001", 10.0), ("000002", 20.0), ("000003", 30.0))
    ])

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


def test_indicators_endpoint_stops_emitting_rank_fields(
    duckdb_store: DuckDBStore, sqlite_store: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/indicators` 不得再下发 930 条排名派生字段（行为断言，不读源码）。

    拦的事故：有人把"为每个可排名指标铺 6 条扁平派生字段"的生成块加回端点
    ——155 个可排名指标 × 6 = 930 条，ROCE 一个指标占 21 条，主指标被埋在
    下拉中间根本选不到（用户 2026-11-16 的原始反馈）。

    做法：真跑 `list_available_indicators`（fake request 只带 duck/sqlite），
    断言返回的指标名里**没有任何** `_{RANK_MODES}` 后缀的派生字段，
    并且响应体确实下发了 `rank_modes` 供前端渲染条件级下拉。
    替代旧写法 `"for col in sorted(RANKABLE_INDICATORS)" not in getsource(...)`
    ——那既绑死代码落点（换个 for 变量名就假绿），又在函数体搬家时假红。
    """
    from types import SimpleNamespace

    import app.core.data_quality as data_quality_module
    from app.web.api.screening import list_available_indicators

    monkeypatch.setattr(  # 单测不跑全量数据质量构建
        data_quality_module, "read_warning_codes", lambda *a, **k: [],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(duck=duckdb_store, sqlite=sqlite_store))
    )
    payload = list_available_indicators(request)
    names = [item["name"] for item in payload["indicators"]]
    derived = sorted(name for name in names if any(name.endswith(s) for s in RANK_SUFFIXES))

    assert derived == [], (
        f"/indicators 又下发了 {len(derived)} 条排名派生字段（样例 {derived[:6]}）："
        "排名口径必须走条件级下拉（rank_modes），不能铺成扁平字段把主指标埋掉"
    )
    assert payload["rank_modes"], "响应体必须下发 rank_modes 供前端渲染下拉"
    assert {mode["value"] for mode in payload["rank_modes"]} == set(RANK_MODES), (
        "rank_modes 必须覆盖全部排名口径，否则前端下拉会少口径（能力静默消失）"
    )


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

    seed = Seed(duckdb_store)
    seed.stocks(
        [("000001", "A"), ("000002", "B"), ("000003", "C")],
        is_listed=True,
        csrc_l1="制造业",
        is_st=False,
        is_suspended=False,
        listing_date="2000-01-01",
    )
    seed.snapshots([
        {
            "stock_code": code,
            "report_date": "2025-12-31",
            # `now()` 就是「库侧此刻」：换哨兵而不是写字面量（迁移守则）。
            "calculated_at": CURRENT_TIMESTAMP,
            "data_version": "fixture",
            "pe_ttm": pe,
        }
        for code, pe in (("000001", 30.0), ("000002", 10.0), ("000003", 20.0))
    ])

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


# ─── 用户可见指标名必须含中文（PRD §12.1）────────────────────────────────


def test_dsl_indicator_label_is_chinese_first() -> None:
    """复合指标的界面标签必须以中文开头。

    用户 2026-11-16 反馈：下拉里出现纯英文的
    `dividend_financing_ratio (DSL v2)`。PRD §12.1 要求
    「用户可见指标名称必须全中文，稳定字段名仅用于 API/规则/导出」。
    """
    from app.web.api.screening import _dsl_indicator_label

    known = _dsl_indicator_label({"name": "dividend_financing_ratio", "version": 2})
    assert known.startswith("分红融资比"), known
    assert "v2" in known

    # 未登记中文名的复合指标也必须带中文标识，不能整条英文
    unknown = _dsl_indicator_label({"name": "my_custom_factor", "version": 3})
    assert any("\u4e00" <= ch <= "\u9fff" for ch in unknown), unknown
    assert unknown.startswith("复合指标"), unknown


def test_every_indicator_label_contains_chinese() -> None:
    """所有下发的指标标签都必须含中文（不变量，防止再退化成纯英文）。

    统计域/排名字段靠模板拼中文（`STAT_METRIC_LABELS`、
    `RANK_MODE_LABELS`），这里只校验**后端直接给 label 的**那些：
    复合指标（DSL）是最容易漏的一类。
    """
    from app.web.api.screening import _dsl_indicator_label

    samples = [
        {"name": "dividend_financing_ratio", "version": 1},
        {"name": "dividend_financing_ratio", "version": 2},
        {"name": "some_unknown_dsl_name", "version": 7},
    ]
    for expression in samples:
        label = _dsl_indicator_label(expression)
        assert any("\u4e00" <= ch <= "\u9fff" for ch in label), (
            f"复合指标标签无中文：{label}"
        )


# ─── 参数化字段：国债期限 / CAGR 年数（reports/143 §2）────────────────────


def test_param_families_hide_suffixed_fields() -> None:
    """15 个带参数后缀的字段不再出现在指标下拉里，只留 4 个基名入口。"""
    from app.core.screening.engine import (
        PARAM_FAMILIES,
        SNAPSHOT_COLUMNS,
        param_field_names,
    )

    hidden = param_field_names()
    assert len(hidden) == 15, f"应为 9 个期限 + 6 个 CAGR，实际 {len(hidden)}"
    assert hidden <= SNAPSHOT_COLUMNS, "隐藏的应都是快照真实列"

    bases = {b for spec in PARAM_FAMILIES.values() for b in spec["bases"]}
    assert bases == {
        "revenue_cagr", "net_profit_cagr", "deducted_profit_cagr", "div_yield_spread",
    }
    # 基名本身不是真实列，必须靠参数展开才能用
    assert not (bases & SNAPSHOT_COLUMNS)


def test_expand_param_cagr_and_tenor() -> None:
    out = expand_rank_modes({
        "logic": "AND",
        "rules": [
            {"field": "revenue_cagr", "cagr_years": 5, "op": ">", "value": 0.2},
            {"field": "div_yield_spread", "tenor": "10y", "op": ">", "value": 0},
        ],
    })
    assert out["rules"][0]["field"] == "revenue_cagr5"
    assert out["rules"][1]["field"] == "div_yield_spread_10y"
    assert "cagr_years" not in out["rules"][0]
    assert "tenor" not in out["rules"][1]


def test_param_value_must_be_in_options() -> None:
    """库里 CAGR 只有 3/5 年 → 传 8 必须显式报错，而不是静默变成未知字段。"""
    with pytest.raises(ValueError, match="cagr_years 取值必须是"):
        expand_rank_modes({"logic": "AND", "rules": [
            {"field": "revenue_cagr", "cagr_years": 8},
        ]})
    with pytest.raises(ValueError, match="tenor 取值必须是"):
        expand_rank_modes({"logic": "AND", "rules": [
            {"field": "div_yield_spread", "tenor": "12y"},
        ]})


def test_param_base_without_value_is_rejected() -> None:
    """基名不带参数 → 必须提示需要指定参数（基名不是真实列）。"""
    with pytest.raises(ValueError, match="需要指定 cagr_years"):
        expand_rank_modes({"logic": "AND", "rules": [{"field": "revenue_cagr"}]})


def test_param_on_wrong_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="不支持 cagr_years"):
        expand_rank_modes({"logic": "AND", "rules": [
            {"field": "roe", "cagr_years": 5},
        ]})


def test_param_and_rank_mode_compose_when_rankable() -> None:
    """参数展开在排名后缀之前：`div_yield_spread` + 10y + market_rank。"""
    out = expand_rank_modes({"logic": "AND", "rules": [
        {"field": "div_yield_spread", "tenor": "10y", "rank_mode": "market_rank"},
    ]})
    assert out["rules"][0]["field"] == "div_yield_spread_10y_market_rank"


def test_unrankable_base_gives_actionable_error() -> None:
    """CAGR 不在可排名清单 → 报「不支持排名口径」，不能只说「未知字段」。"""
    from app.core.screening.engine import unrankable_rank_base

    assert unrankable_rank_base("revenue_cagr3_market_rank", set()) == "revenue_cagr3"
    # 可排名基名不该走这个分支
    assert unrankable_rank_base("pe_ttm_market_rank", set()) is None
    # 完全不存在的字段也不是这条错误
    assert unrankable_rank_base("nonsense_market_rank", set()) is None


def test_param_works_in_conditions_sort_and_columns(
    duckdb_store: DuckDBStore, sqlite_store: Any,
) -> None:
    """参数化字段在条件、排序、结果列三处都能用。"""
    from app.core.screening.engine import ScreeningEngine

    seed = Seed(duckdb_store)
    seed.stocks(
        [("000001", "A"), ("000002", "B")],
        is_listed=True,
        csrc_l1="制造业",
        is_st=False,
        is_suspended=False,
        listing_date="2000-01-01",
    )
    seed.snapshots([
        {
            "stock_code": code,
            "report_date": "2025-12-31",
            # `now()` 就是「库侧此刻」：换哨兵而不是写字面量（迁移守则）。
            "calculated_at": CURRENT_TIMESTAMP,
            "data_version": "fixture",
            "revenue_cagr3": cagr,
            "div_yield_spread_10y": 1.5,
        }
        for code, cagr in (("000001", 0.30), ("000002", 0.10))
    ])

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    rule = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "revenue_cagr", "cagr_years": 3, "op": ">", "value": 0},
        ]},
        "sort": [{"field": "revenue_cagr", "cagr_years": 3, "direction": "desc"}],
        "columns": ["stock_code", {"field": "div_yield_spread", "tenor": "10y"}],
    }
    rows = engine.run(rule, min_listing_years=0)["results"]

    assert [r["stock_code"] for r in rows] == ["000001", "000002"], "按 CAGR 降序"
    assert rows[0]["div_yield_spread_10y"] == 1.5, "结果列参数展开正确"
