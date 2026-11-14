"""补取现金流量表补充资料（折旧摊销）与资本支出 —— CLI 薄包装。

核心逻辑已抽到 `app/core/supplement.py`（自动更新主流程与 CLI 共用同一份实现，
`2026-11-14 reports/119 D29` 自动接入）。本脚本只保留 CLI 语义：
dry-run 抽样、写前备份、单写者锁、证据 JSON。

为什么需要这个域
----------------
CSMAR C17 数据截止 2025-03-31，导致 `indicator_ext` 里的
折旧摊销 / 资本支出 / 自由现金流 / FCF率 / EBITDA / 经营杠杆 / 综合杠杆
这 7 列在**最新报告期覆盖率为 0%**。东方财富 F10 `xjllbAjaxNew`
提供同一批科目且更新到最新报告期（单次最多 5 期，策略见 supplement 模块）。

⚠️ 续传判据已修正（2026-11-14）
------------------------------
旧版是「该股在 `cash_flow_activity` 里有**任意** `source='eastmoney_f10'`
行就跳过」。第一次跑完（当时源只到 2026-06-30）之后，全市场股票都被判为
"已有本源"——**手动再跑也补不到 2026-09-30**。现在判据是「本源最新报告期
< 目标最新报告期」，且带每轮上限与失败冷却（见 `SupplementFetcher`）。

用法（与旧版完全一致）
----------------------
    python scripts/fetch_cashflow_supplement.py                    # dry-run，抽样 5 只
    python scripts/fetch_cashflow_supplement.py --limit 500        # 只跑 500 只
    python scripts/fetch_cashflow_supplement.py --yes              # 备份后写库
    python scripts/fetch_cashflow_supplement.py --yes --concurrency 6
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
    CASHFLOW_SUPPLEMENT,
    SPECS,
    SupplementFetcher,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="补取现金流量表补充资料与资本支出（覆盖 CSMAR 截止后的报告期）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    ap.add_argument("--concurrency", type=int, default=4, help="并发数（源侧限速，建议 ≤6）")
    ap.add_argument("--sample", type=int, default=5, help="dry-run 时抽样只数")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    spec = SPECS[CASHFLOW_SUPPLEMENT]
    fetcher = SupplementFetcher(duck=duck)
    periods = fetcher.resolve_periods(CASHFLOW_SUPPLEMENT)
    logger.info("目标报告期（最近 %d 期）: %s", len(periods), periods)
    pending_all = fetcher.pending_codes(CASHFLOW_SUPPLEMENT, periods)
    logger.info("待补股票 %d 只（判据：本源最新报告期 < %s）", len(pending_all), periods[0])

    if not args.yes:
        for code, exchange in fetcher.pending_codes(
            CASHFLOW_SUPPLEMENT, periods, max_stocks=max(0, args.sample),
        ):
            rows = fetcher._fetch_rows(spec, code, exchange, periods, timeout=20)
            parsed = fetcher._parse_rows(
                spec, code, rows,
                batch_id="dry",
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
                src_hash="dry",
            )
            logger.info(
                "  %s → 间接法 %d 行 / 资本支出 %d 行",
                code, len(parsed["cash_flow_indirect"]), len(parsed["cash_flow_activity"]),
            )
            time.sleep(0.5)
        logger.info("[DRY RUN] 未写库；加 --yes 执行全量（预计 %d 只）", len(pending_all))
        return

    if not pending_all:
        logger.info("没有需要补的股票，退出。")
        return

    from app.core.storage.schema import init_duckdb_schema
    init_duckdb_schema(duck)

    backup_dir = PROJECT_ROOT / ".planning" / f"maintenance-backup-etf-cashflow-{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for table in ("cash_flow_indirect", "cash_flow_activity"):
            target = str(backup_dir / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 已备份到 %s", backup_dir)

    report = fetcher.refresh(
        CASHFLOW_SUPPLEMENT,
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
        "task": "补取现金流量表补充资料（折旧摊销）与资本支出",
        "executed_at": datetime.now(UTC).isoformat(),
        "periods": periods,
        "stocks_requested": report["targeted"],
        "stocks_updated": report["updated"],
        "stocks_failed": report["failed"],
        "indirect_rows": report["rows_written"].get("cash_flow_indirect", 0),
        "activity_rows": report["rows_written"].get("cash_flow_activity", 0),
        "backup_dir": str(backup_dir),
        "status": report["status"],
        "failed_sample": report["failed_codes"],
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r5_cashflow_supplement_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)
    logger.info(
        "下一步: 自动更新会在下一轮按 report_date 门禁重建 indicator_ext；"
        "手动可用 `vd data compute-extended-indicators`"
    )


if __name__ == "__main__":
    main()
