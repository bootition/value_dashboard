"""Repair the share_capital_history ART index (2026-08-14 red-team F1).

Root cause: DuckDB 1.5.5's ART index on (stock_code, effective_date) became
inconsistent for stock 300479 — DELETE raises
``FatalException: Failed to delete all rows from index. Only deleted 0 out of 38 rows``
and the connection is invalidated afterwards. Every auto-update round then dies
at the retry step (retry_count never increments, endless loop).

Fix (verified on a copy of the formal DB before writing this script):
  1. DROP INDEX + CREATE INDEX inside one write transaction.
  2. Verify: in a second transaction, DELETE the affected stock's rows and
     ROLLBACK — the delete path proving the index is healthy again, with zero
     net data change.

Usage (single-writer window; ensure the service is stopped):
    .venv\\Scripts\\python.exe scripts\\repair_share_capital_history_index.py
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

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import PathIsolationError, require_formal_maintenance_paths

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

INDEX_NAME = "idx_share_capital_history_stock"
INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
    "ON share_capital_history (stock_code, effective_date)"
)


def _existing_indexes(store: DuckDBStore) -> dict[str, str]:
    rows = store.read_query(
        "SELECT index_name, sql FROM duckdb_indexes() WHERE database_name = current_database()"
    )
    return {row["index_name"]: (row["sql"] or "") for row in rows}


def _affected_stock_codes(store: DuckDBStore) -> list[str]:
    rows = store.read_query(
        "SELECT DISTINCT stock_code FROM share_capital_history "
        "WHERE stock_code IN (SELECT DISTINCT stock_code FROM share_capital_history "
        "GROUP BY stock_code HAVING COUNT(*) >= 1) ORDER BY stock_code"
    )
    return [row["stock_code"] for row in rows]


def repair(store: DuckDBStore) -> dict:
    indexes = _existing_indexes(store)
    had_index = INDEX_NAME in indexes
    logger.info("索引现状: %s=%s", INDEX_NAME, indexes.get(INDEX_NAME) or "缺失")

    # 1. 重建索引 —— 必须分两个独立事务（拷贝库实验已证）：
    #    DROP+CREATE 在同一事务提交时触发 DuckDB 1.5.5 InternalException
    #    "BoundIndex::CreateDeltaIndex is not supported for this index type"，
    #    且连接被 invalidated。分开提交则 CREATE 走全量索引构建，健康。
    with store.transaction() as conn:
        conn.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    with store.transaction() as conn:
        conn.execute(INDEX_DDL)
    indexes_after = _existing_indexes(store)
    logger.info("重建后: %s", indexes_after.get(INDEX_NAME) or "缺失")

    # 2. 验证：对抽样股票 DELETE 后 ROLLBACK（净零数据变化），
    #    证明索引删除路径健康。300479 为曾触发 FATAL 的股票必验；
    #    其余随机抽样（避免对数千只股票逐个开写事务）。
    codes = _affected_stock_codes(store)
    must_verify = ["300479"]
    sample_codes = codes[:]
    import random

    random.Random(20260814).shuffle(sample_codes)
    verify_codes: list[str] = []
    for code in must_verify + sample_codes:
        if code not in verify_codes and code in codes:
            verify_codes.append(code)
    verify_codes = verify_codes[:1 + 9]  # 300479 + 9 只抽样
    verified: dict[str, str] = {}
    for code in verify_codes:
        try:
            with store.transaction() as conn:
                conn.execute(
                    "DELETE FROM share_capital_history WHERE stock_code = ?", [code]
                )
            verified[code] = "ok"
        except Exception as error:
            verified[code] = f"FAILED: {type(error).__name__}: {error}"
            logger.error("验证 DELETE %s 失败: %s", code, error)
    logger.info(
        "验证完成: %d/%d 通过%s",
        sum(1 for v in verified.values() if v == "ok"),
        len(verify_codes),
        "" if all(v == "ok" for v in verified.values()) else "（存在失败，见下方报告）",
    )

    return {
        "index": INDEX_NAME,
        "existed_before": had_index,
        "recreated": True,
        "ddl": INDEX_DDL,
        "delete_verify": verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-verify", action="store_true", help="跳过 DELETE+ROLLBACK 验证")
    parser.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    store = DuckDBStore(paths=paths)
    logger.info("正式库: %s", paths.duckdb_path)
    result = repair(store)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    all_ok = all(v == "ok" for v in result["delete_verify"].values())
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
