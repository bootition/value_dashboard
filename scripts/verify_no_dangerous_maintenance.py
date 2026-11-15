"""静态检查：`scripts/` 下不得存在未加守卫的主表 DELETE。

背景（reports/120 §5.6）
------------------------
`scripts/patch_deducted_profit.py` 曾执行

    DELETE FROM income_statement WHERE report_date > '2025-03-31'

再从 CSV 回灌。CSV 不完整就会把三表打空。该脚本已移入
`scripts/legacy_dangerous/`，但同类写法可能被复制回来。本检查把
"危险维护脚本"变成可执行门禁：

- 扫描范围：`scripts/**/*.py`，递归，跳过 `legacy_dangerous/`；
- 危险对象：`DELETE FROM <主表>`，主表 =
  `income_statement | balance_sheet | cash_flow |
   price_daily_raw | price_daily_qfq | indicator_snapshot`；
- 守卫启发式（与任务书一致）：
  * 同一个函数内必须出现 `WHERE`（或命中显式全表重载白名单）；
  * 同一个函数内必须出现 `--yes` / `dry_run` / `confirm_destructive`
    等显式确认标记。
- 任一条件不满足 → 打印违规清单并退出 1。

已知局限（有意为之，避免误报）
------------------------------
- 只检查 `xxx.execute("...")` 的第一个参数是字符串常量的调用；
  变量拼接 / 动态表名（如 `DELETE FROM {table}`）无法静态判定，
  不在此检查范围——这类脚本需人工评审。
- 文档字符串里的示例 SQL 不会被当成真实执行（只看 AST Call 参数）。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
EXCLUDED_DIR_NAMES = frozenset({"legacy_dangerous", "__pycache__"})

MAIN_TABLES = frozenset({
    "income_statement",
    "balance_sheet",
    "cash_flow",
    "price_daily_raw",
    "price_daily_qfq",
    "indicator_snapshot",
})

# 显式白名单：整表 DELETE 后立即全量重载的导入脚本。
# 键 = 相对 scripts/ 的 POSIX 路径 + 表名；新增条目必须人工评审后登记。
FULL_TABLE_RELOAD_ALLOWLIST = frozenset({
    "import_csmar.py:balance_sheet",
    "import_csmar.py:income_statement",
    "import_csmar.py:cash_flow",
})

_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_GUARD_RE = re.compile(
    r"--yes|dry[_-]?run|\bdry_run\b|args\.yes|confirm_destructive",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DeleteFinding:
    """一条 `DELETE FROM` 主表语句及其守卫判定。"""

    path: Path
    line: int
    table: str
    function: str
    sql: str
    has_where: bool
    has_guard: bool
    allowlisted: bool

    @property
    def violation(self) -> bool:
        # 全表重载白名单只替代 WHERE 条件，仍然要求显式 --yes/dry_run。
        scope_guarded = self.has_where or self.allowlisted
        return not scope_guarded or not self.has_guard

    def describe(self) -> str:
        state = "VIOLATION" if self.violation else "ok"
        return (
            f"[{state}] {self.path.name}:{self.line} "
            f"table={self.table} function={self.function or '<module>'} "
            f"where={self.has_where} guard={self.has_guard} "
            f"allowlisted={self.allowlisted}"
        )


def _string_literal(node: ast.AST | None) -> str | None:
    """Return the literal text of a str / f-string node, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        chunks: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                chunks.append(value.value)
            else:
                chunks.append("?")
        return "".join(chunks)
    return None


class _DeleteCallVisitor(ast.NodeVisitor):
    """收集 execute("DELETE FROM ...") 调用及其所在函数。"""

    def __init__(self) -> None:
        self._function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.calls: list[
            tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None, str]
        ] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._function_stack.append(node)
        self.generic_visit(node)
        self._function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._function_stack.append(node)
        self.generic_visit(node)
        self._function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        is_execute = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany"}
        )
        sql = _string_literal(node.args[0]) if node.args else None
        if is_execute and sql is not None:
            function = self._function_stack[-1] if self._function_stack else None
            self.calls.append((node, function, sql))
        self.generic_visit(node)


def _relative_key(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def scan_script(path: Path, *, root: Path) -> list[DeleteFinding]:
    """Return every main-table DELETE found in one script (may be empty)."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error

    visitor = _DeleteCallVisitor()
    visitor.visit(tree)

    findings: list[DeleteFinding] = []
    for call, function, sql in visitor.calls:
        matches = list(_DELETE_RE.finditer(sql))
        if not matches:
            continue
        if function is not None:
            function_source = ast.get_source_segment(source, function) or ""
            function_name = function.name
        else:
            function_source = source
            function_name = "<module>"
        has_where = _WHERE_RE.search(function_source) is not None
        has_guard = _GUARD_RE.search(function_source) is not None
        for match in matches:
            table = match.group(1).lower()
            if table not in MAIN_TABLES:
                continue
            findings.append(
                DeleteFinding(
                    path=path,
                    line=call.lineno,
                    table=table,
                    function=function_name,
                    sql=sql.strip(),
                    has_where=has_where,
                    has_guard=has_guard,
                    allowlisted=(
                        f"{_relative_key(path, root)}:{table}"
                        in FULL_TABLE_RELOAD_ALLOWLIST
                    ),
                )
            )
    return findings


def _iter_scripts(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ValueError(f"scripts root not found: {root}")
    scripts: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        relative_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIR_NAMES for part in relative_parts):
            continue
        scripts.append(path)
    return scripts


def scan_directory(root: Path = DEFAULT_SCRIPTS_ROOT) -> tuple[list[Path], list[DeleteFinding]]:
    """Scan all maintenance scripts; returns (files, findings)."""
    scripts = _iter_scripts(root)
    findings: list[DeleteFinding] = []
    for path in scripts:
        findings.extend(scan_script(path, root=root))
    return scripts, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 scripts/ 下主表 DELETE 是否带 WHERE + 显式确认守卫",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_SCRIPTS_ROOT,
        help=f"要扫描的 scripts 根目录（默认 {DEFAULT_SCRIPTS_ROOT}）",
    )
    args = parser.parse_args(argv)

    try:
        scripts, findings = scan_directory(args.root.resolve())
    except ValueError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    violations = [finding for finding in findings if finding.violation]
    print(
        f"扫描 {len(scripts)} 个脚本，发现 {len(findings)} 条主表 DELETE，"
        f"违规 {len(violations)} 条"
    )
    for finding in findings:
        print(f"  {finding.describe()}")
    for finding in violations:
        print(
            f"[FAIL] {finding.path}:{finding.line} `{finding.sql}` 缺少 "
            "`WHERE` 和/或 `--yes`/`dry_run` 守卫（同一函数内）",
            file=sys.stderr,
        )
    if violations:
        return 1
    print("[OK] scripts/ 下没有未守卫的主表 DELETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
