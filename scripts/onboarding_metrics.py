"""认知成本度量（2026-11-17，`reports/134`）。

用途
----
"新人接手这个项目要花多久"不该靠感觉。本脚本把**可测的认知成本**固定成一组指标，
用于：
1. 给复盘报告提供数字（`reports/134` 的每一行都有出处）；
2. 每完成一个简化阶段重跑一次，用 delta 证明"真的变简单了"，而不是"改了很多文件"。

**故意不做成门禁**：门禁本身也是认知成本，而且这些指标（文件行数、类方法数）不是
"对错"，只是"贵不贵"。需要拦截时用 `--fail-on-regression` 显式打开。

用法
----
    python scripts/onboarding_metrics.py                 # 人类可读
    python scripts/onboarding_metrics.py --json          # 机器可读
    python scripts/onboarding_metrics.py --compare docs/reference/onboarding-baseline-2026-11-17.json
    python scripts/onboarding_metrics.py --compare <baseline> --fail-on-regression

指标口径
--------
- `app_loc` / `app_files`：`app/**/*.py` 行数与文件数（不含 __pycache__）
- `top_file_loc`：最大单文件行数（前 5 名列在 `top_files`）
- `functions_gt_150` / `_80`：函数体行数超过阈值的个数（AST，含嵌套）
- `widest_class`：方法数最多的类（名字 + 方法数 + 行数）
- `god_modules`：超过 1000 行的模块数
- `duplicate_helper_names`：同名顶层函数出现在 >1 个模块的组数（复制粘贴信号）
- `report_refs_in_code`：`app/` 里 `reports/NNN` 引用次数（"必须读报告才能懂代码"）
- `day1_docs_chars`：AGENTS/STATUS/README/知识库/PRD/合同的字符数（必读入口）
- `superseded_reports`：`docs/reports/` 中已被取代的报告数（新人必须先判断哪些别读）
- `incident_named_tests`：以 `test_fix<日期>_` / `test_p<数字>_` 命名（按事故而非行为命名）
- `import_cycles`：`app/` 内部循环导入组数
- `closure_<entry>`：从入口模块出发的可达 app 模块数 / LOC（"读懂这条链要读多少"）
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import re
from dataclasses import asdict, dataclass, field

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_PARTS = {"__pycache__"}

ENTRY_POINTS = (
    "app.core.update",
    "app.cli.main",
    "app.web.main",
    "app.core.indicators.calculator",
)
DAY1_DOCS = (
    "AGENTS.md",
    "docs/STATUS.md",
    "docs/README.md",
    "docs/runbooks/ops-knowledge-base.md",
    "docs/reference/CODE_MAP.md",
    "docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md",
    "docs/contracts/path-isolation-contract.md",
)


@dataclass
class Metrics:
    app_files: int = 0
    app_loc: int = 0
    top_file_loc: int = 0
    top_files: list[str] = field(default_factory=list)
    functions_gt_150: int = 0
    functions_gt_80: int = 0
    widest_class: str = ""
    widest_class_methods: int = 0
    god_modules: int = 0
    duplicate_helper_names: int = 0
    duplicate_helper_examples: list[str] = field(default_factory=list)
    report_refs_in_code: int = 0
    day1_docs_chars: int = 0
    superseded_reports: int = 0
    total_reports: int = 0
    incident_named_tests: int = 0
    total_test_files: int = 0
    import_cycles: int = 0
    closures: dict[str, str] = field(default_factory=dict)


def _py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [
        path
        for path in root.rglob("*.py")
        if not SKIP_PARTS & set(path.parts)
    ]


def _loc(path: pathlib.Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _module_name(root: pathlib.Path, path: pathlib.Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _import_graph(root: pathlib.Path, modules: dict[str, pathlib.Path]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            graph[name] = set()
            continue
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                deps.add(node.module)
            elif isinstance(node, ast.Import):
                deps.update(a.name for a in node.names if a.name.startswith("app"))
        graph[name] = {d for d in deps if d in modules}
    return graph


def _closure(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen or current not in graph:
            continue
        seen.add(current)
        stack.extend(graph[current] - seen)
    return seen


def collect(root: pathlib.Path = PROJECT_ROOT) -> Metrics:
    metrics = Metrics()
    app_dir = root / "app"
    app_files = _py_files(app_dir)
    modules = {_module_name(root, path): path for path in app_files}

    metrics.app_files = len(app_files)
    sizes = sorted(((_loc(path), path) for path in app_files), reverse=True)
    metrics.app_loc = sum(size for size, _ in sizes)
    if sizes:
        metrics.top_file_loc = sizes[0][0]
        metrics.top_files = [
            f"{size} {path.relative_to(root).as_posix()}" for size, path in sizes[:5]
        ]
    metrics.god_modules = sum(1 for size, _ in sizes if size > 1000)

    duplicate_names: dict[str, list[str]] = collections.defaultdict(list)
    widest = ("", 0, 0)
    for path in app_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
                if length > 150:
                    metrics.functions_gt_150 += 1
                if length > 80:
                    metrics.functions_gt_80 += 1
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                duplicate_names[node.name].append(path.name)
            elif isinstance(node, ast.ClassDef):
                methods = sum(
                    1 for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
                length = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
                if methods > widest[1]:
                    widest = (f"{path.relative_to(root).as_posix()}::{node.name}", methods, length)
    metrics.widest_class, metrics.widest_class_methods = widest[0], widest[1]

    duplicates = {
        name: files for name, files in duplicate_names.items()
        if len(set(files)) > 1
    }
    metrics.duplicate_helper_names = len(duplicates)
    metrics.duplicate_helper_examples = [
        f"{name} ×{len(set(files))}"
        for name, files in sorted(duplicates.items(), key=lambda kv: -len(set(kv[1])))[:5]
    ]

    report_refs = 0
    for path in app_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        report_refs += len(re.findall(r"reports/\d{1,3}", text))
    metrics.report_refs_in_code = report_refs

    metrics.day1_docs_chars = sum(
        len((root / rel).read_text(encoding="utf-8"))
        for rel in DAY1_DOCS
        if (root / rel).exists()
    )

    reports = sorted((root / "docs/reports").glob("*.md"))
    metrics.total_reports = len(reports)
    metrics.superseded_reports = sum(
        1 for path in reports
        if re.search(r"^status:\s*superseded", path.read_text(encoding="utf-8", errors="replace"), re.M)
    )

    tests = sorted((root / "tests/regression").glob("*.py"))
    metrics.total_test_files = len(tests)
    metrics.incident_named_tests = sum(
        1 for path in tests if re.match(r"test_(fix\d{8}|p\d)_", path.name)
    )

    graph = _import_graph(root, modules)
    cycles: set[frozenset[str]] = set()
    for name in graph:
        for dep in graph[name]:
            if name in graph.get(dep, set()):
                cycles.add(frozenset({name, dep}))
    metrics.import_cycles = len(cycles)

    for entry in ENTRY_POINTS:
        reached = _closure(graph, entry)
        loc = sum(_loc(modules[module]) for module in reached if module in modules)
        metrics.closures[entry] = f"{len(reached)} modules / {loc} LOC"

    return metrics


def _flat(metrics: Metrics) -> dict[str, object]:
    data = asdict(metrics)
    data.pop("closures", None)
    data.update({f"closure::{key}": value for key, value in metrics.closures.items()})
    data.pop("top_files", None)
    data.pop("duplicate_helper_examples", None)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="认知成本度量（reports/134）")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--compare", help="与基线 JSON 比较（打印 delta）")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="配合 --compare：任一指标变差则退出码 1（默认只报告）")
    args = parser.parse_args(argv)

    metrics = collect(pathlib.Path(args.root).resolve())
    current = _flat(metrics)

    if args.compare:
        baseline_raw = json.loads(pathlib.Path(args.compare).read_text(encoding="utf-8"))
        baseline = baseline_raw.get("metrics", baseline_raw)
        worse: list[str] = []
        print(f"{'指标':38s} {'基线':>12s} {'当前':>12s}  变化")
        for key, value in current.items():
            old = baseline.get(key)
            if not isinstance(value, (int, float)) or not isinstance(old, (int, float)):
                continue
            delta = value - old
            flag = ""
            if delta > 0:
                flag = "  ← 变差"
                worse.append(f"{key}: {old} → {value}")
            elif delta < 0:
                flag = "  ← 改善"
            print(f"{key:38s} {old:12d} {value:12d}  {delta:+d}{flag}")
        if worse and args.fail_on_regression:
            print("\n认知成本回归：\n  " + "\n  ".join(worse))
            return 1

    if args.json:
        print(json.dumps(
            {"schema_version": 1, "metrics": current, "top_files": metrics.top_files,
             "duplicate_helpers": metrics.duplicate_helper_examples},
            ensure_ascii=False, indent=2,
        ))
    elif not args.compare:
        print(f"app: {metrics.app_files} files / {metrics.app_loc} LOC")
        print(f"最大文件: {metrics.top_file_loc} LOC  ({metrics.top_files[0] if metrics.top_files else '-'})")
        for line in metrics.top_files:
            print(f"    {line}")
        print(f"函数 >150 LOC: {metrics.functions_gt_150}   >80 LOC: {metrics.functions_gt_80}")
        print(f"方法最多的类: {metrics.widest_class} ({metrics.widest_class_methods} methods)")
        print(f">1000 行模块: {metrics.god_modules}")
        print(f"同名顶层函数(跨模块): {metrics.duplicate_helper_names}  {metrics.duplicate_helper_examples}")
        print(f"代码里 reports/NNN 引用: {metrics.report_refs_in_code}")
        print(f"Day-1 必读文档: {metrics.day1_docs_chars} chars")
        print(f"报告: {metrics.superseded_reports}/{metrics.total_reports} superseded")
        print(f"测试: {metrics.incident_named_tests}/{metrics.total_test_files} 按事故命名")
        print(f"循环导入: {metrics.import_cycles}")
        for entry, value in metrics.closures.items():
            print(f"可达闭包 {entry:38s} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
