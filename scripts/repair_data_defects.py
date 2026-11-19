"""修数据瑕疵（reports/146 §6 的 P1/P2 清单）。

每个 op 独立、可单独跑、默认 dry-run。

```bash
# 看有哪些 op
python scripts/repair_data_defects.py --list

# dry-run 单个 op
python scripts/repair_data_defects.py --op employee_periods --db-path <只读库绝对路径>

# 真正执行（要求 --apply --confirm-write）
python scripts/repair_data_defects.py --op employee_periods --apply --confirm-write
```

设计约束（与 `repair_nonstandard_report_dates.py` 一致）
--------------------------------------------------------
- 默认 dry-run；写库必须 `--apply --confirm-write` 双开关；
- 写库强制走 `DatabasePathSet.from_env()`（worktree 里因 run_root 校验会被拒）；
- dry-run 允许 `--db-path` 直连只读库；
- 写库走 `exclusive_update()` 单写者锁。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.report_periods import annual_period_for, is_standard_report_date  # noqa: E402

#: op 名称 → 一句话说明
OPS: dict[str, str] = {
    "employee_periods": (
        "company_employee_history：把东财批次错记成「抓取当天」的 report_date "
        "改回它当时能看到的最新年度报告期（YYYY-12-31），补齐 FY2025 整期缺失"
    ),
    "declare_date_backfill": (
        "report_metadata.declare_date 仅 10.15%：用 financial_report_dates.announce_date "
        "（100% 覆盖）按 (stock_code, report_date) 回填"
    ),
}


class _ReadOnlyDuck:
    """dry-run 用只读适配器（见 repair_nonstandard_report_dates.py 同名字段说明）。"""

    def __init__(self, db_path: str) -> None:
        import duckdb

        self._connection = duckdb.connect(db_path, read_only=True)
        self._connection.execute("SET memory_limit='4GB'")
        self._connection.execute("SET threads=4")

    def read_query(self, sql: str, params: list | None = None) -> list[dict]:
        cursor = self._connection.execute(sql, params or [])
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


# ─── op: employee_periods ────────────────────────────────────────────


def plan_employee_periods(duck) -> dict:
    """找出 `report_date` 不是标准报告期的员工数行，按「抓取日 → 年度期」归并。"""
    rows = duck.read_query(
        """
        SELECT stock_code, report_date, employee_count, source
        FROM company_employee_history
        WHERE report_date IS NOT NULL
        ORDER BY stock_code, report_date
        """
    )
    moves: list[dict] = []
    for row in rows:
        raw = row["report_date"]
        if is_standard_report_date(raw):
            continue
        target = annual_period_for(raw)
        if target is None:
            continue
        moves.append({
            "stock_code": str(row["stock_code"]),
            "from": str(raw)[:10],
            "to": str(target),
            "employee_count": row["employee_count"],
            "source": row["source"],
        })
    return {"total_rows": len(rows), "moves": moves}


def apply_employee_periods(duck, plan: dict) -> tuple[int, int]:
    """按 (stock_code, source) 把错记行改到年度期；目标已存在则丢弃该行。"""
    moved = dropped = 0
    with duck.write_connection() as connection:
        for item in plan["moves"]:
            exists = connection.execute(
                """SELECT 1 FROM company_employee_history
                   WHERE stock_code = ? AND report_date = CAST(? AS DATE) AND source = ?""",
                [item["stock_code"], item["to"], item["source"]],
            ).fetchone()
            if exists:
                connection.execute(
                    """DELETE FROM company_employee_history
                       WHERE stock_code = ? AND report_date = CAST(? AS DATE)
                         AND source = ?""",
                    [item["stock_code"], item["from"], item["source"]],
                )
                dropped += 1
            else:
                connection.execute(
                    """UPDATE company_employee_history SET report_date = CAST(? AS DATE)
                       WHERE stock_code = ? AND report_date = CAST(? AS DATE)
                         AND source = ?""",
                    [item["to"], item["stock_code"], item["from"], item["source"]],
                )
                moved += 1
    return moved, dropped


# ─── op: declare_date_backfill ───────────────────────────────────────


def plan_declare_date_backfill(duck) -> dict:
    """`report_metadata.declare_date` 为空、但 `financial_report_dates` 有公告日的行。"""
    try:
        rows = duck.read_query(
            """
            SELECT count(*) AS fillable
            FROM report_metadata m
            JOIN financial_report_dates f
              ON f.stock_code = m.stock_code
             AND f.report_date = m.report_date
            WHERE m.declare_date IS NULL
              AND f.announce_date IS NOT NULL
            """
        )
    except Exception as error:  # noqa: BLE001 - 缺表/缺列不算致命
        return {"error": f"{type(error).__name__}: {error}", "fillable": 0}
    total = duck.read_query("SELECT count(*) AS n FROM report_metadata")[0]["n"]
    filled = duck.read_query(
        "SELECT count(*) AS n FROM report_metadata WHERE declare_date IS NOT NULL"
    )[0]["n"]
    return {
        "total_rows": int(total or 0),
        "already_filled": int(filled or 0),
        "fillable": int(rows[0]["fillable"] or 0) if rows else 0,
    }


def apply_declare_date_backfill(duck) -> int:
    with duck.write_connection() as connection:
        before = connection.execute(
            "SELECT count(*) FROM report_metadata WHERE declare_date IS NOT NULL"
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE report_metadata AS m
            SET declare_date = f.announce_date
            FROM financial_report_dates AS f
            WHERE f.stock_code = m.stock_code
              AND f.report_date = m.report_date
              AND m.declare_date IS NULL
              AND f.announce_date IS NOT NULL
            """
        )
        after = connection.execute(
            "SELECT count(*) FROM report_metadata WHERE declare_date IS NOT NULL"
        ).fetchone()[0]
    return int((after or 0) - (before or 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="列出所有 op")
    parser.add_argument("--op", action="append", default=None, help="要执行的 op（可重复）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--confirm-write", action="store_true", help="二次确认写正式库")
    parser.add_argument("--db-path", default=None, help="dry-run 时直连的只读库路径")
    args = parser.parse_args(argv)

    if args.list or not args.op:
        print("可用 op：")
        for name, description in OPS.items():
            print(f"  {name}\n      {description}")
        return 0

    unknown = [name for name in args.op if name not in OPS]
    if unknown:
        print(f"未知 op：{unknown}；用 --list 查看")
        return 2

    write = args.apply and args.confirm_write
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.path_policy import DatabasePathSet
    from app.core.storage.update_lock import exclusive_update

    if write:
        paths = DatabasePathSet.from_env()
        duck = DuckDBStore(paths=paths)
        lock = paths.duckdb_path.parent / ".duckdb.write.lock"
    elif args.db_path:
        duck = _ReadOnlyDuck(args.db_path)
        lock = None
    else:
        paths = DatabasePathSet.from_env()
        duck = DuckDBStore(paths=paths)
        lock = paths.duckdb_path.parent / ".duckdb.write.lock"

    print(f"模式：{'写库' if write else 'dry-run（不写库）'}\n")

    def _run_all() -> None:
        for op in args.op:
            if op == "employee_periods":
                plan = plan_employee_periods(duck)
                print(
                    f"[plan] employee_periods: 总行数={plan['total_rows']}  "
                    f"待改期={len(plan['moves'])}"
                )
                if plan["moves"][:3]:
                    print(f"        样例: {plan['moves'][:3]}")
                if write:
                    moved, dropped = apply_employee_periods(duck, plan)
                    print(f"[write] employee_periods: 改期 {moved} 行，目标已存在丢弃 {dropped} 行")
            elif op == "declare_date_backfill":
                plan = plan_declare_date_backfill(duck)
                if plan.get("error"):
                    print(f"[skip] declare_date_backfill: {plan['error']}")
                    continue
                print(
                    f"[plan] declare_date_backfill: 总行数={plan['total_rows']}  "
                    f"已有值={plan['already_filled']}  可回填={plan['fillable']}"
                )
                if write:
                    filled = apply_declare_date_backfill(duck)
                    print(f"[write] declare_date_backfill: 回填 {filled} 行")

    if write:
        if lock is None:
            raise SystemExit("写库缺少单写者锁路径（不应发生）")
        with exclusive_update(lock):
            _run_all()
    else:
        _run_all()
        print("\n未写库。要执行请加：--apply --confirm-write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
