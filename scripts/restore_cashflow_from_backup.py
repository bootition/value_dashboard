"""恢复 FY2025 现金流量补充域被误删的行（2026-11-17，reports/135）。

事故
----
`app/core/supplement.py::_persist_rows` 在 `replace_source_rows` 模式下按
报告期先删同源行、再只插「至少一个非空值」的行。一轮 `--stale-null` 重抓覆盖了
`2025-06-30`，该期返回全空/缺值 → 旧行被删掉、没有新行补回：

- `cash_flow_indirect` / `cash_flow_activity` 都从 **5 行变成 1 行**（只剩 002731）；
- 丢失的 4 只：000016 / 301139 / 601059 / 601198（capex 230.48M / 255.26M /
  22.64M / 21.63M；`cf_from_operating_indirect` −675.62M / −48.35M /
  +1,165.23M / +11,952.69M，另有折旧、摊销、减值等 8–10 个非空字段）。

本脚本做且只做一件事
--------------------
从事故前的备份 parquet（`.planning/maintenance-backup-etf-cashflow-20261117_135217/`）
读出 `2025-06-30` 的行，与**当前库**逐行对比，把缺失的 `(stock_code, report_date)`
插回去：

- `INSERT ... ON CONFLICT DO NOTHING`：**绝不覆盖**库里已有的值；
- 备份里的 `batch_id` / `raw_response_hash` / `fetch_time` **原样保留**（那是历史
  事实，伪造新批次会让 lineage 对不上账）；
- 默认 dry-run；`--yes` 才写，写前 `exclusive_update`（跨进程单写者）+
  `COPY 两张表现状 TO parquet` 备份到 `.planning/`。

用法
----
    # 1) 只读比对（默认，正式库只读）
    .venv/Scripts/python.exe scripts/restore_cashflow_from_backup.py

    # 2) 真写（主控执行）
    .venv/Scripts/python.exe scripts/restore_cashflow_from_backup.py --yes

    # 3) 只处理其中一张表 / 换备份目录 / 换报告期
    .venv/Scripts/python.exe scripts/restore_cashflow_from_backup.py \
        --table cash_flow_activity --report-date 2025-06-30

退出码
------
0 = 成功（含 dry-run）；2 = 路径/参数错误；3 = 有写锁或写入失败；4 = 备份缺失。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date
from numbers import Real
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    any_write_lock_active,
    exclusive_update,
)
from app.core.supplement import (  # noqa: E402
    ACTIVITY_COLUMNS,
    INDIRECT_COLUMNS,
)
from scripts._maintenance_safety import backup_tables  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger("restore_cashflow_from_backup")

#: 事故备份目录（.planning 已 gitignore；缺失时脚本明确报错而不是"恢复 0 行"）
DEFAULT_BACKUP_DIR = (
    PROJECT_ROOT
    / ".planning"
    / "maintenance-backup-etf-cashflow-20261117_135217"
)
#: 事故报告期
DEFAULT_REPORT_DATE = date(2025, 6, 30)
#: 两张表 → 落库列集合（单一真相来自 supplement.py 的 spec 列声明）
TABLES: dict[str, tuple[str, ...]] = {
    "cash_flow_indirect": INDIRECT_COLUMNS,
    "cash_flow_activity": ACTIVITY_COLUMNS,
}
#: 非空约束列：备份里缺任何一列都不能插
_REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "stock_code", "report_date", "source", "fetch_time",
    "raw_response_hash", "confidence", "batch_id",
})
#: 浮点比较容差（备份是 DOUBLE 往返，做 1e-6 级比较，避免把"同一行"误报成冲突）
_REL_TOL = 1e-9
_ABS_TOL = 1e-6


class BackupMissing(RuntimeError):
    """备份 parquet 缺失/读不出目标报告期。"""


def _normalize(value: Any) -> Any:
    """把 pandas/numpy 的 NA 统一成 None（DuckDB 只认 NULL）。"""
    if value is None:
        return None
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, bool) and missing:
        return None
    return value


def _same_value(left: Any, right: Any) -> bool:
    """两个单元格是否"同值"（数值容差、NA 等价、其余按字符串比）。"""
    left = _normalize(left)
    right = _normalize(right)
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, Real) and isinstance(right, Real):
        return math.isclose(float(left), float(right), rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    if isinstance(left, (date, pd.Timestamp)) and isinstance(right, (date, pd.Timestamp)):
        return pd.Timestamp(left) == pd.Timestamp(right)
    return str(left) == str(right)


def _rows_equal(current: dict[str, Any], backup: dict[str, Any], columns: list[str]) -> bool:
    return all(_same_value(current.get(column), backup.get(column)) for column in columns)


def load_backup_rows(
    backup_dir: Path, table: str, report_date: date,
) -> pd.DataFrame:
    """读备份 parquet 里指定报告期的行（只读，不碰正式库）。"""
    path = Path(backup_dir) / f"{table}.parquet"
    if not path.exists():
        raise BackupMissing(
            f"备份 parquet 不存在：{path}。"
            "请用 --backup-dir 指向事故备份目录（.planning/maintenance-backup-.../）。"
        )
    escaped = str(path).replace("'", "''")
    connection = duckdb.connect()
    try:
        frame = connection.execute(
            f"SELECT * FROM read_parquet('{escaped}') WHERE report_date = CAST(? AS DATE)",
            [report_date.isoformat()],
        ).fetchdf()
    except Exception as error:  # noqa: BLE001 - 备份损坏必须显式失败
        raise BackupMissing(f"读取备份失败 {path}: {type(error).__name__}: {error}") from error
    finally:
        connection.close()
    if frame.empty:
        raise BackupMissing(f"备份 {path} 里没有 {report_date} 的行")
    return frame


def _table_columns(store: DuckDBStore, table: str) -> list[str]:
    rows = store.read_query(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = ? ORDER BY ordinal_position""",
        [table],
    )
    return [str(row["column_name"]) for row in rows]


