"""把被清掉的 74 行「保险合同负债」原值回填到专用列（P0-6，reports/135 第 4 项）。

背景
----
2026-11-16 的一次性脚本（`scripts/repair_insurance_contract_liability.py`）把
5 只险企 74 行 `balance_sheet.contract_liabilities` 置成了 NULL —— 污染止住了，
但金额没有落到正确的列（`financial_sector_items.insurance_contract_liability`
当时 5,640 行 / 0 非空），对外表现从"污染全市场排名"变成"整块信息缺失"。
原值并没有丢：它还在 `balance_sheet.raw_data` 的 JSON 里（`raw_data` 记录的是
适配器归一化后的整行，不是上游原始标题）。

raw_data 里的键名（2026-11-17 正式库只读实测，**不是猜的**）
------------------------------------------------------------
对 5 只险企 390 行 `balance_sheet.raw_data` 逐行解析 JSON、枚举全部键：

* 含「保险」或 `INSURANCE` 字样的键：**0 个**；
* 「保险合同负债」的原值实际存在 **`contract_liabilities`** 这个键下
  （Sina 适配器 `_FIELD_BY_TITLE['contract_liabilities'] = ('合同负债',)`
  是精确匹配，归一化后行的键就是标准列名）。
  实测样例：601318 @2024-12-31 → `contract_liabilities = 4984795000000.0`
  （= 4.98 万亿，与审计给的数字逐位一致）；601628 @2024-12-31 →
  `contract_liabilities = 5825026000000.0`。
* 与 `balance_sheet.contract_liabilities IS NULL` + `占比 >= 0.25` 组合，
  正式库正好命中 **74 行 / 5 只**，与审计口径完全一致。

因此本脚本按 `RAW_VALUE_KEYS` 的优先级逐个尝试（中文名/大写英文名也留在列表里，
以便未来别的源真的按那些键写入时无需改代码），并**打印每个键的命中行数**，
让"键名是否变了"当场可见。

判据（与写库守卫同一个函数，见 `app/core/financial_guards.py`）
---------------------------------------------------------------
* 保险股：`stock_meta` 行业口径（`保险Ⅱ` / 一级「保险」/ 名称含「保险」）；
* 该值占负债合计 >= 阈值（默认 0.25，可用 `--threshold` 或
  `VD_INSURER_CONTRACT_LIABILITY_MAX_SHARE` 覆盖）→ 判定为 CAS25 保险合同负债；
* 占比 < 阈值 → **真实**合同负债（如 601319 2011–2013 的 49.2B/50.3B/41.6B，
  占负债 6%–9%），一行都不碰。

安全
----
* 默认 **dry-run**，`--yes` 才写库；
* `--yes` 先 `COPY financial_sector_items TO …parquet` 备份到 `.planning/`，
  再持单写者锁（`storage/update_lock.py`）在**单个事务**里 upsert（失败整体回滚）；
* 幂等：`insurance_contract_liability` 已有值的行只统计不覆盖（值不同会单独列出）。

用法
----
    python scripts/backfill_insurance_contract_liability.py              # dry-run
    python scripts/backfill_insurance_contract_liability.py --sql        # 只打印 SQL
    python scripts/backfill_insurance_contract_liability.py --yes        # 备份+写锁+写库
    python scripts/backfill_insurance_contract_liability.py --yes --codes 601318,601628

为什么可以放心重跑：`scripts/fetch_financial_sector.py`（金融股专用三表补采）
会把 `source='eastmoney_f10'` 的行删掉重建，而这些重建行不带该列 —— 重跑本脚本
即可把值补回来（这是已知的跨管线交互，根治需 supplement 侧保留该列/改用
`ON CONFLICT DO UPDATE`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.financial_guards import (  # noqa: E402
    BACKFILL_SOURCE,
    FINANCIAL_SECTOR_TABLE,
    INSURANCE_CONTRACT_LIABILITY_COLUMN,
    insurer_contract_liability_decision,
    is_insurer_industry,
    normalize_report_date,
    upsert_insurance_contract_liability,
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
from scripts._maintenance_safety import backup_tables, confirm_destructive  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

#: CAS25 新保险合同准则起算报告期（A+H 险企 2022 年报起，其余 2023 年报起）
CAS25_START = "2022-12-31"

#: raw_data 里可能承载「保险合同负债」原值的键名，按优先级排列。
#: 第 1 个是 2026-11-17 正式库实测命中的那个（**Sina 归一化后的标准列名**）。
RAW_VALUE_KEYS: tuple[str, ...] = (
    "contract_liabilities",
    "insurance_contract_liability",
    "保险合同负债",
    "保险合同负债合计",
    "INSURANCE_CONTRACT_LIAB",
    "INSURANCE_CONTRACT_LIABILITY",
    "INSURANCE_CONTRACT_RESERVE_LIAB",
)

#: 只用于 dry-run 打印的对照名单（**不是**过滤条件，判据永远是行业+量级）
AFFECTED_CODES: tuple[str, ...] = ("601318", "601319", "601336", "601601", "601628")

#: SQL 侧的行业**预筛**（仅减少扫描量）；权威判定是 `is_insurer_industry()`。
_INSURER_PREFILTER = (
    "m.sw_level2 = '保险Ⅱ' OR m.sw_level1 = '保险' OR m.name LIKE '%保险%'"
)

SELECT_CANDIDATE_SQL = f"""
SELECT b.stock_code                       AS stock_code,
       b.report_date                      AS report_date,
       b.report_type                      AS report_type,
       b.contract_liabilities             AS contract_liabilities,
       b.total_liabilities                AS total_liabilities,
       b.raw_data                         AS raw_data,
       m.name                             AS name,
       m.sw_level1                        AS sw_level1,
       m.sw_level2                        AS sw_level2,
       f.{INSURANCE_CONTRACT_LIABILITY_COLUMN} AS specialty_existing
