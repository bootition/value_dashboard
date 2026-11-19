"""空值归因清扫器的「归属与调用面」守卫（2026-11-19 裁决，`reports/140` §13）。

为什么需要这条守卫
------------------
`null_reason_sweeper` 是**离线写入端**：唯一写库入口是 `scripts/classify_null_reasons.py`
（默认 dry-run，`--yes` 才写，写前查 `any_write_lock_active`，写窗口 `exclusive_update`），
读端则在生产侧（`GET /api/data-status/null-reasons` → `reason_distribution()`
→ `frontend/src/components/ScreeningResultsPanel.vue`）。两侧的关系只在模块 docstring
和 CLI docstring 里写着，没有执行器看着——下面四类事故因此都可能悄悄发生：

1. **dry-run 默认被翻成 `True`**：CLI 承诺的"不加 `--yes` 绝不写库"失效，
   一次预演就往正式库写几万行稀疏记录；`domain` 若给了默认值，CLI 的单域清扫
   也会退化成"全库扫一遍"。
2. **多出一处写者**：有人在更新链或 API 路径里直接 `sweep(..., write=True)`，
   绕过写锁（`any_write_lock_active` / `exclusive_update`）与 CLI 抢 DuckDB 写锁。
3. **迁文件忘改守卫**：`scripts/check_indicator_readiness.py::_SECTOR_CONSUMERS`
   是按 `app/` **路径键**做 `is_bank_sql` 收口的前置检查，文件一搬该检查就判
   BLOCK（`check_indicator_readiness.py:87-93` 文件缺失直接进 problems）；
   更隐蔽的失败是键还留着、文件已不在——守卫静默失效。
4. **读路径被改断**：`reason_distribution()` 不再读 `indicator_null_reason`，或
   `GET /null-reasons` 端点被删/换了数据源，CLI 写进去的东西没人消费，
   回到"清扫了但没人看"的原始状态。

实现纪律：**一律走 AST，不按行 grep**。`null_reason_sweeper.py` 的 docstring「用法」
块里本来就有一行形如 `from app.core.quality.null_reason_sweeper import sweep` 的示例，
按行匹配会把文档当成调用点——2026-11-19 的一次"自 import 死代码"误判正是这么来的
（该行落在 docstring 内，从没被导入过）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.core.quality import null_reason_sweeper as sweeper
from app.core.quality import null_reasons

REPO_ROOT = Path(__file__).resolve().parents[2]
SWEEPER_KEY = "app/core/quality/null_reason_sweeper.py"
SWEEPER_MODULE = "app.core.quality.null_reason_sweeper"
CLI_ENTRY = "scripts/classify_null_reasons.py"


# ─── 工具：AST 扫描（不做行匹配） ─────────────────────────────────


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "build" not in path.parts
    )


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _imported_modules(tree: ast.Module) -> set[str]:
    """所有 import 的模块全名。

    `from a.b import c` 记 `a.b` 与 `a.b.c` 两种写法（后者覆盖
    `from app.core.quality import null_reason_sweeper` 这种包内导入）。
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _importing_files(root: Path, module: str) -> list[str]:
    return [_rel(path) for path in _python_files(root) if module in _imported_modules(_parse(path))]


def _constant_true_write_calls(root: Path) -> list[str]:
    """`sweep(..., write=True)` / `sweep_all(..., write=True)` 的硬编码调用点。"""
    hits: list[str] = []
    for path in _python_files(root):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name not in {"sweep", "sweep_all"}:
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "write"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    hits.append(f"{_rel(path)}:{node.lineno}")
    return hits


# ─── 1. 写入只走 CLI：dry-run 是签名默认值 ───────────────────────


def test_sweep_defaults_to_dry_run() -> None:
    """`write` 必须是 keyword-only 且默认 `False`；`domain` 不许有默认值。

    拦：①有人把默认翻成 `True`，CLI 的"预演绝不写库"承诺立刻失效；
    ②`domain` 拿到默认值，"只清扫 ext 域"退化成扫全库（分钟级 → 十几分钟级）。
    """
    for func in (sweeper.sweep, sweeper.sweep_all):
        params = inspect.signature(func).parameters
        write = params["write"]
        assert write.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{func.__name__}.write 必须是 keyword-only：位置参数会被误传"
        )
        assert write.default is False, (
            f"{func.__name__}.write 默认值必须是 False（dry-run）——"
            "否则任何一次调用都在未加 --yes、未持写锁的情况下写正式库"
        )

    domain = inspect.signature(sweeper.sweep).parameters["domain"]
    assert domain.default is inspect.Parameter.empty, (
        "sweep.domain 不应有默认值：全库清扫必须是调用方显式要求（sweep_all）"
    )
    assert inspect.signature(sweeper.sweep_all).parameters["write"].default is False


# ─── 2. 唯一写库入口：`app/` 里没有别的调用点 ─────────────────────


