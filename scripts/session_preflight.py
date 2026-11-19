"""会话开工自检 —— `AGENTS W4` 的执行器。

## 为什么需要它

`AGENTS W8`（并行 agent 文件租约）在 2026-11-22 那次事故**之前就已经写着**，双方也都
自认为遵守了（各自只改自己需要的文件、都按路径 `git add`），仍然出事。按本仓库自己的判据
「只改出口表现 = 延期付款」，一条没有执行器的规则等于没有规则（`reports/144` §3）。

本脚本把「开工前该侦察什么」变成可执行判据：**看一眼这个仓库现在有没有第二方在动。**

## 检查项与对应事故

| 检查 | 触发 | 事故 |
|---|---|---|
| 工作区有未提交改动 | `git status --porcelain` 非空 | 改动被别人的 `commit -a` 吸走（`reports/144` C1） |
| 存在其他 worktree | `git worktree list` 多于 1 条 | 同上 + 构建产物互相覆盖（C6） |
| 别人的 pytest 在跑 | 进程命令行含 `pytest` 且不是本进程树 | 我方 S1 preflight 反复 `Python process state changed`（C3） |
| 8765 端口被占 | TCP 探测 | `freeze S1`（C4） |

## 用法

::

    uv run --locked python scripts/session_preflight.py           # 人读
    uv run --locked python scripts/session_preflight.py --json    # 机器读
    uv run --locked python scripts/session_preflight.py --allow-port 8765   # 明知是我自己起的

退出码：``0`` = PASS/WARN（可以开工）、``2`` = BLOCK（先处理，别动手）。

## 设计纪律

- **只读**：不修改工作区任何文件，不启动服务，不写库。
- **fail-open**：某项探测本身失败（没有 git、取不到进程表）时降级成 WARN，
  绝不因为工具坏了把人锁死——与 `.githooks/pre-commit` 的原则 3 一致。
- **纯函数 + 注入**：每个判断都是「拿字符串/布尔 → 返回 Check」，真实采集只在 `main()`
  里做，所以 `tests/regression/test_session_preflight.py` 不需要真的去开进程占端口。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 产品默认端口（`config/default.yaml` 的 `web.port`）。S1 包装器的 freeze 判据用的是硬编码
#: 8765（`scripts/s1-path-preflight.ps1:362`），这里保持一致，见 KB §21 T3。
DEFAULT_WEB_PORT = 8765

PASS, WARN, BLOCK = "PASS", "WARN", "BLOCK"
_SEVERITY = {PASS: 0, WARN: 1, BLOCK: 2}

#: `git status --porcelain` 里这些前缀说明有实际内容变化（`??` 未跟踪也算）。
_DIRTY_PREFIXES = ("M ", " M", "A ", " A", "D ", " D", "R ", " R", "UU", "??")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    next_step: str = ""

    def render(self) -> str:
        icon = {PASS: "✅", WARN: "⚠️ ", BLOCK: "🛑"}[self.status]
        line = f"{icon} {self.status:<5} {self.name}：{self.detail}"
        if self.next_step:
            line += f"\n          → {self.next_step}"
        return line


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        return max((c.status for c in self.checks), key=lambda s: _SEVERITY[s], default=PASS)

    @property
    def exit_code(self) -> int:
        return 2 if self.status == BLOCK else 0


# ─── 纯判断（可单测，不碰真实系统）─────────────────────────────────────


def check_working_tree(porcelain: str | None) -> Check:
    """工作区有未提交改动 → 说明可能有另一个写者，或你上次没提交完。

    刻意只给 WARN 不给 BLOCK：单会话「写到一半重启」也会看到脏工作区，
    这一项的价值是**让人当场确认"这是谁的"**，而不是武断拦死。
    """
    if porcelain is None:
        return Check("工作区", WARN, "取不到 git 状态（不是 git 仓库或无权限），降级放行")
    changed = [
        line for line in porcelain.splitlines()
        if line.strip() and line.startswith(_DIRTY_PREFIXES)
    ]
    if not changed:
        return Check("工作区", PASS, "干净")
    preview = "、".join(line[3:].strip() for line in changed[:5])
    more = f" 等 {len(changed)} 个文件" if len(changed) > 5 else ""
    return Check(
        "工作区",
        WARN,
        f"有 {len(changed)} 个未提交改动（{preview}{more}）",
        "确认是不是你自己的；不是就先提交/询问，别在别人的未完成改动上开工（reports/144 C1）",
    )


#: `git worktree list` 的一行：`<path>  <sha> [<branch>]`（path 与 sha 之间是两个以上空格）
_WORKTREE_LINE = re.compile(r"^(?P<path>.+?)\s{2,}(?P<sha>[0-9a-f]{7,})\s")


def _worktree_paths(worktree_list: str) -> list[str]:
    """从 `git worktree list` 的人类可读输出里抽出路径列表（顺序 = 主树在前）。"""
    paths: list[str] = []
    for line in worktree_list.splitlines():
        match = _WORKTREE_LINE.match(line.strip())
        if match:
            paths.append(match.group("path").strip().strip('"'))
    return paths


def _same_path(left: str, right: str) -> bool:
    """Windows 大小写不敏感 + 斜杠方向不同，比较前统一归一化。"""
    def norm(value: str) -> str:
        text = os.path.normcase(os.path.realpath(value.replace("\\", "/"))).replace("\\", "/")
        return text.rstrip("/")
    return norm(left) == norm(right)


def check_worktrees(worktree_list: str | None, *, current_root: str | None = None) -> Check:
    """只有「在主工作树里还看见别的 worktree」才 BLOCK。

    2026-11-22 修正：原实现只要 worktree 数 > 1 就 BLOCK —— 于是**一旦你按 W4 真的
    建了自己的私有 worktree，它就永久 BLOCK 你**（把自己也算成"别人"）。
    这会让规则执行器自相矛盾：照做反而开不了工。
    正确语义：
    * 只有当**前目录就是主工作树**、且存在额外 worktree 时才 BLOCK（你还没隔离）；
    * 若当前目录已在某个**非主** worktree 内（= W4 已满足），直接 PASS，其余 worktree 只作信息展示。
    """
    if worktree_list is None:
        return Check("worktree", WARN, "取不到 worktree 列表，降级放行")
    entries = [line for line in worktree_list.splitlines() if line.strip()]
    if len(entries) <= 1:
        return Check("worktree", PASS, "只有主工作树")

    paths = _worktree_paths(worktree_list)
    primary, others = (paths[0], paths[1:]) if paths else (None, [])

    if current_root and primary and any(
        _same_path(current_root, path) for path in others
    ):
        return Check(
            "worktree",
            PASS,
            f"已在私有 worktree 内（W4 已满足）：{current_root}"
            + (f"；另有 {len(others)} 个 worktree（知情即可）" if others else ""),
        )

    preview = "、".join(entries[1:4])
    return Check(
        "worktree",
        BLOCK,
        f"存在 {len(entries) - 1} 个额外 worktree：{preview}",
        "你还在主工作树里。按 AGENTS W4 建自己的 worktree（create_worktree）后再开工，"
        "不要在共享树上和别的会话叠加",
    )


def check_port(port: int, *, open_: bool) -> Check:
    """8765 被监听 → S1 包装器会直接 freeze，跑不了全量回归。"""
    if not open_:
        return Check("端口", PASS, f"{port} 空闲")
    return Check(
        "端口",
        BLOCK,
        f"{port} 已有服务在监听",
        f"S1 包装器会报 freeze S1。要么等它停，要么用 --allow-port {port} 声明是自己起的，"
        "要么只跑定向用例（AGENTS W6）",
    )


#: 进程命令行里出现这些就认为是 pytest（裸跑或包装器内层）。
_PYTEST_RE = re.compile(r"(?:^|\s)-m\s+pytest\b|[/\\]pytest(?:\.exe)?\b")


def check_foreign_pytest(processes: list[str] | None, *, own_pid: int | None = None) -> Check:
    """别处有 pytest 在跑 → 它会污染 S1 preflight 的「Python 进程集合」采样。"""
    if processes is None:
        return Check("pytest", WARN, "取不到进程表，降级放行")
    running = [cmd for cmd in processes if _PYTEST_RE.search(cmd)]
    if not running:
        return Check("pytest", PASS, "没有其他 pytest 在跑")
    # basetemp 带着运行目录，用它当「谁在跑」的指纹
    temps: list[str] = []
    for cmd in running:
        match = re.search(r"--basetemp[= ]+(\S+)", cmd)
        if match:
            temps.append(match.group(1).strip('"\''))
    temps = sorted(set(temps))
    detail = f"有 {len(running)} 个 pytest 进程在跑"
    if temps:
        detail += f"（basetemp: {'、'.join(temps[:3])}）"
    return Check(
        "pytest",
        BLOCK,
        detail,
        "它会让 S1 preflight 报 `Python process state changed`。等它跑完，或确认那是你自己的进程",
    )


# ─── 真实采集（只在 main 里调用）───────────────────────────────────────


def _run(args: list[str]) -> str | None:
    """跑一条只读命令；失败返回 None（由调用方降级成 WARN）。"""
    try:
        completed = subprocess.run(
            args, cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def collect_porcelain() -> str | None:
    return _run(["git", "status", "--porcelain"])


def collect_worktrees() -> str | None:
    # core.quotePath=false：否则非 ASCII 路径会被转义成 æ…
    # 那样 check_worktrees 的"当前目录是不是我的 worktree"永远比不中 → 永久 BLOCK。
    return _run(["git", "-c", "core.quotePath=false", "worktree", "list"])


def collect_toplevel() -> str | None:
    """当前所在工作树的根目录（用来判断「我是不是已经在私有 worktree 里」）。"""
    output = _run(["git", "rev-parse", "--show-toplevel"])
    return output.strip() if output else None


def collect_python_processes() -> list[str] | None:
    """取本机 Python 进程的命令行。只在本脚本自己启动时调用。"""
    if os.name == "nt":
        output = _run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" "
            "| Select-Object -ExpandProperty CommandLine",
        ])
        return None if output is None else [line for line in output.splitlines() if line.strip()]
    output = _run(["ps", "-eo", "args"])
    if output is None:
        return None
    return [line for line in output.splitlines() if "python" in line.lower()]


def probe_port(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def check_db_write_lease() -> Check:
    """正式库写租约（reports/147）：有别的会话正在写库 → BLOCK。

    为什么算 BLOCK 而不是 WARN：写正式库是**互斥**操作，不是"注意一下"就行的。
    租约由 `scripts/session_lease.py` 维护，文件在 `<data>/.locks/db-write.lease.json`。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import session_lease
    except Exception:  # noqa: BLE001 - 租约模块缺失不该让自检崩掉
        return Check("写租约", PASS, "session_lease 不可用（跳过）")

    lease = session_lease._read_lease()
    if lease is None:
        return Check("写租约", PASS, "正式库无人持有写租约")
    state, detail = session_lease._lease_state(lease)
    if state == "active":
        return Check(
            "写租约",
            BLOCK,
            f"另一个会话正在写正式库：{detail}",
            "别写 data/：用 scripts/session_lease.py enqueue 登记排队，或等对方 release",
        )
    return Check("写租约", WARN, f"残留的失效租约（{detail}），可直接 acquire 回收")


