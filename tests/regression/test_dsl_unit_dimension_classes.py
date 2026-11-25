"""DSL 维度校验必须按「存储量纲」比较，而不是按 unit 字符串（2026-11-25，reports/167）。

背景
----
`unit` 一个字符串同时背着两件事：**存储量纲**（小数 / 百分数 / 金额 / 股数）
与**展示形态**（`ratio` 倍数 vs `pct` 百分比）。校验器直接比字符串，于是同时
产生两类硬错误，两类都用正式库口径复现过：

1. **假放行（差 100 倍的静默错）**
   `app/core/dsl/ast_nodes.py` 里 13 个指标声明 `unit="percent"`，实际存的是
   小数（`dividend_yield` 0.0255 = 2.55%，`revenue_yoy` 0.0371 = 3.71% …；
   正式库 3,735 行 `ttm_dividend_yield` = `dividend_yield` × 100 精确成立）。
   于是 `dividend_yield > ttm_dividend_yield` 被判为合法 → 0.0255 > 2.55 恒假，
   规则静默筛出 0 只，没有任何提示。

2. **假报错（把合规用法拦下来）**
   `debt_ratio`（unit=ratio）与 `balance.interest_bearing_debt_ratio`
   （unit=pct）都是 0–1 的小数，却被判成不同维度而拒绝 —— 而
   `app/core/dsl/derived_fields.py` 的 doc 恰恰建议用户把这两个条件组合成
   「高负债率 + 低有息负债率」的排雷条件。文档推荐的用法写不出来。

修法
----
维度校验只认存储量纲：`pct` 与 `ratio` 同为「无量纲小数」，等价；
`percent`（1 = 1%）是另一种量纲，与它们差 100 倍，必须拦。
见 `app/core/dsl/validator.py` 的 `DIMENSION_CLASS` / `dimension_class()`。

本文件同时是一个**棘轮**：把 `INDICATOR_METADATA` / `FIELD_METADATA` 的
`unit` 与存储口径真源 `app/core/screening/field_units.py` 对拍，防止再漂移。
"""

from __future__ import annotations

import pathlib

import pytest

from app.core.dsl.ast_nodes import FIELD_METADATA, INDICATOR_METADATA
from app.core.dsl.parser import parse
from app.core.dsl.validator import DIMENSION_CLASS, Validator, dimension_class
from app.core.screening.field_units import (
    PCT_DECIMAL_FIELDS,
    PERCENT_STORED_FIELDS,
)


def _isolated_store(tmp_path: pathlib.Path):
    """只存在于临时目录的 SQLiteStore（不碰正式库）。"""
    from app.core.storage.path_policy import DatabasePathSet, VdEnv
    from app.core.storage.schema import init_sqlite_schema
    from app.core.storage.sqlite_store import SQLiteStore

    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=tmp_path / "valuedashboard.duckdb",
        sqlite_path=tmp_path / "valuedashboard.sqlite",
        run_root=tmp_path,
    )
    store = SQLiteStore(paths=paths)
    init_sqlite_schema(store)
    return store


def _validate(tmp_path: pathlib.Path, expression: str) -> dict:
    return Validator(sqlite=_isolated_store(tmp_path)).validate(parse(expression))


# ─── 缺陷 1：跨量纲必须拦（修前被静默放行，差 100 倍）────────────────────────


@pytest.mark.parametrize(
    "expression",
    [
        "dividend_yield > ttm_dividend_yield",       # 小数 vs 百分数（历史误标字段）
        "dividend_yield >= div_yield_spread_10y",    # 小数 vs 百分数（利差）
        "revenue_yoy > turnover_rate",               # 同比也是小数口径
        "period_return + div_yield_spread_1y > 3",   # 加减法同样必须拦
    ],
)
def test_cross_magnitude_comparison_is_rejected(tmp_path, expression) -> None:
    result = _validate(tmp_path, expression)
    assert result["valid"] is False, f"{expression} 必须被拦，实际放行"
    assert any("维度不匹配" in error for error in result["errors"]), result["errors"]


def test_rejection_message_names_both_magnitudes(tmp_path) -> None:
    """报错必须说清"差在哪"，否则用户只会看到"维度不匹配"四个字。"""
    result = _validate(tmp_path, "dividend_yield > ttm_dividend_yield")
    message = " ".join(result["errors"])
    assert "decimal" in message or "pct" in message
    assert "percent" in message


# ─── 缺陷 2：同量纲必须放行（修前被误拦）─────────────────────────────────────


@pytest.mark.parametrize(
    "expression",
    [
        # derived_fields.py 明确推荐的组合：高负债率 + 低有息负债率
        "debt_ratio > balance.interest_bearing_debt_ratio",
        "balance.net_cash_to_assets > debt_ratio",
        "balance.interest_bearing_debt_to_liabilities < debt_ratio",
        "balance.advance_from_customers_ratio < debt_ratio",
        # 同为百分比的小数口径
        "roe > gross_margin",
        "revenue_cagr5 > net_profit_cagr5",
        "dividend_yield > payout_ratio",
        # 同为百分数存储
        "ttm_dividend_yield > div_yield_spread_10y",
    ],
)
def test_same_magnitude_comparison_is_accepted(tmp_path, expression) -> None:
    result = _validate(tmp_path, expression)
    assert result["valid"] is True, f"{expression} 不应被拦：{result['errors']}"


def test_money_and_ratio_still_incomparable(tmp_path) -> None:
    """放宽 pct/ratio 不能顺手把金额与比例也放开。"""
    for expression in ("balance.total_assets > roe", "total_market_cap < debt_ratio"):
        assert _validate(tmp_path, expression)["valid"] is False, expression


