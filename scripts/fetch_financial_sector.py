"""补采金融股专用三表（`financial_sector_items`，2025Q2 起）—— 安全版 CLI。

为什么需要它
------------
`financial_sector_items` 现有 5,058 行 / 25 个业务列（`pragma` 实测），
**只覆盖到 2025-03-31**（CSMAR C17 截止）。2025-06-30 之后银行/券商/保险的
「客户存款 / 发放贷款及垫款 / 利息净收入 / 保费收入 / 代理买卖证券款」等
专用科目**没有任何来源**，行业研究域从 2025Q2 起直接断供。

本脚本走 `app/core/supplement.py` 的声明式补采通道（`FINANCIAL_SECTOR` spec）：

- 数据源：东方财富 F10 `zcfzbAjaxNew`（资产负债表）+ `lrbAjaxNew`（利润表）；
- 金融股必须用真实 `companyType`（银行=3 / 保险=2 / 证券=1），
  `companyType=4` 对金融股的资产负债表**直接返回空**；
- 按响应行自带的 `ORG_TYPE` 选择行业字段映射（同名不同义：券商
  `CUSTOMER_DEPOSIT` 是「客户资金存款」资产科目，不写进「吸收存款」）；
- **只写 2025-03-31 之后**的报告期，不碰 CSMAR 覆盖期；
- 按 (source, stock_code, 报告期) 先删后插（幂等），不 DELETE CSMAR 行；
- 只对申万一级「银行 / 非银金融」的上市股票执行。

用法
----
    python scripts/fetch_financial_sector.py                  # dry-run（抽样探测 5 只）
    python scripts/fetch_financial_sector.py --limit 20       # 只处理前 20 只
    python scripts/fetch_financial_sector.py --yes --concurrency 4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402
from app.core.supplement import (  # noqa: E402
    FINANCIAL_SECTOR,
    SPECS,
    SupplementFetcher,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_EVIDENCE_DIR = (
    PROJECT_ROOT / ".planning" / "2026-11-15-financial-sector" / "evidence"
)
# 金融股专用三表：CSMAR C17 止于 2025-03-31，只补这之后。
VALUE_COLUMNS = tuple(
    column for column in SPECS[FINANCIAL_SECTOR].columns["financial_sector_items"]
    if column not in {
        "stock_code", "report_date", "report_type",
        "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
    }
)


# ─── 股票池 ───────────────────────────────────────────────────────────


def financial_universe_counts(duck: DuckDBStore) -> dict[str, int]:
    """申万口径的金融股数量（银行 / 非银金融），用于证据与"只跑金融股"核对。"""
    rows = duck.read_query(
        """SELECT COALESCE(sw_level1, '(null)') AS industry, COUNT(*) AS n
           FROM stock_meta
           WHERE is_listed IS TRUE
             AND (sw_level1 IN ('银行', '非银金融')
                  OR (sw_level1 IS NULL AND csrc_l1 = '金融业'))
           GROUP BY 1 ORDER BY 2 DESC"""
    )
    counts = {str(row["industry"]): int(row["n"]) for row in rows}
    counts["total"] = sum(counts.values())
    return counts


def table_snapshot(duck: DuckDBStore) -> dict[str, Any]:
    """`financial_sector_items` 现状（判断补采前后是否真的前进了）。"""
    rows = duck.read_query(
        """SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stocks,
                  MIN(report_date) AS min_date, MAX(report_date) AS max_date
           FROM financial_sector_items"""
    )
    payload = dict(rows[0]) if rows else {}
    by_source = duck.read_query(
        """SELECT source, COUNT(*) AS rows, MAX(report_date) AS max_date
           FROM financial_sector_items GROUP BY 1 ORDER BY 1"""
    )
    payload["by_source"] = [
        {
            "source": str(row["source"]),
            "rows": int(row["rows"]),
            "max_date": str(row["max_date"])[:10],
        }
        for row in by_source
    ]
    payload["min_date"] = str(payload.get("min_date"))[:10]
    payload["max_date"] = str(payload.get("max_date"))[:10]
    return payload


# ─── 一轮补采（CLI 与测试共用）─────────────────────────────────────────


def run(
    fetcher: SupplementFetcher,
    *,
    periods: list[str] | None = None,
    limit: int = 0,
    concurrency: int = 4,
    write: bool = False,
    sample: int = 5,
    sample_codes: list[str] | None = None,
    deadline_seconds: float = 0.0,
    retry_days: int = 7,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """执行一轮：dry-run 只抽样探测，`write=True` 才落库。

    `sample_codes` 只影响 dry-run 的抽样对象（用于点名复核某几只股票，
    例如 601398/600036 银行 + 600030 券商），不影响待补判据与写库范围。
    """
    spec = SPECS[FINANCIAL_SECTOR]
    resolved = fetcher.resolve_periods(FINANCIAL_SECTOR, periods=periods)
    report: dict[str, Any] = {
        "mode": "write" if write else "dry-run",
        "periods": resolved,
        "universe": financial_universe_counts(fetcher.duck),
        "before": table_snapshot(fetcher.duck),
        "sample": [],
        "refresh": None,
    }
    pending = fetcher.pending_codes(
        FINANCIAL_SECTOR, resolved, max_stocks=limit, retry_days=retry_days,
    )
    report["pending_stocks"] = len(pending)
    if not resolved:
        report["status"] = "skipped"
        report["reason"] = "no_target_periods"
        return report
    if not write and sample_codes:
        by_code = dict(pending)
        targets = [(code, by_code.get(code, "")) for code in sample_codes]
    elif not pending:
        report["status"] = "skipped"
        report["reason"] = "no_pending_codes"
        return report
    else:
        targets = pending[: max(0, sample)]

    if not write:
        fetched_at = datetime.now(UTC).replace(tzinfo=None)
        for code, exchange in targets:
            rows = fetcher._fetch_rows(spec, code, exchange, resolved, timeout=timeout)
            parsed = fetcher._parse_rows(
                spec, code, rows,
                batch_id="dry-run", fetched_at=fetched_at, src_hash="dry-run",
            )
            parsed_rows = parsed.get("financial_sector_items", [])
            report["sample"].append({
                "stock_code": code,
                "raw_rows": len(rows),
                "parsed_rows": len(parsed_rows),
                "filled_columns": sorted(
                    {
                        column
                        for row in parsed_rows
                        for column, value in row.items()
                        if value is not None and column in VALUE_COLUMNS
                    }
                ),
                "preview": [
                    {
                        column: row.get(column)
                        for column in VALUE_COLUMNS
                        if row.get(column) is not None
                    }
                    for row in parsed_rows
                ],
            })
        report["status"] = "dry_run"
        return report

    report["refresh"] = fetcher.refresh(
        FINANCIAL_SECTOR,
        periods=resolved,
        max_stocks=limit,
        concurrency=concurrency,
        deadline_seconds=deadline_seconds,
        retry_days=retry_days,
        timeout=timeout,
    )
    report["after"] = table_snapshot(fetcher.duck)
    report["status"] = report["refresh"].get("status", "unknown")
    return report


def _write_evidence(report: dict[str, Any], evidence_dir: Path) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = evidence_dir / f"financial_sector_{stamp}.json"
    payload = {
        "task": "金融股专用三表补采（2025Q2 起，东财 F10）",
        "executed_at": datetime.now(UTC).isoformat(),
        **report,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    return path


def _backup_table(duck: DuckDBStore) -> Path:
    backup_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-finsector-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup_dir / "financial_sector_items.parquet").replace("'", "''")
        conn.execute(f"COPY financial_sector_items TO '{target}' (FORMAT PARQUET)")
    return backup_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="补采金融股专用三表（2025Q2 起）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部待补）")
    ap.add_argument("--concurrency", type=int, default=4, help="并发数（建议 ≤4）")
    ap.add_argument("--sample", type=int, default=5, help="dry-run 抽样探测只数")
    ap.add_argument(
        "--codes", default="",
        help="dry-run 点名探测的股票代码（逗号分隔，如 601398,600036,600030）",
    )
    ap.add_argument("--deadline", type=float, default=0.0, help="本轮最大耗时秒数（0=不限）")
    ap.add_argument("--timeout", type=float, default=25.0, help="单请求超时秒数")
    ap.add_argument("--retry-days", type=int, default=7, help="失败冷却天数")
    ap.add_argument("--periods", default="", help="显式报告期，逗号分隔（默认最近 6 期）")
    ap.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝执行（避免与自动更新并发）。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    fetcher = SupplementFetcher(duck=duck)
    periods = [item.strip() for item in args.periods.split(",") if item.strip()] or None
    resolved = fetcher.resolve_periods(FINANCIAL_SECTOR, periods=periods)
    logger.info("目标报告期: %s", resolved)
    logger.info("金融股股票池: %s", financial_universe_counts(duck))
    logger.info("补采前: %s", table_snapshot(duck))

    backup_dir = None
    if args.yes:
        backup_dir = _backup_table(duck)
        logger.info("[BACKUP] %s", backup_dir)

    report = run(
        fetcher,
        periods=periods,
        limit=args.limit,
        concurrency=args.concurrency,
        write=args.yes,
        sample=args.sample,
        sample_codes=[item.strip() for item in args.codes.split(",") if item.strip()]
        or None,
        deadline_seconds=args.deadline,
        retry_days=args.retry_days,
        timeout=args.timeout,
    )
    if backup_dir is not None:
        report["backup_dir"] = str(backup_dir)

    for item in report["sample"]:
        logger.info(
            "  抽样 %s: raw=%d parsed=%d 填充列=%s",
            item["stock_code"], item["raw_rows"], item["parsed_rows"],
            item["filled_columns"],
        )
    logger.info(
        "结果: status=%s pending=%s refresh=%s",
        report["status"], report.get("pending_stocks"), report.get("refresh"),
    )
    evidence = _write_evidence(report, Path(args.evidence_dir))
    logger.info("证据 JSON: %s", evidence)
    if not args.yes:
        logger.info("[DRY RUN] 未写库；确认后加 --yes 执行。")


if __name__ == "__main__":
    main()
