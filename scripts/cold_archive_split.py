#!/usr/bin/env python
"""CSMAR 历史归档域冷热分离（2026-11-15，冷库主交付物）。

为什么做这件事
--------------
正式库约 28GB / 59 张表，其中 CSMAR 历史归档域 **17 张表**（16 张已完成物理
切换，第 17 张 ``csmar_risk_factors`` 于 2026-11-15 补登记、已切换）都是
**源截止 2025-03-31 的静态数据**（导入后写路径永久关闭），却与每天变化的
行情/三表/指标挤在同一个 DuckDB 文件里：备份、哈希核对、离线重建都要为这
几百万行重复付费，而且每次都要付出。

分层依据选**可变性**而不是体积：体积会变，可变性不会。一张 300K 行的静态表
每天被整库备份一次，成本随时间无限累积；一张每天都要写的热表却不能分出去。
所以冷库清单只收「写路径永久关闭」的表（清单见 ``config/cold_archive.yaml``）。

复用的既有范式（不重复造轮子）
------------------------------
- manifest + 逐文件 sha256 + ``verified.json`` 回执：``app/core/archive.py``
  （``vd archive create/verify/restore``）。本脚本沿用同一套「manifest 是唯一
  事实、校验回执绑定 manifest 哈希」的结构，运维不必学第二套。
- 逐表记 rows/sha256、临时文件原子改名：``scripts/repartition_cold_archive.py``
  （该脚本按年分区，本脚本的表每张约 30 万行、无需分区，故一表一文件）。
- ``COPY ... TO ... (FORMAT PARQUET, COMPRESSION ZSTD)`` + 临时文件原子改名：
  ``scripts/export_data_package.py``。
- 正式档位路径校验 ``require_formal_maintenance_paths()``：``app/core/storage/path_policy.py``。
- 写窗口判定 ``any_write_lock_active()``：``app/core/storage/update_lock.py``。

为什么 ``--views`` 必须先「停车」物理表
---------------------------------------
DuckDB 不允许视图与同名物理表共存（实测 1.5.x）：

    CREATE OR REPLACE VIEW t ...  →  Catalog Error: Existing object t is of type Table

所以 ``--views`` 在**同一事务**里做三步：``ALTER TABLE t RENAME TO t__cold_parked``
→ 建视图 → 立刻 ``SELECT COUNT(*) FROM t`` 与 manifest 行数比对。任何一步失败
整事务回滚，物理表原样留存，主库不会出现"半冷半热"的中间态。
真正 ``DROP TABLE t__cold_parked`` 的是 ``--drop``（默认永不执行）。

重启重建的坑（reports/117 R18 同类事故）
----------------------------------------
``app/core/storage/schema.py`` 里这些表是 ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` / ``ALTER TABLE``。转成视图后实测会这样炸：
- ``CREATE TABLE IF NOT EXISTS``：对同名视图静默跳过（不炸，但会掩盖问题）；
- ``CREATE INDEX ... ON t``：**Binder Error: can only create an index on a base table**；
- ``ALTER TABLE t ADD COLUMN``：**Catalog Error: Can only modify view with ALTER VIEW**。
后两者在每次启动的 ``init_duckdb_schema()`` 里都会跑，所以 ``--views`` / ``--drop``
必须打印「还需要从 schema.py 删除哪些 DDL 块」的清单（``--print-ddl`` 可单独查看）。

用法
----
    # 0) 只读体检（不写正式库；--measure 会真测体积，写系统临时目录后即删）
    python scripts/cold_archive_split.py --plan
    python scripts/cold_archive_split.py --plan --measure

    # 1) 导出冷库（正式库只读；输出目录必须在 data/ 之外）
    python scripts/cold_archive_split.py --export --out D:/vd-cold-archive/csmar-20261115

    # 2) 校验（重读 parquet 比 rows/columns/sha256，与 manifest 一致才通过）
    python scripts/cold_archive_split.py --verify --pkg D:/vd-cold-archive/csmar-20261115

    # 3) 建视图（物理表自动停车到 <表名>__cold_parked；必须 --yes）
    python scripts/cold_archive_split.py --views --pkg <pkg> --yes

    # 4) 删物理表（最后一刀，默认永不执行；必须 --yes）
    python scripts/cold_archive_split.py --drop --pkg <pkg> --yes

    # 附：只看 schema.py 待删清单
    python scripts/cold_archive_split.py --print-ddl

冷库建立后的日常运维（务必照做）
--------------------------------
- **离线重建后必须重跑 --views**：``scripts/rebuild_duckdb.py`` 只搬
  ``duckdb_tables()`` 里的物理表，视图不会被搬过去。重建完成后立刻
  ``--views --pkg <pkg> --yes``（幂等：物理表已不在时它就只建视图）。
- **搬冷库目录后必须重跑 --views**：视图里存的是建视图那一刻的**绝对路径**。
  manifest 记的是相对文件名，所以搬完重跑一次即可，不用重新导出。
- **DROP 不会让主库文件立刻变小**：DuckDB 只是把块标记为可复用，真正释放
  磁盘空间要到下一次重建/导出（28GB → 小库是"重建收益"，不是"DROP 收益"）。

安全门禁（三重，缺一不可）
--------------------------
1. 目标库默认只能经 ``require_formal_maintenance_paths()`` 解析到正式档位；
   ``--db`` 仅允许 ``VD_ENV=test|staging``（演练/回归），正式档位下直接拒绝。
2. 所有写操作（``--views`` / ``--drop``）必须显式 ``--yes``，且写前
   ``any_write_lock_active()`` 为假。
3. 正式库写入走 ``DuckDBStore.transaction()``（自带 ``.duckdb.write.lock`` 与
   维护锁断言）；``--drop`` 还要求 verify 通过 + 同名视图已存在且行数对得上。

退出码
------
0 成功 / 1 运行错误 / 2 参数错误 / 3 写锁生效 / 4 校验失败 / 5 门禁拒绝（缺 --yes 等）
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    data_root,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cold_archive_split")

CONFIG_PATH = PROJECT_ROOT / "config" / "cold_archive.yaml"
SCHEMA_PATH = PROJECT_ROOT / "app" / "core" / "storage" / "schema.py"

MANIFEST_NAME = "manifest.json"
VERIFIED_NAME = "verified.json"

# 物理表"停车位"：视图建好后旧表改名到此，等 --drop 真正删除。
# 用前缀无关的后缀，便于 `SHOW TABLES` 一眼认出哪些是待删的冷库残留。
PARKED_SUFFIX = "__cold_parked"

# 清单条目的物理切换状态（config/cold_archive.yaml 的 ``physical_switch``）：
#   switched = 已完成 export → create-views → drop，主库只剩 read_parquet 视图；
#   pending  = 已登记进清单（要导出、要算进清单总数），但 schema.py 里它的 DDL
#              还在、主库仍是物理表，物理切换由主控按顺序执行。
# 「登记」与「切换」必须能分开表达：清单是导出/建视图/删表的唯一事实来源，
# 只认清单的话「先登记、后切换」就只能靠口头约定，而这正是本仓库反复踩的坑。
SWITCH_SWITCHED = "switched"
SWITCH_PENDING = "pending"
_ALLOWED_SWITCH_STATES = frozenset({SWITCH_SWITCHED, SWITCH_PENDING})

FORMAT_VERSION = 1
PARQUET_COMPRESSION = "ZSTD"
# SQL 里拼接压缩格式，白名单防注入（值来自常量，但保持防御习惯）
_ALLOWED_COMPRESSION = frozenset({"ZSTD", "SNAPPY", "GZIP", "UNCOMPRESSED"})

# 计划模式体积估算：parquet+zstd 的每行字节数。2026-11-15 在正式库实测标定
# （当时 17 张表 4,557,779 行 → 406.0 MB，均值 93.4 B/行；单表区间 10~178 B/行：
# csmar_fi_t3 178 B、csmar_fi_t11 10 B —— 后者 13 列几乎全 NULL，压缩后极小。
# 2026-11-15 起 financial_sector_items（5,058 行）已移出清单：16 张表 /
# 4,552,721 行 → 约 405.5 MB，均值仍是 93.4 B/行，故本常量不变）。
# 2026-11-15 补登记并**完成物理切换** csmar_risk_factors（67,193 行，
# physical_switch: switched）：清单 17 张全部已切换，冷库包实测
# 4,619,914 行 / 411.4 MB。
# 取 95 作为均值口径，只用于"够不够空间"的心算；要准确数字请用 --measure 实测。
ESTIMATED_BYTES_PER_ROW = 95

# 日期列探测顺序：本域全部是报告期表，report_date 优先；其余是兜底。
_DATE_COLUMN_PREFERENCE = (
    "report_date",
    "trade_date",
    "announcement_date",
    "event_date",
    "end_date",
)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3
EXIT_VERIFY_FAILED = 4
EXIT_REFUSED = 5


# ─────────────────────────── 配置 ───────────────────────────


@dataclass(frozen=True)
class ColdTable:
    """冷库清单里的一行（config/cold_archive.yaml 的 tables 条目）。"""

    table: str
    reason: str
    source_cutoff: str
    consumers: tuple[str, ...] = ()
    physical_switch: str = SWITCH_SWITCHED


@dataclass(frozen=True)
class ColdArchiveConfig:
    version: int
    source_cutoff: str
    tables: tuple[ColdTable, ...]
    path: Path = CONFIG_PATH

    def by_name(self) -> dict[str, ColdTable]:
        return {item.table: item for item in self.tables}

    def switched_tables(self) -> tuple[ColdTable, ...]:
        """已完成物理切换的表（主库只剩视图）。切换/删表只对这些表动手。"""
        return tuple(item for item in self.tables if item.physical_switch == SWITCH_SWITCHED)

    def pending_tables(self) -> tuple[ColdTable, ...]:
        """已登记、待物理切换的表（主库仍是物理表，schema.py 里 DDL 还在）。"""
        return tuple(item for item in self.tables if item.physical_switch == SWITCH_PENDING)


def load_config(config_path: Path = CONFIG_PATH) -> ColdArchiveConfig:
    """读取并校验冷库清单。

    为什么在这里把校验做死：清单是**唯一事实来源**（表集合决定了导出/建视图/删表
    的范围）。清单里有拼写错误而不报错，等于悄悄漏掉一张表 —— 那种事故只能靠
    事后数行数才发现，所以宁可启动即失败。
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"冷库清单必须是 YAML 映射: {config_path}")
    version = raw.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("冷库清单缺少合法的 version")
    default_cutoff = raw.get("source_cutoff")
    if not isinstance(default_cutoff, str) or not default_cutoff:
        raise ValueError("冷库清单缺少 source_cutoff")
    entries = raw.get("tables")
    if not isinstance(entries, list) or not entries:
        raise ValueError("冷库清单 tables 必须是非空列表")

    tables: list[ColdTable] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"冷库清单元素必须是映射: {entry!r}")
        name = entry.get("table")
        if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
            raise ValueError(f"冷库清单表名非法: {name!r}")
        if name in seen:
            raise ValueError(f"冷库清单表名重复: {name}")
        seen.add(name)
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{name} 缺少 reason（为什么可以冷存）")
        cutoff = entry.get("source_cutoff") or default_cutoff
        if not isinstance(cutoff, str) or not cutoff:
            raise ValueError(f"{name} 缺少 source_cutoff")
        consumers_raw = entry.get("consumers") or []
        if not isinstance(consumers_raw, list) or any(
            not isinstance(item, str) for item in consumers_raw
        ):
            raise ValueError(f"{name} 的 consumers 必须是字符串列表")
        switch_state = entry.get("physical_switch", SWITCH_SWITCHED)
        if switch_state not in _ALLOWED_SWITCH_STATES:
            raise ValueError(
                f"{name} 的 physical_switch 只能是 "
                f"{sorted(_ALLOWED_SWITCH_STATES)}: {switch_state!r}"
            )
        tables.append(
            ColdTable(
                table=name,
                reason=reason.strip(),
                source_cutoff=cutoff,
                consumers=tuple(consumers_raw),
                physical_switch=str(switch_state),
            )
        )
    return ColdArchiveConfig(
        version=version,
        source_cutoff=default_cutoff,
        tables=tuple(tables),
        path=config_path,
    )


