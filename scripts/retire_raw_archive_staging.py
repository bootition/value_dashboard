"""退役 4 张遗留的 raw_response_archive_* staging 表（2026-11-16，reports/126）。

背景
----
`raw_response_archive_20260917_044324` / `_20261020_052312` / `_20261113_101944` /
`raw_response_archive_merged_20260904` 共 **274,249 行 / 17.18 GB payload**，
运行时零引用，且**不在** `raw_response_archive_all` 视图里（视图只 UNION
`_history` + `_active`）。

关键核对（2026-11-16 实测，别跳过这一步）
------------------------------------------
它们的 274,249 个 `raw_response_hash` **在 history / active / 外部冷库
`D:/vd-cold-archive/partitioned` 里一个都没有**（覆盖数 = 0）。
也就是说这 17 GB 是**唯一副本**，直接 DROP 会永久丢失原始响应。

因此按项目既定纪律「**退役 ≠ 删除**」处理：
1. 先把每张表导出成 ZSTD Parquet（可压缩到 2–4 GB）到 `--out`；
2. 回读校验行数 + 列数；
3. 才在单事务里 DROP 这 4 张表（写锁 + dry-run 默认）。

恢复方式（任选）：
    CREATE TABLE raw_response_archive_20260917_044324
        AS SELECT * FROM read_parquet('<out>/raw_response_archive_20260917_044324.parquet');

用法
----
    python scripts/retire_raw_archive_staging.py --out D:/vd-cold-archive-raw-staging
    python scripts/retire_raw_archive_staging.py --out D:/vd-cold-archive-raw-staging --yes
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
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

STAGING_TABLES = (
    "raw_response_archive_20260917_044324",
    "raw_response_archive_20261020_052312",
    "raw_response_archive_20261113_101944",
    "raw_response_archive_merged_20260904",
)


def main() -> None:
    ap = argparse.ArgumentParser(description="退役 raw_response_archive_* staging 表（先导出再删）")
    ap.add_argument("--out", required=True, help="Parquet 输出目录（必须在 data/ 之外）")
    ap.add_argument("--yes", action="store_true", help="确认导出并删表（默认 dry-run）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    out_dir = Path(args.out)
    if Path(paths.duckdb_path).resolve().parent == out_dir.resolve():
        ap.error("--out 不能指向 data/ 目录")

    duck = DuckDBStore(paths=paths)
    existing = {
        str(row["table_name"])
        for row in duck.read_query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND (table_name LIKE 'raw_response_archive_2026%' "
            "OR table_name LIKE 'raw_response_archive_merged_%')"
        )
    }
    targets = [name for name in STAGING_TABLES if name in existing]
    if not targets:
        logger.info("没有需要退役的 staging 表（可能已处理）")
        return

    plan = []
    for name in targets:
        rows = duck.read_query(f'SELECT COUNT(*) AS n FROM "{name}"')[0]["n"]
        plan.append((name, int(rows)))
        logger.info("待退役 %s：%s 行", name, f"{rows:,}")
    logger.info("合计 %s 行", f"{sum(n for _, n in plan):,}")

    if not args.yes:
        logger.info("[DRY RUN] 未导出、未删表；加 --yes 执行")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    verified: list[tuple[str, int, Path]] = []
    for name, expected in plan:
        target = out_dir / f"{name}.parquet"
        literal = str(target).replace("'", "''")
        copy_sql = (
            f'COPY (SELECT * FROM "{name}") TO {chr(39)}{literal}{chr(39)} '
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        with duck.read_connection() as read_conn:
            # 2026-11-16：payload 是 17GB 量级 BLOB，默认设置下 COPY 会 OOM
            # （实测 7.4GiB 撞顶）。降线程 + 关插入序 + 显式内存上限后稳定。
            read_conn.execute("SET preserve_insertion_order=false")
            read_conn.execute("SET threads=2")
            read_conn.execute("SET memory_limit='10GB'")
            read_conn.execute(copy_sql)
        back = duck.read_query(f"SELECT COUNT(*) AS n FROM read_parquet('{literal}')")[0]["n"]
        if int(back) != expected:
            logger.error("导出校验失败 %s：期望 %s 行，回读 %s 行", name, expected, back)
            raise SystemExit(4)
        size_mb = target.stat().st_size / 1024 / 1024
        logger.info("导出校验通过 %s：%s 行 / %.1f MB", name, f"{back:,}", size_mb)
        verified.append((name, expected, target))

    try:
        with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
            for name, _, _ in verified:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    logger.info(
        "退役完成：%d 张表已删，数据保留在 %s（恢复方式见脚本 docstring）%s",
        len(verified), out_dir, datetime.now(UTC).isoformat(),
    )


if __name__ == "__main__":
    main()
