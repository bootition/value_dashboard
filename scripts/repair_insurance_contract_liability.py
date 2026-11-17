"""一次性修复：保险股「保险合同负债」被写进 `balance_sheet.contract_liabilities`
（2026-11-16，reports/131 §9.2 P0-6）。

事故
----
601318 中国平安 / 601319 中国人保 / 601336 新华保险 / 601601 中国太保 /
601628 中国人寿 五只保险股，FY2022–FY2025 共 19 个**年报**行
（含季报共 74 行）在 `balance_sheet.contract_liabilities` 里有值，
却占**全市场合同负债合计的 42%–68%**（2026-11-16 复核已达 50%–78%）。

根因：上游金融专用科目「保险合同负债」被按名称/序号匹配到了通用科目
`contract_liabilities`。出现年份恰是新保险合同准则 CAS25 的实施年
（A+H 险企 FY2022、其余 FY2023）—— 只有实施新准则的公司才有这一行，可反证。
旁证（2026-11-16 实测）：东财 F10 保险模板（companyType=2）**整张资产负债表
没有任何字段**等于这些值，而同期 `TOTAL_LIABILITIES` 与库内逐值相等
（601628 FY2024：6,248,298,000,000）—— 说明报表本身是同一份，
只是「保险合同负债」这个 CAS25 新科目在东财模板里没有位置。

判据（自证，四层全部满足才动）
------------------------------
1. `stock_meta.sw_level2 = '保险Ⅱ'`         —— 行业自证，非保险股一行不碰；
2. `contract_liabilities IS NOT NULL`       —— 只处理有值的行；
3. `report_date >= '2022-12-31'`            —— CAS25 起算年，早于该年的是旧准则口径；
4. `contract_liabilities / total_liabilities >= 0.25` —— 量级自证：
   保险合同负债是险企最大的负债科目（实测五只 0.43–0.95），
   而真实「合同负债/预收保费」不可能占到负债的四分之一
   （601319 2011–2013 年的真实合同负债比值只有 0.06–0.09，因此被此条排除）。

判据 4 单独用时对非保险股不成立（有 14,359 行 ≥0.25），所以必须与判据 1 合用；
两条合起来在 2026-11-16 正式库上正好命中 74 行、且**零**非保险行。

⚠️ 2026-11-17 更正（reports/135 第 5 项）：本脚本当时打印的
`scope_breach_rows=0` **不能**当作"零误伤"的证据 —— 那条查询把
`sw_level2 <> '保险Ⅱ'` 与 `sw_level2 = '保险Ⅱ'` 同时写进 WHERE，恒为假。
现已改成真正能检出"非保险股被误改"的反事实探针（去掉矛盾行后正式库 8,546 行）。
真正证明零误伤的是逐行对比（74 行全部来自 5 只险企），不是那条 SQL。

写入路径的根治
--------------
本脚本只处理**存量**。写入路径的守卫（下一期不再复发）在
`app/core/financial_guards.py`，已接入 `init._upsert_financial_row` 与
`update._write_sina_financial_table`；被清掉的金额由
`scripts/backfill_insurance_contract_liability.py` 回填到
`financial_sector_items.insurance_contract_liability`。

用法
----
    python scripts/repair_insurance_contract_liability.py            # dry-run（默认）
    python scripts/repair_insurance_contract_liability.py --sql      # 只打印 SQL
    python scripts/repair_insurance_contract_liability.py --yes      # 备份 + 写锁 + 写库
    python scripts/repair_insurance_contract_liability.py --yes --codes 601318,601628

写库前自动 `COPY balance_sheet TO …parquet` 备份到 `.planning/`，并持有单写者锁；
整个过程单事务，失败整体回滚。

清理 SQL（与脚本内 `SELECT_REPAIR_SQL` 逐字一致）
-------------------------------------------------
    SELECT b.stock_code, b.report_date, b.contract_liabilities, b.total_liabilities
    FROM balance_sheet b
    JOIN stock_meta m ON m.stock_code = b.stock_code
    WHERE m.sw_level2 = '保险Ⅱ'
      AND b.contract_liabilities IS NOT NULL
      AND b.report_date >= DATE '2022-12-31'
      AND b.total_liabilities IS NOT NULL AND b.total_liabilities > 0
      AND b.contract_liabilities / b.total_liabilities >= 0.25;

    UPDATE balance_sheet AS b
       SET contract_liabilities = NULL
      FROM stock_meta m
     WHERE m.stock_code = b.stock_code
       AND m.sw_level2 = '保险Ⅱ'
       AND b.contract_liabilities IS NOT NULL
       AND b.report_date >= DATE '2022-12-31'
       AND b.total_liabilities IS NOT NULL AND b.total_liabilities > 0
       AND b.contract_liabilities / b.total_liabilities >= 0.25;
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)
from app.core.supplement import INSURANCE_SW_LEVEL2  # noqa: E402
from scripts._maintenance_safety import backup_tables, confirm_destructive  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

#: CAS25 新保险合同准则起算报告期（A+H 险企 2022 年报起，其余 2023 年报起）
CAS25_START = "2022-12-31"
#: 保险合同负债 / 负债合计 的最小量级（见模块 docstring 判据 4）
MIN_LIABILITY_SHARE = 0.25
#: 本次事故涉及的 5 只保险股（仅用于 dry-run 时的对照打印，**不是**过滤条件）
AFFECTED_CODES: tuple[str, ...] = ("601318", "601319", "601336", "601601", "601628")

_BASE_PREDICATE = f"""
    m.sw_level2 = '{INSURANCE_SW_LEVEL2}'
    AND b.contract_liabilities IS NOT NULL
    AND b.report_date >= DATE '{CAS25_START}'
    AND b.total_liabilities IS NOT NULL
    AND b.total_liabilities > 0
    AND b.contract_liabilities / b.total_liabilities >= {MIN_LIABILITY_SHARE}
