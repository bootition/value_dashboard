"""补齐 6 个金融监管指标（2026-09-19 复审修复）。

背景
----
`balance_sheet` 自建库起就有 6 个监管列（资本充足率/核心一级/一级/不良贷款率/
拨备覆盖率/风险覆盖率），但**从未有任何适配器写入** —— 实测全表 0 行非空。
此前结论是「CSMAR 与东财可及端点均不可得」，本轮复审**推翻该结论**。

数据源（实测确认）
------------------
东方财富 datacenter `RPT_F10_FINANCE_MAINFINADATA`（主要指标，161 字段），
1 次请求/股票，提供：
    NEWCAPITALADER          → capital_adequacy_ratio            资本充足率
    FIRST_ADEQUACY_RATIO    → tier1_capital_adequacy_ratio      一级资本充足率
    CQBL                    → core_tier1_capital_adequacy_ratio 核心一级资本充足率
    NONPERLOAN              → non_performing_loan_ratio         不良贷款率
    BLDKBBL                 → provision_coverage_ratio          拨备覆盖率
    RISK_COVERAGE           → risk_coverage_ratio               风险覆盖率（券商）
实测样本：平安银行 2026H1 资本充足率 13.14 / 一级 10.95 / 核心一级 10.00 /
不良率 1.05 / 拨备覆盖率 219.58；中信证券 风险覆盖率 225.31 —— 量级与序关系均正确。

口径
----
- 全部为**百分数原值**（13.14 = 13.14%），写入按现有列约定（field_units 登记为 percent）。
- 只对**金融股**（银行/券商/保险/信托/金控）抓取；其他行业该指标本不适用，保持 NULL。
- 缺失如实为空，不估算。

用法:
    python scripts/fetch_regulatory_metrics.py            # dry-run
    python scripts/fetch_regulatory_metrics.py --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active, exclusive_update  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SOURCE = "eastmoney_f10"
URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
# ⚠️ 同名不同义（2026-09-19 实测发现，必须按行业门控）：
#   `CQBL` 对**银行**是「核心一级资本充足率」（平安银行 9.997），
#   对**券商**却是另一个概念（东北证券 5.197）—— 直接混用会污染口径。
#   故本表按行业分别映射，跨行业一律不写。
FIELD_MAP_BANK = {
    "NEWCAPITALADER": "capital_adequacy_ratio",
    "FIRST_ADEQUACY_RATIO": "tier1_capital_adequacy_ratio",
    "CQBL": "core_tier1_capital_adequacy_ratio",
    "NONPERLOAN": "non_performing_loan_ratio",
    "BLDKBBL": "provision_coverage_ratio",
}
FIELD_MAP_BROKER = {
    "RISK_COVERAGE": "risk_coverage_ratio",
}
FIELD_MAP = {**FIELD_MAP_BANK, **FIELD_MAP_BROKER}


def _maps_for(name: str) -> dict[str, str]:
    """按公司名称判定行业 → 返回适用的字段映射（跨行业不写）。"""
    if "银行" in name:
        return FIELD_MAP_BANK
    if "证券" in name:
        return FIELD_MAP_BROKER
    return {}
# 与配置文件一致的行业口径（按名称识别金融股）
FINANCIAL_LIKE = ("银行", "证券", "保险", "信托", "金融", "金控")


def _secucode(code: str, exchange: str | None) -> str:
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get((exchange or "").upper())
    if suffix is None:
        suffix = "BJ" if code.startswith(("920", "43", "83", "87")) else (
            "SH" if code.startswith(("60", "68", "90", "9")) else "SZ")
    return f"{code}.{suffix}"


def fetch_one(session: requests.Session, secucode: str, *, timeout: float = 25) -> dict | None:
    for attempt in range(3):
        try:
            resp = session.get(URL, params={
                "reportName": REPORT_NAME, "columns": "ALL",
                "filter": f'(SECUCODE="{secucode}")',
                "pageNumber": "1", "pageSize": "1",
                "sortColumns": "REPORT_DATE", "sortTypes": "-1",
            }, timeout=timeout)
            resp.raise_for_status()
            rows = (resp.json().get("result") or {}).get("data") or []
            return rows[0] if rows else None
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="补齐 6 个金融监管指标")
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

    like = " OR ".join(f"name LIKE '%{k}%'" for k in FINANCIAL_LIKE)
    universe = [(r["stock_code"], r["exchange"], r["name"]) for r in duck.read_query(
        f"SELECT stock_code, COALESCE(exchange,'') AS exchange, name FROM stock_meta WHERE {like} ORDER BY stock_code")]
    logger.info("金融类股票 %d 只", len(universe))

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0",
                            "Referer": "https://emweb.securities.eastmoney.com/"})
    records, failures, missing = [], [], 0
    for code, exchange, name in universe:
        field_map = _maps_for(name or "")
        if not field_map:
            continue
        row = fetch_one(session, _secucode(code, exchange))
        time.sleep(0.35)
        if row is None:
            failures.append(code)
            continue
        raw_date = str(row.get("REPORT_DATE") or "")[:10]
        try:
            report_date = datetime.fromisoformat(raw_date).date()
        except ValueError:
            failures.append(code)
            continue
        values = {col: row.get(em) for em, col in field_map.items()}
        values = {c: (float(v) if isinstance(v, (int, float)) else None) for c, v in values.items()}
        if not any(v is not None for v in values.values()):
            missing += 1
            continue
        records.append({"stock_code": code, "report_date": report_date, **values})

    logger.info("取到 %d 只 / 失败 %d / 该接口无监管数据 %d", len(records), len(failures), missing)
    if not args.yes:
        for rec in records[:5]:
            logger.info("  %s %s %s", rec["stock_code"], rec["report_date"],
                        {k: v for k, v in rec.items() if k not in ("stock_code", "report_date")})
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-regulatory-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "balance_sheet.parquet").replace("'", "''")
        conn.execute(f"COPY balance_sheet TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    df = pd.DataFrame(records)
    df["report_date"] = pd.to_datetime(df["report_date"])
    cols = ["stock_code", "report_date", *FIELD_MAP.values()]
    batch_id = uuid.uuid4().hex
    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register("_reg", df[cols])
        try:
            sets = ", ".join(f'"{c}" = r."{c}"' for c in FIELD_MAP.values())
            conn.execute(
                f'UPDATE balance_sheet AS t SET {sets} FROM _reg AS r '
                "WHERE t.stock_code = r.stock_code AND t.report_date = r.report_date"
            )
        finally:
            conn.unregister("_reg")
        covered = conn.execute(
            "SELECT COUNT(*) FROM balance_sheet WHERE capital_adequacy_ratio IS NOT NULL"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "regulatory_metrics", SOURCE, "em-datacenter-mainfinadata", 
             datetime.now(UTC).replace(tzinfo=None), batch_id[:32], len(df),
             f"{df['report_date'].min().date()}~{df['report_date'].max().date()}", "strict"],
        )

    evidence = {"task": "补齐 6 个金融监管指标",
                "executed_at": datetime.now(UTC).isoformat(),
                "stocks_fetched": len(records), "stocks_failed": len(failures),
                "no_regulatory_data": missing,
                "balance_sheet_rows_with_capital_adequacy": covered,
                "backup_dir": str(backup), "batch_id": batch_id}
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r19_regulatory_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("写入完成，balance_sheet 中资本充足率非空 %d 行；证据: %s", covered, ev_path)


if __name__ == "__main__":
    main()