# ─────────────────────── 目标库句柄 ───────────────────────


def _raw_connect_config() -> dict[str, str]:
    """演练档位（--db）的裸连接配置。

    memory_limit 保守取 4GB：本域每张表最大 35 万行，4GB 足够，且不与 Web
    主进程抢内存。
    """
    return {
        "memory_limit": os.environ.get("VD_DUCKDB_MEMORY_LIMIT", "4GB"),
        "threads": "2",
        "preserve_insertion_order": "false",
    }


class TargetDatabase:
    """目标 DuckDB 句柄。

    为什么套一层：正式档位必须走 ``DuckDBStore``（它带 ``.duckdb.write.lock``、
    维护锁断言、统一连接配置、读连接退避重试），而演练/回归用临时库只能是
    裸连接（临时库不叫 valuedashboard.duckdb，过不了 DatabasePathSet 校验）。
    两个来源在这里收口，上层逻辑（导出/校验/建视图/删表）不关心差异。
    """

    def __init__(self, db_path: Path, store: DuckDBStore | None = None) -> None:
        self.db_path = db_path
        self._store = store

    @property
    def is_formal_store(self) -> bool:
        return self._store is not None

    @property
    def label(self) -> str:
        suffix = "" if self._store is not None else "（演练：--db 临时库）"
        return f"{self.db_path}{suffix}"

    @contextlib.contextmanager
    def reader(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """只读连接：正式库绝不因本次操作产生写入。"""
        if self._store is not None:
            with self._store.read_connection() as connection:
                yield connection
            return
        connection = duckdb.connect(
            str(self.db_path), read_only=True, config=_raw_connect_config()
        )
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def writer(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """单事务写连接：要么整体生效，要么整体回滚。"""
        if self._store is not None:
            # DuckDBStore.transaction() 自带写锁 + 维护锁断言 + begin/commit/rollback
            with self._store.transaction() as connection:
                yield connection
            return
        connection = duckdb.connect(str(self.db_path), config=_raw_connect_config())
        committed = False
        try:
            connection.begin()
            yield connection
            connection.commit()
            committed = True
        finally:
            if not committed:
                with contextlib.suppress(Exception):
                    connection.rollback()
            connection.close()


def resolve_target_database(explicit_db: str | None = None) -> TargetDatabase:
    """解析目标库。

    - 不给 ``--db``：只能解析到**正式档位**（``require_formal_maintenance_paths()``
      会校验 VD_ENV/VD_FORMAL_ACK/路径三元组），并返回带写锁的 DuckDBStore。
    - 给 ``--db``：只允许 ``VD_ENV=test|staging``，且不允许指向项目 ``data/`` 下
      的任何文件。这条口子只为"在临时库上演练/回归"，永远不能拿来绕正式库门禁。
    """
    if explicit_db is None:
        paths: DatabasePathSet = require_formal_maintenance_paths()
        return TargetDatabase(paths.duckdb_path, DuckDBStore(paths=paths))

    env = os.environ.get("VD_ENV", "")
    if env not in {"test", "staging"}:
        raise PathIsolationError(
            "--db 只允许在 VD_ENV=test/staging 下使用（演练/回归）；"
            "正式库请省略 --db，由 require_formal_maintenance_paths() 解析"
        )
    candidate = Path(explicit_db).expanduser().resolve()
    if _is_inside(candidate, data_root()):
        raise PathIsolationError(f"--db 不允许指向项目 data/ 下的文件: {candidate}")
    if not candidate.is_file():
        raise PathIsolationError(f"--db 目标不存在: {candidate}")
    return TargetDatabase(candidate)


def _is_inside(child: Path, parent: Path) -> bool:
    """大小写不敏感的"是否位于目录内"判断（Windows 路径大小写不固定）。"""
    child_str = os.path.normcase(str(child.resolve())).rstrip("\\/")
    parent_str = os.path.normcase(str(parent.resolve())).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


# ─────────────────────── SQL 小工具 ───────────────────────


def _ident(name: str) -> str:
    """双引号标识符（配置已用正则限死，这里仍显式转义）。"""
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    """单引号字符串字面量。路径统一用正斜杠：DuckDB 两种都认，但正斜杠可读性更好。"""
    return "'" + value.replace("'", "''") + "'"


def _path_literal(path: Path) -> str:
    return _sql_literal(path.as_posix())


def object_type(connection: Any, name: str) -> str | None:
    """返回 'table' / 'view' / None（不存在）。"""
    row = connection.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = ?",
        [name],
    ).fetchone()
    if row is None:
        return None
    return "view" if str(row[0]).upper() == "VIEW" else "table"


def table_columns(connection: Any, name: str) -> list[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [name],
    ).fetchall()
    return [str(row[0]) for row in rows]


def table_rows(connection: Any, name: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) FROM {_ident(name)}").fetchone()
    return int(row[0]) if row else 0


def _date_range(connection: Any, name: str) -> tuple[str | None, str | None]:
    """取该表的日期上/下界，写入 manifest 供人快速判断冷库内容覆盖期。"""
    dated = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? AND data_type = 'DATE' "
        "ORDER BY ordinal_position",
        [name],
    ).fetchall()
    date_columns = [str(row[0]) for row in dated]
    if not date_columns:
        return None, None
    column = next(
        (candidate for candidate in _DATE_COLUMN_PREFERENCE if candidate in date_columns),
        date_columns[0],
    )
    row = connection.execute(
        f"SELECT CAST(MIN({_ident(column)}) AS VARCHAR), CAST(MAX({_ident(column)}) AS VARCHAR) "
        f"FROM {_ident(name)}"
    ).fetchone()
    if row is None:
        return None, None
    return (row[0], row[1])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"期望 JSON 对象: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """原子写：先写临时文件再改名，避免留下半个 manifest。"""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.replace(temporary, path)


def _assert_outside_data_root(path: Path, what: str) -> None:
    """正式库所在目录是只读区：任何输出/写入都不许落进 data/。"""
    if _is_inside(path, data_root()):
        raise PathIsolationError(f"{what}不得位于项目 data/ 下: {path}")


# ─────────────────────────── 计划 ───────────────────────────


@dataclass(frozen=True)
class PlanRow:
    """--plan 的一行体检结果。"""

    table: str
    kind: str
    rows: int
    columns: int
    min_date: str | None
    max_date: str | None
    estimated_bytes: int
    measured_bytes: int | None
    consumers: tuple[str, ...]
    reason: str

    @property
    def budget_bytes(self) -> int:
        return self.measured_bytes if self.measured_bytes is not None else self.estimated_bytes


def plan_tables(
    target: TargetDatabase,
    tables: Sequence[ColdTable],
    *,
    measure: bool = False,
) -> list[PlanRow]:
    """只读体检：行数/列数/日期范围/预算体积。

    measure=True 时把每张表 COPY 到系统临时目录量真实 parquet 体积再删
    （只在临时目录留痕，正式库与项目目录零写入）。
    """
    results: list[PlanRow] = []
    temp_root: Path | None = None
    if measure:
        temp_root = Path(tempfile.mkdtemp(prefix="vd-cold-measure-"))
        _assert_outside_data_root(temp_root, "体积测量临时目录")
    try:
        with target.reader() as connection:
            for cold in tables:
                kind = object_type(connection, cold.table)
                if kind is None:
                    raise LookupError(f"目标库缺少表: {cold.table}")
                columns = table_columns(connection, cold.table)
                rows = table_rows(connection, cold.table)
                low, high = _date_range(connection, cold.table)
                measured: int | None = None
                if measure and kind == "table" and temp_root is not None:
                    probe = temp_root / f"{cold.table}.parquet"
                    connection.execute(
                        f"COPY (SELECT * FROM {_ident(cold.table)}) TO {_path_literal(probe)} "
                        f"(FORMAT PARQUET, COMPRESSION {PARQUET_COMPRESSION})"
                    )
                    measured = probe.stat().st_size
                    probe.unlink(missing_ok=True)
                results.append(
                    PlanRow(
                        table=cold.table,
                        kind=kind,
                        rows=rows,
                        columns=len(columns),
                        min_date=low,
                        max_date=high,
                        estimated_bytes=rows * ESTIMATED_BYTES_PER_ROW,
                        measured_bytes=measured,
                        consumers=cold.consumers,
                        reason=cold.reason,
                    )
                )
    finally:
        if temp_root is not None:
            with contextlib.suppress(OSError):
                for leftover in temp_root.glob("*"):
                    leftover.unlink(missing_ok=True)
                temp_root.rmdir()
    return results


def _human_bytes(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def format_plan(rows: Sequence[PlanRow], *, config: ColdArchiveConfig) -> str:
    """把体检结果排成人读表格（终端宽度无所谓，宁可信息全）。"""
    lines = [
        f"冷库清单：{config.path.name}（{len(rows)} 张表，源截止 {config.source_cutoff}）",
        "",
        f"{'表名':<30}{'类型':<8}{'行数':>12}{'列数':>6}  {'日期范围':<26}"
        f"{'预算体积':>12}{'体积/行':>10}  消费者",
    ]
    lines.append("-" * 132)
    total_rows = 0
    total_columns = 0
    total_budget = 0
    for row in rows:
        span = (
            f"{row.min_date}..{row.max_date}"
            if row.min_date and row.max_date
            else "-"
        )
        per_row = (
            f"{row.measured_bytes / row.rows:.1f} B"
            if row.measured_bytes is not None and row.rows
            else "-"
        )
        marker = "" if row.kind == "table" else " (已是视图)"
        consumers = ", ".join(row.consumers) if row.consumers else "-"
        lines.append(
            f"{row.table:<30}{row.kind:<8}{row.rows:>12,}{row.columns:>6}  {span:<26}"
            f"{_human_bytes(row.budget_bytes):>12}{per_row:>10}  {consumers}{marker}"
        )
        total_rows += row.rows
        total_columns += row.columns
        total_budget += row.budget_bytes
    lines.append("-" * 132)
    measured = any(row.measured_bytes is not None for row in rows)
    label = "实测体积" if measured else "预算体积(估)"
    lines.append(
        f"{'合计':<30}{'':<8}{total_rows:>12,}{total_columns:>6}  {'':<26}"
        f"{_human_bytes(total_budget):>12}{'':<10}  {label}"
    )
    return "\n".join(lines)


# ─────────────────────────── 导出 ───────────────────────────


def export_package(
    target: TargetDatabase,
    tables: Sequence[ColdTable],
    out_dir: Path,
    *,
    compression: str = PARQUET_COMPRESSION,
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    """逐表 COPY 到 parquet（ZSTD）+ 原子改名，产出 manifest。

    只读正式库：COPY 的目标是**外部文件**，DuckDB 不会因为 COPY 写库。
    """
    if compression not in _ALLOWED_COMPRESSION:
        raise ValueError(f"不支持的压缩格式: {compression}")
    out_dir = out_dir.resolve()
    _assert_outside_data_root(out_dir, "冷库输出目录")
    if (out_dir / MANIFEST_NAME).exists():
        raise FileExistsError(f"输出目录已有 manifest，拒绝覆盖: {out_dir / MANIFEST_NAME}")
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("[export] 目标库（只读）：%s", target.label)
    logger.info("[export] 输出目录：%s（%s）", out_dir, compression)

    entries: dict[str, dict[str, Any]] = {}
    with target.reader() as connection:
        for cold in tables:
            kind = object_type(connection, cold.table)
            if kind != "table":
                raise RuntimeError(
                    f"{cold.table} 在主库里不是物理表（{kind}）——"
                    f"冷库导出只接受物理表，避免把已是视图的数据再导一遍"
                )
            entries[cold.table] = _export_one(connection, cold, out_dir, compression)
            logger.info(
                "[export] %-30s rows=%9d size=%10s",
                cold.table,
                entries[cold.table]["rows"],
                _human_bytes(entries[cold.table]["size_bytes"]),
            )

    manifest = {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "source_db": str(target.db_path),
        "source_cutoff": _shared_cutoff(tables),
        "compression": compression,
        "config": _display_path(config_path),
        "tables": entries,
        # 反查索引：某个消费者到底依赖哪些冷表（删消费者/改冷库时先查这里）
        "consumer_index": _consumer_index(tables),
    }
    _write_json(out_dir / MANIFEST_NAME, manifest)
    total_rows = sum(entry["rows"] for entry in entries.values())
    total_bytes = sum(entry["size_bytes"] for entry in entries.values())
    logger.info(
        "[export] 完成：%d 张表 / %s 行 / %s → %s",
        len(entries),
        f"{total_rows:,}",
        _human_bytes(total_bytes),
        out_dir / MANIFEST_NAME,
    )
    return manifest


def _export_one(
    connection: Any, cold: ColdTable, out_dir: Path, compression: str
) -> dict[str, Any]:
    columns = table_columns(connection, cold.table)
    rows = table_rows(connection, cold.table)
    low, high = _date_range(connection, cold.table)
    destination = out_dir / f"{cold.table}.parquet"
    temporary = out_dir / f".{cold.table}.{uuid.uuid4().hex}.tmp"
    try:
        connection.execute(
            f"COPY (SELECT * FROM {_ident(cold.table)}) TO {_path_literal(temporary)} "
            f"(FORMAT PARQUET, COMPRESSION {compression})"
        )
        # 原子改名：中途失败不会留下"看起来完整"的半个 parquet
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "file": destination.name,
        "rows": rows,
        "size_bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "min_date": low,
        "max_date": high,
        "columns": columns,
        "column_count": len(columns),
        "reason": cold.reason,
        "source_cutoff": cold.source_cutoff,
        "consumers": list(cold.consumers),
    }


def _display_path(path: Path) -> str:
    """manifest 里记录路径：仓库内用相对路径（可读、不泄漏本机绝对路径），其余原样。"""
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _shared_cutoff(tables: Sequence[ColdTable]) -> str:
    cutoffs = {cold.source_cutoff for cold in tables}
    return cutoffs.pop() if len(cutoffs) == 1 else "mixed"


def _consumer_index(tables: Sequence[ColdTable]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for cold in tables:
        for consumer in cold.consumers:
            index.setdefault(consumer, []).append(cold.table)
    return {consumer: sorted(names) for consumer, names in sorted(index.items())}


# ─────────────────────────── 校验 ───────────────────────────


@dataclass
class VerifyReport:
    """--verify 结果。ok=False 时调用方必须返回非 0 退出码。"""

    ok: bool
    pkg_dir: Path
    errors: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)

    def error_text(self) -> str:
        return "\n".join(f"  - {error}" for error in self.errors)


def verify_package(pkg_dir: Path, *, write_receipt: bool = True) -> VerifyReport:
    """重读 parquet，逐表校验 rows / 列（数与名）/ sha256 是否与 manifest 一致。

    为什么列要比"名字"而不只是"个数"：列数相同但列名错位（例如 SELECT * 时
    列顺序变了）用个数查不出来，而错位会让所有下游读数静默算错口径。
    """
    pkg_dir = pkg_dir.resolve()
    manifest_path = pkg_dir / MANIFEST_NAME
    report = VerifyReport(ok=False, pkg_dir=pkg_dir)
    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report.errors.append(f"manifest 不可读: {error}")
        return report
    entries = manifest.get("tables")
    if manifest.get("format_version") != FORMAT_VERSION or not isinstance(entries, dict):
        report.errors.append("manifest 格式不合法（format_version / tables）")
        return report
    if not entries:
        report.errors.append("manifest 未登记任何表")
        return report

    connection = duckdb.connect()  # 内存库：校验不碰任何正式库文件
    try:
        for name, entry in sorted(entries.items()):
            _verify_one(connection, pkg_dir, name, entry, report)
    finally:
        connection.close()

    if not report.errors:
        report.ok = True
        if write_receipt:
            try:
                _write_json(
                    pkg_dir / VERIFIED_NAME,
                    {
                        "format_version": FORMAT_VERSION,
                        "manifest_sha256": _sha256(manifest_path),
                        "verified_at": datetime.now(UTC).isoformat(),
                        "tables": report.rows,
                    },
                )
            except OSError as error:
                # 冷库包可能在只读盘/只读目录：校验结论本身仍有效，只是留不下回执。
                # --views / --drop 需要回执，那时会明确失败（见 _require_valid_package）。
                logger.warning("[verify] 回执写入失败（不影响本次校验结论）：%s", error)
    return report


def _verify_one(
    connection: Any,
    pkg_dir: Path,
    name: str,
    entry: Any,
    report: VerifyReport,
) -> None:
    if not isinstance(entry, dict):
        report.errors.append(f"{name}: manifest 条目不是对象")
        return
    filename = entry.get("file")
    if not isinstance(filename, str) or not filename:
        report.errors.append(f"{name}: manifest 缺少 file")
        return
    path = (pkg_dir / filename).resolve()
    if not _is_inside(path, pkg_dir):
        report.errors.append(f"{name}: 文件逃逸出包目录（{filename}）")
        return
    if not path.is_file():
        report.errors.append(f"{name}: parquet 缺失（{filename}）")
        return
    size = path.stat().st_size
    if size != entry.get("size_bytes"):
        report.errors.append(
            f"{name}: 文件大小 {size} ≠ manifest {entry.get('size_bytes')}"
        )
    digest = _sha256(path)
    if digest != entry.get("sha256"):
        report.errors.append(f"{name}: sha256 不一致（{digest[:12]}… ≠ {str(entry.get('sha256'))[:12]}…）")
    try:
        cursor = connection.execute(
            f"SELECT * FROM read_parquet({_sql_literal(path.as_posix())}) LIMIT 0"
        )
        actual_columns = [str(desc[0]) for desc in cursor.description]
        rows = connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_sql_literal(path.as_posix())})"
        ).fetchone()[0]
    except Exception as error:  # noqa: BLE001 - parquet 损坏要计入失败而不是崩溃
        report.errors.append(f"{name}: parquet 不可读（{error}）")
        return
    report.rows[name] = int(rows)
    if int(rows) != entry.get("rows"):
        report.errors.append(f"{name}: 行数 {rows} ≠ manifest {entry.get('rows')}")
    expected_columns = entry.get("columns")
    if isinstance(expected_columns, list):
        if actual_columns != expected_columns:
            report.errors.append(
                f"{name}: 列不一致（实际 {len(actual_columns)} 列 ≠ manifest {len(expected_columns)} 列）"
            )
    elif int(len(actual_columns)) != entry.get("column_count"):
        report.errors.append(f"{name}: 列数 {len(actual_columns)} ≠ manifest {entry.get('column_count')}")


def _require_valid_package(pkg_dir: Path) -> tuple[VerifyReport, dict[str, Any]]:
    """校验通过才继续；顺带返回 manifest（回执随校验一并刷新）。

    ``--views`` / ``--drop`` 都只认"校验通过 + 回执与 manifest 哈希一致"这套组合：
    回执是"这份 parquet 在当前内容上被完整核过"的证据，没有它就不许动主库。
    """
    pkg_dir = pkg_dir.resolve()
    report = verify_package(pkg_dir)
    if not report.ok:
        return report, {}
    manifest_path = pkg_dir / MANIFEST_NAME
    manifest = _read_json(manifest_path)
    try:
        receipt = _read_json(pkg_dir / VERIFIED_NAME)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report.ok = False
        report.errors.append(f"校验回执不可用（{error}）；请确认冷库包目录可写后重试")
        return report, {}
    if receipt.get("manifest_sha256") != _sha256(manifest_path):
        report.ok = False
        report.errors.append("校验回执与 manifest 不匹配（manifest 被改动过）")
        return report, {}
    return report, manifest


# ─────────────────────── 建视图 / 删物理表 ───────────────────────


def create_views(
    target: TargetDatabase,
    pkg_dir: Path,
    names: Sequence[str],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """把每张表换成同名视图（物理表先停车到 <表名>__cold_parked）。

    整个操作在**一个事务**里：任何一张表失败 → 全部回滚，主库不会出现半冷半热。
    """
    pkg_dir = pkg_dir.resolve()
    actions: list[dict[str, Any]] = []
    with target.writer() as connection:
        for name in names:
            entry = manifest["tables"][name]
            parquet = (pkg_dir / str(entry["file"])).resolve()
            if not parquet.is_file():
                raise FileNotFoundError(f"{name}: parquet 不存在: {parquet}")
            parked: str | None = None
            if object_type(connection, name) == "table":
                parked = f"{name}{PARKED_SUFFIX}"
                connection.execute(f"ALTER TABLE {_ident(name)} RENAME TO {_ident(parked)}")
            connection.execute(
                f"CREATE OR REPLACE VIEW {_ident(name)} AS "
                f"SELECT * FROM read_parquet({_path_literal(parquet)})"
            )
            rows = table_rows(connection, name)
            if rows != entry["rows"]:
                raise RuntimeError(
                    f"{name}: 视图行数 {rows} ≠ manifest {entry['rows']}（事务回滚，物理表原样保留）"
                )
            actions.append(
                {"table": name, "parked": parked, "rows": rows, "parquet": parquet.as_posix()}
            )
            logger.info(
                "[views] %-30s → 视图（%d 行），物理表%s",
                name,
                rows,
                f"停车为 {parked}" if parked else "此前已不在主库",
            )
    return actions


def drop_parked_tables(
    target: TargetDatabase,
    names: Sequence[str],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """删除主库里的物理表（= 上一步停车的 <表名>__cold_parked）。

    门槛（全部满足才动手）：
    1. 同名视图存在，且从视图读出来的行数 == manifest 行数（视图真的能读到 parquet）；
    2. manifest 校验已通过（调用方先跑 verify）；
    3. 显式 --yes；
    4. 无写锁（调用方已检查）。
    删除前把行数写进日志（事后对账的唯一依据）。
    """
    actions: list[dict[str, Any]] = []
    with target.writer() as connection:
        for name in names:
            entry = manifest["tables"][name]
            kind = object_type(connection, name)
            if kind != "view":
                raise RuntimeError(
                    f"{name}: 主库里没有同名视图（当前是 {kind or '不存在'}）——先跑 --views"
                )
            view_rows = table_rows(connection, name)
            if view_rows != entry["rows"]:
                raise RuntimeError(
                    f"{name}: 视图行数 {view_rows} ≠ manifest {entry['rows']}，拒绝删物理表"
                )
            parked = f"{name}{PARKED_SUFFIX}"
            if object_type(connection, parked) != "table":
                logger.info("[drop] %-30s 无停车表可删（已是视图，物理表已清理）", name)
                actions.append({"table": name, "dropped": False, "rows": 0})
                continue
            parked_rows = table_rows(connection, parked)
            logger.info(
                "[drop] %-30s 待删物理表 %s：rows=%d（视图 rows=%d）",
                name,
                parked,
                parked_rows,
                view_rows,
            )
            connection.execute(f"DROP TABLE {_ident(parked)}")
            actions.append({"table": name, "dropped": True, "rows": parked_rows})
    return actions


# ─────────────────── schema.py 待删 DDL 清单 ───────────────────

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)")
_CREATE_INDEX_RE = re.compile(r"CREATE INDEX IF NOT EXISTS\s+(\w+)")
_ALTER_TABLE_RE = re.compile(r"ALTER TABLE\s+(\w+)")
_ON_TABLE_RE = re.compile(r"ON\s+(\w+)")


@dataclass(frozen=True)
class DdlHint:
    """schema.py 中与被冷存表相关的 DDL 语句位置。"""

    table: str
    kind: str  # 'table' | 'index' | 'alter'
    line: int
    end_line: int | None
    text: str


def schema_ddl_hints(
    names: Sequence[str], schema_path: Path = SCHEMA_PATH
) -> list[DdlHint]:
    """扫描 schema.py，列出与这些表相关的 CREATE TABLE / CREATE INDEX / ALTER TABLE。

    为什么要动态扫而不是写死行号：行号会随别人改 schema.py 漂移，写死的清单
    一周后就骗人。扫描 + 打印真实行号，任何人执行 --views/--drop 时看到的都是
    当下该删的位置。
    """
    wanted = set(names)
    try:
        text = schema_path.read_text(encoding="utf-8")
    except OSError:
        return []
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def line_of(offset: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    hints: list[DdlHint] = []
    for match in _CREATE_TABLE_RE.finditer(text):
        table = match.group(1)
        if table not in wanted:
            continue
        start = line_of(match.start())
        end = _block_end_line(text, match.end(), line_of)
        hints.append(
            DdlHint(
                table=table,
                kind="table",
                line=start,
                end_line=end,
                text=f"CREATE TABLE IF NOT EXISTS {table} ( … );",
            )
        )
    for match in _CREATE_INDEX_RE.finditer(text):
        window = text[match.end() : match.end() + 240]
        on_match = _ON_TABLE_RE.search(window)
        if on_match is None or on_match.group(1) not in wanted:
            continue
        hints.append(
            DdlHint(
                table=on_match.group(1),
                kind="index",
                line=line_of(match.start()),
                end_line=None,
                text=f"CREATE INDEX IF NOT EXISTS {match.group(1)} ON {on_match.group(1)} …",
            )
        )
    for match in _ALTER_TABLE_RE.finditer(text):
        table = match.group(1)
        if table not in wanted:
            continue
        line = line_of(match.start())
        source = text[line_starts[line - 1] : line_starts[line]].strip()
        hints.append(
            DdlHint(table=table, kind="alter", line=line, end_line=None, text=source[:120])
        )
    return sorted(hints, key=lambda hint: (hint.line, hint.kind))


def _block_end_line(text: str, start_offset: int, line_of: Any) -> int | None:
    """找 CREATE TABLE 块的收尾 `);` 行号（用于给出可整块删除的行范围）。"""
    depth = 0
    index = text.find("(", start_offset)
    if index == -1:
        return None
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return line_of(index)
        index += 1
    return None


def format_ddl_checklist(hints: Sequence[DdlHint], *, config: ColdArchiveConfig) -> str:
    """打印"还需要从 schema.py 删除对应 DDL 块"的清单。

    这条清单不是礼貌提醒：CREATE INDEX / ALTER TABLE 打在视图上会直接抛错，
    每次启动的 init_duckdb_schema() 都会跑一遍，不删就起不来。

    按**行号从大到小**排列：人工删块时从文件底部往上删，行号不会因为前面的
    删除而漂移（这是最容易做错、也最容易重做一遍的地方）。
    """
    by_table: dict[str, list[DdlHint]] = {}
    for hint in hints:
        by_table.setdefault(hint.table, []).append(hint)
    ordered = sorted(
        by_table.items(),
        key=lambda item: max(hint.line for hint in item[1]),
        reverse=True,
    )
    lines = [
        "=" * 108,
        " 还需要从 app/core/storage/schema.py 删除以下 DDL 块（否则重启会被重建/启动即报错）",
        " 依据：这些表已转为视图（read_parquet）。CREATE INDEX / ALTER TABLE 打在视图上会抛错，",
        "       CREATE TABLE IF NOT EXISTS 会静默跳过但掩盖问题。",
        " 顺序：按行号从大到小 —— 请从文件底部往上删，避免行号漂移需要重做。",
        "=" * 108,
    ]
    for table, table_hints in ordered:
        spans = []
        for hint in sorted(table_hints, key=lambda item: item.line):
            if hint.kind == "table" and hint.end_line:
                spans.append(f"L{hint.line}-L{hint.end_line} 表 DDL")
            elif hint.kind == "index":
                spans.append(f"L{hint.line} 索引")
            else:
                spans.append(f"L{hint.line} ALTER")
        lines.append(f"  {table:<30} {' / '.join(spans)}")
    for cold in config.tables:
        if cold.table not in by_table:
            lines.append(f"  {cold.table:<30} （schema.py 中未找到对应 DDL，请人工确认）")
    lines.append("-" * 108)
    lines.append(
        f"  合计 {len(by_table)} 张表 / {len(hints)} 处语句"
        f"（表 DDL {sum(1 for h in hints if h.kind == 'table')} / "
        f"索引 {sum(1 for h in hints if h.kind == 'index')} / "
        f"ALTER {sum(1 for h in hints if h.kind == 'alter')}）"
    )
    lines.append("=" * 108)
    return "\n".join(lines)


# ─────────────────────────── CLI ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cold_archive_split.py",
        description="CSMAR 历史归档域冷热分离（plan / export / verify / views / drop）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="只读体检：表/行数/预算体积（不写正式库）")
    mode.add_argument("--export", action="store_true", help="导出冷库 parquet + manifest（正式库只读）")
    mode.add_argument("--verify", action="store_true", help="重读 parquet 校验 rows/columns/sha256")
    mode.add_argument("--views", action="store_true", help="建视图（物理表停车，需 --yes）")
    mode.add_argument("--drop", action="store_true", help="删物理表（默认永不执行，需 --yes）")
    mode.add_argument("--print-ddl", action="store_true", help="只打印 schema.py 待删 DDL 清单")

    parser.add_argument("--out", type=Path, default=None, help="导出目录（必须在 data/ 之外）")
    parser.add_argument("--pkg", type=Path, default=None, help="冷库包目录（含 manifest.json）")
    parser.add_argument(
        "--db",
        default=None,
        help="目标库路径（仅 VD_ENV=test/staging 演练用；正式库请省略）",
    )
    parser.add_argument("--measure", action="store_true", help="--plan 时实测体积（临时目录，测完即删）")
    parser.add_argument("--yes", action="store_true", help="确认执行写操作（--views / --drop 必须）")
    parser.add_argument("--verbose", action="store_true", help="打印每张表的 reason")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="冷库清单 YAML")
    return parser


def _require_pkg(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Path:
    if args.pkg is None:
        parser.error(f"{_mode_label(args)} 需要 --pkg <冷库包目录>")
    pkg = Path(args.pkg).expanduser().resolve()
    if not (pkg / MANIFEST_NAME).is_file():
        parser.error(f"冷库包缺少 manifest.json: {pkg}")
    return pkg


def _mode_label(args: argparse.Namespace) -> str:
    for flag in ("plan", "export", "verify", "views", "drop", "print_ddl"):
        if getattr(args, flag, False):
            return "--" + flag.replace("_", "-")
    return "--?"


def _guard_write_window(target: TargetDatabase) -> int:
    """写前必须无锁：否则要么读到半成品，要么把 DDL 挤进别人的写事务。"""
    if any_write_lock_active(target.db_path):
        logger.error(
            "检测到写锁生效（.value-dashboard.update.lock / .duckdb.write.lock）：%s\n"
            "  写窗口内的 DDL 会与正在跑的更新/维护交错，已拒绝。等更新结束后重试。",
            target.db_path,
        )
        return EXIT_LOCKED
    return EXIT_OK


def _guard_yes(args: argparse.Namespace) -> int:
    if not args.yes:
        logger.error(
            "%s 是写操作，必须显式加 --yes 才会执行（默认永不执行）。", _mode_label(args)
        )
        return EXIT_REFUSED
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config).expanduser().resolve())
    except (OSError, ValueError) as error:
        logger.error("冷库清单不可用：%s", error)
        return EXIT_ERROR
    names = [cold.table for cold in config.tables]

    if args.print_ddl:
        print(format_ddl_checklist(schema_ddl_hints(names), config=config))
        return EXIT_OK

    # --verify 只读 parquet 文件，不需要碰数据库（除非用户显式给了 --db，也照样只用文件）
    if args.verify:
        pkg = _require_pkg(args, parser)
        report = verify_package(pkg)
        if not report.ok:
            logger.error("[verify] 失败（%d 处不一致）：\n%s", len(report.errors), report.error_text())
            return EXIT_VERIFY_FAILED
        logger.info(
            "[verify] 通过：%d 张表 / %s 行 / manifest 哈希已写入 %s",
            len(report.rows),
            f"{sum(report.rows.values()):,}",
            pkg / VERIFIED_NAME,
        )
        return EXIT_OK

    try:
        target = resolve_target_database(args.db)
    except PathIsolationError as error:
        logger.error("目标库解析失败：%s", error)
        return EXIT_REFUSED

    if args.plan:
        logger.info("[plan] 目标库（只读）：%s", target.label)
        try:
            rows = plan_tables(target, config.tables, measure=args.measure)
        except (LookupError, duckdb.Error) as error:
            logger.error("[plan] 无法完成体检：%s", error)
            return EXIT_ERROR
        print(format_plan(rows, config=config))
        if args.verbose:
            for row in rows:
                print(f"  {row.table}: {row.reason}")
        print()
        print(format_ddl_checklist(schema_ddl_hints(names), config=config))
        return EXIT_OK

    if args.export:
        if args.out is None:
            parser.error(
                "--export 需要 --out <目录>（正式冷库建议 D:/vd-cold-archive/csmar-<日期>；"
                "演练可用 .planning/cold-archive-csmar-<日期>）"
            )
        out_dir = Path(args.out).expanduser().resolve()
        locked = _guard_write_window(target)
        if locked != EXIT_OK:
            return locked
        try:
            export_package(
                target,
                config.tables,
                out_dir,
                config_path=Path(args.config).expanduser().resolve(),
            )
        except (OSError, RuntimeError, ValueError, duckdb.Error) as error:
            logger.error("[export] 失败：%s", error)
            return EXIT_ERROR
        logger.info("[export] 下一步：--verify --pkg %s", out_dir)
        return EXIT_OK

    if args.views:
        pkg = _require_pkg(args, parser)
        guarded = _guard_yes(args)
        if guarded != EXIT_OK:
            return guarded
        locked = _guard_write_window(target)
        if locked != EXIT_OK:
            return locked
        report, manifest = _require_valid_package(pkg)
        if not report.ok:
            logger.error("[views] 冷库校验未通过，拒绝建视图：\n%s", report.error_text())
            return EXIT_VERIFY_FAILED
        try:
            actions = create_views(target, pkg, sorted(manifest["tables"]), manifest)
        except (OSError, RuntimeError, duckdb.Error) as error:
            logger.error("[views] 失败（事务已回滚，主库未变）：%s", error)
            return EXIT_ERROR
        logger.info("[views] 完成：%d 张表已转为视图", len(actions))
        print()
        print(format_ddl_checklist(schema_ddl_hints(names), config=config))
        logger.info("[views] 下一步：确认服务可正常启动后，再跑 --drop --pkg %s --yes", pkg)
        return EXIT_OK

    if args.drop:
        pkg = _require_pkg(args, parser)
        guarded = _guard_yes(args)
        if guarded != EXIT_OK:
            return guarded
        locked = _guard_write_window(target)
        if locked != EXIT_OK:
            return locked
        report, manifest = _require_valid_package(pkg)
        if not report.ok:
            logger.error("[drop] 冷库校验未通过，拒绝删物理表：\n%s", report.error_text())
            return EXIT_VERIFY_FAILED
        try:
            actions = drop_parked_tables(target, sorted(manifest["tables"]), manifest)
        except (RuntimeError, duckdb.Error) as error:
            logger.error("[drop] 失败（事务已回滚，物理表未删）：%s", error)
            return EXIT_ERROR
        dropped = [action for action in actions if action["dropped"]]
        logger.info(
            "[drop] 完成：删除物理表 %d 张 / 共 %s 行",
            len(dropped),
            f"{sum(action['rows'] for action in dropped):,}",
        )
        print()
        print(format_ddl_checklist(schema_ddl_hints(names), config=config))
        return EXIT_OK

    parser.error("未指定模式")
    return EXIT_USAGE  # pragma: no cover - argparse 已 SystemExit


if __name__ == "__main__":
    raise SystemExit(main())
