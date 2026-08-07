"""清理正式库测试期残留（一次性维护脚本）。

来源：2026-07-17~21 功能/S1 测试期直接写入正式库的 DSL 表达式草稿/发布与筛选结果
（路径隔离契约于 2026-08-02 生效，此后测试不再触碰正式库）。

安全护栏：
- 要求 VD_ENV=formal 且 VD_FORMAL_ACK=confirmed，路径校验走正式规格。
- 仅当目标表全部行创建于 2026-07-22 之前（测试期窗口）才允许清空，
  否则拒绝执行并打印需人工核实的行——不误删测试期之后的真实数据。
- 幂等：空表视为无操作。

用法：
  set VD_ENV=formal & set VD_FORMAL_ACK=confirmed
  set VD_DUCKDB_PATH=data\\valuedashboard.duckdb
  set VD_SQLITE_PATH=data\\valuedashboard.sqlite
  python scripts/clean-formal-test-residue.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

TEST_WINDOW_END = datetime.fromisoformat("2026-07-22T00:00:00")

TABLES = ("dsl_expressions", "screening_results")


def _guard_formal() -> sqlite3.Connection:
    from app.core.config import Config
    from app.core.storage.path_policy import resolve_and_validate_paths

    paths = resolve_and_validate_paths()
    if paths.env.value != "formal":
        raise SystemExit("仅允许在 formal profile 下运行")
    if paths.duckdb_path.as_posix().endswith("/data/valuedashboard.duckdb") is False:
        raise SystemExit("DuckDB 路径不符合正式库规格")
    Config.load(paths=paths)
    return sqlite3.connect(str(paths.sqlite_path))


def main() -> None:
    connection = _guard_formal()
    try:
        for table in TABLES:
            rows = connection.execute(
                f"SELECT COUNT(*) AS total, COALESCE(MAX(created_at), '') AS latest "
                f"FROM {table}"
            ).fetchone()
            count, latest = int(rows[0] or 0), rows[1] or ""
            if count == 0:
                print(f"[ok] {table}: 已为空")
                continue
            latest_dt = datetime.fromisoformat(latest) if latest else None
            if latest_dt is None or latest_dt >= TEST_WINDOW_END:
                print(
                    f"[skip] {table}: 存在 {TEST_WINDOW_END:%Y-%m-%d} 之后的 {count} 行，"
                    f"最新 {latest}；拒绝清空，请人工核实"
                )
                continue
            with connection:  # 原子事务
                connection.execute(f"DELETE FROM {table}")
                connection.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
            print(f"[ok] {table}: 删除 {count} 条测试期残留")
    finally:
        connection.close()


if __name__ == "__main__":
    main()