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
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TIMEZONE_OWNER = "app/core/financial_period.py"
# RG2 扫描范围：`app/core/**` 下所有"含步骤聚合语义"的模块（下面按 AST 判定，不再手写清单）。
STATUS_SCAN_ROOT = "app/core"
# `job_status.py` 是 merge_step_status 的**实现方**：它内部按严重度构造
# `{**merged, "status": worst}` 是合法的单出口写法，必须豁免（否则门禁要求它
# 不要用自己）。
RG2_EXCLUDED: dict[str, str] = {
    "app/core/job_status.py": "merge_step_status() 的实现方本身：单出口就在这个函数里",
}
#: 一个模块要"含步骤聚合语义"才会被 RG2 视为调用方：必须出现这些 tokens 之一。
_RG2_STEP_SEMANTICS = re.compile(
    r"report_step|merge_step_status|STEP_NAMES|STEP_ORDER|STEP_LABELS"
    r"|\bsteps?\b|\bstep_name\b|sub_step",
)
AGENTS_FILE = "AGENTS.md"
SCHEMA_FILE = "app/core/storage/schema.py"
FIELD_UNITS_FILE = "app/core/screening/field_units.py"
FRONTEND_UNITS_FILE = "frontend/src/utils/screening-format.ts"

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
    baselined: int = 0

    @property
    def ok(self) -> bool:
        return not self.findings


def _iter_python_files(root: Path, relative: str) -> list[Path]:
    base = root / relative
    if not base.exists():
        return []
    return sorted(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)


# --------------------------------------------------------------------------- RG1

#: 北京时间区 ID：出现即说明有人在自己拼一份 UTC+8（唯一所有者除外）。
_CN_ZONE_IDS = frozenset({
    "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Macau", "Asia/Taipei",
    "Asia/Chongqing", "PRC", "Etc/GMT-8",
})

#: RG1 显式白名单：**只豁免"时刻语义"的 naive 时钟**（路径 glob + 理由）。
#:
#: 白名单**不豁免**任何"今天"语义——`today()`、日期粒度使用（`.date()` / `.year` /
#: 日期格式 `strftime`）、`utcnow()`、时区字面量一律按硬规则报。这条边界是 RG1
#: 曾经 0% 拦截率的教训：白名单只要能让整类写法闭嘴，下一个违规写进被豁免的文件就永远绿。
RG1_INSTANT_ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        "scripts/**",
        "scripts/ 下是一次性运维/考古脚本：datetime.now() 只用于备份目录名、证据文件名与"
        "日志时间戳，不参与任何'今天'/交易日判断；改成北京时区会让历史产物命名与既有证据"
        "不可复现，故只豁免时刻语义（today() 与日期格式仍按硬规则报）。",
    ),
    (
        "app/cli/main.py",
        "manual_overrides.rolled_back_at 是审计'时刻'（naive 本地），不参与'今天'/交易日判断。"
        "⚠️ 已知口径不一致：同表 created_at 是 `DEFAULT CURRENT_TIMESTAMP`（UTC 基准），"
        "两者差 8 小时。这里**只豁免时区**、不认可这个不一致——要不要把 rolled_back_at "
        "统一成 UTC 是独立的口径变更（需改表+迁移历史行），不在 RG1 范围内。",
    ),
    (
        "app/core/auto_update.py",
        "进度日志里的 HH:MM:SS 是给人看的展示时间（前端原样显示），不参与日期/交易日判断。",
    ),
    (
        "app/core/backup/manager.py",
        "① 备份 ID（`full_YYYYmmdd_HHMMSS_<hex>`）是**目录/文件名**，用本地 naive 时间属命名惯例，"
        "改北京时区会让历史备份产物对不上号；② backup_registry/manifest 的 created_at 用 "
        "`datetime.now(UTC)`，与本条无关。两者口径本来就不同，故分开说明，不合并成一句。"
        "备份保留策略按**条数**（最近 3 套）而非时间，因此该差异不影响正确性。",
    ),
)