def _existing_rows(
    store: DuckDBStore, table: str, report_date: date,
) -> dict[str, dict[str, Any]]:
    rows = store.read_query(
        f"SELECT * FROM {table} WHERE report_date = CAST(? AS DATE)",
        [report_date.isoformat()],
    )
    return {str(row["stock_code"]): row for row in rows}


def plan_restore(
    store: DuckDBStore,
    *,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    report_date: date = DEFAULT_REPORT_DATE,
    tables: tuple[str, ...] = tuple(TABLES),
) -> dict[str, Any]:
    """只读比对：返回每张表的 {备份行数, 待恢复, 已存在, 冲突} 及明细。"""
    plan: dict[str, Any] = {
        "report_date": report_date.isoformat(),
        "backup_dir": str(backup_dir),
        "tables": {},
        "totals": {
            "backup_rows": 0, "missing": 0, "identical": 0, "conflict": 0,
        },
    }
    for table in tables:
        if table not in TABLES:
            raise ValueError(f"未知表名 {table!r}，可选：{sorted(TABLES)}")
        current_columns = _table_columns(store, table)
        if not current_columns:
            raise BackupMissing(f"当前库里没有表 {table}")
        backup = load_backup_rows(backup_dir, table, report_date)
        missing_required = _REQUIRED_COLUMNS - set(backup.columns)
        if missing_required:
            raise BackupMissing(f"备份 {table} 缺少非空列：{sorted(missing_required)}")
        compare_columns = [
            column for column in backup.columns if column in set(current_columns)
        ]
        existing = _existing_rows(store, table, report_date)
        missing_rows: list[dict[str, Any]] = []
        identical_codes: list[str] = []
        conflict_codes: list[str] = []
        for record in backup[compare_columns].to_dict("records"):
            row = {column: _normalize(record.get(column)) for column in compare_columns}
            code = str(row["stock_code"])
            current = existing.get(code)
            if current is None:
                missing_rows.append(row)
            elif _rows_equal(current, row, compare_columns):
                identical_codes.append(code)
            else:
                conflict_codes.append(code)
        plan["tables"][table] = {
            "backup_rows": int(len(backup)),
            "missing": len(missing_rows),
            "identical": len(identical_codes),
            "conflict": len(conflict_codes),
            "missing_codes": sorted(str(row["stock_code"]) for row in missing_rows),
            "identical_codes": sorted(identical_codes),
            "conflict_codes": sorted(conflict_codes),
            "insert_columns": compare_columns,
            "rows": missing_rows,
        }
        for key in ("missing", "identical", "conflict"):
            plan["totals"][key] += plan["tables"][table][key]
        plan["totals"]["backup_rows"] += plan["tables"][table]["backup_rows"]
    return plan


