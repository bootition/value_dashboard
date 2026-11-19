"""DuckDB schema 模板：把「每个测试跑一遍完整 DDL」换成「每会话建一次 + 每测试拷一份」。

## 为什么要有这个模块（.planning/2026-11-19-test-audit/T4_cost.md §3.1 / §4 / §5①）

* ``init_duckdb_schema()`` 实测中位 **0.404 s**（1315 行 DDL + ~35 条
  ``ALTER TABLE … ADD COLUMN IF NOT EXISTS``）；
* ``duckdb_store`` fixture 被 **956 条**测试使用 → 956 × 0.404 ≈ **363 s**
  = 全量 1749 s 的 **20.8%**，是所有可优化项里最大的一笔**固定税**；
* 而「复制一份已经建好的 schema 文件 + 打开 + 查询」实测 **0.035–0.040 s**
  （纯 ``shutil.copyfile`` 只占 0.003 s），比重新跑 DDL 快约 **10 倍**。

schema 内容是**与测试无关的常量**（``DUCKDB_SCHEMA_V1`` + 迁移语句），
所以「每条测试从头跑一遍」和「拷一份跑完的结果」在建出的库上等价 ——
差别只有 ``CURRENT_TIMESTAMP`` 默认值/``applied_at`` 的取值时刻。

## 不变式（替代方案的前提；破任何一条都必须回滚到「每测试 init」）

1. **模板只由本模块创建、关闭、重命名，绝不交给被测代码**：本模块返回的
   路径文件名是 ``schema-template.duckdb``，而
   ``DatabasePathSet.validate()`` 只接受 ``valuedashboard.duckdb`` ——
   即使有人误把模板路径塞进 ``DuckDBStore``，也会在第一道门禁上直接失败。
2. **模板必须干净关闭**：DuckDB 最后一个连接关闭时会 checkpoint 并删除
   ``<db>.wal``；复制前断言没有残留 WAL（有 WAL 说明还有活句柄，此时
   「只拷主文件」会静默丢数据 → 直接抛错，不复制）。
3. **每个测试拿到独立文件副本**：``materialize_duckdb_from_template()`` 只做
   ``copyfile``，复制件有自己的 inode / WAL / 写锁文件，fixture 之间不共享
   任何连接或句柄；模板本体对测试不可达，因此测试写得再脏也污染不到模板。
4. **复制件可写**：只读位只打在模板原件上（复制件由 ``copyfile`` 新建，
   不带源文件属性），并在复制后显式 ``chmod`` 成可读写。
5. **内容 == 一次真实 ``init_duckdb_schema()`` 的结果**（含 ``schema_migrations``
   的 48 条版本行、v12 ``funding_events`` 无复合主键的终态），所以断言
   「迁移后状态」的用例语义不变；**「从零建库」这条路径**由继续显式调用
   ``init_duckdb_schema()`` 的用例覆盖（见
   ``tests/regression/test_schema_template_fixture.py``）。
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import (
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    canonicalize_path,
)
from app.core.storage.schema import init_duckdb_schema

#: 模板原件文件名。**故意**不等于 ``valuedashboard.duckdb``：
#: ``DatabasePathSet.validate()`` 只接受后者，因此模板在结构上不可能被当成
#: 「某个测试的隔离库」打开（不变式 1）。
TEMPLATE_DB_FILENAME = "schema-template.duckdb"

#: 建模板时用的临时文件名 —— 必须是 path_policy 认可的正式名，
#: 建完立刻改名为 ``TEMPLATE_DB_FILENAME``。
_BUILD_SOURCE_FILENAME = "valuedashboard.duckdb"

_WAL_SUFFIX = ".wal"

#: 期望的最低表数（实测 49 张表 / 2 个视图 / 48 条迁移记录）。
#: 取一个宽松下界：只用来把「模板是空的/建歪了」变成一次立即失败，
#: 不承担「schema 有哪些表」的契约（那由 test_asset_health 等真 DDL 用例负责）。
_MIN_TABLES = 40

#: 计数账本：让「init 从每测试一次降到每会话一次」可被测试直接断言，
#: 而不是靠读源码或掐表推断。
_STATS: dict[str, int] = {
    "template_builds": 0,
    "init_duckdb_schema_calls": 0,
    "materializations": 0,
}

_READ_ONLY = stat.S_IREAD
_READ_WRITE = stat.S_IREAD | stat.S_IWRITE


def snapshot() -> dict[str, int]:
    """返回计数账本快照（调用方据此做 delta 断言）。"""
    return dict(_STATS)


def build_schema_template(directory: Path) -> Path:
    """在 *directory* 下用**真实** ``init_duckdb_schema()`` 建出 schema 模板。

    只应由 session 级 fixture 调用一次。返回模板原件的绝对路径（已设为只读）。
    """
    workdir = Path(directory)
    _require_isolation(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    source = workdir / _BUILD_SOURCE_FILENAME
    template = workdir / TEMPLATE_DB_FILENAME
    _remove_if_exists(source)
    _remove_if_exists(template)
    _remove_if_exists(_wal_of(source))
    _remove_if_exists(_wal_of(template))

    validated_root = canonicalize_path(workdir)
    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=validated_root / _BUILD_SOURCE_FILENAME,
        sqlite_path=validated_root / "valuedashboard.sqlite",
        run_root=validated_root,
    ).validate()

    _STATS["init_duckdb_schema_calls"] += 1
    store = DuckDBStore(paths=paths)
    init_duckdb_schema(store)

    # 自检：模板必须是「真建成的库」，否则这里立即失败比 956 条测试各失败一次强。
    table_count = int(store.read_query("SELECT count(*) AS c FROM duckdb_tables()")[0]["c"])
    if table_count < _MIN_TABLES:
        raise RuntimeError(
            f"schema template under {workdir} has only {table_count} tables "
            f"(expected >= {_MIN_TABLES}); refusing to serve it to the test suite"
        )

    _assert_cleanly_closed(source)
    source.replace(template)
    _assert_cleanly_closed(template)
    _freeze(template)
    _STATS["template_builds"] += 1
    return template


def materialize_duckdb_from_template(template: Path, destination: Path) -> Path:
    """把模板复制成 *destination* 处的一份独立、可写的库文件。

    目标已存在（或残留 WAL）时先删 —— 与「每条测试拿到一个全新空库」的
    旧语义保持一致：这里**从不**在已有库上增量修改。
    """
    source = Path(template)
    dest = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"schema template is missing: {source}")
    _remove_if_exists(dest)
    _remove_if_exists(_wal_of(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    # copyfile 建的是新文件（不带源文件的只读属性/权限位），
    # 但显式 chmod 一次更稳：Windows 上「属性」与 POSIX 上「mode」语义不同，
    # 这一步保证复制件**对 DuckDB 一定可写**（不变式 4）。
    os.chmod(dest, _READ_WRITE)
    _STATS["materializations"] += 1
    return dest


def release_schema_template(template: Path) -> None:
    """把模板原件恢复成可写（session 结束时调用，便于临时目录被清理）。"""
    with_writable = Path(template)
    if with_writable.exists():
        os.chmod(with_writable, _READ_WRITE)


def _require_isolation(workdir: Path) -> None:
    """模板目录必须落在 wrapper 提供的 ``VD_TEST_RUN_ROOT`` 内。

    与 ``tests/conftest.py::database_paths`` 同一道门禁：模板库同样是「真实
    DuckDB 文件库」，绝不能落在仓库里或系统临时目录的无人管辖处。
    """
    run_root_raw = os.environ.get("VD_TEST_RUN_ROOT")
    if not run_root_raw:
        raise PathIsolationError("VD_TEST_RUN_ROOT is required to build the schema template")
    run_root = Path(run_root_raw)
    if not run_root.is_dir():
        raise RuntimeError("wrapper-created VD_TEST_RUN_ROOT is missing")
    canonical_run_root = canonicalize_path(run_root)
    canonical_workdir = canonicalize_path(workdir)
    if canonical_run_root not in canonical_workdir.parents:
        raise PathIsolationError(
            f"schema template escaped VD_TEST_RUN_ROOT: {canonical_workdir}"
        )


def _wal_of(path: Path) -> Path:
    return Path(f"{path}{_WAL_SUFFIX}")


def _remove_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def _assert_cleanly_closed(database_file: Path) -> None:
    """断言库文件已干净关闭（没有残留 WAL）。

    DuckDB 在最后一个连接关闭时自动 checkpoint 并删除 ``.wal``；残留 WAL 说明
    还有活句柄或上次不是干净退出 —— 此时复制出去的库会缺少 WAL 里的内容，
    属于「静默丢数据」，所以这里一律失败而不是「顺便把 WAL 一起拷走」。
    """
    wal = _wal_of(database_file)
    if wal.exists():
        raise RuntimeError(
            f"schema database was not cleanly closed (residual WAL: {wal}); "
            "refusing to use it as a per-test template"
        )


def _freeze(path: Path) -> None:
    """模板原件设为只读：任何「不小心写到模板本体」的尝试都会直接报错。"""
    os.chmod(path, _READ_ONLY)
