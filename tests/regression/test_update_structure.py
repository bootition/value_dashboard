"""`update.py` 拆分进度与"反重构死结"棘轮（reports/140 §13.7 / §14.7）。

## 这个文件钉的是什么

1. **`update.py` 行数只许降**。god class 拆分是长期工作，行数是唯一便宜的进度
   指标；它一旦反弹，说明有人又把逻辑写回 `IncrementalUpdater` 里了。
   ⚠️ 上限**只许下调**：拆走一块就把它调小；确需上调必须在 PR 说明里写清理由
   （比如搬回一段被别人误删的必要逻辑）。
2. **`inspect.getsource` 调用点只许减**。这类断言防的不是行为，而是"代码写在
   哪个函数体里"——它会让"把方法搬进 `update_parts/`"这种零行为变更的重构
   莫名其妙变红（§13.7 的死结）。新增一处必须在 `GETSOURCE_REGISTRY` 登记
   类别与理由，否则本测试失败。
3. **`_retry_failed_tasks` 必须还是薄委托**，且新模块不许反向 import
   `app.core.update`（循环依赖）。

## 为什么是"棘轮"而不是"清零"

少数守卫确实无法行为化：`data_quality` 不得引用某域（负向耦合，没有行为可跑）、
或者被守卫的函数体有 500 行、真跑要打网络。这些留着，但必须标注清楚
"它保护的是约束还是位置"，让后来者知道哪些可以改、哪些只能等有行为替身。
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

from app.core.update import IncrementalUpdater
from app.core.update_parts import retry_tasks as retry_tasks_module

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_PY = REPO_ROOT / "app" / "core" / "update.py"
TESTS_ROOT = REPO_ROOT / "tests"

# ── 棘轮 1：update.py 行数（只许降）──────────────────────────────────────
# 2026-11-19（本会话）交付值：6457 → 6156（`_retry_failed_tasks` 306 行搬进
# `update_parts/retry_tasks.py`，原地留 5 行同名委托）。**这个数字只许降。**
UPDATE_PY_MAX_LINES = 6156

# ── 棘轮 2：getsource 调用点（只许减，新增必须登记）──────────────────────
# 键 = 相对仓库根的路径；值 = (允许的调用点数, 类别, 理由)
#   类别 "保护约束" = 保护的是架构/契约约束（负向耦合），没有行为替身可写
#   类别 "保护位置" = 保护的是"这段代码写在哪个函数体里"（反重构死结）
#   类别 "收敛层"   = `tests/support/source_guard.py` 自身的唯一实现调用
GETSOURCE_REGISTRY: dict[str, tuple[int, str, str]] = {
    "tests/regression/test_business_overview_domain.py": (
        1, "保护约束", "data_quality 不得引用业务概览域（负向耦合，无行为可跑）",
    ),
    "tests/regression/test_capital_history_domain.py": (
        1, "保护约束", "data_quality 不得引用股本链域（负向耦合）",
    ),
    "tests/regression/test_funding_and_index_valuation_domain.py": (
        1, "保护约束", "data_quality 不得引用融资/指数估值域（负向耦合）",
    ),
    "tests/regression/test_treasury_curve_domain.py": (
        1, "保护约束", "data_quality 不得引用国债曲线域（负向耦合）",
    ),
    "tests/regression/test_extended_screening_exposure.py": (
        1, "保护位置", "/indicators 必须基于 EXTENDED_SCREENING_READY 且不得用 "
        "EXTENDED_COLUMNS：调用端点需 Request 对象，暂无行为替身",
    ),
    "tests/regression/test_screening_rank_mode.py": (
        1, "保护位置", "/indicators 不得再为 RANKABLE_INDICATORS 铺排名派生字段："
        "同上，暂无行为替身",
    ),
    "tests/regression/test_null_reason_placement.py": (
        1, "保护位置", "reason_distribution 必须引用 NULL_REASON_TABLE（AST 级，"
        "强于文本，但仍绑函数落点）",
    ),
    "tests/support/source_guard.py": (
        1, "收敛层", "resolve_body/assert_calls 需要解析函数体，是本仓唯一合法调用点",
    ),
}


def _getsource_calls(path: Path) -> int:
    with warnings.catch_warnings():
        # 有些回归测试文件里的正则字符串不是 raw string（触发 SyntaxWarning），
        # 这里的 ast.parse 只是数调用点，不需要把它们的告警打出来。
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"))
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "getsource":
            total += 1
    return total


def test_update_py_line_count_only_goes_down() -> None:
    """`update.py` 行数不得超过交付值；这个数字只许降。"""
    lines = len(UPDATE_PY.read_text(encoding="utf-8").splitlines())
    assert lines <= UPDATE_PY_MAX_LINES, (
        f"app/core/update.py 现有 {lines} 行 > 棘轮上限 {UPDATE_PY_MAX_LINES}："
        "god class 又在长回去了。请把新逻辑放进 app/core/update_parts/，"
        "或在 PR 说明里写明为什么必须上调上限。"
    )


def test_getsource_sites_match_registry() -> None:
    """全仓 `getsource` 调用点必须与登记表逐文件对得上（总量是棘轮）。"""
    found: dict[str, int] = {}
    for path in TESTS_ROOT.rglob("*.py"):
        count = _getsource_calls(path)
        if count:
            found[path.relative_to(REPO_ROOT).as_posix()] = count

    unregistered = sorted(set(found) - set(GETSOURCE_REGISTRY))
    assert not unregistered, (
        f"这些文件新增了 getsource 断言却没登记: {unregistered}。"
        "先问自己「这能用行为断言吗」（tests/support/source_guard.py 里的 "
        "constructed_with / signature_of / spy_calls / assert_calls）；"
        "确实不能，再在 GETSOURCE_REGISTRY 登记类别与理由。"
    )
    stale = sorted(set(GETSOURCE_REGISTRY) - set(found))
    assert not stale, f"登记表里的文件已经没有 getsource 了，请删掉登记: {stale}"

    for path, (allowed, category, reason) in GETSOURCE_REGISTRY.items():
        assert found[path] <= allowed, (
            f"{path} 现有 {found[path]} 处 getsource > 允许 {allowed}"
            f"（类别：{category}／{reason}）"
        )

    total = sum(found.values())
    cap = sum(allowed for allowed, *_ in GETSOURCE_REGISTRY.values())
    assert total <= cap, f"全仓 getsource 调用点 {total} > 棘轮上限 {cap}（只许减）"
    # 交付时基线：8 处（7 处回归测试 + 1 处收敛层自身）。比上一轮 13 处少 5 处，
    # 全部是"保护实现位置"的那批，已换成行为断言（见 reports/140 §13.7）。
    assert total == 8, f"全仓 getsource 调用点 {total} != 交付基线 8：棘轮只许往小改"


def test_retry_failed_tasks_is_a_thin_delegate() -> None:
    """`_retry_failed_tasks` 必须保留同名委托，实现体在 update_parts 里。"""
    from tests.support import source_guard

    body = source_guard.resolve_body(IncrementalUpdater._retry_failed_tasks)

    assert body is retry_tasks_module.retry_failed_tasks, (
        "_retry_failed_tasks 不再是『同名委托 → update_parts.retry_tasks』："
        "要么逻辑被搬回了 update.py（god class 反弹），要么委托被改坏"
    )


def test_retry_tasks_module_has_no_reverse_dependency() -> None:
    """`update_parts/**` 禁止 `from app.core.update import ...`（循环依赖）。"""
    offenders: list[str] = []
    for path in (REPO_ROOT / "app" / "core" / "update_parts").rglob("*.py"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if module and "app.core.update" in module.split(","):
                offenders.append(f"{path.name}:{node.lineno} -> {module}")
    assert not offenders, (
        f"update_parts 反向 import 了 app.core.update（会成循环依赖）: {offenders}"
    )
