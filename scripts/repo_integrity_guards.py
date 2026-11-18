"""RG6–RG9 仓库完整性门禁（`reports/140`，承 `reports/133` §7 升级条款）。

为什么需要这一组
----------------
`scripts/check_rework_guards.py` 的 RG1–RG5 只覆盖**代码语义**（时区/状态/单位/派生列/规则来源）。
2026-11-18 第二轮复盘实测：规则落地后**代码返工几乎归零，返工整体搬到了文档侧**——
`reports/139` 单文件 6 小时内改 11 次（5 次纯"更正"），`docs/STATUS.md` 被改 128 次。
RG1–RG5 全是静态代码匹配，**没有一条看得见"同一个数字在 4 个文档里手抄 4 遍"**。

四条新规则：

- **RG6 报告编号唯一 + front-matter 合法**：编号重复会让 `reports/133` 这类引用产生二义
  （实测 127/133/139/59 各两份，其中 133 是真冲突：`133_ROCE_WINDOWS` 说自己取代 132，
  而全库 `reports/133` 默认指"返工复盘"）。
- **RG7 supersede 双向闭合**：A 声明 `supersedes: B` 时，B 必须真的被标 `superseded` 且
  `superseded-by` 指回 A。实测违规：`133_ROCE_WINDOWS` 取代 `132_ROCE_FINAL`，而 132
  至今 `status: approved` —— 新人照唯一真相源建认知会拿到**作废的 ROCE 口径**，且无任何机械信号。
- **RG8 真相源不得引用已作废结论**：`docs/STATUS.md` 的「当前裁决」表是唯一真相源，
  表内引用 superseded 报告 = 真相源被污染（实测 29/75 行）。
- **RG9 测试资产必须入库**：`tests/` 下任何文件不得被 gitignore 吞掉。
  实测事故（存活 4 个月）：`.gitignore` 的 `tests/regression/[0-9a-f]*/` 把
  `tests/regression/fixtures/`（首字母 f）整个忽略，4 个 sina 夹具从未入库，
  **新克隆跑 `test_sina_adapter.py` 必 FileNotFoundError**，而本机永远绿。

判定策略（宁可漏报，不要误报）
------------------------------
存量违规用**基线棘轮**（`docs/evidence/repo-integrity-baseline.json`）冻结：
基线里没有的新违规立刻红；已修好的会在输出里提示"可下调基线"。
这样门禁能上线当天即绿，又不给新增开口子 —— 照 `reports/133` §7 的做法而不是"先修出口"。

用法::

    uv run --locked python scripts/repo_integrity_guards.py            # 人读
    uv run --locked python scripts/repo_integrity_guards.py --json     # 机器读
    uv run --locked python scripts/repo_integrity_guards.py --update-baseline   # 显式下调
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = Path("docs/reports")
STATUS_FILE = Path("docs/STATUS.md")
BASELINE_FILE = Path("docs/evidence/repo-integrity-baseline.json")

VALID_STATUS = {"approved", "superseded", "historical", "archived"}
REQUIRED_META = ("status", "category", "last-reviewed")
# 报告编号引用：reports/133、reports/133_REWORK_...、`reports/133` §7
REPORT_REF = re.compile(r"reports/(\d{1,3})(?![\d])")


@dataclass
class Finding:
    rule: str
    path: str
    key: str
    message: str

    def signature(self) -> str:
        return f"{self.rule}|{self.path}|{self.key}"

    def render(self) -> str:
        return f"[{self.rule}] {self.path}: {self.message}"


@dataclass
class Report:
    checked: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def parse_front_matter(path: Path) -> dict[str, str] | None:
    """返回 front-matter 键值；无 front-matter 返回 None。

    只解析开头 `---` 到第二个 `---` 之间的 `key: value`，不追求完整 YAML
    （仓库里的 front-matter 都是扁平的，避免引入 PyYAML 依赖）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return meta
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("'\"")
    return None


