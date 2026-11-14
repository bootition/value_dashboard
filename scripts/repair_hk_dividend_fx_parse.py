"""修复 hk_dividends 里「把汇率当股息」的历史脏数据（2026-11-14 审查发现）。

缺陷
----
东财港股分红方案串里常把换算汇率写进括号，例如：

    每股派人民币0.07元(汇率港币1.00元:人民币0.817026元)(相当于港币0.0856元)

旧解析器用 `_PAREN_HKD_RE.search()` 从左到右扫，先撞上汇率里的「港币1.00元」，
于是把**汇率**当成港币股息写进 `dividend_per_share_hkd`：

    02238  真实 0.0856 港币  →  库里记成 1.0   （虚高约 11.7 倍）
    02880  真实 0.0627 港币  →  库里记成 1.0

实测 89 行 `plan_explain` 含「汇率」字样，其中多数港币股息被写成 1.0。
解析器已修（`app/core/adapters/hk_dividend_adapter.py`，先剥离汇率注记再匹配，
并优先「相当于港币 X 元」），本脚本用同一解析器**从已存的 plan_explain 重算**，
不需要联网、不需要重抓。

纪律
----
- 默认 dry-run；写库需 `--yes`。
- 写前把 `hk_dividends` 全表备份为 Parquet（放 `data/backup_accept/` 同级维护目录）。
- 全程单写者锁；有自动更新在跑时拒绝执行。
- 只改 `dividend_per_share_hkd` / `dividend_per_share_cny` 两列，且**只改有变化**的行。
- 不猜数：解析不出（None, None）的行保持原值不动，只记日志。

用法
----
    python scripts/repair_hk_dividend_fx_parse.py            # dry-run
    python scripts/repair_hk_dividend_fx_parse.py --yes      # 写库
    python scripts/repair_hk_dividend_fx_parse.py --all      # 不限「含汇率」的行，全表重算比对
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.adapters.hk_dividend_adapter import parse_hk_plan_explain  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def _same(left: float | None, right: float | None) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(left - right) <= max(1e-9, abs(right) * 1e-9)


def main() -> None:
    parser = argparse.ArgumentParser(description="修复 hk_dividends 汇率被当股息的历史脏数据")
    parser.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    parser.add_argument("--all", action="store_true", help="不限「含汇率」的行，全表重算比对")
    args = parser.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    duck = DuckDBStore(paths=paths)
    where = "" if args.all else "WHERE plan_explain LIKE '%汇率%'"
    rows = duck.read_query(
        f"""SELECT stock_code, ex_date, plan_explain,
                   dividend_per_share_hkd, dividend_per_share_cny
            FROM hk_dividends {where}
            ORDER BY stock_code, ex_date"""
    )
    logger.info("待核对 %d 行（范围：%s）", len(rows), "全表" if args.all else "含「汇率」的行")

    changes: list[tuple[float | None, float | None, str, str, object]] = []
    unresolved = 0
    for row in rows:
        hkd, cny = parse_hk_plan_explain(row.get("plan_explain"))
        if hkd is None and cny is None:
            unresolved += 1
            continue
        old_hkd = row.get("dividend_per_share_hkd")
        old_cny = row.get("dividend_per_share_cny")
        if _same(old_hkd, hkd) and _same(old_cny, cny):
            continue
        changes.append((hkd, cny, str(row["stock_code"]), str(row["ex_date"]), (old_hkd, old_cny)))

    logger.info("需要修正 %d 行；解析不出（保持原值）%d 行；无变化 %d 行",
                len(changes), unresolved, len(rows) - unresolved - len(changes))
    for hkd, cny, code, ex_date, old in changes[:20]:
        logger.info("  %s %s  hkd %s → %s ; cny %s → %s",
                    code, ex_date, old[0], hkd, old[1], cny)
    if len(changes) > 20:
        logger.info("  ... 其余 %d 行略", len(changes) - 20)

    if not changes:
        logger.info("无需修复")
        return

    if not args.yes:
        logger.info("[DRY RUN] 未写库；确认后加 --yes 执行")
        return

    backup_dir = paths.duckdb_path.parent / "backup_accept"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"hk_dividends_before_fx_repair_{stamp}.parquet"

    try:
        with exclusive_update(paths.duckdb_path):
            with duck.read_connection() as conn:
                conn.execute(
                    f"COPY (SELECT * FROM hk_dividends) TO '{backup_path.as_posix()}' (FORMAT PARQUET)"
                )
            logger.info("已备份 hk_dividends → %s", backup_path)

            with duck.write_connection() as conn:
                conn.execute("BEGIN TRANSACTION")
                try:
                    for hkd, cny, code, ex_date, _old in changes:
                        conn.execute(
                            """UPDATE hk_dividends
                                  SET dividend_per_share_hkd = ?, dividend_per_share_cny = ?
                                WHERE stock_code = ? AND ex_date = CAST(? AS DATE)""",
                            [hkd, cny, code, ex_date],
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    logger.info("修复完成：%d 行", len(changes))
    logger.info(json.dumps({
        "action": "repair_hk_dividend_fx_parse",
        "changed_rows": len(changes),
        "backup": str(backup_path),
        "finished_at": datetime.now(UTC).isoformat(),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
