"""反返工护栏门禁（`reports/133` RG1–RG4）。

本文件把"复盘结论"变成会失败的测试，而不是又一份只能靠人记住的文档。

两层：
1. **对仓库本体**：`scripts/check_rework_guards.py` 的四组规则必须零违规
   （RG1 时区单源、RG2 状态合并单出口、RG3 单位注册表跨语言一致、
   RG4 派生域注册表自洽）。
2. **对检查器自身**：每条规则都必须能用最小样例"抓得到"。只写"应该抓"的
   规则而从不验证它会抓，等于门禁空转——本项目已经踩过（`reports/125`
   的启动清扫"看起来有、实际永不生效"）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_rework_guards import (  # noqa: E402
    check_derived_domain_registry,
    check_rules_single_source,
    check_status_merge_single_exit,
    check_timezone_single_source,
    check_unit_registry_consistency,
    compare_domain_columns,
    compare_unit_sets,
    parse_rule_blocks,
    parse_snapshot_columns,
    parse_sql_set_columns,
    run_all,
)
from scripts.repo_integrity_guards import (  # noqa: E402
    REPLACEMENT_CHAR,
    check_no_replacement_char,
)

# ─── 1. 仓库本体必须零违规（真正的门禁）──────────────────────────────────


def test_repository_passes_all_rework_guards() -> None:
    report = run_all(PROJECT_ROOT)
    assert report.ok, "\n".join(finding.render() for finding in report.findings)
    # RG1–RG5（reports/133）+ RG6–RG10 仓库完整性（reports/140 / reports/144）
    expected = {
        "RG1 时区单源",
        "RG2 状态合并单出口",
        "RG3 单位注册表跨语言一致",
        "RG4 派生域注册表自洽",
        "RG5 规则单一来源",
        "RG6 报告编号唯一 + front-matter 合法",
        "RG7 supersede 双向闭合",
        "RG8 真相源不引用作废结论",
        "RG9 测试资产必须入库",
        "RG10 源码/文档不含 U+FFFD 替换字符",
    }
    assert set(report.checked) == expected, (
        "门禁集合变了：新增/删减必须同步更新本断言与 AGENTS.md 的规则索引"
    )


_RG_TOKEN = re.compile(r"RG(\d{1,2})")
_RG_RANGE = re.compile(r"RG(\d{1,2})\s*(?:–|-|—|~|至)\s*RG(\d{1,2})")


def agents_declared_rule_numbers(agents_text: str) -> set[int]:
    """从 AGENTS.md 文本里解析出它声称存在的门禁编号集合。

    两种写法都要认：单点 `RG3`、区间 `RG1–RG10`（AGENTS.md 用区间报总条数，
    只抓单点会把 RG6/RG7/RG8 漏掉，因为它们在正文里只以区间形式出现）。
    """
    numbers = {int(match) for match in _RG_TOKEN.findall(agents_text)}
    for low, high in _RG_RANGE.findall(agents_text):
        start, end = int(low), int(high)
        if start <= end:
            numbers.update(range(start, end + 1))
    return numbers


def test_guard_inventory_matches_agents_md() -> None:
    """清单漂移守卫：`AGENTS.md` 声称的 RG 编号集合 == `run_all()` 实跑的集合。

    2026-11-18 的真实漂移：AGENTS.md 与 `.githooks/pre-commit` 已经写到 RG10，
    而本文件的期望集合还停在 RG9（`test_repository_passes_all_rework_guards` 红），
    钩子提示文案也还印着 `RG1–RG9`。规则数目是"人写的第三份清单"，只要有第三份
    就会漂。这条用例把它钉成结构断言：
    * 新增了规则却忘了写进 AGENTS.md（或写漏了区间）→ 红；
    * AGENTS.md 写了 RG11 但 `run_all()` 没挂上（或被删）→ 红；
    * 同一编号被挂了两遍 → 红。
    """
    report = run_all(PROJECT_ROOT)
    implemented: list[int] = []
    for name in report.checked:
        match = _RG_TOKEN.match(name)
        assert match, f"门禁名字必须以 RG<n> 开头，否则无法对拍：{name!r}"
        implemented.append(int(match.group(1)))
    assert len(implemented) == len(set(implemented)), f"同一 RG 编号被挂了多遍：{report.checked}"

    declared = agents_declared_rule_numbers(
        (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    )
    implemented_set = set(implemented)
    assert declared == implemented_set, (
        "AGENTS.md 与实跑门禁清单漂移："
        f"AGENTS.md 多写了 {sorted(declared - implemented_set)}、"
        f"实跑多出 {sorted(implemented_set - declared)}；"
        "两边必须同步（改 AGENTS.md 注意 300 行上限）"
    )
    assert implemented_set == set(range(1, max(implemented_set) + 1)), (
        f"RG 编号必须从 1 连续编号，现在是 {sorted(implemented_set)}"
    )


def test_every_roce_snapshot_column_is_registered() -> None:
    """DDL 里每新增一个 ROCE 列，都必须同步登记到派生域（否则不会被保全）。"""
    from app.core.indicators.calculator import derived_snapshot_domains
    from app.core.storage import schema

    columns = parse_snapshot_columns(
        (PROJECT_ROOT / "app/core/storage/schema.py").read_text(encoding="utf-8")
    )
    roce_columns = {column for column in columns if column.startswith("roce_")}
    registered = {
        column
        for domain in derived_snapshot_domains()
        for column in domain.columns
    }
    assert roce_columns, "解析不到 roce_* 列，schema DDL 可能改名"
    assert roce_columns <= registered, sorted(roce_columns - registered)
    assert schema is not None  # 保证 schema 模块可导入（DDL 单一来源）


# ─── 2. RG1 时区单源：检查器必须抓得到重复实现 ───────────────────────────


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_rg1_flags_duplicate_timezone_and_utc_today(tmp_path: Path) -> None:
    _write(
        tmp_path, "app/core/dup.py",
        "from datetime import UTC, datetime, timedelta, timezone\n"
        "_CN_TZ = timezone(timedelta(hours=8))\n"
        "def _cn_today():\n    return datetime.now(_CN_TZ).date()\n"
        "def age(x):\n    return (datetime.now(UTC).date() - x).days\n",
    )
    findings = check_timezone_single_source(tmp_path)
    rules = sorted(f.message.split("；")[0] for f in findings)
    # 1) 私拼 UTC+8  2) 重复定义 cn_today  3) aware now 取 .date()（第 3 行实现里那个）
    # 4) datetime.now(UTC).date() 冒充今天
    assert len(findings) == 4, [f.render() for f in findings]
    assert any("重复定义 UTC+8 时区" in m for m in rules)
    assert any("重复定义 cn_today" in m for m in rules)
    assert any("用某个时区的日期冒充'今天'" in m for m in rules)


def test_rg1_allows_importing_the_single_source(tmp_path: Path) -> None:
    _write(
        tmp_path, "app/core/financial_period.py",
        "from datetime import datetime, timedelta, timezone\n"
        "CN_TZ = timezone(timedelta(hours=8))\n"
        "def cn_today():\n    return datetime.now(CN_TZ).date()\n",
    )
    _write(
        tmp_path, "app/core/ok.py",
        "from app.core.financial_period import CN_TZ, cn_today\n"
        "def age(x):\n    return (cn_today() - x).days\n",
    )
    assert check_timezone_single_source(tmp_path) == []


def test_rg1_catches_the_six_injected_spellings(tmp_path: Path) -> None:
    """曾经 6 种写法注入后仍全绿（拦截率 0%）：现在必须逐个抓到。"""
    _write(
        tmp_path, "app/core/injected.py",
        "import datetime as dt\n"
        "import pandas as pd\n"
        "from datetime import date, datetime, timedelta, timezone\n"
        "from datetime import date as _date\n"
        "from zoneinfo import ZoneInfo\n"
        "a = date.today()\n"
        "b = dt.datetime.now()\n"
        "c = _date.today()\n"
        "d = pd.Timestamp.now()\n"
        "e = dt.datetime.now(dt.timezone.utc).date()\n"
        "f = ZoneInfo('Asia/Shanghai')\n"
        "g = timezone(timedelta(hours=8))\n"
        "h = datetime.utcnow()\n"
        "i = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')\n"
        "j = datetime.now().year\n"
        "k = datetime.now().isoformat()\n"
        "l = datetime.now().astimezone().isoformat()\n",
    )
    findings = check_timezone_single_source(tmp_path)
    lines = {finding.line for finding in findings}
    expected = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
    assert expected <= lines, f"漏报：{sorted(expected - lines)}"
    # 第 17 行 `datetime.now().astimezone()` 立刻补了时区，是合法时刻，不该报
    assert 17 not in lines, [f.render() for f in findings if f.line == 17]


def test_rg1_ignores_comments_strings_and_docstrings(tmp_path: Path) -> None:
    """旧正则的病根：注释里写 `date.today()` 也会红。AST 版本必须一条都不报。"""
    _write(
        tmp_path, "app/core/only_comments.py",
        '"""历史实现的坑：`date.today()` 与 `datetime.now(UTC).date()` 相差一天。\n\n'
        "也别再写 `datetime.datetime.now()` / `pd.Timestamp.now()` / `utcnow()`。\n"
        '时区字面量 `ZoneInfo("Asia/Shanghai")` / `timezone(timedelta(hours=8))` 同样禁止。\n'
        '"""\n'
        "from app.core.financial_period import cn_today\n"
        "# date.today()  datetime.now()  utcnow()  ZoneInfo('Asia/Shanghai')\n"
        "# timezone(timedelta(hours=8))\n"
        'NOTE = "date.today() / datetime.now(UTC).date()"\n'
        "def age(x):\n    return (cn_today() - x).days\n",
    )
    assert check_timezone_single_source(tmp_path) == []


