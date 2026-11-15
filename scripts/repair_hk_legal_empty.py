"""修复 `missing_list` 里被误标为合法空的港股分红记录。

背景（J4 / reports/120 §5）
---------------------------
旧版 `HKDividendUpdater` 在港股源返回 0 行时一律写
`missing_list(reason_code='source_empty')`，语义是"合法空、永不重试"。
但其中一批 A 股在 `dividends` 表里已有**已实施**分红记录——公司明明
分过红，港股侧 0 行只能是源不完整，不应被永久压掉。
（任务书口径 16 只；2026-11-15 对正式库 open 记录实测为 15 只。）

本脚本只读扫描 `missing_list`（SQLite）并用 `dividends`（DuckDB，只读）
交叉核对，把误标行从 `source_empty` 改为 `source_incomplete`。

为什么选择"改原因"而不是"删记录"
--------------------------------
- 删除会丢失审计痕迹，也无法解释"为什么这只股票曾经被判空"；
- `source_incomplete` 仍然 open，`vd data doctor` / 覆盖率报告能继续看到；
- 港股域的重试判据是"该港股代码在 hk_dividends 无行"（`update_all` 的
  pending 集合），所以改原因后下一轮港股更新会自然重抓，不需要额外写
  `retry_list`；而 `retry_list` 的 `data_type='hk_dividends'` 目前没有
  对应的 `refetch_one` 路径，硬插只会制造 unsupported 噪声。

安全
----
- 默认 dry-run，只读 SQLite / DuckDB；
- `--yes` 才写，且写前把受影响行备份为 JSON 到 `.planning/`；
- 写前检查应用级写锁。

用法
----
    python scripts/repair_hk_legal_empty.py            # dry-run
    python scripts/repair_hk_legal_empty.py --yes      # 改原因并留备份
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _entry in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from app.core.hk_dividends import has_implemented_a_share_dividend  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.sqlite_store import SQLiteStore  # noqa: E402
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

MISSING_FIELD = "hk_dividends"
LEGAL_EMPTY_REASON = "source_empty"
RETRYABLE_REASON = "source_incomplete"


def find_legal_empty_rows(sqlite: SQLiteStore) -> list[dict[str, Any]]:
    """All open `hk_dividends` legal-empty rows (read-only)."""
    return sqlite.query(
        """SELECT id, stock_code, field_name, reason_code, detected_at
           FROM missing_list
           WHERE field_name = ?
             AND reason_code = ?
             AND resolved_at IS NULL
           ORDER BY stock_code""",
        [MISSING_FIELD, LEGAL_EMPTY_REASON],
    )


def find_mislabeled_rows(
    duck: DuckDBStore,
    sqlite: SQLiteStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split open legal-empty rows into (mislabeled, genuinely_empty)."""
    # 预检：DuckDB 读不了就直接失败，避免逐股查询异常被 predicate 的
    # fail-safe（按 source_incomplete 处理）放大成"全部误标"。
    duck.read_query("SELECT COUNT(*) AS n FROM dividends")
    candidates = find_legal_empty_rows(sqlite)
    mislabeled: list[dict[str, Any]] = []
    genuinely_empty: list[dict[str, Any]] = []
    for row in candidates:
        if has_implemented_a_share_dividend(duck, row["stock_code"]):
            mislabeled.append(row)
        else:
            genuinely_empty.append(row)
    return mislabeled, genuinely_empty


def repair_rows(sqlite: SQLiteStore, rows: list[dict[str, Any]]) -> int:
    """Flip reason_code to source_incomplete; returns affected rows."""
    if not rows:
        return 0
    updated = 0
    with sqlite.transaction() as conn:
        for row in rows:
            cursor = conn.execute(
                """UPDATE missing_list
                   SET reason_code = ?, detected_at = CURRENT_TIMESTAMP
                   WHERE id = ?
                     AND reason_code = ?
                     AND resolved_at IS NULL""",
                [RETRYABLE_REASON, row["id"], LEGAL_EMPTY_REASON],
            )
            updated += max(cursor.rowcount, 0)
    return updated


def backup_rows(rows: list[dict[str, Any]]) -> Path:
    """Persist the affected rows as JSON before changing SQLite metadata."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-repair-hk-legal-empty-{timestamp}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": "repair_hk_legal_empty",
        "created_at": datetime.now(UTC).isoformat(),
        "rows": [
            {key: str(value) if key == "detected_at" else value for key, value in row.items()}
            for row in rows
        ],
    }
    target = target_dir / "missing_list_hk_dividends.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把被误标为合法空的港股分红 missing 记录改为 source_incomplete",
    )
    parser.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    args = parser.parse_args(argv)

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    mislabeled, genuinely_empty = find_mislabeled_rows(duck, sqlite)
    print(
        f"[missing_list] open legal_empty {len(mislabeled) + len(genuinely_empty)} 条；"
        f"误标 {len(mislabeled)} 条；真合法空 {len(genuinely_empty)} 条"
    )
    for row in mislabeled:
        print(f"  MISLABELED {row['stock_code']} detected_at={row['detected_at']}")
    for row in genuinely_empty:
        print(f"  legal-empty {row['stock_code']} detected_at={row['detected_at']}")

    if not mislabeled:
        print("[OK] 没有被误标的合法空记录")
        return 0
    if not args.yes:
        print("[DRY RUN] 未写库；确认后加 --yes 把 reason_code 改为 source_incomplete")
        return 0
    if any_write_lock_active(paths.duckdb_path):
        print("[ABORT] 检测到写锁生效，拒绝并发写入。", file=sys.stderr)
        return 3

    backup = backup_rows(mislabeled)
    updated = repair_rows(sqlite, mislabeled)
    print(f"[OK] 已改 {updated} 条为 {RETRYABLE_REASON}；备份: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
