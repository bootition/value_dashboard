"""Repair NULL announcement_date in dividends by re-querying CNINFO via AKShare.

The previous supplement run fetched real ex_dates for 53,877 records across 5,074
stocks, but announcement_date was not captured because the AKShare adapter field
map and the supplement scripts column-handling logic did not match the actual
CNINFO API column name "实施公告日期".

This script re-queries only stocks that currently have dividends with missing
announcement_date and updates matching records by (stock_code, ex_date).

P1-3 fix: the dividend UPDATE, raw_response_archive, fetch_batch, and
source_audit writes now share one DuckDB transaction through the canonical
DataInitializer record helpers, so a partial failure cannot leave half-written
lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import akshare as ak

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths
from app.core.storage.schema import init_duckdb_schema
from app.core.storage.sqlite_store import SQLiteStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RATE_LIMIT = 0.5


def _strip_code(code: str) -> str:
    return code.strip().zfill(6)


def repair(duck: DuckDBStore, init: DataInitializer, dry_run: bool, sample: int = 0) -> dict:
    """Re-fetch announcement dates and update only NULL records."""
    stocks = duck.read_query(
        """SELECT DISTINCT stock_code FROM dividends
           WHERE announcement_date IS NULL
             AND ex_date IS NOT NULL
           ORDER BY stock_code"""
    )
    stock_codes = [row["stock_code"] for row in stocks]
    if sample > 0:
        stock_codes = stock_codes[:sample]
    logger.info("候选股票: %d (总 NULL announcement_date 目标)", len(stock_codes))

    updated = 0
    errors = 0
    no_match = 0

    for i, plain_code in enumerate(stock_codes):
        try:
            time.sleep(RATE_LIMIT)

            existing = duck.read_query(
                "SELECT ex_date, dividend_per_share FROM dividends WHERE stock_code = ? AND announcement_date IS NULL AND ex_date IS NOT NULL ORDER BY ex_date",
                [plain_code],
            )
            if not existing:
                continue
            existing_by_date = {str(row["ex_date"])[:10]: row["dividend_per_share"] for row in existing}

            df = ak.stock_dividend_cninfo(symbol=plain_code)
            if df is None or len(df) == 0:
                logger.debug("  %s: CNINFO 无数据", plain_code)
                continue

            matched_rows: list[dict] = []
            fetched_any = False
            for _, row in df.iterrows():
                ex_date_raw = row.get("除权日") or row.get("除权除息日")
                if ex_date_raw is None or (hasattr(ex_date_raw, '__module__') and str(ex_date_raw) == "nan"):
                    continue
                ex_date_str = str(ex_date_raw).strip()
                if not ex_date_str or ex_date_str in ("nan", "NaT") or len(ex_date_str) < 10:
                    continue
                ex_date = ex_date_str[:10]

                try:
                    datetime.strptime(ex_date, "%Y-%m-%d")
                except ValueError:
                    continue

                if ex_date not in existing_by_date:
                    continue

                announce_date_raw = row.get("实施方案公告日期")
                announce_date = None
                if announce_date_raw is not None:
                    raw_str = str(announce_date_raw).strip()
                    if raw_str and raw_str not in ("nan", "NaT", "None") and len(raw_str) >= 10:
                        try:
                            announce_date = raw_str[:10]
                            datetime.strptime(announce_date, "%Y-%m-%d")
                        except ValueError:
                            announce_date = None

                if announce_date is not None:
                    fetched_any = True
                    matched_rows.append({
                        "stock_code": plain_code,
                        "ex_date": ex_date,
                        "announcement_date": announce_date,
                        "dividend_per_share": existing_by_date[ex_date],
                    })
                    updated += 1

            if matched_rows and not dry_run:
                raw = df.to_json(orient="records", date_format="iso", force_ascii=False)
                result = FetchResult(
                    data=matched_rows,
                    metadata=SourceMetadata(
                        source="cninfo",
                        fetch_time=datetime.now(timezone.utc),
                        raw_response_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        confidence="approximate",
                        api_version="cninfo-dividend-1",
                        row_count=len(matched_rows),
                    ),
                    raw_response=raw.encode("utf-8"),
                )
                try:
                    # 业务行 + raw_response_archive + fetch_batch + source_audit
                    # 同一事务，全部提交或全部回滚 (P1-3)。
                    with duck.transaction() as conn:
                        for row in matched_rows:
                            conn.execute(
                                """UPDATE dividends SET announcement_date = ?
                                   WHERE stock_code = ? AND ex_date = ? AND announcement_date IS NULL""",
                                [row["announcement_date"], plain_code, row["ex_date"]],
                            )
                        batch_id = init._record_batch_in_connection(
                            conn, result, "dividends", len(matched_rows),
                        )
                        init._record_field_audit_in_connection(
                            conn, result, matched_rows, plain_code, "ex_date", batch_id,
                        )
                except Exception as error:
                    logger.error("  %s canonical write failed: %s", plain_code, error)
                    errors += 1

            if not fetched_any and existing:
                no_match += 1
                if no_match <= 3:
                    logger.debug("  %s: %d 条待更新但 CNINFO 无匹配", plain_code, len(existing))

            if (i + 1) % 100 == 0:
                logger.info("进度: %d/%d, 已更新 %d 条, 错误 %d", i + 1, len(stock_codes), updated, errors)

        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning("  %s 失败: %s", plain_code, e)

    logger.info("完成: updated=%d, no_match=%d, errors=%d", updated, no_match, errors)
    result = {"updated": updated, "no_match_stocks": no_match, "errors": errors, "stocks_processed": len(stock_codes)}
    if not dry_run:
        remaining = duck.read_query("SELECT COUNT(*) AS count FROM dividends WHERE announcement_date IS NULL AND ex_date IS NOT NULL")[0]["count"]
        result["remaining_null"] = remaining
        logger.info("剩余 NULL announcement_date: %d", remaining)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sample", type=int, default=0)
    args = parser.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    store = DuckDBStore(paths=paths)
    init_duckdb_schema(store)
    init = DataInitializer(duck=store, sqlite=SQLiteStore(paths=paths))
    logger.info("模式: %s 数据库: %s", "DRY RUN" if args.dry_run else "WRITE", paths.duckdb_path)

    result = repair(store, init, dry_run=args.dry_run, sample=args.sample)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
