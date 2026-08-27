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
from datetime import UTC, datetime
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


def _parse_date(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw in ("nan", "NaT", "None") or len(raw) < 10:
        return None
    day = raw[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day


def _fetch_ths_dividends(stock_code: str, remaining: dict[str, float]) -> list[dict[str, str | float]]:
    """同花顺分红历史作为第三降级源；允许旧数据除权日 ±5 天对齐。"""
    try:
        df = ak.stock_fhps_detail_ths(symbol=stock_code)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    matched: list[dict[str, str | float]] = []
    for target_ex in remaining:
        target_day = datetime.strptime(target_ex, "%Y-%m-%d").date()
        best: dict[str, str | float] | None = None
        best_gap = 9999
        for _, row in df.iterrows():
            ex = _parse_date(row.iloc[6] if len(row) > 6 else None)
            ann = _parse_date(row.iloc[3] if len(row) > 3 else None)
            if not ex or not ann:
                continue
            gap = abs((target_day - datetime.strptime(ex, "%Y-%m-%d").date()).days)
            if gap <= 5 and gap < best_gap:
                best = {
                    "stock_code": stock_code,
                    "ex_date": target_ex,
                    "announcement_date": ann,
                    "dividend_per_share": remaining[target_ex],
                    "source_ex_date": ex,
                    "match_gap_days": gap,
                }
                best_gap = gap
        if best is not None:
            matched.append(best)
    return matched


def _fetch_eastmoney_dividends(stock_code: str, remaining: dict[str, float]) -> list[dict[str, str | float]]:
    """东财 F10 分红历史作为 CNINFO 缺失公告日的降级数据源。"""
    try:
        df = ak.stock_fhps_detail_em(symbol=stock_code)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    matched: list[dict[str, str | float]] = []
    for _, row in df.iterrows():
        ex_date = _parse_date(row.iloc[16] if len(row) > 16 else None)
        if not ex_date or ex_date not in remaining:
            continue
        announce_date = _parse_date(row.iloc[18] if len(row) > 18 else None)
        if announce_date is None:
            announce_date = _parse_date(row.iloc[1] if len(row) > 1 else None)
        if announce_date is None:
            continue
        matched.append({
            "stock_code": stock_code,
            "ex_date": ex_date,
            "announcement_date": announce_date,
            "dividend_per_share": remaining[ex_date],
        })
    return matched


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
                        fetch_time=datetime.now(UTC),
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

            cninfo_dates = {row["ex_date"] for row in matched_rows}
            remaining_by_date = {
                ex_date: dps for ex_date, dps in existing_by_date.items()
                if ex_date not in cninfo_dates
            }
            if remaining_by_date:
                eastmoney_rows = _fetch_eastmoney_dividends(plain_code, remaining_by_date)
                if eastmoney_rows:
                    updated += len(eastmoney_rows)
                    if not dry_run:
                        import json as _json
                        raw = _json.dumps(eastmoney_rows, ensure_ascii=False, default=str)
                        result = FetchResult(
                            data=eastmoney_rows,
                            metadata=SourceMetadata(
                                source="eastmoney_f10",
                                fetch_time=datetime.now(UTC),
                                raw_response_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                                confidence="approximate",
                                api_version="eastmoney-dividend-history-1",
                                row_count=len(eastmoney_rows),
                            ),
                            raw_response=raw.encode("utf-8"),
                        )
                        try:
                            with duck.transaction() as conn:
                                for row in eastmoney_rows:
                                    conn.execute(
                                        """UPDATE dividends SET announcement_date = ?
                                           WHERE stock_code = ? AND ex_date = ? AND announcement_date IS NULL""",
                                        [row["announcement_date"], plain_code, row["ex_date"]],
                                    )
                                batch_id = init._record_batch_in_connection(
                                    conn, result, "dividends", len(eastmoney_rows),
                                )
                                init._record_field_audit_in_connection(
                                    conn, result, eastmoney_rows, plain_code, "ex_date", batch_id,
                                )
                        except Exception as error:
                            logger.error("  %s eastmoney canonical write failed: %s", plain_code, error)
                            errors += 1

            eastmoney_dates = {row["ex_date"] for row in eastmoney_rows}
            ths_remaining = {
                ex_date: dps for ex_date, dps in existing_by_date.items()
                if ex_date not in cninfo_dates and ex_date not in eastmoney_dates
            }
            ths_rows: list[dict[str, str | float]] = []
            if ths_remaining:
                ths_rows = _fetch_ths_dividends(plain_code, ths_remaining)
                if ths_rows:
                    updated += len(ths_rows)
                    if not dry_run:
                        import json as _json
                        raw = _json.dumps(ths_rows, ensure_ascii=False, default=str)
                        result = FetchResult(
                            data=ths_rows,
                            metadata=SourceMetadata(
                                source="ths",
                                fetch_time=datetime.now(UTC),
                                raw_response_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                                confidence="approximate",
                                api_version="ths-dividend-history-1",
                                row_count=len(ths_rows),
                            ),
                            raw_response=raw.encode("utf-8"),
                        )
                        try:
                            with duck.transaction() as conn:
                                for row in ths_rows:
                                    conn.execute(
                                        """UPDATE dividends SET announcement_date = ?
                                           WHERE stock_code = ? AND ex_date = ? AND announcement_date IS NULL""",
                                        [row["announcement_date"], plain_code, row["ex_date"]],
                                    )
                                batch_id = init._record_batch_in_connection(
                                    conn, result, "dividends", len(ths_rows),
                                )
                                init._record_field_audit_in_connection(
                                    conn, result, ths_rows, plain_code, "ex_date", batch_id,
                                )
                        except Exception as error:
                            logger.error("  %s ths canonical write failed: %s", plain_code, error)
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
    if updated and not dry_run:
        try:
            import json as _json
            key = "snapshot_recompute_pending"
            existing_rows = sqlite.query("SELECT value FROM data_refresh_state WHERE key = ?", [key])
            existing = _json.loads(existing_rows[0]["value"]) if existing_rows else []
            pending = sorted(set(str(code) for code in list(existing) + list(stock_codes)))
            sqlite.execute(
                "INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                [key, _json.dumps(pending, ensure_ascii=False)],
            )
            result["pending_snapshot_recompute"] = len(pending)
        except Exception as error:
            logger.warning("登记 snapshot_recompute_pending 失败: %s", error)
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
