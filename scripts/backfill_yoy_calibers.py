"""一次性补偿：把快照同比字段改写成 TTM 口径，并回填 `*_yoy_fy` 年报口径（默认 dry-run）。

背景（reports/131 §9.2 P0-4）
-----------------------------
`indicator_snapshot.revenue_yoy / net_profit_yoy / deducted_profit_yoy` 过去由
`calculator._calc_growth()` 用**最新两个年报**计算，而详情页
`stock_detail._metric_history_rows()` 对同一字段用 `base_period='ttm'` 重算，
27.8%（1,527/5,485）的股票两处方向相反。

schema v43 起：
- `*_yoy`    改由 TTM 口径写入（与详情页同源）；
- `*_yoy_fy` 另存年报口径（旧算法原样保留）。

本脚本负责**存量**：把 v43 迁移后仍是旧值/空值的快照行按新口径重算。
新代码写入的快照行已经是双口径，本脚本可安全重跑（幂等）。

安全边界（与仓库其它维护脚本同一套纪律）
--------------------------------------
- **默认 dry-run**：只读、只统计候选，不写库、不落备份；
- **`--yes` 才写**：写前检查写锁 `any_write_lock_active`；写窗口用
  `exclusive_update` 单写者串行；
- **写前备份**：`indicator_snapshot` 整表 COPY 成 parquet 到
  `.planning/maintenance-backup-yoy-calibers-<ts>/`（`--backup-dir` 可覆盖）；
- **只更新同比六列 + calculated_at**：不重算估值/技术/其它基本面字段，不改 report_date；
- **本脚本不建表/不迁移**：schema 由 `vd data init` / 自动更新负责（v43+）。

用法
----
    # 1) 看计划（默认 dry-run；会抽 5 只展示"旧值 → 新值"）
    .venv/Scripts/python.exe scripts/backfill_yoy_calibers.py

    # 2) 全市场真写（需要显式确认 + 无其它写者）
    .venv/Scripts/python.exe scripts/backfill_yoy_calibers.py --yes

    # 3) 小样本试算 / 临时库演练
    .venv/Scripts/python.exe scripts/backfill_yoy_calibers.py --codes 600519,000001 --yes
    VD_ENV=test ... python scripts/backfill_yoy_calibers.py --db /tmp/x/valuedashboard.duckdb --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.indicators.calculator import IndicatorCalculator  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    data_root,
    require_formal_maintenance_paths,
)
from app.core.storage.sqlite_store import SQLiteStore  # noqa: E402
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    any_write_lock_active,
    exclusive_update,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_yoy_calibers")

TABLE = "indicator_snapshot"
# 快照里必须成套更新的同比六列（TTM 三列 + FY 三列）。
FIELDS: tuple[str, ...] = (
    "revenue_yoy", "revenue_yoy_fy",
    "net_profit_yoy", "net_profit_yoy_fy",
    "deducted_profit_yoy", "deducted_profit_yoy_fy",
)
CANDIDATE_SQL = """
    SELECT stock_code, report_date
    FROM indicator_snapshot
    WHERE 1 = 1
    {code_filter}
    ORDER BY stock_code
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重算快照同比六列（*_yoy=TTM，*_yoy_fy=年报；默认 dry-run）",
    )
    parser.add_argument("--yes", action="store_true", help="真正写库（缺省 = dry-run）")
    parser.add_argument("--codes", default="", help="只处理这些代码（逗号分隔）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只（0 = 全部）")
    parser.add_argument(
        "--sample", type=int, default=5,
        help="dry-run 时抽样计算并展示的股票只数（0 = 不计算）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="临时库演练（只允许 VD_ENV=test/staging，且不允许指向 data/ 下文件）",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="备份目录（缺省 .planning/maintenance-backup-yoy-calibers-<ts>）",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return parser.parse_args(argv)


def _is_inside(child: Path, parent: Path) -> bool:
    child_str = os.path.normcase(str(Path(child).resolve())).rstrip("\\/")
    parent_str = os.path.normcase(str(Path(parent).resolve())).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def resolve_target_paths(explicit_db: str | None = None) -> DatabasePathSet:
    """解析目标库：不给 --db 只能走正式档位；--db 仅 test/staging 演练。"""
    if explicit_db is None:
        return require_formal_maintenance_paths()
    env = os.environ.get("VD_ENV", "")
    if env not in {VdEnv.TEST.value, VdEnv.STAGING.value}:
        raise PathIsolationError(
            "--db 只允许在 VD_ENV=test/staging 下使用（演练/回归）；"
            "正式库请省略 --db，由 require_formal_maintenance_paths() 解析"
        )
    candidate = Path(explicit_db).expanduser().resolve()
    if _is_inside(candidate, data_root()):
        raise PathIsolationError(f"--db 不允许指向项目 data/ 下的文件: {candidate}")
    if candidate.name != "valuedashboard.duckdb":
        raise PathIsolationError(f"--db 必须指向 valuedashboard.duckdb: {candidate}")
    if not candidate.is_file():
        raise PathIsolationError(f"--db 目标不存在（先 init schema 建库）: {candidate}")
    return DatabasePathSet(
        env=VdEnv(env),
        duckdb_path=candidate,
        sqlite_path=candidate.with_name("valuedashboard.sqlite"),
        run_root=candidate.parent,
    ).validate()


def select_candidates(
    duck: DuckDBStore, *, codes: list[str] | None = None, limit: int = 0,
) -> list[dict[str, Any]]:
    """选出口径需要重写的快照行（全表；`codes`/`limit` 用于分批验收）。"""
    params: list[Any] = []
    code_filter = ""
    if codes:
        placeholders = ", ".join("?" for _ in codes)
        code_filter = f"AND stock_code IN ({placeholders})"
        params.extend(codes)
    sql = CANDIDATE_SQL.format(code_filter=code_filter)
    if limit and limit > 0:
        sql = f"{sql} LIMIT {int(limit)}"
    return duck.read_query(sql, params)


def _snapshot_columns(duck: DuckDBStore) -> set[str]:
    """当前库 indicator_snapshot 的实际列（v43 迁移前也要能只读预演）。"""
    return {
        str(row["name"])
        for row in duck.read_query("PRAGMA table_info('indicator_snapshot')")
    }


def _coverage(duck: DuckDBStore) -> dict[str, Any]:
    """最新快照期六列覆盖 + 两口径方向相反的只数（写前/写后同一口径）。

    v43 迁移前（库内只有 `*_yoy`、没有 `*_yoy_fy`）也允许 dry-run：缺列按 0
    覆盖展示，绝不因为"还没建列"让只读预演直接崩。
    """
    existing = _snapshot_columns(duck)
    usable = [field for field in FIELDS if field in existing]
    counts = ", ".join(
        (f"COUNT({field}) AS {field}" if field in usable else f"0 AS {field}")
        for field in FIELDS
    )
    flip_expr = "0"
    if {"revenue_yoy", "revenue_yoy_fy"}.issubset(existing):
        flip_expr = (
            "SUM(CASE WHEN revenue_yoy IS NOT NULL "
            "AND revenue_yoy_fy IS NOT NULL "
            "AND (revenue_yoy > 0) <> (revenue_yoy_fy > 0) "
            "THEN 1 ELSE 0 END)"
        )
    rows = duck.read_query(
        f"""SELECT report_date, COUNT(*) AS total, {counts},
                   {flip_expr} AS revenue_direction_flip
            FROM indicator_snapshot
            WHERE report_date = (SELECT MAX(report_date) FROM indicator_snapshot)
            GROUP BY report_date"""
    )
    if not rows:
        return {
            "report_date": None, "total": 0, "counts": {},
            "coverage_pct": dict.fromkeys(FIELDS, 0.0),
            "revenue_direction_flip": 0,
        }
    row = rows[0]
    total = int(row.get("total") or 0)
    return {
        "report_date": str(row.get("report_date")),
        "total": total,
        "counts": {field: int(row.get(field) or 0) for field in FIELDS},
        "coverage_pct": {
            field: (round(100.0 * int(row.get(field) or 0) / total, 2) if total else 0.0)
            for field in FIELDS
        },
        "revenue_direction_flip": int(row.get("revenue_direction_flip") or 0),
    }


def _backup_table(duck: DuckDBStore, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / f"{TABLE}.parquet").replace("'", "''")
    with duck.write_connection() as conn:
        conn.execute(f"COPY {TABLE} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s → %s", TABLE, target)
    return backup_dir / f"{TABLE}.parquet"


def compute_records(
    duck: DuckDBStore, sqlite: SQLiteStore, candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """按新双口径重算同比六列；返回 (records, failed_codes)。"""
    calculator = IndicatorCalculator(duck=duck, sqlite=sqlite)
    records: list[dict[str, Any]] = []
    failed: list[str] = []
    with duck.read_connection() as connection:
        calculator._calculation_read_connection = connection
        try:
            for row in candidates:
                code = str(row["stock_code"])
                try:
                    growth = calculator._calc_growth(code)
                except Exception as error:  # noqa: BLE001 - 单股失败不拖垮整批
                    failed.append(code)
                    logger.warning("同比重算失败 %s: %s", code, error)
                    continue
                records.append({
                    "stock_code": code,
                    "report_date": row["report_date"],
                    "calculated_at": datetime.now(UTC),
                    **{field: growth.get(field) for field in FIELDS},
                })
        finally:
            del calculator._calculation_read_connection
    return records, failed


def _before_snapshot(
    duck: DuckDBStore, candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """取抽样行当前值，供 dry-run 展示"旧 → 新"。"""
    if not candidates:
        return []
    codes = [str(row["stock_code"]) for row in candidates]
    placeholders = ", ".join("?" for _ in codes)
    existing = _snapshot_columns(duck)
    selected = ", ".join(
        (field if field in existing else f"NULL AS {field}") for field in FIELDS
    )
    rows = duck.read_query(
        f"""SELECT stock_code, report_date, {selected}
            FROM indicator_snapshot
            WHERE stock_code IN ({placeholders})
            ORDER BY stock_code""",
        codes,
    )
    by_code = {str(row["stock_code"]): row for row in rows}
    return [by_code[code] for code in codes if code in by_code]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    codes_filter = [item.strip() for item in args.codes.split(",") if item.strip()]
    try:
        paths = resolve_target_paths(args.db)
    except PathIsolationError as error:
        print(f"[ABORT] 无法解析目标库路径：{error}", file=sys.stderr)
        return 2

    write = bool(args.yes)
    if write and any_write_lock_active(paths.duckdb_path):
        print(
            "[ABORT] 检测到写锁（自动更新或其它维护进程正在写库）。"
            "单写者串行：等它结束后重跑，本脚本不排队、不抢锁。",
            file=sys.stderr,
        )
        return 3

    duck = DuckDBStore(paths=paths)
    before = _coverage(duck)
    candidates = select_candidates(duck, codes=codes_filter, limit=args.limit)
    backup_path: Path | None = None
    write_report: dict[str, Any] | None = None
    preview: list[dict[str, Any]] = []

    if write and candidates:
        sqlite = SQLiteStore(paths=paths)
        backup_dir = (
            Path(args.backup_dir)
            if args.backup_dir
            else PROJECT_ROOT / ".planning"
            / f"maintenance-backup-yoy-calibers-{datetime.now():%Y%m%d_%H%M%S}"
        )
        try:
            with exclusive_update(paths.duckdb_path):
                try:
                    backup_path = _backup_table(duck, Path(backup_dir))
                except Exception as error:  # noqa: BLE001 - 备份失败必须中止写入
                    print(f"[ABORT] 写前备份失败，已中止：{error}", file=sys.stderr)
                    return 5
                records, failed = compute_records(duck, sqlite, candidates)
                if records:
                    calculator = IndicatorCalculator(duck=duck, sqlite=sqlite)
                    calculator.update_yoy_snapshot_fields(records)
                write_report = {
                    "attempted": len(candidates),
                    "updated": len(records),
                    "failed": len(failed),
                    "failed_codes": failed[:20],
                }
        except UpdateLockError as error:
            print(f"[ABORT] 另一个更新进程持有写锁：{error}", file=sys.stderr)
            return 3
    elif not write and candidates and args.sample > 0:
        sqlite = SQLiteStore(paths=paths)
        sample = candidates[: int(args.sample)]
        old_rows = {str(row["stock_code"]): row for row in _before_snapshot(duck, sample)}
        records, failed = compute_records(duck, sqlite, sample)
        for record in records:
            old = old_rows.get(str(record["stock_code"]), {})
            preview.append({
                "stock_code": record["stock_code"],
                "report_date": str(record["report_date"]),
                "old_revenue_yoy": old.get("revenue_yoy"),
                "new_revenue_yoy_ttm": record.get("revenue_yoy"),
                "new_revenue_yoy_fy": record.get("revenue_yoy_fy"),
                "old_net_profit_yoy": old.get("net_profit_yoy"),
                "new_net_profit_yoy_ttm": record.get("net_profit_yoy"),
                "new_net_profit_yoy_fy": record.get("net_profit_yoy_fy"),
            })
        if failed:
            logger.warning("dry-run 抽样失败 %d 只: %s", len(failed), failed[:20])

    after = _coverage(duck)
    payload = {
        "task": "同比双口径回填（*_yoy=TTM，*_yoy_fy=年报）",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": not write,
        "database": str(paths.duckdb_path),
        "codes": codes_filter,
        "limit": args.limit,
        "candidates": len(candidates),
        "candidate_codes": [str(row["stock_code"]) for row in candidates[:50]],
        "before": before,
        "after": after,
        "backup_path": str(backup_path) if backup_path else None,
        "written": write_report,
        "dry_run_preview": preview,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}"
            f"  代码过滤: {', '.join(codes_filter) if codes_filter else '无'}"
        )
        print(
            f"最新快照期: {before['report_date']}  总行数: {before['total']}"
            f"  两口径方向相反(营收): {before['revenue_direction_flip']} 只"
        )
        print(f"覆盖率: {before['coverage_pct']}")
        print(f"待重算行: {len(candidates)}")
        for item in preview:
            print(
                f"  {item['stock_code']} {item['report_date']}: "
                f"revenue_yoy 旧={item['old_revenue_yoy']} → "
                f"TTM={item['new_revenue_yoy_ttm']} / FY={item['new_revenue_yoy_fy']}"
            )
        if write_report:
            print(
                f"已重算 {write_report['updated']} 行（失败 {write_report['failed']}）"
            )
            print(f"写后覆盖率: {after['coverage_pct']}")
            print(f"备份: {backup_path}")
        else:
            print("[DRY RUN] 未写库（加 --yes 才会写；写前会自动备份 indicator_snapshot）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