def test_rg1_zoneinfo_allowed_only_in_the_owner_file(tmp_path: Path) -> None:
    _write(
        tmp_path, "app/core/financial_period.py",
        "from datetime import date, datetime, timedelta, timezone\n"
        "from zoneinfo import ZoneInfo\n"
        "CN_TZ = ZoneInfo('Asia/Shanghai')\n"
        "CN_TZ_2 = timezone(timedelta(hours=8))\n"
        "def cn_today() -> date:\n    return datetime.now(CN_TZ).date()\n",
    )
    _write(
        tmp_path, "app/core/not_owner.py",
        "from zoneinfo import ZoneInfo\nTZ = ZoneInfo('Asia/Shanghai')\n",
    )
    findings = check_timezone_single_source(tmp_path)
    assert [finding.path for finding in findings] == ["app/core/not_owner.py"], [
        f.render() for f in findings
    ]


def test_rg1_allowlist_only_covers_instant_not_today(tmp_path: Path) -> None:
    """白名单只豁免 naive 时刻；`today()` 在被豁免的路径里照样报。"""
    _write(
        tmp_path, "scripts/one_off.py",
        "from datetime import date, datetime\n"
        "STAMP = datetime.now().strftime('%Y%m%d_%H%M%S')\n"
        "TODAY = date.today()\n"
        "DAY = datetime.now().strftime('%Y-%m-%d')\n",
    )
    findings = check_timezone_single_source(tmp_path)
    assert sorted(finding.line for finding in findings) == [3, 4], [
        f.render() for f in findings
    ]


