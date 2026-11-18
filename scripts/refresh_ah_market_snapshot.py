"""CLI：把 `build_ah_snapshot()` 的 A+H 结果落到 `ah_market_snapshot`（v41）。

为什么需要这个脚本
------------------
`app/core/ah_metrics.py::build_ah_snapshot()` 从 2026-11-16 起就**算得出** 203 只
A+H 的 `h_share_market_cap_cny`（H 价 × H 股本 × 汇率，人民币元）、AH 溢价率、
A/H 双侧股息率，但全仓只有它自己和它的单测引用它——没有落库列、没有 API、
前端零命中，**用户永远看不到**。本脚本是它的写出口：

    price_daily_raw + hk_price_daily + fx_rate_daily + dividends + hk_dividends
        → build_ah_snapshot() → ah_market_snapshot → GET /api/stock/{code}/ah-metrics

安全边界（正式库只读优先，与仓库其它维护脚本同一套纪律）
------------------------------------------------------
- **默认 dry-run**：不加 `--yes` 只计算 + 打印统计，**绝不写库**；
- **`--yes` 才写**：写前先查写锁 `any_write_lock_active`，有别的进程在写就退出，
  不排队、不抢；写窗口再用 `exclusive_update` 单写者串行；
- **写前备份**：`ah_market_snapshot` 整表 COPY 成 parquet 到
  `.planning/maintenance-backup-ah-market-snapshot-<ts>/`（可用 `--backup-dir` 覆盖）；
- **本脚本不建表**：表由 schema 迁移（`DUCKDB_SCHEMA_VERSION=41`，
  `vd data init`）创建。表不存在时 `--yes` 直接中止并提示，绝不擅自 DDL；
- **幂等 upsert**：主键 `(stock_code, as_of)`，同一天重跑行数不变（只覆盖值）；
- **不碰 `indicator_snapshot`**：本脚本只写 `ah_market_snapshot` 一张表。

口径纪律（与 ah_metrics 模块 docstring 一致，**不允许**在本脚本重新解释）
----------------------------------------------------------------------
1. `h_share_market_cap_cny` = H 价 × H 股本 × 汇率，**人民币元**。这是
   「港股市值」，不是 A 股口径总市值的拆分。
2. 行业口径「总市值」= `indicator_snapshot.total_market_cap`（A 股价格 × 总股本），
   本脚本既不读也不写它；`ah_total_market_cap_cny` 只是 A+H 拆分参考量。
3. **单位转换陷阱**：`build_ah_snapshot()` 的 `a_yield` / `h_yield` /
   `h_yield_after_tax` 是**小数比例**（0.052 = 5.2%），而本表列名是
   `*_yield_pct`，所以写库前必须 **×100**。转换只在这里做一次。
4. 缺数据的股票**也写一行**（计算列为 NULL），原因码进 `note`：
   `status=ok` / `status=incomplete; reason=missing_h_price`。绝不伪造数值。

用法
----
    # 只算不写（默认），看 203 只的覆盖与缺失分布
    .venv/Scripts/python.exe scripts/refresh_ah_market_snapshot.py

    # 小样本试算
    .venv/Scripts/python.exe scripts/refresh_ah_market_snapshot.py --codes 601398,601857

    # 指定 as_of 日期（默认今天）
    .venv/Scripts/python.exe scripts/refresh_ah_market_snapshot.py --as-of 2026-11-21

    # 真写（需要显式确认 + 无其它写者 + 表已迁移到 v41）
    .venv/Scripts/python.exe scripts/refresh_ah_market_snapshot.py --yes

    # 临时库演练/回归（只在 VD_ENV=test|staging 下可用，永不接受 data/ 下路径）
    VD_ENV=test ... python scripts/refresh_ah_market_snapshot.py --db /tmp/x/valuedashboard.duckdb --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.ah_metrics import build_ah_snapshot  # noqa: E402
from app.core.financial_period import cn_today  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    data_root,
    require_formal_maintenance_paths,
)
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
logger = logging.getLogger("refresh_ah_market_snapshot")

TABLE = "ah_market_snapshot"
SOURCE = "ah_metrics.build_ah_snapshot"

# 写入列顺序（也是 UPSERT 的参数顺序，两处必须一致）
COLUMNS: tuple[str, ...] = (
    "stock_code",
    "hk_code",
    "as_of",
    "a_price_date",
    "h_price_date",
    "a_close",
    "h_close_hkd",
    "fx_hkd_cny",
    "fx_date",
    "a_shares",
    "h_shares",
    "total_shares",
    "a_share_market_cap_cny",
    "h_share_market_cap_cny",
    "ah_total_market_cap_cny",
    "ah_premium_pct",
    "a_yield_pct",
    "h_yield_pct",
    "h_yield_after_tax_pct",
    "identity_status",
    "identity_deviation_pct",
    "h_shares_confidence",
    "note",
    "source",
    "fetch_time",
)

_UPDATE_COLUMNS = tuple(column for column in COLUMNS if column not in {"stock_code", "as_of"})
UPSERT_SQL = f"""
INSERT INTO {TABLE} ({", ".join(COLUMNS)})
VALUES ({", ".join("?" for _ in COLUMNS)})
ON CONFLICT (stock_code, as_of) DO UPDATE SET
{", ".join(f"    {column} = excluded.{column}" for column in _UPDATE_COLUMNS)}
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 build_ah_snapshot() 的 A+H 结果落到 ah_market_snapshot（默认 dry-run）",
    )
    parser.add_argument("--yes", action="store_true", help="真正写库（缺省 = dry-run，只算不写）")
    parser.add_argument(
        "--as-of",
        default=None,
        help="快照日 YYYY-MM-DD（默认今天）；A 股价格日/汇率日以其为上限",
    )
    parser.add_argument(
        "--codes",
        default="",
        help="只处理这些 A 股代码（逗号分隔；用于小样本试算）",
    )
    parser.add_argument(
        "--tax-rate",
        type=float,
        default=0.20,
        help="H 股红利税率（默认 0.20 = 港股通个人 20%%），只影响税后股息率",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="临时库演练（只允许 VD_ENV=test/staging，且不允许指向 data/ 下文件）",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="备份目录（缺省 .planning/maintenance-backup-ah-market-snapshot-<ts>）",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果（供脚本/报告采集）")
    return parser.parse_args(argv)


def _is_inside(child: Path, parent: Path) -> bool:
    """大小写不敏感的"是否位于目录内"判断（Windows 路径大小写不固定）。"""
    child_str = os.path.normcase(str(Path(child).resolve())).rstrip("\\/")
    parent_str = os.path.normcase(str(Path(parent).resolve())).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def resolve_target_paths(explicit_db: str | None = None) -> DatabasePathSet:
    """解析目标库。

    - 不给 ``--db``：只能解析到**正式档位**（``require_formal_maintenance_paths()``
      会校验 VD_ENV/VD_FORMAL_ACK/路径三元组）。
    - 给 ``--db``：只允许 ``VD_ENV=test|staging``，且不允许指向项目 ``data/`` 下
      的任何文件。这条口子只为"在临时库上演练/回归"，永远不能拿来绕正式库门禁。
    """
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


def table_exists(duck: DuckDBStore) -> bool:
    """表是否已由 schema 迁移（v41）创建；dry-run 不依赖它。"""
    try:
        rows = duck.read_query(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [TABLE],
        )
    except Exception as error:  # noqa: BLE001 - 未迁移/不可读都不该让 dry-run 崩
        logger.warning("%s 存在性检查失败（按「未建表」处理）: %s", TABLE, error)
        return False
    return bool(rows)


def _pct(fraction: Any) -> float | None:
    """小数比例 → 百分数（0.052 → 5.2）。None 保持 None，绝不补 0。"""
    if fraction is None or isinstance(fraction, bool):
        return None
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return round(value * 100.0, 6)


def _note(row: dict[str, Any]) -> str:
    """`status=ok` / `status=incomplete; reason=missing_h_price`（机器可读）。"""
    status = str(row.get("status") or "unknown")
    reason = row.get("reason")
    return f"status={status}; reason={reason}" if reason else f"status={status}"


def row_to_params(
    row: dict[str, Any], *, as_of: date, fetch_time: datetime,
) -> tuple[Any, ...]:
    """`build_ah_snapshot()` 的一行 → 既建表列顺序的参数元组。"""
    values: dict[str, Any] = {
        "stock_code": row["stock_code"],
        "hk_code": row.get("hk_code"),
        "as_of": as_of,
        "a_price_date": row.get("a_price_date"),
        "h_price_date": row.get("h_price_date"),
        "a_close": row.get("a_close"),
        "h_close_hkd": row.get("h_close"),
        "fx_hkd_cny": row.get("fx_hkd_cny"),
        "fx_date": row.get("fx_date"),
        # circ_shares 就是 ah_metrics 里的「A 股股本」（见 ah_market_cap 实现）
        "a_shares": row.get("circ_shares"),
        "h_shares": row.get("h_shares"),
        "total_shares": row.get("total_shares"),
        "a_share_market_cap_cny": row.get("a_share_market_cap"),
        "h_share_market_cap_cny": row.get("h_share_market_cap_cny"),
        "ah_total_market_cap_cny": row.get("ah_total_market_cap_cny"),
        "ah_premium_pct": row.get("ah_premium_pct"),
        # 小数比例 → 百分数，只在这里转一次（模块 docstring 口径纪律 §7）
        "a_yield_pct": _pct(row.get("a_yield")),
        "h_yield_pct": _pct(row.get("h_yield")),
        "h_yield_after_tax_pct": _pct(row.get("h_yield_after_tax")),
        "identity_status": row.get("identity_status"),
        "identity_deviation_pct": row.get("identity_deviation_pct"),
        "h_shares_confidence": row.get("h_shares_confidence"),
        "note": _note(row),
        "source": SOURCE,
        "fetch_time": fetch_time,
    }
    return tuple(values[column] for column in COLUMNS)


def summarize(
    rows: list[dict[str, Any]], params: list[tuple[Any, ...]],
) -> dict[str, Any]:
    """dry-run / 写后共用的统计（全部来自同一份计算结果）。"""
    h_cap_index = COLUMNS.index("h_share_market_cap_cny")
    with_h_cap = [item for item in params if item[h_cap_index] is not None]
    reasons: dict[str, int] = {}
    for row in rows:
        reason = row.get("reason")
        if reason:
            reasons[str(reason)] = reasons.get(str(reason), 0) + 1
    identity: dict[str, int] = {}
    for row in rows:
        status = str(row.get("identity_status") or "missing")
        identity[status] = identity.get(status, 0) + 1
    return {
        "rows": len(params),
        "rows_with_h_market_cap": len(with_h_cap),
        "rows_missing_h_market_cap": len(params) - len(with_h_cap),
        "reasons": dict(sorted(reasons.items())),
        "identity_status": dict(sorted(identity.items())),
        "h_market_cap_cny_sum": (
            round(sum(float(item[h_cap_index]) for item in with_h_cap), 2)
            if with_h_cap
            else None
        ),
    }


def _backup_table(duck: DuckDBStore, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / f"{TABLE}.parquet").replace("'", "''")
    with duck.write_connection() as conn:
        conn.execute(f"COPY {TABLE} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s → %s", TABLE, target)
    return backup_dir / f"{TABLE}.parquet"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else cn_today()
    except ValueError:
        print(f"[ABORT] --as-of 需为 YYYY-MM-DD：{args.as_of!r}", file=sys.stderr)
        return 2
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
    has_table = table_exists(duck)
    if not has_table:
        # dry-run 照常算（完全不依赖该表）；但必须提前说清 --yes 会中止
        print(
            f"[NOTE] {TABLE} 尚未创建（正式库 schema 未迁移到 v41）："
            "dry-run 不受影响，--yes 会在写入前中止。先跑建表/迁移。",
            file=sys.stderr,
        )
        if write:
            print("[ABORT] 目标表不存在，拒绝写入（本脚本不擅自 DDL）。", file=sys.stderr)
            return 4

    if not args.json:
        print(
            f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}"
            f"  as_of: {as_of}  代码过滤: {', '.join(codes) if codes else '无（全部 A+H）'}"
            f"  H 股红利税率: {args.tax_rate}"
        )

    rows = build_ah_snapshot(
        duck, as_of=as_of, stock_codes=codes or None, tax_rate=args.tax_rate,
    )
    fetch_time = datetime.now(UTC).replace(tzinfo=None)
    params = [row_to_params(row, as_of=as_of, fetch_time=fetch_time) for row in rows]
    stats = summarize(rows, params)

    backup_path: Path | None = None
    written = 0
    if write:
        backup_dir = (
            Path(args.backup_dir)
            if args.backup_dir
            else PROJECT_ROOT / ".planning"
            / f"maintenance-backup-ah-market-snapshot-{datetime.now():%Y%m%d_%H%M%S}"
        )
        try:
            # exclusive_update：跨进程单写者（与 vd data update 同一条锁）。
            # 备份与写入放在同一个互斥窗口里，避免"备份后被别人改一轮"。
            with exclusive_update(paths.duckdb_path):
                try:
                    backup_path = _backup_table(duck, Path(backup_dir))
                except Exception as error:  # noqa: BLE001 - 备份失败必须中止写入
                    print(f"[ABORT] 写前备份失败，已中止：{error}", file=sys.stderr)
                    return 5
                with duck.transaction() as conn:
                    conn.executemany(UPSERT_SQL, params)
                written = len(params)
        except UpdateLockError as error:
            print(f"[ABORT] 另一个更新进程持有写锁：{error}", file=sys.stderr)
            return 3

    payload = {
        "task": f"刷新 A+H 市值快照 → {TABLE}",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": not write,
        "database": str(paths.duckdb_path),
        "as_of": as_of.isoformat(),
        "codes": codes,
        "tax_rate": args.tax_rate,
        "table_ready": has_table,
        "written_rows": written,
        "backup_path": str(backup_path) if backup_path else None,
        **stats,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"\n== {TABLE} @ {as_of} =="
            f"\n   计算行数              {stats['rows']}"
            f"\n   有 H 股市值           {stats['rows_with_h_market_cap']}"
            f"\n   缺 H 股市值           {stats['rows_missing_h_market_cap']}"
            f"\n   H 股市值合计(元)      {stats['h_market_cap_cny_sum']}"
            f"\n   缺数据原因分布        {stats['reasons'] or '无'}"
            f"\n   恒等式状态分布        {stats['identity_status']}"
        )
        if write:
            print(f"\n已写入 {TABLE}：{written} 行（幂等 upsert，主键 stock_code+as_of）")
        else:
            tail = "" if has_table else "；且目标表尚未创建"
            print(f"\n[DRY RUN] 预计写入 {TABLE}：{stats['rows']} 行（加 --yes 才会写{tail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
