"""卖方一致预期（EPS 预测）每日快照 → `analyst_forecast_snapshot`（schema v48）。

## 为什么必须「今天就开始打点」

`akshare.stock_profit_forecast_em(symbol="")` 只返回**当期横截面**（实测 2,802 行 / 13 列 / 1.71s），
接口没有历史参数 —— 昨天的一致预期在免费源里不存在（探源实测见
`.planning/2026-11-18-indicator-feasibility-audit/out/source_probe.md` §1.B6）。
所以这张表的时间序列只能**从今天起每天一行**攒出来；晚一天开始就永久少一天，回补不可能。

## 口径（units）

- `eps_forecast`：**元/股**（茅台 2025E = 65.851755）；
- `analyst_count`：研报数（家/篇，整数）；
- `snapshot_date`：**北京时间**当天（`app.core.financial_period.cn_today()`，AGENTS R5），
  不是 UTC 日期、也不是预测年度；
- `fiscal_year`：从接口列名 `「2025预测每股收益」` 解析成 `2025E`，**不硬编码**年度区间
  （明年接口会滚成 2026E–2029E，硬编码会静默丢一整年）；
- EPS 为空**不写 0**（D49 同款）：空 = 该年度无卖方覆盖，0 = 预期零增长，两者不能混；
- 源端**自带重复行**（2026-11-18 实测：2,802 行里 442 只股票各出现 2 次、载荷逐字相同，
  akshare 内部 6 页拼接重叠），所以「唯一只数」是 **2,360**，不是 2,802 —— 覆盖率
  42.3%（2,360/5,575），探源报告 §1.B6 的 50.3% 是**行数**口径，偏乐观。

## 写库路径

1. 默认 **dry-run**：真抓一次（1.7s）后打印计划与真实读数，不写库；
2. `--yes` 才写库：`require_formal_maintenance_paths()` → 单写者锁 `exclusive_update`
   （拿不到锁**立即退出码 3 跳过**，不排队死等）→ 一个事务内「按范围删旧快照 + 单条
   `INSERT ... SELECT ... ON CONFLICT`」（pandas `register`，禁用 executemany，D19/D36）；
3. 同一天重跑 = 等价覆盖（`replace_scope=snapshot_date`）；`--limit N` 试点时只覆盖这 N 只
   （`replace_scope=codes`），不会把当天其它股票的快照连带删掉。

## 用法

    uv run --locked python scripts/fetch_analyst_forecast_snapshot.py                  # dry-run
    uv run --locked python scripts/fetch_analyst_forecast_snapshot.py --limit 50       # dry-run 试点
    uv run --locked python scripts/fetch_analyst_forecast_snapshot.py --yes            # 写库（今天）
    uv run --locked python scripts/fetch_analyst_forecast_snapshot.py --yes --json-out evidence.json
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

from app.core.analyst_forecast import (  # noqa: E402
    build_snapshot,
)
from app.core.financial_period import cn_today  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"日期必须是 YYYY-MM-DD: {text!r}") from error


def _print_plan(report: dict) -> None:
    plan = report["plan"]
    before = report["before"]
    print("=" * 78)
    print(f"快照日(snapshot_date) : {report['snapshot_date']}  confidence={report['confidence']}")
    print(f"模式                 : {report['mode']}  删除范围={report['replace_scope']}")
    print("源                   : akshare:stock_profit_forecast_em（当期横截面，无历史可回补）")
    print(f"接口原始              : {plan['rows_in_response']} 行 / {len(plan['source_columns'])} 列")
    print(f"年度列(从列名解析)     : {', '.join(plan['fiscal_years'])}")
    print(f"计划写入行数           : {plan['rows_long']}（{plan['stocks']} 只股票）")
    print("fiscal_year 分布      : " + ", ".join(
        f"{year}={count}" for year, count in plan["rows_by_fiscal_year"].items()
    ))
    print(f"源端重复行            : 丢 {plan['duplicate_rows_dropped']} 行"
          f"（同代码同载荷；载荷冲突 {plan['duplicate_conflicts']} 只）")
    print(f"逐股隔离              : 非法代码 {plan['dropped_invalid_code']} 行；"
          f"EPS 为空跳过 {plan['dropped_null_eps']} 行 "
          f"{plan['dropped_null_eps_by_fiscal_year']}")
    print(f"该日库内现有           : {before['rows']} 行 / {before['stocks']} 只")
    print(f"batch_id             : {plan['batch_id']}")
    print(f"raw_hash             : {plan['raw_hash']}")
    print("=" * 78)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="一致预期每日快照 → analyst_forecast_snapshot（默认 dry-run）",
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument(
        "--date", type=_parse_date, default=None,
        help="快照日（默认北京时间今天）；非今天需同时给 --allow-backfill-date",
    )
    ap.add_argument(
        "--allow-backfill-date", action="store_true",
        help="允许把当期横截面写成历史日期（confidence 降级为 approximate；默认拒绝）",
    )
    ap.add_argument("--limit", type=int, default=0, help="只写按代码升序的前 N 只股票（0=全部）")
    ap.add_argument("--json-out", default="", help="把 JSON 证据另存到该路径")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    duck = DuckDBStore(paths=paths)

    snapshot_date = args.date or cn_today()
    try:
        if args.yes:
            # 单写者锁：拿不到立即跳过（不排队死等）；自动更新在跑时本脚本只是旁路观测者。
            with exclusive_update(paths.duckdb_path):
                report = build_snapshot(
                    duck,
                    snapshot_date=snapshot_date,
                    limit=args.limit,
                    dry_run=False,
                    allow_backfill_date=args.allow_backfill_date,
                )
        else:
            report = build_snapshot(
                duck,
                snapshot_date=snapshot_date,
                limit=args.limit,
                dry_run=True,
                allow_backfill_date=args.allow_backfill_date,
            )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，本次跳过（不排队）：%s", error)
        raise SystemExit(3) from error
    except ValueError as error:
        logger.error("%s", error)
        raise SystemExit(2) from error
    except RuntimeError as error:
        logger.error("抓取/规范化失败，未写库：%s", error)
        raise SystemExit(4) from error

    _print_plan(report)
    if args.yes:
        written = report["written"] or {}
        after = report["after"] or {}
        print(f"写入行数              : {written.get('rows_written')}（删除旧行 {written.get('rows_deleted')}）")
        print(f"覆盖只数              : {after.get('stocks')}")
        print("fiscal_year 分布(回读) : " + ", ".join(
            f"{row['fiscal_year']}={row['rows']}({row['stocks']}只)"
            for row in after.get("by_fiscal_year", [])
        ))
        # 写后自证：回读行数必须与计划一致，否则这就是半截状态，如实报错而不是"看起来成功"。
        if int(after.get("rows") or 0) < int(report["plan"]["rows_long"]):
            logger.error(
                "回读行数 %s < 计划 %s：写入不完整（半截状态），请重跑本脚本",
                after.get("rows"), report["plan"]["rows_long"],
            )
            raise SystemExit(5)
    else:
        print("[DRY RUN] 未写库；加 --yes 执行。")

    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        logger.info("JSON 证据: %s", out)
    print(payload)


if __name__ == "__main__":
    main()
