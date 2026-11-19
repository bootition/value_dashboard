"""行为级测试替身：取代 `inspect.getsource(...)` 的"实现位置"断言。

## 为什么不再用 `getsource` 断言位置

`inspect.getsource(obj)` + `"某符号" in source` 断言的**不是行为**，而是
"这行代码此刻写在哪个函数体里"。它把两件本该独立的事绑死了：

1. **行为契约**——"构造 updater 时注入了共享 `adapter_mgr`"、"qfq 补齐后把
   股票写进技术指标待重算名单"；
2. **代码落点**——"这段逻辑留在 `app/core/update.py::IncrementalUpdater` 的
   某个方法体内"。

第 2 件事本是自由变量，却被测试变成了隐式契约：一旦把方法体搬进
`app/core/update_parts/*`（reports/140 §13.7 / §14.7 的反重构死结），
`getsource` 拿到的是 3 行委托壳，断言必然变红——**重构被测试焊死**，
而真正的行为回归它一个也拦不住：把 `adapter=self.adapter_mgr` 改成
`adapter=AdapterManager()` 时，只要文本还在（比如挪进注释或另一个分支），
`getsource` 照样绿。

本模块提供三类**行为级**替身，让断言重新落在"跑起来发生什么"上：

* :func:`constructed_with` —— 用记录 kwargs 的替身顶掉真构造器，断言
  "这次构造注入了共享 manager"（而不是"源码里出现了 `adapter=self.adapter_mgr`"）。
* :func:`signature_of` —— 用 `inspect.signature` 断言参数契约
  （而不是在源码文本里 index 参数名）。
* :func:`spy_calls` —— 包住真方法记录调用后照常委托，用"这个协作者真的被调用了"
  代替"这个名字出现在某函数源码里"。

## 两类残留（无法行为化时的退路）

少数守卫防的是"某个协作者必须被调用"这一**接线事实**，而被守卫的函数体有
500 行、跑一轮要真网络。这类用 :func:`assert_calls`：它把函数体解析成 AST，
断言**存在真实的调用节点**（`self._mark_technical_pending(...)`），而不是
"字符串出现在文本里"——注释、日志文案、docstring 里的同名文字不再造成假绿。
并且 :func:`resolve_body` 会顺着"同名单行委托"往下追一层：

    def _update_prices_incremental(self, ...):      # update.py 里的委托壳
        return _update_prices_incremental_impl(self, ...)   # update_parts/prices.py

于是**方法体搬走后断言仍指向真正的实现**，不再因为搬家而变红。
`assert_calls` 仍是结构级守卫，所以每处都必须登记在
`tests/regression/test_update_structure.py` 的 `GETSOURCE_REGISTRY` 里并标注类别。
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

__all__ = [
    "ConstructionDouble",
    "ConstructionRecord",
    "assert_calls",
    "constructed_with",
    "count_construction_sites",
    "resolve_body",
    "signature_of",
    "spy_calls",
]


class ConstructionDouble:
    """被构造出来的替身对象：所有常见 updater 方法都返回成功。"""

    def __init__(self, record: ConstructionRecord, kwargs: dict[str, Any]) -> None:
        self.record = record
        self.kwargs = kwargs

    # 各 updater 的入口方法名合集（鸭子类型替身，不需要真的继承谁）。
    def refresh_all(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def refresh_if_due(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": "test_double"}

    def update_all(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def update_stock(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def update_daily(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def update_sw_industries(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def update_etf(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def _backfill_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def __repr__(self) -> str:  # pragma: no cover - 只为失败信息好读
        return f"<ConstructionDouble {self.record.label} kwargs={sorted(self.kwargs)}>"


class ConstructionRecord:
    """记录某个类在测试期间**每一次**构造的 kwargs。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[dict[str, Any]] = []

    def _factory(self, *args: Any, **kwargs: Any) -> ConstructionDouble:
        if args:
            raise AssertionError(
                f"{self.label} 被用位置参数构造（{args!r}）：本仓 updater 一律关键字构造，"
                "位置参数会让注入审查失效"
            )
        self.calls.append(dict(kwargs))
        return ConstructionDouble(self, dict(kwargs))

    # ── 断言 ────────────────────────────────────────────────────────────
    @property
    def count(self) -> int:
        return len(self.calls)

    def assert_count(self, expected: int) -> None:
        assert self.count == expected, (
            f"{self.label} 构造次数 {self.count} != {expected}（实际 kwargs: {self.calls}）"
        )

    def assert_injected(self, name: str, expected: Any) -> None:
        """每一处构造都必须把 ``name`` 注入成同一个 ``expected`` 对象。"""
        assert self.calls, f"{self.label} 一次都没被构造：注入点被删了或走不到"
        for index, kwargs in enumerate(self.calls):
            assert name in kwargs, (
                f"{self.label} 第 {index + 1} 处构造未注入 {name}="
                f"（实际 kwargs: {sorted(kwargs)}）；自建实例会让熔断状态无法共享"
            )
            assert kwargs[name] is expected, (
                f"{self.label} 第 {index + 1} 处构造注入的 {name} 不是共享实例"
                f"（{kwargs[name]!r} is not {expected!r}）"
            )

    def assert_not_passing(self, name: str) -> None:
        for index, kwargs in enumerate(self.calls):
            assert name not in kwargs, (
                f"{self.label} 第 {index + 1} 处构造不应传 {name}=（{kwargs[name]!r}）"
            )


