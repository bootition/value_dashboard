"""导入 CSMAR AIQ 年度宽表 + 补录会计恒等式锚点（v36，「榨干」收尾）。

- csmar_aiq_annual：AIQ_LCFinIndexY.dta（68,059 行 / 54 列）
- csmar_balance_items.A004000000：负债与所有者权益总计（会计恒等式对账锚点）

用法:
    python scripts/import_csmar_aiq_and_anchor.py            # dry-run
    python scripts/import_csmar_aiq_and_anchor.py --yes
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
AIQ_FILE = "上市公司财务指标数据表(年)/AIQ_LCFinIndexY.dta"
AIQ_FIELDS = ["Cash", "AccountsReceivable", "NonCurrentAssetsInYear", "TotalCurrentAssets", "Inventory", "OtherCurrentAssets", "FixedAssets", "DisposalOfFixedAssets", "IntangibleAssets", "TotalAssets", "TotalCurrentliabilities", "TotalLiabilities", "ShortTermLoan", "AccountsPayable", "TaxePayable", "StockDividendPayable", "LongLiabInYearChange", "TotalEquity", "CapitalStock", "TotalRevenue", "OperatingRevenue", "TotalOperatingCost", "OperatingCost", "BusinessTaxAndSurcharge", "SellingExpenses", "ManagementExpense", "RDExpenses", "FinanceExpense", "OperatingProfit", "NonOperatingIncome", "NonOperatingExpenses", "TotalProfit", "IncomeTax", "NetProfit", "Depreciation", "AmorOfIntangibleAssets", "AmorOfDeferredExpenses", "OperatingNetCashFlow", "AssetLiabilityRatio", "ROTAA", "ROTAB", "ROTAC", "ROAA", "ROAB", "ROAC", "ROEA", "ROEB", "ROEC", "MarketValueA", "MarketValueB", "ValueBookRatioA", "ValueBookRatioB", "EPS", "NAVPS"]
ANCHOR_FILE, ANCHOR_FOLDER, ANCHOR_FIELD = "FS_Combas.dta", "资产负债表", "A004000000"


def _key(df: pd.DataFrame) -> pd.DataFrame:
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    return df.dropna(subset=["report_date"]).drop_duplicates(subset=["stock_code", "report_date"], keep="last")


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 AIQ 年度宽表 + 会计恒等式锚点")
    ap.add_argument("--yes", action="store_true")
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

    aiq_path = PACKAGE_DIR / AIQ_FILE
    anchor_path = PACKAGE_DIR / ANCHOR_FOLDER / ANCHOR_FILE
    aiq = _key(pd.read_stata(aiq_path, columns=["Stkcd", "Accper", *AIQ_FIELDS], convert_categoricals=False))
    for c in AIQ_FIELDS:
        aiq[c] = pd.to_numeric(aiq[c], errors="coerce")
    aiq = aiq.dropna(subset=AIQ_FIELDS, how="all")
    logger.info("AIQ: %d 行 / %d 只 / %d 列", len(aiq), aiq["stock_code"].nunique(), len(AIQ_FIELDS))

    anchor = _key(pd.read_stata(anchor_path, columns=["Stkcd", "Accper", ANCHOR_FIELD], convert_categoricals=False))
    anchor[ANCHOR_FIELD] = pd.to_numeric(anchor[ANCHOR_FIELD], errors="coerce")
    anchor = anchor.dropna(subset=[ANCHOR_FIELD])
    logger.info("锚点 %s: %d 行", ANCHOR_FIELD, len(anchor))

    if not args.yes:
        logger.info("[DRY RUN] 未写库")
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-csmar-aiq-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex
    with duck.write_connection() as conn:
        for t in ("csmar_aiq_annual", "csmar_balance_items"):
            target = str(backup / f"{t}.parquet").replace("'", "''")
            conn.execute(f"COPY {t} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        data = aiq[["stock_code", "report_date", *AIQ_FIELDS]].copy()
        data["report_date"] = pd.to_datetime(data["report_date"])
        data["source"], data["fetch_time"], data["batch_id"] = SOURCE, fetched_at, batch_id
        conn.register("_aiq", data)
        try:
            conn.execute(f"DELETE FROM csmar_aiq_annual WHERE source = '{SOURCE}'")
            conn.execute("INSERT INTO csmar_aiq_annual BY NAME SELECT * FROM _aiq")
        finally:
            conn.unregister("_aiq")

        # 锚点：只更新/插入该列，不触碰该表其他列
        conn.register("_anchor", anchor[["stock_code", "report_date", ANCHOR_FIELD]].assign(
            report_date=lambda x: pd.to_datetime(x["report_date"])))
        try:
            conn.execute('UPDATE csmar_balance_items AS t SET "A004000000" = a."A004000000" '
                         'FROM _anchor AS a WHERE t.stock_code = a.stock_code AND t.report_date = a.report_date')
        finally:
            conn.unregister("_anchor")

        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "csmar_aiq_annual", SOURCE, "csmar-c17-aiq", fetched_at,
             hashlib.sha256(aiq_path.read_bytes()).hexdigest()[:32], len(data),
             f"{data['report_date'].min().date()}~{data['report_date'].max().date()}", "strict"],
        )

    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r15_csmar_aiq_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps({
        "task": "导入 CSMAR AIQ 年度宽表 + 会计恒等式锚点",
        "executed_at": datetime.now(UTC).isoformat(),
        "aiq_rows": len(data), "aiq_columns": len(AIQ_FIELDS),
        "anchor_rows": len(anchor), "backup_dir": str(backup), "batch_id": batch_id,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成 AIQ %d 行 / 锚点 %d 行；证据: %s", len(data), len(anchor), ev_path)


if __name__ == "__main__":
    main()
