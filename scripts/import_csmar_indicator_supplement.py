"""导入 CSMAR 指标补充域（v33 起）—— 通用配置驱动。

背景
----
「榨干数据包」的收尾批次：把 CSMAR 里**还有价值、但此前未接入**的指标表导入独立域。
每个域在 SCHEMA 里预定义了可读列名；本脚本按 SPECS 配置逐一导入。

配置驱动：新增一张表只需在 SPECS 里加一条（表名 / .dta 相对路径 / 字段映射），
不需要改流程代码。

域纪律
------
这些域**不进筛选界面**：数据截止 2025-03-31，且部分字段与自算指标语义重叠
（同概念两套口径若同时出现在字段选择器里，会制造伪选择）。
定位是**交叉核验与专项研究**。

用法
----
    python scripts/import_csmar_indicator_supplement.py            # dry-run
    python scripts/import_csmar_indicator_supplement.py --yes
    python scripts/import_csmar_indicator_supplement.py --yes --only csmar_risk_factors
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active, exclusive_update  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

PACKAGE_DIR = PROJECT_ROOT / "额外资料" / "C17 a股上市公司财务数据合集（90-25年）" / "原始数据（dta格式）"
SOURCE = "csmar"

# ─── 配置：新增表只需加一条 ────────────────────────────────────────
SPECS: list[dict] = [
    {
        "table": "csmar_disclosure_metrics",
        "file": "披露财务指标/FI_T2.dta",
        "map": {
            "F020101": "non_recurring_gain_loss",
            "F020103": "roe_weighted",
            "F020104": "roe_weighted_deducted",
            "F020105": "eps_deducted_basic",
            "F020106": "ocf_per_share_disclosed",
            "F020107": "bps_parent_disclosed",
            "F020108": "eps_basic",
            "F020109": "eps_diluted",
        },
    },
    {
        "table": "csmar_risk_factors",
        "file": "财务指标/BDT_FinIndex.dta",
        "map": {
            "FinancialLiability": "financial_liability",
            "OperatingLiability": "operating_liability",
            "BookToMarketRatio": "book_to_market_bdt",
            "ManagementExpenseRate": "admin_expense_rate",
            "TangibleAssetRatio": "tangible_asset_ratio",
            "WorkingCapitalTurnover": "working_capital_turnover",
            "CashEquivalentsTurnover": "cash_equivalents_turnover",
            "OperatingRevenueGrowth": "revenue_growth_bdt",
            "NonDebtTaxShield": "non_debt_tax_shield",
            "IncomeTaxTate": "effective_tax_rate",   # CSMAR 字段名拼写即 "Tate"
            "ProfitsVolatility": "profits_volatility_3y",
            "CashFlowVolatility": "cashflow_volatility_3y",
            "InterestCoverageRatio": "interest_coverage_ratio",
            "TaxBearing": "tax_bearing",
            "BankLoanRatio": "bank_loan_ratio",
            "ShortLoanDependence": "short_loan_dependence",
            "ShareholdersOccupy": "shareholders_occupy",
        },
    },
]


def _load(spec: dict) -> tuple[pd.DataFrame, str]:
    path = PACKAGE_DIR / spec["file"]
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    cols = ["Stkcd", "Accper", *spec["map"]]
    df = pd.read_stata(path, columns=cols, convert_categoricals=False)
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    df = df.dropna(subset=["report_date"])
    df = df.rename(columns=spec["map"])
    for col in spec["map"].values():
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=list(spec["map"].values()), how="all")
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    logger.info("%s: %d 行 / %d 只 / %s ~ %s",
                spec["table"], len(df), df["stock_code"].nunique(),
                df["report_date"].min(), df["report_date"].max())
    return df, digest


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 CSMAR 指标补充域")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--only", default="", help="只导入指定表名，逗号分隔")
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

    wanted = {t.strip() for t in args.only.split(",") if t.strip()} or None
    specs = [s for s in SPECS if wanted is None or s["table"] in wanted]
    if not specs:
        logger.error("没有匹配的配置：%s", args.only)
        raise SystemExit(2)

    loaded = [(spec, *_load(spec)) for spec in specs]
    if not args.yes:
        for spec, df, _ in loaded:
            cov = {c: int(df[c].notna().sum()) for c in spec["map"].values()}
            logger.info("[DRY RUN] %s 各列非空: %s", spec["table"], json.dumps(cov, ensure_ascii=False))
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-csmar-ind-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex

    with duck.write_connection() as conn:
        for spec, _, _ in loaded:
            target = str(backup / f"{spec['table']}.parquet").replace("'", "''")
            conn.execute(f"COPY {spec['table']} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    summary = {}
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        for spec, df, digest in loaded:
            cols = ["stock_code", "report_date", *spec["map"].values()]
            data = df[cols].copy()
            data["report_date"] = pd.to_datetime(data["report_date"])
            data["source"] = SOURCE
            data["fetch_time"] = fetched_at
            data["batch_id"] = batch_id
            view = f"_sup_{spec['table'][:20]}"
            conn.register(view, data)
            try:
                conn.execute(f"DELETE FROM {spec['table']} WHERE source = '{SOURCE}'")
                conn.execute(f"INSERT INTO {spec['table']} BY NAME SELECT * FROM {view}")
            finally:
                conn.unregister(view)
            summary[spec["table"]] = len(data)
            conn.execute(
                """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
                   raw_response_hash, row_count, report_date_range, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [f"{batch_id}-{spec['table'][:16]}", spec["table"], SOURCE, "csmar-c17-supplement",
                 fetched_at, digest[:32], len(data),
                 f"{data['report_date'].min().date()}~{data['report_date'].max().date()}", "strict"],
            )

    evidence = {
        "task": "导入 CSMAR 指标补充域",
        "executed_at": datetime.now(UTC).isoformat(),
        "tables": summary, "backup_dir": str(backup), "batch_id": batch_id,
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r12_csmar_supplement_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成 %s；证据: %s", summary, ev_path)


if __name__ == "__main__":
    main()