def constructed_with(
    monkeypatch: Any,
    target: str | type,
    *,
    label: str | None = None,
) -> ConstructionRecord:
    """把 ``target``（点路径字符串或类对象）顶成记录 kwargs 的替身构造器。

    ``target`` 用 **点路径字符串** 时，替身装在"构造发生处所 import 的那个名字"上
    （例如 ``"app.core.business.BusinessOverviewUpdater"``），这样被测代码里的
    `from ... import X` 与模块级 `X(...)` 两种写法都能被拦到。——注意：如果实现
    把 `from ... import X` 写在函数体内，替身同样有效（import 每次都会重新绑定）。
    """
    record = ConstructionRecord(label or getattr(target, "__name__", str(target)))
    monkeypatch.setattr(target, record._factory)
    return record


def signature_of(func: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    """参数契约的唯一来源：`inspect.signature`，不读源码文本。"""
    return dict(inspect.signature(func).parameters)


def spy_calls(monkeypatch: Any, obj: Any, *names: str) -> dict[str, list[tuple]]:
    """在 ``obj`` 上包一层记录器：调用照常委托给原方法，同时记下每次入参。"""

    calls: dict[str, list[tuple]] = {name: [] for name in names}
    for name in names:
        original = getattr(obj, name)
        assert callable(original), f"{obj!r}.{name} 不是可调用对象，无法 spy"

        def wrapper(*args: Any, _name: str = name, _original: Any = original,
                    **kwargs: Any) -> Any:
            calls[_name].append((args, kwargs))
            return _original(*args, **kwargs)

        monkeypatch.setattr(obj, name, wrapper)
    return calls


def count_construction_sites(root: Any, class_name: str) -> int:
    """统计 ``root`` 目录下所有 ``.py`` 里 ``class_name(...)`` 的**调用点数**（AST）。

    用于替代 `inspect.getsource(module).count("X(")`：那种写法既把构造点钉死在
    单一文件，又会被注释/文档字符串里的同名文字干扰。这里跨模块遍历，
    方法体搬到 ``update_parts/`` 也照样数得到。
    """
    import pathlib

    total = 0
    for path in sorted(pathlib.Path(root).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - 不该发生
            continue
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _callee_name(node.func) == class_name
        )
    return total


def resolve_body(func: Callable[..., Any]) -> Callable[..., Any]:
    """顺着"同名单行委托"往下追，拿到真正含方法体的那个函数。

    支持三种壳：

    * ``def f(self, ...): return _impl(self, ...)``      → 追到 ``_impl``
    * ``def f(self, ...): return SomeCls._m(self, ...)`` → 追到类方法
    * 无委托（函数体就是实现）→ 原样返回

    这样把方法体搬进 ``app/core/update_parts/`` 后，结构守卫仍然指向实现，
    不会因为"方法变成 3 行委托"而误报。
    """
    seen: set[int] = set()
    current = func
    for _ in range(6):
        if id(current) in seen:  # pragma: no cover - 循环委托的兜底
            return current
        seen.add(id(current))
        node = _function_node(current)
        if node is None:  # pragma: no cover - C 扩展/builtin
            return current
        target = _delegation_target(node, current)
        if target is None:
            return current
        current = target
    return current  # pragma: no cover


def assert_calls(
    func: Callable[..., Any],
    *required: str,
    forbidden: Iterable[str] = (),
    starred: Mapping[str, int] | Iterable[str] = (),
) -> None:
    """断言 ``func`` 的函数体里**真的调用了**这些名字（AST 调用节点，不是文本）。

    比 `"名字" in inspect.getsource(func)` 强的地方：
    注释 / 日志文案 / docstring / 字符串常量里的同名文字不算数，
    只有 ``ast.Call`` 的被调方（`Name`/`Attribute`）才算。

    ``starred`` 用于"列表必须被 `*` 展开进调用"的形态
    （如 ``calculator(bounded_codes, *tech_pending_codes)``）——只断言
    变量名出现在源码里，是拦不住"读了名单却没并进重算集合"的。
    传映射时值是**最少展开次数**：同一条名单在"有界更新"和"全量更新"两条
    分支里各展开一次，只留一处就等于其中一条路径不再强制重算。
    """
    body = resolve_body(func)
    node = _function_node(body)
    assert node is not None, f"{body!r} 没有可解析的函数体"
    called = {
        _callee_name(sub.func)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
    }
    label = getattr(body, "__qualname__", repr(body))
    missing = [name for name in required if name not in called]
    assert not missing, (
        f"{label} 体内缺少对这些协作者的调用: {missing}"
        f"（实际调用: {sorted(name for name in called if name)}）"
    )
    banned = [name for name in forbidden if name in called]
    assert not banned, f"{label} 体内不应再调用: {banned}"
    wanted = (
        dict(starred) if isinstance(starred, Mapping)
        else {name: 1 for name in starred}
    )
    if wanted:
        expanded: dict[str, int] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Starred) and isinstance(sub.value, ast.Name):
                expanded[sub.value.id] = expanded.get(sub.value.id, 0) + 1
        short = {
            name: (expanded.get(name, 0), count)
            for name, count in wanted.items()
            if expanded.get(name, 0) < count
        }
        assert not short, (
            f"{label} 体内 `*` 展开次数不足（实际, 至少需要）: {short}："
            "名单读了但不并进重算集合 = 强制重算在某条路径上失效"
        )


# ────────────────────────── 内部工具 ──────────────────────────


def _function_node(func: Callable[..., Any]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):  # pragma: no cover - 动态定义
        return None
    module = ast.parse(_dedent(source))
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node
    return None  # pragma: no cover


def _dedent(source: str) -> str:
    import textwrap

    return textwrap.dedent(source)


def _delegation_target(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    func: Callable[..., Any],
) -> Callable[..., Any] | None:
    """函数体只有 ``return <call>`` 时，把 ``<call>`` 的被调方解析回对象。"""
    body = [stmt for stmt in node.body if not isinstance(stmt, ast.Expr)]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    value = body[0].value
    if not isinstance(value, ast.Call):
        return None
    callee = value.func
    if isinstance(callee, ast.Name):
        candidate = func.__globals__.get(callee.id)
    elif isinstance(callee, ast.Attribute):
        holder = callee.value
        owner = func.__globals__.get(holder.id) if isinstance(holder, ast.Name) else None
        candidate = getattr(owner, callee.attr, None) if owner is not None else None
    else:  # pragma: no cover
        return None
    if callable(candidate) and candidate is not func:
        return candidate
    return None


def _callee_name(func_node: ast.expr) -> str | None:
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None