def report_number(path: Path) -> str | None:
    match = re.match(r"(\d{1,3})_", path.name)
    return match.group(1) if match else None


# front-matter 的 supersedes / superseded-by 有时写成人话（如
# `superseded-by: docs/reports/123（分红除权日改走结构化源，同日；以实际落盘的…为准）`），
# 所以只提取其中**确实是报告引用**的 token，不要把散文片段当目标。
REPORT_TOKEN = re.compile(
    r"reports/(\d{1,3})(?:_([A-Za-z0-9_\-]+))?(?:\.md)?"
)
BARE_REPORT_TOKEN = re.compile(r"(?<![\w/])(\d{1,3})_([A-Za-z0-9_\-]+)\.md")
NULLISH = {"", "null", "none", "n/a", "-", "无", "na"}


def supersede_targets(meta: dict[str, str], key: str) -> list[str]:
    """提取 front-matter 里的报告引用，返回规范化 token。"""
    raw = meta.get(key, "").strip()
    if raw.lower() in NULLISH:
        return []
    tokens: list[str] = []
    for match in REPORT_TOKEN.finditer(raw):
        number, stem = match.group(1), match.group(2)
        tokens.append(f"{number}_{stem}" if stem else f"{number}")
    for match in BARE_REPORT_TOKEN.finditer(raw):
        token = f"{match.group(1)}_{match.group(2)}"
        if token not in tokens:
            tokens.append(token)
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


# --------------------------------------------------------------------- RG6


def check_report_identity(root: Path) -> list[Finding]:
    """RG6：报告编号唯一、front-matter 存在且合法。"""
    findings: list[Finding] = []
    reports_dir = root / REPORTS_DIR
    if not reports_dir.is_dir():
        return [Finding("RG6", str(REPORTS_DIR), "missing", "报告目录不存在")]

    by_number: dict[str, list[str]] = {}
    for path in sorted(reports_dir.glob("*.md")):
        rel = _rel(root, path)
        number = report_number(path)
        if number is not None:
            by_number.setdefault(number, []).append(path.name)

        meta = parse_front_matter(path)
        if meta is None:
            findings.append(Finding(
                "RG6", rel, "no-front-matter",
                "缺少 front-matter（status/category/last-reviewed 无法判定，会被当成当前结论）",
            ))
            continue
        missing = [key for key in REQUIRED_META if not meta.get(key)]
        if missing:
            findings.append(Finding(
                "RG6", rel, f"missing-{','.join(missing)}",
                f"front-matter 缺必需字段：{', '.join(missing)}",
            ))
        status = meta.get("status", "")
        if status and status not in VALID_STATUS:
            findings.append(Finding(
                "RG6", rel, f"bad-status-{status}",
                f"status 非法：{status!r}（只允许 {sorted(VALID_STATUS)}）",
            ))

    for number, names in sorted(by_number.items()):
        if len(names) > 1:
            findings.append(Finding(
                "RG6", f"{REPORTS_DIR.as_posix()}/{number}_*", f"dup-{number}",
                f"报告编号 {number} 重复 {len(names)} 份（引用产生二义）：{', '.join(names)}",
            ))
    return findings


# --------------------------------------------------------------------- RG7