"""

SELECT_REPAIR_SQL = f"""
SELECT b.stock_code               AS stock_code,
       m.name                     AS name,
       b.report_date              AS report_date,
       b.report_type              AS report_type,
       b.contract_liabilities     AS contract_liabilities,
       b.total_liabilities        AS total_liabilities,
       b.contract_liabilities / b.total_liabilities AS liability_share
FROM balance_sheet b
JOIN stock_meta m ON m.stock_code = b.stock_code
WHERE {_BASE_PREDICATE.strip()}
ORDER BY b.stock_code, b.report_date
"""

UPDATE_REPAIR_SQL = f"""
UPDATE balance_sheet AS b
   SET contract_liabilities = NULL
  FROM stock_meta m
 WHERE m.stock_code = b.stock_code
   AND {_BASE_PREDICATE.strip()}
"""


def normalize_codes(codes: list[str] | None) -> list[str]:
    """规范化股票代码；非法输入直接报错（**绝不**把未校验字符串拼进 SQL）。"""
    if not codes:
        return []
    normalized: list[str] = []
    for raw in codes:
        code = str(raw).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            raise ValueError(f"非法股票代码: {raw!r}")
        normalized.append(code)
    return sorted(set(normalized))


def build_update_sql(codes: list[str] | None = None) -> str:
    """生成清空 SQL；`codes` 非空时把 UPDATE 也收窄到这些股票。

    ⚠️ 只收窄 SELECT 而不收窄 UPDATE 会把判据命中的**所有**股票都清掉
    （2026-11-16 自测抓到的第一版缺陷：`--codes 601628` 却清了两只）。
    """
    normalized = normalize_codes(codes)
    if not normalized:
        return UPDATE_REPAIR_SQL
    in_list = ", ".join(f"'{code}'" for code in normalized)
    return UPDATE_REPAIR_SQL.rstrip().rstrip(";") + f"\n   AND b.stock_code IN ({in_list})"

#: 行业门槛**承重**证据（反事实探针）：把 UPDATE 的行业判据换成它的补集后，
#: 会命中多少行。这不是"误伤行数"，而是"如果没有行业门槛就会误伤多少行" ——
#: 数字必须 > 0，否则说明行业判据根本不起作用。
#:
#: ⚠️ 2026-11-17 修正（reports/135）：本查询原先把
#: `COALESCE(m.sw_level2,'') <> '保险Ⅱ'` 和 `m.sw_level2 = '保险Ⅱ'` **同时**写在
#: WHERE 里（自相矛盾），恒返回 0；并且脚本把它当成了"零误伤"的证据打印。
#: 去掉矛盾行后正式库实测 8,546 行（非保险股中 `合同负债/负债合计 >= 0.25` 的行数），
#: 证明量级判据单独用时完全不可用，行业判据是唯一的防误伤机制。
SELECT_SCOPE_BREACH_SQL = f"""
SELECT COUNT(*) AS n
FROM balance_sheet b
JOIN stock_meta m ON m.stock_code = b.stock_code
WHERE COALESCE(m.sw_level2, '') <> '{INSURANCE_SW_LEVEL2}'
  AND b.contract_liabilities IS NOT NULL
  AND b.report_date >= DATE '{CAS25_START}'
  AND b.total_liabilities IS NOT NULL
  AND b.total_liabilities > 0
  AND b.contract_liabilities / b.total_liabilities >= {MIN_LIABILITY_SHARE}