# ─── 3. RG2 状态合并单出口：顺序敏感，不能误伤"先定状态再追加字段" ────────


def _dict_literal(source: str) -> ast.Dict:
    node = ast.parse(source).body[0].value  # type: ignore[attr-defined]
    assert isinstance(node, ast.Dict)
    return node


def test_rg2_flags_status_overriding_unpack() -> None:
    from scripts.check_rework_guards import _dict_literal_overrides_status

    assert _dict_literal_overrides_status(
        _dict_literal('{"full_recompute": later, **sub_step, "status": "success"}')
    )


def test_rg2_allows_status_before_unpack() -> None:
    from scripts.check_rework_guards import _dict_literal_overrides_status

    # update.py 里的合法写法：先定 status，再用 ** 追加可选字段
    assert not _dict_literal_overrides_status(
        _dict_literal('{"status": step_status, **({"x": 1} if cond else {})}')
    )
    assert not _dict_literal_overrides_status(_dict_literal('{"status": "ok", "n": 1}'))
    assert not _dict_literal_overrides_status(_dict_literal("{**base}"))


def test_rg2_ignores_the_single_exit_implementation() -> None:
    """`job_status.merge_step_status()` 自身就是单出口，不应被自己拦下。"""
    assert check_status_merge_single_exit(PROJECT_ROOT) == []