FROM balance_sheet b
JOIN stock_meta m ON m.stock_code = b.stock_code
LEFT JOIN {FINANCIAL_SECTOR_TABLE} f
       ON f.stock_code = b.stock_code AND f.report_date = b.report_date
WHERE ({_INSURER_PREFILTER})
  AND b.raw_data IS NOT NULL
  AND b.report_date >= DATE '{CAS25_START}'
ORDER BY b.stock_code, b.report_date
"""


def normalize_codes(codes: list[str] | None) -> list[str]:
    """规范化股票代码；非法输入直接报错（绝不把未校验字符串拼进 SQL）。"""
    if not codes:
        return []
    normalized: list[str] = []
    for raw in codes:
        code = str(raw).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            raise ValueError(f"非法股票代码: {raw!r}")
        normalized.append(code)
    return sorted(set(normalized))


def _as_float(value: Any) -> float | None:
    """JSON 值 → float；字符串数字也接受（raw_data 里可能是字符串）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def extract_raw_contract_liability(raw: Any) -> tuple[float | None, str | None]:
    """从 `balance_sheet.raw_data` 里取出「保险合同负债」原值。

    Args:
        raw: raw_data 列（JSON 字符串）。

    Returns:
        `(value, key)`；取不到时 `(None, None)`。`key` 用于打印"到底是哪个键命中的"。
    """
    if raw is None:
        return None, None
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    for key in RAW_VALUE_KEYS:
        if key in payload:
            value = _as_float(payload[key])
            if value is not None:
                return value, key
    # 容错：大小写/空格不同的同义键（例：'Contract_Liabilities'）。
    normalized = {str(key).strip().upper().replace(" ", ""): key for key in payload}
    for key in RAW_VALUE_KEYS:
        hit = normalized.get(key.strip().upper().replace(" ", ""))
        if hit is None:
            continue
        value = _as_float(payload[hit])
        if value is not None:
            return value, str(hit)
    return None, None