def check_supersede_closure(root: Path) -> list[Finding]:
    """RG7：supersede 双向闭合 —— A 取代 B ⇒ B 必须 superseded 且指回 A。"""
    findings: list[Finding] = []
    reports_dir = root / REPORTS_DIR
    if not reports_dir.is_dir():
        return findings

    metas: dict[str, dict[str, str]] = {}
    for path in sorted(reports_dir.glob("*.md")):
        meta = parse_front_matter(path)
        if meta is None:
            continue
        rel = _rel(root, path)
        metas[rel] = meta

    def resolve(reference: str) -> str | None:
        """把 `132_ROCE_FINAL_2026-11-16` / `132` 解析成仓库相对路径。"""
        if "_" in reference:
            number, _, stem = reference.partition("_")
            for rel in metas:
                if Path(rel).stem == f"{number}_{stem}":
                    return rel
            # 只给了编号+部分名：退回按编号解析
        else:
            number = reference
        hits = [rel for rel in metas if report_number(Path(rel)) == number]
        return sorted(hits)[0] if hits else None

    for rel, meta in metas.items():
        status = meta.get("status", "")
        superseded_by = supersede_targets(meta, "superseded-by")

        # 7a：声明 superseded 必须指出去向，否则读者无从追溯
        if status == "superseded" and not superseded_by:
            findings.append(Finding(
                "RG7", rel, "superseded-without-by",
                "status: superseded 但缺 superseded-by（读者无从追溯被谁取代）",
            ))
        # 7b：superseded-by 必须能解析到真实报告
        for target in superseded_by:
            if resolve(target) is None:
                findings.append(Finding(
                    "RG7", rel, f"dangling-by-{target}",
                    f"superseded-by 指向不存在的报告：{target}",
                ))
        # 7c：本文件声明取代 B ⇒ B 必须真的降级（这是 132 事故的形态）
        for target in supersede_targets(meta, "supersedes"):
            target_rel = resolve(target)
            if target_rel is None:
                findings.append(Finding(
                    "RG7", rel, f"dangling-supersedes-{target}",
                    f"supersedes 指向不存在的报告：{target}",
                ))
                continue
            if target_rel == rel:
                continue
            target_meta = metas[target_rel]
            if target_meta.get("status") != "superseded":
                findings.append(Finding(
                    "RG7", rel, f"not-demoted-{Path(target_rel).name}",
                    f"本文件声明取代 {Path(target_rel).name}，但它仍是 "
                    f"status: {target_meta.get('status', '(缺失)')!r} —— "
                    "照真相源建认知会拿到作废口径",
                ))
    return findings


# --------------------------------------------------------------------- RG8


def _status_by_number(root: Path) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    reports_dir = root / REPORTS_DIR
    if not reports_dir.is_dir():
        return index
    for path in reports_dir.glob("*.md"):
        number = report_number(path)
        if number is None:
            continue
        meta = parse_front_matter(path) or {}
        index.setdefault(number, set()).add(meta.get("status", "(无front-matter)"))
    return index


def check_truth_source_cites(root: Path) -> list[Finding]:
    """RG8：`docs/STATUS.md` 不得把 superseded 报告当当前结论引用。

    只查「当前裁决」表 —— 其余小节是历史叙述，引用旧报告是合理追溯。
    """
    findings: list[Finding] = []
    status_path = root / STATUS_FILE
    if not status_path.is_file():
        return findings
    index = _status_by_number(root)
    text = status_path.read_text(encoding="utf-8", errors="replace").splitlines()

    in_section = False
    hits: dict[str, list[int]] = {}
    for lineno, line in enumerate(text, start=1):
        if line.startswith("## "):
            in_section = "当前裁决" in line
            continue
        if not in_section or not line.startswith("|"):
            continue
        for number in sorted(set(REPORT_REF.findall(line)), key=int):
            statuses = index.get(number)
            if not statuses:
                continue  # 引用不存在的编号交给别处（可能是 reports/ 之外的编号体系）
            if statuses <= {"superseded"}:
                hits.setdefault(number, []).append(lineno)

    # 签名只按「报告编号」聚合，**不带行号** —— 基线一旦含行号，
    # 任何在表里插一行都会让全部存量签名失效（本门禁首次上线就踩了这个坑：
    # 插入 reports/140 那行后，28 条基线瞬间全部"变成新违规"）。
    for number in sorted(hits, key=int):
        lines = hits[number]
        findings.append(Finding(
            "RG8", _rel(root, status_path), f"cites-superseded-r{number}",
            f"「当前裁决」表把 reports/{number}（superseded）当当前依据引用"
            f"（第 {', '.join(map(str, lines))} 行）",
        ))
    return findings


# --------------------------------------------------------------------- RG9


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