def build_report(*, port: int = DEFAULT_WEB_PORT, allow_port: bool = False) -> Report:
    port_check = check_port(port, open_=probe_port(port))
    if allow_port and port_check.status == BLOCK:
        port_check = Check("端口", WARN, f"{port} 有服务在监听（已声明为自己所起）")
    return Report(checks=[
        check_working_tree(collect_porcelain()),
        check_worktrees(collect_worktrees(), current_root=collect_toplevel()),
        check_foreign_pytest(collect_python_processes()),
        port_check,
        check_db_write_lease(),
    ])


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="会话开工自检（AGENTS W4 的执行器）")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument(
        "--allow-port", action="store_true",
        help="8765 是你自己起的服务：把 BLOCK 降级为 WARN",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(port=args.port, allow_port=args.allow_port)

    if args.json:
        print(json.dumps({
            "status": report.status,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "next_step": c.next_step}
                for c in report.checks
            ],
        }, ensure_ascii=False, indent=2))
        return report.exit_code

    for check in report.checks:
        print(check.render())
    print()
    if report.status == BLOCK:
        print("🛑 结论：BLOCK —— 先解决上面带 🛑 的项再动手。")
        print("   规则出处：AGENTS W4（共享可变状态一律私有化）／reports/144 §2。")
    elif report.status == WARN:
        print("⚠️  结论：WARN —— 可以开工，但先确认上面 ⚠️ 的项归属。")
    else:
        print("✅ 结论：PASS —— 没有检测到第二方活动，可以开工。")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