def collect_backfill_rows(
    duck: DuckDBStore,
    *,
    codes: list[str] | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    """只读收集可回填行，并给出分类统计（不写库）。

    Returns:
        `{"rows": [...], "stats": {...}}`；`rows` 每项含
        `stock_code / report_date / value / raw_key / total_liabilities / share`。
    """
    wanted = set(normalize_codes(codes))
    candidates = duck.read_query(SELECT_CANDIDATE_SQL)
    rows: list[dict[str, Any]] = []
    raw_missing: list[dict[str, Any]] = []
    genuine: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    not_insurer = 0
    key_counter: Counter[str] = Counter()

    for candidate in candidates:
        code = str(candidate["stock_code"])
        if wanted and code not in wanted:
            continue
        if not is_insurer_industry(
            industry_level1=candidate.get("sw_level1"),
            industry_level2=candidate.get("sw_level2"),
            company_name=candidate.get("name"),
        ):
            not_insurer += 1
            continue
        day = normalize_report_date(candidate["report_date"])
        value, raw_key = extract_raw_contract_liability(candidate.get("raw_data"))
        if value is None:
            raw_missing.append({
                "stock_code": code,
                "report_date": day,
                "contract_liabilities": candidate.get("contract_liabilities"),
            })
            continue
        key_counter[raw_key or "?"] += 1
        allowed, reason = insurer_contract_liability_decision(
            code,
            candidate.get("sw_level1"),
            candidate.get("sw_level2"),
            value,
            candidate.get("total_liabilities"),
            threshold=threshold,
            company_name=candidate.get("name"),
        )
        if allowed:
            # 占比低于阈值 → 真实合同负债（601319 2011–2013 那三行就是这一类）。
            genuine.append({
                "stock_code": code, "report_date": day, "value": value, "reason": reason,
            })
            continue
        if candidate.get("contract_liabilities") is not None:
            # 还没被清过（或已被重新写回）：不属于"被清的 74 行"，不予改道。
            genuine.append({
                "stock_code": code, "report_date": day, "value": value,
                "reason": "still_present_in_contract_liabilities",
            })
            continue
        existing = candidate.get("specialty_existing")
        if existing is not None:
            already.append({
                "stock_code": code, "report_date": day, "value": value, "existing": existing,
            })
            if _as_float(existing) != value:
                conflicts.append({
                    "stock_code": code, "report_date": day,
                    "raw_value": value, "specialty_value": existing,
                })
            continue
        total = _as_float(candidate.get("total_liabilities"))
        rows.append({
            "stock_code": code,
            "report_date": day,
            "value": value,
            "raw_key": raw_key,
            "raw_hash": hashlib.sha256(str(candidate.get("raw_data")).encode("utf-8")).hexdigest(),
            "total_liabilities": total,
            "share": (value / total) if total else None,
            "reason": reason,
        })

    stats = {
        "candidates": len(candidates),
        "backfillable_rows": len(rows),
        "backfillable_codes": sorted({row["stock_code"] for row in rows}),
        "backfillable_sum": sum(row["value"] for row in rows),
        "raw_missing_rows": len(raw_missing),
        "genuine_contract_liability_rows": len(genuine),
        "already_filled_rows": len(already),
        "already_filled_conflicts": conflicts,
        "non_insurer_rows_skipped": not_insurer,
        "raw_key_hits": dict(key_counter),
        "first_report_date": rows[0]["report_date"] if rows else None,
        "last_report_date": rows[-1]["report_date"] if rows else None,
    }
    return {
        "rows": rows,
        "stats": stats,
        "raw_missing": raw_missing,
        "genuine": genuine,
        "already_filled": already,
    }


@contextmanager
def _no_lock() -> Iterator[None]:
    """不额外加锁（测试用；正式路径必须传 `lock_path`）。"""
    yield


def backfill(
    duck: DuckDBStore,
    *,
    yes: bool = False,
    codes: list[str] | None = None,
    threshold: float | None = None,
    backup: bool = True,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """执行（或演练）回填，返回可核对的结果字典。

    Args:
        duck: 已打开的 DuckDBStore（CLI 用正式库；测试用临时库）。
        yes: False = dry-run，只读不写。
        codes: 只处理这些股票（None = 判据命中的全部）。
        threshold: 覆盖默认占比阈值。
        backup: 写库前是否备份 `financial_sector_items`（CLI 恒为 True）。
        lock_path: 单写者锁的库路径；None = 不额外加锁（仅限测试/演练）。
    """
    collected = collect_backfill_rows(duck, codes=codes, threshold=threshold)
    rows = collected["rows"]
    report: dict[str, Any] = {
        "dry_run": not yes,
        "mode": "dry_run" if not yes else "write",
        "selected": collected["stats"],
        "written": 0,
        "written_rows": [],
        "remaining_after": None,
        "backup_dir": None,
        "specialty_non_null_after": None,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    if not rows or not yes:
        return report

    columns = table_columns_from_store(duck)
    if not columns or INSURANCE_CONTRACT_LIABILITY_COLUMN not in columns:
        report["mode"] = "skipped"
        report["reason"] = f"{FINANCIAL_SECTOR_TABLE}.{INSURANCE_CONTRACT_LIABILITY_COLUMN} 列不存在"
        return report

    if backup:
        report["backup_dir"] = str(
            backup_tables(duck, [FINANCIAL_SECTOR_TABLE], "p06-insurance-contract-liability")
        )
    lock = exclusive_update(lock_path) if lock_path is not None else _no_lock()
    written: list[dict[str, Any]] = []
    with lock, duck.transaction() as conn:
        for row in rows:
            outcome = upsert_insurance_contract_liability(
                conn,
                stock_code=row["stock_code"],
                report_date=row["report_date"],
                value=row["value"],
                available_columns=columns,
                source=BACKFILL_SOURCE,
                # 溯源哈希用 balance_sheet.raw_data 的**原文**（收集阶段已算好；
                # 绝不在写事务里再开读连接 —— 那会和写锁互等）。
                raw_response_hash=row["raw_hash"],
            )
            if outcome in {"inserted", "updated"}:
                written.append({**row, "write": outcome})
    report["written"] = len(written)
    report["written_rows"] = written

    remaining = collect_backfill_rows(duck, codes=codes, threshold=threshold)
    report["remaining_after"] = remaining["stats"]["backfillable_rows"]
    report["specialty_non_null_after"] = int(
        duck.read_query(
            f"SELECT COUNT({INSURANCE_CONTRACT_LIABILITY_COLUMN}) AS n "
            f"FROM {FINANCIAL_SECTOR_TABLE}"
        )[0]["n"]
    )
    return report


def table_columns_from_store(duck: DuckDBStore) -> set[str] | None:
    """整库维度取一次列集合（CLI/脚本路径，没有 initializer 缓存可复用）。"""
    rows = duck.read_query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [FINANCIAL_SECTOR_TABLE],
    )
    return {str(row["column_name"]) for row in rows} if rows else None


def _print_summary(collected: Mapping[str, Any]) -> None:
    stats = collected["stats"]
    logger.info(
        "可回填 %d 行（%d 只股票；合计 %.4f 亿元；%s ~ %s）",
        stats["backfillable_rows"], len(stats["backfillable_codes"]),
        stats["backfillable_sum"] / 1e8,
        stats["first_report_date"], stats["last_report_date"],
    )
    logger.info("  行业：%s", ", ".join(stats["backfillable_codes"]) or "-")
    logger.info(
        "  raw_data 键命中分布：%s",
        ", ".join(f"{key}×{count}" for key, count in stats["raw_key_hits"].items()) or "-",
    )
    logger.info(
        "  raw_data 里找不到原值的行：%d；占比低于阈值（真实合同负债）跳过：%d；"
        "专用列已有值（不覆盖）：%d；非保险股跳过：%d",
        stats["raw_missing_rows"], stats["genuine_contract_liability_rows"],
        stats["already_filled_rows"], stats["non_insurer_rows_skipped"],
    )
    if stats["already_filled_conflicts"]:
        logger.warning(
            "  专用列已有值但与原值不同（需人工判读）：%s",
            stats["already_filled_conflicts"],
        )
    for row in collected["rows"][:20]:
        logger.info(
            "  %s %s 保险合同负债=%.4f 亿 负债合计=%.4f 亿 占比=%s 键=%s",
            row["stock_code"], row["report_date"], row["value"] / 1e8,
            (row["total_liabilities"] or 0) / 1e8,
            f"{row['share']:.3f}" if row["share"] is not None else "-",
            row["raw_key"],
        )
    if len(collected["rows"]) > 20:
        logger.info("  … 其余 %d 行略", len(collected["rows"]) - 20)
    for row in collected["raw_missing"][:5]:
        logger.info(
            "  [缺原值] %s %s contract_liabilities=%s",
            row["stock_code"], row["report_date"], row["contract_liabilities"],
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "把 balance_sheet.raw_data 里的「保险合同负债」原值回填到 "
            "financial_sector_items.insurance_contract_liability（P0-6）"
        ),
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--sql", action="store_true", help="只打印候选 SQL 后退出")
    ap.add_argument("--codes", default="", help="只处理这些股票代码（逗号分隔）")
    ap.add_argument(
        "--threshold", type=float, default=None,
        help="占比阈值（默认 0.25，也可用 VD_INSURER_CONTRACT_LIABILITY_MAX_SHARE）",
    )
    args = ap.parse_args()

    if args.sql:
        print("-- 候选行（行业预筛 + raw_data 原值在 Python 侧解析）--")
        print(SELECT_CANDIDATE_SQL.strip().rstrip(";") + ";")
        print()
        print("-- raw_data 键名优先级 --")
        for key in RAW_VALUE_KEYS:
            print(f"--   {key}")
        return

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    codes = [item.strip() for item in args.codes.split(",") if item.strip()] or None
    collected = collect_backfill_rows(duck, codes=codes, threshold=args.threshold)
    _print_summary(collected)
    if not collected["rows"]:
        logger.info("没有可回填行（可能已回填过），退出。")
        return
    if not args.yes:
        logger.info(
            "[DRY RUN] 未写库；加 --yes 执行"
            "（会先备份 financial_sector_items 并加写锁）"
        )
        return
    if not confirm_destructive(args.yes):
        return

    try:
        report = backfill(
            duck, yes=True, codes=codes, threshold=args.threshold,
            lock_path=paths.duckdb_path,
        )
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error

    if report["backup_dir"]:
        logger.info("[BACKUP] %s", report["backup_dir"])
    logger.info(
        "已回填 %d 行；复核剩余可回填 %s 行；专用列非空 %s 行",
        report["written"], report["remaining_after"], report["specialty_non_null_after"],
    )


if __name__ == "__main__":
    main()