def test_rg2_flags_status_inside_dict_update(tmp_path: Path) -> None:
    """`step.update({"status": ...})`（reports/117 P0-2 的另一种写法）。"""
    _write(
        tmp_path, "app/core/aggregate/steps_demo.py",
        "def merge(step, other):\n"
        "    step.update({'status': 'success'})\n"
        "    return step\n",
    )
    findings = check_status_merge_single_exit(tmp_path)
    assert findings, "app/core 子目录里的聚合点没被扫到"
    assert any("step.update" in finding.message for finding in findings), [
        f.message for f in findings
    ]


def test_rg2_flags_second_layer_unpack_of_status(tmp_path: Path) -> None:
    """`{**a, **{"status": ...}}`：第二层解包，机制与 P0-2 相同。"""
    _write(
        tmp_path, "app/core/steps_demo.py",
        "def merge(step, other):\n"
        "    return {**step, **{'status': 'success'}}\n",
    )
    findings = check_status_merge_single_exit(tmp_path)
    assert findings
    assert any("字典解包后又覆盖" in finding.message for finding in findings)


def test_rg2_allows_result_write_into_fresh_skeleton(tmp_path: Path) -> None:
    """`update.py` 的 5 处合法写法：先造本步骨架，再把结果整块写回。"""
    _write(
        tmp_path, "app/core/steps_demo.py",
        "def _new_domain_step():\n    return {'status': 'skipped'}\n"
        "def run():\n"
        "    step = _new_domain_step()\n"
        "    step.update({'status': 'success'})\n"
        "    return step\n",
    )
    assert check_status_merge_single_exit(tmp_path) == []


def test_rg2_ignores_modules_without_step_semantics(tmp_path: Path) -> None:
    """纯数据模块的汇总行 status 不是步骤聚合，不该被卷进来。"""
    _write(
        tmp_path, "app/core/index_dashboard.py",
        "def summarize(item):\n    return {**item, 'status': 'error'}\n",
    )
    assert check_status_merge_single_exit(tmp_path) == []


# ─── 4. RG3 单位注册表：口径漂移 = 100 倍误差，必须抓得到 ─────────────────


def test_rg3_flags_missing_and_misclassified_units() -> None:
    python_sets = {
        "PCT_DECIMAL_FIELDS": {"roe"},
        "PERCENT_STORED_FIELDS": {"ttm_dividend_yield"},
        "RATIO_FIELDS": {"current_ratio", "cash_content_of_revenue"},
    }
    ts_sets = {
        # cash_content_of_revenue 在前端被错登记成"小数比例"（真实存量缺陷）
        "PCT_FIELDS": {"roe", "cash_content_of_revenue"},
        "PERCENT_FIELDS": {"ttm_dividend_yield"},
        "RATIO_FIELDS": set(),
    }
    findings = compare_unit_sets(python_sets, ts_sets)
    messages = " | ".join(f.message for f in findings)
    assert "current_ratio" in messages and "缺失" in messages
    assert "cash_content_of_revenue" in messages and "口径漂移" in messages


def test_rg3_passes_when_frontend_mirrors_backend() -> None:
    python_sets = {"PCT_DECIMAL_FIELDS": {"roe"}, "PERCENT_STORED_FIELDS": {"a"},
                   "RATIO_FIELDS": {"current_ratio"}}
    ts_sets = {"PCT_FIELDS": {"roe"}, "PERCENT_FIELDS": {"a"},
               "RATIO_FIELDS": {"current_ratio"}}
    # 两侧逐键相等 = 零违规（前端不再是"允许的超集"，见下面的死键用例）。
    assert compare_unit_sets(python_sets, ts_sets) == []


def _rg3_minimal_backend() -> dict[str, set[str]]:
    return {
        "PCT_DECIMAL_FIELDS": {"roe"},
        "PERCENT_STORED_FIELDS": {"ttm_dividend_yield"},
        "RATIO_FIELDS": {"current_ratio"},
    }


