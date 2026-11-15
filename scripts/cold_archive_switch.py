#!/usr/bin/env python
"""冷热分库「物理切换」一键开关（2026-11-15，reports/120 遗留项）。

这个脚本解决什么问题
--------------------
``scripts/cold_archive_split.py`` 已把 16 张 CSMAR 静态归档表导出成 Parquet 冷库
（``config/cold_archive.yaml``），并验证了 rows / columns / sha256。但主库仍是
「16 张物理表」。把主库切成「16 个视图 + Parquet」需要严格顺序，任何一步
跳步都会制造「半冷半热」或「删了数据没替代品」的事故：

1. **先校验冷库**：parquet 必须完好（rows/columns/sha256 与 manifest 一致）；
2. **再建视图**：DuckDB 不允许视图与同名物理表共存，所以每张表先把物理表
   停车到 ``<表名>__cold_parked``，再 ``CREATE OR REPLACE VIEW``，最后逐表
   ``SELECT COUNT(*)`` 与 manifest 对账；整个动作在一个事务里，失败全回滚；
3. **最后删物理表**：只有「同名视图存在 + 视图能 ``SELECT COUNT(*)`` + 行数
   与 manifest 一致」才允许 ``DROP TABLE <表名>__cold_parked``。
   **默认 dry-run，必须显式 ``--yes`` 才执行**。

为什么还要检查 schema.py
------------------------
``init_duckdb_schema()`` 每次启动都跑。表转视图后：
- ``CREATE TABLE IF NOT EXISTS`` 会静默跳过（掩盖问题）；
- ``CREATE INDEX ... ON <view>`` 直接 Binder Error；
- ``ALTER TABLE <view>`` 直接 Catalog Error。
所以 ``--create-views`` / ``--drop-tables`` 在动手前会扫 ``schema.py``：只要还残留
任何一处这 16 张表的 DDL，就拒绝并打印 ``cold_archive_split.py --print-ddl``
风格的清单。**schema.py 必须先合并，再跑本脚本**。

用法（严格按顺序，每一步都有验收判据）
--------------------------------------
    # 0) 冷库校验（只读 parquet，不碰数据库）
    python scripts/cold_archive_switch.py --verify-cold --pkg <冷库包目录>

    # 1) 预演建视图 SQL（只读，打印将执行的确切 SQL）
    python scripts/cold_archive_switch.py --create-views --pkg <冷库包目录> --dry-run

    # 2) 建视图（物理表停车到 <表名>__cold_parked；单事务）
    python scripts/cold_archive_switch.py --create-views --pkg <冷库包目录>

    # 3) 预演删表（默认 dry-run；逐表验视图可读 + 行数对账）
    python scripts/cold_archive_switch.py --drop-tables --pkg <冷库包目录>

    # 4) 真正删物理表（最后一刀）
    python scripts/cold_archive_switch.py --drop-tables --pkg <冷库包目录> --yes

回滚方式
--------
- 第 2 步之前：什么都不用做（脚本只读/被拒绝时主库未变）。
- 第 2 步之后、第 4 步之前：物理表还在 ``<表名>__cold_parked``，可以
  ``DROP VIEW <表名>`` + ``ALTER TABLE <表名>__cold_parked RENAME TO <表名>`` 还原；
  或用 ``cold_archive_split.py --views`` 重跑（幂等）。
- 第 4 步之后：只能从冷库包 ``COPY <表名> FROM read_parquet(...)`` 恢复物理表；
  parquet 与 manifest 是唯一事实来源，所以**第 4 步前必须保留冷库包**。

冷库建立后的日常运维
--------------------
- **离线重建后必须重跑 ``--create-views``**：``scripts/rebuild_duckdb.py`` 只搬
  ``duckdb_tables()`` 里的物理表，视图不会搬过去 → 重建后主库没有这 17 个视图。
- **搬冷库目录后必须重跑 ``--create-views``**：视图里存的是建视图那一刻的
  **绝对路径**；manifest 记的是相对文件名，搬完重跑即可，不必重新导出。
- **DROP 不会让主库文件立刻变小**：DuckDB 只是把块标记为可复用，真正释放磁盘
  要到下一次离线重建/导出（收益是"重建后的库更小"，不是"DROP 立刻省空间"）。

冷库前提由数据证明（2026-11-15 新增门禁）
----------------------------------------
config 里的 "静态归档" 只是一句声明，不能当证据。切换前脚本会逐表查日期列：
**只要某张表（或它的停车表）在 ``source_cutoff`` 之后还有行，就默认拒绝**，
必须显式 ``--accept-nonstatic`` 才放行。正式库实测已经抓到一例：
``financial_sector_items`` = 5,058 行 CSMAR（≤2025-03-31）+ **582 行
eastmoney_f10（2025-06-30~2026-09-30）** —— `app/core/supplement.py` 的
FINANCIAL_SECTOR 补采在 2026-11-15 已重新写这张表。**2026-11-15 裁决：该表已
移出冷库清单**（保留主库物理表 + schema.py DDL），冷库只剩 16 张静态表；
**不要**用 `--accept-nonstatic` 把这类表糊过去。

安全边界
--------
- 正式库只允许经 ``require_formal_maintenance_paths()`` 解析（不给 ``--db``）；
  ``--db`` 仅允许 ``VD_ENV=test|staging`` 的临时库演练。
- 任何写操作前检查 ``update_lock.any_write_lock_active()``；有写锁直接退出 3。
- ``--drop-tables`` 默认 dry-run；``--yes`` 才执行。``--create-views`` 也支持
  ``--dry-run`` 先看 SQL。
- 本脚本**不会**被测试/CI 在正式库上执行：回归全部用临时 DuckDB + 临时 Parquet。

退出码
------
0 成功 / 1 运行错误 / 2 参数错误 / 3 写锁生效 / 4 冷库校验失败 / 5 门禁拒绝
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.path_policy import PathIsolationError  # noqa: E402
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cold_archive_switch")

SPLIT_SCRIPT = PROJECT_ROOT / "scripts" / "cold_archive_split.py"
MANIFEST_NAME = "manifest.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3
EXIT_VERIFY_FAILED = 4
EXIT_REFUSED = 5


class SwitchError(RuntimeError):
    """切换过程中的可预期失败（配置/manifest/状态不一致）。"""


@dataclass(frozen=True)
class Package:
    """已通过完整性校验的冷库包。"""

    pkg_dir: Path
    manifest: dict[str, Any]

    @property
    def entries(self) -> dict[str, dict[str, Any]]:
        return self.manifest["tables"]  # type: ignore[no-any-return]

    def rows(self, table: str) -> int:
        return int(self.entries[table]["rows"])

    def parquet_path(self, table: str) -> Path:
        entry = self.entries[table]
        filename = entry.get("file")
        if not isinstance(filename, str) or not filename:
            raise SwitchError(f"{table}: manifest 缺少 file")
        path = (self.pkg_dir / filename).resolve()
        if not _is_inside(path, self.pkg_dir):
            raise SwitchError(f"{table}: parquet 路径逃逸出冷库包目录（{filename}）")
        if not path.is_file():
            raise SwitchError(f"{table}: parquet 不存在（{path}）")
        return path


def _load_split_module() -> Any:
    """按文件路径加载 cold_archive_split.py（scripts/ 不是包）。

    复用它的 manifest 校验 / 视图停车常量，避免两套实现漂移。模块必须先登记到
    ``sys.modules``：脚本里的 ``@dataclass`` 会在 exec 时通过
    ``sys.modules[cls.__module__]`` 取模块字典。
    """
    name = "vd_cold_archive_split"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SPLIT_SCRIPT)
    if spec is None or spec.loader is None:
        raise SwitchError(f"无法加载 {SPLIT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _is_inside(child: Path, parent: Path) -> bool:
    child_str = os.path.normcase(str(child.resolve())).rstrip("\\/")
    parent_str = os.path.normcase(str(parent.resolve())).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _path_literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _expected_names(config: Any) -> list[str]:
    return [cold.table for cold in config.tables]


# ─────────────────────────── 冷库包 ───────────────────────────


def load_package(pkg_dir: Path, config: Any) -> Package:
    """读取 manifest 并核对「清单 = 冷库包表集合」严格一致。

    为什么要严格相等：少一张表 → 切完主库少一个域；多一张表 → 容器里出现
    清单外的视图。宁可启动即失败，也不要静默部分切换。
    """
    split = _load_split_module()
    pkg_dir = pkg_dir.expanduser().resolve()
    manifest_path = pkg_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SwitchError(f"冷库包缺少 manifest.json: {pkg_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SwitchError(f"manifest 不可读: {error}") from error
    if not isinstance(manifest, dict):
        raise SwitchError("manifest 必须是 JSON 对象")
    if manifest.get("format_version") != split.FORMAT_VERSION:
        raise SwitchError(
            f"manifest format_version 不是 {split.FORMAT_VERSION}，拒绝切换"
        )
    entries = manifest.get("tables")
    if not isinstance(entries, dict) or not entries:
        raise SwitchError("manifest 未登记任何表")
    expected = set(_expected_names(config))
    actual = set(entries)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SwitchError(
            f"冷库包与 config/cold_archive.yaml 不一致（缺 {missing} / 多 {extra}）"
        )
    return Package(pkg_dir=pkg_dir, manifest=manifest)


def verified_package(pkg_dir: Path, config: Any) -> Package:
    """先跑冷库校验（rows/columns/sha256 + 回执哈希），再返回包。

    与 ``cold_archive_split.py --verify`` 完全同一套实现（直接调用
    ``verify_package``），所以这里不会出现「两套校验标准」。
    """
    split = _load_split_module()
    report, manifest = split._require_valid_package(pkg_dir)  # noqa: SLF001 - 同域工具
    if not report.ok:
        raise SwitchError("冷库校验未通过：\n" + report.error_text())
    package = load_package(pkg_dir, config)
    # 用校验后的 manifest 重新构造，保证 rows/file 来自刚校验过的内容。
    package = Package(pkg_dir=package.pkg_dir, manifest=manifest)
    total = sum(package.rows(name) for name in package.entries)
    logger.info(
        "[verify-cold] 通过：%d 张表 / %s 行 / manifest 与 parquet 一致（%s）",
        len(package.entries),
        f"{total:,}",
        package.pkg_dir,
    )
    return package


# ─────────────────── schema.py 残留 DDL 检查 ───────────────────


def residual_ddl(config: Any) -> int:
    """schema.py 仍声明冷库表 DDL 时打印清单并返回非 0。

    这不是提醒：CREATE INDEX / ALTER TABLE 打在视图上会让每次启动直接报错。
    """
    split = _load_split_module()
    names = _expected_names(config)
    hints = split.schema_ddl_hints(names)
    if not hints:
        return EXIT_OK
    logger.error(
        "schema.py 仍有 %d 处冷库表 DDL：表转视图后 init_duckdb_schema() 会启动即报错，"
        "拒绝切换。",
        len(hints),
    )
    print()
    print(split.format_ddl_checklist(hints, config=config))
    return EXIT_REFUSED


# ─────────────────── 冷库前提：切后不再有新行 ───────────────────


def find_nonstatic_objects(
    connection: duckdb.DuckDBPyConnection, config: Any
) -> list[dict[str, Any]]:
    """找出「日期列上仍有 source_cutoff 之后新行」的冷库对象（含停车表）。

    冷库成立的唯一前提是**写路径永久关闭**。只要某张表在 cutoff 之后还有行，
    转成只读 Parquet 视图就会让这些行：
    - 写不进去（后续补采/导入直接失败），或
    - 从读者视野里消失（视图读的是导出那一刻的 parquet）。

    这正是 2026-11-15 正式库实测发现的事故面：``financial_sector_items`` 里
    5,058 行 CSMAR（≤2025-03-31）+ **582 行 eastmoney_f10（2025-06-30~2026-09-30）**
    —— 该表已经重新有了写路径。所以切换前必须做这条数据驱动的检查，
    而不是只相信 config 里那句 "静态归档"。

    返回 ``[{object, table, date_column, max_date, cutoff, hot_rows}]``。
    探测失败的表只记 warning（不阻塞）：真正的可读性由 manifest 校验负责。
    """
    split = _load_split_module()
    offenders: list[dict[str, Any]] = []
    for cold in config.tables:
        candidates = [cold.table, f"{cold.table}{split.PARKED_SUFFIX}"]
        for object_name in candidates:
            if split.object_type(connection, object_name) is None:
                continue
            try:
                dated = connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = ? AND data_type = 'DATE' "
                    "ORDER BY ordinal_position",
                    [object_name],
                ).fetchall()
                date_columns = [str(row[0]) for row in dated]
                if not date_columns:
                    continue
                column = next(
                    (c for c in split._DATE_COLUMN_PREFERENCE if c in date_columns),  # noqa: SLF001
                    date_columns[0],
                )
                row = connection.execute(
                    f'SELECT CAST(MAX({_ident(column)}) AS VARCHAR) FROM {_ident(object_name)}'
                ).fetchone()
                max_date = str(row[0])[:10] if row and row[0] is not None else None
                if max_date is None or max_date <= cold.source_cutoff:
                    continue
                hot_rows = connection.execute(
                    f"SELECT COUNT(*) FROM {_ident(object_name)} "
                    f"WHERE {_ident(column)} > CAST(? AS DATE)",
                    [cold.source_cutoff],
                ).fetchone()[0]
                offenders.append(
                    {
                        "object": object_name,
                        "table": cold.table,
                        "date_column": column,
                        "max_date": max_date,
                        "cutoff": cold.source_cutoff,
                        "hot_rows": int(hot_rows),
                    }
                )
            except Exception as error:  # noqa: BLE001 - 探测失败不应让门禁崩溃
                logger.warning("[preflight] %s 静态性探测失败（跳过）：%s", object_name, error)
    return offenders


def guard_static_premise(
    target: Any, config: Any, *, accept_nonstatic: bool
) -> int:
    """切换前的数据驱动门禁：冷表在 cutoff 后仍有行 → 默认拒绝。"""
    with target.reader() as connection:
        offenders = find_nonstatic_objects(connection, config)
    if not offenders:
        return EXIT_OK
    for item in offenders:
        logger.error(
            "[preflight] %-30s 有 %d 行晚于 source_cutoff %s（MAX=%s，列 %s）"
            "—— 该表已不是静态归档，转视图后这些行写不进去/会从读者视野消失",
            item["object"],
            item["hot_rows"],
            item["cutoff"],
            item["max_date"],
            item["date_column"],
        )
    if not accept_nonstatic:
        logger.error(
            "拒绝切换：冷库前提被破坏（见上）。请先裁决 —— 把该表移出 "
            "config/cold_archive.yaml，或把这些行迁到独立热表；"
            "如确认这些行已完整包含在冷库包中且写路径永久关闭，"
            "显式加 --accept-nonstatic。"
        )
        return EXIT_REFUSED
    logger.warning(
        "已通过 --accept-nonstatic 显式放行 %d 个非静态冷库对象（后果自负）",
        len(offenders),
    )
    return EXIT_OK


# ─────────────────────────── SQL 计划 ───────────────────────────


def _print_sql(statements: Sequence[str], *, title: str) -> None:
    print("=" * 100)
    print(title)
    print("=" * 100)
    if not statements:
        print("  （无需执行任何 SQL）")
    for index, statement in enumerate(statements, 1):
        print(f"{index:2d}. {statement};")
    print("=" * 100)


def _indexes_on(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """该表上的索引名（DuckDB 1.5.5 的 RENAME 会被索引阻塞，必须先删）。"""
    try:
        rows = connection.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = ?", [table]
        ).fetchall()
    except Exception:  # noqa: BLE001 - 旧版本没有 duckdb_indexes 时按无索引处理
        return []
    return [str(row[0]) for row in rows]


def plan_create_views(
    connection: duckdb.DuckDBPyConnection,
    package: Package,
    names: Sequence[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """计算「停车物理表 + 建视图」的 SQL（只读探测，不写库）。"""
    split = _load_split_module()
    statements: list[str] = []
    actions: list[dict[str, Any]] = []
    for name in names:
        parquet = package.parquet_path(name)
        kind = split.object_type(connection, name)
        parked = f"{name}{split.PARKED_SUFFIX}"
        parked_kind = split.object_type(connection, parked)
        if kind == "table":
            if parked_kind == "table":
                raise SwitchError(
                    f"{name}: 停车位 {parked} 已存在，拒绝覆盖（先人工确认上次切换残留）"
                )
            # 2026-11-15 实测：DuckDB 1.5.5 里带索引的表**不能** ALTER TABLE RENAME
            # （Dependency Error: Cannot alter entry ... because there are entries
            # that depend on it）。停车表不需要索引（只做回滚保险），所以先删索引。
            for index_name in _indexes_on(connection, name):
                statements.append(f"DROP INDEX IF EXISTS {_ident(index_name)}")
            statements.append(f"ALTER TABLE {_ident(name)} RENAME TO {_ident(parked)}")
        elif kind == "view":
            # 幂等重跑：替换视图定义，不改动已停车的物理表。
            pass
        elif kind is None:
            logger.warning("[views] %s 在主库不存在，将直接创建视图", name)
        statements.append(
            f"CREATE OR REPLACE VIEW {_ident(name)} AS "
            f"SELECT * FROM read_parquet({_path_literal(parquet)})"
        )
        actions.append(
            {
                "table": name,
                "previous": kind,
                "parked": parked if kind == "table" else None,
                "rows": package.rows(name),
                "parquet": parquet.as_posix(),
            }
        )
    return statements, actions


def plan_drop_tables(
    connection: duckdb.DuckDBPyConnection,
    package: Package,
    names: Sequence[str],
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """计算 DROP 计划；任何一表不满足门槛就返回 errors（调用方整体拒绝）。

    门槛（逐表）：
    1. 同名对象必须是**视图**（不是物理表/不存在）；
    2. 视图必须能 ``SELECT COUNT(*)``（parquet 丢失/损坏会在这里炸）；
    3. 行数必须与 manifest 一致（视图真的指向这份冷库）。
    """
    split = _load_split_module()
    statements: list[str] = []
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in names:
        kind = split.object_type(connection, name)
        if kind != "view":
            errors.append(
                f"{name}: 主库里没有同名视图（当前 {kind or '不存在'}）——先跑 --create-views"
            )
            continue
        try:
            view_rows = split.table_rows(connection, name)
        except Exception as error:  # noqa: BLE001 - parquet 丢失/损坏都要计入拒绝
            errors.append(f"{name}: 视图 COUNT(*) 失败（parquet 可能丢失）：{error}")
            continue
        expected = package.rows(name)
        if int(view_rows) != expected:
            errors.append(f"{name}: 视图行数 {view_rows} ≠ manifest {expected}")
            continue
        parked = f"{name}{split.PARKED_SUFFIX}"
        parked_kind = split.object_type(connection, parked)
        if parked_kind == "table":
            statements.append(f"DROP TABLE {_ident(parked)}")
            actions.append({"table": name, "drop": parked, "view_rows": int(view_rows)})
        elif parked_kind is None:
            logger.info("[drop] %-30s 无停车表可删（已是纯视图状态）", name)
        else:
            errors.append(f"{name}: 停车位 {parked} 不是物理表（{parked_kind}）")
    return statements, actions, errors


# ─────────────────────────── 执行 ───────────────────────────


def create_views(target: Any, package: Package, names: Sequence[str]) -> int:
    """建视图（单事务）。返回 0 成功；失败抛异常由 main 统一转退出码。"""
    split = _load_split_module()
    with target.writer() as connection:
        statements, actions = plan_create_views(connection, package, names)
        _print_sql(statements, title="将执行 SQL（同一事务，失败全部回滚）")
        for statement in statements:
            connection.execute(statement)
        # 逐表验：对象类型 = VIEW，且行数与 manifest 一致。
        for action in actions:
            name = action["table"]
            if split.object_type(connection, name) != "view":
                raise SwitchError(f"{name}: 建视图后对象类型不是 VIEW")
            definition = connection.execute(
                "SELECT sql FROM duckdb_views() WHERE view_name = ?", [name]
            ).fetchone()
            normalized = str(definition[0]).replace("\\\\", "/") if definition else ""
            if "read_parquet" not in normalized.lower():
                raise SwitchError(f"{name}: 视图定义里没有 read_parquet")
            if action["parquet"] not in normalized:
                raise SwitchError(
                    f"{name}: 视图未指向冷库包的绝对路径（期望 {action['parquet']}）"
                )
            rows = split.table_rows(connection, name)
            if rows != action["rows"]:
                raise SwitchError(
                    f"{name}: 视图行数 {rows} ≠ manifest {action['rows']}（事务回滚）"
                )
            logger.info(
                "[views] %-30s → 视图（%d 行）%s",
                name,
                rows,
                f"，物理表停车为 {action['parked']}" if action["parked"] else "",
            )
    return EXIT_OK


def _read_parquet_paths(view_sql: str) -> list[Path]:
    """从视图定义里抠出 read_parquet('...') 的路径（只用于"文件还在不在"探测）。"""
    return [Path(match) for match in re.findall(r"read_parquet\(\s*'([^']+)'", view_sql)]


def _revalidate_drop_actions(
    connection: duckdb.DuckDBPyConnection,
    actions: Sequence[dict[str, Any]],
) -> None:
    """写事务里做**不会抛错的**二次确认：对象仍是视图、停车表仍在、parquet 文件仍在。

    为什么不在写事务里跑 ``SELECT COUNT(*)``：DuckDB 里任何查询错误都会把当前
    事务标记为 aborted，后续 rollback/close 会抛 "Current transaction is aborted"，
    掩盖真正的原因。COUNT 校验放在**只读连接**（plan_drop_tables）里做；
    写事务只做 information_schema / 文件存在性探测，这些不会 abort 事务。
    """
    split = _load_split_module()
    for action in actions:
        name = action["table"]
        if split.object_type(connection, name) != "view":
            raise SwitchError(f"{name}: 写事务内复查发现同名对象不再是视图，拒绝 DROP")
        if split.object_type(connection, action["drop"]) != "table":
            raise SwitchError(
                f"{name}: 写事务内复查发现停车表 {action['drop']} 不在了，拒绝 DROP"
            )
        row = connection.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name = ?", [name]
        ).fetchone()
        if row is None:
            raise SwitchError(f"{name}: duckdb_views() 里找不到该视图")
        for path in _read_parquet_paths(str(row[0])):
            if not path.is_file():
                raise SwitchError(f"{name}: 视图引用的 parquet 不存在（{path}），拒绝 DROP")


def drop_tables(target: Any, package: Package, names: Sequence[str], *, execute: bool) -> int:
    """删停车表。execute=False 时只打印 SQL（dry-run）。

    执行顺序（严格）：
    1. **只读连接**里逐表验：视图存在 + ``SELECT COUNT(*)`` 可执行 + 行数=manifest；
       任何一项失败 → 直接拒绝（此时还没打开写事务，主库零改动）；
    2. 打印将执行的确切 SQL；
    3. **写事务**里只做不会触发 aborted 的二次确认（视图/停车表/parquet 文件），
       再 ``DROP TABLE <停车表>``，最后重建视图行数校验（parquet 已在步骤 1 读过）。
    """
    split = _load_split_module()
    with target.reader() as connection:
        statements, actions, errors = plan_drop_tables(connection, package, names)
    if errors:
        for error in errors:
            logger.error("[drop] 拒绝：%s", error)
        return EXIT_ERROR
    _print_sql(
        statements,
        title=(
            "将执行 SQL（同一事务，失败全部回滚）"
            if execute
            else "干跑：将执行 SQL（加 --yes 才真正执行）"
        ),
    )
    if not execute:
        for action in actions:
            logger.info(
                "[drop] %-30s 将删除 %s（视图 rows=%d）",
                action["table"],
                action["drop"],
                action["view_rows"],
            )
        return EXIT_OK

    with target.writer() as connection:
        _revalidate_drop_actions(connection, actions)
        for statement in statements:
            logger.info("[drop] 执行 %s", statement)
            connection.execute(statement)
        # 删完后视图必须仍可读且行数不变。parquet 在步骤 1 已逐份读过，
        # 这里正常不会失败；若真的失败，事务回滚会把停车表原样恢复。
        for action in actions:
            name = action["table"]
            rows = split.table_rows(connection, name)
            if rows != package.rows(name):
                raise SwitchError(
                    f"{name}: DROP 后视图行数 {rows} ≠ manifest {package.rows(name)}"
                )
            logger.info(
                "[drop] %-30s 已删物理表 %s，视图仍可读（%d 行）",
                name,
                action["drop"],
                rows,
            )
    return EXIT_OK


# ─────────────────────────── CLI ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cold_archive_switch.py",
        description="冷热分库物理切换（verify-cold / create-views / drop-tables）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-cold", action="store_true", help="校验冷库 parquet（只读）")
    mode.add_argument(
        "--create-views", action="store_true", help="物理表停车 + 建同名 read_parquet 视图"
    )
    mode.add_argument(
        "--drop-tables", action="store_true", help="删停车表（默认 dry-run，--yes 才执行）"
    )
    parser.add_argument(
        "--pkg", type=Path, required=True, help="冷库包目录（必须含 manifest.json）"
    )
    parser.add_argument("--db", default=None, help="目标库路径（仅 VD_ENV=test/staging 演练用）")
    parser.add_argument("--yes", action="store_true", help="--drop-tables 真正执行（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="--create-views 只打印 SQL 不写库")
    parser.add_argument(
        "--accept-nonstatic",
        action="store_true",
        help="显式放行「cutoff 后仍有新行」的冷表（默认拒绝，见 find_nonstatic_objects）",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="冷库清单 YAML（默认 config/cold_archive.yaml）"
    )
    return parser


def _guard_write_window(target: Any) -> int:
    if any_write_lock_active(target.db_path):
        logger.error(
            "检测到写锁生效（.value-dashboard.update.lock / .duckdb.write.lock）：%s\n"
            "  写窗口内做 DDL 会与更新交错，已拒绝；等更新结束再重试。",
            target.db_path,
        )
        return EXIT_LOCKED
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.yes and args.dry_run:
        logger.error("--yes 与 --dry-run 互斥")
        return EXIT_USAGE
    split = _load_split_module()
    config_path = (args.config or split.CONFIG_PATH).expanduser().resolve()
    try:
        config = split.load_config(config_path)
    except (OSError, ValueError) as error:
        logger.error("冷库清单不可用：%s", error)
        return EXIT_ERROR
    names = _expected_names(config)
    pkg = args.pkg.expanduser().resolve()

    # 0) 冷库校验：只读 parquet，不需要数据库。
    if args.verify_cold:
        try:
            package = verified_package(pkg, config)
        except SwitchError as error:
            logger.error("[verify-cold] %s", error)
            return EXIT_VERIFY_FAILED
        for name in names:
            logger.info("  %-30s %10d 行", name, package.rows(name))
        return EXIT_OK

    # 1) 解析目标库：正式库必须走 path_policy；--db 仅 test/staging。
    try:
        target = split.resolve_target_database(args.db)
    except PathIsolationError as error:
        logger.error("目标库解析失败：%s", error)
        return EXIT_REFUSED
    logger.info("目标库：%s", target.label)
    locked = _guard_write_window(target)
    if locked != EXIT_OK:
        return locked

    # 2) 严格顺序：每次写操作前都重新校验冷库（拒绝使用过期回执）。
    try:
        package = verified_package(pkg, config)
    except SwitchError as error:
        logger.error("%s", error)
        return EXIT_VERIFY_FAILED

    # 3) schema.py 必须已经删掉这些表的 DDL，否则重启会被建回来/启动即报错。
    residual = residual_ddl(config)
    if residual != EXIT_OK:
        return residual

    # 3.5) 冷库前提必须由**数据**证明：cutoff 后仍有新行的表不允许转只读视图。
    try:
        static_code = guard_static_premise(
            target, config, accept_nonstatic=bool(args.accept_nonstatic)
        )
    except (RuntimeError, duckdb.Error) as error:
        logger.error("[preflight] 无法完成静态性检查：%s", error)
        return EXIT_ERROR
    if static_code != EXIT_OK:
        return static_code

    if args.create_views:
        if args.dry_run:
            with target.reader() as connection:
                statements, actions = plan_create_views(connection, package, names)
            _print_sql(statements, title="干跑：将执行 SQL（去掉 --dry-run 才真正执行）")
            for action in actions:
                logger.info(
                    "[views] %-30s 当前=%s → 视图（rows=%d）",
                    action["table"],
                    action["previous"] or "不存在",
                    action["rows"],
                )
            return EXIT_OK
        try:
            code = create_views(target, package, names)
        except (OSError, RuntimeError, duckdb.Error) as error:
            logger.error("[views] 失败（事务已回滚，主库未变）：%s", error)
            return EXIT_ERROR
        logger.info("[views] 完成：%d 张表已转为视图", len(names))
        logger.info("[views] 下一步：重启服务确认可正常启动，再跑 --drop-tables 预演")
        return code

    # 4) 删表：默认 dry-run；--yes 才执行。
    execute = bool(args.yes)
    try:
        code = drop_tables(target, package, names, execute=execute)
    except (RuntimeError, duckdb.Error) as error:
        logger.error("[drop] 失败（事务已回滚，物理表未删）：%s", error)
        return EXIT_ERROR
    if not execute:
        logger.info("[drop] 干跑完成：加 --yes 才真正删除物理表")
    else:
        logger.info("[drop] 完成：停车物理表已删除；视图仍指向冷库 parquet")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
