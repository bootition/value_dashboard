"""筛选快照库 —— 写锁窗口内的**第二数据源**（2026-11-20，reports/151）。

## 为什么不是"结果回放"

2026-11-20 第一版修的是"上次成功结果回放"（`read_cache` + `_screening_run_key`），
键里含**规则体 + 基础池 + 列 + strict + 数据指纹**。于是只有"完全相同的请求曾经
成功过"才能回放：
- 用户换个条件 → 新键 → miss；
- 用户第一次在更新窗口里筛 → 从来没错过 → miss。
两种都落到 409。服务日志实证（reports/151 §1）：

```
19:29:17  筛选完成: 590 条, 耗时 1406ms          ← 键 A 跑成功过
19:29:42  启动自动更新子进程...
19:42:33  筛选运行被写锁挡住且无上次成功结果      ← 用户 UI 那次（键 B）→ 失败
```

**回放不是解耦**：它只保证"同一个问题再问一次"能用。真正要的是"更新期间
**任何条件**都能按上一次更新完成后的数据真跑"——那就需要一份**数据**，不是一份
**结果**。

## 这份快照是什么

把筛选真正要读的小表，从主库**物化**到一个独立的只读库文件
（`<run_root>/screening_snapshot.duckdb`），写锁窗口里让引擎连它跑，
**SQL 一个字都不改，只换连接**（`ScreeningEngine(duck=SnapshotStore(...))`）。

## 引擎到底读了哪些表（依据 `engine._build_sql` / `_build_where` 逐条核对）

======================  ==========================================  ============
表                      用途                                         物化
======================  ==========================================  ============
`stock_meta`            基础池（上市/ST/停牌/行业/股本）                全表
`indicator_snapshot`    `s.*`（指标列、均线、估值、股息率利差…）         全表
`balance_sheet`         归一化科目（`total_assets` 等 60 列）           投影
`income_statement`      归一化科目（`revenue` 等 26 列）                投影
`cash_flow`             归一化科目（`cf_from_operating` 等 16 列）      投影
`indicator_ext`         扩展指标域 55 列                              投影
`research_statistics`   `*_stat_<win>y_<method>` 研究统计域             投影
`source_audit`          strict 模式的血缘谓词                           最新一条
`price_daily_raw/qfq`   **筛选从不读**（均线/涨跌幅早已算进             **不物化**
                        `indicator_snapshot`）                        1750 万行
======================  ==========================================  ============

`price_daily_*` 一行都不进快照：`engine.py` 里对它的引用数为 **0**，技术指标
（`ma5/ma10/latest_close/period_return/…`）是 `indicator_snapshot` 的**物理列**。
所以不存在"价格类条件在更新窗口内不可用"这件事。

## 两条铁律

1. **原子**：先写 `*.tmp-<pid>-<uuid>.duckdb` 同目录临时文件，写完 `CHECKPOINT` +
   `close()`，再 `os.replace()` 到正式名。中途失败只删临时文件，旧快照**原样可用**；
   正式名上永远不会出现半个文件。
2. **失败不炸主流程**：`build_screening_snapshot()` 任何异常都只记日志并返回
   `None`，更新收尾/服务启动照常往下走。

## 诚实标注（不可省）

快照不是"现在"的数据。用它跑出来的响应必须带
`served_from="snapshot"` + `as_of`（快照生成时间）+ `stale=True` + 中文 `notice`，
否则用户会把"三天前的完整快照"当成"刚刚的数据"——那比 409 严重得多。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 建快照的进程内互斥：两个线程同时建只会互相抢临时文件/rename，
#: 语义上又完全等价（幂等），串行化是最省事也最不容易错的选择。
_BUILD_LOCK = threading.Lock()

#: 快照文件名。与主库同级（`run_root`），跟主库同卷 → `os.replace` 是原子的。
SNAPSHOT_FILENAME = "screening_snapshot.duckdb"

#: meta 表名（单行）。读它 = 判断快照是否存在/何时建的/由哪个库物化。
META_TABLE = "screening_snapshot_meta"

#: 写锁窗口内响应的 `served_from` 取值。与 `read_cache` 的
#: `"live"` / `"cache"` 同一套字段语义。
SERVED_FROM_SNAPSHOT = "snapshot"

#: 快照过期阈值（秒）。更新成功收尾会重写；服务启动发现超过它就补一次。
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60

#: 读/写连接共用的 DuckDB 配置。同一进程内对同一文件**必须**用同一套配置，
#: 否则 DuckDB 报 "different configuration"（`duckdb_store` 模块顶部实测）。
_CONNECTION_CONFIG: dict[str, str] = {
    "memory_limit": "3GB",
    "threads": "2",
    "preserve_insertion_order": "false",
}

#: 物化的表清单（顺序即建表顺序）。`columns=None` 表示整表复制。
#: 其余条目是"引擎/门禁真正会 SELECT 的列"的投影：物化范围只由**引用面**决定，
#: 不因为"顺手多拷几列"把快照吹成第二个 28GB 库。
_TABLE_SPECS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("stock_meta", None),
    ("indicator_snapshot", None),
)


def snapshot_path(duck: Any) -> Path:
    """快照文件的规范位置：主库所在目录（`run_root`）下的固定名。"""
    return Path(str(duck.db_path)).parent / SNAPSHOT_FILENAME


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _screening_audit_names() -> list[str]:
    """`source_audit` 里筛选可能引用到的 `field_name` 全集。

    唯一真源在 `engine.py` 的字段白名单（S7 §5.1 CB-09 的单一判定入口）：
    这里**只做读取**，绝不抄第二份字段清单。
    """
    from app.core.screening import engine

    names: set[str] = set()
    for fields in engine.STATEMENT_FIELDS.values():
        names |= set(fields)
    names |= set(engine.EXTENDED_COLUMNS)
    names |= set(engine.SNAPSHOT_COLUMNS)
    for key in engine.DERIVED_FIELDS_BY_KEY:
        names.add(key.split(".", 1)[-1])
        for field in engine.derived_input_fields(key):
            names.add(field.split(".", 1)[-1])
    return sorted(names)


#: 物理表名 → `engine.STATEMENT_FIELDS` 的键（引擎里按 balance/income/cashflow 分组）。
_STATEMENT_KEY = {
    "balance_sheet": "balance",
    "income_statement": "income",
    "cash_flow": "cashflow",
}


def _statement_columns(table: str) -> tuple[str, ...]:
    from app.core.screening import engine

    return tuple(sorted(engine.STATEMENT_FIELDS[_STATEMENT_KEY[table]]))


def _extended_columns() -> tuple[str, ...]:
    from app.core.screening import engine

    return tuple(sorted(engine.EXTENDED_COLUMNS))


def _existing_columns(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    except Exception:  # noqa: BLE001 - 取不到列清单就按"整表"处理
        return set()
    return {str(row[0]) for row in rows}


def _table_exists(conn: Any, table: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchall()
    return bool(rows)


def _projection(conn: Any, table: str, wanted: tuple[str, ...]) -> str | None:
    """按主库真实存在的列裁剪投影；缺列不报错，只少拷一列（版本漂移兜底）。"""
    existing = _existing_columns(conn, table)
    if not existing:
        return None
    keep = [column for column in wanted if column in existing]
    if "stock_code" not in keep:
        keep.insert(0, "stock_code")
    if "report_date" not in keep and "report_date" in existing:
        keep.insert(1, "report_date")
    return ", ".join(f'"{column}"' for column in keep)


def _create_statements(conn: Any) -> list[tuple[str, str]]:
    """(目标表名, SELECT 语句)。只None 存在的表才会进入列表。"""
    statements: list[tuple[str, str]] = []
    for table, columns in _TABLE_SPECS:
        if not _table_exists(conn, table):
            continue
        select = "*" if columns is None else _projection(conn, table, columns)
        if select is None:
            continue
        statements.append((table, f'SELECT {select} FROM main."{table}"'))

    for table in ("balance_sheet", "income_statement", "cash_flow"):
        if not _table_exists(conn, table):
            continue
        select = _projection(conn, table, _statement_columns(table))
        if select is None:
            continue
        statements.append((table, f'SELECT {select} FROM main."{table}"'))

    if _table_exists(conn, "indicator_ext"):
        select = _projection(conn, "indicator_ext", _extended_columns())
        if select is not None:
            statements.append(
                ("indicator_ext", f'SELECT {select} FROM main."indicator_ext"')
            )

    if _table_exists(conn, "research_statistics"):
        select = _projection(
            conn,
            "research_statistics",
            ("stock_code", "metric", "window_years", "method", "value", "version"),
        )
        if select is not None:
            statements.append(
                (
                    "research_statistics",
                    f'SELECT {select} FROM main."research_statistics"',
                )
            )

    if _table_exists(conn, "source_audit"):
        names = _screening_audit_names()
        if names:
            placeholders = ", ".join(["?"] * len(names))
            statements.append((
                "source_audit",
                # 只保留引擎 strict 谓词真正会取的那一条：同一
                # (stock_code, field_name, report_date) 下 `ORDER BY fetch_time DESC,
                # id DESC LIMIT 1`。物化 2,100 万行没有意义，谓词只看得见最后一条。
                f"""SELECT stock_code, field_name, report_date, value, source,
                           confidence, fetch_time, id
                    FROM main.source_audit
                    WHERE field_name IN ({placeholders})
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY stock_code, field_name, report_date
                        ORDER BY fetch_time DESC, id DESC
                    ) = 1""",
            ))
    return statements


def build_screening_snapshot(
    duck: Any,
    *,
    dest: Path | None = None,
    reason: str = "manual",
) -> dict[str, Any] | None:
    """把筛选要读的小表物化到独立只读库。**原子 + 幂等 + 失败不影响主流程**。

    实现要点：**不新开主库连接**。用调用方给的那条只读连接 `ATTACH` 空的临时库
    （`READ_WRITE`），再 `CREATE TABLE snap.x AS SELECT ... FROM main.x` ——
    跨库 CTAS 由 DuckDB 自己流式搬运，不需要把 37 万行拉回 Python，
    也不会因为"再开一条主库连接"在写锁窗口里二次失败。

    Args:
        duck: 主库 store（只被 **读**）。
        dest: 目标文件；缺省 `<run_root>/screening_snapshot.duckdb`。
        reason: 落进 meta 的触发来源（`update_finished` / `startup_backfill` / …）。

    Returns:
        成功 → meta dict（含 `built_at` / `row_counts` / `source_duckdb`）；
        失败 → `None`（只记 warning，从不抛）。
    """
    target = Path(dest) if dest is not None else snapshot_path(duck)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(
        f"{target.stem}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}{target.suffix}"
    )
    source_path = str(getattr(duck, "db_path", ""))
    row_counts: dict[str, int] = {}
    with _BUILD_LOCK:
        try:
            with duck.read_connection() as source:
                statements = _create_statements(source)
                if not statements:
                    logger.warning("筛选快照跳过：主库里没有任何筛选表")
                    return None
                source.execute(f"ATTACH '{_quote_path(staging)}' AS snap (READ_WRITE)")
                try:
                    for table, select in statements:
                        params: list[Any] = (
                            _screening_audit_names() if table == "source_audit" else []
                        )
                        source.execute(f'CREATE TABLE snap."{table}" AS {select}', params)
                        count = source.execute(
                            f'SELECT COUNT(*) FROM snap."{table}"'
                        ).fetchone()
                        row_counts[table] = int(count[0]) if count else 0
                    built_at = _now_iso()
                    source.execute(
                        f'CREATE TABLE snap."{META_TABLE}" ('
                        "built_at VARCHAR, built_by VARCHAR, source_duckdb VARCHAR, "
                        "source_mtime DOUBLE, row_counts VARCHAR, snapshot_version INTEGER)"
                    )
                    source_mtime = 0.0
                    try:
                        source_mtime = float(Path(source_path).stat().st_mtime)
                    except OSError:
                        source_mtime = 0.0
                    source.execute(
                        f'INSERT INTO snap."{META_TABLE}" VALUES (?, ?, ?, ?, ?, ?)',
                        [
                            built_at,
                            reason,
                            source_path,
                            source_mtime,
                            json.dumps(row_counts, sort_keys=True),
                            1,
                        ],
                    )
                    # CHECKPOINT 把一切落进主文件：临时名上不会留半个 WAL。
                    source.execute("CHECKPOINT snap")
                finally:
                    try:
                        source.execute("DETACH snap")
                    except Exception:  # noqa: BLE001 - detach 失败不影响后面 rename
                        logger.debug("DETACH 快照临时库失败", exc_info=True)
            # 原子替换：同一卷上的 os.replace 要么换成功、要么旧文件原样在。
            _replace_with_retry(staging, target)
            logger.info(
                "筛选快照已生成: %s (reason=%s, %s)",
                target, reason,
                ", ".join(f"{k}={v}" for k, v in sorted(row_counts.items())),
            )
            return {
                "built_at": built_at,
                "built_by": reason,
                "source_duckdb": source_path,
                "path": str(target),
                "row_counts": row_counts,
            }
        except Exception as error:  # noqa: BLE001 - 快照失败绝不能连累更新/启动
            logger.warning("生成筛选快照失败（旧快照保持可用）: %s", error, exc_info=True)
            return None
        finally:
            # 无论成败：临时文件 + 它的 WAL 都不留在磁盘上（"不留半个文件"）。
            for leftover in (staging, Path(str(staging) + ".wal")):
                try:
                    if leftover.exists():
                        leftover.unlink()
                except OSError:  # pragma: no cover - 极端占用场景
                    logger.debug("清理快照临时文件失败: %s", leftover, exc_info=True)


def _quote_path(path: Path) -> str:
    """DuckDB 的 ATTACH 路径字面量（单引号转义）。"""
    return str(path).replace("'", "''")


def _replace_with_retry(staging: Path, target: Path, *, attempts: int = 5) -> None:
    """`os.replace` + 短退避。

    旧快照可能正被一条读查询打开（Windows 上会 `PermissionError`）。重试几次；
    仍失败就把异常抛给上层 → 旧快照原样保留、临时文件被清掉，**绝不出现半个文件**。
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            os.replace(staging, target)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error


