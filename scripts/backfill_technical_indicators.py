"""一次性补偿：重算快照里 `period_return IS NULL` 且 qfq 收盘 ≥2 条的股票（默认 dry-run）。

背景（reports/131 §9.2 P0-5）
-----------------------------
`period_return / annualized_volatility / max_drawdown` 由
`IndicatorCalculator._calc_technical()` 从 `price_daily_qfq` 的连续收盘序列算。
qfq 是**事后补齐**的：这些股票的快照行在 qfq 落库前就算过一次并写死 NULL，
补齐后旧机制只认"qfq 最新日 < raw 最新日"的滞后形态，抓不到"补齐后已齐平"的
股票 → 正式库实测 2,931/5,575 行（52.6%）为空，而 `price_daily_qfq` 对 5,585
只是完整的（2026-11-16 只读复测）。

自动修复路径是 `app/core/update.py` 的 `data_refresh_state['technical_pending']`
（qfq 写入成功就强制重算）。本脚本只负责**存量**：把当前仍为 NULL 且 qfq 收盘
≥2 条的股票一次性补齐。

安全边界（与仓库其它维护脚本同一套纪律）
--------------------------------------
- **默认 dry-run**：只读、只统计候选，不写库、不落备份；
- **`--yes` 才写**：写前检查写锁 `any_write_lock_active`；写窗口用
  `exclusive_update` 单写者串行；
- **写前备份**：`indicator_snapshot` 整表 COPY 成 parquet 到
  `.planning/maintenance-backup-technical-indicators-<ts>/`（`--backup-dir` 可覆盖）；
- **只更新技术三列 + calculated_at**：不重算估值/基本面，不改 report_date；
- **本脚本不建表/不迁移**：schema 由 `vd data init` / 自动更新负责（v43+）；
- 任何一只计算失败都计入 failed 并保留 NULL，绝不把 NULL 伪造成 0。

用法
----
    # 1) 看计划（默认 dry-run）
    .venv/Scripts/python.exe scripts/backfill_technical_indicators.py

    # 2) 小样本试算（仍然 dry-run）
    .venv/Scripts/python.exe scripts/backfill_technical_indicators.py --limit 20

    # 3) 真写（需要显式确认 + 无其它写者）
    .venv/Scripts/python.exe scripts/backfill_technical_indicators.py --yes

    # 临时库演练/回归（只在 VD_ENV=test|staging 下可用，永不接受 data/ 下路径）
    VD_ENV=test ... python scripts/backfill_technical_indicators.py --db /tmp/x/valuedashboard.duckdb --yes
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
logger = logging.getLogger("backfill_technical_indicators")

TABLE = "indicator_snapshot"
FIELDS: tuple[str, ...] = (
    "period_return",
    "annualized_volatility",
    "max_drawdown",
)

CANDIDATE_SQL = """
    SELECT s.stock_code,
           s.report_date,
           q.qfq_rows
    FROM indicator_snapshot s
    JOIN (
        SELECT stock_code, COUNT(*) AS qfq_rows
        FROM price_daily_qfq
        WHERE close IS NOT NULL
        GROUP BY stock_code
        HAVING COUNT(*) >= 2
    ) q
      ON q.stock_code = s.stock_code
    WHERE s.period_return IS NULL
    {code_filter}
    ORDER BY s.stock_code
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重算技术三列为 NULL 且有 ≥2 条 qfq 收盘的股票（默认 dry-run）",
    )
    parser.add_argument("--yes", action="store_true", help="真正写库（缺省 = dry-run）")
    parser.add_argument("--codes", default="", help="只处理这些代码（逗号分隔）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只（0 = 全部）")
    parser.add_argument(
        "--db",
        default=None,
        help="临时库演练（只允许 VD_ENV=test/staging，且不允许指向 data/ 下文件）",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="备份目录（缺省 .planning/maintenance-backup-technical-indicators-<ts>）",
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
    """选出"period_return IS NULL + qfq 收盘 ≥2 条"的快照行。

    返回列：stock_code / report_date / qfq_rows。`codes` 为空表示全市场。
    `limit > 0` 时按 stock_code 排序截断（便于分批验收）。
    """
    params: list[Any] = []
    code_filter = ""
    if codes:
        placeholders = ", ".join("?" for _ in codes)
        code_filter = f"AND s.stock_code IN ({placeholders})"
        params.extend(codes)
    sql = CANDIDATE_SQL.format(code_filter=code_filter)
    if limit and limit > 0:
        sql = f"{sql} LIMIT {int(limit)}"
    return duck.read_query(sql, params)


def _coverage(duck: DuckDBStore) -> dict[str, Any]:
    """最新快照期技术三列的覆盖率（写前/写后共用的验收口径）。"""
    counts = ", ".join(f"COUNT({field}) AS {field}" for field in FIELDS)
    rows = duck.read_query(
        f"""SELECT report_date, COUNT(*) AS total, {counts}
            FROM indicator_snapshot
            WHERE report_date = (SELECT MAX(report_date) FROM indicator_snapshot)
            GROUP BY report_date"""
    )
    if not rows:
        return {"report_date": None, "total": 0, "counts": {}, "coverage_pct": dict.fromkeys(FIELDS, 0.0)}
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
    }


