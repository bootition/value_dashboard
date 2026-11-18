"""抓取 HKD/CNY 中行牌价历史 → `fx_rate_daily`（reports/120 J12）。

默认 dry-run；`--yes` 才写正式库，写前检查
`update_lock.any_write_lock_active`（与其它维护脚本同一单写者纪律）。

数据源与口径
------------
- `ak.currency_boc_sina(symbol="港币", start_date, end_date)`，**一次请求返回
  全部历史**；
- 返回列：`日期 / 中行汇买价 / 中行钞买价 / 中行钞卖价/汇卖价 / 央行中间价 /
  中行折算价`；
- 口径：**中行折算价优先**（PRODUCT_GAPS §1.3 G2 指定），缺失回退央行中间价；
- ⚠️ 源单位是「人民币元 / 100 港元」（实测 2026-11-13 折算价 85.604），
  适配器已 ÷100；落库 `rate` 是 **元/港元**（≈0.856）；
- `pair='HKDCNY'`，`source='boc_sina'`，PK `(pair, trade_date)` upsert。

断点续传
--------
默认从 `max(trade_date) − 7 天` 开始重取最近一段（覆盖牌价回补），
首次/`--full` 从 `--start-date` 全历史回填。

用法
----
    python scripts/fetch_fx_rates.py                 # dry-run，打印样本
    python scripts/fetch_fx_rates.py --yes           # 增量写库
    python scripts/fetch_fx_rates.py --yes --full    # 全历史回填
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.adapters.hk_price_adapter import (  # noqa: E402
    FX_RATE_SOURCE,
    HKD_CNY_PAIR,
    fetch_hkd_cny_history,
    to_date,
)
from app.core.financial_period import cn_today  # noqa: E402
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
INSERT INTO fx_rate_daily (pair, trade_date, rate, source, fetched_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (pair, trade_date) DO UPDATE SET
    rate = excluded.rate,
    source = excluded.source,
    fetched_at = excluded.fetched_at
"""


def _cli_date(text: str) -> date:
    parsed = to_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"无法解析日期: {text!r}（用 YYYYMMDD 或 YYYY-MM-DD）")
    return parsed


def _existing_stats(duck: DuckDBStore) -> dict[str, Any]:
    """库内已有 HKDCNY 汇率；v40 之前表不存在时按空处理。"""
    try:
        rows = duck.read_query(
            """SELECT COUNT(*) AS row_count, MIN(trade_date) AS min_date,
                      MAX(trade_date) AS max_date
               FROM fx_rate_daily WHERE pair = ?""",
            [HKD_CNY_PAIR],
        )
        dates = duck.read_query(
            "SELECT trade_date FROM fx_rate_daily WHERE pair = ?",
            [HKD_CNY_PAIR],
        )
    except Exception as error:  # noqa: BLE001 - 未迁移的正式库应可 dry-run
        logger.warning("fx_rate_daily 尚不可读（可能未建表，dry-run 按空处理）: %s", error)
        return {"row_count": 0, "min_date": None, "max_date": None, "dates": set()}
    row = rows[0] if rows else {}
    return {
        "row_count": int(row.get("row_count") or 0),
        "min_date": to_date(row.get("min_date")),
        "max_date": to_date(row.get("max_date")),
        "dates": {to_date(item["trade_date"]) for item in dates},
    }


