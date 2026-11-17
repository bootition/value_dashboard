"""清理 reports/124 遗留的「描述型归档」并让血缘缺口如实归类（2026-11-17，reports/135）。

问题
----
2026-11-15，`app/core/supplement.py` 把批次摘要写成
`f"{endpoint}:{batch_id}"[:32]`（如 `xjllbAjaxNew:451ef0…`），既不是内容寻址，
也没有归档原始响应 → 13 个批次触发 `LINEAGE_INVALID`，整个快照被遮蔽。

当时的修复（`reports/124`）给这 13 个批次各写了一条**描述型**归档记录
（payload 是本轮请求描述 JSON，不是原始响应）。这解决了 `batch_archive_gap_rows`，
但违反了内容寻址不变式：`sha256(payload) != raw_response_hash`
→ 现在触发 `hash_mismatch_rows = 13` → **同一个 LINEAGE_INVALID 换了个形式复发**。

真相
----
这 13 个批次的**原始响应从来没有被保留**（那次抓取发生在归档改造之前）。
描述型 payload 不是原始响应，留在归档表里等于**伪装成有原始字节**——正是
「ROCE 事故」同型的假证据。

正确处置（本脚本）
------------------
1. **删掉 13 条描述型归档记录**（它们是假的原始响应），备份到 `.planning/`；
2. 13 个 `fetch_batch` 记录**保留**（它们是真实发生过的抓取事实），
   但其摘要是「非内容寻址形态」→ 归入 `legacy_archive_gap_batches`
   （与 32 位 MD5 时代那 35 个批次同类：已披露的历史事实，不是今天的数据损坏），
   由 `data_quality.py` 的 legacy 判据放宽后自动归类；
3. 生成端已在 `reports/124` 改为 `sha256(请求描述)` + **先归档后登记**，
   不再产生新的同类记录。

用法
----
    python scripts/repair_lineage_backfill_rows.py            # dry-run
    python scripts/repair_lineage_backfill_rows.py --yes      # 写库（备份 + 锁 + 单事务）
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

BACKFILL_SOURCE_PREFIX = "lineage-backfill:"


def main() -> None:
    ap = argparse.ArgumentParser(description="清理描述型归档记录（reports/124 遗留）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    rows = duck.read_query(
        "SELECT raw_response_hash, source, fetch_time, OCTET_LENGTH(payload) AS n "
        "FROM raw_response_archive WHERE source LIKE ? ORDER BY fetch_time",
        [BACKFILL_SOURCE_PREFIX + "%"],
    )
    logger.info("描述型归档记录 %d 条", len(rows))
    for row in rows:
        logger.info("  %s  bytes=%s  source=%s", row["raw_response_hash"], row["n"], row["source"])
    if not rows:
        logger.info("没有需要清理的记录")
        return
    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行")
        return

    backup_dir = PROJECT_ROOT / ".planning" / f"maintenance-backup-lineage-backfill-{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / "raw_response_archive_backfill.parquet").replace("'", "''")

    try:
        with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
            conn.execute("SET preserve_insertion_order=false")
            conn.execute(
                f"COPY (SELECT * FROM raw_response_archive WHERE source LIKE '{BACKFILL_SOURCE_PREFIX}%') "
                f"TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            hashes = [row["raw_response_hash"] for row in rows]
            slots = ",".join("?" for _ in hashes)
            # valid_hash 里若登记过这些摘要，一并清掉（它们从未对应真实字节）
            conn.execute(
                f"DELETE FROM raw_response_archive_valid_hash WHERE raw_response_hash IN ({slots})",
                hashes,
            )
            deleted = conn.execute(
                f"DELETE FROM raw_response_archive WHERE raw_response_hash IN ({slots})",
                hashes,
            ).rowcount
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    logger.info("清理完成：删除 %s 条，备份 %s", deleted, backup_dir)
    remaining_batches = duck.read_query(
        "SELECT COUNT(*) AS n FROM fetch_batch WHERE raw_response_hash LIKE '%:%.%' OR "
        "(LENGTH(raw_response_hash) = 32 AND NOT regexp_matches(lower(raw_response_hash), '^[0-9a-f]{32}$'))"
    )[0]["n"]
    logger.info("仍带非内容寻址摘要的批次：%s（应归入 legacy_archive_gap_batches）", remaining_batches)


if __name__ == "__main__":
    main()