class SnapshotStore:
    """快照库的只读 store（鸭子类型，`ScreeningEngine(duck=...)` 直接用）。

    与 `DuckDBStore` 的 `read_query()` 语义对齐（open-per-query、只读、
    `SELECT` 单语句），但**只**指向快照文件：它不受主库写锁影响，
    这正是"更新期间也能真跑筛选"的物理前提。
    """

    def __init__(self, path: Path, *, info: dict[str, Any] | None = None) -> None:
        self.db_path = Path(path)
        self.info = dict(info or {})

    # 引擎/门禁只依赖 read_query；这里补齐少量被间接用到的属性。
    @property
    def as_of(self) -> str | None:
        return self.info.get("built_at")

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        import duckdb

        try:
            conn = duckdb.connect(
                str(self.db_path), read_only=True, config=dict(_CONNECTION_CONFIG)
            )
        except Exception as error:  # noqa: BLE001 - 读不到=没有快照，交给上层 409
            from app.core.storage.duckdb_store import DuckDBReadLockedError

            raise DuckDBReadLockedError(
                f"screening snapshot unavailable at {self.db_path}: {error}"
            ) from error
        try:
            cursor = conn.execute(sql, params or [])
            columns = [description[0] for description in cursor.description or ()]
            rows = cursor.fetchall()
            return [dict(zip(columns, row, strict=True)) for row in rows]
        finally:
            conn.close()

    def close(self) -> None:  # 与 DuckDBStore.close() 同形（无长期句柄）
        return None


