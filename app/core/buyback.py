"""回购/注销事件低频域（2026-08-26，分红融资比“广义分红”数据补齐）

数据源：东财股票回购明细（ak.stock_repurchase_em），一次请求返回全市场，
故采用全量替换写入 buyback_events，不进入 stock_meta / 筛选池 / readiness。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["BuybackUpdater"]


class BuybackUpdater:
    """回购事件低频率全量刷新器。"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        adapter: AdapterManager | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("BuybackUpdater requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("BuybackUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        self.adapter = adapter or AdapterManager()

    def refresh_all(self) -> dict[str, Any]:
        """全量拉取东财回购明细并原子替换 buyback_events。"""
        result = self.adapter.fetch(FetchRequest(data_type="buyback_funding"))
        if result.metadata.error:
            return {
                "status": "failed",
                "error": result.metadata.error,
                "retained": True,
            }
        # 全量替换语义的防数据丢失门槛：源返回空列表时（akshare 部分时段
        # 会返回 0 行且不带 error），必须保留既有回购事件，不能 DELETE 后
        # 空写。否则一次瞬时空响应就会清空 dividend_financing_ratio_pct
        # 的“回购注销”输入。
        if not result.data:
            logger.warning("东财回购明细返回空列表，保留既有回购事件不执行全量替换")
            return {
                "status": "failed",
                "reason": "source_empty",
                "error": "eastmoney repurchase source returned no rows",
                "retained": True,
            }

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(UTC)
        with self.duck.transaction() as conn:
            conn.execute("DELETE FROM buyback_events")
            if result.data:
                conn.executemany(
                    """INSERT INTO buyback_events
                       (stock_code, start_date, announce_date, buyback_shares,
                        buyback_amount, progress, source, fetch_time, raw_hash,
                        confidence, batch_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        [
                            row["stock_code"],
                            row.get("start_date"),
                            row.get("announce_date"),
                            row.get("buyback_shares"),
                            row.get("buyback_amount"),
                            row.get("progress"),
                            row.get("source", "eastmoney_repurchase"),
                            fetch_time,
                            result.metadata.raw_response_hash,
                            result.metadata.confidence,
                            batch_id,
                        ]
                        for row in result.data
                    ],
                )
        return {
            "status": "success",
            "batch_id": batch_id,
            "event_rows": len(result.data),
            "source": result.metadata.source,
        }

    def status_report(self) -> dict[str, Any]:
        try:
            rows = self.duck.read_query(
                """SELECT
                       COUNT(*) AS rows,
                       COUNT(DISTINCT stock_code) AS stocks,
                       SUM(buyback_amount) AS total_amount,
                       MIN(start_date) AS min_date,
                       MAX(start_date) AS max_date
                   FROM buyback_events"""
            )[0]
        except Exception as error:
            return {"status": "error", "error": str(error)}
        return {
            "status": "ok",
            "event_rows": rows["rows"],
            "stocks": rows["stocks"],
            "total_amount": rows["total_amount"],
            "min_date": str(rows["min_date"]) if rows["min_date"] else None,
            "max_date": str(rows["max_date"]) if rows["max_date"] else None,
        }
