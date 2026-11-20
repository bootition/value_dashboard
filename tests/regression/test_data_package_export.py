"""数据包导出器不变量回归（2026-09-19 建立；2026-11-14 扩展）。

守住几条底线（都是真实踩过或极易踩的坑）：
1. **绝不导出个人数据**（自选/筛选规则/ETF 流水/预算）—— 由**白名单**保证，导出前还再断言一次；
2. **来源标注必须齐全**（每张表登记来源性质）；
3. **load.sql 必须真的能生成**（首版曾因「用 dict 键做 endswith('.parquet')」而全空）；
4. **文档里的每个数字都来自实际导出**（首版把 51 列 / 公告台账 / 年度频率写成常量，
   重导出从不纠正 —— reports/119 §3 的 5 处失真全是这么来的）；
5. **`--verify` 能抓出「包落后于库」**（首版包落后 57 天只能人工发现）。

测试全部使用临时 DuckDB + 临时 SQLite（`database_paths` fixture），不碰正式库。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

_REPO = Path(__file__).resolve().parents[2]
_EXPORTER = _REPO / "scripts" / "export_data_package.py"

# 只导出这几张表就能覆盖全部生成逻辑：DuckDB 表 + SQLite 表 + 混合来源 + 覆盖率 + 滞后。
_MINIMAL_TABLES = ("price_daily_raw", "income_statement", "announcement_registry", "trading_dates")


def _load_exporter():
    spec = importlib.util.spec_from_file_location("vd_exporter", _EXPORTER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先登记进 sys.modules：脚本用了 `from __future__ import annotations`，
    # 而 dataclasses 在解析字符串注解时要回查 sys.modules[cls.__module__]。
    sys.modules["vd_exporter"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exporter_module():
    """导出脚本本体（按路径加载，避免 scripts/ 不是包的问题）。"""
    return _load_exporter()


# --------------------------------------------------------------------------- #
# 白名单与来源标注（纯静态检查）
# --------------------------------------------------------------------------- #
def test_all_exported_tables_are_whitelisted() -> None:
    """导出面必须是**显式白名单**：任何未登记的表都不会被打包。"""
    exporter = _load_exporter()
    listed = {spec.table for spec in exporter.all_specs(with_lineage=True)}
    assert listed, "白名单不能为空"
    # 表名必须全部是普通标识符（防止把 SQL 片段写进白名单）
    for name in listed:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", name), f"非法表名 {name}"


def test_personal_data_is_never_exported() -> None:
    """个人数据（个性化/用户输入）绝不能进包 —— 对应 AR9 约束。"""
    exporter = _load_exporter()
    listed = {spec.table for spec in exporter.all_specs(with_lineage=True)}
    leaked = listed & set(exporter.EXCLUDED_PERSONAL_TABLES)
    assert not leaked, f"个人数据泄漏进导出白名单: {sorted(leaked)}"


def test_export_guard_rejects_personal_table() -> None:
    """白名单写错也要被拦住：导出前的兜底断言必须真的抛错。"""
    exporter = _load_exporter()
    bad = [exporter.TableSpec("B_supplements", "watchlist", "watchlist", "自采", "自选股")]
    with pytest.raises(exporter.PersonalDataLeakError):
        exporter.assert_no_personal_tables(bad)


def test_source_classes_cover_all_tables() -> None:
    """每张导出表都必须标来源性质，且取值必须在登记的类别内。"""
    exporter = _load_exporter()
    for spec in exporter.all_specs(with_lineage=True):
        assert spec.source_class in exporter.SOURCE_CLASSES, (
            f"{spec.table} 来源类别非法: {spec.source_class}"
        )
        # 具体期望（TableSpec 的 docstring：note 是给人看的"这张表是什么"）：
        # 非空 str、无首尾空白、且不能直接把输出表名抄一遍。
        assert isinstance(spec.note, str), f"{spec.table} 的说明必须是 str"
        assert spec.note.strip() == spec.note and spec.note, (
            f"{spec.table} 缺少说明或带首尾空白: {spec.note!r}"
        )
        assert spec.note != spec.output, f"{spec.table} 的说明不得直接抄表名"


def test_history_archived_tables_are_marked() -> None:
    """含历史段的表必须标为「历史归档」或「混合」，便于使用者识别数据截止期。"""
    exporter = _load_exporter()
    by_table = {spec.output: spec.source_class for spec in exporter.all_specs()}
    for table in ("disclosure_metrics", "risk_factors"):
        assert by_table.get(table) == "历史归档", f"{table} 应标「历史归档」"
    # financial_report_dates 自 2026-09-20 起含本项目季度续写（实测到 2026-06-30），
    # 已越过归档截止期 2025-03-31，因此标「混合」而不是误报“归档表越界”。
    for table in ("balance_sheet", "income_statement", "cash_flow",
                  "company_employee_history", "financial_report_dates"):
        assert by_table.get(table) == "混合", f"{table} 应标「混合」"


def test_package_layout_matches_the_ab_split() -> None:
    """包按两层组织：A=基础财务与自算指标；B=本项目独有数据。"""
    exporter = _load_exporter()
    layers = {spec.layer for spec in exporter.all_specs()}
    assert layers == {"A_updates", "B_supplements"}
    a_tables = {spec.table for spec in exporter.A_TABLES}
    b_tables = {spec.table for spec in exporter.B_TABLES}
    # 本项目独有的数据必须在 B 类
    for table in ("price_daily_raw", "price_daily_qfq", "xdxr", "dividends",
                  "share_capital_history", "treasury_yield_curve", "etf_daily"):
        assert table in b_tables, f"{table} 应归入 B_supplements（本项目独有）"
    assert not (a_tables & b_tables), "同一张表不能同时属于 A 与 B"


def test_sqlite_tables_ship_in_b_supplements() -> None:
    """2026-11-14 审查 §3 ④⑤：公告台账与交易日历必须进包，且放在 B_supplements。"""
    exporter = _load_exporter()
    by_table = {spec.table: spec for spec in exporter.all_specs()}
    for table in ("announcement_registry", "trading_dates"):
        spec = by_table.get(table)
        assert spec is not None, f"{table} 不在导出白名单里"
        assert spec.layer == "B_supplements", f"{table} 应放 B_supplements"
        assert spec.source_class == "自采", f"{table} 来源性质应写「自采」"
        assert spec.backend == "sqlite", f"{table} 在 SQLite 里，后端必须标 sqlite"


def test_notes_never_hardcode_column_counts() -> None:
    """回归审查 §3 ③：note 里写死「51 列」这类数字，重导出永远不会纠正。"""
    exporter = _load_exporter()
    for spec in exporter.all_specs(with_lineage=True):
        assert not re.search(r"\d+\s*列", spec.note), (
            f"{spec.output} 的说明里写死了列数：{spec.note}"
        )
        assert not re.search(r"20\d{2}-\d{2}-\d{2}", spec.note), (
            f"{spec.output} 的说明里写死了日期：{spec.note}"
        )


def test_load_sql_builder_uses_file_paths_not_dict_keys() -> None:
    """回归：首版用 files 的**键**（无扩展名）做 endswith('.parquet') 判断，
    导致 load.sql 生成为空。此处直接校验生成逻辑针对 value 的 file 字段。"""
    source = _EXPORTER.read_text(encoding="utf-8")
    assert 'v.get("file", "")' in source or "v.get('file', '')" in source, (
        "load.sql 的 parquet 判定必须基于 value 的 file 字段（含扩展名）"
    )
    assert 'endswith(".parquet")' in source


def test_load_sql_lists_every_parquet() -> None:
    """load.sql 必须给每张导出的表建一个视图（空包/漏表都是致命问题）。"""
    exporter = _load_exporter()
    entries = {
        "A_updates/income_statement": {"file": "A_updates/income_statement.parquet"},
        "lineage/missing_list": {"file": "lineage/missing_list.parquet"},
        "README.md": {"file": "README.md"},
    }
    text = exporter.render_load_sql(entries)
    assert "read_parquet('A_updates/income_statement.parquet')" in text
    assert "read_parquet('lineage/missing_list.parquet')" in text
    assert "README" not in text


# --------------------------------------------------------------------------- #
# 真实导出（临时库）：文档 / manifest 必须由实测结果生成
# --------------------------------------------------------------------------- #
def _seed(duckdb_store, sqlite_store) -> None:
    """造 2 张 DuckDB 表 + 2 张 SQLite 表的最小数据集。"""
    with duckdb_store.write_connection() as connection:
        seed = Seed(duckdb_store).join(connection)
        seed.prices_bulk(
            [
                ("600519", "2026-06-29", 1500, {"volume": 100}),
                ("600519", "2026-06-30", 1510, {"volume": 120}),
                ("000001", "2026-06-30", 11, {"volume": 200}),
            ]
        )
        # 最新报告期放 6 行（覆盖率样本量门槛是 5 行）：扣非净利润整列为 NULL，
        # 用来验证"字段 X 在当前报告期无数据"这条规则真的会触发。
        for stock_code, report_date, revenue, profit, deducted in [
            ("600519", "2026-03-31", 100, 10, 9),
            ("600519", "2026-06-30", 200, 20, None),
            ("000001", "2026-06-30", 201, 21, None),
            ("000002", "2026-06-30", 202, 22, None),
            ("000004", "2026-06-30", 203, 23, None),
            ("000006", "2026-06-30", 204, 24, None),
            ("000008", "2026-06-30", 205, 25, None),
        ]:
            seed.income(
                stock_code,
                report_date,
                revenue=revenue,
                parent_net_profit=profit,
                deducted_net_profit=deducted,
            )
    with sqlite_store.transaction() as connection:
        # `trading_dates` 只有 `trade_date` 一列，`overwrite=True`（`INSERT OR REPLACE`）
        # 对"同一天再播一次"与原来的 `INSERT OR IGNORE` 落库结果完全一致。
        SeedSqlite(sqlite_store).join(connection).trading_dates(
            ["2026-03-02", "2026-03-31", "2026-04-01", "2026-05-04",
             "2026-06-01", "2026-06-29", "2026-06-30", "2026-07-01"],
            overwrite=True,
        )
        # 留（第 4 类·只出现一次的表）：`announcement_registry` 全仓**只有这一处**播种
        # （`grep -rn announcement_registry tests/ --include=*.py` = 1），且这一行是
        # "文档说含公告台账 → 包里必须真的有"这条对账规则的唯一输入；为单个调用点补
        # 工厂方法只会多一层没有第二个用户的抽象。
        connection.execute(
            """INSERT INTO announcement_registry
                   (announcement_id, stock_code, announcement_time, title)
               VALUES ('ann-1', '600519', '2026-03-01 09:30:00', '年度报告')"""
        )


def _build_minimal(exporter, paths, root: Path) -> dict:
    todo = [spec for spec in exporter.all_specs() if spec.output in _MINIMAL_TABLES]
    assert len(todo) == len(_MINIMAL_TABLES), "最小测试集必须能在白名单里全部找到"
    return exporter.build_package(paths, root, todo=todo)


def test_build_package_generates_docs_from_actual_export(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    """核心断言：README/SOURCES/KNOWN_ISSUES/manifest 的每个数字都来自本次导出。"""
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    manifest = _build_minimal(exporter, database_paths, root)

    # --- manifest 每表可用性元信息（要求 D）---
    income = manifest["files"]["A_updates/income_statement"]
    assert income["rows"] == 7
    # 列数取自 parquet 元数据本身（这正是审查 §3 ③ 抱怨的「51 列 vs 60 列」那种数字）
    assert income["columns"] == pq.ParquetFile(root / income["file"]).metadata.num_columns
    assert income["columns"] > 10
    assert income["date_column"] == "report_date"
    assert income["latest_date"] == "2026-06-30"
    coverage = income["field_coverage"]["coverage"]
    assert coverage["deducted_net_profit"] == 0.0, "最新报告期该字段全 NULL 必须如实反映"
    assert coverage["revenue"] == 1.0
    assert income["field_coverage"]["period_rows"] == 6

    announcement = manifest["files"]["B_supplements/announcement_registry"]
    assert announcement["backend"] == "sqlite"
    assert announcement["date_column"] == "announcement_time"
    assert announcement["latest_date"] == "2026-03-01"

    trading = manifest["files"]["B_supplements/trading_dates"]
    assert trading["rows"] == 8
    assert trading["latest_date"] == "2026-07-01"

    # --- as_of 分域（要求 D）---
    assert manifest["as_of"]["price"]["latest_date"] == "2026-06-30"
    assert manifest["as_of"]["financial"]["latest_date"] == "2026-06-30"
    assert manifest["as_of"]["event"]["latest_date"] == "2026-03-01"
    assert manifest["as_of"]["price"]["tables"]["price_daily_raw"] == "2026-06-30"

    # --- KNOWN_ISSUES 规则生成（要求 A）---
    issues = "\n".join(manifest["known_issues"])
    assert "announcement_registry" in issues and "滞后" in issues, (
        "公告台账 2026-03-01 落后价格域 2026-06-30 超过 5 个交易日，必须自动报出"
    )
    assert "覆盖率为 0%" in issues and "income_statement" in issues, (
        "deducted_net_profit 在最新报告期覆盖率为 0 时必须自动报出"
    )

    # --- README / SOURCES 由实测生成 ---
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "| `income_statement` | 7 |" in readme, "README 行数必须来自实测"
    assert "| 财务三表 | 2026-06-30 |" in readme
    assert "51 列" not in readme and "61 列" not in readme, "不许出现首版硬编码的列数"
    sources = (root / "SOURCES.md").read_text(encoding="utf-8")
    assert "announcement_registry" in sources, "SOURCES 必须列出真正进包的公告台账"
    assert "交易日历" in sources
    assert "fetch_batch" not in sources, "没导出的表不许出现在来源说明里"

    # --- load.sql 覆盖全部 parquet ---
    load_sql = (root / "load.sql").read_text(encoding="utf-8")
    for name in _MINIMAL_TABLES:
        assert f"{name}.parquet" in load_sql
    # --- manifest 自身是自引用项，不参与 sha 校验 ---
    assert manifest["files"]["manifest.json"]["self"] is True
    # --- 声明本次打算导出哪些表，--verify 据此核对完整性 ---
    assert set(manifest["expected_tables"]) == {
        key for key in manifest["files"] if key.count("/") == 1
    }


def test_known_issues_are_not_handwritten_constants() -> None:
    """回归：KNOWN_ISSUES 必须是函数，不许再退化回模块级常量。"""
    exporter = _load_exporter()
    assert not hasattr(exporter, "KNOWN_ISSUES"), "KNOWN_ISSUES 已改为规则生成，不许回退成常量"
    assert callable(exporter.build_known_issues)


# --------------------------------------------------------------------------- #
# --verify 对账（只读）
# --------------------------------------------------------------------------- #
def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }


def test_verify_passes_on_fresh_package(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    _build_minimal(exporter, database_paths, root)
    assert exporter.verify_package(root, paths=database_paths) == 0


def test_verify_is_read_only(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    """要求 C：对账不写任何文件（包目录的内容与 mtime 都不能变）。"""
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    _build_minimal(exporter, database_paths, root)
    before = _snapshot(root)
    exporter.verify_package(root, paths=database_paths)
    assert _snapshot(root) == before, "对账模式改动了包内文件"


def test_verify_detects_file_tampering(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    """文件被动过 = 致命不一致，退出码 1。"""
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    _build_minimal(exporter, database_paths, root)
    readme = root / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n手工改动\n", encoding="utf-8")
    assert exporter.verify_package(root, paths=database_paths) == 1


def test_verify_detects_missing_file(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    _build_minimal(exporter, database_paths, root)
    (root / "B_supplements" / "trading_dates.parquet").unlink()
    assert exporter.verify_package(root, paths=database_paths) == 1


def test_verify_reports_library_drift(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    """库里有新数据 = 漂移（不致命），退出码 2 —— 首版包落后 57 天就靠这个发现。"""
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    _build_minimal(exporter, database_paths, root)
    with duckdb_store.write_connection() as connection:
        # 库里多一天 = 漂移（不致命）→ 退出码 2。
        Seed(duckdb_store).join(connection).prices(
            "600519", [("2026-07-02", 1520)], volume=130
        )
    assert exporter.verify_package(root, paths=database_paths) == 2


def test_verify_reports_package_incompleteness(
    exporter_module, database_paths, duckdb_store, sqlite_store, tmp_path: Path
) -> None:
    """审查 §3 ④⑤：SOURCES 说"含公告台账"、包里却没有 —— 必须能自动抓出来。

    旧包没有 expected_tables 字段，对账按当前 A+B 白名单核对完整性，
    缺的每一张表都算漂移（不致命）。
    """
    exporter = exporter_module
    _seed(duckdb_store, sqlite_store)
    root = tmp_path / "pkg"
    _build_minimal(exporter, database_paths, root)
    # 模拟首版 manifest：没有 expected_tables
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("expected_tables")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert exporter.verify_package(root, paths=database_paths) == 2


def test_verify_without_manifest_is_fatal(tmp_path: Path) -> None:
    exporter = _load_exporter()
    assert exporter.verify_package(tmp_path) == 1


def test_verify_reads_legacy_flat_as_of() -> None:
    """--verify 必须能读 2026-09-19 首版的扁平 as_of（真实旧包就是这个结构）。"""
    exporter = _load_exporter()
    legacy = {"as_of": {"price_latest": "2026-09-17", "financial_latest": "2026-06-30"}}
    domains = exporter._manifest_domains(legacy)
    assert domains["price"]["latest_date"] == "2026-09-17"
    assert domains["financial"]["latest_date"] == "2026-06-30"
