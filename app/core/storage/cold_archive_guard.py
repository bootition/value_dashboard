"""冷库表写入护栏（2026-11-15，冷热分库物理切换配套）。

为什么需要这个模块
------------------
16 张 CSMAR 静态归档表已导出为 Parquet 冷库（``config/cold_archive.yaml``），
主库里的同名对象将变成 ``read_parquet`` 视图。视图**不可写**：

- ``INSERT INTO <view>`` → Catalog Error；
- ``DELETE FROM <view>`` / ``UPDATE <view>`` 同样失败。

历史导入脚本一旦在切换后重跑，会在写过一半时才发现写不进去（备份已做、
batch 已建），既浪费维护窗口又制造半成品证据。所以写路径必须在**动手前**
显式检查目标对象类型：只要目标是视图，就立即以可行动的报错退出，而不是让
DuckDB 在 SQL 层丢一句 Catalog Error。

报错文案必须包含的两件事
------------------------
1. 事实：``该表已迁冷库``（否则维护者会以为是视图权限/拼写问题）；
2. 动作：``先 restore 再导入``（把 Parquet 恢复成物理表后再重跑导入）。

判定口径
--------
用 ``information_schema.tables.table_type``（DuckDB 对 BASE TABLE / VIEW 都登记）
或 ``duckdb_views()`` 均可；这里统一走 information_schema，避免依赖版本差异。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: 报错里必须出现的动作提示（测试与运维检索都依赖这一句）。
COLD_ARCHIVE_REMEDY = "该表已迁冷库，先 restore 再导入"

#: 冷库视图写入被拒时的进程退出码（与更新锁 busy=3 / 门禁拒绝=5 不冲突）。
EXIT_COLD_ARCHIVE_VIEW = 6


class ColdArchiveTableError(RuntimeError):
    """目标表已是 Parquet 冷库视图，写入必然失败。"""


def table_object_type(connection: Any, table: str, schema: str = "main") -> str | None:
    """返回 ``'table'`` / ``'view'`` / ``None``（不存在）。

    为什么不用 ``SELECT * FROM duckdb_tables()``：视图不在 ``duckdb_tables()`` 里，
    而 ``duckdb_views()`` 又不含物理表；``information_schema.tables`` 一次覆盖两类，
    且列名是 SQL 标准，跨 DuckDB 版本稳定。
    """
    row = connection.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()
    if row is None:
        return None
    value = str(row[0]).upper()
    return "view" if value in {"VIEW", "VIEW TABLE"} else "table"


def find_cold_archive_views(
    connection: Any, tables: Iterable[str], schema: str = "main"
) -> list[str]:
    """返回这些目标表中**当前是视图**的表名（保持传入顺序、去重）。"""
    seen: set[str] = set()
    views: list[str] = []
    for table in tables:
        if table in seen:
            continue
        seen.add(table)
        if table_object_type(connection, table, schema) == "view":
            views.append(table)
    return views


def ensure_writable_tables(
    connection: Any, tables: Iterable[str], schema: str = "main"
) -> None:
    """写前护栏：任一目标是视图就抛 ``ColdArchiveTableError``（文案可直接给用户）。

    不检查「表不存在」：那是另一种故障（先 ``vd data init``），让原有 SQL 报错
    即可，避免把一个缺失表误报成冷库问题。
    """
    views = find_cold_archive_views(connection, tables, schema)
    if not views:
        return
    names = "、".join(views)
    raise ColdArchiveTableError(
        f"{names} 已是 Parquet 冷库视图：{COLD_ARCHIVE_REMEDY}。"
        "冷库清单见 config/cold_archive.yaml；"
        "恢复方式见 scripts/cold_archive_switch.py 的说明，"
        "把 Parquet 恢复成物理表后再重跑本脚本。"
    )


def guard_writable_or_exit(
    store: Any,
    tables: Iterable[str],
    *,
    logger: Any | None = None,
    schema: str = "main",
) -> None:
    """脚本入口的便捷护栏：查一次目标对象类型，是视图就记录并以专用退出码退出。

    只供维护脚本在**真正写库前**调用（dry-run 不必调用：读视图是合法的）。
    退出码用 :data:`EXIT_COLD_ARCHIVE_VIEW`（6），让上层脚本/运维能区分
    「数据源问题」与「冷库视图拒绝写入」。
    """
    try:
        with store.read_connection() as connection:
            ensure_writable_tables(connection, tables, schema)
    except ColdArchiveTableError as error:
        if logger is not None:
            logger.error("%s", error)
        raise SystemExit(EXIT_COLD_ARCHIVE_VIEW) from error
