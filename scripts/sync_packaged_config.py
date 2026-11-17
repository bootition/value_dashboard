"""打包兜底配置的构建期同步（`reports/136` §6.1，起因 `reports/135`）。

背景事故（reports/135）
----------------------
仓库里有两份 `default.yaml`：

- `config/default.yaml`：源码运行时 `app/core/config.py` 读取的那份；
- `app/resources/config/default.yaml`：wheel / 安装版在没有仓库 `config/`
  目录时的兜底副本（`importlib.resources` 读它）。

2026-11 的真实事故：兜底副本漂移到了 93 行，而仓库版是 269 行 —— 它缺了
51 个 `update.*` 键与整个 `asset_health:` 段，把 `adapters.primary.dividends`
塞回了 `cninfo`（仓库版有硬注释明令禁止），还把 DuckDB 内存上限从 8GB 砍到
2GB/4GB。源码形态根本不读那份副本，所以症状是"测试全绿、发布即事故"。

在此之前的同步方式只有"人记得手工 `cp`"——漏一次就是一次发布事故。本脚本
把这件事变成可执行的一步：构建期同步 + 一条命令可修 + `--check` 只读校验。

为什么源必须是 `config/default.yaml`
-----------------------------------
1. 它是**唯一被人编辑的读取源**：文档、报告、调参改的都是它；兜底副本不该
   拥有独立内容，它是生成物。
2. 方向不能反。如果允许"从副本同步回仓库"，某次事故里的禁用值（`cninfo`
   分红链、2GB 内存上限）就会被反向写进正式配置——事故会升级成永久缺陷。
3. PyInstaller 产物里的 `_internal/config/default.yaml`（`value-dashboard.spec`
   的 `required_file('config/default.yaml')`）同样来自它。只有三者同源，
   才能保证"源码跑 / 安装版跑"读到完全一致的配置。

因此数据流是单向的：

    config/default.yaml  --(二进制复制)-->  app/resources/config/default.yaml

用法
----
    uv run --locked python scripts/sync_packaged_config.py           # 同步（构建时调用）
    uv run --locked python scripts/sync_packaged_config.py --check   # 只比较：一致 0，漂移 1

复制是**二进制**的（`read_bytes` / `write_bytes`，不经文本模式），不做换行风格
转换：`tests/regression/test_config_dead_keys.py` 断言两份文件逐字节相等，
CRLF 与 LF 的差异同样算漂移。
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

#: 仓库根目录（本脚本位于 `scripts/`）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 唯一源：源码运行读取的那份，只能在这里改。
REPO_CONFIG_RELATIVE = Path("config") / "default.yaml"

#: 生成物：wheel / 安装版兜底副本，不要手写。
PACKAGED_CONFIG_RELATIVE = Path("app") / "resources" / "config" / "default.yaml"

#: `--check` 摘要里 unified diff 最多打印多少行。
_DIFF_LINE_LIMIT = 20


def _read_bytes_or_none(path: Path) -> bytes | None:
    """读文件字节；不存在时返回 None（"副本缺失"也算漂移）。"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _line_count(payload: bytes) -> int:
    newlines = payload.count(b"\n")
    if payload and not payload.endswith(b"\n"):
        newlines += 1
    return newlines


def _newline_summary(payload: bytes) -> str:
    crlf = payload.count(b"\r\n")
    lf = payload.count(b"\n") - crlf
    return f"CRLF={crlf} / LF={lf}"