def read_snapshot_info(path: Path) -> dict[str, Any] | None:
    """读快照的 meta 行；文件缺失/损坏/没有 meta 表 → `None`。"""
    import duckdb

    if not path.exists():
        return None
    try:
        conn = duckdb.connect(
            str(path), read_only=True, config=dict(_CONNECTION_CONFIG)
        )
    except Exception:  # noqa: BLE001 - 打不开就当没有快照
        logger.warning("筛选快照无法打开: %s", path, exc_info=True)
        return None
    try:
        rows = conn.execute(
            f'SELECT built_at, built_by, source_duckdb, source_mtime, row_counts '
            f'FROM "{META_TABLE}" LIMIT 1'
        ).fetchall()
    except Exception:  # noqa: BLE001 - 没有 meta 表 = 不是本模块产的快照
        logger.warning("筛选快照缺少 meta 表（按不可用处理）: %s", path)
        return None
    finally:
        conn.close()
    if not rows:
        return None
    built_at, built_by, source_duckdb, source_mtime, row_counts = rows[0]
    try:
        counts = json.loads(row_counts or "{}")
    except (TypeError, ValueError):
        counts = {}
    return {
        "path": str(path),
        "built_at": str(built_at),
        "built_by": str(built_by),
        "source_duckdb": str(source_duckdb),
        "source_mtime": float(source_mtime or 0.0),
        "row_counts": counts if isinstance(counts, dict) else {},
    }