def test_dimension_class_taxonomy() -> None:
    """等价类表本身就是契约：pct/ratio 同族，percent 独立。"""
    assert dimension_class("pct") == dimension_class("ratio")
    assert dimension_class("percent") != dimension_class("ratio")
    assert dimension_class("CNY") == dimension_class("price")
    assert dimension_class("unknown") == "unknown"
    # 未登记的量纲自成一类（宁可拦住，不许默默等价）
    assert dimension_class("blivion") == "blivion"
    # 登记表里出现的新量纲必须给出归类，否则 dimension_class 回落成自身
    used = {meta["unit"] for meta in INDICATOR_METADATA.values()}
    used |= {meta["unit"] for meta in FIELD_METADATA.values()}
    missing = sorted(unit for unit in used if unit not in DIMENSION_CLASS)
    assert not missing, f"这些 unit 没有登记等价类: {missing}"


# ─── 棘轮：unit 声明必须与存储口径真源（field_units.py）一致 ─────────────────


def _declared_units() -> dict[str, str]:
    declared = {name: meta["unit"] for name, meta in INDICATOR_METADATA.items()}
    for key, meta in FIELD_METADATA.items():
        declared.setdefault(key, meta["unit"])
        # 裸名简写（balance.x → x）也要能查到
        declared.setdefault(key.split(".", 1)[-1], meta["unit"])
    return declared


def test_percent_stored_fields_are_declared_percent() -> None:
    """百分数存储（5.29 = 5.29%）的字段必须声明 percent。"""
    declared = _declared_units()
    wrong = {
        name: declared[name]
        for name in PERCENT_STORED_FIELDS
        if name in declared and declared[name] != "percent"
    }
    assert not wrong, f"这些字段实存百分数却声明成别的量纲: {wrong}"


def test_decimal_pct_fields_are_not_declared_percent() -> None:
    """小数存储（0.0529 = 5.29%）的字段绝不能声明 percent —— 那会放行 100 倍错比。"""
    declared = _declared_units()
    wrong = {
        name: declared[name]
        for name in PCT_DECIMAL_FIELDS
        if name in declared and declared[name] == "percent"
    }
    assert not wrong, f"这些字段实存小数却声明 percent（跨量纲比较会被放行）: {wrong}"


def test_every_pct_decimal_field_is_known_to_the_dsl() -> None:
    """field_units 里的每个小数百分比字段，要么在 DSL 侧登记，要么属于两类已披露例外。

    例外必须**由结构推导**而不是硬编码名单，否则名单本身会变成第二份真相源：

    (a) 扩展指标域（`EXTENDED_SCREENING_READY`）：来自 `indicator_ext` 表，筛选
        引擎直读列；DSL 侧没有 `indicator.*` 登记（`FIELD_METADATA` 里 0 条），
        所以它们本来就走不到维度校验，不构成漂移。
    (b) 年报口径同比 `*_yoy_fy`：按设计只做快照列，不作为可筛 DSL 指标暴露
        （见 `ast_nodes.py` 成长段的注释）。
    """
    from app.core.screening.engine import EXTENDED_SCREENING_READY

    declared = _declared_units()
    unknown = set(PCT_DECIMAL_FIELDS) - set(declared)
    explained = (unknown & EXTENDED_SCREENING_READY) | {
        name for name in unknown if name.endswith("_yoy_fy")
    }
    missing = sorted(unknown - explained)
    assert not missing, (
        f"field_units 已登记小数百分比但 DSL 侧没有 unit（会回落 unknown，"
        f"跨量纲比较将被放行）: {missing}"
    )


# ─── 条件 JSON（right_field）也必须拦跨量纲比较 ────────────────────────────────
#
# 同一类错在两条链路上各有一份实现：
#   - DSL 表达式 → `Validator._check_dimensions`（上面已覆盖）
#   - 条件 JSON 的字段对字段比较（`right_field`）→ `ScreeningEngine._build_condition`
# 后者此前只校验"两边都不是元数据列"，于是同一组 100 倍错比能存能跑。


def _build_condition(field: str, op: str, *, right_field: str | None = None):
    from app.core.screening.engine import ScreeningEngine

    engine = ScreeningEngine.__new__(ScreeningEngine)  # 不碰库：只测条件编译前的校验
    engine._custom_fields = set()  # type: ignore[attr-defined]
    cond: dict = {"field": field, "op": op}
    if right_field is None:
        cond["value"] = 1
    else:
        cond["right_field"] = right_field
    return ScreeningEngine._build_condition(engine, cond)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("dividend_yield", "ttm_dividend_yield"),
        ("revenue_yoy", "turnover_rate"),
        ("dividend_yield", "div_yield_spread_10y"),
    ],
)
def test_condition_right_field_rejects_cross_magnitude(left, right) -> None:
    with pytest.raises(ValueError, match="量纲冲突"):
        _build_condition(left, ">", right_field=right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("roe", "roce_5y_median"),                   # 小数 vs 小数
        ("debt_ratio", "balance.interest_bearing_debt_ratio"),
        ("ttm_dividend_yield", "div_yield_spread_10y"),  # 百分数 vs 百分数
        ("total_market_cap", "circ_market_cap"),      # 金额 vs 金额
    ],
)
def test_condition_right_field_allows_same_magnitude(left, right) -> None:
    sql, params = _build_condition(left, ">", right_field=right)
    assert right.replace(".", "__") in sql  # 字段名进 SQL 时点号换成双下划线
    assert params == []
