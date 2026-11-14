"""补取资产负债表补充科目 —— CLI 薄包装。

核心逻辑已抽到 `app/core/supplement.py`（与自动更新主流程共用同一份实现，
`2026-11-14 reports/119 D29` 自动接入）。本脚本只保留 CLI 语义：
dry-run 抽样、写前备份、单写者锁、证据 JSON。

为什么需要这个域
----------------
主链 `balance_sheet` 未映射这三个科目，但它们各自解锁一项**无法自算**的能力：

- `other_payables` 其他应付款 → 与已有的 `other_receivables` 配对，
  可算「大股东占款」= (其他应收款 − 其他应付款) / 总资产 —— 治理红旗指标；
- `non_current_liab_due_1y` 一年内到期的非流动负债 → 修正有息负债口径
  （主链 `interest_bearing_debt` 目前仅 短借+长借+应付债券，系统性低估）；
- `total_other_receivable` 其他应收款合计 → 与应付侧同源配对，
  避免主链 `other_receivables`（净额）与应付口径不匹配。

⚠️ 续传判据已修正（2026-11-14）
------------------------------
旧版本（原第 175-177 行）与 `fetch_cashflow_supplement.py` 是同一个 bug：
「本表有任意 `source='eastmoney_f10'` 行就跳过」→ 第一次跑完后永远补不到
新报告期。现在判据是「本源最新报告期 < 目标最新报告期」（见 `SupplementFetcher`）。

用法（与旧版完全一致）
----------------------
    python scripts/fetch_balance_sheet_ext.py              # dry-run
    python scripts/fetch_balance_sheet_ext.py --yes        # 全量
    python scripts/fetch_balance_sheet_ext.py --yes --concurrency 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
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
    BALANCE_SHEET_EXT,
    SPECS,
    SupplementFetcher,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="补取资产负债表补充科目")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--sample", type=int, default=3)
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
    init_duckdb_schema(duck)   # 确保 v27 的 balance_sheet_ext 存在（dry-run 也会读它）

    spec = SPECS[BALANCE_SHEET_EXT]
    fetcher = SupplementFetcher(duck=duck)
    periods = fetcher.resolve_periods(BALANCE_SHEET_EXT)
    logger.info("目标报告期: %s", periods)
    pending_all = fetcher.pending_codes(BALANCE_SHEET_EXT, periods)
    logger.info("待补股票 %d 只（判据：本源最新报告期 < %s）", len(pending_all), periods[0])

    if not args.yes:
        for code, exchange in fetcher.pending_codes(
            BALANCE_SHEET_EXT, periods, max_stocks=max(0, args.sample),
        ):
            rows = fetcher._fetch_rows(spec, code, exchange, periods, timeout=20)
            parsed = fetcher._parse_rows(
                spec, code, rows,
                batch_id="dry",
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
                src_hash="dry",
            )
            logger.info("  %s(%s) → %d 行", code, exchange, len(parsed["balance_sheet_ext"]))
            time.sleep(0.5)
        logger.info("[DRY RUN] 未写库")
        return

    if not pending_all:
        logger.info("没有需要补的股票，退出。")
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-bs-ext-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "balance_sheet_ext.parquet").replace("'", "''")
        conn.execute(f"COPY balance_sheet_ext TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    report = fetcher.refresh(
        BALANCE_SHEET_EXT,
        periods=periods,
        max_stocks=args.limit,
        concurrency=args.concurrency,
        deadline_seconds=0,   # CLI 手动执行不设截止时间，语义与旧脚本一致
    )
    logger.info(
        "取数完成：updated=%d failed=%d rows=%s status=%s",
        report["updated"], report["failed"], report["rows_written"], report["status"],
    )

    evidence = {
        "task": "补取资产负债表补充科目（其他应付款/一年内到期非流动负债/其他应收款合计）",
        "executed_at": datetime.now(UTC).isoformat(), "periods": periods,
        "stocks_requested": report["targeted"], "stocks_updated": report["updated"],
        "stocks_failed": report["failed"],
        "rows": report["rows_written"].get("balance_sheet_ext", 0),
        "backup_dir": str(backup), "status": report["status"],
        "failed_sample": report["failed_codes"],
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r7_balance_sheet_ext_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)


if __name__ == "__main__":
    main()
