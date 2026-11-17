"""反返工护栏静态检查（2026-11-17 复盘 `reports/133`）。

背景
----
最近一个月（2026-10-18 ~ 2026-11-17）46 次提交里 23 次是 `fix`，且 25 次
fix/chore 中有 15 次在 96 小时内改到与上一次提交 ≥40% 相同的文件——返工不是
"个别失误"，而是几类**固定生产机制**在反复产出同类缺陷。复盘把其中能机械化
拦截的四类固化成门禁（每条都能对上真实事故，不是假想的规范）：

RG1 时区单源
    同一语义（"北京时间今天"）曾有四处私有实现，`index_valuation_adapter`
    甚至退化成 `date.today()`。已产出两次事故：
    `d7bf840` 详情页 `snapshot_age_days=-1`、`32a2e9d` 凌晨重跑全量 universe。
    → 只允许 `app/core/financial_period.py` 定义 UTC+8 时区与 `cn_today()`。

RG2 状态合并单出口
    2026-11-12 `reports/117` P0-2：`{**后一个子步骤, "full_recompute": 前一个}`
    让 success 覆盖 rejected，对外假绿 14 天。
    → 状态一律 `job_status.merge_step_status()`；禁止在步骤聚合模块里用
      字典解包再显式覆盖 `status`。

RG3 单位注册表跨语言一致
    存储口径（小数比例 / 百分数 / 倍数）在后端 `field_units.py` 与前端
    `screening-format.ts` 各存一份，漂移就是 100 倍误差（`reports/80` 簇 A/B/C、
    `dividend_yield` 量纲、科创板成交量 ×100）。
    本检查当天就抓出两处存量漂移：`cash_content_of_revenue` 后端=倍数/前端=比例、
    `balance.advance_from_customers_ratio` 前端缺登记（会被当百分数显示）。

RG4 派生域注册表自洽
    `indicator_snapshot` 的发布是 DELETE+INSERT，凡不由计算器 staging 产出的列
    都会被静默写成 NULL（`reports/131` K5、D28、以及增量发布路径仍在清空 ROCE）。
    → 每个派生域必须在 `calculator.derived_snapshot_domains()` 登记，且登记的列
      与该域重建 SQL 实际 `SET` 的列必须完全一致（少登记一列 = 少保全一列）。

用法
----
    python scripts/check_rework_guards.py            # 人工自检
    python scripts/check_rework_guards.py --json     # 机器可读

也被 `tests/regression/test_rework_guards.py` 调用，因此会随 S1 门禁一起跑。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TIMEZONE_OWNER = "app/core/financial_period.py"
# 只扫"聚合步骤状态"的调用方。`job_status.py` 是 merge_step_status 的**实现方**，
# 它内部按严重度构造 `{**merged, "status": worst}` 是合法的单出口写法，不在此列。
STATUS_MODULES = (
    "app/core/update.py",
    "app/core/auto_update.py",
)
AGENTS_FILE = "AGENTS.md"
SCHEMA_FILE = "app/core/storage/schema.py"
FIELD_UNITS_FILE = "app/core/screening/field_units.py"
FRONTEND_UNITS_FILE = "frontend/src/utils/screening-format.ts"

_TZ_LITERAL_RE = re.compile(r"timezone\s*\(\s*timedelta\s*\(\s*hours\s*=\s*8\s*\)")
_UTC_TODAY_RE = re.compile(
    r"datetime\.now\s*\(\s*(?:UTC|timezone\.utc)\s*\)\s*\.date\s*\(\s*\)"
)
_CN_TODAY_DEF_RE = re.compile(r"^\s*def\s+(?:_?cn_today)\s*\(", re.MULTILINE)
_TS_SET_RE = r"const {name} = new Set\(\[(.*?)\]\s*\)"
_PY_SET_RE = r"^{name}:?[^=]*=\s*frozenset\(\s*\{{(.*?)\}}\s*\)"

#: AGENTS.md 的容量上限：规则越多越没人读，超限必须按「规则维护」降级（reports/138）。
MAX_AGENTS_LINES = 300
#: 只有 AGENTS.md 能定义 AI 行为规则；docs/ 下出现这类文件名说明规则又被分散了。
_FORBIDDEN_RULE_DOC = re.compile(r"(?:lessons?|教训|规则|rules)", re.IGNORECASE)
_RULE_HEADING = re.compile(r"^###\s+([WR]\d{1,2})\s+(.+)$", re.MULTILINE)

UNIT_PAIRS = (
    ("PCT_DECIMAL_FIELDS", "PCT_FIELDS"),
    ("PERCENT_STORED_FIELDS", "PERCENT_FIELDS"),
    ("RATIO_FIELDS", "RATIO_FIELDS"),
)


@dataclass(frozen=True)
class Finding:
    """一条门禁违规。`rule` 对应本文件顶部的 RG 编号。"""

    rule: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"[{self.rule}] {self.path}:{self.line} {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def _iter_python_files(root: Path, relative: str) -> list[Path]:
    base = root / relative
    if not base.exists():
        return []
    return sorted(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)


# --------------------------------------------------------------------------- RG1


def check_timezone_single_source(root: Path) -> list[Finding]:
    """UTC+8 时区与"北京时间今天"只能有一个实现（`app/core/financial_period.py`）。"""
    findings: list[Finding] = []
    owner = (root / TIMEZONE_OWNER).resolve()
    for path in _iter_python_files(root, "app") + _iter_python_files(root, "scripts"):
        text = path.read_text(encoding="utf-8")
        is_owner = path.resolve() == owner
        for number, line in enumerate(text.splitlines(), start=1):
            if is_owner or line.lstrip().startswith("#"):
                continue
            if _TZ_LITERAL_RE.search(line):
                findings.append(Finding(
                    "RG1", _rel(root, path), number,
                    "重复定义 UTC+8 时区；请 from app.core.financial_period import CN_TZ",
                ))
            elif _UTC_TODAY_RE.search(line):
                findings.append(Finding(
                    "RG1", _rel(root, path), number,
                    "用 UTC 日期冒充'今天'；请改用 cn_today()（北京时间）",
                ))
        if not is_owner:
            for match in _CN_TODAY_DEF_RE.finditer(text):
                line = text[: match.start()].count("\n") + 1
                findings.append(Finding(
                    "RG1", _rel(root, path), line,
                    "重复定义 cn_today 家族函数；请 from app.core.financial_period import cn_today",
                ))
    return findings


# --------------------------------------------------------------------------- RG2


def _dict_literal_overrides_status(node: ast.Dict) -> bool:
    """`"status"` 键出现在 `**` 解包**之后** → 后写的值覆盖了被解包的 status。

    注意顺序：`{"status": x, **optional}` 是"我先定状态、再追加字段"，不违规；
    `{**后一个子步骤, "status": 前一个}` 才是 reports/117 P0-2 那个假绿机制。
    """
    unpack_positions = [index for index, key in enumerate(node.keys) if key is None]
    if not unpack_positions:
        return False
    first_unpack = min(unpack_positions)
    for index, key in enumerate(node.keys):
        if isinstance(key, ast.Constant) and key.value == "status" and index > first_unpack:
            return True
    return False


def check_status_merge_single_exit(root: Path) -> list[Finding]:
    """步骤状态不得用字典解包覆盖，必须走 `merge_step_status()`。"""
    findings: list[Finding] = []
    for relative in STATUS_MODULES:
        path = root / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and _dict_literal_overrides_status(node):
                findings.append(Finding(
                    "RG2", relative, node.lineno,
                    '字典解包后又覆盖 "status"；状态必须交给 '
                    "app.core.job_status.merge_step_status()",
                ))
    return findings


# --------------------------------------------------------------------------- RG3


def _parse_python_unit_sets(text: str) -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {}
    for name, _ in UNIT_PAIRS:
        match = re.search(
            rf"^{name}[^=\n]*=\s*frozenset\(\s*\{{(.*?)\}}\s*\)", text, re.S | re.M
        )
        if match:
            sets[name] = set(re.findall(r'"([^"]+)"', match.group(1)))
    return sets


def _parse_ts_unit_sets(text: str) -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {}
    for _, name in UNIT_PAIRS:
        match = re.search(_TS_SET_RE.format(name=name), text, re.S)
        if match:
            sets[name] = set(re.findall(r"'([^']+)'", match.group(1)))
    return sets


def compare_unit_sets(
    python_sets: dict[str, set[str]], ts_sets: dict[str, set[str]],
) -> list[Finding]:
    """后端单位注册表必须被前端完整镜像，且同一字段不得被归到不同口径。

    判据（前端可以是超集——它还有纯展示字段）：
    * 后端每个字段都要在前端出现，否则运行时元数据缺失时会退回默认口径；
    * 后端/前端对同一字段的**口径类别**必须一致（比例 vs 百分数 = 100 倍误差）；
    * 后端自己不能把同一字段登记到两个口径。
    """
    findings: list[Finding] = []
    python_class: dict[str, str] = {}
    for python_name, ts_name in UNIT_PAIRS:
        py = python_sets.get(python_name)
        ts = ts_sets.get(ts_name)
        if py is None or ts is None:
            findings.append(Finding(
                "RG3", FIELD_UNITS_FILE, 0,
                f"找不到单位集合 {python_name} / {ts_name}（改名后必须同步本检查）",
            ))
            continue
        for name in sorted(py):
            previous = python_class.get(name)
            if previous is not None and previous != python_name:
                findings.append(Finding(
                    "RG3", FIELD_UNITS_FILE, 0,
                    f"{name} 同时被登记为 {previous} 与 {python_name}",
                ))
            python_class.setdefault(name, python_name)
    if findings:
        return findings

    ts_class: dict[str, str] = {}
    for python_name, ts_name in UNIT_PAIRS:
        for name in ts_sets.get(ts_name, set()):
            ts_class.setdefault(name, python_name)

    for name, owner in sorted(python_class.items()):
        frontend_class = ts_class.get(name)
        if frontend_class is None:
            findings.append(Finding(
                "RG3", FRONTEND_UNITS_FILE, 0,
                f"{name} 后端登记为 {owner}，前端缺失（会退回默认口径）",
            ))
        elif frontend_class != owner:
            findings.append(Finding(
                "RG3", FRONTEND_UNITS_FILE, 0,
                f"{name} 口径漂移：后端 {owner} / 前端 {frontend_class}（100 倍误差风险）",
            ))
    return findings


def _ts_class_of(ts_sets: dict[str, set[str]], name: str) -> str | None:
    for python_name, ts_name in UNIT_PAIRS:
        if name in ts_sets.get(ts_name, set()):
            return python_name
    return None


def check_unit_registry_consistency(root: Path) -> list[Finding]:
    python_path = root / FIELD_UNITS_FILE
    ts_path = root / FRONTEND_UNITS_FILE
    if not python_path.exists() or not ts_path.exists():
        return [Finding("RG3", str(FIELD_UNITS_FILE), 0, "单位注册表文件缺失")]
    return compare_unit_sets(
        _parse_python_unit_sets(python_path.read_text(encoding="utf-8")),
        _parse_ts_unit_sets(ts_path.read_text(encoding="utf-8")),
    )


# --------------------------------------------------------------------------- RG4


def parse_snapshot_columns(schema_text: str) -> set[str]:
    """从 `schema.py` 的 `indicator_snapshot` DDL 里解析列名。"""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS indicator_snapshot \((.*?)\n\);", schema_text, re.S
    )
    if not match:
        return set()
    columns: set[str] = set()
    for raw in match.group(1).splitlines():
        line = raw.split("--")[0].strip().rstrip(",")
        column = re.match(r"^([a-z_][a-z0-9_]*)\s+(?:DOUBLE|INTEGER|BIGINT|VARCHAR|DATE|TIMESTAMP|BOOLEAN)", line)
        if column:
            columns.add(column.group(1))
    return columns


def parse_sql_set_columns(sql: str) -> set[str]:
    """解析 `UPDATE ... SET a = ..., b = ...` 的目标列名。"""
    set_clause = re.search(r"\bSET\b(.*?)\bFROM\b", sql, re.S | re.I)
    if not set_clause:
        return set()
    return set(re.findall(r"([a-z_][a-z0-9_]*)\s*=", set_clause.group(1)))


def compare_domain_columns(
    domain_name: str,
    domain_columns: set[str],
    snapshot_columns: set[str],
    sql_columns: set[str] | None,
) -> list[Finding]:
    findings: list[Finding] = []
    unknown = sorted(domain_columns - snapshot_columns)
    if unknown:
        findings.append(Finding(
            "RG4", SCHEMA_FILE, 0,
            f"派生域 {domain_name} 登记的列不在 indicator_snapshot DDL 里：{', '.join(unknown)}",
        ))
    if sql_columns is not None and sql_columns != domain_columns:
        missing = sorted(sql_columns - domain_columns)
        extra = sorted(domain_columns - sql_columns)
        detail = []
        if missing:
            detail.append(f"重建 SQL 写了但没登记（不会保全）：{', '.join(missing)}")
        if extra:
            detail.append(f"登记了但重建 SQL 没写：{', '.join(extra)}")
        findings.append(Finding(
            "RG4", SCHEMA_FILE, 0, f"派生域 {domain_name} 登记与重建 SQL 不一致：{'；'.join(detail)}",
        ))
    return findings


def check_derived_domain_registry(root: Path) -> list[Finding]:
    schema_path = root / SCHEMA_FILE
    if not schema_path.exists():
        return [Finding("RG4", SCHEMA_FILE, 0, "schema.py 缺失")]
    snapshot_columns = parse_snapshot_columns(schema_path.read_text(encoding="utf-8"))
    if not snapshot_columns:
        return [Finding("RG4", SCHEMA_FILE, 0, "解析不到 indicator_snapshot 的列（DDL 改名？）")]
    sys.path.insert(0, str(root))
    try:
        from app.core.indicators.calculator import derived_snapshot_domains
    finally:
        sys.path.pop(0)
    findings: list[Finding] = []
    for domain in derived_snapshot_domains():
        sql_columns: set[str] | None = None
        module = sys.modules.get(getattr(domain.rebuild, "__module__", ""), None)
        sql = getattr(module, "SNAPSHOT_UPDATE_SQL", None) if module else None
        if isinstance(sql, str):
            sql_columns = parse_sql_set_columns(sql)
        findings.extend(
            compare_domain_columns(
                domain.name, set(domain.columns), snapshot_columns, sql_columns
            )
        )
    return findings


# --------------------------------------------------------------------------- RG5


def parse_rule_blocks(text: str) -> list[tuple[str, str, str]]:
    """把 AGENTS.md 里的 ``### W1 标题`` / ``### R1 标题`` 切成 (编号, 标题, 正文块)。"""
    headings = list(_RULE_HEADING.finditer(text))
    blocks: list[tuple[str, str, str]] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        blocks.append((match.group(1), match.group(2).strip(), text[match.end():end]))
    return blocks