def snapshot_age_seconds(info: dict[str, Any] | None, *, now: datetime | None = None) -> float | None:
    if not info:
        return None
    raw = info.get("built_at")
    if not raw:
        return None
    try:
        built = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if built.tzinfo is None:
        built = built.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - built).total_seconds()


def snapshot_is_fresh(info: dict[str, Any] | None, *, max_age_seconds: float) -> bool:
    age = snapshot_age_seconds(info)
    return age is not None and 0 <= age <= max_age_seconds


def open_snapshot(duck: Any) -> SnapshotStore | None:
    """打开（并校验）快照；不可用时返回 `None` —— 绝不伪造"成功"。"""
    path = snapshot_path(duck)
    info = read_snapshot_info(path)
    if info is None:
        return None
    return SnapshotStore(path, info=info)


def ensure_screening_snapshot(
    duck: Any,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    reason: str = "startup_backfill",
) -> dict[str, Any] | None:
    """快照缺失/过旧才重建（服务启动补一次用）。失败返回 `None`，不抛。"""
    path = snapshot_path(duck)
    info = read_snapshot_info(path)
    if snapshot_is_fresh(info, max_age_seconds=max_age_seconds):
        logger.info(
            "筛选快照仍然新鲜（%s，age=%.0fs），跳过重建",
            info.get("built_at"), snapshot_age_seconds(info) or 0.0,
        )
        return info
    if info is None:
        logger.info("筛选快照缺失，启动时补建一次: %s", path)
    else:
        logger.info(
            "筛选快照已过期（%s，age=%.0fs），重建",
            info.get("built_at"), snapshot_age_seconds(info) or 0.0,
        )
    return build_screening_snapshot(duck, dest=path, reason=reason)


def snapshot_notice(info: dict[str, Any] | None) -> str:
    """给用户看的中文一句话（不含字段名/原因码这类内部词汇）。"""
    as_of = (info or {}).get("built_at")
    day = str(as_of)[:10] if as_of else "最近一次更新完成时"
    return (
        f"数据正在自动更新，本次筛选基于 {day} 的完整数据快照"
        "（不是更新中的半成品数据）；更新结束后刷新即可看到最新结果。"
    )