# `evidence/` = 测试跑出来的产物（截图/JSON），按设计不入库，不是夹具。
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".venv", "evidence"}


def check_test_assets_tracked(root: Path) -> list[Finding]:
    """RG9：`tests/` 与 `frontend/tests/` 下的文件不得被 gitignore 吞掉。

    实测事故：`.gitignore` 的 `tests/regression/[0-9a-f]*/` 命中 `fixtures/`，
    4 个夹具从未入库，新克隆必红而本机全绿。
    """
    findings: list[Finding] = []
    candidates = [root / "tests", root / "frontend" / "tests"]
    existing = [path for path in candidates if path.is_dir()]
    if not existing:
        return findings

    tracked = set()
    result = _git(root, "ls-files", "-z")
    if result.returncode != 0:
        return [Finding("RG9", "git", "no-git", "无法读取 git 索引，RG9 跳过")]
    tracked = {entry for entry in result.stdout.split("\0") if entry}

    for base in existing:
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = _rel(root, path)
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if rel in tracked:
                continue
            ignored = _git(root, "check-ignore", "-q", rel).returncode == 0
            findings.append(Finding(
                "RG9", rel, "ignored" if ignored else "untracked",
                "被 .gitignore 吞掉 → 新克隆缺失，本机却全绿（必红测试资产）"
                if ignored else
                "存在于工作区但未入库 → 新克隆缺失",
            ))
    return findings


# --------------------------------------------------------------------- 基线


def load_baseline(root: Path) -> set[str]:
    path = root / BASELINE_FILE
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("known", []))


def save_baseline(root: Path, signatures: list[str]) -> None:
    path = root / BASELINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": (
            "RG6–RG9 存量违规基线（棘轮）。只允许减少：修好一条就删一行。"
            "新增违规不受基线保护，门禁会红。生成：--update-baseline"
        ),
        "known": sorted(signatures),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


CHECKS = (
    ("RG6 报告编号唯一 + front-matter 合法", check_report_identity),
    ("RG7 supersede 双向闭合", check_supersede_closure),
    ("RG8 真相源不引用作废结论", check_truth_source_cites),
    ("RG9 测试资产必须入库", check_test_assets_tracked),
)


def run_all(root: Path = PROJECT_ROOT, baseline: set[str] | None = None) -> Report:
    report = Report()
    known = load_baseline(root) if baseline is None else baseline
    fresh: list[Finding] = []
    for name, check in CHECKS:
        report.checked.append(name)
        for finding in check(root):
            if finding.signature() in known:
                report.notes.append(f"（基线内，未计红）{finding.render()}")
            else:
                fresh.append(finding)
    report.findings = fresh
    return report


def main(argv: list[str] | None = None) -> int:
    # 控制台默认 GBK 会把中文打成 mojibake（reports/138 §3.1 的同类事故）
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="RG6–RG9 仓库完整性门禁（reports/140）")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="把当前全部违规写进基线（只应在存量冻结时用一次；修好后应下调）",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.update_baseline:
        everything: list[str] = []
        for _, check in CHECKS:
            everything.extend(f.signature() for f in check(root))
        save_baseline(root, everything)
        print(f"基线已写入 {BASELINE_FILE}（{len(everything)} 条）")
        return 0

    report = run_all(root)
    if args.json:
        print(json.dumps({
            "ok": not report.findings,
            "checked": report.checked,
            "findings": [
                {"rule": f.rule, "path": f.path, "message": f.message}
                for f in report.findings
            ],
            "baselined": len(report.notes),
        }, ensure_ascii=False, indent=2))
    else:
        for finding in report.findings:
            print(finding.render())
        if report.findings:
            print(f"\n{len(report.findings)} 条新违规（基线内 {len(report.notes)} 条未计红）")
        else:
            print(f"仓库完整性护栏通过（{len(report.checked)} 组规则，基线内 {len(report.notes)} 条）")
    return 0 if not report.findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
