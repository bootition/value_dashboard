"""维护脚本安全门禁回归（J11 / reports/120 §5.6）。

覆盖：
- 主仓 `scripts/**/*.py` 不得出现未守卫的主表 DELETE（WHERE + --yes/dry_run）；
- `patch_deducted_profit.py` 已移入 `scripts/legacy_dangerous/`，原地不存在；
- 隔离目录里的危险脚本若被直接扫描仍会被判违规（证明检查不是摆设）；
- 没有任何模块 import 隔离目录 / 危险函数；
- 合成危险脚本 → 检查器退出 1；合成安全脚本 → 退出 0。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_no_dangerous_maintenance import (  # noqa: E402
    FULL_TABLE_RELOAD_ALLOWLIST,
    MAIN_TABLES,
    scan_directory,
    scan_script,
)

LEGACY_FILE = SCRIPTS_DIR / "legacy_dangerous" / "patch_deducted_profit.py"
LEGACY_README = SCRIPTS_DIR / "legacy_dangerous" / "README.md"


def test_main_scripts_have_no_unguarded_main_table_delete() -> None:
    scripts, findings = scan_directory(SCRIPTS_DIR)

    assert len(scripts) > 10
    assert set(MAIN_TABLES) == {
        "income_statement", "balance_sheet", "cash_flow",
        "price_daily_raw", "price_daily_qfq", "indicator_snapshot",
    }
    violations = [finding for finding in findings if finding.violation]
    assert violations == [], "\n".join(item.describe() for item in violations)
    # 现存的全表重载必须显式登记白名单，不能靠"恰好没触发"蒙混过关。
    allowlisted = {
        f"{finding.path.name}:{finding.table}"
        for finding in findings
        if finding.allowlisted
    }
    assert allowlisted <= FULL_TABLE_RELOAD_ALLOWLIST


def test_dangerous_patch_script_is_quarantined_with_warning() -> None:
    assert not (SCRIPTS_DIR / "patch_deducted_profit.py").exists()
    assert LEGACY_FILE.is_file()
    assert LEGACY_README.is_file()

    source = LEGACY_FILE.read_text(encoding="utf-8")
    header = source[:1500]
    assert "危险脚本" in header
    assert "禁止在正式库执行" in header
    assert "fetch_deducted_profit.py" in header
    assert "DELETE FROM income_statement" in source
    # 移动后仍要能定位项目根（parents[2]），不能沿用 scripts/ 时代的 parents[1]。
    assert "Path(__file__).resolve().parents[2]" in source

    readme = LEGACY_README.read_text(encoding="utf-8")
    assert "全量回灌" in readme
    assert "DELETE FROM income_statement" in readme
    assert "fetch_deducted_profit.py" in readme


def test_legacy_dangerous_script_would_be_flagged_if_scanned_directly() -> None:
    findings = scan_script(LEGACY_FILE, root=SCRIPTS_DIR)

    assert findings, "隔离脚本里的主表 DELETE 必须能被扫描到"
    assert all(finding.table == "income_statement" for finding in findings)
    assert all(finding.violation for finding in findings), (
        "import_to_db() 没有 --yes/dry_run 守卫，必须判违规"
    )
    # 但目录级扫描排除 legacy_dangerous/，主仓应保持零违规。
    scripts, main_findings = scan_directory(SCRIPTS_DIR)
    assert LEGACY_FILE not in scripts
    assert all(not finding.violation for finding in main_findings)


def test_no_module_imports_legacy_dangerous_or_patch_function() -> None:
    import_pattern = re.compile(
        r"^\s*(?:from|import)\s+[^\n]*(?:legacy_dangerous|patch_deducted_profit)",
        re.MULTILINE,
    )

    offenders: list[str] = []
    for base in (PROJECT_ROOT / "app", SCRIPTS_DIR, PROJECT_ROOT / "tests"):
        for path in base.rglob("*.py"):
            if "legacy_dangerous" in path.parts:
                continue
            if import_pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == [], f"危险脚本不得被 import: {offenders}"


def _write_script(path: Path, body: str) -> None:
    path.write_text(
        'import duckdb\n\n\n' + body,
        encoding="utf-8",
    )


def test_verifier_exits_one_for_synthetic_unguarded_deletes(tmp_path: Path) -> None:
    _write_script(
        tmp_path / "danger.py",
        '''
def wipe_range(conn):
    conn.execute("DELETE FROM income_statement WHERE report_date > '2025-03-31'")


def wipe_all(conn):
    conn.execute("DELETE FROM cash_flow")
''',
    )

    from scripts.verify_no_dangerous_maintenance import main as verifier_main

    assert verifier_main(["--root", str(tmp_path)]) == 1


def test_verifier_exits_zero_for_guarded_and_allowlisted_deletes(tmp_path: Path) -> None:
    _write_script(
        tmp_path / "safe.py",
        '''
def cleanup(conn, dry_run):
    conn.execute("DELETE FROM indicator_snapshot WHERE stock_code = ?")
''',
    )
    # 白名单文件 + 显式 dry_run：允许整表重载，不误报。
    _write_script(
        tmp_path / "import_csmar.py",
        '''
def import_balance_sheet(conn, source_dir, dry_run):
    if dry_run:
        return
    conn.execute("DELETE FROM balance_sheet")
''',
    )

    from scripts.verify_no_dangerous_maintenance import main as verifier_main

    assert verifier_main(["--root", str(tmp_path)]) == 0


def test_verifier_ignores_docstring_examples_and_legacy_directory(tmp_path: Path) -> None:
    (tmp_path / "legacy_dangerous").mkdir()
    _write_script(
        tmp_path / "docs_only.py",
        '''
"""历史说明：旧脚本执行 DELETE FROM income_statement WHERE report_date > '2025-03-31'。"""
''',
    )
    _write_script(
        tmp_path / "legacy_dangerous" / "old.py",
        '''
def wipe(conn):
    conn.execute("DELETE FROM balance_sheet")
''',
    )

    from scripts.verify_no_dangerous_maintenance import main as verifier_main

    assert verifier_main(["--root", str(tmp_path)]) == 0