def rg1_allowlist_reason(relative: str) -> str | None:
    """命中 RG1 时刻白名单时返回理由（``scripts/**`` 表示整棵子树）。"""
    for pattern, reason in RG1_INSTANT_ALLOWLIST:
        if pattern.endswith("/**"):
            if relative.startswith(pattern[:-2]):
                return reason
        elif fnmatch.fnmatchcase(relative, pattern):
            return reason
    return None


@dataclass(frozen=True)
class _ClockAliases:
    """一个模块里 `datetime` 家族的绑定名（`import datetime as dt` / `from datetime import date as _date`）。

    RG1 必须按**调用点**判断，就必须知道 `dt.datetime.now()` / `_date.today()` 这些别名
    指向谁；纯文本匹配会在注释、字符串、同名自有函数（如 `etf_strategy._now()`）上误报。
    """

    datetime_modules: frozenset[str] = frozenset()
    zoneinfo_modules: frozenset[str] = frozenset()
    pandas_modules: frozenset[str] = frozenset()
    date_classes: frozenset[str] = frozenset()
    datetime_classes: frozenset[str] = frozenset()
    timestamp_classes: frozenset[str] = frozenset()
    timezone_names: frozenset[str] = frozenset()
    timedelta_names: frozenset[str] = frozenset()
    zoneinfo_names: frozenset[str] = frozenset()


_CLOCK_ATTR_KIND = {"today": "today", "now": "now", "utcnow": "utcnow"}


