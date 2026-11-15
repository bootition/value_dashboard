"""清理 `income_statement.exchange_gain` 的口径污染（2026-11-15，reports/121）。

背景
----
本列的历史语义是「公允价值变动收益」（CSMAR B001301000、`import_csmar.py`、
`supplement_akshare.py` 三处一致）。但新浪适配器把「汇兑收益」也映射进了同一列
（`sina_adapter.py:133`，已于 2026-11-15 修正），导致：

- 2025-06-30 ~ 2026-03-31 之间约 52% 的覆盖率来自「公允价值变动收益」；
- 之后的覆盖率掉到 3.5%，且 **1,428 行** 里塞的是「汇兑收益」；
- 空值清扫器把这 5,381 个 NULL 判成 `NOT_DISPATCHED`（"没去抓"），其实是口径打架。

做法
----
把这 1,428 行的 `exchange_gain` 置 NULL（**只清这一列，不动其它字段**），
然后由 `scripts/fetch_exchange_gain.py` 用东财 `FAIRVALUE_CHANGE_INCOME`
按"只填 NULL"的纪律重新补。识别依据：`raw_data` 里出现 `"exchange_gain"` 键
（只有新浪那条映射会写这个键；CSMAR/akshare 批次不带）。

用法
----
    python scripts/clean_exchange_gain_pollution.py            # dry-run
    python scripts/clean_exchange_gain_pollution.py --yes      # 写库（备份 + 锁）
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

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
    exclusive_update,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

CUTOFF = "2025-03-31"


def main() -> None:
    ap = argparse.ArgumentParser(description="清理 exchange_gain 的汇兑收益污染")
    ap.add_argument("--yes", action="store_true", help="确认写库")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    rows = duck.read_query(
        """SELECT report_date, COUNT(*) AS n
           FROM income_statement
           WHERE raw_data LIKE '%"exchange_gain"%'
             AND report_date >= CAST(? AS DATE)
             AND exchange_gain IS NOT NULL
           GROUP BY 1 ORDER BY 1""",
        [CUTOFF],
    )
    total = sum(int(row["n"]) for row in rows)
    logger.info("拟清理 %d 行（按报告期：%s）", total, {str(r["report_date"])[:10]: int(r["n"]) for r in rows})
    if not total:
        logger.info("无需清理")
        return
    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行")
        return

    backup_dir = PROJECT_ROOT / ".planning" / f"maintenance-backup-exchange-gain-{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / "income_statement_exchange_gain.parquet").replace("'", "''")
    try:
        with exclusive_update(paths.duckdb_path):
            with duck.read_connection() as conn:
                conn.execute(
                    f"""COPY (SELECT stock_code, report_date, exchange_gain, raw_data
                             FROM income_statement
                             WHERE raw_data LIKE '%"exchange_gain"%'
                               AND report_date >= CAST('{CUTOFF}' AS DATE))
                        TO '{target}' (FORMAT PARQUET)"""
                )
            with duck.write_connection() as conn:
                conn.execute(
                    """UPDATE income_statement SET exchange_gain = NULL
                       WHERE raw_data LIKE '%"exchange_gain"%'
                         AND report_date >= CAST(? AS DATE)
                         AND exchange_gain IS NOT NULL""",
                    [CUTOFF],
                )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error
    logger.info("清理完成：%d 行置 NULL，备份 %s", total, backup_dir)


if __name__ == "__main__":
    main()
