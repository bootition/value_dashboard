"""`screening.*` 配置键必须真被读取（今天以前是静默无效的假配置）。

背景（reports/124 §5 第 10 项：代码注释/配置与事实不一致）：
`config/default.yaml` 的 136 个叶子键里有 9 个在 `app/` + `scripts/` +
`frontend/src/` 零命中，用户改了也不会有任何效果。本文件钉死 screening 段
这些键的真实接线点：

- ``max_nesting_levels`` → ``app/core/dsl/parser.py::_nesting_limit()``
  （配置优先；配置缺失/非法回退历史硬编码 50）；
- ``max_conditions`` → ``ScreeningEngine._enforce_condition_budget()``
  （配置优先；缺省 20；超限在执行前报明确 ValueError）；
- ``default_exclude_st`` / ``default_exclude_suspended`` /
  ``default_min_listing_years`` → ``ScreeningEngine.run()`` 的缺省实参
  （调用方不传时才生效）与 ``app/web/api/watchlist.py`` 的刷新路径。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.core.config import Config
from app.core.dsl.parser import parse
from app.core.screening.defaults import (
    DEFAULT_MAX_CONDITIONS,
    DEFAULT_MAX_NESTING_LEVELS,
    base_pool_defaults,
    max_conditions,
    max_nesting_levels,
)
from app.core.screening.engine import ScreeningEngine


def _install_config(monkeypatch: pytest.MonkeyPatch, screening: dict[str, Any] | None) -> None:
    """把 Config 单例换成只含 ``screening`` 段的假配置（测试后自动还原）。"""
    data: dict[str, Any] = {} if screening is None else {"screening": screening}
    monkeypatch.setattr(Config, "_instance", Config(data))


# ─── max_nesting_levels：解析器括号嵌套上限 ────────────────────────────────


def test_max_nesting_levels_defaults_to_50_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置里没有该键时必须回退 50（历史 MAX_NESTING_DEPTH），不是 3。"""
    _install_config(monkeypatch, {})
    assert max_nesting_levels() == DEFAULT_MAX_NESTING_LEVELS == 50
    # 4 层括号在缺省回退下必须能解析（旧硬编码 50 的行为不变）。
    assert parse("((((" "1" "))))") is not None


def test_parser_honours_configured_nesting_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置 2 → 2 层可以解析，3 层必须报错并点明配置项。"""
    _install_config(monkeypatch, {"max_nesting_levels": 2})
    assert max_nesting_levels() == 2
    assert parse("((1))") is not None

    with pytest.raises(ValueError, match="screening.max_nesting_levels"):
        parse("(((1)))")


def test_parser_rejects_nesting_three_when_repo_config_says_three() -> None:
    """本仓 `config/default.yaml` 取 3：默认配置下 3 层可解析、4 层报错。

    这条用例同时是"配置真的生效"的证据 —— 旧实现硬编码 50，4 层永远不报错。
    """
    import yaml

    from app.core.config import _CONFIG_DIR

    shipped = yaml.safe_load((_CONFIG_DIR / "default.yaml").read_text(encoding="utf-8"))
    assert shipped["screening"]["max_nesting_levels"] == 3

    from app.core.screening import defaults as screening_defaults

    original = Config._instance
    try:
        Config._instance = Config({"screening": shipped["screening"]})
        assert screening_defaults.max_nesting_levels() == 3
        assert parse("(((1)))") is not None
        with pytest.raises(ValueError, match="nesting depth"):
            parse("((((1))))")
    finally:
        Config._instance = original


# ─── max_conditions：规则条件数上限 ───────────────────────────────────────


def _rule_with_leaf_count(count: int) -> dict[str, Any]:
    return {
        "conditions": {
            "logic": "AND",
            "rules": [{"field": "pe_ttm", "op": ">", "value": i} for i in range(count)],
        },
        "sort": [],
        "columns": [],
    }


def test_max_conditions_defaults_to_20_and_rejects_21(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, {})
    assert max_conditions() == DEFAULT_MAX_CONDITIONS == 20

    # 20 项是产品口径的上限本身（前端编辑器 max-conditions=20），必须放行。
    ScreeningEngine._enforce_condition_budget(_rule_with_leaf_count(20)["conditions"])

    with pytest.raises(ValueError, match="超过上限 20 项"):
        ScreeningEngine._enforce_condition_budget(_rule_with_leaf_count(21)["conditions"])


def test_max_conditions_follows_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_config(monkeypatch, {"max_conditions": 3})
    assert max_conditions() == 3
    ScreeningEngine._enforce_condition_budget(_rule_with_leaf_count(3)["conditions"])
    with pytest.raises(ValueError, match="筛选条件 4 项超过上限 3 项"):
        ScreeningEngine._enforce_condition_budget(_rule_with_leaf_count(4)["conditions"])


def test_nested_groups_count_as_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    """嵌套组里的叶子一起计数 —— 绕过前端直接 POST 嵌套结构也要拦住。"""
    _install_config(monkeypatch, {"max_conditions": 2})
    conditions = {
        "logic": "AND",
        "rules": [
            {"field": "pe_ttm", "op": ">", "value": 0},
            {"logic": "OR", "rules": [
                {"field": "pb_mrq", "op": "<", "value": 1},
                {"field": "roe", "op": ">", "value": 0.1},
            ]},
        ],
    }
    with pytest.raises(ValueError, match="超过上限 2 项"):
        ScreeningEngine._enforce_condition_budget(conditions)


def test_run_rejects_rule_over_configured_max_conditions_before_query(
    monkeypatch: pytest.MonkeyPatch,
    database_paths,
) -> None:
    """`run()` 真的会调用上限校验（不是只在静态方法里存在）。"""
    _install_config(monkeypatch, {"max_conditions": 2})
    engine = ScreeningEngine(paths=database_paths)
    with pytest.raises(ValueError, match="超过上限 2 项"):
        # include_* / min_listing_years 显式给值 → 不触发"基础池元数据不全"
        # 的库查询，异常必定来自条件数上限。
        engine.run(
            _rule_with_leaf_count(3),
            include_st=True,
            include_suspended=True,
            min_listing_years=0,
        )


# ─── default_exclude_st / default_exclude_suspended / default_min_listing_years ──


def test_base_pool_defaults_follow_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_config(monkeypatch, {
        "default_exclude_st": False,
        "default_exclude_suspended": False,
        "default_min_listing_years": 3,
    })
    assert base_pool_defaults() == {
        "exclude_st": False,
        "exclude_suspended": False,
        "min_listing_years": 3,
    }


def test_base_pool_defaults_fall_back_to_previous_hardcoded_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置缺键时行为与历史硬编码完全一致（True/True/1）。"""
    _install_config(monkeypatch, {})
    assert base_pool_defaults() == {
        "exclude_st": True,
        "exclude_suspended": True,
        "min_listing_years": 1,
    }


