"""补采固定资产净额（`balance_sheet.fixed_assets`）—— 安全版。

为什么需要它
------------
2026Q3 主链 `balance_sheet.fixed_assets` 只剩 102/5,575（1.8%），而上期 99.9%：
新浪 Q3 返回的资产负债表里没有「固定资产净额」这一项（抽 000408 raw_data：
Q2 有、Q3 无）。该字段是 `fixed_asset_turnover` / `fixed_asset_ratio` 的输入，
也是 `balance.fixed_assets` 筛选字段。

本脚本走 `app/core/supplement.py` 的声明式补采通道：
- 数据源：东方财富 F10 `zcfzbAjaxNew`（字段 `FIXED_ASSET`，
  2026-09-30 000408 = 1,950,024,052.03）；
- **只 UPDATE 既有行里为 NULL 的列**，绝不 INSERT / DELETE / 覆盖既有值；
- 续传判据是"目标报告期内该列仍为 NULL 的股票"，补到即出队；
- 写前 Parquet 备份 + 单写者锁。

用法
----
    python scripts/fetch_fixed_assets.py                 # dry-run，抽样 5 只
    python scripts/fetch_fixed_assets.py --limit 500     # 只跑 500 只
    python scripts/fetch_fixed_assets.py --yes --concurrency 8
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
    FIXED_ASSET_SUPPLEMENT,
    SPECS,
    SupplementFetcher,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="补采固定资产净额（只填 NULL，不做替换）")
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
    spec = SPECS[FIXED_ASSET_SUPPLEMENT]
    periods = fetcher.resolve_periods(FIXED_ASSET_SUPPLEMENT)
    logger.info("目标报告期（最近 %d 期）: %s", len(periods), periods)
    pending = fetcher.pending_codes(FIXED_ASSET_SUPPLEMENT, periods, max_stocks=args.limit)
    logger.info("待补股票 %d 只（判据：目标期内 fixed_assets 为 NULL）", len(pending))

    if not args.yes:
        for code, exchange in pending[: max(0, args.sample)]:
            rows = fetcher._fetch_rows(spec, code, exchange, periods, timeout=20)
            parsed = fetcher._parse_rows(
                spec, code, rows,
                batch_id="dry",
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
                src_hash="dry",
            )
            rows_written = parsed.get("balance_sheet", [])
            logger.info("  %s → balance_sheet %d 行", code, len(rows_written))
        logger.info("[DRY RUN] 未写库；加 --yes 执行（预计 %d 只）", len(pending))
        return

    if not pending:
        logger.info("没有需要补的股票，退出。")
        return

    backup_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-fixed-assets-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup_dir / "balance_sheet.parquet").replace("'", "''")
        conn.execute(f"COPY balance_sheet TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 已备份 balance_sheet → %s", backup_dir)

    report = fetcher.refresh(
        FIXED_ASSET_SUPPLEMENT,
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
