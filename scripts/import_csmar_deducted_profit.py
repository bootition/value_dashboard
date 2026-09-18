"""CSMAR C17 FI_T2 → income_statement.deducted_net_profit 回填（Phase B1, 2026-09-17）

背景（核验证据 .planning/2026-09-17-datapackage-research/PHASE_A_VERIFICATION_REPORT.md）
--------------------------------------------------------------------------------
2026-09-17 对 CSMAR C17 外来数据包的逐行交叉核验发现：

- income_statement.deducted_net_profit 在 2007-01-01 ~ 2025-03-31 区间
  352,702 行中【非空 0 行】（0.00%）；
- 根因是两条路径互相让位：
    1) scripts/import_csmar.py 在导入利润表时显式 `insert_df["deducted_net_profit"] = None`；
    2) scripts/patch_deducted_profit.py 设 CSMAR_CUTOFF="2025-03-31" 并
       `if rd <= CSMAR_CUTOFF: continue`（假设 CSMAR 已覆盖，但实际从未导入）。
- 后果：indicator_snapshot.deducted_profit_yoy / _cagr3 / _cagr5 全表 5,542 行
  均为 NULL，而这三个字段已注册进 DSL 字段表（dsl/ast_nodes.py）、
  筛选引擎（screening/engine.py）、单位元数据（screening/field_units.py）、
  个股详情页与 CLI 导出清单 —— 用户可选中但永远筛不出结果。

CSMAR 侧可用数据：FI_T2（披露财务指标）F020102「归属于上市公司股东的扣除非
经常性损益的净利润」，243,202 行，2007-03-31 ~ 2025-03-31，说明书原文：
「该数据直接来源于上市公司年报的财务摘要」→ 属官方披露值，非二次计算值。
按 (stock_code, report_date) 精确匹配可得 242,548 行。

安全设计（沿用项目维护脚本规范）
--------------------------------
1. 只回填 `deducted_net_profit IS NULL` 的行，绝不覆盖任何已有值；
2. 写库前检查更新锁（any_write_lock_active），有则拒绝；
3. 默认 dry-run，写库需显式 --yes；
4. 写前自动 parquet 备份 income_statement + source_audit；
5. 全程持有 exclusive_update 单写者锁；
6. 写入 source_audit lineage（source='csmar', confidence='strict'），
   并登记一条 fetch_batch，保证「每格数据有出生证明」；
7. 大批量 lineage 走 pandas register + 单条 INSERT SELECT
   （避免 DuckDB 1.5.5 executemany date 参数 ~450KB/行 的事务内存堆积，见 reports/110 D19）。

用法
----
    python scripts/import_csmar_deducted_profit.py            # dry-run，只统计
    python scripts/import_csmar_deducted_profit.py --yes      # 备份后写库
    python scripts/import_csmar_deducted_profit.py --source DIR --yes

写库后需另行重算派生指标：
    vd data compute_indicators     # 重建 indicator_snapshot（含 3 个扣非派生字段）
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

DEFAULT_SOURCE = (
    PROJECT_ROOT / "额外资料" / "C17 a股上市公司财务数据合集（90-25年）" / "原始数据（dta格式）" / "披露财务指标"
)
DTA_NAME = "FI_T2.dta"
SOURCE_NAME = "csmar"
API_VERSION = "csmar-c17-fi_t2"
ADAPTER_VERSION = "csmar-c17-fi_t2/1"
CONFIDENCE = "strict"
FIELD = "deducted_net_profit"
CSMAR_FIELD = "F020102"
FORMULA_NOTE = "直接来源于上市公司年报的财务摘要（CSMAR C17 / FI_T2.F020102）"
WINDOW_START = "2007-01-01"
WINDOW_END = "2025-03-31"

# CSMAR FI_T2 配套字段（本次只回填 F020102 到主链，其余记录到 raw_data 供后续 Phase B 使用）
COMPANION = {
    "F020101": "non_recurring_gain_loss",
    "F020103": "weighted_roe",
    "F020104": "weighted_roe_deducted",
    "F020105": "basic_eps_deducted",
    "F020106": "ocf_per_share",
    "F020107": "bps_parent",
    "F020108": "basic_eps",
    "F020109": "diluted_eps",
}


def _load_fi_t2(source_dir: Path) -> pd.DataFrame:
    dta = source_dir / DTA_NAME
    if not dta.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {dta}")
    df = pd.read_stata(dta, columns=["Stkcd", "Accper", CSMAR_FIELD, *COMPANION], convert_categoricals=False)
    df["stock_code"] = (
        df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    df = df.dropna(subset=["report_date", CSMAR_FIELD])
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    logger.info("CSMAR %s: %d 行有效, %d 只股票, %s ~ %s",
                DTA_NAME, len(df), df["stock_code"].nunique(),
                df["report_date"].min(), df["report_date"].max())
    return df


def _collect_targets(duck: DuckDBStore) -> pd.DataFrame:
    with duck.read_connection() as conn:
        return conn.execute(
            f"""SELECT stock_code, report_date
                FROM income_statement
                WHERE report_date BETWEEN DATE '{WINDOW_START}' AND DATE '{WINDOW_END}'
                  AND deducted_net_profit IS NULL"""
        ).df()


def _build_plan(csmar: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    t = targets.copy()
    t["report_date"] = pd.to_datetime(t["report_date"]).dt.date
    plan = t.merge(
        csmar[["stock_code", "report_date", CSMAR_FIELD]].rename(columns={CSMAR_FIELD: "value"}),
        on=["stock_code", "report_date"], how="inner",
    )
    plan = plan.dropna(subset=["value"])
    logger.info("可回填 %d 行（目标空值行 %d，CSMAR 有效行 %d，匹配率 %.1f%%）",
                len(plan), len(targets), len(csmar), len(plan) / max(len(targets), 1) * 100)
    return plan


def _backup(duck: DuckDBStore, tag: str) -> Path:
    out = PROJECT_ROOT / ".planning" / f"maintenance-backup-{tag}-{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for table in ("income_statement", "source_audit", "fetch_batch"):
            conn.execute(f"COPY {table} TO '{str(out / (table + '.parquet')).replace(chr(39), chr(39)*2)}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 受影响表已备份到 %s", out)
    return out


def _apply(duck: DuckDBStore, paths, plan: pd.DataFrame, backup_dir: Path) -> dict:
    batch_id = uuid.uuid4().hex
    fetch_time = datetime.now(UTC).replace(tzinfo=None)
    stage = f"_stage_csmar_deducted_{uuid.uuid4().hex[:8]}"
    lineage_view = f"_lineage_csmar_deducted_{uuid.uuid4().hex[:8]}"

    frame = plan.copy()
    frame["report_date"] = pd.to_datetime(frame["report_date"])
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["value"])

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
            conn.register(stage, frame)
            try:
                before = conn.execute(
                    f"SELECT COUNT(deducted_net_profit) FROM income_statement "
                    f"WHERE report_date BETWEEN DATE '{WINDOW_START}' AND DATE '{WINDOW_END}'"
                ).fetchone()[0]
                conn.execute(
                    f"""UPDATE income_statement AS t
                        SET deducted_net_profit = s.value
                        FROM {stage} AS s
                        WHERE t.stock_code = s.stock_code
                          AND t.report_date = CAST(s.report_date AS DATE)
                          AND t.deducted_net_profit IS NULL"""
                )
                after = conn.execute(
                    f"SELECT COUNT(deducted_net_profit) FROM income_statement "
                    f"WHERE report_date BETWEEN DATE '{WINDOW_START}' AND DATE '{WINDOW_END}'"
                ).fetchone()[0]

                # 登记采集批次
                conn.execute(
                    """INSERT INTO fetch_batch
                       (batch_id, data_type, source, adapter_version, fetch_time,
                        raw_response_hash, row_count, report_date_range, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [batch_id, "income_statement", SOURCE_NAME, ADAPTER_VERSION, fetch_time,
                     hashlib.sha256(f"{SOURCE_NAME}:{DTA_NAME}:{batch_id}".encode()).hexdigest()[:32],
                     len(frame), f"{WINDOW_START}~{WINDOW_END}", CONFIDENCE],
                )

                # lineage（pandas 向量化写入，避免 D19 executemany 内存病理）
                lin = pd.DataFrame({
                    "stock_code": frame["stock_code"].astype(str),
                    "field_name": FIELD,
                    "report_date": frame["report_date"],
                    "value": frame["value"].astype(float),
                    "source": SOURCE_NAME,
                    "fetch_batch_id": batch_id,
                    "fetch_time": fetch_time,
                    "raw_response_hash": [
                        hashlib.sha256(f"{SOURCE_NAME}|{DTA_NAME}|{c}|{d}".encode()).hexdigest()[:32]
                        for c, d in zip(
                            frame["stock_code"],
                            frame["report_date"].dt.strftime("%Y-%m-%d"),
                            strict=True,
                        )
                    ],
                    "confidence": CONFIDENCE,
                    "reason_code": None,
                    "api_version": API_VERSION,
                    "effective_date": frame["report_date"],
                    "data_version": 1,
                    "formula": FORMULA_NOTE,
                })
                conn.register(lineage_view, lin)
                try:
                    conn.execute(
                        f"""INSERT INTO source_audit
                            (stock_code, field_name, report_date, value, source, fetch_batch_id,
                             fetch_time, raw_response_hash, confidence, reason_code, api_version,
                             effective_date, data_version, formula)
                            SELECT stock_code, field_name, CAST(report_date AS DATE), value, source,
                                   fetch_batch_id, fetch_time, raw_response_hash, confidence,
                                   reason_code, api_version, CAST(effective_date AS DATE),
                                   data_version, formula
                            FROM {lineage_view}"""
                    )
                finally:
                    conn.unregister(lineage_view)
            finally:
                conn.unregister(stage)

    return {"batch_id": batch_id, "rows_written": int(after - before),
            "lineage_rows": int(len(lin)), "before_nonnull": int(before), "after_nonnull": int(after)}