def test_rg3_flags_frontend_only_dead_key() -> None:
    """双向化核心：前端登记了后端不存在的键 = 死键，必须红。

    事故原型（2026-11-18）：`PERCENT_FIELDS` 里挂着裸名 `dividend_financing_ratio`，
    后端只有 `dividend_financing_ratio_pct`。前端于是把后端不存在的字段当百分数
    处理，单向检查（只查"后端有、前端缺"）永远看不见它。
    """
    ts_sets = {
        "PCT_FIELDS": {"roe"},
        "PERCENT_FIELDS": {"ttm_dividend_yield", "dividend_financing_ratio"},
        "RATIO_FIELDS": {"current_ratio"},
    }
    findings = compare_unit_sets(_rg3_minimal_backend(), ts_sets)
    assert len(findings) == 1, [f.render() for f in findings]
    assert findings[0].rule == "RG3"
    assert "dividend_financing_ratio" in findings[0].message
    assert "死键" in findings[0].message
    assert findings[0].path == "frontend/src/utils/screening-format.ts"


def test_rg3_flags_frontend_duplicate_class() -> None:
    """前端把同一字段登记进两个口径 = 后者才是真口径，判定顺序不可预期。"""
    ts_sets = {
        "PCT_FIELDS": {"roe", "turnover_rate"},
        "PERCENT_FIELDS": {"ttm_dividend_yield", "turnover_rate"},
        "RATIO_FIELDS": {"current_ratio"},
    }
    python_sets = _rg3_minimal_backend()
    python_sets["PERCENT_STORED_FIELDS"] = {"ttm_dividend_yield", "turnover_rate"}
    messages = " | ".join(f.message for f in compare_unit_sets(python_sets, ts_sets))
    assert "turnover_rate" in messages and "同时被登记" in messages


def test_rg3_flags_cross_class_drift_for_unpaired_sets() -> None:
    """前端把「价格/每股」字段登记成百分数 = 跨类漂移，不能误报成"后端没这个字段"。"""
    python_sets = dict(_rg3_minimal_backend(), PER_SHARE_FIELDS={"bps"})
    ts_sets = {
        "PCT_FIELDS": {"roe"},
        "PERCENT_FIELDS": {"ttm_dividend_yield", "bps"},
        "RATIO_FIELDS": {"current_ratio"},
    }
    messages = " | ".join(f.message for f in compare_unit_sets(python_sets, ts_sets))
    assert "bps" in messages and "跨类漂移" in messages and "PER_SHARE_FIELDS" in messages
    assert "死键" not in messages


def test_rg3_whitelist_allows_live_frontend_only_alias() -> None:
    """白名单能豁免"必须只在前端存在"的历史别名，且理由会写进提示里。"""
    ts_sets = {
        "PCT_FIELDS": {"roe"},
        "PERCENT_FIELDS": {"ttm_dividend_yield", "legacy_alias"},
        "RATIO_FIELDS": {"current_ratio"},
    }
    allowed = {"legacy_alias": "历史发布型 DSL 指标名，不是快照列"}
    assert compare_unit_sets(_rg3_minimal_backend(), ts_sets, allowed) == []


def test_rg3_flags_expired_whitelist_entry() -> None:
    """白名单过期检测：两侧都不存在的键必须报，白名单不许越攒越烂。"""
    ts_sets = {
        "PCT_FIELDS": {"roe"},
        "PERCENT_FIELDS": {"ttm_dividend_yield"},
        "RATIO_FIELDS": {"current_ratio"},
    }
    allowed = {"gone_alias": "上个季度删掉的历史别名"}
    findings = compare_unit_sets(_rg3_minimal_backend(), ts_sets, allowed)
    assert len(findings) == 1, [f.render() for f in findings]
    assert "白名单过期" in findings[0].message
    assert "gone_alias" in findings[0].message
    # 指回白名单文件本身（不是指向前端源码）
    assert findings[0].path == "scripts/check_rework_guards.py"