def _insert_missing(
    store: DuckDBStore,
    plan: dict[str, Any],
    report_date: date,
) -> dict[str, int]:
    """在单事务里把缺失行插回（ON CONFLICT DO NOTHING，绝不覆盖既有值）。"""
    restored: dict[str, int] = {}
    with store.transaction() as connection:
        for table, table_plan in plan["tables"].items():
            rows = table_plan["rows"]
            if not rows:
                restored[table] = 0
                continue
            columns = list(table_plan["insert_columns"])
            frame = pd.DataFrame(rows, columns=columns)
            frame["report_date"] = pd.to_datetime(frame["report_date"])
            connection.register("_restore_rows", frame)
            try:
                before = int(connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE report_date = CAST(? AS DATE)",
                    [report_date.isoformat()],
                ).fetchone()[0])
                projection = ", ".join(f'"{column}"' for column in columns)
                connection.execute(
                    f"""INSERT INTO {table} ({projection})
                        SELECT {projection} FROM _restore_rows
                        ON CONFLICT DO NOTHING"""
                )
                after = int(connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE report_date = CAST(? AS DATE)",
                    [report_date.isoformat()],
                ).fetchone()[0])
            finally:
                connection.unregister("_restore_rows")
            restored[table] = after - before
    return restored


def restore(
    store: DuckDBStore,
    *,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    report_date: date = DEFAULT_REPORT_DATE,
    tables: tuple[str, ...] = tuple(TABLES),
    yes: bool = False,
    backup: bool = True,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """执行（或演练）恢复，返回可核对的结果字典。

    Args:
        store: 已打开的 DuckDBStore（测试用临时库；CLI 用正式库）。
        backup_dir: 事故备份目录（含两张表的 parquet）。
        report_date: 要恢复的报告期（默认 2025-06-30）。
        tables: 只处理这些表（默认两张都处理）。
        yes: False = dry-run，只读不写。
        backup: `--yes` 写库前是否 `COPY` 两张表现状到 `.planning/`。
        lock_path: 单写者锁的库路径；`--yes` 时必须给出（CLI 恒传正式库路径）。
    """
    plan = plan_restore(
        store, backup_dir=backup_dir, report_date=report_date, tables=tables,
    )
    report: dict[str, Any] = {
        "mode": "dry_run" if not yes else "write",
        "report_date": report_date.isoformat(),
        "backup_dir": str(backup_dir),
        "plan": plan,
        "restored_by_table": {table: 0 for table in tables},
        "restored_total": 0,
        "backup_snapshot": None,
        "conflicts_preserved": True,
    }
    if not yes or plan["totals"]["missing"] == 0:
        return report
    if lock_path is None:
        raise ValueError("--yes 必须提供 lock_path（跨进程单写者锁）")

    # 写前快照冲突行的**库内现状**，写完逐个复比 —— ON CONFLICT DO NOTHING 的
    # 承诺是"不覆盖现有值"，不能只看行还在不在。
    conflict_before: dict[str, tuple[list[str], dict[str, dict[str, Any]]]] = {}
    for table, table_plan in plan["tables"].items():
        if not table_plan["conflict_codes"]:
            continue
        current = _existing_rows(store, table, report_date)
        conflict_before[table] = (
            list(table_plan["insert_columns"]),
            {
                code: current[code]
                for code in table_plan["conflict_codes"]
                if code in current
            },
        )

    with exclusive_update(lock_path):
        # 备份必须发生在锁内：否则"备份完 → 拿锁前"别的写进程改动会让快照与
        # 实际写窗口错位，回滚证据就不可信了。
        if backup:
            report["backup_snapshot"] = str(
                backup_tables(store, list(tables), "restore-cashflow-20250630")
            )
        restored = _insert_missing(store, plan, report_date)
    for table, (columns, before_rows) in conflict_before.items():
        current = _existing_rows(store, table, report_date)
        for code, before in before_rows.items():
            after = current.get(code)
            if after is None or not _rows_equal(after, before, columns):
                report["conflicts_preserved"] = False
                logger.error("冲突行被覆盖：%s %s（本脚本只该跳过它）", table, code)
    report["restored_by_table"] = restored
    report["restored_total"] = sum(restored.values())
    return report


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"报告期需为 YYYY-MM-DD：{value!r}") from error


