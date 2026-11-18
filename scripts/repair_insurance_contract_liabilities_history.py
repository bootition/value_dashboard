"""一次性清洗：保险股「保险合同负债」的历史残留（reports/139 §6 P0-6 尾巴）。

事故
----
`reports/131` §9.2 P0-6 的**当前期**已被写入路径的守卫
（`app/core/financial_guards.py`，接入 `init._upsert_financial_row` /
`update._write_sina_financial_table`）拦住，2025-12-31 保险股占
`contract_liabilities` 合计已经降到 0.0%。但**历史段仍在库里**：

    601319 中国人保 FY2011 / FY2012 / FY2013 三行
    `balance_sheet.contract_liabilities` = 491.6 / 503.1 / 416.4 亿元
    （合计 1,411.1 亿），占这 3 年**全市场** `contract_liabilities` 合计的 100%。

后果：2011–2013 年任何「合同负债 / 总资产」类的排名与时间序列都是脏的
（分母正常、分子被单只股票撑满）。`scripts/repair_insurance_contract_liability.py`
只处理 CAS25 之后（`report_date >= 2022-12-31`）的行，历史段不在它射程内。

修法（本脚本）
--------------
把这 5 只保险股（601318 中国平安 / 601319 中国人保 / 601336 新华保险 /
601601 中国太保 / 601628 中国人寿）**所有** `contract_liabilities` 非空且非 0 的行
置 NULL，并在 `indicator_null_reason` 写 `NOT_APPLICABLE`
（字段名 `balance.contract_liabilities`，与清扫器 / `missing_list` 的源表域命名一致）。

为什么是「不适用」而不是「源没提供」：保险股的「保险合同负债」对应的是专用科目
`financial_sector_items.insurance_contract_liability`，**不是**通用的合同负债
（`reports/131` §9.2 P0-6 的裁决）。落成 NULL 之后如果不写原因，这批单元格会
重新变成「没有」和「坏了」分不清——正是 `reports/119` §5 要消灭的形态。

为什么按**股票代码**而不是按 `sw_level2='保险Ⅱ'` 划范围：本脚本要清的是
「上游把一个专用科目错配到通用列」这件事的**存量**，范围由主控点名（5 只险企）；
`sw_level2` 只用于**自证打印**（见 `insurer_evidence`），不用来收窄范围——
否则行业标签一旦被修/被补，同一批历史行就再也清不掉了。

用法
----
    python scripts/repair_insurance_contract_liabilities_history.py                 # dry-run（默认）
    python scripts/repair_insurance_contract_liabilities_history.py --dry-run       # 显式 dry-run
    python scripts/repair_insurance_contract_liabilities_history.py --sql           # 只打印 SQL
    python scripts/repair_insurance_contract_liabilities_history.py --yes           # 备份 + 写锁 + 写库
    python scripts/repair_insurance_contract_liabilities_history.py --yes --codes 601319
    python scripts/repair_insurance_contract_liabilities_history.py --json-out out.json

写库路径
--------
1. 先只读地按**同一个谓词**数一遍（`before`），并给出聚合明细（按股票 / 按年），
   **不 dump 明细行**（`reports/138` §1：明细 dump 一次吐 14KB 被截断，关键信息淹没）；
2. `--yes` 时先 `COPY` 备份 `balance_sheet` + `indicator_null_reason` 到 `.planning/`；
3. 在 `exclusive_update` 单写者锁里 UPDATE（事务），随后用
   `null_reasons.upsert_reasons` 写原因行——**先改值、后写原因**：反过来的话，
   中途崩溃会留下「有值却挂着 NOT_APPLICABLE」的假结论（清扫器的铁律是
   「有值的单元格一行都不许进原因表」）。反过来（值已 NULL、原因没写上）只是
   信息缺失，重跑清扫器即可，不会制造假结论；
4. 改完按**同一个谓词**再数一遍（`after`），把 before / after / 明细 / 市场占比
   一起打成 JSON 证据（stdout；`--json-out` 另存文件）。

写库前会检查 `indicator_null_reason` 是否存在，**不存在就直接拒绝**（DDL 是发布动作，
维护脚本不擅自建表），避免"值清了、原因写不进去"的半截状态。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.quality.null_reasons import (  # noqa: E402
    NullReason,
    NullReasonRow,
    null_reason_table_exists,
    upsert_reasons,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)
from scripts._maintenance_safety import backup_tables  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

#: 本次事故点名的 5 只保险股（`reports/131` §9.2 / `reports/139` §6）。
AFFECTED_CODES: tuple[str, ...] = ("601318", "601319", "601336", "601601", "601628")
#: 被误写进通用列的专用科目 —— 清洗后在 `indicator_null_reason` 登记该字段。
TARGET_COLUMN = "contract_liabilities"
#: 原因表的字段名带源表域前缀（与清扫器 `balance.fixed_assets` 一致）。
TARGET_FIELD_NAME = f"balance.{TARGET_COLUMN}"
#: 原因码：保险股的「保险合同负债」本来就不是通用合同负债 ⇒ 不适用，不是缺失。
NULL_REASON = NullReason.NOT_APPLICABLE
#: 原因行 detail 的前缀（后接该股该期的原值合计，便于人工复核）
REASON_DETAIL = (
    "保险股的「保险合同负债」是金融专用科目，不是通用合同负债"
    "（reports/131 §9.2 P0-6 / reports/139 §6）；清洗前原值合计 {amount_yi:.4f} 亿元"
)
#: 备份 tag（`scripts/_maintenance_safety.py::backup_tables` 用它拼目录名）
BACKUP_TAG = "p06-contract-liab-history"


def normalize_codes(codes: Sequence[str] | None) -> list[str]:
    """规范化股票代码；非法输入直接报错（**绝不**把未校验字符串拼进 SQL）。"""
    if not codes:
        return sorted(AFFECTED_CODES)
    normalized: list[str] = []
    for raw in codes:
        code = str(raw).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            raise ValueError(f"非法股票代码: {raw!r}")
        normalized.append(code)
    return sorted(set(normalized))


def scope_predicate(codes: Sequence[str], *, alias: str = "b") -> str:
    """本脚本的**唯一**判据：点名股票 + `contract_liabilities` 非空且非 0。

    `before` / `after` 两次计数、明细、UPDATE 全部复用这一份字符串——否则
    「改前数一遍、改后再数一遍」就变成了两套口径，数字对不上也看不出原因。
    """
    in_list = ", ".join(f"'{code}'" for code in codes)
    return (
        f"{alias}.stock_code IN ({in_list})"
        f" AND {alias}.{TARGET_COLUMN} IS NOT NULL"
        f" AND {alias}.{TARGET_COLUMN} <> 0"
    )


def count_sql(codes: Sequence[str]) -> str:
    return f"SELECT COUNT(*) AS n FROM balance_sheet b WHERE {scope_predicate(codes)}"


def detail_sql(codes: Sequence[str]) -> str:
    """聚合明细：按 股票 × 年 分组（不 dump 行）。"""
    return f"""
