"""补齐 `income_statement.exchange_gain`（只回填 NULL）—— 安全版 CLI。

结论先行（2026-11-15 实测，reports/122 §B）
--------------------------------------------
1. **这个列的既有语义是「公允价值变动收益」，不是「汇兑收益」：**
   `schema.py` 注释、CSMAR `B001301000`（公允价值变动收益）、
   `scripts/import_csmar.py` / `scripts/supplement_akshare.py`
   （`FAIRVALUE_CHANGE_INCOME → exchange_gain`）三方一致，且逐值可核：
   600028 2026-03-31 库里 -8,997,000,000 == 东财 `FAIRVALUE_CHANGE_INCOME`；
   600030 2026-03-31 库里 6,126,738,961.31 == 东财 `FAIRVALUE_CHANGE_INCOME`。
2. **"52.1% → 3.5% 断崖"是两个口径交替的结果，不是调度缺失：**
   2025-06-30~2026-03-31 由 `supplement_akshare` 灌「公允价值变动收益」（~52%），
   之后只有新浪刷新，而新浪适配器把「汇兑收益」映射进本列、且只在 ~194 只
   股票上返回该科目（2026-09-30 全库 194/5,575 = 3.5%）→ 于是"断崖"。
   对 2026-09-30 的 5,381 个 NULL，新浪源里**根本没有这一行**
   （`sina_adapter._parse_payload` 只写真实存在的字段，raw_data 里无该键），
   应判 **SOURCE_EMPTY**，不是 NOT_DISPATCHED。
3. **源是有的**：东财 F10 `lrbAjaxNew` 同时提供
   `FAIRVALUE_CHANGE_INCOME`（公允价值变动收益，本列语义）与
   `EXCHANGE_INCOME`（汇兑收益），7 只实测（601398/600036/600030/601318/
   601628/601601/600028）全部有响应，且 2026-09-30 的 NULL 股也能取到值。

所以本脚本默认用 `FAIRVALUE_CHANGE_INCOME` 回填（`--field fairvalue_change_income`），
保留 `--field exchange_income` 供"把列重新定义成汇兑收益"的裁决使用。
**只 UPDATE 现有行的 NULL，绝不 INSERT / DELETE / 覆盖既有非空值。**

用法
----
    python scripts/fetch_exchange_gain.py                       # dry-run，抽样探测
    python scripts/fetch_exchange_gain.py --limit 50            # 只处理 50 只
    python scripts/fetch_exchange_gain.py --yes --concurrency 4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
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
    EXCHANGE_GAIN,
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
# 两个候选口径：目标列都是 exchange_gain，源字段不同。
FIELD_CHOICES: dict[str, dict[str, str]] = {
    # 本列的既有语义（CSMAR B001301000 / schema 注释 / 历史数据）
    "fairvalue_change_income": {"FAIRVALUE_CHANGE_INCOME": "exchange_gain"},
    # 前端标签与新浪适配器使用的语义（与历史数据冲突，需产品裁决后使用）
    "exchange_income": {"EXCHANGE_INCOME": "exchange_gain"},
}
RAW_FIELDS = ("FAIRVALUE_CHANGE_INCOME", "EXCHANGE_INCOME")


# ─── 覆盖度快照 ───────────────────────────────────────────────────────


def coverage_snapshot(duck: DuckDBStore, periods: list[str]) -> list[dict[str, Any]]:
    """逐报告期的 exchange_gain 覆盖（分母=三表行数）。"""
    if not periods:
        return []
    rows = duck.read_query(
        """SELECT CAST(report_date AS VARCHAR) AS report_date,
                  COUNT(*) AS rows,
                  COUNT(exchange_gain) AS non_null,
                  COUNT(*) - COUNT(exchange_gain) AS nulls
           FROM income_statement
           WHERE report_date >= CAST(? AS DATE) AND report_date <= CAST(? AS DATE)
           GROUP BY 1 ORDER BY 1 DESC""",
        [str(periods[-1])[:10], str(periods[0])[:10]],
    )
    return [
        {
            "report_date": str(row["report_date"])[:10],
            "rows": int(row["rows"]),
            "non_null": int(row["non_null"]),
            "nulls": int(row["nulls"]),
            "coverage_pct": round(100.0 * int(row["non_null"]) / int(row["rows"]), 1)
            if int(row["rows"]) else 0.0,
        }
        for row in rows
    ]


# ─── 一轮补采（CLI 与测试共用）─────────────────────────────────────────


def run(
    fetcher: SupplementFetcher,
    *,
    field: str = "fairvalue_change_income",
    periods: list[str] | None = None,
    limit: int = 0,
    concurrency: int = 4,
    write: bool = False,
    sample: int = 3,
    sample_codes: list[str] | None = None,
    deadline_seconds: float = 0.0,
    retry_days: int = 7,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """执行一轮：dry-run 只抽样探测，`write=True` 才落库（只填 NULL）。

    `sample_codes` 只影响 dry-run 的抽样对象（点名复核，如 600028 无值 /
    601398 有值），不影响待补判据与写库范围。
    """
    if field not in FIELD_CHOICES:
        raise ValueError(f"unknown field: {field}")
    field_map = FIELD_CHOICES[field]
    spec = replace(SPECS[EXCHANGE_GAIN], field_map=dict(field_map))
    resolved = fetcher.resolve_periods(EXCHANGE_GAIN, periods=periods)
    report: dict[str, Any] = {
        "mode": "write" if write else "dry-run",
        "field": field,
        "source_field": next(iter(field_map)),
        "periods": resolved,
        "coverage_before": coverage_snapshot(fetcher.duck, resolved),
        "sample": [],
        "refresh": None,
    }
    pending = fetcher.pending_codes(
        EXCHANGE_GAIN, resolved, max_stocks=limit, retry_days=retry_days,
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
            parsed_rows = parsed.get("income_statement", [])
            source_hits = [
                {
                    "report_date": str(row.get("REPORT_DATE") or "")[:10],
                    **{raw: row.get(raw) for raw in RAW_FIELDS},
                }
                for row in rows
            ]
            report["sample"].append({
                "stock_code": code,
                "raw_rows": len(rows),
                "parsed_rows": len(parsed_rows),
                "source_values": source_hits,
                "preview": [
                    {
                        "report_date": str(item.get("report_date")),
                        "exchange_gain": item.get("exchange_gain"),
                    }
                    for item in parsed_rows
                ],
            })
        report["status"] = "dry_run"
        return report

    report["refresh"] = fetcher.refresh(
        EXCHANGE_GAIN,
        periods=resolved,
        max_stocks=limit,
        concurrency=concurrency,
        deadline_seconds=deadline_seconds,
        retry_days=retry_days,
        timeout=timeout,
        field_map=field_map,
    )
    report["coverage_after"] = coverage_snapshot(fetcher.duck, resolved)
    report["status"] = report["refresh"].get("status", "unknown")
    return report


def _write_evidence(report: dict[str, Any], evidence_dir: Path) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = evidence_dir / f"exchange_gain_{stamp}.json"
    payload = {
        "task": "回填 income_statement.exchange_gain（公允价值变动收益，只填 NULL）",
        "executed_at": datetime.now(UTC).isoformat(),
        "semantics_note": (
            "列 exchange_gain 的历史语义是公允价值变动收益（CSMAR B001301000 / "
            "supplement_akshare FAIRVALUE_CHANGE_INCOME）；新浪适配器把汇兑收益写进"
            "本列，且只覆盖 ~194 只 → 52.1%→3.5% 断崖的根因。"
            "2026-09-30 的 5,381 个 NULL 应判 SOURCE_EMPTY 而非 NOT_DISPATCHED。"
        ),
        **report,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    return path


def _backup_column(duck: DuckDBStore, periods: list[str]) -> Path:
    backup_dir = (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-exchange-gain-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not periods:
        raise SystemExit("没有目标报告期，无法备份")
    start = date.fromisoformat(str(periods[-1])[:10])
    end = date.fromisoformat(str(periods[0])[:10])
    with duck.write_connection() as conn:
        target = str(backup_dir / "income_statement_exchange_gain.parquet")
        target = target.replace("'", "''")
        conn.execute(
            f"""COPY (
                    SELECT stock_code, report_date, exchange_gain
                    FROM income_statement
                    WHERE report_date >= CAST('{start.isoformat()}' AS DATE)
                      AND report_date <= CAST('{end.isoformat()}' AS DATE)
                ) TO '{target}' (FORMAT PARQUET)"""
        )
    return backup_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="回填 exchange_gain（只填 NULL，不覆盖）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument(
        "--field", choices=sorted(FIELD_CHOICES), default="fairvalue_change_income",
        help="源字段口径：fairvalue_change_income=公允价值变动收益（默认，列既有语义）；"
             "exchange_income=汇兑收益（前端标签口径，需产品裁决）",
    )
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部待补）")
    ap.add_argument("--concurrency", type=int, default=4, help="并发数（建议 ≤4）")
    ap.add_argument("--sample", type=int, default=3, help="dry-run 抽样探测只数")
    ap.add_argument(
        "--codes", default="",
        help="dry-run 点名探测的股票代码（逗号分隔，如 600028,601398）",
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
    resolved = fetcher.resolve_periods(EXCHANGE_GAIN, periods=periods)
    source_field = next(iter(FIELD_CHOICES[args.field]))
    logger.info("目标报告期: %s", resolved)
    logger.info("口径: %s → exchange_gain（源字段 %s）", args.field, source_field)
    logger.info("补采前覆盖: %s", coverage_snapshot(duck, resolved))

    backup_dir = None
    if args.yes:
        backup_dir = _backup_column(duck, resolved)
        logger.info("[BACKUP] %s", backup_dir)

    report = run(
        fetcher,
        field=args.field,
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
            "  抽样 %s: raw=%d parsed=%d 源值=%s",
            item["stock_code"], item["raw_rows"], item["parsed_rows"],
            item["source_values"],
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
