"""Repair total_shares / circ_shares for stocks currently at zero.

The AKShare adapter previously skipped share-capital columns for SSE stocks
(主板A股, 科创板). After the adapter fix, this script re-fetches listing info
for zero-share-capital stocks only, preserving existing metadata that is already
correct.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import akshare as ak
import pandas as pd

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths
from app.core.storage.schema import init_duckdb_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RATE_LIMIT = 0.5


def _strip_code(code: str) -> str:
    return code.strip().zfill(6)


def _share_count(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return None
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _fetch_sse_shares() -> dict[str, dict[str, float | None]]:
    """SSE exchange lists do not include share capital via AKShare.

    stock_info_sh_name_code returns only 证券代码/简称/全称/上市日期
    for both 主板A股 and 科创板. Per-stock API calls would require 0.5s
    each for ~2,310 stocks and are deferred to a later targeted repair.
    """
    logger.warning("SSE 交易所清单不含总股本/流通股本，需逐股票 API 或授权批量源")
    return {}


def _fetch_szse_bse_shares() -> dict[str, dict[str, float | None]]:
    """SZSE and BSE stocks already have shares; re-fetch only if data is needed."""
    result: dict[str, dict[str, float | None]] = {}

    try:
        time.sleep(RATE_LIMIT)
        df = ak.stock_info_sz_name_code(symbol="A股列表")
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                code = _strip_code(str(row.get("A股代码", "")).strip()).zfill(6)
                if not code:
                    continue
                result[code] = {
                    "total_shares": _share_count(row.get("A股总股本")),
                    "circ_shares": _share_count(row.get("A股流通股本")),
                }
    except Exception as error:
        logger.warning("SZSE share fetch failed: %s", error)

    try:
        time.sleep(RATE_LIMIT)
        df = ak.stock_info_bj_name_code()
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                code = _strip_code(str(row.get("证券代码", "")).strip()).zfill(6)
                if not code:
                    continue
                existing = result.get(code, {})
                existing["total_shares"] = _share_count(row.get("总股本"))
                existing["circ_shares"] = _share_count(row.get("流通股本"))
                result[code] = existing
    except Exception as error:
        logger.warning("BSE share fetch failed: %s", error)

    return result


def repair(duck: DuckDBStore, dry_run: bool) -> dict:
    targets = duck.read_query(
        """SELECT stock_code, exchange, total_shares, circ_shares FROM stock_meta
           WHERE is_listed IS TRUE AND (total_shares IS NULL OR total_shares = 0 OR circ_shares IS NULL OR circ_shares = 0)
           ORDER BY stock_code"""
    )
    target_codes = {row["stock_code"] for row in targets}
    logger.info("目标股票: %d (总股本或流通股本缺失/为零)", len(target_codes))

    if not target_codes:
        return {"updated": 0, "message": "no zero-share-capital stocks found"}

    sse_shares = _fetch_sse_shares()
    logger.info("SSE 股本来源: %d stocks with share data", len(sse_shares))

    szse_bse_shares = _fetch_szse_bse_shares()
    logger.info("SZSE/BSE 股本来源: %d stocks with share data", len(szse_bse_shares))

    all_shares = {**szse_bse_shares, **sse_shares}

    patches = []
    for row in targets:
        code = row["stock_code"]
        shares = all_shares.get(code)
        if shares is None:
            continue
        total = shares.get("total_shares")
        circ = shares.get("circ_shares")
        if total is None and circ is None:
            continue
        if (total is not None and total > 0 and (row["total_shares"] is None or row["total_shares"] == 0)) or \
           (circ is not None and circ > 0 and (row["circ_shares"] is None or row["circ_shares"] == 0)):
            patches.append((code, total, circ))

    if patches and not dry_run:
        with duck.transaction() as conn:
            conn.execute("CREATE TEMP TABLE share_patch (stock_code VARCHAR, total_shares DOUBLE, circ_shares DOUBLE)")
            conn.executemany("INSERT INTO share_patch VALUES (?, ?, ?)", patches)
            conn.execute(
                """UPDATE stock_meta SET
                   total_shares = COALESCE(NULLIF(share_patch.total_shares, NULL), stock_meta.total_shares),
                   circ_shares = COALESCE(NULLIF(share_patch.circ_shares, NULL), stock_meta.circ_shares)
                   FROM share_patch
                   WHERE stock_meta.stock_code = share_patch.stock_code
                     AND (stock_meta.total_shares IS NULL OR stock_meta.total_shares = 0
                       OR stock_meta.circ_shares IS NULL OR stock_meta.circ_shares = 0)"""
            )
            conn.execute("DROP TABLE share_patch")
    updated = len(patches)

    result = {"updated": updated, "total_targets": len(target_codes), "sse_source": len(sse_shares), "szse_bse_source": len(szse_bse_shares)}
    if not dry_run:
        remaining = duck.read_query(
            "SELECT COUNT(*) AS count FROM stock_meta WHERE is_listed IS TRUE AND (total_shares IS NULL OR total_shares = 0 OR circ_shares IS NULL OR circ_shares = 0)"
        )[0]["count"]
        result["remaining_zero"] = remaining
    logger.info("完成: updated=%d, remaining_zero=%d", updated, result.get("remaining_zero", len(target_codes) - updated))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    store = DuckDBStore(paths=paths)
    init_duckdb_schema(store)
    logger.info("模式: %s 数据库: %s", "DRY RUN" if args.dry_run else "WRITE", paths.duckdb_path)

    result = repair(store, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
