"""RG6–RG9 仓库完整性门禁的**反向用例**（`reports/140`）。

按 `AGENTS.md` R2 的硬要求：新增探针必须同时写"它会失败"的反向用例 ——
`reports/125` 的教训是守卫空转 24 小时没人发现，因为只测了"通过"没测"该红时红"。

每个用例都在 `tmp_path` 里造一棵**最小违规树**，断言检查器**必须**报出来；
最后一个用例造一棵**干净树**，断言它**不报**（防假阳性，`RG1` 的旧正则就是在
注释上误报、在真实违规上漏报，两个病根同一个）。

事故映射：
- RG6 ← 127/133/139/59 编号各两份（`reports/133` 引用二义）
- RG7 ← `133_ROCE_WINDOWS` 声称取代 `132_ROCE_FINAL`，而 132 至今 `approved`
- RG8 ← `docs/STATUS.md`「当前裁决」表 29/75 行引用 superseded
- RG9 ← `.gitignore` 的 `[0-9a-f]*` 误吞 `tests/regression/fixtures/`（存活 4 个月）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.repo_integrity_guards import (  # noqa: E402
    check_report_identity,
    check_supersede_closure,
    check_test_assets_tracked,
    check_truth_source_cites,
    load_baseline,
    run_all,
)


def _report(path: Path, *, status: str = "approved", extra: str = "", body: str = "正文") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: t\nstatus: {status}\ncategory: reports\n"
        f"last-reviewed: 2026-11-18\n{extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)


# ---------------------------------------------------------------- RG6


def test_rg6_detects_duplicate_report_numbers(tmp_path: Path) -> None:
    _report(tmp_path / "docs/reports/133_REWORK_RETROSPECTIVE_2026-11-17.md")
    _report(tmp_path / "docs/reports/133_ROCE_WINDOWS_2026-11-16.md")

    findings = check_report_identity(tmp_path)

    assert any("dup-133" in f.key for f in findings), "133 重号必须被抓到"


def test_rg6_detects_missing_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "docs/reports/112_NO_META_2026-09-08.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 没有 front-matter 的报告\n", encoding="utf-8")

    findings = check_report_identity(tmp_path)

    assert any(f.key == "no-front-matter" for f in findings)


def test_rg6_detects_invalid_status(tmp_path: Path) -> None:
    _report(tmp_path / "docs/reports/01_X_2026-01-01.md", status="current-ish")

    findings = check_report_identity(tmp_path)

    assert any("bad-status" in f.key for f in findings)


# ---------------------------------------------------------------- RG7


def test_rg7_detects_supersedes_target_not_demoted(tmp_path: Path) -> None:
    """132 事故：A 说取代 B，B 还写着 approved。"""
    _report(tmp_path / "docs/reports/132_ROCE_FINAL_2026-11-16.md", status="approved")
    _report(
        tmp_path / "docs/reports/133_ROCE_WINDOWS_2026-11-16.md",
        extra="supersedes: reports/132_ROCE_FINAL_2026-11-16.md\n",
    )

    findings = check_supersede_closure(tmp_path)

    assert any("not-demoted" in f.key for f in findings), (
        "被取代者仍 approved 必须被抓到（新人会读到作废口径）"
    )


def test_rg7_detects_dangling_superseded_by(tmp_path: Path) -> None:
    _report(
        tmp_path / "docs/reports/121_X_2026-11-15.md",
        status="superseded",
        extra="superseded-by: reports/999_NOT_THERE_2026-11-15.md\n",
    )

    findings = check_supersede_closure(tmp_path)

    assert any("dangling-by" in f.key for f in findings)


def test_rg7_detects_superseded_without_pointer(tmp_path: Path) -> None:
    _report(tmp_path / "docs/reports/129_X_2026-11-16.md", status="superseded")

    findings = check_supersede_closure(tmp_path)

    assert any(f.key == "superseded-without-by" for f in findings)


def test_rg7_ignores_prose_valued_superseded_by(tmp_path: Path) -> None:
    """front-matter 写成人话时不得误报（RG1 假阳性的同型病）。"""
    _report(
        tmp_path / "docs/reports/123_Y_2026-11-15.md",
        extra="supersedes: reports/121_X_2026-11-15.md\n",
    )
    path = tmp_path / "docs/reports/121_X_2026-11-15.md"
    _report(
        path,
        status="superseded",
        extra="superseded-by: docs/reports/123（分红除权日改走结构化源，同日；以实际落盘为准）\n",
    )

    findings = check_supersede_closure(tmp_path)

    assert not [f for f in findings if "dangling" in f.key], "散文式 superseded-by 不应报悬空"


# ---------------------------------------------------------------- RG8


def test_rg8_detects_truth_source_citing_superseded(tmp_path: Path) -> None:
    _report(tmp_path / "docs/reports/80_RED_TEAM_2026-08-14.md", status="superseded",
            extra="superseded-by: reports/81_X_2026-08-14.md\n")
    _report(tmp_path / "docs/reports/81_X_2026-08-14.md")
    status = tmp_path / "docs/STATUS.md"
    status.write_text(
        "## 当前裁决（Verdict）\n\n| 层 | 状态 | 依据 |\n|---|---|---|\n"
        "| 整体 | PASS | `reports/80`（已作废的审查） |\n",
        encoding="utf-8",
    )

    findings = check_truth_source_cites(tmp_path)

    assert findings, "真相源引用 superseded 必须被抓到"


def test_rg8_ignores_historical_sections(tmp_path: Path) -> None:
    """「当前裁决」之外的历史叙述引用旧报告是合理追溯，不该报。"""
    _report(tmp_path / "docs/reports/80_RED_TEAM_2026-08-14.md", status="superseded",
            extra="superseded-by: reports/81_X_2026-08-14.md\n")
    _report(tmp_path / "docs/reports/81_X_2026-08-14.md")
    status = tmp_path / "docs/STATUS.md"
    status.write_text(
        "## 当前裁决（Verdict）\n\n| 层 | 状态 | 依据 |\n|---|---|---|\n"
        "| 整体 | PASS | `reports/81` |\n\n"
        "## 历史（时不我待）\n\n| 层 | 依据 |\n|---|---|\n| 旧 | `reports/80` |\n",
        encoding="utf-8",
    )

    assert check_truth_source_cites(tmp_path) == []


# ---------------------------------------------------------------- RG9


def test_rg9_detects_test_asset_swallowed_by_gitignore(tmp_path: Path) -> None:
    """复刻真实事故：`[0-9a-f]*` 把 `fixtures/` 当成 hash 证据目录吞掉。"""
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("tests/regression/[0-9a-f]*/\n", encoding="utf-8")
    fixture = tmp_path / "tests/regression/fixtures/sina/sina_lrb_600519.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("{}", encoding="utf-8")

    findings = check_test_assets_tracked(tmp_path)

    assert any(f.path.endswith("sina_lrb_600519.json") and f.key == "ignored" for f in findings), (
        "被 gitignore 吞掉的测试夹具必须报出来（新克隆必红、本机全绿）"
    )


def test_rg9_passes_when_fixture_negated(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text(
        "tests/regression/[0-9a-f]*[0-9]*/\n!tests/regression/fixtures/\n", encoding="utf-8"
    )
    fixture = tmp_path / "tests/regression/fixtures/sina/x.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)

    assert check_test_assets_tracked(tmp_path) == []


# ---------------------------------------------------------------- 基线棘轮


def test_baseline_suppresses_known_but_new_violation_still_fails(tmp_path: Path) -> None:
    _report(tmp_path / "docs/reports/133_A_2026-11-17.md")
    _report(tmp_path / "docs/reports/133_B_2026-11-16.md")

    # 基线覆盖当前这条
    _ = run_all(tmp_path, baseline={"RG6|docs/reports/133_*|dup-133"})
    known = load_baseline(tmp_path)
    assert known == set(), "仓库里没写基线文件时应为空"

    (tmp_path / "docs/evidence").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs/evidence/repo-integrity-baseline.json").write_text(
        '{"known": ["RG6|docs/reports/133_*|dup-133"]}', encoding="utf-8"
    )
    report = run_all(tmp_path)
    assert report.findings == [], "基线内的存量违规不该计红"

    # 再长一条新的，必须红
    _report(tmp_path / "docs/reports/139_A_2026-11-18.md")
    _report(tmp_path / "docs/reports/139_B_2026-11-18.md")
    report = run_all(tmp_path)
    assert any("dup-139" in f.key for f in report.findings), "基线只保护存量，新增必须红"


@pytest.mark.parametrize("check", [
    check_report_identity,
    check_supersede_closure,
    check_truth_source_cites,
])
def test_clean_tree_has_no_findings(tmp_path: Path, check) -> None:  # noqa: ANN001
    _report(tmp_path / "docs/reports/01_OK_2026-01-01.md")
    status = tmp_path / "docs/STATUS.md"
    status.write_text(
        "## 当前裁决（Verdict）\n\n| 层 | 状态 | 依据 |\n|---|---|---|\n"
        "| 整体 | PASS | `reports/01` |\n",
        encoding="utf-8",
    )

    assert check(tmp_path) == [], f"{check.__name__} 在干净树上不该有发现（防假阳性）"
