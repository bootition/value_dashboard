"""补采扣非净利润（`income_statement.deducted_net_profit`）—— 安全版。

为什么需要它
------------
`deducted_net_profit` 是扣非同比 / 扣非 3·5 年复合增长三项指标的输入。
历史段由外部数据包导入（≤2025-03-31），之后只能靠补丁脚本
`scripts/patch_deducted_profit.py` —— 但那个脚本会
`DELETE FROM income_statement WHERE report_date > '2025-03-31'` 再从 CSV 回灌，
**一旦 CSV 不全会把三表打空**，不可在生产环境执行。

本脚本走 `app/core/supplement.py` 的声明式补采通道：
- 数据源：东方财富 F10 `lrbAjaxNew`（实测字段 `DEDUCT_PARENT_NETPROFIT`，
  2026-09-30 贵州茅台 = 64,324,824,599.18）；
- **只 UPDATE 既有行里为 NULL 的列**，绝不 INSERT / DELETE / 覆盖既有值；
- 续传判据是"目标报告期内该列仍为 NULL 的股票"，补到即出队；
- 写前 Parquet 备份 + 单写者锁。

用法
----
    python scripts/fetch_deducted_profit.py                 # dry-run，抽样 5 只
    python scripts/fetch_deducted_profit.py --limit 500     # 只跑 500 只
    python scripts/fetch_deducted_profit.py --yes --concurrency 8
"""

from __future__ import annotations

import argparse
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
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402
from app.core.supplement import (  # noqa: E402
    DEDUCTED_PROFIT,
    SPECS,
    SupplementFetcher,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="补采扣非净利润（只填 NULL，不做替换）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    ap.add_argument("--concurrency", type=int, default=8, help="并发数（源侧限速，建议 ≤8）")
    ap.add_argument("--sample", type=int, default=5, help="dry-run 时抽样只数")
    ap.add_argument("--deadline", type=float, default=0.0, help="本轮最大耗时秒数（0=不限）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    fetcher = SupplementFetcher(duck=duck)
    spec = SPECS[DEDUCTED_PROFIT]
    periods = fetcher.resolve_periods(DEDUCTED_PROFIT)
    logger.info("目标报告期（最近 %d 期）: %s", len(periods), periods)
    pending = fetcher.pending_codes(DEDUCTED_PROFIT, periods, max_stocks=args.limit)
    logger.info("待补股票 %d 只（判据：目标期内 deducted_net_profit 为 NULL）", len(pending))

    if not args.yes:
        for code, exchange in pending[: max(0, args.sample)]:
            rows = fetcher._fetch_rows(spec, code, exchange, periods, timeout=20)
            parsed = fetcher._parse_rows(
                spec, code, rows,
                batch_id="dry",
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
                src_hash="dry",
            )
            rows_written = parsed.get("income_statement", [])
            logger.info("  %s → income_statement %d 行", code, len(rows_written))
        logger.info("[DRY RUN] 未写库；加 --yes 执行（预计 %d 只）", len(pending))
        return

    if not pending:
        logger.info("没有需要补的股票，退出。")
        return

    backup_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-deducted-profit-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup_dir / "income_statement.parquet").replace("'", "''")
        conn.execute(f"COPY income_statement TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 已备份 income_statement → %s", backup_dir)

    report = fetcher.refresh(
        DEDUCTED_PROFIT,
        periods=periods,
        max_stocks=args.limit,
        concurrency=args.concurrency,
        deadline_seconds=args.deadline,
    )
    logger.info("写入完成：updated=%s failed=%s rows=%s status=%s",
                report.get("updated"), report.get("failed"),
                report.get("rows_written"), report.get("status"))


if __name__ == "__main__":
    main()
