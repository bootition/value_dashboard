"""冷热分库物理切换器（scripts/cold_archive_switch.py）回归（2026-11-15）。

守住四条底线（每条都对应一次真实事故面）
----------------------------------------
1. **切换后数据还在**：views → drop 停车表 → 查询仍读到原数据（不是空视图）；
2. **没建视图就删表必须拒绝**：否则数据删没了还没有替代品；
3. **视图存在但 parquet 丢失/行数不对必须拒绝**：视图是唯一读取路径，
   它读不到数据时删物理表 = 永久丢数据；
4. **写前打印确切 SQL + 写锁生效直接拒绝**：DDL 不能与更新事务交错。

隔离：全部用 ``tmp_path`` 下的临时 DuckDB + 临时 Parquet 包（16 张已切换的迷你
表；清单里 ``physical_switch: pending`` 的第 17 张不参与，理由见 ``cold_switch``
夹具），**绝不触碰** ``data/valuedashboard.duckdb``。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml

from app.core.storage.cold_archive_guard import (
    COLD_ARCHIVE_REMEDY,
    ColdArchiveTableError,
    ensure_writable_tables,
)

_REPO = Path(__file__).resolve().parents[2]
_SWITCH_SCRIPT = _REPO / "scripts" / "cold_archive_switch.py"
_SPLIT_SCRIPT = _REPO / "scripts" / "cold_archive_split.py"


def _load_module(path: Path, name: str):
    """按文件路径加载脚本模块（scripts/ 不是包）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_switch():
    return _load_module(_SWITCH_SCRIPT, "vd_cold_archive_switch")


def _load_split():
    return _load_module(_SPLIT_SCRIPT, "vd_cold_archive_split")


def _query(db_path: Path, sql: str) -> list[tuple]:
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _object_kinds(db_path: Path, names: list[str] | None = None) -> dict[str, str]:
    if names is None:
        rows = _query(
            db_path,
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'main'",
        )
    else:
        placeholders = ", ".join("?" for _ in names)
        connection = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = connection.execute(
                "SELECT table_name, table_type FROM information_schema.tables "
                f"WHERE table_schema = 'main' AND table_name IN ({placeholders})",
                names,
            ).fetchall()
        finally:
            connection.close()
    return {str(name): str(kind) for name, kind in rows}


def _parked_names(config) -> list[str]:
    return [f"{cold.table}__cold_parked" for cold in config.tables]


