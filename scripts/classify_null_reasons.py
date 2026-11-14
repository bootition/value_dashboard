"""CLI：把指标表的 NULL 分类成 6 种原因并写入 `indicator_null_reason`。

为什么单独做成脚本（docs/reports/119 §5 P2-1）
--------------------------------------------
空值原因清扫是**离线维护动作**，不是请求路径上的事：一次全库清扫要扫
`indicator_snapshot`(5.5k) + `indicator_ext`(5.5k/期) 并回查源三表与分红，
写进去是几万行稀疏记录。放进 Web 请求里会把状态页拖垮，放进自动更新里
会把「取数」和「体检」两件事耦死。

安全边界（正式库只读优先）
--------------------------
- **默认 dry-run**：不加 `--yes` 只统计、绝不写库。
- **`--yes` 才写**：写之前先查写锁 `any_write_lock_active`，有别的进程在写就退出，
  不排队、不抢。
- **单写者**：真正的写窗口用 `exclusive_update` 保护（与 CLI 其它写命令同一条锁），
  再叠加 `DuckDBStore.transaction()` 的 DuckDB 写锁，跨进程串行。
- 只写 `indicator_null_reason` 一张稀疏表，不碰任何指标/源数据表。

用法
----
    # 看统计（只读）
    .venv/Scripts/python.exe scripts/classify_null_reasons.py

    # 只清扫扩展指标域
    .venv/Scripts/python.exe scripts/classify_null_reasons.py --domain ext --dry-run

    # 只清扫源表域（扣非/固定资产/银行监管指标这类"有源、没调度"的重灾区）
    .venv/Scripts/python.exe scripts/classify_null_reasons.py --domain income --dry-run

    # 真写（需要显式确认 + 无其它写者）
    .venv/Scripts/python.exe scripts/classify_null_reasons.py --domain all --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.quality.null_reason_sweeper import SweepResult, sweep  # noqa: E402
from app.core.quality.null_reasons import (  # noqa: E402
    DOMAINS,
    NullReasonTableMissing,
    null_reason_table_exists,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    any_write_lock_active,
    exclusive_update,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("classify_null_reasons")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="归因指标表 NULL 并写入 indicator_null_reason（默认 dry-run）",
    )
    parser.add_argument(
        "--domain",
        choices=[*DOMAINS, "all"],
        default="all",
        help=(
            "清扫范围：snapshot | ext | income | balance | cashflow | all（默认 all）；"
            "源表域字段带前缀写入（如 income.deducted_net_profit）"
        ),
    )
    parser.add_argument(
        "--report-date",
        default=None,
        help="只清扫指定报告期（YYYY-MM-DD，默认取该域最新报告期）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="真正写库（缺省 = dry-run，只统计不写）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="只输出 JSON 结果（供脚本/报告采集）",
    )
    return parser.parse_args(argv)


def _report_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise SystemExit(f"--report-date 需为 YYYY-MM-DD：{value!r}（{error}）") from error


def _print_summary(result: SweepResult) -> None:
    payload = result.as_dict()
    print(
        f"\n== {payload['domain']} ({payload['table']} @ {payload['report_date']}) =="
        f"\n   扫描行数        {payload['rows_scanned']}"
        f"\n   NULL 单元格      {payload['null_cells']}"
        f"\n   可归因(计划写入) {payload['planned_rows']}"
        f"\n   判不出(不写)     {payload['unclassified_cells']}"
        f"\n   本次实写        {payload['written_rows']}"
    )
    if payload["by_reason"]:
        print("   原因分布: " + "  ".join(f"{k}={v}" for k, v in payload["by_reason"].items()))
    if payload["not_dispatched_fields"]:
        print("   NOT_DISPATCHED 字段: " + ", ".join(payload["not_dispatched_fields"]))
    if payload["report_period_misaligned"]:
        print(f"   报告期错配: {payload['report_period_detail']}")
    if payload["unclassified"]:
        top = sorted(payload["unclassified"].items(), key=lambda kv: -kv[1])[:10]
        print("   判不出的字段(前 10): " + ", ".join(f"{name}={count}" for name, count in top))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        print(f"[ABORT] 无法解析正式库路径：{error}", file=sys.stderr)
        return 2

    write = bool(args.yes)
    if write and any_write_lock_active(paths.duckdb_path):
        print(
            "[ABORT] 检测到写锁（自动更新或其它维护进程正在写库）。"
            "单写者串行：等它结束后重跑，本脚本不排队、不抢锁。",
            file=sys.stderr,
        )
        return 3

    store = DuckDBStore(paths=paths)
    table_ready = null_reason_table_exists(store)
    if not table_ready:
        # dry-run 照常出统计（完全不依赖该表）；但必须提前说清 --yes 会中止
        print(
            "[NOTE] indicator_null_reason 尚未创建（正式库 schema 未迁移到建表版本）："
            "dry-run 不受影响，--yes 会在写入前中止。",
            file=sys.stderr,
        )
    domains = list(DOMAINS) if args.domain == "all" else [args.domain]
    report_date = _report_date(args.report_date)
    if not args.json:
        print(
            f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}"
            f"  域: {', '.join(domains)}  报告期: {report_date or '最新'}"
        )

    results: dict[str, dict] = {}
    try:
        if write:
            # exclusive_update：跨进程单写者（与 vd data update 同一条锁）
            with exclusive_update(paths.duckdb_path):
                for domain in domains:
                    swept = sweep(store, domain=domain, report_date=report_date, write=True)
                    results[domain] = swept.as_dict()
                    if not args.json:
                        _print_summary(swept)
        else:
            for domain in domains:
                swept = sweep(store, domain=domain, report_date=report_date, write=False)
                results[domain] = swept.as_dict()
                if not args.json:
                    _print_summary(swept)
    except UpdateLockError as error:
        print(f"[ABORT] 另一个更新进程持有写锁：{error}", file=sys.stderr)
        return 3
    except NullReasonTableMissing as error:
        # 建表属于发布动作（schema 迁移 + 单写者），清扫器不擅自 DDL
        print(f"[ABORT] {error}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    elif write:
        total = sum(int(item.get("written_rows") or 0) for item in results.values())
        print(f"\n已写入 indicator_null_reason：{total} 行")
    else:
        total = sum(int(item.get("planned_rows") or 0) for item in results.values())
        print(f"\n[DRY RUN] 预计写入 indicator_null_reason：{total} 行（加 --yes 才会写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
