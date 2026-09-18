"""CSMAR C17 Phase B 数据层导入（v24/v25 三域，2026-09-19）

导入目标
--------
1. `cash_flow_indirect`      ← `FS_Comscfi.dta`（间接法现金流量表，29 科目，216,862 行）
2. `cash_flow_activity`      ← `FS_Comscfd.dta`（直接法投资/筹资活动科目，21 列）
3. `financial_report_dates`  ← `FAR_Finidx.dta.Annodt`（年报公布日，74,509 行）

为什么是这三张表
----------------
Phase A 核验（`.planning/2026-09-17-datapackage-research/PHASE_A_VERIFICATION_REPORT.md`）
与公式库比对得出依赖链：

    间接法现金流 → 折旧摊销 ─┐
    直接法资本支出 ──────────┼→ { EBITDA / EBIT、经营杠杆、综合杠杆、企业自由现金流 }
    经营现金流净额 ─────────┘

本项目此前只导入了**直接法的 16 个主干科目**，既没有折旧摊销，也没有资本支出，
因而 FCF 与杠杆族指标一直无法编制。同时 `Annodt` 是本项目实现「当时可见」
（point-in-time）口径的唯一现成来源——PRD §8.1 目前只能声明历史研究
「非当时可见、不用于回测」。

三色灯裁定（config/csmar_field_verdict.json）
--------------------------------------------
- `cash_flow_indirect` / `cash_flow_activity`：green（原始披露科目）→ 直接导入
- `financial_report_dates`：日期事实，不涉及数值口径 → 直接导入

安全设计（沿用项目维护脚本规范）
--------------------------------
1. 幂等：按主键 DELETE + INSERT BY NAME，可重复执行；
2. 写库前检查更新锁；默认 dry-run，写库需 --yes；
3. 写前自动 parquet 备份目标表；
4. 全程 exclusive_update 单写者锁；
5. 每行携带 source/fetch_time/batch_id/confidence；另登记 fetch_batch，
   其 raw_response_hash 记为**源 .dta 文件的 SHA-256**（文件型源的可追溯锚点）；
6. `cash_flow_indirect` 另对关键字段 `cf_from_operating_indirect` 写
   source_audit lineage（该字段可与直接法经营现金流交叉核验）；
   其余列由行内 source/batch_id 提供溯源，不逐格放大 lineage 表。

用法
----
    python scripts/import_csmar_phase_b.py            # dry-run
    python scripts/import_csmar_phase_b.py --yes      # 备份后写库
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
API_VERSION = "csmar-c17-phase-b/1"

# FS_Comscfi（间接法）29 个数据科目 → 本项目标准列名
INDIRECT_MAP: dict[str, str] = {
    "D000101000": "net_profit",
    "D000118000": "credit_impairment_loss",
    "D000117000": "unconfirmed_investment_loss",
    "D000102000": "asset_impairment_provision",
    "D000103000": "fixed_asset_depreciation",
    "D000119000": "investment_property_depreciation",
    "D000120000": "right_of_use_asset_depreciation",
    "D000104000": "intangible_asset_amortization",
    "D000105000": "long_term_prepaid_amortization",
    "D000106000": "disposal_long_term_asset_loss",
    "D000107000": "fixed_asset_scrap_loss",
    "D000108000": "fair_value_change_loss",
    "D000109000": "financial_expense",
    "D000110000": "investment_loss",
    "D000111000": "deferred_tax_asset_decrease",
    "D000112000": "deferred_tax_liability_increase",
    "D000113000": "inventory_decrease",
    "D000114000": "operating_receivable_decrease",
    "D000115000": "operating_payable_increase",
    "D000116000": "other_adjustment",
    "D000100000": "cf_from_operating_indirect",
    "D000201000": "debt_to_capital",
    "D000202000": "convertible_bond_due_within_1y",
    "D000203000": "finance_lease_fixed_assets",
    "D000204000": "cash_ending_balance",
    "D000205000": "cash_beginning_balance",
    "D000206000": "cash_equivalent_ending",
    "D000207000": "cash_equivalent_beginning",
    "D000200000": "cash_equivalent_net_increase",
}
INDIRECT_COLUMNS = ["stock_code", "report_date", "report_type", *INDIRECT_MAP.values(),
                    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id", "raw_data"]

# FS_Comscfd（直接法）中投资/筹资活动科目 → 本项目标准列名。
# 主要为自由现金流提供「资本支出」输入；其余用于分红总额与融资流水交叉核验。
ACTIVITY_MAP: dict[str, str] = {
    "C002006000": "capex",
    "C002001000": "investment_recovered",
    "C002002000": "investment_income_cash",
    "C002003000": "disposal_long_asset_cash",
    "C002004000": "disposal_subsidiary_cash",
    "C002005000": "other_investing_inflow",
    "C002100000": "investing_inflow_total",
    "C002007000": "investment_paid",
    "C002009000": "acquire_subsidiary_cash",
    "C002010000": "other_investing_outflow",
    "C002200000": "investing_outflow_total",
    "C003001000": "equity_investment_received",
    "C003002000": "borrow_received",
    "C003003000": "bond_issued",
    "C003004000": "other_financing_inflow",
    "C003100000": "financing_inflow_total",
    "C003005000": "debt_repaid",
    "C003006000": "dividend_interest_paid",
    "C003007000": "other_financing_outflow",
    "C003200000": "financing_outflow_total",
    "C007000000": "other_cash_effect",
}
ACTIVITY_COLUMNS = ["stock_code", "report_date", "report_type", *ACTIVITY_MAP.values(),
                    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id"]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_key(df: pd.DataFrame) -> pd.DataFrame:
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    return df.dropna(subset=["report_date"])


def _report_type(accper: pd.Series) -> pd.Series:
    month = accper.astype(str).str[5:7]
    return month.map({"12": "annual", "06": "semi_annual", "03": "quarterly", "09": "quarterly"}).fillna("quarterly")


def _load_mapped(relative: str, field_map: dict[str, str], label: str) -> tuple[pd.DataFrame, str]:
    path = PACKAGE_DIR / relative
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    df = _normalize_key(pd.read_stata(path, columns=["Stkcd", "Accper", *field_map], convert_categoricals=False))
    df = df.rename(columns=field_map)  # CSMAR 代码列 → 本项目标准列名
    df["report_type"] = _report_type(df["Accper"])
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    logger.info("%s: %d 行 / %d 只 / %s ~ %s", label, len(df), df["stock_code"].nunique(),
                df["report_date"].min(), df["report_date"].max())
    return df, _sha256_file(path)


def load_indirect() -> tuple[pd.DataFrame, str]:
    return _load_mapped("现金流量表(间接法)/FS_Comscfi.dta", INDIRECT_MAP, "FS_Comscfi(间接法)")


def load_activity() -> tuple[pd.DataFrame, str]:
    return _load_mapped("现金流量表(直接法)/FS_Comscfd.dta", ACTIVITY_MAP, "FS_Comscfd(投资/筹资)")


def load_report_dates() -> tuple[pd.DataFrame, str]:
    path = PACKAGE_DIR / "财务指标文件(年度)" / "FAR_Finidx.dta"
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    df = _normalize_key(pd.read_stata(path, columns=["Stkcd", "Accper", "Annodt"], convert_categoricals=False))
    df["announce_date"] = pd.to_datetime(df["Annodt"], errors="coerce").dt.date
    before = len(df)
    df = df.dropna(subset=["announce_date"])
    df = df[df["announce_date"] >= df["report_date"]]
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    logger.info("FAR_Finidx.Annodt: %d 行有效（丢弃 %d 行缺失/早于报告期）/ %d 只 / %s ~ %s",
                len(df), before - len(df), df["stock_code"].nunique(),
                df["report_date"].min(), df["report_date"].max())
    return df, _sha256_file(path)


def _backup(duck: DuckDBStore, tag: str) -> Path:
    out = PROJECT_ROOT / ".planning" / f"maintenance-backup-{tag}-{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for table in ("cash_flow_indirect", "cash_flow_activity",
                      "financial_report_dates", "fetch_batch"):
            target = str(out / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 已备份到 %s", out)
    return out


def _write(duck: DuckDBStore, paths, indirect: pd.DataFrame, activity: pd.DataFrame,
           dates: pd.DataFrame, hashes: dict[str, str]) -> dict:
    fetch_time = datetime.now(UTC).replace(tzinfo=None)
    batch_ind, batch_act, batch_dts = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex

    def _stamp(df: pd.DataFrame, src_hash: str, batch_id: str, columns: list[str]) -> pd.DataFrame:
        out = df.copy()
        out["report_date"] = pd.to_datetime(out["report_date"])
        out["source"] = SOURCE
        out["fetch_time"] = fetch_time
        out["raw_response_hash"] = src_hash[:32]
        out["confidence"] = CONFIDENCE
        out["batch_id"] = batch_id
        return out[columns]

    indirect = indirect.copy()
    indirect["raw_data"] = "{}"   # 标准列已承载全部 29 科目；原文列留空占位
    ind = _stamp(indirect, hashes["indirect"], batch_ind, INDIRECT_COLUMNS)
    act = _stamp(activity, hashes["activity"], batch_act, ACTIVITY_COLUMNS)

    dts = dates.copy()
    dts["report_date"] = pd.to_datetime(dts["report_date"])
    dts["announce_date"] = pd.to_datetime(dts["announce_date"])
    dts["source"] = SOURCE
    dts["fetch_time"] = fetch_time
    dts["batch_id"] = batch_dts
    dts = dts[["stock_code", "report_date", "announce_date", "source", "fetch_time", "batch_id"]]

    vid = f"_stage_ind_{uuid.uuid4().hex[:8]}"
    vat = f"_stage_act_{uuid.uuid4().hex[:8]}"
    vdt = f"_stage_dts_{uuid.uuid4().hex[:8]}"
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register(vid, ind)
        conn.register(vat, act)
        conn.register(vdt, dts)
        try:
            conn.execute(f"DELETE FROM cash_flow_indirect; INSERT INTO cash_flow_indirect BY NAME SELECT * FROM {vid}")
            conn.execute(f"DELETE FROM cash_flow_activity; INSERT INTO cash_flow_activity BY NAME SELECT * FROM {vat}")
            conn.execute(f"DELETE FROM financial_report_dates; INSERT INTO financial_report_dates BY NAME SELECT * FROM {vdt}")

            for batch_id, data_type, rows, src_hash, frame in (
                (batch_ind, "cash_flow_indirect", len(ind), hashes["indirect"], ind),
                (batch_act, "cash_flow_activity", len(act), hashes["activity"], act),
                (batch_dts, "financial_report_dates", len(dts), hashes["dates"], dts),
            ):
                conn.execute(
                    """INSERT INTO fetch_batch
                       (batch_id, data_type, source, adapter_version, fetch_time,
                        raw_response_hash, row_count, report_date_range, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [batch_id, data_type, SOURCE, API_VERSION, fetch_time, src_hash[:32], rows,
                     f"{frame['report_date'].min()}~{frame['report_date'].max()}", CONFIDENCE],
                )

            # 关键字段 lineage：间接法经营现金流（可与直接法交叉核验）
            lin = ind[["stock_code", "report_date", "cf_from_operating_indirect"]].rename(
                columns={"cf_from_operating_indirect": "value"}
            ).assign(
                field_name="cf_from_operating_indirect",
                source=SOURCE,
                fetch_batch_id=batch_ind,
                fetch_time=fetch_time,
                raw_response_hash=hashes["indirect"][:32],
                confidence=CONFIDENCE,
                api_version=API_VERSION,
                data_version=1,
                formula="间接法现金流量表原始披露值（CSMAR C17 / FS_Comscfi.D000100000）",
            )
            lin["effective_date"] = lin["report_date"]
            lin = lin[["stock_code", "field_name", "report_date", "value", "source", "fetch_batch_id",
                       "fetch_time", "raw_response_hash", "confidence", "api_version",
                       "effective_date", "data_version", "formula"]]
            conn.register("_lin_ind", lin)
            try:
                conn.execute(
                    """INSERT INTO source_audit
                       (stock_code, field_name, report_date, value, source, fetch_batch_id,
                        fetch_time, raw_response_hash, confidence, api_version,
                        effective_date, data_version, formula)
                       SELECT stock_code, field_name, CAST(report_date AS DATE), value, source,
                              fetch_batch_id, fetch_time, raw_response_hash, confidence,
                              api_version, CAST(effective_date AS DATE), data_version, formula
                       FROM _lin_ind"""
                )
            finally:
                conn.unregister("_lin_ind")
        finally:
            conn.unregister(vid)
            conn.unregister(vat)
            conn.unregister(vdt)

    return {"indirect_rows": len(ind), "activity_rows": len(act), "report_date_rows": len(dts),
            "lineage_rows": len(lin), "batch_indirect": batch_ind,
            "batch_activity": batch_act, "batch_dates": batch_dts}


