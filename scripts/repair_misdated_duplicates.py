"""修复两类数据缺陷（2026-09-19 数据库复审发现）。

缺陷 1：三表 20.6 万行 report_date 被记为「次年 1 月 1 日」
--------------------------------------------------------
现象：`balance_sheet` 71,860 / `income_statement` 71,859 / `cash_flow` 62,370 行
report_date 为 YYYY-01-01（report_type 全为 'quarterly'、raw_data 全为 NULL）。
核实：这些行是**同一份年报的第二个来源副本**，日期被错记成次年 1 月 1 日 ——
92.6% 的数值与「前一年 12-31」行相同或仅有重述级微小差异
（例：000001 的 2000-01-01 值 = 1999-12-31 值）。
修复：
  · 同 (股票, 年) 已存在正确 12-31 行 → **删除** 01-01 错位行；
  · 无 12-31 对应行（589 行，多为 1990 年代上市初期）→ **改期** 为 12-31，
    保留数据不丢失。

缺陷 2：`price_daily_raw` 10 万行股票代码未补零
------------------------------------------------
现象：13 只股票被存为 '565'/'1914' 等 3~4 位代码（应 '000565'/'001914'）。
核实：与补零版本**同日数值完全相同**，且 100% 被补零版本按日期覆盖 —— 纯重复。
修复：删除。

安全：写前全量备份受影响表；全程单写者锁；支持 dry-run。

用法:
    python scripts/repair_misdated_duplicates.py            # dry-run
    python scripts/repair_misdated_duplicates.py --yes
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

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active, exclusive_update  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

TABLES = ("balance_sheet", "income_statement", "cash_flow")
MISDATED = "EXTRACT(month FROM report_date)=1 AND EXTRACT(day FROM report_date)=1"
HAS_CORRECT = """EXISTS (SELECT 1 FROM {t} x WHERE x.stock_code = {t}.stock_code
    AND x.report_date = make_date(EXTRACT(year FROM {t}.report_date)::INT, 12, 31))"""


def main() -> None:
    ap = argparse.ArgumentParser(description="修复错位日期副本与未补零股票代码")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    from app.core.storage.schema import init_duckdb_schema
    init_duckdb_schema(duck)

    plan: dict[str, dict[str, int]] = {}
    with duck.read_connection() as conn:
        for t in TABLES:
            n_del = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {MISDATED} AND {HAS_CORRECT.format(t=t)}").fetchone()[0]
            n_fix = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {MISDATED} AND NOT ({HAS_CORRECT.format(t=t)})").fetchone()[0]
            plan[t] = {"delete_misdated": n_del, "redate_misdated": n_fix}
        plan["price_daily_raw"] = {"delete_short_code": conn.execute(
            "SELECT COUNT(*) FROM price_daily_raw WHERE LENGTH(stock_code) < 6").fetchone()[0]}

    logger.info("修复计划:")
    for t, v in plan.items():
        logger.info("  %-20s %s", t, v)
    if not args.yes:
        logger.info("[DRY RUN] 未执行")
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-repair-dup-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for t in (*TABLES, "price_daily_raw"):
            target = str(backup / f"{t}.parquet").replace("'", "''")
            conn.execute(f"COPY {t} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 受影响表已全量备份到 %s", backup)

    done: dict[str, dict[str, int]] = {}
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t} WHERE {MISDATED} AND {HAS_CORRECT.format(t=t)}")
            conn.execute(
                f"UPDATE {t} SET report_date = make_date(EXTRACT(year FROM report_date)::INT, 12, 31) "
                f"WHERE {MISDATED}"
            )
            done[t] = plan[t]
        conn.execute("DELETE FROM price_daily_raw WHERE LENGTH(stock_code) < 6")
        done["price_daily_raw"] = plan["price_daily_raw"]

    evidence = {"task": "修复错位日期副本与未补零股票代码",
                "executed_at": datetime.now(UTC).isoformat(),
                "plan": plan, "backup_dir": str(backup)}
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r17_repair_misdated_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("修复完成；证据: %s", ev_path)


if __name__ == "__main__":
    main()
