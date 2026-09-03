"""source_audit 冷热分离维护（2026-09-03）。

日常数据质量/lineage 检查只需要最近价格与最近完整财报的溯源行；
历史审计行按 report_date 迁入 source_audit_archive 后，hot 表大幅变小，
全量核对不再扫描数千万历史行。历史排查通过 source_audit_all 视图查询。

迁移按主键 id 分页，每批一个独立 DuckDB 事务：
- 写锁窗口有界，Web 服务在批次之间可继续读取；
- 批次失败可重跑，已插入的 archive 行有主键不会重复；
- 每批 INSERT 与 DELETE 行数必须一致，否则回滚该批。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

STATE_KEY = "source_audit_archive_state"

# source_audit 热表与归档表共同列。archive 表另有 archived_at 列。
_COLUMNS = (
    "id", "stock_code", "field_name", "report_date", "value", "source",
    "fetch_batch_id", "fetch_time", "raw_response_hash", "confidence",
    "reason_code", "api_version", "is_override", "override_id", "created_at",
    "effective_date", "data_version", "formula",
)


def read_archive_state(sqlite: SQLiteStore) -> dict[str, Any] | None:
    """读取最近一次归档元数据（只读）。"""
    rows = sqlite.query(
        "SELECT value, updated_at FROM data_refresh_state WHERE key = ?",
        [STATE_KEY],
    )
    if not rows:
        return None
    try:
        state = json.loads(rows[0]["value"])
    except (json.JSONDecodeError, TypeError):
        return {"legacy_value": rows[0]["value"], "updated_at": rows[0]["updated_at"]}
    state["updated_at"] = rows[0]["updated_at"]
    return state


def _store_archive_state(sqlite: SQLiteStore, state: dict[str, Any]) -> None:
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            [STATE_KEY, json.dumps(state, ensure_ascii=False, default=str),
             datetime.now(UTC).isoformat()],
        )


def archive_before(
    duck: DuckDBStore,
    sqlite: SQLiteStore,
    before: date,
    *,
    batch_size: int = 20_000,
    max_batches: int = 0,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Move source_audit rows with report_date < before into the archive table.

    Returns a report dict; safe to re-run (idempotent).
    """
    if batch_size < 1_000 or batch_size > 500_000:
        raise ValueError("batch_size must be within [1000, 500000]")

    rows = duck.read_query(
        "SELECT MIN(id) AS lo, MAX(id) AS hi, COUNT(*) AS total "
        "FROM source_audit"
    )[0]
    hi = int(rows["hi"]) if rows["hi"] is not None else 0
    if hi <= 0:
        return {
            "status": "skipped", "reason": "empty_hot_audit",
            "before": str(before), "archived_rows": 0,
        }

    eligible = duck.read_query(
        "SELECT COUNT(*) AS c FROM source_audit WHERE report_date < ?",
        [str(before)],
    )[0]["c"]
    if int(eligible) == 0:
        _store_archive_state(sqlite, {
            "before": str(before), "archived_rows": 0,
            "archived_at": datetime.now(UTC).isoformat(),
            "hot_rows": int(rows["total"]),
        })
        return {
            "status": "success", "before": str(before),
            "eligible_rows": 0, "archived_rows": 0,
            "hot_rows": int(rows["total"]),
        }

    batches_done = 0
    archived_total = 0
    cursor = 0
    while True:
        id_rows = duck.read_query(
            """SELECT id FROM source_audit
               WHERE report_date < ? AND id > ?
               ORDER BY id ASC LIMIT ?
            """,
            [str(before), cursor, batch_size],
        )
        if not id_rows:
            break
        ids = [int(row["id"]) for row in id_rows]
        cursor = ids[-1]
        placeholders = ", ".join("?" for _ in ids)
        with duck.transaction() as conn:
            hot_before = conn.execute(
                f"SELECT COUNT(*) AS c FROM source_audit WHERE id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            if hot_before == 0:
                continue
            conn.execute(
                f"""INSERT INTO source_audit_archive
                    ({', '.join(_COLUMNS)}, archived_at)
                    SELECT {', '.join(_COLUMNS)}, CURRENT_TIMESTAMP
                    FROM source_audit
                    WHERE id IN ({placeholders})
                """,
                ids,
            )
            archived_in_batch = conn.execute(
                f"SELECT COUNT(*) AS c FROM source_audit_archive "
                f"WHERE id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            conn.execute(
                f"DELETE FROM source_audit WHERE id IN ({placeholders})",
                ids,
            )
            hot_after = conn.execute(
                f"SELECT COUNT(*) AS c FROM source_audit WHERE id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            if hot_after != 0 or archived_in_batch < hot_before:
                raise RuntimeError(
                    f"archive batch mismatch: hot_before={hot_before}, "
                    f"archived={archived_in_batch}, hot_after={hot_after}"
                )
            inserted = hot_before
        archived_total += inserted
        batches_done += 1
        if progress_cb is not None:
            progress_cb({
                "batch": batches_done, "cursor": cursor,
                "batch_rows": inserted, "total_archived": archived_total,
            })
        if max_batches and batches_done >= max_batches:
            break

    hot_rows = duck.read_query("SELECT COUNT(*) AS c FROM source_audit")[0]["c"]
    state = {
        "before": str(before),
        "archived_rows": archived_total,
        "hot_rows": hot_rows,
        "archived_at": datetime.now(UTC).isoformat(),
    }
    _store_archive_state(sqlite, state)
    return {
        "status": "success" if archived_total == int(eligible) else "partial",
        "before": str(before),
        "eligible_rows": int(eligible),
        "archived_rows": archived_total,
        "hot_rows": hot_rows,
        "batches_done": batches_done,
    }
