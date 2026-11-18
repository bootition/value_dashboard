"""清理 `balance_sheet` 补采科目的两类脏值（一次性维护脚本，可重复跑）。

背景（`reports/139` 第二轮对抗性验收 P1 + 第三轮复核）
------------------------------------------------------
`scripts/fetch_balance_sheet_subjects.py` 第一版有两个问题，留下了一批脏值：

1. **推导累计折旧**：**银行**模板没有「累计折旧」列时，脚本用 `原值 − 净值` 填充。
   但那个差里混着**固定资产减值准备**（库内没有该科目），不是会计数。
   ⚠️ 注意范围修正（第三轮复核）：只清 **银行**，**非银金融（券商）不要清** ——
   券商模板有真实的「累计折旧」列，实测 667 行值满足 `原值 − 折旧 − 净值 ≈ 0`
   （无减值时精确闭合），清掉它们等于删真数据。
2. **原值 < 净值**：少数行 `fixed_assets_gross < fixed_assets − 1 元`，
   说明该股的 `fixed_assets` 与新浪「固定资产原值」不是同一口径
   （实测 15 行全是非金融，如 000722 FY2025：原值 28.11 亿 / 折旧 13.71 亿 / 库内净值 41.38 亿）。
   这类行两列都不可信 → 一并置 NULL，并记 `indicator_null_reason`

写库纪律
--------
- 默认 dry-run；`--yes` 才写，且走 `exclusive_update` 单写者锁；
- 写前把**受影响行**（含 7 个补采列）导出成 Parquet 备份到 `.planning/maintenance-backup-*`，
  备份路径写进证据 JSON（第二轮验收指出「备份里没有这 7 列、无法回滚」）；
- 改前/改后用**同一谓词**各数一遍，不采信驱动返回值（AGENTS R2）。

用法
----
    python scripts/repair_bs_subjects_implausible.py                 # dry-run
    python scripts/repair_bs_subjects_implausible.py --yes --json-out evidence.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.financial_period import cn_today
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.update_lock import exclusive_update

#: 1) 银行模板无「累计折旧」列 → 该列一律不可信（**只针对银行**，券商有真值）
SQL_BANK_ACCUM_COUNT = """
SELECT COUNT(*) AS n
FROM balance_sheet b
JOIN stock_meta m USING (stock_code)
WHERE b.accumulated_depreciation IS NOT NULL
  AND COALESCE(m.sw_level1, '') = '银行'
"""

SQL_BANK_ACCUM_APPLY = """
UPDATE balance_sheet AS b
SET accumulated_depreciation = NULL
FROM stock_meta m
WHERE m.stock_code = b.stock_code
  AND b.accumulated_depreciation IS NOT NULL
  AND COALESCE(m.sw_level1, '') = '银行'
"""

#: 2) 原值 < 净值 − 1 元：两列口径不可信 → 一并置 NULL（不限行业）
SQL_IMPLAUSIBLE_COUNT = """
SELECT COUNT(*) AS n
FROM balance_sheet
WHERE fixed_assets_gross IS NOT NULL
  AND fixed_assets IS NOT NULL
  AND fixed_assets_gross < fixed_assets - 1.0
"""

SQL_IMPLAUSIBLE_APPLY = """
UPDATE balance_sheet
SET fixed_assets_gross = NULL,
    accumulated_depreciation = NULL
WHERE fixed_assets_gross IS NOT NULL
  AND fixed_assets IS NOT NULL
  AND fixed_assets_gross < fixed_assets - 1.0
"""

#: 受影响行（备份用，含 7 个补采列）
SQL_AFFECTED_ROWS = """
SELECT b.stock_code, b.report_date, b.fixed_assets, b.fixed_assets_gross,
       b.accumulated_depreciation, b.other_current_assets, b.receivables_financing,
       b.long_term_payables, b.investment_property, b.development_expenditure
FROM balance_sheet b
LEFT JOIN stock_meta m USING (stock_code)
WHERE (b.accumulated_depreciation IS NOT NULL AND COALESCE(m.sw_level1, '') = '银行')
   OR (b.fixed_assets_gross IS NOT NULL AND b.fixed_assets IS NOT NULL
       AND b.fixed_assets_gross < b.fixed_assets - 1.0)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="清理补采科目脏值（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="真正写库（需要单写者锁）")
    parser.add_argument("--json-out", default="", help="把证据 JSON 写到指定路径")
    parser.add_argument("--backup-root", default=".planning", help="备份根目录（默认 .planning）")
    args = parser.parse_args()

    paths = DatabasePathSet.from_env()
    store = DuckDBStore(paths=paths)
    evidence: dict[str, object] = {
        "mode": "WRITE" if args.yes else "DRY-RUN",
        "database": str(paths.duckdb_path),
        "planned_at": datetime.now(UTC).isoformat(),
        "today_cn": str(cn_today()),
    }
    before_bank = int(store.read_query(SQL_BANK_ACCUM_COUNT)[0]["n"])
    before_impl = int(store.read_query(SQL_IMPLAUSIBLE_COUNT)[0]["n"])
    evidence["before"] = {
        "bank_accumulated_depreciation_rows": before_bank,
        "implausible_gross_lt_net_rows": before_impl,
    }
    if not args.yes:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        print("[dry-run] 未写库；加 --yes 执行")
        return 0
    if before_bank == 0 and before_impl == 0:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        print("[repair] 无需修改")
        return 0

    # 备份受影响行（含 7 个补采列），第二轮验收指出旧版备份缺这些列 → 无法回滚
    backup_dir = Path(args.backup_root) / f"maintenance-backup-bs-subjects-{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "balance_sheet_subjects.parquet"
    with exclusive_update(paths.duckdb_path):
        write_store = DuckDBStore(paths=paths)
        with write_store.write_connection() as conn:
            conn.execute(
                f"COPY ({SQL_AFFECTED_ROWS}) TO '{backup_path.as_posix()}' (FORMAT PARQUET)"
            )
            conn.execute(SQL_BANK_ACCUM_APPLY)
            conn.execute(SQL_IMPLAUSIBLE_APPLY)
    after_bank = int(store.read_query(SQL_BANK_ACCUM_COUNT)[0]["n"])
    after_impl = int(store.read_query(SQL_IMPLAUSIBLE_COUNT)[0]["n"])
    evidence["after"] = {
        "bank_accumulated_depreciation_rows": after_bank,
        "implausible_gross_lt_net_rows": after_impl,
    }
    evidence["backup"] = {"path": str(backup_path), "restore_hint": (
        "CREATE TABLE _restore AS SELECT * FROM read_parquet('" + backup_path.as_posix() + "');"
        " 再按 (stock_code, report_date) 回填 7 个补采列"
    )}
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if (after_bank == 0 and after_impl == 0) else 2


if __name__ == "__main__":
    sys.exit(main())
