"""抓取 A+H 公司 H 股不复权日线 → `hk_price_daily`（reports/120 J12）。

默认 dry-run（只读 + 抽样抓取，不写库）；加 `--yes` 才写正式库，且写前检查
`update_lock.any_write_lock_active`，沿用仓库维护脚本的单写者纪律。

为什么需要
----------
`indicator_snapshot.total_market_cap = A 股收盘价 × 总股本`，而总股本含 H 股，
导致 203 只 A+H 的市值/PE/PB/PS 系统性偏大（工行实测完全吻合）。要按真实
两地价格拆分市值，H 股收盘价是硬阻断项之一。

取数策略
--------
- 目标：`app/core/ah_hk_mapping.py` 的 A+H 映射（约 202 只）；
- 主源：`ak.stock_hk_hist`（东财，不复权）；本机实测东财域名不可达时自动降级
  `ak.stock_hk_daily`（新浪），证据 JSON 的 `upstream` 如实记录；
- **分批 + 每轮上限 + 断点续传**：每只股票按库内最新 `trade_date` 计算起点，
  只补「最新 H 价 < 对应 A 股最新价格日」的部分；
- `source='akshare_hk'`，PK `(stock_code, trade_date)` upsert；
- 失败只记录不阻断；空响应也算失败（不写入、不伪造）。

用法
----
    python scripts/fetch_hk_prices.py                         # dry-run，抽样 2 只
    python scripts/fetch_hk_prices.py --codes 601398,601857 --sample 2
    python scripts/fetch_hk_prices.py --yes --max-stocks 60   # 每轮最多 60 只
    python scripts/fetch_hk_prices.py --yes --max-stocks 0 --prefer tencent  # 全量（1,500 根/只，约 2 分钟）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.adapters.hk_price_adapter import (  # noqa: E402
    HK_PRICE_SOURCE,
    fetch_hk_daily,
    to_date,
)
from app.core.ah_hk_mapping import build_ah_hk_mapping  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

EVIDENCE_ROOT = PROJECT_ROOT / ".planning" / "2026-11-14-ah-metrics" / "evidence"
UPSERT_SQL = """
INSERT INTO hk_price_daily (stock_code, trade_date, close, source, fetched_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (stock_code, trade_date) DO UPDATE SET
    close = excluded.close,
    source = excluded.source,
    fetched_at = excluded.fetched_at
"""


def _cli_date(text: str) -> date:
    parsed = to_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"无法解析日期: {text!r}（用 YYYYMMDD 或 YYYY-MM-DD）")
    return parsed


def _existing_hk_stats(duck: DuckDBStore) -> tuple[dict[str, dict[str, Any]], int]:
    """库内已有 H 股行情；v40 之前表不存在时按空处理。"""
    try:
        rows = duck.read_query(
            """SELECT stock_code, MAX(trade_date) AS max_date, COUNT(*) AS row_count
               FROM hk_price_daily GROUP BY stock_code"""
        )
    except Exception as error:  # noqa: BLE001 - 未迁移的正式库应可 dry-run
        logger.warning("hk_price_daily 尚不可读（可能未建表，dry-run 按空处理）: %s", error)
        return {}, 0
    existing = {
        str(row["stock_code"]).zfill(5): {
            "max_date": to_date(row["max_date"]),
            "row_count": int(row["row_count"] or 0),
        }
        for row in rows
    }
    total_rows = sum(item["row_count"] for item in existing.values())
    return existing, total_rows


def _latest_a_dates(
    duck: DuckDBStore, codes: list[str], end_date: date,
) -> dict[str, date]:
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    rows = duck.read_query(
        f"""SELECT stock_code, MAX(trade_date) AS max_date
            FROM price_daily_raw
            WHERE stock_code IN ({placeholders})
              AND close IS NOT NULL AND trade_date <= ?
            GROUP BY stock_code""",
        [*codes, end_date],
    )
    return {
        row["stock_code"]: to_date(row["max_date"])
        for row in rows
        if row["max_date"] is not None
    }


def _filter_mapping(
    mapping: dict[str, dict[str, Any]], codes_raw: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """`--codes` 支持 A 股代码或 5 位港股代码；返回过滤后的映射与未命中清单。"""
    wanted = [item.strip() for item in codes_raw.split(",") if item.strip()]
    if not wanted:
        return mapping, []
    by_hk = {str(meta["hk_code"]).zfill(5): code for code, meta in mapping.items()}
    selected: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []
    for raw in wanted:
        if raw in mapping:
            selected[raw] = mapping[raw]
            continue
        hk_code = raw.zfill(5)
        a_code = by_hk.get(hk_code)
        if a_code is None:
            unmatched.append(raw)
        else:
            selected[a_code] = mapping[a_code]
    return selected, unmatched


def _build_targets(
    mapping: dict[str, dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    latest_a: dict[str, date],
    *,
    start_date: date,
    end_date: date,
    full: bool,
) -> list[dict[str, Any]]:
    """按「已有最新 H 价 < 对应 A 股最新价格日」生成待补清单（断点续传判据）。"""
    targets: list[dict[str, Any]] = []
    seen_hk: set[str] = set()
    # 个别 A 股无行情时用全市场最新 A 股价格日兜底，避免每天对同一只
    # 停牌/退市股重复抓取（fetch_start > target_end 会自然跳过）。
    market_end = max(latest_a.values()) if latest_a else end_date
    for a_code in sorted(mapping):
        meta = mapping[a_code]
        hk_code = str(meta["hk_code"]).zfill(5)
        if hk_code in seen_hk:  # 同一港股代码只抓一次
            continue
        seen_hk.add(hk_code)
        target_end = latest_a.get(a_code) or market_end
        if target_end > end_date:
            target_end = end_date
        previous = existing.get(hk_code) or {}
        previous_max: date | None = previous.get("max_date")
        if not full and previous_max is not None and previous_max >= target_end:
            continue
        fetch_start = start_date
        if not full and previous_max is not None:
            fetch_start = max(start_date, previous_max + timedelta(days=1))
        if fetch_start > target_end:
            continue
        targets.append({
            "stock_code": a_code,
            "hk_code": hk_code,
            "fetch_start": fetch_start,
            "target_end": target_end,
            "existing_max": previous_max,
        })
    return targets


def _write_evidence(payload: dict[str, Any], prefix: str) -> Path | None:
    try:
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE_ROOT / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        return path
    except OSError as error:  # 证据写不进去不应毁掉取数结果
        logger.warning("证据 JSON 写入失败: %s", error)
        return None


def _fetch_one(target: dict[str, Any], *, prefer: str) -> Any:
    return fetch_hk_daily(
        target["hk_code"],
        start_date=target["fetch_start"],
        end_date=target["target_end"],
        prefer=prefer,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="抓取 A+H 公司 H 股不复权日线（默认 dry-run）"
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument(
        "--start-date", type=_cli_date, default=date(2000, 1, 1),
        help="全量起点，默认 2000-01-01",
    )
    ap.add_argument(
        "--end-date", type=_cli_date, default=None,
        help="目标终点，默认今天；每股实际终点还会被其 A 股最新价格日封顶",
    )
    ap.add_argument(
        "--max-stocks", type=int, default=60,
        help="本轮最多处理多少只（0=不限；默认 60，配合断点续传分轮跑）",
    )
    ap.add_argument("--batch-size", type=int, default=20, help="每批提交事务的股票数")
    ap.add_argument("--sample", type=int, default=2, help="dry-run 抽样抓取只数")
    ap.add_argument(
        "--prefer", choices=("auto", "tencent", "eastmoney", "sina"), default="auto",
        help="港股日线源：auto=东财失败降级新浪；本机东财不可达时可用 sina",
    )
    ap.add_argument("--sleep", type=float, default=0.5, help="单股请求间隔秒数")
    ap.add_argument(
        "--codes", default="",
        help="只处理这些代码（A 股或 5 位港股，逗号分隔；用于小样本探测）",
    )
    args = ap.parse_args()

    if args.max_stocks < 0 or args.batch_size <= 0 or args.sample < 0 or args.sleep < 0:
        ap.error("--max-stocks/--batch-size/--sample/--sleep 参数非法")
    end_date = args.end_date or date.today()
    if args.start_date > end_date:
        ap.error("--start-date 不能晚于 --end-date")

    try:
        paths: DatabasePathSet = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    if args.yes and any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    mapping_report = build_ah_hk_mapping(duck)
    mapping: dict[str, dict[str, Any]] = mapping_report["mapping"]
    mapping, unmatched = _filter_mapping(mapping, args.codes)
    if args.codes and not mapping:
        ap.error(f"--codes 未命中任何 A+H 映射: {unmatched}")

    existing, existing_rows = _existing_hk_stats(duck)
    a_codes = list(mapping)
    latest_a = _latest_a_dates(duck, a_codes, end_date)
    pending = _build_targets(
        mapping, existing, latest_a,
        start_date=args.start_date, end_date=end_date, full=False,
    )

    logger.info(
        "映射目标 %d 只 A+H（映射源=%s，未匹配 %d）",
        len(mapping), mapping_report.get("source"), len(mapping_report.get("unmatched_hk_codes") or []),
    )
    if unmatched:
        logger.warning("--codes 未命中（已忽略）: %s", ", ".join(unmatched))
    logger.info(
        "hk_price_daily 已有 %d 行 / %d 只；待补 %d 只（判据：最新 H 价 < 对应 A 股最新价格日）",
        existing_rows, len(existing), len(pending),
    )
    if not pending:
        logger.info("没有需要补的 H 股行情。")
        evidence = {
            "task": "抓取 A+H 港股日线 → hk_price_daily",
            "executed_at": datetime.now(UTC).isoformat(),
            "dry_run": not args.yes,
            "targets": len(mapping),
            "existing_rows": existing_rows,
            "existing_stocks": len(existing),
            "pending_stocks": 0,
            "status": "up_to_date",
        }
        path = _write_evidence(evidence, "fetch_hk_prices")
        if path:
            logger.info("证据 JSON: %s", path)
        return

    requested = pending if args.max_stocks == 0 else pending[: args.max_stocks]

    if not args.yes:
        sample = requested[: args.sample]
        fetched_rows = 0
        samples: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        upstream_counts: dict[str, int] = {}
        for index, target in enumerate(sample, start=1):
            result = _fetch_one(target, prefer=args.prefer)
            upstream_counts[result.upstream] = upstream_counts.get(result.upstream, 0) + 1
            if result.error:
                failures.append({
                    "stock_code": target["stock_code"],
                    "hk_code": target["hk_code"],
                    "reason": result.error,
                })
                logger.warning(
                    "  [%d/%d] %s(%s) 失败: %s",
                    index, len(sample), target["stock_code"], target["hk_code"], result.error,
                )
            else:
                rows = result.rows
                fetched_rows += len(rows)
                item = {
                    "stock_code": target["stock_code"],
                    "hk_code": target["hk_code"],
                    "rows": len(rows),
                    "start": rows[0]["trade_date"],
                    "end": rows[-1]["trade_date"],
                    "first_close": rows[0]["close"],
                    "last_close": rows[-1]["close"],
                    "upstream": result.upstream,
                    "note": result.note,
                }
                samples.append(item)
                logger.info(
                    "  [%d/%d] %s(%s) → %d 行 %s~%s 首/末收盘=%s/%s upstream=%s%s",
                    index, len(sample), target["stock_code"], target["hk_code"],
                    len(rows), rows[0]["trade_date"], rows[-1]["trade_date"],
                    rows[0]["close"], rows[-1]["close"], result.upstream,
                    f" note={result.note}" if result.note else "",
                )
            if index < len(sample):
                time.sleep(args.sleep)
        average = fetched_rows / len(sample) if sample else 0.0
        estimated = int(round(average * len(requested)))
        logger.info(
            "[DRY RUN] 未写库；落库行数: 0；本轮待补 %d 只，抽样 %d 只抓取 %d 行，"
            "失败 %d 只，按均值估计全量约 %d 行",
            len(requested), len(sample), fetched_rows, len(failures), estimated,
        )
        if failures:
            logger.warning("失败清单: %s", json.dumps(failures, ensure_ascii=False))
        evidence = {
            "task": "抓取 A+H 港股日线 → hk_price_daily",
            "executed_at": datetime.now(UTC).isoformat(),
            "dry_run": True,
            "args": {**vars(args), "start_date": str(args.start_date), "end_date": str(end_date)},
            "mapping_source": mapping_report.get("source"),
            "mapping_matched": len(mapping),
            "mapping_unmatched": len(mapping_report.get("unmatched_hk_codes") or []),
            "targets": len(mapping),
            "existing_rows": existing_rows,
            "existing_stocks": len(existing),
            "pending_stocks": len(pending),
            "requested_stocks": len(requested),
            "sampled_stocks": len(sample),
            "sampled_rows": fetched_rows,
            "estimated_full_rows": estimated,
            "upstream_counts": upstream_counts,
            "samples": samples,
            "failures": failures,
            "status": "dry_run" if not failures else "dry_run_with_failures",
        }
        path = _write_evidence(evidence, "fetch_hk_prices")
        if path:
            logger.info("证据 JSON: %s", path)
        return

    # ─── 写库路径（本任务未执行） ────────────────────────────────
    from app.core.storage.schema import init_duckdb_schema

    init_duckdb_schema(duck)
    backup_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-ah-hk-prices-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = str(backup_dir / "hk_price_daily.parquet").replace("'", "''")
    with duck.write_connection() as conn:
        conn.execute(f"COPY hk_price_daily TO '{backup_path}' (FORMAT PARQUET)")
    logger.info("[BACKUP] hk_price_daily → %s", backup_path)

    if any_write_lock_active(paths.duckdb_path):
        logger.error("写前再次检测到写锁生效，中止。")
        raise SystemExit(3)

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    written_rows = 0
    fetched_rows = 0
    failures: list[dict[str, str]] = []
    upstream_counts: dict[str, int] = {}
    aborted = False
    for batch_start in range(0, len(requested), args.batch_size):
        batch = requested[batch_start: batch_start + args.batch_size]
        started = time.monotonic()
        params: list[tuple[Any, ...]] = []
        for target in batch:
            result = _fetch_one(target, prefer=args.prefer)
            upstream_counts[result.upstream] = upstream_counts.get(result.upstream, 0) + 1
            if result.error or not result.rows:
                reason = result.error or "empty_response"
                failures.append({
                    "stock_code": target["stock_code"],
                    "hk_code": target["hk_code"],
                    "reason": reason,
                })
                logger.warning(
                    "  %s(%s) 失败: %s", target["stock_code"], target["hk_code"], reason,
                )
            else:
                fetched_rows += len(result.rows)
                params.extend(
                    (target["hk_code"], row["trade_date"], row["close"],
                     HK_PRICE_SOURCE, fetched_at)
                    for row in result.rows
                )
            time.sleep(args.sleep)
        if params:
            with duck.transaction() as conn:
                conn.executemany(UPSERT_SQL, params)
            written_rows += len(params)
        logger.info(
            "批次 %d-%d 完成：本批 %d 行，累计写入 %d 行，失败 %d 只，用时 %.1fs",
            batch_start + 1, batch_start + len(batch), len(params), written_rows,
            len(failures), time.monotonic() - started,
        )
        if any_write_lock_active(paths.duckdb_path):
            logger.error("检测到外部写锁，停止后续批次（已提交批次保留）。")
            aborted = True
            break

    status = "aborted" if aborted else ("ok" if not failures else "partial")
    logger.info(
        "完成：请求 %d 只，抓取 %d 行，落库 %d 行，失败 %d 只，status=%s",
        len(requested), fetched_rows, written_rows, len(failures), status,
    )
    if failures:
        logger.warning("失败清单: %s", json.dumps(failures, ensure_ascii=False))
    evidence = {
        "task": "抓取 A+H 港股日线 → hk_price_daily",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": False,
        "args": {**vars(args), "start_date": str(args.start_date), "end_date": str(end_date)},
        "mapping_source": mapping_report.get("source"),
        "mapping_matched": len(mapping),
        "targets": len(mapping),
        "existing_rows": existing_rows,
        "existing_stocks": len(existing),
        "pending_stocks": len(pending),
        "requested_stocks": len(requested),
        "fetched_rows": fetched_rows,
        "rows_written": written_rows,
        "upstream_counts": upstream_counts,
        "backup_path": backup_path,
        "failures": failures,
        "status": status,
    }
    path = _write_evidence(evidence, "fetch_hk_prices")
    if path:
        logger.info("证据 JSON: %s", path)
    if status == "aborted":
        raise SystemExit(4)


if __name__ == "__main__":
    main()