def _describe_drift(source: bytes, target: bytes | None) -> list[str]:
    """给 `--check` / 同步日志用的差分摘要：规模 + 换行风格 + 前 N 行 diff。"""
    lines = [
        f"  源   {REPO_CONFIG_RELATIVE.as_posix()}: "
        f"{len(source)} 字节 / {_line_count(source)} 行 / {_newline_summary(source)}",
    ]
    if target is None:
        lines.append(f"  副本 {PACKAGED_CONFIG_RELATIVE.as_posix()}: 不存在")
        return lines

    lines.append(
        f"  副本 {PACKAGED_CONFIG_RELATIVE.as_posix()}: "
        f"{len(target)} 字节 / {_line_count(target)} 行 / {_newline_summary(target)}"
    )
    if len(source) == len(target):
        lines.append("  两份文件字节数相同，但逐字节比较不相等（换行风格或个别字符不同）")

    diff = list(
        difflib.unified_diff(
            target.decode("utf-8", "replace").splitlines(keepends=True),
            source.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=f"{PACKAGED_CONFIG_RELATIVE.as_posix()}（副本，现状）",
            tofile=f"{REPO_CONFIG_RELATIVE.as_posix()}（源，期望）",
            n=1,
        )
    )
    if not diff:  # 字节不同但按行无法表示（例如行尾/编码层差异），摘要到此为止
        lines.append("  （按行比较无差异，差异只在字节层：请核对换行风格与末尾字符）")
        return lines
    lines.extend(line.rstrip("\r\n") for line in diff[:_DIFF_LINE_LIMIT])
    if len(diff) > _DIFF_LINE_LIMIT:
        lines.append(f"  ...（其余 {len(diff) - _DIFF_LINE_LIMIT} 行差异已省略）")
    return lines


def compare_and_sync(repo_root: Path, *, check: bool) -> int:
    """比较或同步两份配置，返回退出码。

    0 = 已一致 / 同步成功；1 = `--check` 发现漂移；2 = 前置条件不满足
    （源缺失、写回校验失败）。

    测试直接调用本函数（`tmp_path` 造两份小文件，路径由 `repo_root` 注入）。
    """
    repo_root = Path(repo_root)
    source = repo_root / REPO_CONFIG_RELATIVE
    target = repo_root / PACKAGED_CONFIG_RELATIVE

    source_bytes = _read_bytes_or_none(source)
    if source_bytes is None:
        print(f"错误：找不到唯一源配置 {source}（它必须存在，副本不能反向成为源）", file=sys.stderr)
        return 2
    target_bytes = _read_bytes_or_none(target)

    if target_bytes == source_bytes:
        if check:
            print(
                "打包兜底配置与仓库配置逐字节相等"
                f"（{len(source_bytes)} 字节）：{PACKAGED_CONFIG_RELATIVE.as_posix()}"
            )
        return 0

    if check:
        print("打包兜底配置与仓库配置不一致（正是 reports/135 的事故形态）：", file=sys.stderr)
        for line in _describe_drift(source_bytes, target_bytes):
            print(line, file=sys.stderr)
        print("修复：uv run --locked python scripts/sync_packaged_config.py", file=sys.stderr)
        return 1

    for line in _describe_drift(source_bytes, target_bytes):
        print(line)
    # 二进制写回：先写同目录临时文件再原子替换，中断时不会留下半份配置。
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f"{target.name}.syncing")
    try:
        tmp_path.write_bytes(source_bytes)
        tmp_path.replace(target)
    finally:
        tmp_path.unlink(missing_ok=True)

    written = target.read_bytes()
    if written != source_bytes:
        print(
            f"错误：同步后 {target} 与源仍不一致（{len(written)} != {len(source_bytes)} 字节），"
            "构建必须中止",
            file=sys.stderr,
        )
        return 2
    print(
        f"已同步 {PACKAGED_CONFIG_RELATIVE.as_posix()} ← {REPO_CONFIG_RELATIVE.as_posix()}"
        f"（{len(source_bytes)} 字节，写回后逐字节校验通过）"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 config/default.yaml 同步到 app/resources/config/default.yaml"
        "（reports/136 §6.1；两份必须逐字节相等）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比较不写入：一致退出 0，漂移打印 diff 摘要并退出 1（构建门禁用）",
    )
    parser.add_argument(
        "--root",
        default=str(PROJECT_ROOT),
        help="仓库根目录（默认：本脚本所在的仓库）",
    )
    args = parser.parse_args(argv)
    return compare_and_sync(Path(args.root), check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