def main() -> None:
    ap = argparse.ArgumentParser(description="CSMAR Phase B 数据层导入（间接法 + 投资筹资 + 年报公布日）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    indirect, h_ind = load_indirect()
    activity, h_act = load_activity()
    dates, h_dts = load_report_dates()

    if not args.yes:
        logger.info("[DRY RUN] 间接法 %d 行 / 投资筹资 %d 行 / 年报公布日 %d 行；未写库。",
                    len(indirect), len(activity), len(dates))
        logger.info("源 .dta SHA-256: FS_Comscfi=%s… FS_Comscfd=%s… FAR_Finidx=%s…",
                    h_ind[:16], h_act[:16], h_dts[:16])
        return

    from app.core.storage.schema import init_duckdb_schema

    logger.info("初始化 schema（确保 v24/v25 各表存在）...")
    init_duckdb_schema(duck)
    backup_dir = _backup(duck, "csmar-phase-b")
    result = _write(duck, paths, indirect, activity, dates,
                    {"indirect": h_ind, "activity": h_act, "dates": h_dts})
    logger.info("写库完成: %s", result)

    evidence = {
        "task": "Phase B 数据层：cash_flow_indirect + cash_flow_activity + financial_report_dates",
        "executed_at": datetime.now(UTC).isoformat(),
        "backup_dir": str(backup_dir),
        "source_sha256": {"FS_Comscfi.dta": h_ind, "FS_Comscfd.dta": h_act, "FAR_Finidx.dta": h_dts},
        **result,
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"b4_phase_b_import_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)


if __name__ == "__main__":
    main()