@pytest.fixture
def cold_switch(tmp_path: Path) -> SimpleNamespace:
    """临时 DuckDB + 临时 Parquet 冷库包（16 张**已切换**的迷你表）。

    2026-11-15：清单变成 17 张，其中 ``csmar_risk_factors`` 是
    ``physical_switch: pending``（已登记、待主控在正式库执行切换），schema.py 里
    它的 DDL 还在 —— ``cold_archive_switch.py`` 会**有意**因残留 DDL 整体拒绝。
    本文件演练的是"切换已放行"之后的行为，所以给脚本喂一份只含
    ``switched_tables()`` 的清单副本（``--config``）；等主控删掉 DDL 并把它改成
    switched，过滤结果自动变成 17 张，测试代码不用再动。
    """
    switch = _load_switch()
    split = _load_split()
    full_config = split.load_config()
    config = replace(full_config, tables=full_config.switched_tables())
    config_path = tmp_path / "cold_archive.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": config.version,
                "source_cutoff": config.source_cutoff,
                "tables": [
                    {
                        "table": cold.table,
                        "reason": cold.reason,
                        "source_cutoff": cold.source_cutoff,
                        "consumers": list(cold.consumers),
                        "physical_switch": cold.physical_switch,
                    }
                    for cold in config.tables
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "valuedashboard.duckdb"

    connection = duckdb.connect(str(db_path))
    try:
        for index, cold in enumerate(config.tables):
            connection.execute(
                f"CREATE TABLE {cold.table} ("
                "stock_code VARCHAR NOT NULL, report_date DATE NOT NULL, value DOUBLE)"
            )
            # 留（第 5 类·测试自建表）：`cold.table` 是冷库包里的表，连接是 `duckdb.connect(db_path)`
            connection.execute(
                f"INSERT INTO {cold.table} VALUES "
                f"('000001', DATE '2020-12-31', {index + 0.5}), "
                f"('600519', DATE '2021-12-31', {index + 1.5})"
            )
    finally:
        connection.close()

    pkg_dir = tmp_path / "cold-pkg"
    target = split.TargetDatabase(db_path)
    split.export_package(target, config.tables, pkg_dir)
    report = split.verify_package(pkg_dir)
    assert report.ok, report.error_text()

    return SimpleNamespace(
        switch=switch,
        split=split,
        config=config,
        config_path=config_path,
        db_path=db_path,
        pkg_dir=pkg_dir,
        expected_rows={
            cold.table: 2 for cold in config.tables
        },
    )


def _run(cold: SimpleNamespace, *args: str) -> int:
    return cold.switch.main(
        [
            *args,
            "--config",
            str(cold.config_path),
            "--pkg",
            str(cold.pkg_dir),
            "--db",
            str(cold.db_path),
        ]
    )


# ───────────────────────── 主链路：views → drop → 仍可查 ─────────────────────────


def test_create_views_dry_run_prints_exact_sql_without_writing(
    cold_switch: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run 必须只打印 SQL（含 read_parquet 绝对路径），主库保持物理表。"""
    cold = cold_switch
    names = [cold.table for cold in cold.config.tables]

    code = _run(cold, "--create-views", "--dry-run")

    assert code == cold.switch.EXIT_OK
    out = capsys.readouterr().out
    assert "CREATE OR REPLACE VIEW" in out
    assert "read_parquet(" in out
    assert cold.pkg_dir.as_posix() in out, "SQL 里必须是冷库包的绝对路径"
    assert all(kind == "BASE TABLE" for kind in _object_kinds(cold.db_path, names).values())


def test_views_then_drop_then_query_still_reads_data(
    cold_switch: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """核心链路：建视图（物理表停车）→ 删停车表 → 视图仍读到原数据。"""
    cold = cold_switch
    names = [cold.table for cold in cold.config.tables]

    assert _run(cold, "--create-views") == cold.switch.EXIT_OK

    # 建视图后：16 个 VIEW + 16 个 __cold_parked 物理表。
    kinds = _object_kinds(cold.db_path, [*names, *_parked_names(cold.config)])
    assert all(kinds[name] == "VIEW" for name in names)
    assert all(kinds[name] == "BASE TABLE" for name in _parked_names(cold.config))

    # 每个视图都能读到 manifest 行数，且值不是空的。
    for name in names:
        assert _query(cold.db_path, f"SELECT COUNT(*) FROM {name}") == [(2,)]
        rows = _query(cold.db_path, f"SELECT value FROM {name} ORDER BY stock_code")
        assert [float(row[0]) for row in rows] != []

    # 默认 dry-run：只打印 SQL，不删停车表。
    assert _run(cold, "--drop-tables") == cold.switch.EXIT_OK
    out = capsys.readouterr().out
    assert "DROP TABLE" in out
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, _parked_names(cold.config)).values()
    )

    # --yes 才真正删；删完视图仍能读。
    assert _run(cold, "--drop-tables", "--yes") == cold.switch.EXIT_OK
    assert _object_kinds(cold.db_path, _parked_names(cold.config)) == {}
    for name in names:
        assert _query(cold.db_path, f"SELECT COUNT(*) FROM {name}") == [(2,)]
    rows = _query(cold.db_path, "SELECT value FROM csmar_fi_t1 ORDER BY stock_code")
    assert [float(row[0]) for row in rows] == [0.5, 1.5]


def test_create_views_is_idempotent(cold_switch: SimpleNamespace) -> None:
    """重复跑 --create-views 不得把停车表再改一次名/报错（幂等）。"""
    cold = cold_switch
    assert _run(cold, "--create-views") == cold.switch.EXIT_OK
    assert _run(cold, "--create-views") == cold.switch.EXIT_OK
    kinds = _object_kinds(
        cold.db_path,
        [*[c.table for c in cold.config.tables], *_parked_names(cold.config)],
    )
    assert sorted(kinds.values()).count("VIEW") == len(cold.config.tables)
    assert sorted(kinds.values()).count("BASE TABLE") == len(cold.config.tables)


def test_init_duckdb_schema_is_safe_after_views(cold_switch: SimpleNamespace) -> None:
    """切换后重启必须能跑 init_duckdb_schema()，且视图不被建表/索引语句破坏。

    这是本任务最核心的重启坑：CREATE INDEX / ALTER TABLE 打在视图上会直接抛错，
    CREATE TABLE IF NOT EXISTS 会静默跳过。schema.py 删干净 49 处 DDL 后，
    这里用真实 init_duckdb_schema() 在"已是视图"的库上跑一遍验证。
    """
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.path_policy import DatabasePathSet, VdEnv
    from app.core.storage.schema import init_duckdb_schema

    cold = cold_switch
    assert _run(cold, "--create-views") == cold.switch.EXIT_OK
    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=cold.db_path,
        sqlite_path=cold.db_path.with_name("valuedashboard.sqlite"),
        run_root=cold.db_path.parent,
    ).validate()
    init_duckdb_schema(DuckDBStore(paths=paths))

    names = [item.table for item in cold.config.tables]
    kinds = _object_kinds(cold.db_path, names)
    assert all(kind == "VIEW" for kind in kinds.values())
    for name in names:
        assert _query(cold.db_path, f"SELECT COUNT(*) FROM {name}") == [(2,)]
    # 对照热表仍由 schema.py 正常建出来（且带索引）。
    # 这条断言不能挂在 csmar_risk_factors 上：它 2026-11-15 已登记进冷库清单
    # （physical_switch: pending），物理切换一执行它就不再是主库物理表。
    assert _object_kinds(cold.db_path, ["financial_sector_items"]) == {
        "financial_sector_items": "BASE TABLE"
    }


# ─────────────────── 冷库前提：cutoff 后不能有新行 ───────────────────


def _insert_hot_row(cold: SimpleNamespace) -> None:
    """往 csmar_fi_t1 写一条 2026 行：模拟"表已重新有写路径"。

    2026-11-15 正式库实测就是这种形态：``financial_sector_items`` 里有 582 行
    eastmoney_f10（2025-06-30~2026-09-30），远晚于 source_cutoff 2025-03-31。
    """
    connection = duckdb.connect(str(cold.db_path))
    try:
        # 留（第 5 类·测试自建表）：直接往**冷库文件**里插热表行，用裸 `duckdb.connect`
        connection.execute(
            "INSERT INTO csmar_fi_t1 VALUES ('000002', DATE '2026-09-30', 9.9)"
        )
    finally:
        connection.close()


def test_create_views_refuses_when_table_has_rows_after_cutoff(
    cold_switch: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """冷表在 source_cutoff 后仍有行 → 默认拒绝转视图（否则新行会丢/写不进）。"""
    cold = cold_switch
    _insert_hot_row(cold)
    names = [item.table for item in cold.config.tables]

    code = _run(cold, "--create-views")

    assert code == cold.switch.EXIT_REFUSED
    assert "已不是静态归档" in caplog.text
    assert all(kind == "BASE TABLE" for kind in _object_kinds(cold.db_path, names).values())


def test_nonstatic_table_requires_explicit_accept_flag(cold_switch: SimpleNamespace) -> None:
    """显式 --accept-nonstatic 后放行；视图读 manifest 行数（热行留在停车表里）。"""
    cold = cold_switch
    _insert_hot_row(cold)

    assert _run(cold, "--create-views", "--accept-nonstatic") == cold.switch.EXIT_OK

    kinds = _object_kinds(
        cold.db_path, [*[c.table for c in cold.config.tables], *_parked_names(cold.config)]
    )
    assert sorted(kinds.values()).count("VIEW") == len(cold.config.tables)
    assert sorted(kinds.values()).count("BASE TABLE") == len(cold.config.tables)
    # 视图读的是冷库包（2 行）；热行仍在停车物理表里（3 行）
    assert _query(cold.db_path, "SELECT COUNT(*) FROM csmar_fi_t1") == [(2,)]
    assert _query(cold.db_path, "SELECT COUNT(*) FROM csmar_fi_t1__cold_parked") == [(3,)]


def test_drop_refuses_nonstatic_parked_table_without_flag(
    cold_switch: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """停车表里仍有 cutoff 后新行 → drop 也必须拒绝（否则新行随物理表一起删掉）。"""
    cold = cold_switch
    _insert_hot_row(cold)
    assert _run(cold, "--create-views", "--accept-nonstatic") == cold.switch.EXIT_OK

    assert _run(cold, "--drop-tables", "--yes") == cold.switch.EXIT_REFUSED
    assert "csmar_fi_t1__cold_parked" in caplog.text
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, _parked_names(cold.config)).values()
    )

    assert _run(cold, "--drop-tables", "--accept-nonstatic", "--yes") == cold.switch.EXIT_OK
    assert _object_kinds(cold.db_path, _parked_names(cold.config)) == {}
    assert _query(cold.db_path, "SELECT COUNT(*) FROM csmar_fi_t1") == [(2,)]


# ───────────────────────── 拒绝场景 ─────────────────────────


def test_drop_refuses_before_views_exist(
    cold_switch: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """没建视图就 drop = 删数据还没替代品，必须拒绝且物理表原样保留。"""
    cold = cold_switch
    names = [cold.table for cold in cold.config.tables]

    code = _run(cold, "--drop-tables", "--yes")

    assert code == cold.switch.EXIT_ERROR
    assert all(kind == "BASE TABLE" for kind in _object_kinds(cold.db_path, names).values())
    assert "没有同名视图" in caplog.text


def test_drop_refuses_when_parquet_missing_after_views(
    cold_switch: SimpleNamespace,
) -> None:
    """视图建好后 parquet 丢失：冷库校验失败，绝不能继续删物理表。"""
    cold = cold_switch
    assert _run(cold, "--create-views") == cold.switch.EXIT_OK
    missing = cold.pkg_dir / "csmar_fi_t1.parquet"
    missing.unlink()

    code = _run(cold, "--drop-tables", "--yes")

    assert code == cold.switch.EXIT_VERIFY_FAILED
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, _parked_names(cold.config)).values()
    )


def test_drop_refuses_when_view_points_to_missing_parquet(
    cold_switch: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """parquet 文件仍在包内、但视图定义指向丢失路径：DROP 前的 COUNT(*) 必须拦住。

    这是最危险的一种：manifest 校验能通过（manifest 里的文件没动），但视图
    本身指向了不存在的 parquet。做法：先把视图指到包内一个**不在 manifest 里**
    的临时 parquet，再删掉它 —— 这样 --verify-cold 仍通过，只有 COUNT(*) 会炸。
    """
    cold = cold_switch
    assert _run(cold, "--create-views") == cold.switch.EXIT_OK
    ephemeral = cold.pkg_dir / "ephemeral-not-in-manifest.parquet"
    connection = duckdb.connect()
    try:
        connection.execute(
            f"COPY (SELECT '000001' AS stock_code, DATE '2020-12-31' AS report_date, "
            f"1.0 AS value) TO '{ephemeral.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        connection.close()
    connection = duckdb.connect(str(cold.db_path))
    try:
        connection.execute(
            f"CREATE OR REPLACE VIEW csmar_fi_t1 AS SELECT * FROM read_parquet("
            f"'{ephemeral.as_posix()}')"
        )
    finally:
        connection.close()
    ephemeral.unlink()

    code = _run(cold, "--drop-tables", "--yes")

    assert code == cold.switch.EXIT_ERROR
    assert "COUNT(*) 失败" in caplog.text
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, _parked_names(cold.config)).values()
    )


def test_drop_refuses_when_view_row_count_mismatch(cold_switch: SimpleNamespace) -> None:
    """视图能读但行数与 manifest 不符（指向了另一份 parquet）→ 拒绝删表。"""
    cold = cold_switch
    assert _run(cold, "--create-views") == cold.switch.EXIT_OK
    # 造一个 1 行的 parquet，把视图指过去：COUNT 成功但行数不对。
    other = cold.pkg_dir / "wrong-rows.parquet"
    connection = duckdb.connect()
    try:
        connection.execute(
            "COPY (SELECT '000001' AS stock_code, DATE '2020-12-31' AS report_date, "
            "1.0 AS value) TO ? (FORMAT PARQUET)",
            [other.as_posix()],
        )
    finally:
        connection.close()
    connection = duckdb.connect(str(cold.db_path))
    try:
        connection.execute(
            f"CREATE OR REPLACE VIEW csmar_fi_t1 AS "
            f"SELECT * FROM read_parquet('{other.as_posix()}')"
        )
    finally:
        connection.close()

    code = _run(cold, "--drop-tables", "--yes")

    assert code == cold.switch.EXIT_ERROR
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, _parked_names(cold.config)).values()
    )


def test_switch_refuses_while_write_lock_is_active(cold_switch: SimpleNamespace) -> None:
    """写锁生效时直接退出 3（DDL 不能与更新事务交错）。"""
    cold = cold_switch
    lock = cold.db_path.parent / ".duckdb.write.lock"
    lock.write_text(f"pid={os.getpid()}\ntime=0\n", encoding="ascii")
    try:
        code = _run(cold, "--create-views")
    finally:
        lock.unlink(missing_ok=True)

    assert code == cold.switch.EXIT_LOCKED
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, [c.table for c in cold.config.tables]).values()
    )


def test_switch_refuses_when_schema_still_declares_cold_ddl(
    cold_switch: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """schema.py 残留 DDL 时拒绝切换（否则重启会建表/启动即报错）。"""
    cold = cold_switch
    fake_hint = cold.split.DdlHint(
        table="csmar_fi_t1",
        kind="index",
        line=1,
        end_line=None,
        text="CREATE INDEX ... ON csmar_fi_t1",
    )
    monkeypatch.setattr(cold.split, "schema_ddl_hints", lambda names: [fake_hint])

    code = _run(cold, "--create-views")

    assert code == cold.switch.EXIT_REFUSED
    assert all(
        kind == "BASE TABLE"
        for kind in _object_kinds(cold.db_path, [c.table for c in cold.config.tables]).values()
    )


def test_verify_cold_rejects_tampered_parquet(cold_switch: SimpleNamespace) -> None:
    """--verify-cold 是第一步门禁：parquet 被改必须失败。"""
    cold = cold_switch
    parquet = cold.pkg_dir / "csmar_fi_t1.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"tampered")

    code = cold.switch.main(
        ["--verify-cold", "--config", str(cold.config_path), "--pkg", str(cold.pkg_dir)]
    )

    assert code == cold.switch.EXIT_VERIFY_FAILED


def test_manifest_must_match_config_exactly(cold_switch: SimpleNamespace) -> None:
    """冷库包少一张表就拒绝（防止只切一半）。"""
    cold = cold_switch
    manifest_path = cold.pkg_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"].pop("csmar_fi_t1")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    code = _run(cold, "--create-views")

    assert code in {cold.switch.EXIT_VERIFY_FAILED, cold.switch.EXIT_ERROR}


# ───────────────────────── 写入护栏（导入脚本）─────────────────────────


def test_guard_rejects_view_target(tmp_path: Path) -> None:
    """导入脚本的共享护栏：目标已是视图时必须抛可行动错误。"""
    connection = duckdb.connect(str(tmp_path / "guard.duckdb"))
    try:
        connection.execute("CREATE TABLE ok_table (x INTEGER)")
        connection.execute("CREATE VIEW frozen_table AS SELECT 1 AS x")
        # 物理表放行
        ensure_writable_tables(connection, ["ok_table"])
        # 视图拒绝，且文案必须包含"已迁冷库 + 先 restore"
        with pytest.raises(ColdArchiveTableError) as error:
            ensure_writable_tables(connection, ["ok_table", "frozen_table"])
        assert "frozen_table" in str(error.value)
        assert COLD_ARCHIVE_REMEDY in str(error.value)
    finally:
        connection.close()


def test_guard_writable_or_exit_uses_dedicated_exit_code(tmp_path: Path) -> None:
    """脚本护栏必须 raise SystemExit(6)，让运维从退出码就能区分冷库拒绝写入。"""
    from contextlib import contextmanager

    from app.core.storage.cold_archive_guard import (
        EXIT_COLD_ARCHIVE_VIEW,
        guard_writable_or_exit,
    )

    connection = duckdb.connect(str(tmp_path / "guard-exit.duckdb"))
    try:
        connection.execute("CREATE VIEW frozen_table AS SELECT 1 AS x")
    finally:
        connection.close()

    class FakeStore:
        @contextmanager
        def read_connection(self):
            conn = duckdb.connect(str(tmp_path / "guard-exit.duckdb"), read_only=True)
            try:
                yield conn
            finally:
                conn.close()

    with pytest.raises(SystemExit) as error:
        guard_writable_or_exit(FakeStore(), ["frozen_table"])
    assert error.value.code == EXIT_COLD_ARCHIVE_VIEW


def test_mandated_import_scripts_have_guard() -> None:
    """6 个指定导入脚本必须 import 并调用冷库护栏（静态检查，防止漏改）。"""
    scripts = (
        "import_csmar.py",
        "import_csmar_deducted_profit.py",
        "import_csmar_indicator_supplement.py",
        "import_csmar_tables_full.py",
        "import_csmar_statement_items.py",
        "import_csmar_aiq_and_anchor.py",
    )
    for name in scripts:
        source = (_REPO / "scripts" / name).read_text(encoding="utf-8")
        assert "cold_archive_guard" in source, f"{name} 未 import 冷库护栏"
        assert "guard_writable_or_exit" in source, f"{name} 未调用冷库护栏"
