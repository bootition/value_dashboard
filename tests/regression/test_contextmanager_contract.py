"""上下文管理器契约守卫（2026-11-19 事故）。

事故
----
`reports/147` 的「跨会话正式库写租约」同一提交里同时踩了两个**互补**的错误：

1. `exclusive_update` 是**生成器**（函数体里有 `yield`），却被调用方
   `with exclusive_update(...)` 当成上下文管理器用 —— **漏了**
   `@contextlib.contextmanager`。于是 `with` 直接抛
   `TypeError: 'generator' object does not support the context manager protocol`。
2. `_lease_path` 是**纯路径计算**（`return database_path.parent / ...`），却**多加了**
   `@contextlib.contextmanager`。于是它返回 `_GeneratorContextManager` 而不是 `Path`，
   下游 `lease.parent.mkdir(...)` 抛
   `AttributeError: '_GeneratorContextManager' object has no attribute 'parent'`。

两个错误**互相掩盖**：只修一个，另一个才暴露。最终后果是**每一个**
`with exclusive_update(...)` 的调用方全部崩掉 —— CLI 的 `data auto-update run`、
各类重建命令、ROCE 重建。而它在本机只表现为 22 个 ROCE 测试红，
很容易被当成"测试环境问题"糊过去（当时我第一反应也是怀疑自己）。

为什么值得一条守卫
------------------
静态可判：装饰器与函数体是否自洽，`ast` 两分钟就能查全仓。
这类错误 ruff/flake8 **都不报**（`B027` 之类只管空函数体），只能靠跑起来才发现，
而跑起来往往只暴露"某个域红了"，很难一眼看出根因。

判据（两条，都是零误报）
------------------------
A. 被 `@contextmanager`（或 `@contextlib.contextmanager`）装饰的函数**必须**含 `yield`；
   否则它根本不是上下文管理器，返回的是个"永远不执行函数体"的生成器对象。
B. 函数体含 `yield`（= 生成器）、**没有**被 `@contextmanager` 装饰，
   且**名字在 `with <name>(...)` 里出现过** → 漏装饰。
   （只看"被 with 用过"的，避免把合法的生成器/迭代器误报成错误。）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def _is_contextmanager_decorator(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "contextmanager"
    return isinstance(node, ast.Name) and node.id == "contextmanager"


def _scan() -> tuple[list[str], list[str]]:
    """返回 `(假上下文管理器, 漏装饰的生成器)` 两个问题清单。"""
    fake: list[str] = []
    missing: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # 语法错的文件由 ruff/编译期负责，这里不重复报
            continue
        with_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                        with_names.add(expr.func.id)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorated = any(_is_contextmanager_decorator(d) for d in node.decorator_list)
            has_yield = any(
                isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)
            )
            try:
                shown = path.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:  # 变异验证时注入的文件在 tmp 目录下
                shown = path.as_posix()
            location = f"{shown}:{node.lineno}"
            if decorated and not has_yield:
                fake.append(f"{location} {node.name} 被 @contextmanager 装饰但没有 yield")
            if has_yield and not decorated and node.name in with_names:
                missing.append(
                    f"{location} {node.name} 是生成器却被 `with` 使用，但没有 @contextmanager"
                )
    return fake, missing


def test_no_contextmanager_decorated_without_yield() -> None:
    """A：装饰了 `@contextmanager` 却没有 `yield` —— 它返回的不是上下文管理器。"""
    fake, _ = _scan()
    assert fake == [], (
        "发现假上下文管理器（`_lease_path` 事故形态）：\n  " + "\n  ".join(fake)
    )


def test_no_generator_used_as_context_manager_without_decorator() -> None:
    """B：生成器被 `with` 使用却没装饰 —— `with` 会抛 TypeError。"""
    _, missing = _scan()
    assert missing == [], (
        "发现漏装饰的上下文管理器（`exclusive_update` 事故形态）：\n  "
        + "\n  ".join(missing)
    )


def test_guard_actually_detects_both_shapes(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """变异验证：守卫自己必须会红，否则它就是个摆设。

    用临时目录替换 `APP_ROOT` 来注入两种坏形态，避免改真实源码。
    """
    broken = tmp_path / "app"
    broken.mkdir()
    (broken / "bad.py").write_text(
        "import contextlib\n"
        "\n"
        "@contextlib.contextmanager\n"
        "def fake_cm(path):\n"
        "    return path.parent\n"          # 没有 yield → 假 CM
        "\n"
        "def real_generator(path):\n"
        "    yield path\n"                  # 生成器却没装饰
        "\n"
        "def use():\n"
        "    with real_generator('x') as value:\n"
        "        return value\n",
        encoding="utf-8",
    )
    # 用 sys.modules[__name__] 直接改本模块的全局：
    # pytest 的 rootdir 导入方式下模块名不一定是 `tests.regression.xxx`，
    # 用字符串路径可能解析到**另一个模块对象**，于是 _scan 读到的还是旧值。
    monkeypatch.setattr(sys.modules[__name__], "APP_ROOT", broken)

    fake, missing = _scan()
    assert any("fake_cm" in item for item in fake), fake
    assert any("real_generator" in item for item in missing), missing
