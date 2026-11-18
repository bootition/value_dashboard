"""事故出处：reports/139 / 2026-11-18。

行为契约（cb-09）：保存期字段校验 `validate_rule_fields()` 与运行期
`ScreeningEngine._is_known_field()` 必须共用**同一份**字段白名单。

事故表现：保存期曾并列维护第二份手工白名单，漏了 `EXTENDED_COLUMNS`
（`indicator_ext` 的扩展指标列）。引用扩展字段的规则在运行时能跑，保存时却抛
`未知筛选字段` —— "能跑不能存"。本文件钉住四条契约：

1. 只含扩展字段的规则能通过保存期校验（正向）；
2. 虚构字段仍被拒，异常类型/文案与修复前一致（反向）；
3. `EXTENDED_COLUMNS` 每个字段都能过保存期校验（参数化，防再次漏登记）；
4. 运行期与保存期对同一字段的判定完全一致（防两份白名单以别的形式重新分叉）。
"""

from __future__ import annotations

import pytest

from app.core.screening.engine import (
    EXTENDED_COLUMNS,
    METADATA_COLUMNS,
    NORMALIZED_FIELDS,
    RANK_SUFFIXES,
    RANKABLE_INDICATORS,
    SNAPSHOT_COLUMNS,
    STAT_FIELDS,
    ScreeningEngine,
    validate_rule_fields,
)
from app.core.storage.duckdb_store import DuckDBStore


def _rule_for(field: str) -> dict:
    """一条只引用 `field` 的最小规则（conditions + sort + columns）。"""
    return {
        "conditions": {
            "logic": "AND",
            "rules": [{"field": field, "op": ">", "value": 0}],
        },
        "sort": [{"field": field, "direction": "desc"}],
        "columns": [field],
    }


# ─── 正向：只含扩展字段的规则可以保存 ─────────────────────────────────────


def test_extended_only_rule_is_savable() -> None:
    validate_rule_fields(
        {
            "conditions": {
                "logic": "AND",
                "rules": [
                    {"field": "free_cash_flow", "op": ">", "value": 0},
                    {
                        "field": "receivables_turnover",
                        "op": ">",
                        "right_field": "inventory_turnover",
                    },
                ],
            },
            "sort": [{"field": "market_cap_at_report", "direction": "desc"}],
            "columns": ["free_cash_flow", "inventory_turnover", "market_cap_at_report"],
        }
    )


@pytest.mark.parametrize("field", sorted(EXTENDED_COLUMNS))
def test_every_extended_column_is_savable(field: str) -> None:
    validate_rule_fields(_rule_for(field))


# ─── 反向：虚构字段仍然被拒，异常文案不变 ─────────────────────────────────


def test_unknown_field_still_rejected_with_same_error_messages() -> None:
    with pytest.raises(ValueError, match="^未知筛选字段: not_a_field$"):
        validate_rule_fields(_rule_for("not_a_field"))
    with pytest.raises(ValueError, match="^未知排序字段: not_a_field$"):
        validate_rule_fields({"sort": [{"field": "not_a_field", "direction": "asc"}]})
    with pytest.raises(ValueError, match="^未知结果字段: not_a_field$"):
        validate_rule_fields({"columns": ["not_a_field"]})


# ─── 一致性：运行期判定 == 保存期判定 ─────────────────────────────────────


def _save_time_accepts(field: str, custom_fields: set[str]) -> bool:
    try:
        validate_rule_fields(_rule_for(field), custom_fields)
    except ValueError:
        return False
    return True


def test_runtime_and_save_time_whitelists_agree(duckdb_store: DuckDBStore) -> None:
    engine = ScreeningEngine(duck=duckdb_store)
    rank_fields = {
        f"{base}{suffix}" for base in RANKABLE_INDICATORS for suffix in RANK_SUFFIXES
    }
    candidates = (
        set(SNAPSHOT_COLUMNS)
        | set(NORMALIZED_FIELDS)
        | set(EXTENDED_COLUMNS)
        | set(METADATA_COLUMNS)
        | set(STAT_FIELDS)
        | set(RANKABLE_INDICATORS)
        | rank_fields
        | {"not_a_field", ""}
    )
    for field in sorted(candidates):
        assert engine._is_known_field(field) == _save_time_accepts(field, set()), field

    # 已发布 DSL 自定义字段及其 rank 派生名：两边也必须同步放行
    custom_fields = {"my_factor"}
    engine._custom_fields = set(custom_fields)
    for field in ("my_factor", "my_factor_market_rank"):
        assert engine._is_known_field(field) is True
        assert _save_time_accepts(field, custom_fields) is True
