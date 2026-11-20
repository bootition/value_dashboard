"""临时目录卫生：把"跑测试会留下临时根"这件事变成**可自动回收**的。

## 为什么需要这个模块（2026-11-20 事故）

`reports/149` 记录：`%TEMP%` 一度涨到 **107 GB**，直接导致全量回归出现 50 个
`OSError: [Errno 28] No space left on device` —— 看起来像"代码塌了"，实际是磁盘满了。

来源**不是项目代码**，是**跑测试/探针的人（含 AI agent）手搓的运行根**：
```bash
RUN="$(mktemp -d)/vd-xxx" && ... pytest --basetemp="$RUN/tmp"     # 跑完不删
```
19 小时累积 **455 个 `tmp.*` 目录 / 74 GB**。项目自己造的三类临时目录
（`vd-s1-*` / `vd-restore-*` / `vd-cold-measure-*`）都有主、都有人清，
**手搓的没有主、没人清**。

## 三条设计原则

1. **有主才回收**：新建的 scratch 根一律带**拥有者标记文件**（`.vd-scratch-owner`）
   和固定前缀（`vd-scoped-`）。清扫只认"带标记的"或"我们的前缀"，**绝不**按
   "`%TEMP%` 里所有旧目录"乱删 —— 那会删到别的程序的缓存。
2. **活着的不动**：正在跑的运行根会写 `.vd-active`；清扫跳过它。
   另外按 mtime 再兜一层（`older_than_minutes`）。
3. **跑完必删**：`scoped_pytest` 用 `try/finally` 保证 —— 成功、失败、抛异常、
   Ctrl-C 都会删。**不依赖使用者记得。**

## 用法

```bash
# 清理（默认 dry-run，只报不删）
uv run --locked python scripts/temp_hygiene.py --older-than-minutes 60
# 真删
uv run --locked python scripts/temp_hygiene.py --older-than-minutes 60 --execute
# 连历史上手搓的 vd-*/tmp.* 一起（危险档，需显式开关）
uv run --locked python scripts/temp_hygiene.py --older-than-minutes 360 --include-legacy --execute
```
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

#: 新工具创建的 scratch 根前缀（有主）。
SCRATCH_PREFIXES = ("vd-scoped-", "vd-run-")

#: 拥有者标记 + 活性标记（文件名固定，别改，历史清理脚本认它）。
OWNER_MARKER = ".vd-scratch-owner"
ACTIVE_MARKER = ".vd-active"

#: 历史上"手搓"的运行根常见前缀（`reports/149` 实测出现过的）。
#: 这些**没有标记**，只有名字线索 —— 所以只在显式 `--include-legacy` 时才碰。
LEGACY_PREFIXES = (
    "vd-r",          # vd-r3 / vd-r4 / vd-r4b …
    "vd-L",          # vd-L1b / vd-L4b …
    "vd-M",          # vd-M1 / vd-M3b …
    "vd-final",      # vd-final / vd-final2
    "vd-p",          # vd-p0789k-1794887393
    "vd-mut",        # 变异验证用
    "vd-big",        # vd-big4
    "vd-epoch",      # vd-epoch2 …
    "vd-smoke",
    "vd-msmoke",
    "vd-cutoff",
    "vd-recheck",
    "vd-probe",
    "vd-t4",
    "vd_audit",
    "vdbatch-",
    "t4cost",
    "fxsplit",
)

#: `%TEMP%` 里"可能是我们造的、但名字被系统随机化"的那一类。
#: 只在 `--include-tmp` 时碰（Python 的 `tempfile.mkdtemp()` 默认就是 `tmp*`）。
TMP_PREFIX = "tmp."


@dataclass(frozen=True)
class Candidate:
    path: Path
    size_bytes: int
    age_seconds: float
    reason: str  # owned / legacy / tmp-pattern

    @property
    def age_minutes(self) -> float:
        return self.age_seconds / 60.0


def temp_root(temp_dir: str | Path | None = None) -> Path:
    """`%TEMP%`（或显式目录）。"""
    return Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())


def _tree_size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
    return total


def _is_active(path: Path, *, active_grace_seconds: float) -> bool:
    """正在跑的运行根不许删：有活性标记，或刚被碰过。"""
    if (path / ACTIVE_MARKER).exists():
        return True
    try:
        return (time.time() - path.stat().st_mtime) < active_grace_seconds
    except OSError:
        return True  # 读不到就不动（保守）


def scan(
    temp_dir: str | Path | None = None,
    *,
    include_legacy: bool = False,
    include_tmp: bool = False,
) -> list[Candidate]:
    """扫描候选目录（只读，不删）。"""
    root = temp_root(temp_dir)
    if not root.is_dir():
        return []
    found: list[Candidate] = []
    now = time.time()
    for entry in root.iterdir():
        try:
            if not entry.is_dir():
                continue
            stat = entry.stat()
        except OSError:
            continue
        name = entry.name
        if name in SCRATCH_PREFIXES:  # 防御：前缀元组里不该有完整名
            continue
        reason = ""
        if name.startswith(SCRATCH_PREFIXES) or (entry / OWNER_MARKER).exists():
            reason = "owned"
        elif include_legacy and name.startswith(LEGACY_PREFIXES):
            reason = "legacy"
        elif include_tmp and name.startswith(TMP_PREFIX):
            reason = "tmp-pattern"
        if not reason:
            continue
        found.append(
            Candidate(
                path=entry,
                size_bytes=_tree_size(entry),
                age_seconds=max(0.0, now - stat.st_mtime),
                reason=reason,
            )
        )
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def usage(temp_dir: str | Path | None = None) -> dict[str, float]:
    """`%TEMP%` 的总量/总量上限参考值（给门禁与脚本打日志用）。"""
    root = temp_root(temp_dir)
    owned = scan(root)
    everything = 0
    count = 0
    if root.is_dir():
        for entry in root.iterdir():
            try:
                if entry.is_dir():
                    everything += _tree_size(entry)
                    count += 1
            except OSError:
                continue
    return {
        "temp_root_bytes": float(everything),
        "temp_root_dirs": float(count),
        "scratch_bytes": float(sum(c.size_bytes for c in owned)),
        "scratch_dirs": float(len(owned)),
    }


def prune(
    temp_dir: str | Path | None = None,
    *,
    older_than_minutes: float = 60.0,
    include_legacy: bool = False,
    include_tmp: bool = False,
    dry_run: bool = True,
    active_grace_seconds: float = 600.0,
    log: object | None = None,
) -> list[Candidate]:
    """删掉"旧的、没人在用"的 scratch 目录；返回**实际删除**的清单。

    保护条件（任一命中就跳过）：带 `.vd-active`、mtime 在
    `active_grace_seconds` 内、或 age < `older_than_minutes`。
    """
    emit = log if callable(log) else (lambda _msg: None)
    candidates = scan(
        temp_dir, include_legacy=include_legacy, include_tmp=include_tmp
    )
    removed: list[Candidate] = []
    for candidate in candidates:
        if candidate.age_minutes < older_than_minutes:
            continue
        if _is_active(candidate.path, active_grace_seconds=active_grace_seconds):
            continue
        if dry_run:
            removed.append(candidate)
            continue
        try:
            shutil.rmtree(candidate.path)
        except OSError as error:
            emit(f"[temp-hygiene] 删除失败 {candidate.path}: {error}")
            continue
        removed.append(candidate)
    return removed


def human_gb(value: float) -> str:
    return f"{value / (1024 ** 3):.2f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理测试/探针留下的临时目录")
    parser.add_argument("--older-than-minutes", type=float, default=60.0)
    parser.add_argument("--include-legacy", action="store_true",
                        help="连历史上手搓的 vd-*/vdbatch-* 等一起清（无标记，按名字）")
    parser.add_argument("--include-tmp", action="store_true",
                        help="连 tmp.* 一起清（危险：该前缀别的程序也在用）")
    parser.add_argument("--execute", action="store_true", help="真删（默认只报告）")
    args = parser.parse_args(argv)

    before = usage()
    print(
        f"[temp-hygiene] %TEMP% 合计 {human_gb(before['temp_root_bytes'])} / "
        f"{int(before['temp_root_dirs'])} 个目录；"
        f"其中我们造的 {human_gb(before['scratch_bytes'])} / "
        f"{int(before['scratch_dirs'])} 个"
    )

    candidates = scan(include_legacy=args.include_legacy, include_tmp=args.include_tmp)
    stale = [c for c in candidates if c.age_minutes >= args.older_than_minutes]
    print(
        f"[temp-hygiene] 超过 {args.older_than_minutes:g} 分钟的候选：{len(stale)} 个 / "
        f"{human_gb(sum(c.size_bytes for c in stale))}"
    )
    for candidate in stale[:10]:
        print(f"    - {candidate.path.name}  {human_gb(candidate.size_bytes)}  "
              f"{candidate.age_minutes:.0f} 分钟前  [{candidate.reason}]")

    removed = prune(
        older_than_minutes=args.older_than_minutes,
        include_legacy=args.include_legacy,
        include_tmp=args.include_tmp,
        dry_run=not args.execute,
        log=print,
    )
    verb = "已删除" if args.execute else "可删除（dry-run，加 --execute 才真删）"
    print(f"[temp-hygiene] {verb}：{len(removed)} 个 / "
          f"{human_gb(sum(c.size_bytes for c in removed))}")

    after = usage()
    print(
        f"[temp-hygiene] 之后 %TEMP% 合计 {human_gb(after['temp_root_bytes'])}；"
        f"我们造的 {human_gb(after['scratch_bytes'])} / {int(after['scratch_dirs'])} 个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