def _collect_clock_aliases(tree: ast.AST) -> _ClockAliases:
    buckets: dict[str, set[str]] = {
        "datetime_modules": set(), "zoneinfo_modules": set(), "pandas_modules": set(),
        "date_classes": set(), "datetime_classes": set(), "timestamp_classes": set(),
        "timezone_names": set(), "timedelta_names": set(), "zoneinfo_names": set(),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if alias.name == "datetime":
                    buckets["datetime_modules"].add(bound)
                elif alias.name == "zoneinfo":
                    buckets["zoneinfo_modules"].add(bound)
                elif alias.name == "pandas":
                    buckets["pandas_modules"].add(bound)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module == "datetime":
                    key = {
                        "date": "date_classes",
                        "datetime": "datetime_classes",
                        "timezone": "timezone_names",
                        "timedelta": "timedelta_names",
                    }.get(alias.name)
                elif node.module == "zoneinfo" and alias.name == "ZoneInfo":
                    key = "zoneinfo_names"
                elif node.module == "pandas" and alias.name == "Timestamp":
                    key = "timestamp_classes"
                else:
                    key = None
                if key:
                    buckets[key].add(bound)
    return _ClockAliases(**{name: frozenset(values) for name, values in buckets.items()})


def _dotted_name(node: ast.AST) -> list[str] | None:
    """`a.b.c` → ``["a", "b", "c"]``（不是纯名字/属性链则 None）。"""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return list(reversed(parts))


def _classify_clock_call(node: ast.Call, aliases: _ClockAliases) -> str | None:
    """把调用点归类成 ``today`` / ``now`` / ``utcnow``；不是时钟调用返回 None。"""
    dotted = _dotted_name(node.func)
    if not dotted:
        return None
    head, tail = dotted[0], dotted[1:]
    if head in aliases.datetime_modules:
        if tail == ["date", "today"]:
            return "today"
        if len(tail) == 2 and tail[0] == "datetime":
            return _CLOCK_ATTR_KIND.get(tail[1])
        if len(tail) == 1:
            return _CLOCK_ATTR_KIND.get(tail[0])
        return None
    if head in aliases.datetime_classes:
        return _CLOCK_ATTR_KIND.get(tail[0]) if len(tail) == 1 else None
    if head in aliases.date_classes:
        return "today" if tail == ["today"] else None
    if head in aliases.pandas_modules:
        if tail in (["Timestamp", "now"], ["Timestamp", "utcnow"]):
            return tail[1]
        return None
    if head in aliases.timestamp_classes:
        return tail[0] if len(tail) == 1 and tail[0] in {"now", "utcnow"} else None
    return None


def _first_positional_or_keyword(
    node: ast.Call, position: int, keyword: str
) -> ast.AST | None:
    if len(node.args) > position and not isinstance(node.args[position], ast.Starred):
        return node.args[position]
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return None


def _is_eight_hour_timedelta(node: ast.AST | None, aliases: _ClockAliases) -> bool:
    """`timedelta(hours=8)`。"""
    if not isinstance(node, ast.Call):
        return False
    dotted = _dotted_name(node.func)
    if not dotted or dotted[-1] not in aliases.timedelta_names:
        return False
    hours = _first_positional_or_keyword(node, 1, "hours")
    return isinstance(hours, ast.Constant) and hours.value == 8


def _timezone_literal(node: ast.Call, aliases: _ClockAliases) -> str | None:
    """识别私拼 UTC+8 的调用点：`ZoneInfo("Asia/Shanghai")` / `timezone(timedelta(hours=8))`。"""
    dotted = _dotted_name(node.func)
    if not dotted:
        return None
    is_zoneinfo = (
        (len(dotted) == 1 and dotted[0] in aliases.zoneinfo_names)
        or (len(dotted) == 2 and dotted[0] in aliases.zoneinfo_modules and dotted[1] == "ZoneInfo")
    )
    if is_zoneinfo:
        zone = _first_positional_or_keyword(node, 0, "key")
        if isinstance(zone, ast.Constant) and zone.value in _CN_ZONE_IDS:
            return f'ZoneInfo("{zone.value}")'
        return None
    if (
        len(dotted) == 1
        and dotted[0] in aliases.timezone_names
        and _is_eight_hour_timedelta(_first_positional_or_keyword(node, 0, "offset"), aliases)
    ):
        return "timezone(timedelta(hours=8))"
    return None


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


_DATE_FORMAT_TOKEN = re.compile(r"%[YymjdeFGgUV]|%[DFx]")
_TIME_FORMAT_TOKEN = re.compile(r"%[HIJMSXTLpRrZz]|%[TDR]")


def _format_is_date_only(fmt: str) -> bool:
    """`"%Y-%m-%d"` / `"%Y%m%d"` 是"今天"；`"%Y%m%d_%H%M%S"` 是时间戳。"""
    return bool(_DATE_FORMAT_TOKEN.search(fmt)) and not _TIME_FORMAT_TOKEN.search(fmt)


def _is_date_granularity_use(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """`now()` 的结果是否被当成"某一天的日期"使用。

    看穿 `(now - timedelta(days=30)).strftime("%Y-%m-%d")` 这类算术包装，
    但 `now().isoformat()`（时刻入库）、`now().astimezone()`（补时区）不算日期语义。
    """
    current: ast.AST = node
    for _ in range(5):
        parent = parents.get(id(current))
        if parent is None:
            return False
        if (
            isinstance(parent, ast.Attribute)
            and parent.value is current
            and isinstance(parent.ctx, ast.Load)
        ):
            if parent.attr == "date":
                return True
            if parent.attr in {
                "year", "month", "day", "weekday", "isoweekday", "timetuple", "toordinal",
            }:
                return True
            current = parent
            continue
        if isinstance(parent, ast.Call) and isinstance(current, ast.Attribute) and parent.func is current:
            if current.attr == "astimezone":
                current = parent
                continue
            if current.attr == "replace":
                keywords = {item.arg for item in parent.keywords}
                return bool(keywords & {"year", "month", "day"}) or len(parent.args) >= 3
            if current.attr == "strftime":
                fmt = _first_positional_or_keyword(parent, 0, "format")
                if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
                    return _format_is_date_only(fmt.value)
            return False
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Sub | ast.Add):
            current = parent
            continue
        return False
    return False


def _becomes_aware(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """`datetime.now().astimezone()`：naive 只活一瞬，按机器时区转 aware，不算时区歧义。

    **刻意不再放行 `.replace(tzinfo=...)`**（2026-11-18 审查 F5）：`now().replace(tzinfo=CN_TZ)`
    不是"变成 aware"，而是**把本地墙钟时间直接断言成北京时间**——在东八区外的机器上这是错的，
    而且它看起来比 `cn_now()` 还"规范"，最容易被后人复制。当前仓库无此写法，一旦出现必须报红。
    """
    attribute = parents.get(id(node))
    if not (isinstance(attribute, ast.Attribute) and attribute.value is node):
        return False
    call = parents.get(id(attribute))
    if not (isinstance(call, ast.Call) and call.func is attribute):
        return False
    return attribute.attr == "astimezone"


def _naive_now(node: ast.Call) -> bool:
    """`now()` 没给 tzinfo（或显式 None）→ 跟机器时区走。"""
    tz = _first_positional_or_keyword(node, 0, "tz")
    return tz is None or (isinstance(tz, ast.Constant) and tz.value is None)


def _duplicate_clock_defs(tree: ast.AST, relative: str) -> list[Finding]:
    """别处不许再定义 `cn_today` / `cn_now` 家族（语义单源）。"""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and re.fullmatch(
            r"_?cn_(?:today|now)", node.name
        ):
            findings.append(Finding(
                "RG1", relative, node.lineno,
                "重复定义 cn_today/cn_now 家族函数；请 from app.core.financial_period "
                "import cn_today",
            ))
    return findings


def check_timezone_single_source(root: Path) -> list[Finding]:
    """UTC+8 时区与"北京时间今天"只能有一个实现（`app/core/financial_period.py`）。

    按 **AST 调用点**判断（不看文本），因此注释/字符串里出现 `date.today()` 不误报；
    同时 `dt.datetime.now()` / `_date.today()` 这类别名写法照样抓得到。
    判定顺序：时区字面量 → `today()` → `utcnow()` → `now()` 的日期粒度使用 → naive `now()`。
    """
    findings: list[Finding] = []
    owner = (root / TIMEZONE_OWNER).resolve()
    for path in _iter_python_files(root, "app") + _iter_python_files(root, "scripts"):
        relative = _rel(root, path)
        if path.resolve() == owner:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # 解析不了就不能假装合规
            findings.append(Finding(
                "RG1", relative, exc.lineno or 0, f"无法解析，RG1 无法判断时区单源：{exc.msg}",
            ))
            continue
        aliases = _collect_clock_aliases(tree)
        allow_reason = rg1_allowlist_reason(relative)
        parents = _build_parent_map(tree)
        findings.extend(_duplicate_clock_defs(tree, relative))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            literal = _timezone_literal(node, aliases)
            if literal is not None:
                findings.append(Finding(
                    "RG1", relative, node.lineno,
                    f"重复定义 UTC+8 时区 {literal}；请 from app.core.financial_period import CN_TZ",
                ))
                continue
            kind = _classify_clock_call(node, aliases)
            if kind is None:
                continue
            if kind == "today":
                findings.append(Finding(
                    "RG1", relative, node.lineno,
                    "用系统本地日期冒充'今天'；请改用 cn_today()（北京时间）",
                ))
            elif kind == "utcnow":
                findings.append(Finding(
                    "RG1", relative, node.lineno,
                    "utcnow() 是 naive UTC 且已废弃：日期语义用 cn_today()，时刻语义用 datetime.now(UTC)",
                ))
            elif _is_date_granularity_use(node, parents):
                findings.append(Finding(
                    "RG1", relative, node.lineno,
                    "用某个时区的日期冒充'今天'（.date()/.year/日期格式 strftime）；请改用 cn_today()",
                ))
            elif _naive_now(node) and not _becomes_aware(node, parents) and allow_reason is None:
                findings.append(Finding(
                    "RG1", relative, node.lineno,
                    "naive datetime.now() 跟机器时区走：日期语义用 cn_today()，时刻语义用 "
                    "datetime.now(UTC)；确为日志/展示时刻请登记 RG1_INSTANT_ALLOWLIST",
                ))
    return findings


# --------------------------------------------------------------------------- RG2


def _dict_has_literal_status(node: ast.Dict) -> bool:
    return any(
        isinstance(key, ast.Constant) and key.value == "status" for key in node.keys
    )


def _dict_status_override_indexes(node: ast.Dict) -> list[int]:
    """`**` 解包**之后**仍写入字面量 status 的元素下标。

    覆盖两种写法（同一个假绿机制）：
    * ``{**后一个子步骤, "status": 前一个}`` —— 直接写字面量键（`reports/117` P0-2）；
    * ``{**a, **{"status": 前一个}}`` —— **第二层解包**：解包的还是一个字面量 dict，
      结果一样是"后写覆盖前面解包进来的 status"。

    顺序仍然敏感：``{"status": x, **optional}`` 是"先定状态、再追加字段"，不违规。
    """
    first_unpack: int | None = None
    hits: list[int] = []
    for index, key in enumerate(node.keys):
        if key is None:
            if first_unpack is None:
                first_unpack = index
            elif isinstance(node.values[index], ast.Dict) and _dict_has_literal_status(
                node.values[index]
            ):
                hits.append(index)
        elif isinstance(key, ast.Constant) and key.value == "status" and first_unpack is not None:
            hits.append(index)
    return hits


def _dict_literal_overrides_status(node: ast.Dict) -> bool:
    """兼容入口：该 dict 是否用解包之后的字面量 status 覆盖了被解包的状态。"""
    return bool(_dict_status_override_indexes(node))


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            return current
        current = parents.get(id(current))
    return None


def _receiver_is_fresh_step_skeleton(
    node: ast.Call, parents: dict[int, ast.AST], receiver: ast.Name
) -> bool:
    """`step.update({"status": ...})` 的目标是不是"刚建好、还没状态的步骤骨架"。

    `update.py` 里有 5 处合法写法：`step = self._new_domain_step(...)` 造出该步的骨架，
    跑完再把结果整块写回——这是"填写本步结果"，不是 reports/117 P0-2 那个
    "把两个步骤状态合并、后写覆盖前写"。判据：同函数内**最近的** ``step = ...``
    赋值来自 dict 字面量或名字里带 ``step`` 的工厂调用（骨架）；取不到赋值
    （例如把外部传进来的 dict 就地改状态）就按违规处理——宁可报，不可漏。
    """
    function = _enclosing_function(node, parents)
    if function is None:
        return False
    latest: ast.Assign | ast.AnnAssign | None = None
    for candidate in ast.walk(function):
        if not isinstance(candidate, ast.Assign | ast.AnnAssign):
            continue
        if candidate.lineno >= node.lineno:
            continue
        targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
        if not any(
            isinstance(target, ast.Name) and target.id == receiver.id for target in targets
        ):
            continue
        if latest is None or candidate.lineno > latest.lineno:
            latest = candidate
    if latest is None:
        return False
    value = latest.value
    if isinstance(value, ast.Dict):
        return True
    if isinstance(value, ast.Call):
        dotted = _dotted_name(value.func) or []
        return any("step" in part.lower() for part in dotted)
    return False


def _status_update_shape(
    node: ast.Call, parents: dict[int, ast.AST] | None = None
) -> str | None:
    """`step.update({"status": ...})` / `step.update(status=...)` / `dict(step, status=...)`。"""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "update":
        receiver = func.value
        overrides = any(
            isinstance(argument, ast.Dict) and _dict_has_literal_status(argument)
            for argument in node.args[:1]
        ) or any(keyword.arg == "status" for keyword in node.keywords)
        if not overrides:
            return None
        if (
            parents is not None
            and isinstance(receiver, ast.Name)
            and _receiver_is_fresh_step_skeleton(node, parents, receiver)
        ):
            return None
        shape = 'step.update({"status": ...})' if node.args else "step.update(status=...)"
        return shape
    if isinstance(func, ast.Name) and func.id == "dict":
        for keyword in node.keywords:
            if keyword.arg == "status":
                return "dict(..., status=...)"
    return None


def _iter_step_aggregator_modules(root: Path) -> list[tuple[Path, str]]:
    """`app/core/**` 下"含步骤聚合语义"的模块（不是手写清单，按内容判定）。

    RG2 原来只扫 2 个文件：任何新写的聚合点（或把状态合并挪到第三处）都能绕过。
    现在范围是整棵 `app/core`，只排除 :data:`RG2_EXCLUDED` 里写明理由的实现方；
    "有没有步骤语义"用 ``report_step`` / ``merge_step_status`` / ``steps`` 等 token 判定，
    纯数据模块（例如 `index_dashboard` 的汇总行 status）不会被卷进来。
    """
    modules: list[tuple[Path, str]] = []
    for path in _iter_python_files(root, STATUS_SCAN_ROOT):
        relative = _rel(root, path)
        if relative in RG2_EXCLUDED:
            continue
        source = path.read_text(encoding="utf-8")
        if _RG2_STEP_SEMANTICS.search(source):
            modules.append((path, relative))
    return modules


def check_status_merge_single_exit(root: Path) -> list[Finding]:
    """步骤状态不得用字典解包/`dict.update` 覆盖，必须走 `merge_step_status()`。"""
    findings: list[Finding] = []
    for path, relative in _iter_step_aggregator_modules(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            findings.append(Finding(
                "RG2", relative, exc.lineno or 0, f"无法解析，RG2 无法检查状态合并：{exc.msg}",
            ))
            continue
        parents = _build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and _dict_status_override_indexes(node):
                findings.append(Finding(
                    "RG2", relative, node.lineno,
                    '字典解包后又覆盖 "status"（含 `**{"status": ...}` 第二层解包）；状态必须交给 '
                    "app.core.job_status.merge_step_status()",
                ))
            elif isinstance(node, ast.Call):
                shape = _status_update_shape(node, parents)
                if shape:
                    findings.append(Finding(
                        "RG2", relative, node.lineno,
                        f"用 {shape} 原地覆盖步骤状态；状态必须交给 "
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
        module_name = getattr(domain.rebuild, "__module__", "") or "<unknown>"
        module = sys.modules.get(module_name)
        sql = getattr(module, "SNAPSHOT_UPDATE_SQL", None) if module else None
        if not isinstance(sql, str) or not sql.strip():
            # fail-open 曾经在这里：取不到常量就 `sql_columns = None` 然后静默跳过，
            # 于是"把 SNAPSHOT_UPDATE_SQL 改名/删掉"就能让 RG4 永远绿。
            findings.append(Finding(
                "RG4", SCHEMA_FILE, 0,
                f"无法解析派生域 {domain.name} 的重建 SQL（{module_name}.SNAPSHOT_UPDATE_SQL "
                "常量缺失/改名/为空）→ 门禁 fail-open：登记列与重建 SQL 是否一致未被核对",
            ))
            continue
        findings.extend(
            compare_domain_columns(
                domain.name,
                set(domain.columns),
                snapshot_columns,
                parse_sql_set_columns(sql),
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

    # RG6–RG9 仓库完整性（`reports/140`）：编号唯一 / supersede 闭环 /
    # 真相源不引用作废结论 / 测试资产必须入库。实现放在独立模块，
    # 避免本文件继续膨胀；挂在这里保证"一条命令跑全部门禁"，
    # 否则两个脚本必然有一个被人忘掉（本项目没有 CI，见 reports/140）。
    report.checked.extend(_run_repo_integrity(root, report))
    return report


def _run_repo_integrity(root: Path, report: Report) -> list[str]:
    try:
        from scripts import repo_integrity_guards as rig
    except ImportError:  # 允许以 `python scripts/xxx.py` 直接执行（无包上下文）
        import repo_integrity_guards as rig  # type: ignore[no-redef]
    integrity = rig.run_all(root)
    for finding in integrity.findings:
        report.findings.append(Finding(finding.rule, finding.path, 0, finding.message))
    if integrity.notes:
        report.baselined = len(integrity.notes)
    return integrity.checked


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
            suffix = f"，基线内 {report.baselined} 条未计红" if report.baselined else ""
            print(f"反返工护栏通过（{len(report.checked)} 组规则{suffix}）")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
