"""更新步骤清单与真实执行流程的一致性护栏（reports/134 §5 S1 第一步）。

背景：``app/core/update.py::IncrementalUpdater._run_incremental_update_flow()`` 用 500+ 行
代码表达"一轮更新有哪几步"，步骤清单只存在于**代码执行顺序**里。``app/core/update_steps.py``
的 ``STEP_ORDER`` 是这份清单的唯一可读索引，本文件用 AST 静态扫描保证两者不漂移：

1. 代码里出现的每个 ``report_step("字面量", ...)`` 首参都必须在 ``STEP_ORDER`` 登记；
2. ``STEP_ORDER`` 内没有重名；
3. ``STEP_ORDER`` 至少覆盖 20 步（防止有人把清单删空后测试仍绿）；
4. 每个 ``UpdateStep.label`` 非空且是中文；
5. ``STEP_ORDER`` 的顺序 == 代码里 ``report_step`` 首参**首次出现**的顺序。

第 5 条是"严格按真实执行顺序"这一要求的机器表达：顺序只写在 ``STEP_ORDER`` 的注释里
是守不住的，插入新步骤时最容易放错位置（reports/134 §3 热点 #2），所以这里把它变成断言。

反向（``STEP_ORDER`` 里有、代码里暂时没有）**故意不**断言：允许先登记一个待接入的
步骤，但不允许代码里出现未登记的步骤。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.core.update_steps import STEP_NAMES, STEP_ORDER

#: 主流程的**源码集合**。
#
#: 2026-11-19：`_run_incremental_update_flow` 搬到了 `update_parts/flow.py`。
#: 本文件原来只扫 `update.py` —— 搬走后那里 `report_step` 归零，于是：
#:   ① `test_report_step_calls_are_found_at_all` 红（哨兵**正确**报警了）；
#:   ② 更危险的是顺序断言变成 `[] == []` **真空通过** —— "顺序错要红"的能力静默消失。
#: 所以扫描范围必须是"主流程可能所在的全部文件"，而不是某一个固定路径。
_FLOW_SOURCES = (
    Path(__file__).resolve().parents[2] / "app" / "core" / "update.py",
    Path(__file__).resolve().parents[2] / "app" / "core" / "update_parts" / "flow.py",
)

#: 首参不是字符串字面量的 ``report_step(...)`` 调用，按行号登记（含原因）。
#:
#: 现状为**空**：`_run_incremental_update_flow()` 里 29 处调用全部是
#: ``report_step("<名字>", ...)`` 的字面量形式。若将来出现
#: ``report_step(_STEP_NAMES[key], ...)`` 这类动态名字，AST 静态扫描看不到真实步骤名，
#: 就在这里按行号登记（键=行号，值=为什么无法静态确定 + 实际取值来源），并考虑把动态
#: 名字改成显式字面量，否则步骤清单会悄悄失守。
_NON_LITERAL_REPORT_STEP_WHITELIST: dict[int, str] = {}

#: label 允许的字符：汉字 + 常用中文标点/括号/空格 + ASCII 字母数字（"ETF 行情" 这类
#: 带缩写的展示名）。纯 ASCII（如 "prices"）没有汉字，会被下面判成"不是中文"。
_ALLOWED_LABEL_CHARS = re.compile(
    r"^[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9（）()、，,/·．.\-—\s]+$"
)
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _report_step_calls() -> list[tuple[int, str | None]]:
    """返回**主流程源码集合**里每处 ``report_step(...)`` 调用的 ``(行号, 首参字面量或 None)``。"""
    calls: list[tuple[int, str | None]] = []
    for source in _FLOW_SOURCES:
        if not source.is_file():
            continue
        calls.extend(_report_step_calls_in(source))
    return calls


def _report_step_calls_in(source: Path) -> list[tuple[int, str | None]]:
    """扫描单个文件里的 ``report_step(...)`` 调用。"""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    calls: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "report_step"):
            continue
        literal: str | None = None
        first: ast.expr | None = node.args[0] if node.args else None
        if first is None:
            # 兼容 report_step(name="prices", step=...) 的关键字写法。
            first = next(
                (kw.value for kw in node.keywords if kw.arg == "name"),
                None,
            )
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literal = first.value
        calls.append((node.lineno, literal))
    return sorted(calls)


def test_report_step_calls_are_found_at_all() -> None:
    """扫描本身要能工作（否则后面几条断言会因为空的调用列表而假绿）。"""
    calls = _report_step_calls()
    assert len(calls) >= 20, f"只扫到 {len(calls)} 处 report_step 调用，AST 扫描或流程结构可能已变"


def test_non_literal_report_step_names_are_whitelisted() -> None:
    """非字面量首参必须显式登记（否则静态清单无法保证覆盖）。"""
    dynamic = {lineno for lineno, name in _report_step_calls() if name is None}
    unexpected = sorted(dynamic - set(_NON_LITERAL_REPORT_STEP_WHITELIST))
    assert not unexpected, (
        f"update.py 第 {unexpected} 行的 report_step 首参不是字符串字面量；"
        "请改成字面量，或在 _NON_LITERAL_REPORT_STEP_WHITELIST 登记原因"
    )
    stale = sorted(set(_NON_LITERAL_REPORT_STEP_WHITELIST) - dynamic)
    assert not stale, f"白名单第 {stale} 行已不再是非字面量调用，请从白名单删除"


def test_every_report_step_call_is_registered_in_step_order() -> None:
    """代码里出现的每个步骤名都必须在 STEP_ORDER 里（漏登记 = 清单不完整）。"""
    called = [(lineno, name) for lineno, name in _report_step_calls() if name is not None]
    missing = sorted({(lineno, name) for lineno, name in called if name not in STEP_NAMES})
    assert not missing, (
        "update.py 里这些 report_step 调用的步骤名没有登记在 "
        f"app/core/update_steps.py::STEP_ORDER：{missing}"
    )


def test_step_order_has_no_duplicate_names() -> None:
    """STEP_ORDER 内不许重名（同名步骤多次出现时只登记一次，并在 note 里说明条件重跑）。"""
    names = [step.name for step in STEP_ORDER]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"STEP_ORDER 里有重复步骤名：{duplicates}"


def test_step_order_covers_at_least_twenty_steps() -> None:
    """清单不得被删空：一轮更新至少有 20 个步骤（2026-11-17 实测 25 个）。"""
    assert len(STEP_ORDER) >= 20, f"STEP_ORDER 只有 {len(STEP_ORDER)} 步，低于 20 步下限"


@pytest.mark.parametrize("step", STEP_ORDER, ids=lambda step: step.name)
def test_step_label_is_non_empty_chinese(step) -> None:
    """label 非空且是中文（允许 "ETF 行情" 这类带 ASCII 缩写的写法）。"""
    label = step.label
    assert label.strip(), f"{step.name} 的 label 为空"
    assert label == label.strip(), f"{step.name} 的 label 首尾有空白：{label!r}"
    assert _CJK_CHAR.search(label), f"{step.name} 的 label 不含汉字（不是中文）：{label!r}"
    assert _ALLOWED_LABEL_CHARS.match(label), f"{step.name} 的 label 含非法字符：{label!r}"
    assert step.domain.strip(), f"{step.name} 的 domain 为空"
    assert step.name, f"存在空 name 的 UpdateStep：{step!r}"


def test_step_order_matches_first_appearance_in_update_flow() -> None:
    """STEP_ORDER 的顺序必须等于 update.py 里 report_step 首参的首次出现顺序。

    这是"严格按真实执行顺序"的唯一机器表达（清单顺序本身就是给人读的，写错位置比漏登记
    更隐蔽）。改流程顺序 / 插入新步骤后，请同步调整 ``STEP_ORDER`` 再跑本测试。
    """
    first_appearance: list[str] = []
    for _, name in _report_step_calls():
        if name is not None and name not in first_appearance:
            first_appearance.append(name)
    order = [step.name for step in STEP_ORDER]
    if order == first_appearance:
        return
    # 只在"已登记的名字"上比较顺序，避免把"清单里有、代码里暂时没有"报成顺序错误。
    comparable = [name for name in order if name in set(first_appearance)]
    assert comparable == first_appearance, (
        "STEP_ORDER 的顺序与 update.py 中 report_step 首次出现顺序不一致：\n"
        f"  代码顺序: {first_appearance}\n"
        f"  清单顺序: {comparable}"
    )