def test_rg3_flags_whitelist_entry_that_backend_already_registers() -> None:
    """后端已经补上了同名列 → 白名单条目失效（否则它会永久掩盖新的漂移）。"""
    ts_sets = {
        "PCT_FIELDS": {"roe"},
        "PERCENT_FIELDS": {"ttm_dividend_yield"},
        "RATIO_FIELDS": {"current_ratio"},
    }
    allowed = {"current_ratio": "曾几何时后端没有这个名字"}
    findings = compare_unit_sets(_rg3_minimal_backend(), ts_sets, allowed)
    assert len(findings) == 1, [f.render() for f in findings]
    assert "白名单过期" in findings[0].message and "已无必要" in findings[0].message


def test_rg3_whitelist_is_empty_and_documented() -> None:
    """白名单现在必须是空的：唯一的历史别名（裸名 dividend_financing_ratio）不该静态登记。

    它只是发布型 DSL 指标名（`app/web/api/screening.py` 的 `_DSL_INDICATOR_UNITS`），
    单位由运行时元数据下发；后端唯一权威列是 `dividend_financing_ratio_pct`。
    任何人把它塞回前端集合或塞进白名单，都必须先改这条用例。
    """
    from scripts.check_rework_guards import FRONTEND_ONLY_ALLOWED

    assert FRONTEND_ONLY_ALLOWED == {}, (
        "白名单加了条目就必须同时解释：哪一侧缺、为什么必须缺，并更新本用例"
    )


def test_rg3_repository_registries_agree() -> None:
    assert check_unit_registry_consistency(PROJECT_ROOT) == []


# ─── 5. RG4 派生域注册表：登记列必须与重建 SQL 完全一致 ──────────────────


def test_rg4_flags_unknown_and_drifted_columns() -> None:
    findings = compare_domain_columns(
        "demo",
        {"a", "b"},
        snapshot_columns={"a", "b", "c"},
        sql_columns={"a", "c"},
    )
    messages = " | ".join(f.message for f in findings)
    assert "登记了但重建 SQL 没写：b" in messages
    assert "重建 SQL 写了但没登记" in messages and "c" in messages
    assert compare_domain_columns("demo", {"a"}, {"a"}, {"a"}) == []


def test_rg4_flags_column_missing_from_ddl() -> None:
    findings = compare_domain_columns("demo", {"ghost"}, {"a"}, {"ghost"})
    assert any("不在 indicator_snapshot DDL 里" in f.message for f in findings)


def test_rg4_parsers_read_real_sql_and_ddl() -> None:
    sql = """
        UPDATE indicator_snapshot AS s
        SET roce_latest = agg.latest,
            roce_eligible = agg.eligible
        FROM (SELECT 1) agg
        WHERE s.stock_code = agg.stock_code
    """
    assert parse_sql_set_columns(sql) == {"roce_latest", "roce_eligible"}
    snapshot_columns = parse_snapshot_columns(
        (PROJECT_ROOT / "app/core/storage/schema.py").read_text(encoding="utf-8")
    )
    assert {"stock_code", "report_date", "roce_5y_median"} <= snapshot_columns


def test_rg4_repository_registry_is_consistent() -> None:
    assert check_derived_domain_registry(PROJECT_ROOT) == []