SELECT b.stock_code                                        AS stock_code,
       any_value(m.name)                                   AS name,
       CAST(EXTRACT(year FROM b.report_date) AS INTEGER)   AS report_year,
       COUNT(*)                                            AS rows,
       SUM(b.{TARGET_COLUMN}) / 1e8                        AS amount_yi,
       COUNT(DISTINCT b.report_date)                       AS periods,
       MIN(b.report_date)                                  AS first_report_date,
       MAX(b.report_date)                                  AS last_report_date
FROM balance_sheet b
LEFT JOIN stock_meta m ON m.stock_code = b.stock_code
WHERE {scope_predicate(codes)}
GROUP BY b.stock_code, report_year
ORDER BY b.stock_code, report_year
"""


def market_share_sql(codes: Sequence[str]) -> str:
    """受影响报告期的「命中合计 / 全市场合计」—— P0-6 的核心证据。

    reports/139 §6 实测：这 3 年全市场 `contract_liabilities` 合计 **100%** 来自
    601319，所以清洗前后的占比应当从 1.000 掉到 0.000。
    """
    return f"""
WITH affected AS (
    SELECT b.report_date AS report_date, SUM(b.{TARGET_COLUMN}) AS amount
    FROM balance_sheet b
    WHERE {scope_predicate(codes)}
    GROUP BY b.report_date
), market AS (
    SELECT report_date, SUM({TARGET_COLUMN}) AS amount
    FROM balance_sheet
    WHERE {TARGET_COLUMN} IS NOT NULL AND {TARGET_COLUMN} <> 0
    GROUP BY report_date
)
SELECT CAST(EXTRACT(year FROM a.report_date) AS INTEGER) AS report_year,
       COUNT(*)                                          AS periods,
       SUM(a.amount) / 1e8                                AS affected_yi,
       SUM(m.amount) / 1e8                                AS market_yi
FROM affected a
JOIN market m USING (report_date)
GROUP BY report_year
ORDER BY report_year
"""


def reason_targets_sql(codes: Sequence[str]) -> str:
    """要写 `indicator_null_reason` 的单元格：(股票, 报告期)，附原值合计。

    同一个 (股票, 报告期) 可能有多条 `report_type` 行，原因表主键是
    (stock_code, report_date, field_name)，所以这里先聚合再写。
    """
    return f"""
SELECT b.stock_code                    AS stock_code,
       b.report_date                   AS report_date,
       SUM(b.{TARGET_COLUMN}) / 1e8    AS amount_yi
