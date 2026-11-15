"""用当前解析器从已存 `plan_explain` 重算港股分红每股股息。

为什么需要它
------------
2026-11-14 修复了港股分红解析器的"汇率当股息"缺陷（89 行含「汇率」，
47 行值错）。当时的修复脚本是一次性 ad-hoc SQL。本脚本把"重算"固化成
可重复、可审计的维护通道：

- 输入：`hk_dividends` 全表（不只含汇率行），只读 `plan_explain` 原文；
- 解析：调用与线上同一份 `parse_hk_plan_explain()`；
- 写库语义（保守）：
  * 解析出某一侧（HKD/CNY）才更新该侧；
  * 解析器返回 None 的一侧保留库中旧值，**绝不清空**；
  * 两侧都解析不出的行整行跳过并计入"未解析"；
- 默认 dry-run；`--yes` 才写；写前 Parquet 备份 + 单写者锁检查。

用法
----
    python scripts/reparse_hk_dividends.py            # dry-run（只读，打印计划）
    python scripts/reparse_hk_dividends.py --yes      # 备份后写库
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _entry in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from app.core.adapters.hk_dividend_adapter import parse_hk_plan_explain  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

# 源侧金额最多 8 位小数；1e-9 绝对容差足以区分"真的变了"与浮点噪声。
_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ReparseChange:
    """一行 `hk_dividends` 的重算结果。"""

    stock_code: str
    ex_date: Any
    plan_explain: str
    old_hkd: float | None
    new_hkd: float | None
    old_cny: float | None
    new_cny: float | None

    @property
    def hkd_changed(self) -> bool:
        return not _same_number(self.old_hkd, self.new_hkd)

    @property
    def cny_changed(self) -> bool:
        return not _same_number(self.old_cny, self.new_cny)

    @property
    def changed_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.hkd_changed:
            fields.append("dividend_per_share_hkd")
        if self.cny_changed:
            fields.append("dividend_per_share_cny")
        return tuple(fields)


@dataclass(frozen=True)
class ReparsePlan:
    total_rows: int
    changes: tuple[ReparseChange, ...]
    unparsed: tuple[dict[str, Any], ...]
    hkd_side_missing: int = 0
    cny_side_missing: int = 0

    @property
    def changed_rows(self) -> int:
        return len(self.changes)

    @property
    def unparsed_rows(self) -> int:
        return len(self.unparsed)

    @property
    def partial_rows(self) -> int:
        return self.hkd_side_missing + self.cny_side_missing

    @property
    def hkd_fields_changed(self) -> int:
        return sum(1 for change in self.changes if change.hkd_changed)

    @property
    def cny_fields_changed(self) -> int:
        return sum(1 for change in self.changes if change.cny_changed)

    def summary(self) -> str:
        return (
            f"总行数 {self.total_rows}；拟变更 {self.changed_rows} 行"
            f"（HKD {self.hkd_fields_changed} 个字段 / CNY {self.cny_fields_changed} 个字段）；"
            f"未解析 {self.unparsed_rows} 行；"
            f"单侧解析 {self.partial_rows} 行"
            f"（缺 HKD {self.hkd_side_missing} / 缺 CNY {self.cny_side_missing}）"
        )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(left - right) <= _TOLERANCE * max(1.0, abs(left), abs(right))


def read_rows(duck: Any) -> list[dict[str, Any]]:
    """Read every HK dividend row needed for a reparse (read-only query)."""
    return duck.read_query(
        """SELECT stock_code, ex_date, plan_explain,
                  dividend_per_share_hkd, dividend_per_share_cny
           FROM hk_dividends
           ORDER BY stock_code, ex_date, plan_explain"""
    )


def build_plan(rows: list[dict[str, Any]]) -> ReparsePlan:
    """Pure function: turn stored rows into a conservative update plan."""
    changes: list[ReparseChange] = []
    unparsed: list[dict[str, Any]] = []
    hkd_side_missing = 0
    cny_side_missing = 0
    for row in rows:
        plan_explain = str(row.get("plan_explain") or "")
        parsed_hkd, parsed_cny = parse_hk_plan_explain(plan_explain)
        if parsed_hkd is None and parsed_cny is None:
            unparsed.append({
                "stock_code": row.get("stock_code"),
                "ex_date": row.get("ex_date"),
                "plan_explain": plan_explain,
            })
            continue
        if parsed_hkd is None:
            hkd_side_missing += 1
        if parsed_cny is None:
            cny_side_missing += 1
        old_hkd = _number(row.get("dividend_per_share_hkd"))
        old_cny = _number(row.get("dividend_per_share_cny"))
        # 解析器返回 None 的一侧保留旧值；只有解析出值才覆盖。
        new_hkd = parsed_hkd if parsed_hkd is not None else old_hkd
        new_cny = parsed_cny if parsed_cny is not None else old_cny
        if _same_number(old_hkd, new_hkd) and _same_number(old_cny, new_cny):
            continue
        changes.append(ReparseChange(
            stock_code=str(row.get("stock_code")),
            ex_date=row.get("ex_date"),
            plan_explain=plan_explain,
            old_hkd=old_hkd,
            new_hkd=new_hkd,
            old_cny=old_cny,
            new_cny=new_cny,
        ))
    return ReparsePlan(
        total_rows=len(rows),
        changes=tuple(changes),
        unparsed=tuple(unparsed),
        hkd_side_missing=hkd_side_missing,
        cny_side_missing=cny_side_missing,
    )


def apply_plan(duck: Any, plan: ReparsePlan) -> int:
    """Apply a plan inside one transaction; returns updated row count."""
    if not plan.changes:
        return 0
    with duck.transaction() as conn:
        before = conn.execute("SELECT COUNT(*) FROM hk_dividends").fetchone()[0]
        for change in plan.changes:
            assignments: list[str] = []
            params: list[Any] = []
            if change.hkd_changed:
                assignments.append("dividend_per_share_hkd = ?")
                params.append(change.new_hkd)
            if change.cny_changed:
                assignments.append("dividend_per_share_cny = ?")
                params.append(change.new_cny)
            if not assignments:  # pragma: no cover - build_plan 不会产出空变更
                continue
            params.extend([change.stock_code, change.ex_date, change.plan_explain])
            conn.execute(
                f"UPDATE hk_dividends SET {', '.join(assignments)} "
                "WHERE stock_code = ? AND ex_date = ? AND plan_explain = ?",
                params,
            )
        after = conn.execute("SELECT COUNT(*) FROM hk_dividends").fetchone()[0]
        if after != before:
            raise RuntimeError(
                f"reparse 改变了行数: {before} -> {after}，已回滚"
            )
    return len(plan.changes)


def execute_reparse(duck: Any, *, write: bool) -> ReparsePlan:
    """Read + plan; only writes when ``write`` is True and changes exist."""
    plan = build_plan(read_rows(duck))
    if write:
        apply_plan(duck, plan)
    return plan


def _print_unparsed(plan: ReparsePlan, *, limit: int = 10) -> None:
    if not plan.unparsed:
        return
    print(f"  未解析样本（最多 {limit} 条，保留旧值、不写库）：")
    for item in plan.unparsed[:limit]:
        print(
            f"    {item['stock_code']} {item['ex_date']} "
            f"{item['plan_explain']!r}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 plan_explain 重算港股每股股息（默认 dry-run）",
    )
    parser.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    args = parser.parse_args(argv)

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        parser.error(str(error))

    duck = DuckDBStore(paths=paths)
    plan = execute_reparse(duck, write=False)
    print(f"[hk_dividends] {plan.summary()}")
    _print_unparsed(plan)

    if not plan.changes:
        print("[OK] 无需变更（dry-run 与写库结果一致）")
        return 0
    if not args.yes:
        print("[DRY RUN] 未写库；确认后加 --yes 执行（写前会备份 hk_dividends）")
        return 0

    if any_write_lock_active(paths.duckdb_path):
        print("[ABORT] 检测到写锁生效，拒绝并发写入。", file=sys.stderr)
        return 3

    from _maintenance_safety import backup_tables

    backup_tables(duck, ["hk_dividends"], tag="reparse-hk-dividends")
    updated = apply_plan(duck, plan)
    print(f"[OK] 已重算 {updated} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
