"""导入 CSMAR FI_T9 每股指标历史（交叉核验源）。

定位
----
本项目已在 `indicator_ext` 自算四个每股指标（bps / revenue_per_share /
ocf_per_share / retained_earnings_per_share，覆盖最新报告期）。
`FI_T9`（每股指标，303,065 行，1990-2025Q1）是 CSMAR 用**其自有股本口径**
独立算出的一套，两条独立路径可互为核对。

本脚本做两件事：
1. 把 FI_T9 的核心每股指标导入 `csmar_per_share_history`（历史域）；
2. **顺带做交叉核验**：把 CSMAR 值与本项目自算值按 (stock_code, report_date)
   逐行比对，输出一致率证据 —— 这是「三色灯」规则里 green 字段的标准用法。

不进筛选界面：与 indicator_ext 每股族语义重叠，同概念两套口径会造成伪选择。

用法
----
    python scripts/import_per_share_history.py            # dry-run
    python scripts/import_per_share_history.py --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
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

FIELD_MAP = {
    "F091001A": "bps",
    "F091701A": "bps_parent",
    "F090501B": "revenue_per_share",
    "F091801B": "ocf_per_share",
    "F090901B": "operating_profit_per_share",
    "F090701B": "ebit_per_share",
    "F091101A": "tangible_asset_per_share",
    "F091201A": "liability_per_share",
    "F091301A": "capital_reserve_per_share",
    "F091401A": "surplus_reserve_per_share",
    "F091501A": "undistributed_profit_per_share",
    "F091601A": "retained_earnings_per_share",
}
VALUE_COLUMNS = list(FIELD_MAP.values())
COLUMNS = ["stock_code", "report_date", *VALUE_COLUMNS, "source", "fetch_time", "batch_id"]
# 与 indicator_ext 的自算列对应关系（用于交叉核验）
CROSS_CHECK = {
    "bps": "bps",
    "revenue_per_share": "revenue_per_share",
    "ocf_per_share": "ocf_per_share",
    "retained_earnings_per_share": "retained_earnings_per_share",
}


def load_csmar() -> pd.DataFrame:
    path = PACKAGE_DIR / "每股指标" / "FI_T9.dta"
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSMAR 源文件: {path}")
    df = pd.read_stata(path, columns=["Stkcd", "Accper", *FIELD_MAP], convert_categoricals=False)
    df["stock_code"] = df["Stkcd"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["report_date"] = pd.to_datetime(df["Accper"], errors="coerce").dt.date
    df = df.dropna(subset=["report_date"])
    df = df.rename(columns=FIELD_MAP)
    for col in VALUE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=VALUE_COLUMNS, how="all")
    df = df.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    logger.info("FI_T9: %d 行 / %d 只 / %s ~ %s",
                len(df), df["stock_code"].nunique(), df["report_date"].min(), df["report_date"].max())
    return df


def cross_check(duck: DuckDBStore, csmar: pd.DataFrame) -> dict:
    """与本项目自算的每股族逐行比对，返回一致率。"""
    ours = duck.read_query(
        "SELECT stock_code, report_date, " + ", ".join(CROSS_CHECK.values()) + " FROM indicator_ext"
    )
    ours_df = pd.DataFrame(ours)
    if ours_df.empty:
        return {"note": "indicator_ext 为空，跳过比对"}
    ours_df["report_date"] = pd.to_datetime(ours_df["report_date"]).dt.date
    merged = ours_df.merge(
        csmar[["stock_code", "report_date", *CROSS_CHECK.keys()]].rename(
            columns={k: f"{k}__csmar" for k in CROSS_CHECK}
        ),
        on=["stock_code", "report_date"], how="inner",
    )
    result: dict = {"compared_rows": int(len(merged)), "fields": {}}
    for ours_col, csmar_col in CROSS_CHECK.items():
        d = merged[[ours_col, f"{csmar_col}__csmar"]].dropna()
        if d.empty:
            result["fields"][ours_col] = {"compared": 0}
            continue
        a = d[ours_col].to_numpy(dtype=float)
        b = d[f"{csmar_col}__csmar"].to_numpy(dtype=float)
        ok = np.isfinite(a) & np.isfinite(b)
        a, b = a[ok], b[ok]
        if a.size == 0:
            result["fields"][ours_col] = {"compared": 0}
            continue
        rel = np.abs(a - b) / np.maximum(np.abs(b), 1e-9)
        result["fields"][ours_col] = {
            "compared": int(a.size),
            "within_0.1pct": round(float((rel <= 0.001).mean() * 100), 2),
            "within_1pct": round(float((rel <= 0.01).mean() * 100), 2),
            "within_5pct": round(float((rel <= 0.05).mean() * 100), 2),
            "median_rel_diff_pct": round(float(np.median(rel) * 100), 4),
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 CSMAR 每股指标历史（含交叉核验）")
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

    csmar = load_csmar()
    check = cross_check(duck, csmar)
    logger.info("交叉核验结果: %s", json.dumps(check, ensure_ascii=False))

    if not args.yes:
        logger.info("[DRY RUN] 未写库")
        return

    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (ev_dir / f"r11_per_share_crosscheck_{stamp}.json").write_text(
        json.dumps({"task": "CSMAR FI_T9 每股指标交叉核验", "result": check},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    batch_id = uuid.uuid4().hex
    data = csmar[["stock_code", "report_date", *VALUE_COLUMNS]].copy()
    data["report_date"] = pd.to_datetime(data["report_date"])
    data["source"] = SOURCE
    data["fetch_time"] = fetched_at
    data["batch_id"] = batch_id
    data = data[COLUMNS]

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register("_ps", data)
        conn.execute(f"DELETE FROM csmar_per_share_history WHERE source = '{SOURCE}'")
        conn.execute("INSERT INTO csmar_per_share_history BY NAME SELECT * FROM _ps")
        conn.unregister("_ps")
        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "csmar_per_share_history", SOURCE, "csmar-c17-fi_t9", fetched_at,
             batch_id[:32], len(data),
             f"{data['report_date'].min().date()}~{data['report_date'].max().date()}", "strict"],
        )
    logger.info("写库完成 %d 行", len(data))


if __name__ == "__main__":
    main()
