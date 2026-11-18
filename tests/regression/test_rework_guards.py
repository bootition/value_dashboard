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

# ─── 1. 仓库本体必须零违规（真正的门禁）──────────────────────────────────


def test_repository_passes_all_rework_guards() -> None:
    report = run_all(PROJECT_ROOT)
    assert report.ok, "\n".join(finding.render() for finding in report.findings)
    # RG1–RG5（reports/133）+ RG6–RG9 仓库完整性（reports/140）
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
    }
    assert set(report.checked) == expected, (
        "门禁集合变了：新增/删减必须同步更新本断言与 AGENTS.md 的规则索引"
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
    assert len(findings) == 3, [f.render() for f in findings]
    assert any("重复定义 UTC+8 时区" in m for m in rules)
    assert any("重复定义 cn_today" in m for m in rules)
    assert any("用 UTC 日期冒充" in m for m in rules)


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
    ts_sets = {"PCT_FIELDS": {"roe", "display_only"}, "PERCENT_FIELDS": {"a"},
               "RATIO_FIELDS": {"current_ratio"}}
    # 前端多出纯展示字段是允许的（TS 是超集）；后端字段必须被镜像。
    assert compare_unit_sets(python_sets, ts_sets) == []


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
