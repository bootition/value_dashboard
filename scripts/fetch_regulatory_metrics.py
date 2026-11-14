"""补齐 6 个金融监管指标 —— CLI 薄包装（2026-09-19 复审修复）。

核心逻辑已抽到 `app/core/supplement.py::RegulatoryMetricsUpdater`
（与自动更新主流程共用同一份实现，`2026-11-14 reports/119 D29` 自动接入）。
本脚本只保留 CLI 语义：写前备份、单写者锁、证据 JSON。

背景
----
`balance_sheet` 自建库起就有 6 个监管列（资本充足率/核心一级/一级/不良贷款率/
拨备覆盖率/风险覆盖率），但**从未有任何适配器写入**。东财 datacenter
`RPT_F10_FINANCE_MAINFINADATA`（主要指标，161 字段）1 次请求/股票即可提供：

    NEWCAPITALADER          → capital_adequacy_ratio            资本充足率
    FIRST_ADEQUACY_RATIO    → tier1_capital_adequacy_ratio      一级资本充足率
    CQBL                    → core_tier1_capital_adequacy_ratio 核心一级（**仅银行**）
    NONPERLOAN              → non_performing_loan_ratio         不良贷款率
    BLDKBBL                 → provision_coverage_ratio          拨备覆盖率
    RISK_COVERAGE           → risk_coverage_ratio               风险覆盖率（**仅券商**）

口径
----
- 全部为**百分数原值**（13.14 = 13.14%），与现有列约定一致；
- 只对**金融股**（银行/券商）抓取；其他行业该指标本不适用，保持 NULL；
- 缺失如实为空，不估算；只 UPDATE 既有 `balance_sheet` 行，不 INSERT/DELETE。

续传语义（与另外两个补采脚本不同）
--------------------------------
本域**每次到期全量重抓**（每只金融股 1 次请求、只取最新一期），没有
"有没有本源行"的判据，因此不存在 `fetch_cashflow_supplement.py` 那类
"跑完一次就永久跳过"的 bug；接入自动更新只需要到期判断 + 每轮上限。

用法
----
    python scripts/fetch_regulatory_metrics.py            # dry-run
    python scripts/fetch_regulatory_metrics.py --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
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
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402
from app.core.supplement import RegulatoryMetricsUpdater  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="补齐 6 个金融监管指标")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部金融股）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    from app.core.storage.schema import init_duckdb_schema
    init_duckdb_schema(duck)

    updater = RegulatoryMetricsUpdater(duck=duck)
    universe = updater.universe()
    logger.info("金融类股票 %d 只（银行/券商）", len(universe))

    if not args.yes:
        # dry-run：不写库，逐只探测前 --limit 只（默认 5 只）并打印取值。
        for code, exchange, name in universe[: (args.limit or 5)]:
            from app.core.supplement import datacenter_secucode, regulatory_field_map

            row = updater._fetch_row(datacenter_secucode(code, exchange))
            if row is None:
                logger.info("  %s %s → 无返回", code, name)
                continue
            fields = regulatory_field_map(name)
            logger.info(
                "  %s %s %s → %s",
                code, name, str(row.get("REPORT_DATE") or "")[:10],
                {column: row.get(em_field) for em_field, column in fields.items()},
            )
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-regulatory-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "balance_sheet.parquet").replace("'", "''")
        conn.execute(f"COPY balance_sheet TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    report = updater.refresh(max_stocks=args.limit, deadline_seconds=0)
    logger.info(
        "写入完成：updated=%d failed=%d 本轮写入 %d 行 / balance_sheet 中资本充足率"
        "非空 %d 行 (status=%s)",
        report["updated"], report["failed"], report["rows_updated"],
        report["coverage_rows"], report["status"],
    )

    evidence = {
        "task": "补齐 6 个金融监管指标",
        "executed_at": datetime.now(UTC).isoformat(),
        "stocks_requested": report["targeted"],
        "stocks_fetched": report["updated"], "stocks_failed": report["failed"],
        "no_regulatory_data": report["skipped"],
        "rows_updated": report["rows_updated"],
        "balance_sheet_rows_with_capital_adequacy": report["coverage_rows"],
        "backup_dir": str(backup), "status": report["status"],
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r19_regulatory_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)


if __name__ == "__main__":
    main()
