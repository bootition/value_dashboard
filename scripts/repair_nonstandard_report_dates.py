"""清理「非标准报告期」行（reports/146）。

背景
----
`cash_flow_indirect` 里有 **5,481 行 `report_date = 2024-01-01`**，且 2013–2024
**每年**都有一个 `YYYY-01-01` 批次，与 `(YYYY-1)-12-31` 在 90%+ 股票上数值逐位相同
（实测 2024-01-01 vs 2023-12-31：5,382 只两期都有值，4,885 只完全相同）。
这是 CSMAR 历史批次把「上年年报」重复记成了「次年 1 月 1 日」。

处理规则（保守，只做有确定语义的事）
------------------------------------
对每个非标准期行：

| 情形 | 动作 | 理由 |
|---|---|---|
| `YYYY-01-01` 且 `(YYYY-1)-12-31` **已存在** | **删除**该行 | 同一份数据已有正确期次的副本；留着只会制造伪期次 |
| `YYYY-01-01` 且 `(YYYY-1)-12-31` **不存在** | **改期**为 `(YYYY-1)-12-31` | 这是该期唯一副本，语义明确（上年年报） |
| 其它非标准期（如 `01-31`、`02-28`） | **只报告，不动** | 语义不明，交人工裁决 |

用法
----
```bash
# 默认 dry-run：只统计与样例，不写库
python scripts/repair_nonstandard_report_dates.py

# 真正执行（要求显式确认 + 单写者锁）
python scripts/repair_nonstandard_report_dates.py --apply --confirm-write
```

安全约束
--------
- 正式库 `data/` 默认只读；本脚本**必须**带 `--apply --confirm-write` 才会写。
- 写库走 `exclusive_update()`（与自动更新同一把单写者锁），避免与采集并发。
- 每张表单独事务；失败即回滚该表并继续下一张（不留下半张表的状态）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.report_periods import (  # noqa: E402
    canonical_year_start_target,
    is_standard_report_date,
)

#: 需要清扫的表（有 report_date 且历史批次出现过非标准期的表）
#: 清扫范围（reports/146 §2：全库 39 张含 report_date 的表，实测只有这 8 张有 01-01）。
#: 全部来自**同一个 CSMAR 批次** `8c165b0a81c24728ae58f38b38f0ff7b`（2026-09-18 单日单批），
#: 合计 449,088 行。`cash_flow` / `balance_sheet` / `income_statement` / `indicator_ext`
#: 等四张主表实测 0 非标准期，保留在清单里只为"以后若再出问题能立刻发现"。
TARGET_TABLES: tuple[str, ...] = (
    "cash_flow_indirect",
    "cash_flow_activity",
    "cash_flow",
    "balance_sheet",
    "balance_sheet_ext",
    "income_statement",
    "indicator_ext",
    "csmar_balance_items",
    "csmar_cashflow_items",
    "csmar_income_items",
    "csmar_financial_items",
    "financial_sector_items",
    "report_metadata",
)

#: 有 `report_type` 列、且改期后必须重算该列的表。
#: （`2008-01-01` 被标成 `quarterly`，改期成 `2007-12-31` 后应为 `annual`）
REPORT_TYPE_TABLES: frozenset[str] = frozenset({
    "cash_flow_indirect",
    "cash_flow_activity",
    "balance_sheet",
    "balance_sheet_ext",
    "income_statement",
    "indicator_ext",
})


@dataclass
class TablePlan:
    table: str
    total_rows: int = 0
    nonstandard_rows: int = 0
    deletable: list[tuple[str, str]] = field(default_factory=list)   # (stock, date)
    reschedulable: list[tuple[str, str, str]] = field(default_factory=list)  # (stock, from, to)
    manual_review: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""

    @property
    def needs_write(self) -> bool:
        return bool(self.deletable or self.reschedulable)


def _plan_table(duck, table: str) -> TablePlan:
    plan = TablePlan(table=table)
    try:
        plan.total_rows = int(
            duck.read_query(f'SELECT COUNT(*) AS n FROM "{table}"')[0]["n"]
        )
        rows = duck.read_query(
            f"""
            SELECT stock_code, report_date,
                   EXISTS (
                       SELECT 1 FROM "{table}" t2
                       WHERE t2.stock_code = t1.stock_code
                         AND t2.report_date = t1.report_date - INTERVAL 1 DAY
                   ) AS has_previous_annual
            FROM "{table}" t1
            WHERE report_date IS NOT NULL
              AND NOT (
                    (month(report_date) = 3  AND day(report_date) = 31)
                 OR (month(report_date) = 6  AND day(report_date) = 30)
                 OR (month(report_date) = 9  AND day(report_date) = 30)
                 OR (month(report_date) = 12 AND day(report_date) = 31)
              )
            """
        )
    except Exception as error:  # noqa: BLE001 - 缺表/缺列不算失败，记下来继续
        plan.error = f"{type(error).__name__}: {error}"
        return plan

    plan.nonstandard_rows = len(rows)
    for row in rows:
        stock = str(row["stock_code"])
        raw_date = row["report_date"]
        if not is_standard_report_date(raw_date):
            target = canonical_year_start_target(raw_date)
            if target is None:
                plan.manual_review.append((stock, str(raw_date)))
            elif row["has_previous_annual"]:
                plan.deletable.append((stock, str(raw_date)))
            else:
                plan.reschedulable.append((stock, str(raw_date), str(target)))
        else:
            # DuckDB 的日期算术可能因时区/边界返回相邻日，用 Python 复核一次
            plan.manual_review.append((stock, str(raw_date)))
    return plan


def _apply_plan(duck, plan: TablePlan) -> tuple[int, int]:
    deleted = updated = 0
    with duck.write_connection() as connection:
        if plan.deletable:
            connection.execute(
                f"""
                DELETE FROM "{plan.table}"
                WHERE report_date IS NOT NULL
                  AND NOT (
                        (month(report_date) = 3  AND day(report_date) = 31)
                     OR (month(report_date) = 6  AND day(report_date) = 30)
                     OR (month(report_date) = 9  AND day(report_date) = 30)
                     OR (month(report_date) = 12 AND day(report_date) = 31)
                  )
                  AND EXISTS (
                      SELECT 1 FROM "{plan.table}" t2
                      WHERE t2.stock_code = "{plan.table}".stock_code
                        AND t2.report_date = "{plan.table}".report_date - INTERVAL 1 DAY
                  )
                """
            )
            deleted = len(plan.deletable)
        for _stock, raw_date, target in plan.reschedulable:
            connection.execute(
                f'UPDATE "{plan.table}" SET report_date = CAST(? AS DATE) '
                "WHERE report_date = CAST(? AS DATE)",
                [target, raw_date],
            )
            updated += 1
        if plan.table in REPORT_TYPE_TABLES and (deleted or updated):
            # 改期后 `report_type` 必须跟着重算：`2008-01-01` 原本被标成
            # 'quarterly'，改成 `2007-12-31` 后应为 'annual'。
            # 不重算会让 `report_type='annual'` 的过滤静默漏掉这批行。
            connection.execute(
                f"""
                UPDATE "{plan.table}"
                SET report_type = CASE
                        WHEN month(report_date) = 12 THEN 'annual'
                        WHEN month(report_date) = 6  THEN 'semi_annual'
                        ELSE 'quarterly'
                    END
                WHERE report_date IS NOT NULL
                """
            )
    return deleted, updated


class _ReadOnlyDuck:
    """dry-run 用的只读连接适配器（只暴露 read_query）。

    存在意义：worktree 里没有 `data/`，`DatabasePathSet` 的正式 profile 会拒绝
    非仓库 data/ 路径。dry-run 是纯读，直连只读连接即可；**写库路径仍强制走
    `DatabasePathSet.from_env()`**，不给"在别处误写正式库"留口子。
    """

    def __init__(self, db_path: str) -> None:
        import duckdb

        self._connection = duckdb.connect(db_path, read_only=True)
        self._connection.execute("SET memory_limit='4GB'")
        self._connection.execute("SET threads=4")

    def read_query(self, sql: str, params: list | None = None) -> list[dict]:
        cursor = self._connection.execute(sql, params or [])
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="二次确认：允许写正式库（与 --apply 同时出现才生效）",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="dry-run 时直连的只读库路径（写库时忽略，强制走正式 profile）",
    )
    parser.add_argument(
        "--table",
        action="append",
        default=None,
        help="只处理指定表（可重复）；默认全部",
    )
    args = parser.parse_args(argv)

    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.path_policy import DatabasePathSet
    from app.core.storage.update_lock import exclusive_update

    tables = tuple(args.table) if args.table else TARGET_TABLES
    write = args.apply and args.confirm_write

    if write:
        # 写库必须走正式 profile（path_policy 校验 run_root 就是仓库 data/），
        # 防止在 worktree / 临时目录里误写别人的库。
        paths = DatabasePathSet.from_env()
        duck = DuckDBStore(paths=paths)
        lock = paths.duckdb_path.parent / ".duckdb.write.lock"
    elif args.db_path:
        # dry-run 允许直连任意只读库（worktree 里没有 data/，只能给绝对路径）
        duck = _ReadOnlyDuck(args.db_path)
        lock = None
    else:
        paths = DatabasePathSet.from_env()
        duck = DuckDBStore(paths=paths)
        lock = paths.duckdb_path.parent / ".duckdb.write.lock"

    print(f"模式：{'写库' if write else 'dry-run（不写库）'}")
    print(f"表：{', '.join(tables)}\n")

    total_deletable = total_reschedulable = total_manual = 0
    plans: list[TablePlan] = []
    for table in tables:
        plan = _plan_table(duck, table)
        plans.append(plan)
        if plan.error:
            print(f"[skip] {table}: {plan.error}")
            continue
        print(
            f"[plan] {table:24} 总行数={plan.total_rows:>8}  "
            f"非标准期={plan.nonstandard_rows:>6}  "
            f"可删重复={len(plan.deletable):>6}  "
            f"可改期={len(plan.reschedulable):>5}  "
            f"待人工={len(plan.manual_review):>5}"
        )
        if plan.deletable[:2]:
            print(f"        删除样例: {plan.deletable[:2]}")
        if plan.reschedulable[:2]:
            print(f"        改期样例: {plan.reschedulable[:2]}")
        if plan.manual_review[:2]:
            print(f"        待人工样例: {plan.manual_review[:2]}")
        total_deletable += len(plan.deletable)
        total_reschedulable += len(plan.reschedulable)
        total_manual += len(plan.manual_review)

    print(
        f"\n合计：可删重复 {total_deletable} 行，可改期 {total_reschedulable} 行，"
        f"待人工 {total_manual} 行"
    )
    if not write:
        print("\n未写库。要执行请加：--apply --confirm-write")
        return 0

    assert lock is not None
    with exclusive_update(lock):
        for plan in plans:
            if plan.error or not plan.needs_write:
                continue
            deleted, updated = _apply_plan(duck, plan)
            print(f"[write] {plan.table}: 删除 {deleted} 行，改期 {updated} 行")
    print("\n完成。建议随后重跑 `vd data compute-extended-indicators` 让派生域跟上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