def _parse_tables(value: str) -> tuple[str, ...]:
    names = tuple(item.strip() for item in value.split(",") if item.strip())
    unknown = [name for name in names if name not in TABLES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"未知表名 {unknown}，可选：{sorted(TABLES)}"
        )
    return names or tuple(TABLES)


def _print_plan(report: dict[str, Any]) -> None:
    plan = report["plan"]
    print(f"备份目录: {plan['backup_dir']}")
    print(f"报告期  : {plan['report_date']}")
    for table, table_plan in plan["tables"].items():
        print(
            f"  {table}: 备份 {table_plan['backup_rows']} 行 | "
            f"待恢复 {table_plan['missing']} | "
            f"已存在跳过 {table_plan['identical']} | "
            f"冲突 {table_plan['conflict']}"
        )
        if table_plan["missing_codes"]:
            print(f"      待恢复股票: {', '.join(table_plan['missing_codes'])}")
        if table_plan["conflict_codes"]:
            print(
                f"      冲突股票（库内值不同，未覆盖）: "
                f"{', '.join(table_plan['conflict_codes'])}"
            )
    totals = plan["totals"]
    print(
        f"  合计: 备份 {totals['backup_rows']} 行 | 待恢复 {totals['missing']} | "
        f"已存在跳过 {totals['identical']} | 冲突 {totals['conflict']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从事故备份 parquet 恢复 FY2025 现金流量补充域被误删的行",
    )
    parser.add_argument(
        "--backup-dir", default=str(DEFAULT_BACKUP_DIR),
        help=f"事故备份目录（默认 {DEFAULT_BACKUP_DIR}）",
    )
    parser.add_argument(
        "--report-date", type=_parse_date, default=DEFAULT_REPORT_DATE,
        help="要恢复的报告期（默认 2025-06-30）",
    )
    parser.add_argument(
        "--table", type=_parse_tables, default=tuple(TABLES),
        help="只处理这些表（逗号分隔：cash_flow_indirect,cash_flow_activity）",
    )
    parser.add_argument("--yes", action="store_true", help="真写（缺省 = dry-run 只读）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON（供报告采集）")
    args = parser.parse_args(argv)

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        print(f"[ABORT] 无法解析正式库路径：{error}", file=sys.stderr)
        return 2
    if args.yes and any_write_lock_active(paths.duckdb_path):
        print(
            "[ABORT] 检测到写锁（自动更新或其它维护进程正在写库）。"
            "单写者串行：等它结束后重跑，本脚本不排队、不抢锁。",
            file=sys.stderr,
        )
        return 3

    store = DuckDBStore(paths=paths)
    try:
        report = restore(
            store,
            backup_dir=Path(args.backup_dir),
            report_date=args.report_date,
            tables=args.table,
            yes=bool(args.yes),
            backup=True,
            lock_path=paths.duckdb_path if args.yes else None,
        )
    except BackupMissing as error:
        print(f"[ABORT] {error}", file=sys.stderr)
        return 4
    except (UpdateLockError, ValueError) as error:
        print(f"[ABORT] {error}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    _print_plan(report)
    if report["mode"] == "dry_run":
        print(
            f"\n[DRY RUN] 合计待恢复 {report['plan']['totals']['missing']} 行"
            "（加 --yes 才会写；写前会备份两张表现状）"
        )
    else:
        print(
            f"\n[WRITE] 已恢复 {report['restored_total']} 行 "
            f"{report['restored_by_table']}"
        )
        if report["backup_snapshot"]:
            print(f"[BACKUP] 写前快照: {report['backup_snapshot']}")
        if not report["conflicts_preserved"]:
            print("[WARN] 有冲突行在写后消失了，请立即复核！", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
