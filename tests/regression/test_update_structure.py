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

## 为什么还剩 1 处

上一轮把 13 → 8；本轮把剩下的 7 处**全部**改成非文本断言，只剩
`tests/support/source_guard.py` 里的 1 处 —— 它是**收敛层自身的唯一实现**：
`resolve_body` / `assert_calls` 必须把函数体解析成 AST，绕不开
`inspect.getsource`；全仓只有这一个调用点，而它自己就是「位置断言 → 结构断言」
的降级实现。

本轮替换对照（7 处文本断言 → 0）：

* **负向耦合 4 处**（`data_quality` 不得引用某域）→
  `source_guard.assert_no_coupling()`：**import 图**（`ast.walk` 收全部
  `import` / `from ... import`，**含函数体内的延迟 import**）+ **AST 符号表**
  （标识符与字符串常量切片，显式排除 docstring）。旧写法拦文本不拦耦合：
  把表名写进注释就假红，真加延迟 import 未必拦得住。
* **保护位置 3 处** → 行为断言：
  - `/indicators` 暴露范围（extended）：真调 `list_available_indicators`，
    把 ready 收窄一列，断言该列**不出现**（接口若按 `EXTENDED_COLUMNS` 铺
    字段就会红）；
  - `/indicators` 不再下发排名派生字段：真调端点，断言没有任何
    `_{RANK_MODES}` 后缀字段，且 `rank_modes` 覆盖全部口径；
  - `reason_distribution` 读稀疏表：真跑两次（空表 / 有数据 + 诱饵表），
    断言分布逐字段等于稀疏表内容、诱饵表字段一条都不出现。
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
UPDATE_PY_MAX_LINES = 5226

# ── 棘轮 2：getsource 调用点（只许减，新增必须登记）──────────────────────
# 键 = 相对仓库根的路径；值 = (允许的调用点数, 类别, 理由)
#   类别 "保护约束" = 保护的是架构/契约约束（负向耦合），没有行为替身可写
#   类别 "保护位置" = 保护的是"这段代码写在哪个函数体里"（反重构死结）
#   类别 "收敛层"   = `tests/support/source_guard.py` 自身的唯一实现调用
#
# 2026-11-19（本轮）清账：剩 7 处中
#   4 处 "保护约束" → `source_guard.assert_no_coupling()`（import 图 + AST 符号表）
#   3 处 "保护位置" → 行为断言（真调 list_available_indicators / reason_distribution）
# 于是登记表只剩下面这一条收敛层自身；总量上限同步下调 8 → 1。
GETSOURCE_REGISTRY: dict[str, tuple[int, str, str]] = {
    "tests/support/source_guard.py": (
        1, "收敛层",
        "resolve_body/assert_calls 必须把函数体解析成 AST；全仓唯一合法调用点，"
        "它自己就是『位置断言 → 结构断言』的降级实现",
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
    # 交付时基线：**1 处**（只有 tests/support/source_guard.py 这个收敛层自身）。
    # 演进：13 → 8（reports/140 §13.7 的行为化）→ 1（本轮把 4 处负向耦合改成
    # import 图 + AST 符号表、3 处位置守卫改成行为断言）。这个数字只许往小改。
    assert total == 1, f"全仓 getsource 调用点 {total} != 交付基线 1：棘轮只许往小改"


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


# ── 棘轮 3：替代品自身必须能红（变异自检）────────────────────────────────
#
# 本轮把 7 处 `getsource` 文本断言换成了 import 图 / 行为断言。**替代品失效**
# 是这类"清扫"最常见的返工原因（守卫变成永远绿的摆设），所以这里用**喂源码
# 字符串**的方式做等价变异：`source_guard.assert_no_coupling()` 对真实模块和
# 对这段字符串走的是**同一个函数**，因此"含函数体内延迟 import 的源码会被判红"
# 等价于"给真实模块加一处延迟 import 会被判红"。
#
# 为什么不在 `app/core/data_quality.py` 上真做这条变异：该文件正被另一个会话
# 改（内容纪元方案），跨租约改写别人正在编辑的文件会制造假冲突（AGENTS W8）。

_LAZY_IMPORT_MUTANT = '''
"""docstring 里提到 treasury_yield_curve 不算耦合（要能被排除）"""

from __future__ import annotations


def _probe(readiness: dict) -> dict:
    from app.core import treasury  # 函数体内的延迟 import —— 必须被判红

    return {**readiness, "treasury": treasury}
'''

_COMMENT_ONLY_CONTROL = '''
"""模块 docstring：这里说明为什么 data_quality 不得读 treasury_yield_curve。"""

from __future__ import annotations


def _probe(readiness: dict) -> dict:
    # 只是在注释里提一句 treasury_yield_curve —— 不是耦合，不许假红
    return readiness
'''


def test_import_graph_guard_goes_red_on_lazy_import() -> None:
    """喂"含函数体内延迟 import 的源码"给 import 图断言 → 必须红（变异自检）。"""
    import pytest

    from tests.support import source_guard

    with pytest.raises(AssertionError, match="app.core.treasury"):
        source_guard.assert_no_coupling(
            _LAZY_IMPORT_MUTANT,
            forbidden_modules=("app.core.treasury",),
            forbidden_symbols=("treasury_yield_curve",),
        )
    # 函数体内的 import 确实进了 import 图（这就是"上一轮的坑"）
    assert "app.core.treasury" in source_guard.imported_modules(_LAZY_IMPORT_MUTANT)


def test_import_graph_guard_stays_green_on_docstring_mention() -> None:
    """同结构但只出现在 docstring/注释里 → 必须绿（重构改文案不许假红）。"""
    from tests.support import source_guard

    source_guard.assert_no_coupling(
        _COMMENT_ONLY_CONTROL,
        forbidden_modules=("app.core.treasury",),
        forbidden_symbols=("treasury_yield_curve",),
    )


def test_coupling_guard_goes_red_on_sql_literal() -> None:
    """不 import 模块、直接在 SQL 字符串里查该域的表 → 也必须红。"""
    import pytest

    from tests.support import source_guard

    mutant = '''
"""空 docstring"""

from __future__ import annotations


def readiness(store) -> list:
    return store.read_query("SELECT COUNT(*) FROM treasury_yield_curve")
'''

    with pytest.raises(AssertionError, match="treasury_yield_curve"):
        source_guard.assert_no_coupling(
            mutant,
            forbidden_modules=("app.core.treasury",),
            forbidden_symbols=("treasury_yield_curve",),
        )
