"""`shared_data_roots()` 必须进程内缓存（2026-11-23 性能事故）。

现场：指标全量重算 **33–39 只/分钟**（5,575 只 ≈ 2.4 小时）。py-spy 抓栈：

    Popen._execute_child            ← 正在 spawn 子进程！
    shared_data_roots(path_policy)
    DatabasePathSet.validate
    SQLiteStore._revalidate
    SQLiteStore.connection / query
    IndicatorCalculator.compute_all_for_stock

**每一次 `sqlite.query()` 都 spawn 一个 `git rev-parse`**（Windows 进程创建
50–150 ms），每只股票要查好几次 → 单股 1.5 秒。这是纯开销：仓库根在进程
生命周期内不可能变。

本用例钉住：
1. 第二次及以后**不再** spawn 子进程；
2. 缓存的值与首次一致（不能缓存错值）；
3. 非 git 环境（空结果）**也缓存** —— 否则每次查询都重试一次失败的 git；
4. 缓存**不削弱路径隔离守卫**：`validate()` 仍会跑（见 test_path_isolation.py）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.storage import path_policy


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个用例都从"没缓存过"开始（缓存是进程级全局）。"""
    original = path_policy._SHARED_ROOTS_CACHE
    path_policy._SHARED_ROOTS_CACHE = None
    yield
    path_policy._SHARED_ROOTS_CACHE = original


class _CountingRun:
    """替身 `subprocess.run`：数调用次数，返回可控结果。"""

    def __init__(self, stdout: str = "/repo/.git", returncode: int = 0) -> None:
        self.calls = 0
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, *args, **kwargs):
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(returncode=self._returncode, stdout=self._stdout)


def test_second_call_does_not_spawn_git(monkeypatch, tmp_path) -> None:
    # 用 tmp_path 构造**真实绝对路径**：手写 Windows 反斜杠字面量会被转义吃掉
    # （实测把 D:\repo 写成了 D:epo，路径校验直接判"不是绝对路径"）。
    git_dir = tmp_path / "repo" / ".git"
    fake = _CountingRun(stdout=str(git_dir))
    monkeypatch.setattr(path_policy.subprocess, "run", fake)

    first = path_policy.shared_data_roots()
    assert fake.calls == 1, "首次必须真的问一次 git"
    assert first == {path_policy.canonicalize_path(tmp_path / "repo" / "data")}

    for _ in range(50):
        again = path_policy.shared_data_roots()
    assert fake.calls == 1, (
        f"第二次及以后不许再 spawn git（实际被调用 {fake.calls} 次）——"
        "每次 sqlite.query() 都走这里，不缓存等于单股多花 0.5–1 秒"
    )
    assert again == first, "缓存值必须与首次一致"


def test_empty_result_is_cached_too(monkeypatch) -> None:
    """非 git 环境（返回空）也必须缓存，否则每次查询都重试失败 git。"""
    fake = _CountingRun(stdout="", returncode=128)
    monkeypatch.setattr(path_policy.subprocess, "run", fake)

    assert path_policy.shared_data_roots() == set()
    for _ in range(20):
        assert path_policy.shared_data_roots() == set()
    assert fake.calls == 1, f"空结果也要缓存（实际被调用 {fake.calls} 次）"


def test_cached_function_is_still_the_only_subprocess_user() -> None:
    """架构纪律不变：本模块里 `subprocess` 只许出现在 `shared_data_roots` 里。"""
    source = Path(path_policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "subprocess" in ast.get_source_segment(source, node)
        and node.name != "shared_data_roots"
    ]
    assert offenders == [], f"subprocess 只许在 shared_data_roots() 里用：{offenders}"
