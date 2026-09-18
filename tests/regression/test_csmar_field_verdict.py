"""CSMAR 三色灯裁定读取器回归（Phase B2, 2026-09-17）。

守住三件事：
1. 裁定表可分类型读全（fields / families / traps / policy）；
2. fail-closed —— 未登记字段一律 red，不得默认可用；
3. 已知陷阱与关键裁定不得被无声改写（防止将来有人"顺手"把 red 调成 green）。
"""

from __future__ import annotations

import json

import pytest

from app.core.csmar_formulas import (
    CsmarVerdictError,
    adoption_policy,
    families,
    is_adoptable,
    load_raw,
    reset_cache,
    traps,
    verdict_for,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def test_config_is_loadable_and_wellformed():
    doc = load_raw()
    assert doc["schema_version"] >= 1
    assert doc["fields"], "裁定表必须包含字段级裁定"
    assert doc["families"], "裁定表必须包含缺失指标族裁定"
    assert set(doc["rules"]) >= {"green", "yellow", "red", "none"}


def test_green_fields_are_adoptable():
    for name in ("debt_ratio", "current_ratio", "quick_ratio"):
        verdict = verdict_for(name)
        assert verdict.color == "green", f"{name} 应为 green"
        assert is_adoptable(name) is True


def test_red_fields_are_never_adoptable():
    """市值口径陷阱：CSMAR 市值含负债，绝不可替换本项目口径。"""
    assert verdict_for("total_market_cap").color == "red"
    assert is_adoptable("total_market_cap") is False
    assert is_adoptable("interest_coverage") is False


def test_unregistered_field_fails_closed():
    verdict = verdict_for("some_field_that_does_not_exist")
    assert verdict.color == "red"
    assert verdict.adoptable is False


def test_known_traps_are_preserved():
    t = traps()
    assert "market_cap_includes_debt" in t
    assert "equity_total_is_parent_only" in t
    # 陷阱字段必须仍在裁定表里被标红，避免被改成 green
    market_cap = verdict_for("total_market_cap")
    assert market_cap.is_trap is True


def test_families_carry_dependency_chain():
    by_key = {f.key: f for f in families()}
    assert {"turnover", "cashflow_indirect", "free_cash_flow", "leverage"} <= set(by_key)
    # 自由现金流 / 杠杆 依赖间接法现金流
    assert by_key["free_cash_flow"].depends_on == ("cashflow_indirect",)
    assert by_key["leverage"].depends_on == ("cashflow_indirect",)
    # 周转率不需额外依赖，可直接自算
    assert by_key["turnover"].depends_on == ()
    assert by_key["turnover"].color == "green"


def test_adoption_policy_forbids_external_distribution():
    policy = adoption_policy()
    assert "禁止" in policy.get("forbidden", "")
    assert "csmar" in policy.get("lineage_requirement", "")


def test_missing_config_raises(tmp_path):
    with pytest.raises(CsmarVerdictError):
        load_raw(tmp_path / "nope.json")


def test_broken_config_raises(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CsmarVerdictError):
        load_raw(bad)


def test_config_contains_no_csmar_verbatim_documentation():
    """裁定表只应有本项目的判定与理由，不得夹带 CSMAR 说明书原文段落。"""
    text = json.dumps(load_raw(), ensure_ascii=False)
    for marker in ("计算公式为：", "统计截止日期", "以沪、深、北证券交易所公布"):
        assert marker not in text, f"裁定表疑似夹带 CSMAR 文档原文: {marker}"