def check_rules_single_source(root: Path) -> list[Finding]:
    """RG5：AI 行为规则必须只有一个落点（`AGENTS.md`），且每条规则都带证据。

    背景（`reports/138`）：一个会话把 5 条工作流教训写进了
    `docs/runbooks/agent-workflow-lessons.md`，而 AGENTS.md 里一条都没有——**下一个 AI 根本读不到**。
    规则散落 = 规则不存在。本检查钉住四件事：

    1. `AGENTS.md` 存在且 ≤ `MAX_AGENTS_LINES` 行（超限必须降级，不许无限追加）；
    2. `W*` / `R*` 编号唯一，且**每块至少引用一处证据**（`reports/NNN` / `tests/` / `scripts/`）——
       没有证据的规则下一轮就会被当成"想当然"删掉；
    3. `docs/**` 下不得出现 lesson / 教训 / 规则 / rules 命名的文档（规则不许再分家）；
    4. 全仓文档里被引用的 `W<n>` 必须在 `AGENTS.md` 里定义（防止引用已降级/改号的规则）。
    """
    findings: list[Finding] = []
    agents_path = root / AGENTS_FILE
    if not agents_path.exists():
        return [Finding("RG5", AGENTS_FILE, 0, "缺少 AGENTS.md（AI 规则的唯一来源）")]

    text = agents_path.read_text(encoding="utf-8")
    line_count = len(text.splitlines())
    if line_count > MAX_AGENTS_LINES:
        findings.append(Finding(
            "RG5", AGENTS_FILE, line_count,
            f"规则文件 {line_count} 行，超过上限 {MAX_AGENTS_LINES}：请按「规则维护」降级旧规则，"
            "不要继续追加",
        ))

    blocks = parse_rule_blocks(text)
    if not blocks:
        findings.append(Finding("RG5", AGENTS_FILE, 0, "解析不到任何 `### W<n>` / `### R<n>` 规则块"))
        return findings

    seen: dict[str, int] = {}
    for rule_id, title, body in blocks:
        if rule_id in seen:
            findings.append(Finding("RG5", AGENTS_FILE, 0, f"规则编号 {rule_id} 重复定义"))
        seen[rule_id] = 1
        if not re.search(r"reports/\d+|tests/|scripts/", body):
            findings.append(Finding(
                "RG5", AGENTS_FILE, 0, f"{rule_id}（{title}）没有引用证据（reports/NNN / tests/ / scripts/）",
            ))

    for path in sorted((root / "docs").rglob("*.md")):
        if _FORBIDDEN_RULE_DOC.search(path.stem):
            findings.append(Finding(
                "RG5", _rel(root, path), 0,
                "docs/ 下出现 lesson/规则 命名的文档：AI 规则只能写在 AGENTS.md；"
                "事件证据请写 docs/reports/",
            ))

    referenced: set[str] = set()
    for source in [agents_path, *sorted((root / "docs").rglob("*.md"))]:
        try:
            referenced.update(re.findall(r"\bW\d{1,2}\b", source.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    undefined = sorted(referenced - set(seen))
    if undefined:
        findings.append(Finding(
            "RG5", AGENTS_FILE, 0,
            f"文档里引用了未定义的规则编号：{', '.join(undefined)}（可能已降级或改号）",
        ))
    return findings


# --------------------------------------------------------------------------- main


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def run_all(root: Path = PROJECT_ROOT) -> Report:
    report = Report()
    for name, check in (
        ("RG1 时区单源", check_timezone_single_source),
        ("RG2 状态合并单出口", check_status_merge_single_exit),
        ("RG3 单位注册表跨语言一致", check_unit_registry_consistency),
        ("RG4 派生域注册表自洽", check_derived_domain_registry),
        ("RG5 规则单一来源", check_rules_single_source),
    ):
        findings = check(root)
        report.checked.append(name)
        report.findings.extend(findings)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="反返工护栏静态检查（reports/133 / reports/138）")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="仓库根目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = run_all(Path(args.root).resolve())
    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "checked": report.checked,
            "findings": [
                {"rule": f.rule, "path": f.path, "line": f.line, "message": f.message}
                for f in report.findings
            ],
        }, ensure_ascii=False, indent=2))
    else:
        for finding in report.findings:
            print(finding.render())
        if report.ok:
            print(f"反返工护栏通过（{len(report.checked)} 组规则）")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
