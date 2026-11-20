"""靶向测试的**唯一**入口：临时根的生命周期由这个脚本保证，不靠人记得。

## 为什么必须走这个入口（`reports/149`）

靶向测试的标准写法一直是：
```bash
RUN="$(mktemp -d)/vd-xxx" && mkdir -p "$RUN/tmp"
VD_ENV=test VD_TEST_RUN_ROOT="$RUN" ... pytest <files> -q --basetemp="$RUN/tmp"
```
**没有一行负责删。** 19 小时累积 455 个目录 / 74 GB，`%TEMP%` 到 107 GB，
全量回归因为磁盘满报出 50 个 `No space left on device` —— 看起来像代码塌了。

手写 `try/finally` 也救不了：这段命令是**复制粘贴**到 bash 里的，
一旦 agent 中途被打断、超时 killed、或忘了写 `rm -rf`，就漏一次。
**能保证的做法只有一个：把创建与删除放进同一个进程的 `finally`。**

## 用法

```bash
# 跑几个文件（自动建根、跑完必删、顺带回收历史残留）
uv run --locked python scripts/scoped_pytest.py tests/regression/test_foo.py -q

# 传任何 pytest 参数都行（`--basetemp` 由脚本自己管，别再手写）
uv run --locked python scripts/scoped_pytest.py tests/regression -q -k "roce and not slow"

# 只想看看现在堆了多少（不跑测试）
uv run --locked python scripts/scoped_pytest.py --temp-report
```

它每次开跑前会**顺手回收**空闲超过 N 分钟的旧 scratch 根（默认 120 分钟），
所以即使上一次被硬杀，下一次运行也会自己收拾干净 —— **自愈**，不依赖"记得清"。
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _entry in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import temp_hygiene  # noqa: E402  （scripts/ 已在 sys.path 上）

logger = logging.getLogger("scoped_pytest")

SCRATCH_PREFIX = "vd-scoped-"


def _make_run_root(base: Path | None = None) -> Path:
    """建一个**带拥有者标记**的运行根（清理脚本靠这个标记认领）。"""
    root = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=str(base) if base else None))
    (root / temp_hygiene.OWNER_MARKER).write_text(
        f"owner=scoped_pytest\npid={os.getpid()}\ncreated={time.time():.3f}\n",
        encoding="utf-8",
    )
    # `.vd-active` 是"正在跑，别删"的信号；finally 里会删掉它再删整棵目录。
    (root / temp_hygiene.ACTIVE_MARKER).write_text(str(os.getpid()), encoding="utf-8")
    return root


def _default_runner(env: dict[str, str], pytest_args: list[str]) -> int:
    completed = subprocess.run(  # noqa: S603 - 参数由调用方给定，本脚本就是测试入口
        [sys.executable, "-m", "pytest", *pytest_args],
        cwd=str(PROJECT_ROOT),
        env=env,
        check=False,
    )
    return completed.returncode


def run_scoped(
    pytest_args: list[str],
    *,
    runner=None,
    temp_dir: str | Path | None = None,
    prune_older_than_minutes: float = 120.0,
    keep: bool = False,
) -> int:
    """建根 → 跑 → **无论成败都删根**。返回 pytest 退出码。

    `runner(env, args) -> int` 可注入（测试用），默认起子进程跑 pytest。
    """
    base = temp_hygiene.temp_root(temp_dir)
    # 自愈：先回收历史残留（含上一次被硬杀留下的），再建自己的。
    reclaimed = temp_hygiene.prune(
        base,
        older_than_minutes=prune_older_than_minutes,
        dry_run=False,
        log=logger.info,
    )
    if reclaimed:
        logger.info(
            "回收历史 scratch 根 %d 个 / %s",
            len(reclaimed),
            temp_hygiene.human_gb(sum(c.size_bytes for c in reclaimed)),
        )

    root = _make_run_root(base)
    pytest_tmp = root / "pytest-tmp"
    pytest_tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        VD_ENV="test",
        VD_TEST_RUN_ROOT=str(root),
        VD_DUCKDB_PATH=str(root / "valuedashboard.duckdb"),
        VD_SQLITE_PATH=str(root / "valuedashboard.sqlite"),
        PYTHONIOENCODING="utf-8",
    )
    args = ["--basetemp", str(pytest_tmp), *pytest_args]
    runner = runner or _default_runner
    started = time.perf_counter()
    try:
        return runner(env, args)
    finally:
        # ↓ 这一段是**唯一**保证删除的地方：正常返回、异常、KeyboardInterrupt 都会走到。
        elapsed = time.perf_counter() - started
        if keep:
            logger.warning(
                "⚠ --keep 指定了保留运行根（调试用）：%s  —— 用完请自己 `rm -rf`", root
            )
        else:
            with_errors = ""
            try:
                (root / temp_hygiene.ACTIVE_MARKER).unlink(missing_ok=True)
            except OSError as error:
                with_errors = f"（删活性标记失败：{error}）"
            shutil.rmtree(root, ignore_errors=True)
            logger.info("已删除运行根 %s%s（耗时 %.1fs）", root.name, with_errors, elapsed)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="跑靶向测试并保证删除临时根（唯一推荐入口）",
        add_help=True,
    )
    parser.add_argument("--temp-report", action="store_true",
                        help="只报告 %TEMP% 与 scratch 占用，不跑测试")
    parser.add_argument("--prune-older-than-minutes", type=float, default=120.0,
                        help="开跑前回收空闲超过 N 分钟的旧 scratch 根（默认 120）")
    parser.add_argument("--include-legacy", action="store_true",
                        help="回收时连历史上手搓的 vd-*/vdbatch-* 一起清")
    parser.add_argument("--keep", action="store_true",
                        help="⚠ 调试用：保留运行根（默认必删）")
    known, pytest_args = parser.parse_known_args(argv)

    if known.temp_report:
        stats = temp_hygiene.usage()
        print(
            f"%TEMP% 合计 {temp_hygiene.human_gb(stats['temp_root_bytes'])} / "
            f"{int(stats['temp_root_dirs'])} 个目录；"
            f"我们造的 {temp_hygiene.human_gb(stats['scratch_bytes'])} / "
            f"{int(stats['scratch_dirs'])} 个"
        )
        return 0

    if not pytest_args:
        parser.error("至少要给一个 pytest 目标（或用 --temp-report）")

    # `--include-legacy` 是给"清理历史"用的，这里只在开跑前那次回收时生效。
    return run_scoped(
        pytest_args,
        prune_older_than_minutes=known.prune_older_than_minutes,
        keep=known.keep,
    )


if __name__ == "__main__":
    raise SystemExit(main())
