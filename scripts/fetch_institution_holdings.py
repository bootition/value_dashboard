"""机构持仓 → `institution_holdings`（schema v49，reports/139 §11.3）。

## 一句话

把「按报告期的机构持股比例」（基金/QFII/社保/券商/保险/信托 六类）按
**(股票 × 报告期 × 持有人类型)** 落进 `institution_holdings`，一个报告期一类持有人
= 一次全市场抓取（约 11 页 / 5,300 行 / 6s）。

## 源与口径

- 源：东方财富数据中心「主力数据-机构持仓」`dataapi/zlsj/list` —— 也就是
  `akshare.stock_report_fund_hold` 的**底层接口**。为什么不用 akshare 包装：1.18.81
  把 22 个原始字段按 23 个列名硬套，整表列错位一格（`股票代码` 列里是 2.87e9 的市值）。
  直接读具名字段既免平移，又拿得回 akshare 丢掉的 `FREESHARES_RATIO`（占流通股比例）。
  实测矩阵见 `.planning/2026-11-18-indicator-feasibility-audit/out/inst_holdings_probe.md`。
- `shares` 单位**股**；`ratio_of_float` 单位**百分数**（13.9565 = 13.96%）；
  `holder_count` 机构家数。源端比例 0 而股数 > 0（新股流通股本未登记）→ 置 NULL 不写 0。
- 报告期 = 四个季末之一；历史实测可回溯到 **2005-06-30**。

## 写库路径（D19/D36 + AGENTS R6）

1. 默认 **dry-run**：真抓一次、打印计划与真实读数，**不写库**；
2. `--yes` 才写：`require_formal_maintenance_paths()` → 单写者锁 `exclusive_update`
   （拿不到锁**立即 exit 3**，不排队死等）→ **逐 (报告期 × 持有人类型) 独立事务**
   「按范围 `DELETE` + 单条 `INSERT ... SELECT ... ON CONFLICT`」→ 事务后回读自证；
3. `--limit` / `--stock` 试点走 `replace_scope=codes`，只覆盖本次写入的股票，
   **不会**连带删掉同一报告期其它股票的已有行；
4. `--resume`：跳过库内已有数据的 (报告期 × 类型)，中断后重跑不重抓。

## 用法

    # dry-run（默认），最近一个已披露季度 × 6 类持有人
    uv run --locked python scripts/fetch_institution_holdings.py

    # 试点：持仓市值最大的 30 只 × 2025Q2 × 6 类，真写
    uv run --locked python scripts/fetch_institution_holdings.py --quarter 2025Q2 --limit 30 --yes

    # 指定股票 + 最近 4 个季度
    uv run --locked python scripts/fetch_institution_holdings.py \\
        --stock 600519,000001,601318,300750 --quarters 4 --yes --json-out evidence.json

    # 全量（主控在决定何时跑）：2005Q1 起全部季度 × 6 类 ≈ 84 期 × 6 × 11 页 ≈ 5,500 req
    uv run --locked python scripts/fetch_institution_holdings.py --since 2005-06-30 --yes

退出码：0 成功 / 2 参数或路径错误 / 3 拿不到单写者锁（跳过）/ 4 抓取或规范化失败 / 5 回读不完整。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.financial_period import cn_today  # noqa: E402
from app.core.institution_holdings import (  # noqa: E402
    HOLDER_TYPES,
    TABLE,
    fetch_holder_period,
    holdings_evidence,
    latest_quarter_end,
    normalize,
    quarter_end,
    quarters_back,
    select_top_stocks,
    write_holdings,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.schema import init_duckdb_schema  # noqa: E402
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_QUARTER_PATTERN = re.compile(r"^(?P<year>20\d{2})[Qq](?P<quarter>[1-4])$")


def _parse_period(text: str) -> date:
    """接受 `2025Q2` / `2025q2` / `2025-06-30` / `20250630` 三种写法。"""
    raw = text.strip()
    match = _QUARTER_PATTERN.match(raw)
    if match:
        return quarter_end(int(match.group("year")), int(match.group("quarter")))
    compact = raw.replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        try:
            parsed = date(int(compact[:4]), int(compact[4:6]), int(compact[6:]))
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"报告期不是合法日期: {text!r}") from error
        if parsed != quarter_end(parsed.year, (parsed.month - 1) // 3 + 1):
            raise argparse.ArgumentTypeError(
                f"报告期必须是季末（03-31/06-30/09-30/12-31），实际 {parsed}: {text!r}"
            )
        return parsed
    raise argparse.ArgumentTypeError(f"报告期写法不认识（用 2025Q2 或 2025-06-30）: {text!r}")


def _resolve_periods(args: argparse.Namespace, today: date) -> list[date]:
    if args.quarter:
        return sorted(set(args.quarter))
    latest = latest_quarter_end(today)
    if args.since:
        start = args.since
        if start > latest:
            raise ValueError(f"--since {start} 晚于最近已结束季度 {latest}")
        found: list[date] = []
        count = ((latest.year - start.year) * 4) + ((latest.month - 1) // 3 - (start.month - 1) // 3)
        found = quarters_back(latest, max(1, count + 1))
        return [value for value in found if value >= start]
    if args.quarters and args.quarters > 0:
        return quarters_back(latest, args.quarters)
    return [latest]


def _existing_scopes(duck: DuckDBStore, periods: list[date]) -> set[tuple[str, str]]:
    """`--resume` 用：库里已有数据的 (report_date, holder_type) 组合。"""
    if not periods:
        return set()
    placeholders = ", ".join("?" for _ in periods)
    rows = duck.read_query(
        f"SELECT report_date, holder_type, COUNT(*) AS rows FROM {TABLE} "
        f"WHERE report_date IN ({placeholders}) GROUP BY report_date, holder_type",
        [value.isoformat() for value in periods],
    )
    return {
        (str(row["report_date"]), str(row["holder_type"]))
        for row in rows
        if int(row["rows"] or 0) > 0
    }


def _before_counts(duck: DuckDBStore, periods: list[date]) -> dict[str, object]:
    try:
        return holdings_evidence(duck, periods)
    except Exception as error:  # noqa: BLE001 - dry-run 不该因为读库失败就中止抓取
        logger.warning("读库失败（表可能尚未创建）：%s", error)
        return {"rows": 0, "stocks": 0, "by_scope": []}


def build_report(
    duck: DuckDBStore,
    *,
    periods: list[date],
    limit: int,
    stocks: list[str],
    resume: bool,
    dry_run: bool,
) -> dict[str, object]:
    """抓取 → 规范化 → 逐 (报告期 × 类型) 写库；返回可机读证据。"""
    before = _before_counts(duck, periods)
    existing = _existing_scopes(duck, periods) if resume else set()
    scopes: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    total_written = 0
    for period in periods:
        for type_code, holder_type in HOLDER_TYPES:
            key = (period.isoformat(), holder_type)
            if resume and key in existing:
                scopes.append({
                    "report_date": period.isoformat(), "holder_type": holder_type,
                    "skipped": "resume:库内已有数据",
                })
                continue
            try:
                frame = fetch_holder_period(type_code, period)
                if frame.empty:
                    scopes.append({
                        "report_date": period.isoformat(), "holder_type": holder_type,
                        "rows": 0, "skipped": "源端本期无此类持有人",
                    })
                    continue
                rows, stats = normalize(frame, report_date=period, holder_type=holder_type)
                if stocks:
                    keep = set(stocks)
                    rows = rows.loc[rows["stock_code"].isin(keep)].copy()
                    stats["stock_filter"] = sorted(keep)
                    stats["rows_after_stock_filter"] = int(len(rows))
                    if rows.empty:
                        scopes.append({
                            "report_date": period.isoformat(), "holder_type": holder_type,
                            "rows": 0, "skipped": "指定的股票本期该类持有人无持仓",
                        })
                        continue
                elif limit and limit > 0:
                    rows = select_top_stocks(rows, limit)
                    stats["limit"] = int(limit)
                    stats["rows_after_limit"] = int(len(rows))
                # 试点（--limit/--stock）只覆盖本次写入的股票，不动同期其它股票
                scope = "codes" if (limit and limit > 0) or stocks else "period"
                entry: dict[str, object] = {"stats": stats, "replace_scope": scope}
                if dry_run:
                    entry["planned_rows"] = int(len(rows))
                else:
                    entry["written"] = write_holdings(
                        duck, rows, report_date=period, holder_type=holder_type,
                        replace_scope=scope,
                    )
                    total_written += int(len(rows))
                    entry["after_scope"] = holdings_evidence(duck, [period])
                scopes.append(entry)
            except (RuntimeError, ValueError) as error:
                # 逐 (报告期 × 类型) 隔离（R6）：一类抓失败不连坐其它类
                logger.error("跳过 %s 的「%s」：%s", period, holder_type, error)
                failures.append({
                    "report_date": period.isoformat(), "holder_type": holder_type,
                    "error": str(error),
                })
    after = _before_counts(duck, periods) if not dry_run else before
    return {
        "table": TABLE,
        "periods": [value.isoformat() for value in periods],
        "limit": int(limit or 0),
        "stocks": stocks,
        "resume": bool(resume),
        "dry_run": bool(dry_run),
        "before": before,
        "after": after,
        "scopes": scopes,
        "failures": failures,
        "rows_written": total_written,
    }


def _print_summary(report: dict[str, object]) -> None:
    print("=" * 78)
    print(f"报告期                : {', '.join(str(v) for v in report['periods'])}")
    print(f"持有人类型            : {'/'.join(name for _, name in HOLDER_TYPES)}")
    mode = "DRY RUN（未写库）" if report["dry_run"] else "已写库"
    print(f"模式                  : {mode}")
    if report["limit"]:
        print(f"--limit               : 持仓市值前 {report['limit']} 只")
    if report["stocks"]:
        print(f"--stock               : {', '.join(report['stocks'])}")
    for entry in report["scopes"]:  # type: ignore[union-attr]
        if "skipped" in entry:
            print(f"  [跳过] {entry['report_date']} {entry['holder_type']}: {entry['skipped']}")
            continue
        stats = entry.get("stats") or {}
        if "stats" not in entry and "rows" in entry:
            print(f"  [空]   {entry['report_date']} {entry['holder_type']}: {entry['rows']} 行")
            continue
        print(
            f"  [OK]   {stats.get('report_date')} {stats.get('holder_type')}: "
            f"{entry.get('planned_rows', (entry.get('written') or {}).get('rows_written'))} 行 / "
            f"{stats.get('stocks')} 只 / 比例覆盖 {stats.get('ratio_coverage_pct')}% / "
            f"删旧 {(entry.get('written') or {}).get('rows_deleted', 0)} 行 / scope={entry['replace_scope']}"
        )
    for failure in report["failures"]:  # type: ignore[union-attr]
        print(f"  [FAIL] {failure['report_date']} {failure['holder_type']}: {failure['error']}")
    before, after = report["before"], report["after"]
    print(f"写前库内              : {before.get('rows')} 行 / {before.get('stocks')} 只")
    if not report["dry_run"]:
        print(f"写入行数              : {report['rows_written']}")
        print(f"写后库内              : {after.get('rows')} 行 / {after.get('stocks')} 只")
        print(f"报告期范围            : {after.get('min_date')} → {after.get('max_date')}")
    print("=" * 78)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="机构持仓 → institution_holdings（默认 dry-run）",
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument(
        "--quarter", type=_parse_period, action="append", default=None,
        help="报告期，可重复（如 --quarter 2025Q2 --quarter 2024Q4）",
    )
    ap.add_argument("--quarters", type=int, default=0, help="最近 N 个季度（含最新已披露）")
    ap.add_argument("--since", type=_parse_period, default=None, help="从该季末起抓到最新（全量回补用）")
    ap.add_argument("--limit", type=int, default=0, help="只写持仓市值最大的 N 只（0=全部）")
    ap.add_argument("--stock", default="", help="只写这些股票代码（逗号分隔，如 600519,000001）")
    ap.add_argument("--resume", action="store_true", help="跳过库内已有数据的 (报告期 × 类型)")
    ap.add_argument("--json-out", default="", help="把 JSON 证据另存到该路径")
    args = ap.parse_args()

    stocks = [code.strip() for code in args.stock.split(",") if code.strip()]
    bad = [code for code in stocks if not re.fullmatch(r"\d{6}", code)]
    if bad:
        ap.error(f"--stock 必须是 6 位代码，非法值={bad}")

    today = cn_today()
    try:
        periods = _resolve_periods(args, today)
    except (ValueError, argparse.ArgumentTypeError) as error:
        ap.error(str(error))

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    duck = DuckDBStore(paths=paths)

    try:
        if args.yes:
            # 单写者锁：拿不到立即跳过（exit 3），不排队死等（自动更新在跑时本脚本只是旁路观测者）
            with exclusive_update(paths.duckdb_path):
                # 幂等建表（schema v49）：表体由 schema.py 的 CREATE TABLE IF NOT EXISTS 建，
                # 写库前必须保证它存在（照 `scripts/fetch_fx_rates.py` 的既有范式）。
                init_duckdb_schema(duck)
                report = build_report(
                    duck, periods=periods, limit=args.limit, stocks=stocks,
                    resume=args.resume, dry_run=False,
                )
        else:
            report = build_report(
                duck, periods=periods, limit=args.limit, stocks=stocks,
                resume=args.resume, dry_run=True,
            )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，本次跳过（不排队）：%s", error)
        raise SystemExit(3) from error
    except (RuntimeError, ValueError) as error:
        logger.error("抓取/规范化失败，未写库：%s", error)
        raise SystemExit(4) from error

    _print_summary(report)
    if not args.yes:
        print("[DRY RUN] 未写库；加 --yes 执行。")

    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        logger.info("JSON 证据: %s", out)
    print(payload)

    if report["failures"] and not report["rows_written"] and args.yes:
        raise SystemExit(4)


if __name__ == "__main__":
    main()
