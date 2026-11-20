"""临时目录卫生守卫（`reports/149` 磁盘爆满事故）。

事故回顾
--------
`%TEMP%` 一度涨到 **107 GB**，全量回调因此报出 **50 个**
`OSError: [Errno 28] No space left on device` —— 症状长得像"代码塌了"，
实际是跑测试/探针时手搓的运行根（`mktemp -d` + `pytest --basetemp`）**跑完不删**，
19 小时累积 455 个目录 / 74 GB。

要守住的四条
------------
1. **`scoped_pytest` 必须保证删根**：正常返回、非零退出、抛异常、Ctrl-C 都要删
   （这是"不依赖人记得"的关键）。
2. **清扫只认有主的目录**：带 `.vd-scratch-owner` 标记或固定前缀的才碰；
   普通目录、别的程序缓存一律不动。
3. **活着的目录不动**：带 `.vd-active` 或 mtime 很新的跳过。
4. **门禁要提前拦**：`%TEMP%` 里我们造的部分超过 WARN/BLOCK 阈值就报出来，
   别等 107 GB 才靠"跑不动了"发现。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _load(name: str):
    """按路径加载 `scripts/*.py`（它们不是包）。"""
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_vd_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hygiene = _load("temp_hygiene")
scoped = _load("scoped_pytest")
preflight = _load("session_preflight")


# ─── 1. scoped_pytest 必须保证删根 ──────────────────────────────────────────


def _make_stale_owner_dirs(base: Path, count: int) -> list[Path]:
    """造几个"有主但很旧"的 scratch 根（模拟上次被硬杀留下的）。"""
    made: list[Path] = []
    for index in range(count):
        path = base / f"{hygiene.SCRATCH_PREFIXES[0]}stale{index}"
        path.mkdir(parents=True)
        (path / hygiene.OWNER_MARKER).write_text("owner=test\n", encoding="utf-8")
        (path / "payload.bin").write_bytes(b"x" * 1024)
        made.append(path)
    return made


def test_scoped_pytest_deletes_run_root_even_when_runner_raises(tmp_path: Path) -> None:
    """**核心保证**：runner 抛异常也必须删根（否则就是"跑挂了留一堆"）。"""
    def boom(env, args):  # noqa: ANN001
        raise RuntimeError("测试中途炸了")

    with pytest.raises(RuntimeError, match="测试中途炸了"):
        scoped.run_scoped([], runner=boom, temp_dir=tmp_path)

    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(scoped.SCRATCH_PREFIX)
    ]
    assert leftovers == [], f"runner 抛异常后运行根没删：{leftovers}"


def test_scoped_pytest_deletes_run_root_on_nonzero_exit(tmp_path: Path) -> None:
    """非零退出码同样要删（失败是最常见的情况，不能只保证成功路径）。"""
    def failing(env, args):  # noqa: ANN001
        # 顺手验证环境变量真的指向这个根，且 --basetemp 被自动注入
        assert env["VD_ENV"] == "test"
        assert "--basetemp" in args
        assert Path(env["VD_TEST_RUN_ROOT"]).is_dir()
        return 1

    code = scoped.run_scoped([], runner=failing, temp_dir=tmp_path)
    assert code == 1
    assert [
        p for p in tmp_path.iterdir() if p.name.startswith(scoped.SCRATCH_PREFIX)
    ] == []


def test_scoped_pytest_keeps_root_only_when_explicitly_asked(tmp_path: Path) -> None:
    """`--keep` 是调试逃生口，必须显式要；默认永远删。"""
    scoped.run_scoped([], runner=lambda env, args: 0, temp_dir=tmp_path, keep=True)
    kept = [p for p in tmp_path.iterdir() if p.name.startswith(scoped.SCRATCH_PREFIX)]
    assert len(kept) == 1, "keep=True 时应当保留运行根（供调试）"
    assert (kept[0] / hygiene.ACTIVE_MARKER).exists(), "保留时活性标记应仍在（防止被误清）"


def test_scoped_pytest_reclaims_stale_roots_before_running(tmp_path: Path) -> None:
    """**自愈**：上一次被硬杀留下的旧根，下一次运行应顺手回收。"""
    stale = _make_stale_owner_dirs(tmp_path, 2)
    old = time.time() - 5 * 3600
    for path in stale:
        for target in (path, *path.iterdir()):
            import os

            os.utime(target, (old, old))

    scoped.run_scoped(
        [], runner=lambda env, args: 0, temp_dir=tmp_path,
        prune_older_than_minutes=60,
    )
    assert [p for p in stale if p.exists()] == [], "上次留下的旧根没有被回收"


# ─── 2/3. 清扫只认有主的、活着的 ────────────────────────────────────────────


def test_prune_only_touches_owned_scratch_dirs(tmp_path: Path) -> None:
    """**别人的目录一律不动** —— 这是"通用清扫"最容易造成的二次事故。"""
    owned = _make_stale_owner_dirs(tmp_path, 1)[0]
    foreign = tmp_path / "someone-elses-cache"
    foreign.mkdir()
    (foreign / "data.bin").write_bytes(b"y" * 2048)
    unmatched = tmp_path / "vd-looks-like-ours"
    unmatched.mkdir()  # 前缀像我们的，但没有标记也没开 --include-legacy

    old = time.time() - 5 * 3600
    for path in (owned, foreign, unmatched):
        for target in (path, *path.iterdir()):
            import os

            os.utime(target, (old, old))

    removed = hygiene.prune(tmp_path, older_than_minutes=60, dry_run=False)
    assert [c.path.name for c in removed] == [owned.name]
    assert not owned.exists()
    assert foreign.exists(), "普通目录被误删了 —— 清扫越界"
    assert unmatched.exists(), "未开 --include-legacy 时不该按名字猜"


def test_prune_skips_active_and_recent_roots(tmp_path: Path) -> None:
    """带 `.vd-active` 的、以及刚被碰过的，**都不许删**（那是正在跑的）。"""
    active = tmp_path / f"{hygiene.SCRATCH_PREFIXES[0]}active"
    active.mkdir()
    (active / hygiene.OWNER_MARKER).write_text("owner=test\n", encoding="utf-8")
    (active / hygiene.ACTIVE_MARKER).write_text("12345", encoding="utf-8")

    recent = tmp_path / f"{hygiene.SCRATCH_PREFIXES[0]}recent"
    recent.mkdir()
    (recent / hygiene.OWNER_MARKER).write_text("owner=test\n", encoding="utf-8")

    import os

    old = time.time() - 5 * 3600
    os.utime(active / hygiene.OWNER_MARKER, (old, old))
    os.utime(active, (old, old))  # 目录本身很旧，但活性标记在 → 仍不许删

    removed = hygiene.prune(tmp_path, older_than_minutes=60, dry_run=False)
    assert removed == []
    assert active.exists(), "带 .vd-active 的运行根被删了 —— 会打断正在跑的测试"
    assert recent.exists(), "刚创建的运行根被删了"


def test_prune_dry_run_deletes_nothing(tmp_path: Path) -> None:
    """默认 dry-run：只报不删（防止手滑清空）。"""
    stale = _make_stale_owner_dirs(tmp_path, 1)
    import os

    old = time.time() - 5 * 3600
    os.utime(stale[0], (old, old))
    removed = hygiene.prune(tmp_path, older_than_minutes=60, dry_run=True)
    assert len(removed) == 1
    assert stale[0].exists(), "dry-run 竟然真删了"


# ─── 4. 门禁阈值 ────────────────────────────────────────────────────────────


def _stats(scratch_gb: float) -> dict[str, float]:
    return {
        "temp_root_bytes": scratch_gb * (1024 ** 3) * 1.2,
        "temp_root_dirs": 100.0,
        "scratch_bytes": scratch_gb * (1024 ** 3),
        "scratch_dirs": 50.0,
    }


def test_preflight_temp_check_passes_when_clean() -> None:
    assert preflight.check_temp_hygiene(_stats(0.2)).status == preflight.PASS


def test_preflight_temp_check_warns_then_blocks() -> None:
    """**提前拦**：到 5 GB 就提示、15 GB 就 BLOCK —— 不是等 107 GB 靠跑不动发现。"""
    assert (
        preflight.check_temp_hygiene(_stats(preflight.TEMP_SCRATCH_WARN_GB + 0.1)).status
        == preflight.WARN
    )
    blocked = preflight.check_temp_hygiene(_stats(preflight.TEMP_SCRATCH_BLOCK_GB + 0.1))
    assert blocked.status == preflight.BLOCK
    assert "temp_hygiene.py" in (blocked.next_step or ""), "BLOCK 时必须给出可执行的清理命令"


def test_preflight_temp_check_fails_open() -> None:
    """探测本身失败要降级放行，不能因为工具坏了把人锁死。"""
    # 一个"取值就会炸"的假 stats，模拟探测异常路径
    class _Broken(dict):
        def __getitem__(self, key):  # noqa: ANN001, D105
            raise RuntimeError("探测炸了")

    result = preflight.check_temp_hygiene(_Broken())
    assert result.status == preflight.WARN
    assert "降级放行" in result.detail