def test_base_pool_defaults_tolerate_broken_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """坏配置回落到缺省值，绝不让筛选/解析炸掉。"""
    _install_config(monkeypatch, {
        "default_exclude_st": "not-a-bool",
        "default_min_listing_years": "many",
        "max_conditions": -5,
        "max_nesting_levels": 0,
    })
    assert base_pool_defaults() == {
        "exclude_st": True,
        "exclude_suspended": True,
        "min_listing_years": 1,
    }
    assert max_conditions() == DEFAULT_MAX_CONDITIONS
    assert max_nesting_levels() == DEFAULT_MAX_NESTING_LEVELS


def test_engine_run_defers_pool_defaults_to_config() -> None:
    """参数缺省必须是 None 哨兵 —— 否则配置永远轮不到生效。"""
    params = inspect.signature(ScreeningEngine.run).parameters
    assert params["include_st"].default is None
    assert params["include_suspended"].default is None
    assert params["min_listing_years"].default is None


def test_run_resolves_pool_defaults_from_config(
    monkeypatch: pytest.MonkeyPatch,
    duckdb_store,
    sqlite_store,
) -> None:
    """调用方不传时，`run()` 把配置值原样送进基础池 SQL 构建。

    只钉"配置 → SQL 参数"这一段：`_build_sql` 被替换成立即中断的探针，
    避免真的执行查询。断言 exclude_*=false → include_*=true、年限取配置值。
    """
    _install_config(monkeypatch, {
        "default_exclude_st": False,
        "default_exclude_suspended": False,
        "default_min_listing_years": 5,
    })

    captured: dict[str, Any] = {}

    class _StopAfterCapture(Exception):
        pass

    def _probe(self, **kwargs):  # 测试桩：抓到参数就中断
        captured.update(kwargs)
        raise _StopAfterCapture

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(ScreeningEngine, "_build_sql", _probe)

    with pytest.raises(_StopAfterCapture):
        engine.run({"conditions": {"logic": "AND", "rules": []}, "sort": [], "columns": []})

    assert captured["include_st"] is True
    assert captured["include_suspended"] is True
    assert captured["min_listing_years"] == 5


def test_run_keeps_explicit_pool_arguments(
    monkeypatch: pytest.MonkeyPatch,
    duckdb_store,
    sqlite_store,
) -> None:
    """显式传参必须压过配置（绝不静默覆盖用户的勾选）。"""
    _install_config(monkeypatch, {
        "default_exclude_st": True,
        "default_min_listing_years": 5,
    })

    captured: dict[str, Any] = {}

    class _StopAfterCapture(Exception):
        pass

    def _probe(self, **kwargs):  # 测试桩：抓到参数就中断
        captured.update(kwargs)
        raise _StopAfterCapture

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(ScreeningEngine, "_build_sql", _probe)

    with pytest.raises(_StopAfterCapture):
        engine.run(
            {"conditions": {"logic": "AND", "rules": []}, "sort": [], "columns": []},
            include_st=True,
            include_suspended=True,
            min_listing_years=0,
        )

    assert captured["include_st"] is True
    assert captured["min_listing_years"] == 0
