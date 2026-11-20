"""冷热分离工具（scripts/cold_archive_split.py）回归测试（2026-11-15）。

守住的底线（都是这个工具存在的理由，破了就出数据事故）
------------------------------------------------------
1. **正式库只读**：``--plan`` / ``--export`` 跑完，库文件字节不变；
2. **输出绝不落进 data/**：冷库输出目录在 data/ 下必须被拒绝（且不创建目录）；
3. **写操作默认永不执行**：``--views`` / ``--drop`` 没有 ``--yes`` 一律拒绝；
4. **写窗口内一律拒绝**：存在活写锁时 ``--views`` 直接失败；
5. **校验能真的抓错**：parquet 被改 / manifest 行数被改，``--verify`` 必须失败；
6. **视图先停车再落地**：DuckDB 不允许视图与同名物理表共存，``--views`` 必须把
   物理表改名到 ``__cold_parked``，``--drop`` 才真正删；删之前视图必须能读到
   与 manifest 一致的行数；
7. **重启重建坑的清单必须齐**：schema.py 里每张冷表的 CREATE TABLE / CREATE INDEX
   都要出现在待删清单里（索引语句打在视图上会让每次启动直接报错）。

隔离：全部用 ``tmp_path`` 下的临时 DuckDB + 临时输出目录，**绝不碰**
``data/valuedashboard.duckdb``。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "cold_archive_split.py"

# 与 config/cold_archive.yaml 约定的 17 张表（顺序无关）。
# financial_sector_items 不在其中：2025Q2+ 有 eastmoney_f10 补采热数据，
# 必须留在主库物理表（2026-11-15 裁决，见 config/cold_archive.yaml 头部例外登记）。
# csmar_risk_factors 于 2026-11-15 补登记并完成物理切换（16 → 17 张）：
# 已登记、照常导出，但仍是主库物理表，物理切换由主控在正式库执行。
_EXPECTED_TABLES = {
    "csmar_fi_t1",
    "csmar_fi_t3",
    "csmar_fi_t4",
    "csmar_fi_t5",
    "csmar_fi_t6",
    "csmar_fi_t8",
    "csmar_fi_t9",
    "csmar_fi_t10",
    "csmar_fi_t11",
    "csmar_far_finidx",
    "csmar_balance_items",
    "csmar_income_items",
    "csmar_cashflow_items",
    "csmar_financial_items",
    "csmar_per_share_history",
    "csmar_disclosure_metrics",
    "csmar_risk_factors",
}

_ARCHIVE_CROSSCHECK = "app/core/quality/archive_crosscheck.py"
_DATA_PACKAGE = "scripts/export_data_package.py"


def _load_module():
    """按文件路径加载脚本模块（scripts/ 不是包，与 test_data_package_export 一致）。

    必须先登记到 ``sys.modules``：脚本里的 ``@dataclass`` 会在 exec 时通过
    ``sys.modules[cls.__module__]`` 取模块字典，不登记会 AttributeError。
    """
    name = "vd_cold_archive_split"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cold(tmp_path: Path):
    module = _load_module()
    db_path = tmp_path / "temp.duckdb"
    _build_source_db(db_path)
    return module, module.TargetDatabase(db_path), db_path


def _build_source_db(db_path: Path) -> None:
    """造一个"迷你主库"：两张 CSMAR 形状的表（含元数据列）+ 日期列。"""
    connection = duckdb.connect(str(db_path))
    try:
        connection.execute(
            """
            CREATE TABLE csmar_fi_t1 (
                stock_code VARCHAR NOT NULL,
                report_date DATE NOT NULL,
                "F010101A" DOUBLE,
                "F011701A" DOUBLE,
                source VARCHAR NOT NULL,
                fetch_time TIMESTAMP NOT NULL,
                batch_id VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO csmar_fi_t1 VALUES
              ('000001', DATE '2020-12-31', 1.5, 2.5, 'csmar', TIMESTAMP '2025-04-01 00:00:00', 'b1'),
              ('000001', DATE '2021-12-31', 1.6, 2.6, 'csmar', TIMESTAMP '2025-04-01 00:00:00', 'b1'),
              ('600519', DATE '2020-12-31', 9.9, 9.9, 'csmar', TIMESTAMP '2025-04-01 00:00:00', 'b1')
            """
        )
        connection.execute(
            """
            CREATE TABLE csmar_disclosure_metrics (
                stock_code VARCHAR NOT NULL,
                report_date DATE NOT NULL,
                roe_weighted DOUBLE,
                source VARCHAR NOT NULL,
                fetch_time TIMESTAMP NOT NULL,
                batch_id VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO csmar_disclosure_metrics VALUES
              ('000001', DATE '2019-12-31', 11.1, 'csmar', TIMESTAMP '2025-04-01 00:00:00', 'b2'),
              ('000001', DATE '2020-12-31', 12.2, 'csmar', TIMESTAMP '2025-04-01 00:00:00', 'b2')
            """
        )
    finally:
        connection.close()


def _cold_tables(module):
    return [
        module.ColdTable(
            table="csmar_fi_t1",
            reason="测试用：静态归档域",
            source_cutoff="2025-03-31",
            consumers=(_ARCHIVE_CROSSCHECK,),
        ),
        module.ColdTable(
            table="csmar_disclosure_metrics",
            reason="测试用：官方披露口径",
            source_cutoff="2025-03-31",
            consumers=(_DATA_PACKAGE,),
        ),
    ]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _export(cold, tmp_path: Path, name: str = "pkg") -> Path:
    module, target, _db = cold
    pkg = tmp_path / name
    module.export_package(target, _cold_tables(module), pkg)
    return pkg


def _query(db_path: Path, sql: str):
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


# ───────────────────────── 配置清单 ─────────────────────────


def test_config_lists_exactly_the_agreed_tables() -> None:
    """清单必须正好是约定的 17 张表；已切换 16 张 + 待切换 1 张。

    2026-11-15：`csmar_risk_factors`（67,193 行）补登记进清单（16 → 17 张），
    带 ``physical_switch: pending`` —— 登记与物理切换是两件事，用状态字段表达，
    否则「以为切了其实没切」只能靠口头约定。切换落地时把它改成 switched 即可。
    """
    module = _load_module()
    config = module.load_config()
    names = {cold.table for cold in config.tables}
    assert names == _EXPECTED_TABLES
    # 2026-11-15 第二批切换完成后，清单里不应再有 pending 表。
    assert not config.pending_tables(), "全部 17 张均已物理切换，不应有 pending"
    assert {cold.table for cold in config.switched_tables()} == _EXPECTED_TABLES
    for cold in config.tables:
        assert cold.reason.strip(), f"{cold.table} 缺少 reason"
        assert cold.source_cutoff == "2025-03-31", f"{cold.table} 截止日必须与源一致"


def test_config_registers_the_known_consumers() -> None:
    """消费者登记是"以后谁知道谁在用"的唯一线索，必须与代码事实一致。"""
    module = _load_module()
    by_table = {cold.table: set(cold.consumers) for cold in module.load_config().tables}
    for table in (
        "csmar_fi_t1",
        "csmar_fi_t3",
        "csmar_fi_t4",
        "csmar_fi_t5",
        "csmar_fi_t9",
        "csmar_per_share_history",
    ):
        assert by_table[table] == {_ARCHIVE_CROSSCHECK}, f"{table} 的消费者登记不对"
    assert by_table["csmar_disclosure_metrics"] == {_DATA_PACKAGE}
    # csmar_risk_factors 与 disclosure_metrics 同域：运行时零引用，但数据包发布要带上。
    assert by_table["csmar_risk_factors"] == {_DATA_PACKAGE}
    for table, consumers in by_table.items():
        if table in {
            "csmar_fi_t1",
            "csmar_fi_t3",
            "csmar_fi_t4",
            "csmar_fi_t5",
            "csmar_fi_t9",
            "csmar_per_share_history",
            "csmar_disclosure_metrics",
            "csmar_risk_factors",
        }:
            continue
        assert consumers == set(), f"{table} 当前无运行时消费者，不应登记 {consumers}"


def test_registered_consumer_paths_exist() -> None:
    """消费者路径写错 = 登记失效，所以按仓库真实文件校验。"""
    module = _load_module()
    for cold in module.load_config().tables:
        for consumer in cold.consumers:
            assert (_REPO / consumer).is_file(), f"{cold.table} 登记的消费者不存在: {consumer}"


def test_config_rejects_duplicate_tables(tmp_path: Path) -> None:
    """清单重复表名必须报错（否则导出会覆盖同名列、静默少导一张）。"""
    module = _load_module()
    bad = tmp_path / "cold.yaml"
    bad.write_text(
        "version: 1\nsource_cutoff: '2025-03-31'\ntables:\n"
        "  - {table: csmar_fi_t1, reason: a}\n"
        "  - {table: csmar_fi_t1, reason: b}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        module.load_config(bad)


# ───────────────────────── 计划 / 只读 ─────────────────────────


def test_plan_reports_rows_and_does_not_touch_the_database(cold, tmp_path: Path) -> None:
    """--plan 必须只读：跑完库文件字节不变，且行数/列数真实。"""
    module, target, db_path = cold
    before = _sha256_file(db_path)
    rows = module.plan_tables(target, _cold_tables(module))
    assert _sha256_file(db_path) == before, "--plan 改动了目标库文件"
    by_name = {row.table: row for row in rows}
    assert by_name["csmar_fi_t1"].rows == 3
    assert by_name["csmar_fi_t1"].columns == 7
    assert by_name["csmar_fi_t1"].min_date == "2020-12-31"
    assert by_name["csmar_fi_t1"].max_date == "2021-12-31"
    assert by_name["csmar_fi_t1"].kind == "table"
    text = module.format_plan(rows, config=module.load_config())
    assert "csmar_fi_t1" in text and "合计" in text


def test_plan_measure_reports_real_parquet_size(cold) -> None:
    """--measure 会把表真写成 parquet 量体积（临时目录，测完即删）。"""
    module, target, _db = cold
    rows = module.plan_tables(target, _cold_tables(module), measure=True)
    measured = {row.table: row.measured_bytes for row in rows}
    assert all(size is not None and size > 0 for size in measured.values())
    assert any(row.budget_bytes == row.measured_bytes for row in rows)


def test_plan_reports_missing_table(cold) -> None:
    """清单里的表在主库不存在时，计划必须失败而不是静默跳过。"""
    module, target, _db = cold
    missing = module.ColdTable(table="csmar_fi_t9", reason="x", source_cutoff="2025-03-31")
    with pytest.raises(LookupError, match="csmar_fi_t9"):
        module.plan_tables(target, [missing])


# ───────────────────────── 导出 / 校验 ─────────────────────────


def test_export_writes_manifest_with_required_fields(cold, tmp_path: Path) -> None:
    """manifest 字段是下游（视图/删表/审计）的唯一依据，一个都不能少。"""
    module, target, db_path = cold
    before = _sha256_file(db_path)
    pkg = _export(cold, tmp_path)
    assert _sha256_file(db_path) == before, "--export 改动了正式库文件"

    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    exported_at = datetime.fromisoformat(manifest["exported_at"])
    assert exported_at.utcoffset() == timedelta(0), "exported_at 必须带 UTC 时区"
    assert abs((datetime.now(UTC) - exported_at).total_seconds()) < 600, (
        "exported_at 必须是本次导出的时刻（下游按 UTC 切片展示）"
    )
    assert Path(manifest["source_db"]) == db_path
    assert manifest["source_cutoff"] == "2025-03-31"
    # 清单来源要记对（否则事后无法判断这包是按哪版清单导的）
    assert manifest["config"] == "config/cold_archive.yaml"
    entry = manifest["tables"]["csmar_fi_t1"]
    for field in ("file", "rows", "size_bytes", "sha256", "min_date", "max_date", "columns"):
        assert field in entry, f"manifest 缺少字段 {field}"
    assert entry["rows"] == 3
    assert entry["columns"] == [
        "stock_code",
        "report_date",
        "F010101A",
        "F011701A",
        "source",
        "fetch_time",
        "batch_id",
    ]
    parquet = pkg / entry["file"]
    assert parquet.is_file()
    assert entry["size_bytes"] == parquet.stat().st_size
    assert entry["sha256"] == _sha256_file(parquet)
    assert entry["consumers"] == [_ARCHIVE_CROSSCHECK]
    assert manifest["consumer_index"][_ARCHIVE_CROSSCHECK] == ["csmar_fi_t1"]


def test_export_refuses_output_inside_data_root(cold) -> None:
    """冷库输出绝不能落进 data/（正式库只读区），且不得先建目录。"""
    module, target, _db = cold
    forbidden = module.data_root() / "cold-should-never-exist"
    with pytest.raises(module.PathIsolationError, match="data/"):
        module.export_package(target, _cold_tables(module), forbidden)
    assert not forbidden.exists()


def test_export_refuses_to_overwrite_existing_package(cold, tmp_path: Path) -> None:
    """已有 manifest 的目录不覆盖（避免把两次导出混成一包）。"""
    module, target, _db = cold
    pkg = _export(cold, tmp_path)
    with pytest.raises(FileExistsError):
        module.export_package(target, _cold_tables(module), pkg)


def test_verify_passes_and_writes_receipt(cold, tmp_path: Path) -> None:
    module, _target, _db = cold
    pkg = _export(cold, tmp_path)
    report = module.verify_package(pkg)
    assert report.ok, report.error_text()
    assert report.rows["csmar_fi_t1"] == 3
    receipt = json.loads((pkg / "verified.json").read_text(encoding="utf-8"))
    assert receipt["manifest_sha256"] == _sha256_file(pkg / "manifest.json")


def test_verify_fails_on_tampered_parquet(cold, tmp_path: Path) -> None:
    """parquet 被改（哪怕行数列数都对）必须靠 sha256 抓出来。"""
    module, _target, _db = cold
    pkg = _export(cold, tmp_path)
    target = pkg / "csmar_fi_t1.parquet"
    target.write_bytes(target.read_bytes() + b"tampered")
    report = module.verify_package(pkg)
    assert not report.ok
    assert any("sha256" in error for error in report.errors)


def test_verify_fails_on_row_count_mismatch(cold, tmp_path: Path) -> None:
    """manifest 行数被改（parquet 未动）也必须失败 —— 防止凭空多报/少报行。"""
    module, _target, _db = cold
    pkg = _export(cold, tmp_path)
    manifest_path = pkg / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"]["csmar_fi_t1"]["rows"] = 999
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    report = module.verify_package(pkg)
    assert not report.ok
    assert any("行数" in error for error in report.errors)


def test_verify_fails_on_column_drift(cold, tmp_path: Path) -> None:
    """列名漂移（个数相同）也必须失败：错位会让下游静默算错口径。"""
    module, _target, _db = cold
    pkg = _export(cold, tmp_path)
    manifest_path = pkg / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    columns = manifest["tables"]["csmar_fi_t1"]["columns"]
    columns[2] = "F999999A"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    report = module.verify_package(pkg)
    assert not report.ok
    assert any("列不一致" in error for error in report.errors)


# ───────────────────────── 门禁（--yes / 写锁 / 正式路径）─────────────────────────


def test_views_refuses_without_yes(cold, tmp_path: Path) -> None:
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    code = module.main(["--views", "--pkg", str(pkg), "--db", str(db_path)])
    assert code == module.EXIT_REFUSED
    assert _query(db_path, "SELECT table_type FROM information_schema.tables "
                          "WHERE table_name = 'csmar_fi_t1'") == [("BASE TABLE",)]


def test_views_refuses_while_write_lock_is_active(cold, tmp_path: Path) -> None:
    """写窗口内做 DDL 会与更新事务交错，必须直接拒绝。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    lock = db_path.parent / ".duckdb.write.lock"
    lock.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    try:
        code = module.main(["--views", "--pkg", str(pkg), "--db", str(db_path), "--yes"])
    finally:
        lock.unlink(missing_ok=True)
    assert code == module.EXIT_LOCKED
    assert _query(db_path, "SELECT table_type FROM information_schema.tables "
                          "WHERE table_name = 'csmar_fi_t1'") == [("BASE TABLE",)]


def test_write_modes_refuse_formal_paths_in_test_profile(tmp_path: Path) -> None:
    """不给 --db 时只能解析正式档位；测试档位下必须被拒绝（不能误伤正式库）。"""
    module = _load_module()
    out = tmp_path / "should-not-be-written"
    code = module.main(["--export", "--out", str(out)])
    assert code == module.EXIT_REFUSED
    assert not out.exists()


def test_db_option_rejects_formal_database_path(tmp_path: Path) -> None:
    """哪怕演练也不许把 --db 指向正式库（一道硬保险）。"""
    module = _load_module()
    with pytest.raises(module.PathIsolationError, match="data/"):
        module.resolve_target_database(str(module.data_root() / "valuedashboard.duckdb"))


# ───────────────────────── 建视图 / 删物理表 ─────────────────────────


def test_views_parks_table_and_view_reads_the_parquet(cold, tmp_path: Path) -> None:
    """建视图必须先把物理表停车（DuckDB 不允许同名表与视图共存）。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    code = module.main(["--views", "--pkg", str(pkg), "--db", str(db_path), "--yes"])
    assert code == module.EXIT_OK

    kinds = dict(
        _query(
            db_path,
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_name IN ('csmar_fi_t1', 'csmar_fi_t1__cold_parked')",
        )
    )
    assert kinds == {"csmar_fi_t1": "VIEW", "csmar_fi_t1__cold_parked": "BASE TABLE"}
    assert _query(db_path, "SELECT COUNT(*) FROM csmar_fi_t1") == [(3,)]
    # 视图数据必须和原表一致（不是空视图）
    assert _query(
        db_path, 'SELECT "F011701A" FROM csmar_fi_t1 WHERE stock_code = \'600519\''
    ) == [(9.9,)]
    # 视图定义必须是 read_parquet + 绝对路径（搬目录后要重跑 --views 才能生效）
    definition = _query(
        db_path, "SELECT sql FROM duckdb_views() WHERE view_name = 'csmar_fi_t1'"
    )[0][0]
    assert "read_parquet" in definition
    assert (pkg / "csmar_fi_t1.parquet").as_posix() in definition.replace("\\\\", "/").replace(
        "'", ""
    )


def test_export_refuses_once_tables_became_views(cold, tmp_path: Path) -> None:
    """已经是视图的表不能再"导出"一遍（否则会把冷数据当热数据重复打冷库）。"""
    module, target, db_path = cold
    pkg = _export(cold, tmp_path)
    assert module.main(["--views", "--pkg", str(pkg), "--db", str(db_path), "--yes"]) == module.EXIT_OK
    with pytest.raises(RuntimeError, match="不是物理表"):
        module.export_package(target, _cold_tables(module), tmp_path / "pkg2")


def test_drop_refuses_before_views_exist(cold, tmp_path: Path) -> None:
    """没有同名视图就删表 = 把数据删没了还没替代品，必须拒绝。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    code = module.main(["--drop", "--pkg", str(pkg), "--db", str(db_path), "--yes"])
    assert code == module.EXIT_ERROR
    assert _query(db_path, "SELECT COUNT(*) FROM csmar_fi_t1") == [(3,)]


def test_drop_refuses_without_yes(cold, tmp_path: Path) -> None:
    """--drop 默认永不执行：加了 --views 也不会因为"顺手"就删表。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    assert module.main(["--views", "--pkg", str(pkg), "--db", str(db_path), "--yes"]) == module.EXIT_OK
    assert module.main(["--drop", "--pkg", str(pkg), "--db", str(db_path)]) == module.EXIT_REFUSED
    assert _query(
        db_path,
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_name = 'csmar_fi_t1__cold_parked'",
    ) == [("BASE TABLE",)]


def test_drop_removes_parked_table_and_keeps_view_working(cold, tmp_path: Path) -> None:
    """--drop 只删停车表；视图仍然能读到 parquet 且行数不变。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    assert module.main(["--views", "--pkg", str(pkg), "--db", str(db_path), "--yes"]) == module.EXIT_OK
    code = module.main(["--drop", "--pkg", str(pkg), "--db", str(db_path), "--yes"])
    assert code == module.EXIT_OK
    remaining = {
        name for (name,) in _query(db_path, "SELECT table_name FROM information_schema.tables")
    }
    assert "csmar_fi_t1__cold_parked" not in remaining
    assert "csmar_fi_t1" in remaining
    assert _query(db_path, "SELECT COUNT(*) FROM csmar_fi_t1") == [(3,)]
    assert _query(db_path, "SELECT COUNT(*) FROM csmar_disclosure_metrics") == [(2,)]


def test_drop_refuses_when_package_no_longer_verifies(cold, tmp_path: Path) -> None:
    """verify 不通过就绝不删物理表（退出码必须是校验失败，而不是"删了再说"）。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    assert module.main(["--views", "--pkg", str(pkg), "--db", str(db_path), "--yes"]) == module.EXIT_OK
    parquet = pkg / "csmar_fi_t1.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"corrupt")
    assert module.main(["--drop", "--pkg", str(pkg), "--db", str(db_path), "--yes"]) == (
        module.EXIT_VERIFY_FAILED
    )
    assert _query(
        db_path,
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_name = 'csmar_fi_t1__cold_parked'",
    ) == [("BASE TABLE",)]


def test_verify_mode_exit_code_is_non_zero_on_failure(cold, tmp_path: Path) -> None:
    """--verify 失败必须以非 0 退出码告知调用方（CI/运维脚本的唯一信号）。"""
    module, _target, db_path = cold
    pkg = _export(cold, tmp_path)
    parquet = pkg / "csmar_fi_t1.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"corrupt")
    assert module.main(["--verify", "--pkg", str(pkg)]) == module.EXIT_VERIFY_FAILED
    assert db_path.exists()


# ───────────────────────── schema.py 待删清单 ─────────────────────────


def _write_schema_with_cold_ddl(tmp_path: Path) -> Path:
    """造一份"尚未删 DDL"的 schema 片段，专门测扫描器本身。

    2026-11-15 冷热分库物理切换后，真实 ``schema.py`` 里这些 DDL 已全部删除
    （回归见 ``test_real_schema_has_no_cold_ddl``）。如果继续拿真实文件测扫描器，
    扫描器一旦坏掉反而"没有 DDL 可扫"而假通过。所以这里自造带 DDL 的样本：
    每表 1 个 CREATE TABLE + 2 个 CREATE INDEX（DDL 段 + 迁移函数段各一条），
    ``csmar_balance_items`` 再多 1 条 ALTER（视图不能 ALTER 的那类）。
    """
    lines: list[str] = []
    for table in sorted(_EXPECTED_TABLES):
        lines.extend(
            [
                f"CREATE TABLE IF NOT EXISTS {table} (",
                "    stock_code VARCHAR NOT NULL,",
                "    report_date DATE NOT NULL,",
                "    source VARCHAR NOT NULL",
                ");",
                f"CREATE INDEX IF NOT EXISTS idx_{table}_stock",
                f"    ON {table} (stock_code, report_date);",
                "",
            ]
        )
    lines.append("def init_duckdb_schema(store):")
    lines.append("    with store.transaction() as connection:")
    for table in sorted(_EXPECTED_TABLES):
        lines.append(
            f'        connection.execute("CREATE INDEX IF NOT EXISTS idx_{table}_mig '
            f'ON {table} (stock_code, report_date)")'
        )
    alter_sql = (
        "ALTER TABLE csmar_balance_items ADD COLUMN IF NOT EXISTS "
        '"A004000000" DOUBLE'
    )
    lines.append(f"        connection.execute({alter_sql!r})")
    path = tmp_path / "schema_fixture.py"
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return path


def test_ddl_checklist_covers_every_cold_table(tmp_path: Path) -> None:
    """每张冷表都要在清单里出现 CREATE TABLE + CREATE INDEX 两类语句。

    索引语句打在视图上会让每次启动的 init_duckdb_schema() 直接 BinderException，
    所以清单漏一条就等于"重启炸库"（reports/117 R18 同类事故）。
    """
    module = _load_module()
    config = module.load_config()
    schema_path = _write_schema_with_cold_ddl(tmp_path)
    hints = module.schema_ddl_hints(
        [cold.table for cold in config.tables], schema_path=schema_path
    )
    # 每表 1 表 DDL + 2 索引（DDL 段 + 迁移段），csmar_balance_items 再多 1 条 ALTER。
    # 处数从配置推导（16×3+1=49）：清单再变一次就不该再改断言里的常数。
    expected_hints = 3 * len(config.tables) + 1
    assert len(hints) == expected_hints, (
        f"样本处数应为 3×{len(config.tables)}表+1 ALTER = {expected_hints}，实际 {len(hints)}"
    )
    by_table: dict[str, set[str]] = {}
    for hint in hints:
        by_table.setdefault(hint.table, set()).add(hint.kind)
    assert set(by_table) == _EXPECTED_TABLES
    for table in _EXPECTED_TABLES:
        assert "table" in by_table[table], f"{table} 的 CREATE TABLE 未进清单"
        assert "index" in by_table[table], f"{table} 的 CREATE INDEX 未进清单"
    # 视图不能 ALTER：csmar_balance_items 在迁移块里有 ADD COLUMN
    assert "alter" in by_table["csmar_balance_items"]
    assert all(hint.line > 0 for hint in hints)
    text = module.format_ddl_checklist(hints, config=config)
    assert "schema.py" in text and "csmar_fi_t1" in text
    # 清单必须按行号从大到小排：人工删块要从文件底部往上删，否则行号漂移要重做
    row_max = [
        max(int(value) for value in re.findall(r"L(\d+)", line))
        for line in text.splitlines()
        if re.findall(r"L(\d+)", line)
    ]
    assert row_max == sorted(row_max, reverse=True), f"待删清单未按行号倒序: {row_max}"


def test_ddl_checklist_reports_real_line_numbers(tmp_path: Path) -> None:
    """行号必须真的指到传入的 schema 文件那行（写死行号一周后就会骗人）。"""
    module = _load_module()
    schema_path = _write_schema_with_cold_ddl(tmp_path)
    hints = module.schema_ddl_hints(["csmar_fi_t1"], schema_path=schema_path)
    table_hint = next(hint for hint in hints if hint.kind == "table")
    lines = schema_path.read_text(encoding="utf-8").splitlines()
    assert "CREATE TABLE IF NOT EXISTS csmar_fi_t1" in lines[table_hint.line - 1]
    assert table_hint.end_line is not None and table_hint.end_line > table_hint.line


def test_real_schema_has_no_cold_ddl() -> None:
    """真实 schema.py：已切换的冷表 DDL 必须删干净（16 张 / 49 处）。

    这是物理切换的硬前提：``init_duckdb_schema()`` 每次启动都跑，
    CREATE INDEX / ALTER TABLE 打在视图上会直接让服务起不来。

    2026-11-15：清单变成 17 张，其中 ``csmar_risk_factors`` 是
    ``physical_switch: pending``（已登记、待主控在正式库执行切换），它**必须**
    仍在 schema.py 里 —— 两个方向都钉住，避免出现"清单说切了、库里其实还是
    物理表"这类静默漂移：`pending` 表有 DDL 才算合规，切换后改成 switched
    就会落到上面的"零 DDL"断言里。
    """
    module = _load_module()
    config = module.load_config()
    switched = [cold.table for cold in config.switched_tables()]
    pending = [cold.table for cold in config.pending_tables()]
    hints = module.schema_ddl_hints(switched)
    assert hints == [], (
        "schema.py 仍残留冷库表 DDL："
        + ", ".join(f"{hint.table}:{hint.kind}@L{hint.line}" for hint in hints)
    )
    assert not pending, "清单里不应再有 pending 表；17 张已于 2026-11-15 全部物理切换"
