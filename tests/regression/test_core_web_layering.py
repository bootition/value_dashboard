"""分层契约：`app/core/` 不得反向依赖 `app/web/`。

背景（reports/124 §5）：`app/core/data_quality.py::actionable_missing_count()`
曾经 `from app.web.api.data_status import _actionable_missing_condition`
（注释自认"惰性导入避免成环"）—— 这是全仓唯一一处 core→web 层反转，也是
"core 能被 CLI 单独使用"这条设计的破口。修复方式是把口径下沉到
`app/core/missing_conditions.py`，web 改为从 core 导入。

本文件用 AST 静态扫描钉死这条约束：任何 `import app.web...` /
`from app.web... import ...`（包括函数体内的惰性导入）都算违规。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CORE_ROOT = _PROJECT_ROOT / "app" / "core"
_WEB_PREFIX = "app.web"

#: 行首 import 语句，或对 "app.web..." 的动态导入调用。
_WEB_IMPORT_PATTERN = re.compile(
    r"""^\s*(?:from|import)\s+app\.web"""
    r"""|(?:import_module|__import__)\(\s*["']app\.web"""
)


def _iter_core_modules() -> list[Path]:
    return sorted(
        path
        for path in _CORE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _web_imports(path: Path) -> list[str]:
    """Return `path:lineno  <imported module>` for every core→web import."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith(_WEB_PREFIX):
                offenders.append(f"{path}:{node.lineno} from {module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_WEB_PREFIX):
                    offenders.append(f"{path}:{node.lineno} import {alias.name}")
    return offenders


def test_core_never_imports_the_web_layer() -> None:
    modules = _iter_core_modules()
    assert modules, "app/core 下没找到任何模块，扫描路径可能写错了"

    offenders: list[str] = []
    for path in modules:
        offenders.extend(_web_imports(path))

    assert offenders == [], (
        "app/core 不得 import app/web（层反转）。请把共享逻辑下沉到 app/core/：\n"
        + "\n".join(offenders)
    )


def test_actionable_missing_condition_is_single_sourced_from_core() -> None:
    """web 侧只保留向后兼容别名，真源在 core，且两处是同一个函数对象。"""
    from app.core.missing_conditions import (
        DISABLED_MISSING_FIELDS,
        actionable_missing_condition,
    )
    from app.web.api import data_status

    assert data_status._actionable_missing_condition is actionable_missing_condition
    assert set(data_status._DISABLED_MISSING_FIELDS) == set(DISABLED_MISSING_FIELDS)

    condition, params = actionable_missing_condition()
    assert condition.count("?") == len(params) == len(DISABLED_MISSING_FIELDS)
    assert set(params) == set(DISABLED_MISSING_FIELDS)
    # 口径本体必须留在 SQL 里（禁用字段走参数，不拼接字面量）
    assert "placement_funding" in condition


def test_core_sources_do_not_even_mention_web_imports() -> None:
    """源码级 grep 兜底：core 里连 `from app.web` / `import app.web` 都不许出现。

    AST 用例只认真正的 import 语句；这条正则用例额外挡住
    `importlib.import_module("app.web...")` / `__import__("app.web...")`
    这类绕法（以及有人把惰性导入塞回函数体）。只匹配"行首 import"或
    importlib 调用，因此文档里描述这次修复的散文不会误报。
    """
    offenders: list[str] = []
    for path in _iter_core_modules():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _WEB_IMPORT_PATTERN.search(line):
                offenders.append(f"{path}:{lineno} {line.strip()}")
    assert offenders == [], "app/core 源码里出现 web 层导入：\n" + "\n".join(offenders)