"""


@contextmanager
def _no_lock() -> Iterator[None]:
    """不额外加锁（测试用；正式路径必须传 `lock_path`）。"""
    yield


def select_repair_rows(
    duck: DuckDBStore, *, codes: list[str] | None = None,
) -> list[dict]:
    """按判据选出应清空的 `balance_sheet` 行（只读）。"""
    wanted = normalize_codes(codes)
    rows = duck.read_query(SELECT_REPAIR_SQL)
    if not wanted:
        return rows
    return [row for row in rows if str(row["stock_code"]) in set(wanted)]


def summarize(rows: list[dict]) -> dict:
    """把待处理行汇总成便于核对/上报的统计。"""
    by_code: dict[str, int] = {}
    annual = 0
    for row in rows:
        code = str(row["stock_code"])
        by_code[code] = by_code.get(code, 0) + 1
        if str(row.get("report_date"))[5:10] == "12-31":
            annual += 1
    total = sum(float(row["contract_liabilities"]) for row in rows)
    return {
        "rows": len(rows),
        "annual_rows": annual,
        "codes": sorted(by_code),
        "by_code": by_code,
        "sum_contract_liabilities": total,
        "first_report_date": str(rows[0]["report_date"])[:10] if rows else None,
        "last_report_date": str(rows[-1]["report_date"])[:10] if rows else None,
    }


def repair(
    duck: DuckDBStore,
    *,
    yes: bool = False,
    codes: list[str] | None = None,
    backup: bool = True,
    lock_path: Path | None = None,
) -> dict:
    """执行（或演练）修复，返回可核对的结果字典。

    Args:
        duck: 已打开的 DuckDBStore（测试用临时库；CLI 用正式库）。
        yes: False = dry-run，只读不写。
        codes: 只处理这些股票（None = 判据命中的全部）。
        backup: 写库前是否备份 `balance_sheet`（CLI 恒为 True）。
        lock_path: 单写者锁的库路径；None = 不额外加锁（仅限测试/演练）。
    """
    rows = select_repair_rows(duck, codes=codes)
    summary = summarize(rows)
    report: dict = {
        "dry_run": not yes,
        "mode": "dry_run" if not yes else "write",
        "selected": summary,
        "scope_breach_rows": None,
        "written": 0,
        "backup_dir": None,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    if not rows:
        return report
    if not yes:
        return report

    if backup:
        report["backup_dir"] = str(backup_tables(duck, ["balance_sheet"], "p06-contract-liab"))
    try:
        lock = exclusive_update(lock_path) if lock_path is not None else _no_lock()
        with lock, duck.write_connection() as conn:
            conn.execute(build_update_sql(codes))
    except UpdateLockError:
        raise
    # `written` 必须只统计**本判据选中**的行（用全库非空数之差会被别的股票带偏）。
    remaining = select_repair_rows(duck, codes=codes)
    report["written"] = len(rows) - len(remaining)
    report["remaining_after"] = len(remaining)
    report["scope_breach_rows"] = int(duck.read_query(SELECT_SCOPE_BREACH_SQL)[0]["n"])
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        description="修复保险股「保险合同负债」误写入 balance_sheet.contract_liabilities",
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--sql", action="store_true", help="只打印清理 SQL 后退出")
    ap.add_argument(
        "--codes", default="",
        help="只处理这些股票代码（逗号分隔；默认按判据全量）",
    )
    args = ap.parse_args()

    if args.sql:
        print("-- 选中行（自证判据）--")
        print(SELECT_REPAIR_SQL.strip().rstrip(";") + ";")
        print()
        print("-- 清空 --")
        print(UPDATE_REPAIR_SQL.strip().rstrip(";") + ";")
        return

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    codes = [item.strip() for item in args.codes.split(",") if item.strip()] or None
    rows = select_repair_rows(duck, codes=codes)
    summary = summarize(rows)
    logger.info(
        "判据命中 %d 行（年报 %d 行；股票 %d 只；合计 %.4f 亿元；%s ~ %s）",
        summary["rows"], summary["annual_rows"], len(summary["codes"]),
        summary["sum_contract_liabilities"] / 1e8,
        summary["first_report_date"], summary["last_report_date"],
    )
    for row in rows:
        logger.info(
            "  %s %s %s 合同负债=%.4f亿 负债合计=%.4f亿 占比=%.3f",
            row["stock_code"], str(row["report_date"])[:10], row.get("report_type") or "-",
            float(row["contract_liabilities"]) / 1e8,
            float(row["total_liabilities"]) / 1e8,
            float(row["liability_share"]),
        )
    if not rows:
        logger.info("判据未命中任何行（可能已修复过），退出。")
        return
    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行（会先备份 balance_sheet 并加写锁）")
        return
    if not confirm_destructive(args.yes):
        return

    try:
        report = repair(duck, yes=True, codes=codes, lock_path=paths.duckdb_path)
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    logger.info("[BACKUP] %s", report["backup_dir"])
    logger.info("已清空 %d 行；复核剩余 %d 行", report["written"], report.get("remaining_after", 0))
    logger.info(
        "行业门槛承重证据：去掉 sw_level2='%s' 后会被同一量级判据命中的非保险股行数 = %s"
        "（必须 > 0；= 0 说明行业判据没在起作用的范围内，见 reports/135 第 5 项）",
        INSURANCE_SW_LEVEL2, report["scope_breach_rows"],
    )
    logger.info(
        "下一步：重跑 `vd data update` 让 indicator_ext 的合同负债相关派生列（若有）随之为 NULL；"
        "保险合同负债的正式归宿需主控给 financial_sector_items 加列 "
        "insurance_contract_liability（reports/131 §9.2 P0-6）。"
    )


if __name__ == "__main__":
    main()
