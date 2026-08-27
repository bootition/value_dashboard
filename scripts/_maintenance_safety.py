"""维护脚本安全守卫（2026-08-14 红队 P2）。

破坏性维护脚本（patch_deducted_profit / revert_sse_circ / import_csmar 等）
直接修改正式数据库。统一提供：

1. confirm_destructive：--yes 显式确认；非交互（EOFError）无 --yes 时拒绝。
2. backup_tables：写操作前把受影响表 COPY 成 parquet 快照到
   .planning/maintenance-backup-<tag>-<ts>/（.planning 已 gitignore，
   不污染仓库，同时给手工回滚留出口）。

两个防线缺一不可：备份在确认之后、写操作之前执行。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def confirm_destructive(yes: bool) -> bool:
    """Return True only when the operator explicitly confirmed the mutation."""
    if yes:
        return True
    try:
        answer = input("此操作会修改正式数据库，输入 yes 继续: ")
    except EOFError:
        print("[ABORT] 未获得确认（--yes 或交互输入 yes），已终止。")
        return False
    if answer.strip().lower() != "yes":
        print("[ABORT] 未获得确认，已终止。")
        return False
    return True


def backup_tables(duck, tables: list[str], tag: str) -> Path:
    """Snapshot affected tables to parquet under .planning/ before mutation."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PROJECT_ROOT / ".planning" / f"maintenance-backup-{tag}-{timestamp}"
    out.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for table in tables:
            target = str(out / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    print(f"[BACKUP] 受影响表已自动备份到: {out}")
    return out
