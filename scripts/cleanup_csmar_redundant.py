"""清理 CSMAR 导入中的纯冗余（2026-09-19，"榨干"纠偏）。

背景
----
上一轮为追求"100% 导入"把 845 个字段全量入库，但其中 **223 个字段是同一概念的
不同算法变体**（如「净资产收益率」有 A/B/C/TTM 四种算法、「托宾Q值」有 A/B/C/D），
平均每个概念被算 1.4 遍，FI_T4 更是 67 字段只有 18 个概念（3.7 倍冗余）。
用户指出：**导入 ≠ 有价值；无价值的冗余不应进库**。本脚本据此清理。

清理原则（只删「同一概念的次要与变体算法」，不删任何独有概念）
------------------------------------------------------------
1. **整表删除**：`csmar_aiq_annual`（54 列全部与 FI_T* 重叠，无一独有）、
   `csmar_fi_t7`（3 列杠杆，本项目已自算且三色灯全绿）。
2. **变体裁剪**：按「概念」分组，每个概念只保留主口径（CSMAR 列序中靠前者，
   通常为 A 式），删除其余 A/B/C/D/TTM 变体列。

保留原则
--------
- 每个概念的**主口径全部保留**（交叉核验价值）；
- 所有**独有概念**（我们没有的）全部保留；
- 三表未映射科目、金融专用科目、风险因子、披露指标一律不动。

脚本可从已提交的 import 脚本一键回滚（重跑 import_csmar_tables_full.py 等）。

用法:
    python scripts/cleanup_csmar_redundant.py            # dry-run
    python scripts/cleanup_csmar_redundant.py --yes
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

PLAN_PATH = Path("/tmp/cleanup_plan.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="清理 CSMAR 纯冗余导入")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--plan", type=Path, default=PLAN_PATH, help="清理计划 JSON（由 .planning 的分析脚本生成）")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    drop_tables: dict[str, str] = plan["drop_tables"]
    drop_columns: dict[str, list[str]] = plan["drop_columns"]

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    logger.info("计划：删 %d 张表 / 裁剪 %d 列（%d 张表）",
                len(drop_tables), sum(len(v) for v in drop_columns.values()), len(drop_columns))
    if not args.yes:
        for t, why in drop_tables.items():
            logger.info("  DROP TABLE %s（%s）", t, why)
        for t, cols in drop_columns.items():
            logger.info("  %s: 删 %d 列，例: %s", t, len(cols), ", ".join(cols[:4]))
        logger.info("[DRY RUN] 未执行")
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-cleanup-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for t in drop_tables:
            target = str(backup / f"{t}.parquet").replace("'", "''")
            conn.execute(f"COPY {t} TO '{target}' (FORMAT PARQUET)")
        for t in drop_columns:
            target = str(backup / f"{t}.parquet").replace("'", "''")
            conn.execute(f"COPY {t} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 受影响表已全量备份到 %s（可完整回滚）", backup)

    removed_cols = 0
    # DuckDB 限制：表上存在索引时不允许 DROP COLUMN（DependencyException）。
    # 且项目知识库 **D2** 记载：DROP INDEX 与 CREATE INDEX 在**同一事务**内会触发
    # DuckDB 1.5.5 的 FATAL bug（BoundIndex::CreateDeltaIndex）。故分三个事务：
    #   ① 删索引 → ② 删列/删表 → ③ 重建索引
    with exclusive_update(paths.duckdb_path):
        with duck.write_connection() as conn:                     # ① 删索引
            for t in drop_columns:
                conn.execute(f'DROP INDEX IF EXISTS "idx_{t}_stock"')
        for t, cols in drop_columns.items():                       # ② 删列（独立事务）
            with duck.write_connection() as conn:
                existing = {c[0] for c in conn.execute(f'DESCRIBE "{t}"').fetchall()}
                for col in cols:
                    if col in existing:
                        conn.execute(f'ALTER TABLE "{t}" DROP COLUMN "{col}"')
                        removed_cols += 1
        with duck.write_connection() as conn:                      # ② 删表
            for t in drop_tables:
                conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        with duck.write_connection() as conn:                      # ③ 重建索引
            for t in drop_columns:
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "idx_{t}_stock" ON "{t}" (stock_code, report_date)'
                )

    evidence = {
        "task": "清理 CSMAR 纯冗余导入（同概念算法变体）",
        "executed_at": datetime.now(UTC).isoformat(),
        "dropped_tables": list(drop_tables), "dropped_columns": removed_cols,
        "kept_tables": len(drop_columns), "backup_dir": str(backup),
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r16_csmar_cleanup_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("清理完成：删表 %d 张 / 删列 %d 个；证据: %s", len(drop_tables), removed_cols, ev_path)


if __name__ == "__main__":
    main()
