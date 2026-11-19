"""正式库串行维护执行器：先拿写租约，再按顺序跑修复，最后释放（reports/147）。

为什么需要它
------------
`reports/146` 留下三步「需要串行执行」的维护动作，但**没有任何主控会话**。
裸跑的风险：另一个会话正好在写库 → 互相等锁超时 / 覆盖刚写入的行。

本执行器把「抢租约 → 逐步执行 → 续租 → 释放」固化成一个命令，
任何会话都能安全跑；抢不到就直接退出并提示排队，不做忙等。

用法
----
```bash
# 只演练（不写库）：打印将执行哪几步
python scripts/run_db_maintenance.py --dry-run

# 真正执行（自动抢租约）
python scripts/run_db_maintenance.py --owner fix-data-defects

# 只跑某一步
python scripts/run_db_maintenance.py --owner me --only nonstandard_dates
```

步骤与顺序（不可乱）
--------------------
1. `nonstandard_dates` —— 清理 44.9 万行 01-01 错记**必须最先**：
   它会让「上一财年 12-31」多出正确副本，后续步骤依赖干净的报告期。
2. `employee_periods` —— 员工数报告期从「抓取日」改回年度期末。
3. `declare_date_backfill` —— 用 `financial_report_dates.announce_date` 回填公告日。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    argv: tuple[str, ...]


STEPS: tuple[Step, ...] = (
    Step(
        "nonstandard_dates",
        "清理非标准报告期（44.9 万行 01-01 错记）",
        ("scripts/repair_nonstandard_report_dates.py", "--apply", "--confirm-write"),
    ),
    Step(
        "employee_periods",
        "员工数报告期改回年度期末",
        ("scripts/repair_data_defects.py", "--op", "employee_periods",
         "--apply", "--confirm-write"),
    ),
    Step(
        "declare_date_backfill",
        "回填 report_metadata.declare_date",
        ("scripts/repair_data_defects.py", "--op", "declare_date_backfill",
         "--apply", "--confirm-write"),
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="", help="会话标识（写进租约供他人查看）")
    parser.add_argument("--purpose", default="reports/146 串行维护", help="租约用途说明")
    parser.add_argument("--dry-run", action="store_true", help="只打印步骤，不写库")
    parser.add_argument("--only", action="append", default=None, help="只跑指定步骤（可重复）")
    parser.add_argument("--ttl", type=int, default=3600, help="租约秒数（默认 1 小时）")
    args = parser.parse_args(argv)

    steps = [step for step in STEPS if not args.only or step.key in args.only]
    if not steps:
        print(f"没有匹配的步骤；可用：{', '.join(s.key for s in STEPS)}")
        return 2

    print("将执行的步骤：")
    for index, step in enumerate(steps, 1):
        print(f"  {index}. [{step.key}] {step.title}")
    if args.dry_run:
        print("\n--dry-run：不抢租约、不写库。")
        return 0

    if not args.owner:
        print("\n❌ 必须提供 --owner（会写进租约，供其他会话判断谁在写库）")
        return 2

    import session_lease

    lease_args = argparse.Namespace(owner=args.owner, purpose=args.purpose, ttl=args.ttl)
    code = session_lease.cmd_acquire(lease_args)
    if code != 0:
        print("\n👉 未获得租约。请用：python scripts/session_lease.py enqueue "
              f"--owner {args.owner} --purpose \"{args.purpose}\"")
        return code

    failures: list[str] = []
    try:
        for index, step in enumerate(steps, 1):
            print(f"\n{'=' * 60}\n[{index}/{len(steps)}] {step.title}\n{'=' * 60}")
            # 每步前续租：单步可能跑很久，避免租约在长任务中途过期被别的会话回收
            session_lease.cmd_renew(argparse.Namespace(owner=args.owner, ttl=args.ttl))
            result = subprocess.run(
                [sys.executable, *step.argv],
                cwd=str(REPO),
                check=False,
            )
            if result.returncode != 0:
                failures.append(step.key)
                print(f"⚠ 步骤失败（returncode={result.returncode}）：{step.key}")
                print("  后续步骤继续执行（各步之间无强依赖），但请看上面的报错。")
    finally:
        print()
        session_lease.cmd_release(argparse.Namespace(owner=args.owner, force=False))

    if failures:
        print(f"\n❌ 有 {len(failures)} 步失败：{', '.join(failures)}")
        return 1
    print("\n✅ 全部步骤完成。建议随后跑 `vd data compute-extended-indicators` 让派生域跟上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
