"""会话开工自检回归（`AGENTS W4` 的执行器，`reports/144`）。

守住的底线
----------
1. **它会失败**：每一项在「有第二方活动」的输入下必须返回 BLOCK，干净输入返回 PASS ——
   `reports/125` 的教训是「守卫空转 24 小时」比没有守卫更坏，所以每条判据都要有正反两例；
2. **脏工作区只 WARN 不 BLOCK**：单会话写到一半重启也会看到脏工作区，武断拦死会把人逼去绕过；
3. **fail-open**：探测本身失败（不是 git 仓库 / 取不到进程表）降级成 WARN 且退出码仍为 0，
   绝不因为工具坏了把仓库锁死（与 `.githooks/pre-commit` 原则 3 一致）；
4. **RG10 抓得到 U+FFFD**，且**本仓库自身**必须是干净的（含门禁脚本自己 —— 它一度写字面
   替换字符而自我引爆）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"


def _load(name: str):
    """按文件路径加载维护脚本（`scripts/` 不是包，不能按名字 import）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load("session_preflight")
guards = _load("repo_integrity_guards")

REPLACEMENT = chr(0xFFFD)


# ─── 1. 工作区 ─────────────────────────────────────────────────────


def test_clean_working_tree_passes() -> None:
    check = preflight.check_working_tree("")
    assert check.status == preflight.PASS


def test_dirty_working_tree_warns_with_file_names() -> None:
    porcelain = " M app/core/update.py\n?? app/web/api/blacklist.py\n"
    check = preflight.check_working_tree(porcelain)
    assert check.status == preflight.WARN, "脏工作区只 WARN：单会话写到一半也算脏，不能武断 BLOCK"
    assert "app/core/update.py" in check.detail
    assert check.next_step, "必须告诉人下一步做什么"


def test_missing_git_status_fails_open() -> None:
    """取不到 git 状态不是「仓库脏」的证据，不能因此拦人。"""
    check = preflight.check_working_tree(None)
    assert check.status == preflight.WARN
    assert check.status != preflight.BLOCK


# ─── 2. worktree ───────────────────────────────────────────────────


def test_single_worktree_passes() -> None:
    listing = "/repo  abc1234 [fix/audit-remediation]\n"
    assert preflight.check_worktrees(listing).status == preflight.PASS


def test_额外_worktree_必须_block() -> None:
    listing = (
        "/repo  abc1234 [main]\n"
        "/repo/.worktrees/agent-2  def5678 [feat/x]\n"
    )
    check = preflight.check_worktrees(listing)
    assert check.status == preflight.BLOCK
    assert "agent-2" in check.detail


# ─── 3. 端口 ───────────────────────────────────────────────────────


def test_free_port_passes() -> None:
    assert preflight.check_port(8765, open_=False).status == preflight.PASS


def test_occupied_port_blocks_because_s1_freezes() -> None:
    check = preflight.check_port(8765, open_=True)
    assert check.status == preflight.BLOCK
    assert "freeze" in check.next_step.lower() or "S1" in check.next_step


# ─── 4. 别人的 pytest ──────────────────────────────────────────────


def test_no_pytest_running_passes() -> None:
    assert preflight.check_foreign_pytest([]).status == preflight.PASS


def test_foreign_pytest_blocks_and_reports_basetemp() -> None:
    """这条正是 `reports/144` C3：别人的裸 pytest 让本会话的 S1 preflight 反复失败。"""
    processes = [
        r"C:\Python314\python.exe -m pytest --basetemp D:/vdtest-1/pytest-tmp tests/regression/x.py",
    ]
    check = preflight.check_foreign_pytest(processes)
    assert check.status == preflight.BLOCK
    assert "D:/vdtest-1/pytest-tmp" in check.detail


def test_auto_update_child_is_not_mistaken_for_pytest() -> None:
    """自动更新子进程 / Web 服务不是 pytest，不能误报。"""
    processes = [
        r"D:\proj\.venv\Scripts\python.exe -m app.cli.main data auto-update run",
        r"D:\proj\.venv\Scripts\python.exe -m uvicorn app.web.main:app",
    ]
    assert preflight.check_foreign_pytest(processes).status == preflight.PASS


def test_unavailable_process_table_fails_open() -> None:
    assert preflight.check_foreign_pytest(None).status == preflight.WARN


# ─── 5. 汇总与退出码 ───────────────────────────────────────────────


def test_report_exit_code_is_2_only_when_blocked() -> None:
    ok = preflight.Report(checks=[
        preflight.Check("a", preflight.PASS, ""),
        preflight.Check("b", preflight.WARN, ""),
    ])
    assert ok.status == preflight.WARN
    assert ok.exit_code == 0, "WARN 不该阻断开工"

    blocked = preflight.Report(checks=[
        preflight.Check("a", preflight.WARN, ""),
        preflight.Check("b", preflight.BLOCK, ""),
    ])
    assert blocked.status == preflight.BLOCK
    assert blocked.exit_code == 2


# ─── 6. RG10：仓库不得含 U+FFFD ────────────────────────────────────


def test_rg10_detects_replacement_char(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(f"# {REPLACEMENT} broken\n", encoding="utf-8")
    findings = guards.check_no_replacement_char(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "RG10"
    assert "app.py" in findings[0].path


def test_rg10_reports_the_line_number(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    target.write_text(f"ok\nok\nbad {REPLACEMENT}\n", encoding="utf-8")
    findings = guards.check_no_replacement_char(tmp_path)
    assert len(findings) == 1
    assert "L3" in findings[0].key


def test_rg10_ignores_clean_files_and_vendored_dirs(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text("print('ok')\n", encoding="utf-8")
    vendored = tmp_path / "node_modules"
    vendored.mkdir()
    (vendored / "dep.js").write_text(f"// {REPLACEMENT}\n", encoding="utf-8")
    assert guards.check_no_replacement_char(tmp_path) == []


def test_rg10_is_registered_in_checks() -> None:
    """门禁必须真的挂在 CHECKS 上，否则永远不会被跑（reports/125 的同型失败）。"""
    names = [label for label, _ in guards.CHECKS]
    assert any("RG10" in name for name in names), names


def test_repository_itself_has_no_replacement_char() -> None:
    """本仓库必须自洽：包括门禁脚本自己（它一度写字面替换字符而自我引爆）。"""
    findings = guards.check_no_replacement_char(_REPO)
    assert findings == [], "\n".join(f.render() for f in findings)