def _backup_table(duck: DuckDBStore, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / f"{TABLE}.parquet").replace("'", "''")
    with duck.write_connection() as conn:
        conn.execute(f"COPY {TABLE} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s → %s", TABLE, target)
    return backup_dir / f"{TABLE}.parquet"


def recompute_candidates(
    duck: DuckDBStore, sqlite: SQLiteStore, candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """重算候选股票的技术三列并写回；返回 success/failed 明细。"""
    calculator = IndicatorCalculator(duck=duck, sqlite=sqlite)
    records: list[dict[str, Any]] = []
    failed: list[str] = []
    # 复用一只只读连接：逐股 open-per-query 在 2,900 只上会明显变慢。
    with duck.read_connection() as connection:
        calculator._calculation_read_connection = connection
        try:
            for row in candidates:
                code = str(row["stock_code"])
                try:
                    technical = calculator._calc_technical(code)
                except Exception as error:  # noqa: BLE001 - 单股失败不拖垮整批
                    failed.append(code)
                    logger.warning("技术指标重算失败 %s: %s", code, error)
                    continue
                records.append({
                    "stock_code": code,
                    "report_date": row["report_date"],
                    "calculated_at": datetime.now(UTC),
                    **{field: technical.get(field) for field in FIELDS},
                })
        finally:
            del calculator._calculation_read_connection

    if records:
        calculator.update_technical_snapshot_fields(records)
    filled = sum(1 for record in records if record.get("period_return") is not None)
    return {
        "attempted": len(candidates),
        "updated": len(records),
        "filled_period_return": filled,
        "still_null": len(records) - filled,
        "failed": len(failed),
        "failed_codes": failed[:20],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    codes = [item.strip() for item in args.codes.split(",") if item.strip()]
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
    candidates = select_candidates(duck, codes=codes, limit=args.limit)
    backup_path: Path | None = None
    write_report: dict[str, Any] | None = None

    if write and candidates:
        sqlite = SQLiteStore(paths=paths)
        backup_dir = (
            Path(args.backup_dir)
            if args.backup_dir
            else PROJECT_ROOT / ".planning"
            / f"maintenance-backup-technical-indicators-{datetime.now():%Y%m%d_%H%M%S}"
        )
        try:
            with exclusive_update(paths.duckdb_path):
                try:
                    backup_path = _backup_table(duck, Path(backup_dir))
                except Exception as error:  # noqa: BLE001 - 备份失败必须中止写入
                    print(f"[ABORT] 写前备份失败，已中止：{error}", file=sys.stderr)
                    return 5
                write_report = recompute_candidates(duck, sqlite, candidates)
        except UpdateLockError as error:
            print(f"[ABORT] 另一个更新进程持有写锁：{error}", file=sys.stderr)
            return 3

    after = _coverage(duck)
    payload = {
        "task": "补齐技术三列（period_return / annualized_volatility / max_drawdown）",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": not write,
        "database": str(paths.duckdb_path),
        "codes": codes,
        "limit": args.limit,
        "candidates": len(candidates),
        "candidate_codes": [str(row["stock_code"]) for row in candidates[:50]],
        "candidate_preview": [
            {
                "stock_code": str(row["stock_code"]),
                "report_date": str(row["report_date"]),
                "qfq_rows": int(row["qfq_rows"]),
            }
            for row in candidates[:20]
        ],
        "before": before,
        "after": after,
        "backup_path": str(backup_path) if backup_path else None,
        "written": write_report,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}"
            f"  代码过滤: {', '.join(codes) if codes else '无'}"
        )
        print(
            f"最新快照期: {before['report_date']}  总行数: {before['total']}"
            f"  技术三列覆盖率: {before['coverage_pct']}"
        )
        print(f"候选（period_return IS NULL 且 qfq 收盘 ≥2）: {len(candidates)} 只")
        if candidates:
            print(
                "  前 10 只: "
                + ", ".join(
                    f"{row['stock_code']}({row['qfq_rows']}行)" for row in candidates[:10]
                )
            )
        if write_report:
            print(
                f"已重算 {write_report['updated']} 只（成功补出 period_return "
                f"{write_report['filled_period_return']} / 仍空 {write_report['still_null']} / "
                f"失败 {write_report['failed']}）"
            )
            print(f"写后覆盖率: {after['coverage_pct']}")
            print(f"备份: {backup_path}")
        else:
            print("[DRY RUN] 未写库（加 --yes 才会写；写前会自动备份 indicator_snapshot）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