def main() -> None:
    ap = argparse.ArgumentParser(description="CSMAR FI_T2 → income_statement.deducted_net_profit 回填")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="CSMAR FI_T2.dta 所在目录")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到自动更新写锁正在生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    csmar = _load_fi_t2(args.source)
    targets = _collect_targets(duck)
    plan = _build_plan(csmar, targets)

    if not args.yes:
        logger.info("[DRY RUN] 未写库。抽样 5 行：")
        logger.info("\n%s", plan.head(5).to_string(index=False))
        logger.info("确认后加 --yes 执行")
        return

    if plan.empty:
        logger.info("没有可回填的行，退出。")
        return

    backup_dir = _backup(duck, "csmar-deducted-profit")
    result = _apply(duck, paths, plan, backup_dir)
    logger.info("写库完成: %s", result)

    evidence = {
        "task": "Phase B1 CSMAR FI_T2 → income_statement.deducted_net_profit 回填",
        "executed_at": datetime.now(UTC).isoformat(),
        "source_file": str(args.source / DTA_NAME),
        "source_rows": int(len(csmar)),
        "candidate_rows": int(len(plan)),
        "backup_dir": str(backup_dir),
        **result,
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"b1_deducted_profit_import_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)
    logger.info("下一步: vd data compute_indicators  (重算 indicator_snapshot 的 3 个扣非派生字段)")


if __name__ == "__main__":
    main()