FROM balance_sheet b
WHERE {scope_predicate(codes)}
GROUP BY b.stock_code, b.report_date
ORDER BY b.stock_code, b.report_date
"""


def update_sql(codes: Sequence[str]) -> str:
    """把命中的 `contract_liabilities` 置 NULL（**不带**别名，避免 UPDATE+FROM 写法漂移）。"""
    in_list = ", ".join(f"'{code}'" for code in codes)
    return (
        f"UPDATE balance_sheet SET {TARGET_COLUMN} = NULL\n"
        f" WHERE stock_code IN ({in_list})\n"
        f"   AND {TARGET_COLUMN} IS NOT NULL\n"
        f"   AND {TARGET_COLUMN} <> 0"
    )


def insurer_evidence(duck: DuckDBStore, codes: Sequence[str]) -> list[dict]:
    """自证：点名的每只股票在 `stock_meta` 里到底是不是保险股。

    只**打印**、不收窄范围（见模块 docstring「为什么按股票代码」）。
    """
    in_list = ", ".join(f"'{code}'" for code in codes)
    return duck.read_query(
        "SELECT stock_code, name, sw_level1, sw_level2, csrc_l1, is_listed\n"
        "FROM stock_meta\n"
        f"WHERE stock_code IN ({in_list})\n"
        "ORDER BY stock_code"
    )


@contextmanager
def _no_lock() -> Iterator[None]:
    """不额外加锁（测试用；正式路径必须传 `lock_path`）。"""
    yield


def select_detail(duck: DuckDBStore, codes: Sequence[str]) -> list[dict]:
    return duck.read_query(detail_sql(codes))


def select_reason_targets(duck: DuckDBStore, codes: Sequence[str]) -> list[dict]:
    return duck.read_query(reason_targets_sql(codes))


def build_plan(duck: DuckDBStore, codes: Sequence[str]) -> dict:
    """只读地数一遍并汇总（dry-run 与写库共用同一份口径）。"""
    before = int(duck.read_query(count_sql(codes))[0]["n"])
    detail = select_detail(duck, codes)
    reasons = select_reason_targets(duck, codes)
    return {
        "codes": list(codes),
        "predicate": scope_predicate(codes),
        "before_rows": before,
        "before_amount_yi": round(sum(float(row["amount_yi"]) for row in detail), 4),
        "periods": sorted({str(row["report_date"])[:10] for row in reasons}),
        "planned_reason_rows": len(reasons),
        "by_code_year": [
            {
                "stock_code": row["stock_code"],
                "name": row["name"],
                "report_year": row["report_year"],
                "rows": int(row["rows"]),
                "amount_yi": round(float(row["amount_yi"]), 4),
                "periods": int(row["periods"]),
                "first_report_date": str(row["first_report_date"])[:10],
                "last_report_date": str(row["last_report_date"])[:10],
            }
            for row in detail
        ],
        "market_share_before": [
            {
                "report_year": int(row["report_year"]),
                "periods": int(row["periods"]),
                "affected_yi": round(float(row["affected_yi"]), 4),
                "market_yi": round(float(row["market_yi"]), 4),
                "share": round(float(row["affected_yi"]) / float(row["market_yi"]), 6)
                if float(row["market_yi"])
                else None,
            }
            for row in duck.read_query(market_share_sql(codes))
        ],
    }


def repair(
    duck: DuckDBStore,
    *,
    yes: bool = False,
    codes: Sequence[str] | None = None,
    backup: bool = True,
    lock_path: Path | None = None,
) -> dict:
    """执行（或演练）清洗，返回可核对的 JSON 证据。

    Args:
        duck: 已打开的 DuckDBStore（测试用临时库；CLI 用正式库）。
        yes: False = dry-run，**只读不写**。
        codes: 只处理这些股票（None/空 = 点名的 5 只险企）。
        backup: 写库前是否备份两张受影响表（CLI 恒为 True）。
        lock_path: 单写者锁的库路径；None = 不额外加锁（仅限测试/演练）。
    """
    wanted = normalize_codes(codes)
    plan = build_plan(duck, wanted)
    report: dict = {
        "task": "reports/139 §6 P0-6 历史残留：保险股 contract_liabilities 置 NULL + NOT_APPLICABLE",
        "script": Path(__file__).name,
        "dry_run": not yes,
        "mode": "dry_run" if not yes else "write",
        "executed_at": datetime.now(UTC).isoformat(),
        "target_field_name": TARGET_FIELD_NAME,
        "reason_code": NULL_REASON.value,
        "insurer_evidence": insurer_evidence(duck, wanted),
        "plan": plan,
        "written_rows": 0,
        "reason_rows_written": 0,
        "backup_dir": None,
        "after_rows": plan["before_rows"] if not yes else None,
    }
    if not plan["before_rows"]:
        logger.info("谓词未命中任何行（可能已清洗过），退出。")
        return report
    if not yes:
        logger.info(
            "[DRY RUN] 将把 %d 行 contract_liabilities 置 NULL（%d 只股票 / %d 个报告期，"
            "合计 %.4f 亿元），并写 %d 行 %s",
            plan["before_rows"], len(wanted), len(plan["periods"]),
            plan["before_amount_yi"], plan["planned_reason_rows"], NULL_REASON.value,
        )
        return report

    # 表不存在就拒绝：值清了却写不进原因 = 半截状态（见模块 docstring）。
    if not null_reason_table_exists(duck):
        raise RuntimeError(
            "indicator_null_reason 表不存在：请先跑 schema 迁移（发布动作），"
            "本脚本不擅自 DDL。"
        )

    if backup:
        report["backup_dir"] = str(
            backup_tables(duck, ["balance_sheet", "indicator_null_reason"], BACKUP_TAG)
        )
    lock = exclusive_update(lock_path) if lock_path is not None else _no_lock()
    with lock:
        # 原因行必须在**改值之前**取：scope 谓词要求 `contract_liabilities` 非空，
        # 置 NULL 之后再查就一行都取不到了（dry-run 里 `planned_reason_rows` 用的
        # 也是这一份查询，两处口径必须相同）。
        reasons = select_reason_targets(duck, wanted)
        with duck.transaction() as connection:
            updated = connection.execute(update_sql(wanted)).fetchone()[0]
        report["reason_rows_written"] = upsert_reasons(
            duck,
            [
                NullReasonRow(
                    stock_code=str(row["stock_code"]),
                    report_date=row["report_date"],
                    field_name=TARGET_FIELD_NAME,
                    reason_code=NULL_REASON,
                    detail=REASON_DETAIL.format(amount_yi=float(row["amount_yi"])),
                )
                for row in reasons
            ],
        )
        report["written_rows"] = int(updated)
        report["after_rows"] = int(duck.read_query(count_sql(wanted))[0]["n"])
        report["market_share_after"] = [
            {
                "report_year": int(row["report_year"]),
                "periods": int(row["periods"]),
                "affected_yi": round(float(row["affected_yi"]), 4),
                "market_yi": round(float(row["market_yi"]), 4),
                "share": round(float(row["affected_yi"]) / float(row["market_yi"]), 6)
                if float(row["market_yi"])
                else None,
            }
            for row in duck.read_query(market_share_sql(wanted))
        ]
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def _print_sql(codes: Sequence[str]) -> None:
    print("-- 判据（before / after 计数、明细、UPDATE 共用）--")
    print(scope_predicate(codes) + ";")
    print("\n-- 改前/改后计数 --")
    print(count_sql(codes) + ";")
    print("\n-- 聚合明细（按 股票 × 年）--")
    print(detail_sql(codes).strip() + ";")
    print("\n-- 市场占比 --")
    print(market_share_sql(codes).strip() + ";")
    print("\n-- 置 NULL --")
    print(update_sql(codes) + ";")
    print(
        f"\n-- 写空值原因（字段 {TARGET_FIELD_NAME}，原因 {NULL_REASON.value}；"
        "由 null_reasons.upsert_reasons 落库）--"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="清洗保险股「保险合同负债」历史残留（reports/139 §6 P0-6）",
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为，写库需 --yes）")
    ap.add_argument("--sql", action="store_true", help="只打印 SQL 后退出")
    ap.add_argument(
        "--codes", default="",
        help="只处理这些股票代码（逗号分隔；默认点名的 5 只险企）",
    )
    ap.add_argument("--json-out", default="", help="把 JSON 证据另存到该路径")
    args = ap.parse_args()

    codes = normalize_codes(
        [item.strip() for item in args.codes.split(",") if item.strip()] or None
    )
    if args.sql:
        _print_sql(codes)
        return

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    duck = DuckDBStore(paths=paths)

    if args.dry_run:
        logger.info("[DRY RUN] 显式 --dry-run：只读，不写库")

    try:
        report = repair(duck, yes=args.yes, codes=codes, lock_path=paths.duckdb_path)
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error
    except RuntimeError as error:
        logger.error("%s", error)
        raise SystemExit(4) from error

    if report["backup_dir"]:
        logger.info("[BACKUP] %s", report["backup_dir"])
    logger.info(
        "%s：改前 %d 行 / 改后 %s 行；写原因 %d 行",
        "DRY RUN（未写库）" if report["dry_run"] else "已写库",
        report["plan"]["before_rows"],
        report["after_rows"],
        report["reason_rows_written"],
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        logger.info("JSON 证据: %s", out)
    print(payload)


if __name__ == "__main__":
    main()