def test_only_the_offline_cli_reaches_the_sweeper() -> None:
    """`app/` 不得 import 清扫器；`scripts/` 只许 CLI 一处。

    拦：有人把清扫接进更新链/请求路径——那会绕过写锁
    （`any_write_lock_active` + `exclusive_update`）与 CLI 抢 DuckDB 写锁，
    并把"取数"与"体检"耦死（CLI docstring 记录的裁决）。
    真要接线，请先改这条测试与模块 docstring 的「归属与调用面」一节。
    """
    assert _importing_files(REPO_ROOT / "app", SWEEPER_MODULE) == [], (
        "app/ 内出现了空值归因清扫器的 import：它只应在离线 CLI 里被调用"
    )
    assert _importing_files(REPO_ROOT / "scripts", SWEEPER_MODULE) == [CLI_ENTRY], (
        f"清扫器在 scripts/ 下的调用点应只有 {CLI_ENTRY}"
    )
    assert _constant_true_write_calls(REPO_ROOT / "app") == [], (
        "app/ 里出现了 write=True 的清扫调用：写入必须只经 CLI（持写锁 + dry-run 默认）"
    )


# ─── 3. 前置检查的路径键必须指向真实文件并真的 import 了该符号 ────


def _sector_consumer_map() -> dict[str, str]:
    tree = _parse(REPO_ROOT / "scripts/check_indicator_readiness.py")
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "_SECTOR_CONSUMERS" for t in node.targets):
            raw = ast.literal_eval(node.value)
            assert isinstance(raw, dict)
            return {str(k): str(v) for k, v in raw.items()}
    raise AssertionError("scripts/check_indicator_readiness.py 里找不到 _SECTOR_CONSUMERS")


def test_readiness_guard_path_keys_point_at_real_importers() -> None:
    """`_SECTOR_CONSUMERS` 的每条 路径键→符号 都必须真实成立。

    这条是**为"迁文件"准备的地雷探测器**：清扫器若被搬到 `scripts/lib/`，
    键指向的文件就不存在了，前置检查 `_check_sector_single_source()` 会判 BLOCK
    （`check_indicator_readiness.py:87-93`）。搬家的正确做法是同步改这条键
    并扩大该检查的扫描范围，而不是让它静默失效。
    """
    consumers = _sector_consumer_map()
    assert consumers.get(SWEEPER_KEY) == "is_bank_sql", (
        f"_SECTOR_CONSUMERS 必须仍以 {SWEEPER_KEY} 为键收口 is_bank_sql"
        "（清扫器的行业口径是金融股判据的消费方之一）"
    )

    for module_path, symbol in consumers.items():
        path = REPO_ROOT / module_path
        assert path.is_file(), (
            f"_SECTOR_CONSUMERS 的键 {module_path} 指向的文件不存在："
            "前置检查会判 BLOCK，请同步改这条路径键（或恢复文件）"
        )
        imported = {
            alias.name
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.ImportFrom) and node.module == "app.core.sector"
            for alias in node.names
        }
        assert symbol in imported, (
            f"{module_path} 未从 app.core.sector 导入 {symbol}："
            "金融股判据的单一真相收口被绕过了"
        )


# ─── 4. 读路径仍在：端点 → reason_distribution → 稀疏表 ────────────


def test_read_path_still_reads_the_sparse_table() -> None:
    """`GET /api/data-status/null-reasons` → `reason_distribution()` → 稀疏表。

    拦：端点被删/改名、或换成别的数据源（CLI 写进去的原因没人消费）；
    也拦 `reason_distribution()` 丢掉 `NULL_REASON_TABLE` 改为写死别的表名。
    """
    assert null_reasons.NULL_REASON_TABLE == "indicator_null_reason", (
        f"稀疏表名被改成 {null_reasons.NULL_REASON_TABLE!r}：清扫器写入目标与读端查询目标已分叉"
    )
    distribution_ast = ast.parse(
        inspect.getsource(null_reasons.reason_distribution)
    )
    names = {node.id for node in ast.walk(distribution_ast) if isinstance(node, ast.Name)}
    assert "NULL_REASON_TABLE" in names, (
        "reason_distribution() 不再引用 NULL_REASON_TABLE：读端已脱离清扫器的写入目标"
    )

    api = _parse(REPO_ROOT / "app/web/api/data_status.py")
    endpoints: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(api):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == "/null-reasons"
            ):
                endpoints[node.name] = node
    assert "get_null_reasons" in endpoints, (
        "GET /null-reasons 端点不见了：清扫结果的唯一查询入口被删除/改名"
    )
    body_names = {
        node.id for node in ast.walk(endpoints["get_null_reasons"]) if isinstance(node, ast.Name)
    }
    assert "reason_distribution" in body_names, (
        "get_null_reasons 不再调用 reason_distribution()：查询入口换了数据源"
    )


# ─── 5. 归属说明本身也要在（文档即契约的落点） ────────────────────


def test_module_docstring_keeps_the_ownership_section() -> None:
    """模块 docstring 的「归属与调用面」一节是这次裁决的**唯一**文字落点。

    拦：重构时把这段说明顺手删掉，于是下一个人重新纠结"为什么不在更新链里 /
    为什么不挪到 scripts/"（`reports/136` L2 已经因此错判过一次）。
    """
    doc = sweeper.__doc__ or ""
    for anchor in ("归属与调用面", "唯一写库入口", "读端是在线的", "为什么不接进更新链", "scripts/lib"):
        assert anchor in doc, f"模块 docstring 缺少「归属与调用面」要点：{anchor}"
    assert "reports/136" in doc, "docstring 应保留 reports/136 迁出建议被否决的追溯说明"
