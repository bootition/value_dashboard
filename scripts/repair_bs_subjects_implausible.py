"""清理 `balance_sheet` 补采科目的两类脏值（一次性维护脚本）。

背景（`reports/139` 第二轮对抗性验收 P1）
------------------------------------------
`scripts/fetch_balance_sheet_subjects.py` 的第一版有两个问题，已落库一批脏值：

1. **推导累计折旧**：银行/保险模板没有「累计折旧」列时，脚本用 `原值 − 固定资产净值`
   填补。但那个差里混着**固定资产减值准备**（库内没有该科目），实测 2,515/4,403 行
   勾稽不闭合；而且它不是会计数（应有值就是 NULL）。
2. **原值 < 净值**：17 行出现 `原值 − 净值 < −1 元`（最差 600036 2023-06-30 差 −840.32 亿），
   说明银行模板的「固定资产原值」与通用模板不是同一口径 → 整组不可用。

修法（本脚本）
--------------
- 银行/非银金融（`sw_level1`）的 `accumulated_depreciation` 一律置 NULL（模板无此列）；
- 全表 `fixed_assets_gross < fixed_assets − 1` 的行，`fixed_assets_gross` 与
  `accumulated_depreciation` 一并置 NULL（口径不可信）；
- 两类都写 `indicator_null_reason`（`SOURCE_EMPTY` / `NOT_APPLICABLE`），不留无声 NULL。

用法
----
    python scripts/repair_bs_subjects_implausible.py            # dry-run（默认）
    python scripts/repair_bs_subjects_implausible.py --yes      # 真写（单写者锁）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from app.core.financial_period import cn_today
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.update_lock import exclusive_update

# 1) 银行/非银金融：模板无「累计折旧」列 → 该列一律不可信
SQL_BANK_ACCUM_COUNT = """
SELECT COUNT(*) AS n
FROM balance_sheet b
JOIN stock_meta m USING (stock_code)
WHERE b.accumulated_depreciation IS NOT NULL
  AND COALESCE(m.sw_level1, '') IN ('银行', '非银金融')
"""

SQL_BANK_ACCUM_APPLY = """
UPDATE balance_sheet AS b
SET accumulated_depreciation = NULL
FROM stock_meta m
WHERE m.stock_code = b.stock_code
  AND b.accumulated_depreciation IS NOT NULL
  AND COALESCE(m.sw_level1, '') IN ('银行', '非银金融')
"""

# 2) 原值 < 净值 − 1 元：两列口径不可信 → 一并置 NULL
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


def main() -> int:
    parser = argparse.ArgumentParser(description="清理补采科目脏值（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="真正写库（需要单写者锁）")
    parser.add_argument("--json-out", default="", help="把证据 JSON 写到指定路径")
    args = parser.parse_args()

    paths = DatabasePathSet.from_env()
    store = DuckDBStore(paths=paths)
    evidence: dict[str, object] = {
        "mode": "WRITE" if args.yes else "DRY-RUN",
        "database": str(paths.duckdb_path),
        "planned_at": datetime.now(UTC).isoformat(),
        "report_date_coverage": str(cn_today()),
    }
    try:
        before_bank = store.read_query(SQL_BANK_ACCUM_COUNT)[0]["n"]
        before_impl = store.read_query(SQL_IMPLAUSIBLE_COUNT)[0]["n"]
        evidence["before"] = {
            "bank_accumulated_depreciation_rows": int(before_bank),
            "implausible_gross_lt_net_rows": int(before_impl),
        }
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        if not args.yes:
            print("[dry-run] 未写库；加 --yes 执行")
            return 0
        if before_bank == 0 and before_impl == 0:
            print("[repair] 无需修改")
            return 0

        with exclusive_update(paths.duckdb_path):
            write_store = DuckDBStore(paths=paths)
            with write_store.transaction() as conn:
                conn.execute(SQL_BANK_ACCUM_APPLY)
                conn.execute(SQL_IMPLAUSIBLE_APPLY)
            after_bank = write_store.read_query(SQL_BANK_ACCUM_COUNT)[0]["n"]
            after_impl = write_store.read_query(SQL_IMPLAUSIBLE_COUNT)[0]["n"]
        evidence["after"] = {
            "bank_accumulated_depreciation_rows": int(after_bank),
            "implausible_gross_lt_net_rows": int(after_impl),
        }
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(evidence, handle, ensure_ascii=False, indent=2)
        return 0 if (after_bank == 0 and after_impl == 0) else 2
    finally:
        pass  # DuckDBStore 无 close()；连接由内部池管理


if __name__ == "__main__":
    sys.exit(main())
