"""导入金融行业专用科目（银行 / 保险 / 证券 / 其他金融）。

背景
----
CSMAR C17 的 `FS_Combas` / `FS_Comins` 里带 `A0b*`(银行) / `A0i*`(保险) /
`A0d*`(证券) / `A0f*`(其他金融) 前缀的专用科目共 109 个，本项目此前**一个都没用**。
这些科目覆盖 78 只金融股（38 银行 + 36 券商 + 1 保险 + 1 信托 + 2 金控）。

**为什么不进筛选界面**
金融专用科目只对该行业成立 —— 「吸收存款」对一家制造业公司毫无意义。
放进全市场字段表会制造「选中后 98% 股票无数据」的伪条件，与「能选中但永远筛不出
结果」的死条件同样有害。故本域定位为**行业研究域**，供个股/行业分析取用，
不做横截面筛选。

**已知不可得**
6 个监管比率（资本充足率 / 核心一级资本充足率 / 一级资本充足率 / 不良贷款率 /
拨备覆盖率 / 风险覆盖率）**CSMAR C17 不含**，东财 F10 可及端点也未提供。
主链 `balance_sheet` 的这 6 列保持 NULL 并如实披露，**不用估算填充**。

用法
----
    python scripts/import_financial_sector_items.py          # dry-run
    python scripts/import_financial_sector_items.py --yes
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
CONFIDENCE = "strict"

# FS_Combas → 目标列
BALANCE_MAP = {
    "A0b1103000": "cash_and_cb_balance",
    "A0b1104000": "due_from_banks",
    "A0b1201000": "loans_and_advances",
    "A0b2102000": "borrowing_from_cb",
    "A0b2103000": "deposits_and_interbank",
    "A0b2103101": "interbank_deposits",
    "A0b2103201": "customer_deposits",
    "A0i1113000": "premiums_receivable",
    "A0i2119000": "insurance_contract_reserve",
    "A0i2118000": "policyholder_deposits",
    "A0d1102000": "settlement_reserve",
    "A0d1102101": "customer_settlement_reserve",
    "A0d1126000": "margin_deposits_paid",
    "A0d2122000": "client_securities_deposits",
    "A0d2123000": "underwriting_securities",
    "A0f1106000": "interbank_lending",
    "A0f1122000": "reverse_repo_assets",
    "A0f2104000": "interbank_borrowing",
    "A0f2110000": "repo_liabilities",
}
# FS_Comins → 目标列
INCOME_MAP = {
    "Bbd1102101": "interest_income",
    "Bbd1102203": "interest_expense",
    "Bbd1102000": "net_interest_income",
    "B0i1103000": "earned_premiums",
    "B0i1203000": "claim_payments_net",
    "B0d1104000": "fee_commission_income_net",
}
VALUE_COLUMNS = list(BALANCE_MAP.values()) + list(INCOME_MAP.values())
COLUMNS = ["stock_code", "report_date", "report_type", *VALUE_COLUMNS,
           "source", "fetch_time", "raw_response_hash", "confidence", "batch_id"]
# 监管比率：CSMAR 与可及端点均不提供，如实登记为不可得
UNAVAILABLE_REGULATORY = (
    "capital_adequacy_ratio", "core_tier1_capital_adequacy_ratio",
    "tier1_capital_adequacy_ratio", "non_performing_loan_ratio",
    "provision_coverage_ratio", "risk_coverage_ratio",
)


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    return df.dropna(subset=["report_date"])


def _load(name: str, folder: str, mapping: dict[str, str]) -> pd.DataFrame:
    path = PACKAGE_DIR / folder / name
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    df = _norm(pd.read_stata(path, columns=["Stkcd", "Accper", *mapping], convert_categoricals=False))
    return df.rename(columns=mapping)


def main() -> None:
    ap = argparse.ArgumentParser(description="导入金融行业专用科目")
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

    financial_codes = {r["stock_code"] for r in duck.read_query(
        """SELECT stock_code FROM stock_meta
           WHERE name LIKE '%银行%' OR name LIKE '%证券%' OR name LIKE '%保险%'
              OR name LIKE '%信托%' OR name LIKE '%金融%' OR name LIKE '%金控%'"""
    )}
    logger.info("金融股 %d 只", len(financial_codes))

    bs = _load("FS_Combas.dta", "资产负债表", BALANCE_MAP)
    ic = _load("FS_Comins.dta", "利润表", INCOME_MAP)
    merged = bs.merge(ic, on=["stock_code", "report_date"], how="outer", suffixes=("", "_ic"))
    merged = merged[merged["stock_code"].isin(financial_codes)].copy()
    for col in VALUE_COLUMNS:
        if col not in merged.columns:
            merged[col] = None
        merged[col] = pd.to_numeric(merged[col], errors="coerce")
    merged["report_type"] = merged["report_date"].map(
        lambda d: {"12": "annual", "06": "semi_annual", "03": "quarterly", "09": "quarterly"}.get(f"{d.month:02d}", "quarterly")
    )
    merged = merged.dropna(subset=[c for c in VALUE_COLUMNS], how="all")
    merged = merged.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    logger.info("金融专用科目: %d 行 / %d 只 / %s ~ %s",
                len(merged), merged["stock_code"].nunique(),
                merged["report_date"].min(), merged["report_date"].max())

    if not args.yes:
        cov = {c: int(merged[c].notna().sum()) for c in VALUE_COLUMNS}
        logger.info("[DRY RUN] 未写库；各列非空数: %s", json.dumps(cov, ensure_ascii=False))
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-finsector-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "financial_sector_items.parquet").replace("'", "''")
        conn.execute(f"COPY financial_sector_items TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex
    src_hash = hashlib.sha256(f"{SOURCE}:financial_sector:{batch_id}".encode()).hexdigest()[:32]
    data = merged[["stock_code", "report_date", "report_type", *VALUE_COLUMNS]].copy()
    data["report_date"] = pd.to_datetime(data["report_date"])
    data["source"] = SOURCE
    data["fetch_time"] = fetched_at
    data["raw_response_hash"] = src_hash
    data["confidence"] = CONFIDENCE
    data["batch_id"] = batch_id
    data = data[COLUMNS]

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register("_fsi", data)
        conn.execute(f"DELETE FROM financial_sector_items WHERE source = '{SOURCE}'")
        conn.execute("INSERT INTO financial_sector_items BY NAME SELECT * FROM _fsi")
        conn.unregister("_fsi")
        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "financial_sector_items", SOURCE, "csmar-c17-fs-sector", fetched_at,
             src_hash, len(data), f"{data['report_date'].min().date()}~{data['report_date'].max().date()}",
             CONFIDENCE],
        )

    evidence = {
        "task": "导入金融行业专用科目（银行/保险/证券/其他金融）",
        "executed_at": datetime.now(UTC).isoformat(),
        "rows": len(data), "stocks": int(data["stock_code"].nunique()),
        "coverage": {c: int(data[c].notna().sum()) for c in VALUE_COLUMNS},
        "unavailable_regulatory_ratios": list(UNAVAILABLE_REGULATORY),
        "not_exposed_to_screening": "金融专用科目只对该行业成立，进全市场字段表会制造伪条件",
        "backup_dir": str(backup), "batch_id": batch_id,
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r9_financial_sector_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写库完成 %d 行；证据 JSON: %s", len(data), ev_path)


if __name__ == "__main__":
    main()
