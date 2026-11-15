"""修复补采批次的血缘摘要（2026-11-15，reports/124）。

问题
----
`app/core/supplement.py` 原来把批次的 `raw_response_hash` 写成
`f"{endpoint}:{batch_id}"[:32]`，产出 `xjllbAjaxNew:451ef0802792468b909` 这种
**32 位非十六进制**串。数据质量规则按"32 位且形如 MD5 = legacy 历史批次"判定，
其余形态一律算**真血缘断裂** → `LINEAGE_INVALID` → `_SNAPSHOT_BLOCKING_WARNINGS`
→ **整个快照的 34 个指标全部被遮蔽**（`/api/ready` 返回
`reason=snapshot_values_masked`）。

实测：13 个批次（cashflow_supplement / balance_sheet_ext / deducted_profit /
fixed_assets_supplement / financial_sector，2026-11-15 10:21 起）触发该状态。

修法
----
1. 生成侧已改：新批次用 `sha256(请求描述)` 作摘要，并**先归档 payload 再登记批次**；
2. 存量侧由本脚本补齐：对这 13 个批次写入一条**明确标注为回填**的归档记录
   （`payload` 是批次描述 JSON，不是原始响应——原始响应当时没有保留）。
   归档后 `batch_archive_gap_rows` 归零、遮蔽解除。

用法
----
    python scripts/repair_supplement_lineage.py            # dry-run
    python scripts/repair_supplement_lineage.py --yes      # 写库（写锁 + 单事务）
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

from app.core.storage.duckdb_store import (  # noqa: E402
    DuckDBStore,
    archive_raw_response_if_absent,
)
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

_GAP_SQL = """
WITH valid_hashes AS (
    SELECT raw_response_hash FROM raw_response_archive_valid_hash
)
SELECT b.batch_id, b.raw_response_hash, b.data_type, b.source, b.adapter_version,
       b.row_count, b.fetch_time, b.report_date_range, b.confidence
FROM fetch_batch b
WHERE NOT EXISTS (SELECT 1 FROM valid_hashes v WHERE v.raw_response_hash = b.raw_response_hash)
  AND NOT COALESCE(
      LENGTH(b.raw_response_hash) = 32
      AND regexp_matches(lower(b.raw_response_hash), '^[0-9a-f]{32}$'),
      FALSE
  )
ORDER BY b.fetch_time
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="修复补采批次的血缘摘要（回填归档记录）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    rows = duck.read_query(_GAP_SQL)
    logger.info("待回填批次 %d 个", len(rows))
    for row in rows:
        logger.info("  %s %s rows=%s %s", str(row["fetch_time"])[:19], row["data_type"],
                    row["row_count"], row["raw_response_hash"])
    if not rows:
        logger.info("没有需要回填的批次")
        return
    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行")
        return

    try:
        with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
            for row in rows:
                payload = json.dumps({
                    "lineage_backfill": True,
                    "note": (
                        "补采批次的原始响应当时未保留；本记录为该批次的请求/结果描述，"
                        "用于闭合 2026-11-15 的 lineage 缺口（reports/124）。"
                        "生成侧已改为 sha256(请求描述) + 先归档后登记。"
                    ),
                    "batch_id": row["batch_id"],
                    "data_type": row["data_type"],
                    "source": row["source"],
                    "adapter_version": row["adapter_version"],
                    "row_count": row["row_count"],
                    "report_date_range": row["report_date_range"],
                    "confidence": row["confidence"],
                    "fetch_time": str(row["fetch_time"]),
                    "backfilled_at": datetime.now(UTC).isoformat(),
                }, ensure_ascii=False, sort_keys=True).encode("utf-8")
                archive_raw_response_if_absent(
                    conn,
                    raw_response_hash=row["raw_response_hash"],
                    source=f"lineage-backfill:{row['source']}",
                    fetch_time=row["fetch_time"],
                    payload=payload,
                    api_version=row["adapter_version"],
                )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    remaining = duck.read_query(_GAP_SQL)
    logger.info("回填完成，剩余缺口批次 %d 个", len(remaining))


if __name__ == "__main__":
    main()