def _write_evidence(payload: dict[str, Any], prefix: str) -> Path | None:
    try:
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE_ROOT / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        return path
    except OSError as error:
        logger.warning("证据 JSON 写入失败: %s", error)
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取 HKD/CNY 中行牌价历史（默认 dry-run）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument(
        "--start-date", type=_cli_date, default=date(2000, 1, 1),
        help="全历史起点，默认 2000-01-01",
    )
    ap.add_argument(
        "--end-date", type=_cli_date, default=None, help="终点，默认今天",
    )
    ap.add_argument("--full", action="store_true", help="忽略已有最新日期，从 --start-date 全量重取")
    ap.add_argument("--sample", type=int, default=5, help="dry-run 打印样本行数")
    args = ap.parse_args()

    end_date = args.end_date or cn_today()
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
    existing = _existing_stats(duck)
    if args.full or existing["max_date"] is None:
        fetch_start = args.start_date
    else:
        fetch_start = max(args.start_date, existing["max_date"] - timedelta(days=7))

    logger.info("目标 pair=%s（source=%s）", HKD_CNY_PAIR, FX_RATE_SOURCE)
    logger.info(
        "fx_rate_daily 已有 %d 行 %s~%s；本次请求区间 %s~%s%s",
        existing["row_count"], existing["min_date"], existing["max_date"],
        fetch_start, end_date, "（--full 全量）" if args.full else "",
    )

    result = fetch_hkd_cny_history(start_date=fetch_start, end_date=end_date)
    if result.error or not result.rows:
        logger.error("汇率抓取失败: %s", result.error or "empty_response")
        evidence = {
            "task": "抓取 HKD/CNY 中行牌价历史 → fx_rate_daily",
            "executed_at": datetime.now(UTC).isoformat(),
            "dry_run": not args.yes,
            "pair": HKD_CNY_PAIR,
            "existing_rows": existing["row_count"],
            "request_range": [str(fetch_start), str(end_date)],
            "error": result.error or "empty_response",
            "status": "failed",
        }
        path = _write_evidence(evidence, "fetch_fx_rates")
        if path:
            logger.info("证据 JSON: %s", path)
        raise SystemExit(1)

    rows = result.rows
    new_rows = [row for row in rows if row["trade_date"] not in existing["dates"]]
    pending_estimate = len(new_rows) if not args.full else len(rows)
    rates = sorted(row["rate"] for row in rows)
    median = rates[len(rates) // 2]
    logger.info(
        "目标区间返回 %d 行 %s~%s；已有 %d 行；待补约 %d 行；中位汇率 %.6f 元/港元",
        len(rows), rows[0]["trade_date"], rows[-1]["trade_date"],
        existing["row_count"], pending_estimate, median,
    )
    logger.info("牌价列使用情况: %s", result.mid_source_counts)
    for row in rows[: max(0, args.sample)]:
        logger.info(
            "  样本 %s  rate=%.6f 元/港元（%s）",
            row["trade_date"], row["rate"], row["mid_source"],
        )

    if not args.yes:
        logger.info(
            "[DRY RUN] 未写库；落库行数: 0；加 --yes 将写入 %d 行（pair=%s, source=%s）",
            len(rows), HKD_CNY_PAIR, FX_RATE_SOURCE,
        )
        evidence = {
            "task": "抓取 HKD/CNY 中行牌价历史 → fx_rate_daily",
            "executed_at": datetime.now(UTC).isoformat(),
            "dry_run": True,
            "args": {**vars(args), "start_date": str(args.start_date), "end_date": str(end_date)},
            "pair": HKD_CNY_PAIR,
            "source": FX_RATE_SOURCE,
            "upstream": result.upstream,
            "existing_rows": existing["row_count"],
            "existing_range": [str(existing["min_date"]), str(existing["max_date"])],
            "request_range": [str(fetch_start), str(end_date)],
            "returned_rows": len(rows),
            "would_write_rows": len(rows),
            "new_dates": len(new_rows),
            "first_date": str(rows[0]["trade_date"]),
            "last_date": str(rows[-1]["trade_date"]),
            "median_rate": median,
            "mid_source_counts": result.mid_source_counts,
            "sample": [
                {"trade_date": str(row["trade_date"]), "rate": row["rate"],
                 "mid_source": row["mid_source"]}
                for row in rows[: max(0, args.sample)]
            ],
            "status": "dry_run",
        }
        path = _write_evidence(evidence, "fetch_fx_rates")
        if path:
            logger.info("证据 JSON: %s", path)
        return

    # ─── 写库路径（本任务未执行） ────────────────────────────────
    from app.core.storage.schema import init_duckdb_schema

    init_duckdb_schema(duck)
    backup_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-ah-fx-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = str(backup_dir / "fx_rate_daily.parquet").replace("'", "''")
    with duck.write_connection() as conn:
        conn.execute(f"COPY fx_rate_daily TO '{backup_path}' (FORMAT PARQUET)")
    logger.info("[BACKUP] fx_rate_daily → %s", backup_path)

    if any_write_lock_active(paths.duckdb_path):
        logger.error("写前再次检测到写锁生效，中止。")
        raise SystemExit(3)

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    params = [
        (HKD_CNY_PAIR, row["trade_date"], row["rate"], FX_RATE_SOURCE, fetched_at)
        for row in rows
    ]
    written = 0
    for start in range(0, len(params), 1000):
        with duck.transaction() as conn:
            conn.executemany(UPSERT_SQL, params[start: start + 1000])
        written += len(params[start: start + 1000])
    logger.info(
        "完成：请求 %d 行，落库 %d 行，pair=%s，status=ok",
        len(rows), written, HKD_CNY_PAIR,
    )
    evidence = {
        "task": "抓取 HKD/CNY 中行牌价历史 → fx_rate_daily",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": False,
        "args": {**vars(args), "start_date": str(args.start_date), "end_date": str(end_date)},
        "pair": HKD_CNY_PAIR,
        "source": FX_RATE_SOURCE,
        "upstream": result.upstream,
        "existing_rows": existing["row_count"],
        "request_range": [str(fetch_start), str(end_date)],
        "returned_rows": len(rows),
        "rows_written": written,
        "first_date": str(rows[0]["trade_date"]),
        "last_date": str(rows[-1]["trade_date"]),
        "median_rate": median,
        "mid_source_counts": result.mid_source_counts,
        "backup_path": backup_path,
        "status": "ok",
    }
    path = _write_evidence(evidence, "fetch_fx_rates")
    if path:
        logger.info("证据 JSON: %s", path)


if __name__ == "__main__":
    main()