def test_rg4_flags_missing_snapshot_sql_instead_of_failing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到 `SNAPSHOT_UPDATE_SQL` 时必须报，不许静默跳过（fail-open）。"""
    import types

    from app.core.indicators import calculator as calculator_module

    module_name = "app.core.indicators.demo_domain"
    monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))

    def rebuild(duck: object) -> None:  # pragma: no cover - 只被静态检查引用
        return None

    rebuild.__module__ = module_name
    fake_domain = types.SimpleNamespace(
        name="demo", columns=("stock_code",), rebuild=rebuild
    )
    monkeypatch.setattr(
        calculator_module, "derived_snapshot_domains", lambda: [fake_domain]
    )
    findings = check_derived_domain_registry(PROJECT_ROOT)
    assert any("fail-open" in finding.message for finding in findings), [
        f.render() for f in findings
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))


# ─── 6. RG5 规则单一来源：规则散落 = 规则不存在（reports/138）──────────────


def test_rg5_repository_rules_are_single_sourced() -> None:
    assert check_rules_single_source(PROJECT_ROOT) == []


def test_rg5_requires_agents_md(tmp_path: Path) -> None:
    findings = check_rules_single_source(tmp_path)
    assert findings and "缺少 AGENTS.md" in findings[0].message


def test_rg5_flags_missing_evidence_in_rule_block(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "# 规则\n\n### W1 做事\n- 规则：好好做\n")
    findings = check_rules_single_source(tmp_path)
    assert any("没有引用证据" in f.message for f in findings), [f.message for f in findings]


def test_rg5_flags_duplicate_rule_id(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "AGENTS.md",
        "### W1 甲\n- 规则：x\n- 为什么：reports/1\n\n"
        "### W1 乙\n- 规则：y\n- 为什么：reports/2\n",
    )
    findings = check_rules_single_source(tmp_path)
    assert any("重复定义" in f.message for f in findings)


def test_rg5_flags_new_lessons_document(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "### W1 甲\n- 规则：x\n- 为什么：reports/1\n")
    _write(tmp_path, "docs/runbooks/agent-workflow-lessons.md", "# 教训\n")
    findings = check_rules_single_source(tmp_path)
    assert any("lesson/规则 命名的文档" in f.message for f in findings), [
        f.message for f in findings
    ]


def test_rg5_flags_undefined_rule_reference(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "### W1 甲\n- 规则：x\n- 为什么：reports/1\n")
    _write(tmp_path, "docs/STATUS.md", "按 W9 执行\n")
    findings = check_rules_single_source(tmp_path)
    assert any("未定义的规则编号" in f.message and "W9" in f.message for f in findings)


def test_rg5_flags_oversized_agents_file(tmp_path: Path) -> None:
    body = "### W1 甲\n- 规则：x\n- 为什么：reports/1\n" + ("\n" * 400)
    _write(tmp_path, "AGENTS.md", body)
    findings = check_rules_single_source(tmp_path)
    assert any("超过上限" in f.message for f in findings)


def test_rg5_parser_reads_real_agents_file() -> None:
    blocks = parse_rule_blocks((PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    ids = {rule_id for rule_id, _, _ in blocks}
    assert {"R1", "R7"} <= ids, "反返工规则编号应保持 R1–R7（历史报告按编号引用）"
    assert {"W1", "W7"} <= ids, "工作流规则 W1–W7 必须定义在 AGENTS.md"


# ─── RG10：源码/文档不得含 U+FFFD（reports/144 §4）───────────────────────


def test_rg10_flags_replacement_char_in_source(tmp_path: Path) -> None:
    """注入一个 U+FFFD 必须被抓到，且行号要准（否则没法定位）。

    这是 RG10 的**反向用例**：没有它，这条门禁就是"永远绿"的装饰。
    """
    bad = tmp_path / "app" / "core"
    bad.mkdir(parents=True)
    (bad / "oops.py").write_text(
        f"x = 1\n# ─── 黑名单操作 {REPLACEMENT_CHAR} ───\ny = 2\n", encoding="utf-8"
    )

    findings = check_no_replacement_char(tmp_path)

    assert len(findings) == 1, [f.render() for f in findings]
    assert findings[0].rule == "RG10"
    assert findings[0].path == "app/core/oops.py"
    assert "第 2 行" in findings[0].message


def test_rg10_ignores_escaped_form_and_clean_files(tmp_path: Path) -> None:
    """`chr(0xFFFD)` / 文字描述「U+FFFD」是**正确写法**，不能误报。

    零误报是这条规则能常驻的前提：有人为了修复 RG10 会把字面字符改成转义写法，
    如果转义写法也被报红，人就只能去写 `# noqa` 了。
    """
    app = tmp_path / "app"
    app.mkdir()
    (app / "ok.py").write_text(
        "C = chr(0xFFFD)\n# 用文字描述 U+FFFD，不放字面替换字符\n", encoding="utf-8"
    )
    (app / "doc.md").write_text("报告里写 U+FFFD 三个字，不是字符本身\n", encoding="utf-8")

    assert check_no_replacement_char(tmp_path) == []


def test_rg10_repository_itself_is_clean() -> None:
    """本仓库不得含 U+FFFD —— 含门禁脚本自己。

    历史教训：这条规则刚上线时，**门禁脚本自己**写字面替换字符而自我引爆。
    """
    findings = check_no_replacement_char(PROJECT_ROOT)
    assert findings == [], [f.render() for f in findings]
