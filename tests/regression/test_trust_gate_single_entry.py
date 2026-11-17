"""遮蔽判定单入口回归：字段级遮蔽只能有一个公开函数名。

防的是什么事故（reports/136 §6.2）：`masked_fields_for_codes()` 曾有一个纯
别名 `masked_indicator_fields()`，两个名字指向同一实现，都能跑。后果不是"报错"，
而是**静默漂移**：

- 有人改判据（比如新增一条 warning code）只改一个名字 → 两条路径给出不同遮蔽
  结论，"筛选说能用、详情说不可信"的分裂就是这么长出来的；
- `grep` 找不齐调用点 → 代码考古时误判"这里没人用，可以删/可改"。

本文件钉死三件事：

1. `app.core.data_quality` 上**没有** `masked_indicator_fields` 这个属性
   （alias 已删除；若有人"顺手"加回来，这里立刻红）；
2. `masked_fields_for_codes` 仍可调用，且 `indicator_trust` 仍定义在本模块
   （入口被搬走时能第一时间发现）；
3. 两个出口**同一份判定**：`masked_fields_for_codes(codes)` 必须等于
   `indicator_trust(codes)["masked_fields"]` —— 否则字段遮蔽与可信度响应体
   会走到两个真相上。

注意：`app/web/api/screening.py` 里另有一个**私有** `_masked_indicator_fields
(request)`，它读的是 `indicator_trust()["masked_fields"]`，与本文件的"单入口"
不冲突（那是 HTTP 层的取参 helper，不是判定实现）。
"""

from __future__ import annotations

import app.core.data_quality as dq

# 两条真实判定各自覆盖一个分支：扩展域错位（55 列）与扣非缺口（4 列）。
_CODES = ["EXT_PERIOD_MISMATCH", "DEDUCTED_PROFIT_GAP"]


def test_alias_masked_indicator_fields_is_gone() -> None:
    assert not hasattr(dq, "masked_indicator_fields"), (
        "masked_indicator_fields 是被删除的纯别名（reports/136 §6.2）；"
        "请统一调用 masked_fields_for_codes()，不要恢复第二个名字"
    )


def test_single_entry_is_callable_and_owned_by_data_quality() -> None:
    assert callable(dq.masked_fields_for_codes)
    assert dq.indicator_trust.__module__ == "app.core.data_quality"


def test_function_and_trust_response_share_one_decision() -> None:
    assert dq.masked_fields_for_codes(_CODES) == dq.indicator_trust(_CODES)["masked_fields"]
