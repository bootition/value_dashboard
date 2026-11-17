"""按「股本链变更股」重建 `indicator_ext`（2026-11-16，reports/131 §9.2 P0-9）。

为什么需要这个脚本
------------------
P0-9 现场：689009（九号公司 CDR）的 `share_capital_history` 全链带
`change_reason='unit_rescale:10'`（股本链后来被 ×10 校正），但 `indicator_ext`
是校正**之前**构建的。旧输入指纹里没有股本链，于是 `_refresh_indicator_ext()`
每轮都判"输入没变、不必重建" —— 24 期 `market_cap_at_report` 与
`bps / bps_parent / revenue_per_share / ocf_per_share` 等 14 个派生列静默错 10 倍
（2025-12-31 应 400.4 亿、表内 40.04 亿；全库 43,478 个观测里仅此 1 只）。

修法拆成两半：
1. **自动**：`extended.input_state()` 的指纹已含 `share_capital_history` 的
   400 天尾段摘要 → 股本一变，`rebuild_decision()` 就判 `input_fingerprint_changed`
   → 自动更新流程会重建（`vd data compute-extended-indicators` 亦可手动全量）。
2. **最小重建**：`rebuild_decision()["force_codes"]` 给出"股本链变了但派生值还是
   旧的"股票清单（自证式探针：重算市值 vs 表内值），本脚本据此只重写这些股票，
   不必为了 1 只股票重写 40 万行。

用法
----
    python scripts/backfill_indicator_ext.py                 # 只看清单（dry-run）
    python scripts/backfill_indicator_ext.py --yes           # 只重建命中股
    python scripts/backfill_indicator_ext.py --yes --all     # 全量重建（等大范围变更时）
    python scripts/backfill_indicator_ext.py --codes 689009,600519 --yes

退出码：0 成功/无需处理；3 自动更新正在持锁（稍后重试）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.indicators.extended import (  # noqa: E402
    ExtendedIndicatorBuilder,
    input_state,
    share_capital_drift_codes,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import UpdateLockError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _report_current_values(duck: DuckDBStore, codes: list[str]) -> dict[str, dict]:
    """命中股票在 indicator_ext 的市值/每股族现值（验收对照用）。"""
    if not codes:
        return {}
    slots = ",".join("?" for _ in codes)
    rows = duck.read_query(
        f"""
        SELECT x.stock_code,
               CAST(x.report_date AS VARCHAR) AS report_date,
               x.market_cap_at_report, x.bps, x.revenue_per_share, x.ocf_per_share
        FROM indicator_ext x
        JOIN (
            SELECT stock_code, MAX(report_date) AS report_date
            FROM indicator_ext WHERE stock_code IN ({slots})
            GROUP BY stock_code
        ) m ON m.stock_code = x.stock_code AND m.report_date = x.report_date
        ORDER BY x.stock_code
        """,
        codes,
    )
    return {str(row["stock_code"]): dict(row) for row in rows}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="按股本链变更股重建 indicator_ext（reports/131 P0-9）",
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--all", action="store_true", help="全量重建（忽略 force_codes）")
    ap.add_argument("--codes", default="", help="显式指定股票代码，逗号分隔")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    state = input_state(duck)
    logger.info(
        "输入指纹：ext_rows=%s ext_max_period=%s complete_period=%s",
        state["ext_rows"], state["ext_max_period"], state["complete_period"],
    )
    share_capital = state["share_capital"]
    logger.info(
        "股本链尾段 %s 天：%s 行 / %s 只 / 最新 %s / digest %s",
        share_capital["window_days"], share_capital["rows"],
        share_capital["stocks"], share_capital["latest"],
        (share_capital["digest"] or "")[:12],
    )

    explicit = [code.strip() for code in args.codes.split(",") if code.strip()]
    if explicit:
        target, scope = explicit, "codes"
    elif args.all:
        target, scope = [], "all"
    else:
        target = share_capital_drift_codes(duck)
        scope = "force_codes"

    if scope == "all":
        logger.info("目标：全量重建")
    else:
        logger.info("目标：%d 只 —— %s", len(target), ", ".join(target) or "(无)")
        if not target:
            logger.info("派生表与股本链一致，无需重建")
            return

    before = _report_current_values(duck, target)
    if before:
        logger.info("重建前现值（最新一期）：%s", json.dumps(before, ensure_ascii=False, default=str))

    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行")
        return

    builder = ExtendedIndicatorBuilder(duck=duck, paths=paths)
    try:
        report = builder.build(
            codes=target if scope == "codes" else None,
            force_codes=target if scope == "force_codes" else None,
        )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    logger.info("重建完成：%s", json.dumps(report, ensure_ascii=False, default=str, indent=2))

    after = _report_current_values(duck, target)
    if after:
        logger.info("重建后现值（最新一期）：%s", json.dumps(after, ensure_ascii=False, default=str))
    moved = [code for code in after if before.get(code) != after.get(code)]
    logger.info("数值发生变化的股票：%d 只 %s", len(moved), ", ".join(moved))

    remaining = share_capital_drift_codes(duck)
    logger.info("重建后仍与股本链不一致的股票：%d 只 %s",
                len(remaining), ", ".join(remaining) or "(无)")
    if remaining:
        raise SystemExit(4)


if __name__ == "__main__":
    main()
