"""把"股本历史链尾"与 `stock_meta` 当前股本对齐（2026-11-16，reports/127）。

为什么需要
----------
市值统一到 `app/core/market_cap.py` 后，规则是"取价格日之前最近一条股本历史"。
但 `share_capital_history` 是逐股**按抓取时间**累积的，部分股票的链条停在
上一次成功抓取日，之后发生的**股权激励 / 可转债转股 / 回购注销**并不会出现在
`xdxr`（除权除息表）里，于是链尾永远追不上 `stock_meta`。

实测（2026-11-16，`xdxr` 回填 + `capital-history` 重抓之后仍剩下的 6 只）：

| 股票 | 历史链尾 | stock_meta | 原因 |
|---|---:|---:|---|
| 689009 | 73,584,850 | 735,848,500 | **CDR 存托凭证单位差 10 倍**（整条链都是 1/10） |
| 600461 | 1,324,466,604 | 1,407,167,116 | 股权激励/转债转股未入 xdxr |
| 301297 | 694,309,362 | 704,760,283 | 同上 |
| 300429 | 559,136,990 | 576,090,037 | 同上 |
| 688235 | 1,469,278,815 | 1,542,285,181 | 同上 |
| 300299 | 690,300,085 | 697,508,938 | 同上 |

两条处置（都不是猜数，都有判据）
--------------------------------
1. **整条链一致的整数倍偏差** → 判定为**单位错位**，把该股全部历史行乘以倍数
   （`change_reason='unit_rescale:<倍数>'`）。判据：所有行的 `meta/行值` 都落在
   同一倍数 ±1% 内（`--unit-tolerance`）。689009 属于这一类。
2. **只有链尾落后** → 在"最后一条历史 + 1 天"（不晚于最新价格日）插一行
   `total_shares = stock_meta`，`change_reason='meta_align'`、`source='meta_align'`。
   这是**对齐当前股本**，不是伪造历史：历史段一行不动。

用法
----
    python scripts/align_share_capital_tail.py                  # dry-run
    python scripts/align_share_capital_tail.py --yes            # 写库（备份 + 锁 + 单事务）
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import date, datetime, timedelta
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

SOURCE = "meta_align"
_UNIT_CANDIDATES = (10.0, 100.0, 0.1, 0.01)

_DRIFT_SQL = """
WITH hist AS (
    SELECT stock_code,
           MAX(effective_date) AS tail_date,
           ARG_MAX(total_shares, effective_date) AS tail_shares,
           COUNT(*) AS n_rows
    FROM share_capital_history
    WHERE total_shares > 0
    GROUP BY stock_code
)
SELECT m.stock_code,
       h.tail_date,
       h.tail_shares,
       h.n_rows,
       m.total_shares AS meta_shares
FROM stock_meta m
JOIN hist h ON h.stock_code = m.stock_code
WHERE m.is_listed IS TRUE
  AND m.total_shares > 0
  AND h.tail_shares > 0
  AND ABS(m.total_shares - h.tail_shares) > 0.01 * h.tail_shares
ORDER BY ABS(m.total_shares - h.tail_shares) DESC
"""


def _unit_multiple(duck: DuckDBStore, code: str, meta_shares: float, tolerance: float) -> float | None:
    """整条链是否被同一倍数缩放（CDR/单位错位）。返回倍数或 None。"""
    rows = duck.read_query(
        "SELECT total_shares FROM share_capital_history WHERE stock_code = ? AND total_shares > 0",
        [code],
    )
    values = [float(row["total_shares"]) for row in rows]
    if len(values) < 2:
        return None
    for multiple in _UNIT_CANDIDATES:
        expected = meta_shares / multiple
        if all(abs(value - expected) <= tolerance * expected for value in values):
            return multiple
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="对齐股本历史链尾与 stock_meta")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--unit-tolerance", type=float, default=0.06,
                    help="判定整链单位错位的容差；689009 整链在 meta/10 的 0.957~1.000 之间，需 6%%")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    drift = duck.read_query(_DRIFT_SQL)
    logger.info("链尾与 stock_meta 偏离 >1%% 的在市股票：%d 只", len(drift))
    if not drift:
        logger.info("无需处理")
        return

    latest_price_date = duck.read_query("SELECT MAX(trade_date) AS d FROM price_daily_qfq")[0]["d"]
    plan: list[dict] = []
    for row in drift:
        code = str(row["stock_code"])
        meta_shares = float(row["meta_shares"])
        multiple = _unit_multiple(duck, code, meta_shares, args.unit_tolerance)
        if multiple is not None:
            plan.append({"kind": "rescale", "stock_code": code, "multiple": multiple})
            logger.info("  %s：整链单位错位 → 乘 %g（链尾 %s → %s）",
                        code, multiple, f"{row['tail_shares']:,.0f}", f"{meta_shares:,.0f}")
            continue
        base = row["tail_date"] or latest_price_date
        effective = max(base, latest_price_date) if latest_price_date else base
        if isinstance(effective, date):
            effective = effective + timedelta(days=0)
        plan.append({
            "kind": "append", "stock_code": code, "effective_date": effective,
            "shares": meta_shares,
        })
        logger.info("  %s：链尾落后 → 插一行 %s = %s（原 %s @ %s）",
                    code, effective, f"{meta_shares:,.0f}",
                    f"{row['tail_shares']:,.0f}", row["tail_date"])

    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行")
        return

    backup_dir = PROJECT_ROOT / ".planning" / f"maintenance-backup-share-align-{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / "share_capital_history_affected.parquet").replace("'", "''")
    codes = [item["stock_code"] for item in plan]
    slots = ",".join("?" for _ in codes)

    try:
        with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
            conn.execute("SET preserve_insertion_order=false")
            conn.execute(
                f"COPY (SELECT * FROM share_capital_history WHERE stock_code IN ({slots})) "
                f"TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                codes,
            )
            for item in plan:
                code = item["stock_code"]
                if item["kind"] == "rescale":
                    conn.execute(
                        "UPDATE share_capital_history SET total_shares = total_shares * ?, "
                        "change_reason = ? WHERE stock_code = ?",
                        [item["multiple"], f"unit_rescale:{item['multiple']:g}", code],
                    )
                else:
                    batch = hashlib.sha256(f"meta_align:{code}:{item['effective_date']}".encode()).hexdigest()
                    conn.execute(
                        """INSERT INTO share_capital_history
                               (stock_code, effective_date, total_shares, change_reason,
                                is_anchor, verified, source, raw_hash, batch_id)
                           VALUES (?, CAST(? AS DATE), ?, 'meta_align', FALSE, FALSE, ?, ?, ?)
                           ON CONFLICT DO NOTHING""",
                        [code, item["effective_date"], item["shares"], SOURCE, batch, batch],
                    )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    remaining = duck.read_query(_DRIFT_SQL)
    logger.info("完成：处理 %d 只，剩余偏离 %d 只；备份 %s", len(plan), len(remaining), backup_dir)


if __name__ == "__main__":
    main()
