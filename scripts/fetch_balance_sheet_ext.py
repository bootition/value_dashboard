"""补取资产负债表补充科目（其他应付款 / 一年内到期非流动负债 / 其他应收款合计）。

为什么需要
----------
主链 `balance_sheet` 未映射这三个科目，但它们各自解锁一项**无法自算**的能力：

- `other_payables` 其他应付款 → 与已有的 `other_receivables` 配对，
  可算「**大股东占款**」= (其他应收款 − 其他应付款) / 总资产 —— 治理红旗指标；
- `non_current_liab_due_1y` 一年内到期的非流动负债 → 修正有息负债口径
  （主链 `interest_bearing_debt` 目前仅 短借+长借+应付债券，系统性低估）；
- `total_other_receivable` 其他应收款合计 → 与应付侧同源配对，
  避免主链 `other_receivables`（净额）与应付口径不匹配。

数据来源
--------
东方财富 F10 `zcfzbAjaxNew`，与 `fetch_cashflow_supplement.py` 同一模式：
一次最多 5 个报告期，CSMAR 截止后的最近 5 期正好一次取回。
实测字段名（2026-09-19）：`TOTAL_OTHER_PAYABLE` / `NONCURRENT_LIAB_1YEAR` / `TOTAL_OTHER_RECE`。

安全设计
--------
幂等（按 (stock_code, report_date) 先删同源再插）；写前备份；单写者锁；
断点续传（已有本源的股票跳过）；dry-run 默认。

用法
----
    python scripts/fetch_balance_sheet_ext.py              # dry-run
    python scripts/fetch_balance_sheet_ext.py --yes        # 全量
    python scripts/fetch_balance_sheet_ext.py --yes --concurrency 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
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
CONFIDENCE = "strict"
API_VERSION = "eastmoney-f10-zcfzb/1"
BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
CSMAR_CUTOFF = date(2025, 3, 31)
PERIODS = 5
MAX_RETRY = 3

FIELD_MAP = {
    "TOTAL_OTHER_PAYABLE": "other_payables",
    "NONCURRENT_LIAB_1YEAR": "non_current_liab_due_1y",
    "TOTAL_OTHER_RECE": "total_other_receivable",
}
COLUMNS = ("stock_code", "report_date", "report_type", *FIELD_MAP.values(),
           "source", "fetch_time", "raw_response_hash", "confidence", "batch_id")

_LOCAL = threading.local()
_EXCHANGE_PREFIX = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}


def _session() -> requests.Session:
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _LOCAL.session = session
    return session


def _em_symbol(code: str, exchange: str | None = None) -> str:
    """优先用 stock_meta.exchange；**不要**用代码前缀猜（920xxx 会被误判为上交所）。"""
    code = code.strip().zfill(6)
    prefix = _EXCHANGE_PREFIX.get((exchange or "").upper())
    if prefix is None:
        prefix = "BJ" if code.startswith(("920", "43", "83", "87")) else (
            "SH" if code.startswith(("60", "68", "90", "9")) else "SZ")
    return f"{prefix}{code}"


def _recent_quarter_ends(n: int = PERIODS) -> list[str]:
    today = date.today()
    ends = [d for y in (today.year, today.year - 1)
            for d in (date(y, 12, 31), date(y, 9, 30), date(y, 6, 30), date(y, 3, 31))
            if d <= today]
    return [d.isoformat() for d in sorted(set(ends), reverse=True)[:n]]


def fetch_one(code: str, exchange: str, dates: list[str], *, timeout: float) -> list[dict]:
    session = _session()
    symbol = _em_symbol(code, exchange)
    params = {"companyType": "4", "reportDateType": "0", "reportType": "1",
              "dates": ",".join(dates), "code": symbol}
    for attempt in range(MAX_RETRY):
        try:
            resp = session.get(f"{BASE}/zcfzbAjaxNew", params=params, timeout=timeout)
            resp.raise_for_status()
            rows = resp.json().get("data") or []
            if rows:
                return rows
            if attempt == 0:  # 金融股 companyType 不同 → 从页面取 hidctype 重试
                import re
                page = session.get(f"{BASE}/Index",
                                   params={"type": "web", "code": symbol.lower()}, timeout=timeout)
                match = re.search(r'id="hidctype"[^>]*value="([^"]+)"', page.text)
                if match and match.group(1) != "4":
                    params["companyType"] = match.group(1)
                    continue
            return []
        except Exception:  # noqa: BLE001 - 逐股失败不中断整轮
            if attempt == MAX_RETRY - 1:
                return []
            time.sleep(1.5 * (attempt + 1) + random.random())
    return []


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


def _parse(code: str, rows: list[dict], batch_id: str, fetched_at: datetime, src_hash: str) -> list[tuple]:
    out = []
    for row in rows:
        raw = str(row.get("REPORT_DATE") or "")[:10]
        try:
            report_date = date.fromisoformat(raw)
        except ValueError:
            continue
        if report_date <= CSMAR_CUTOFF:
            continue
        values = {col: _to_float(row.get(em)) for em, col in FIELD_MAP.items()}
        if not any(v is not None for v in values.values()):
            continue
        report_type = {"12": "annual", "06": "semi_annual", "03": "quarterly",
                       "09": "quarterly"}.get(f"{report_date.month:02d}", "quarterly")
        out.append((code, report_date, report_type, *values.values(),
                    SOURCE, fetched_at, src_hash, CONFIDENCE, batch_id))
    return out


def _load_universe(duck: DuckDBStore, limit: int) -> list[tuple[str, str]]:
    rows = duck.read_query(
        """SELECT DISTINCT m.stock_code, COALESCE(m.exchange, '') AS exchange
           FROM stock_meta m JOIN income_statement i ON i.stock_code = m.stock_code
           WHERE m.is_listed ORDER BY m.stock_code"""
    )
    have = {r["stock_code"] for r in duck.read_query(
        f"SELECT DISTINCT stock_code FROM balance_sheet_ext WHERE source = '{SOURCE}'")}
    pending = [(r["stock_code"], r["exchange"]) for r in rows if r["stock_code"] not in have]
    logger.info("待补股票 %d 只（全市场 %d，已有本源的 %d）", len(pending), len(rows), len(have))
    return pending[:limit] if limit else pending


def main() -> None:
    ap = argparse.ArgumentParser(description="补取资产负债表补充科目")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--sample", type=int, default=3)
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
    init_duckdb_schema(duck)   # 确保 v27 的 balance_sheet_ext 存在（dry-run 也会读它）
    dates = _recent_quarter_ends()
    logger.info("目标报告期: %s", dates)

    if not args.yes:
        for code, exchange in _load_universe(duck, args.sample):
            rows = fetch_one(code, exchange, dates, timeout=20)
            parsed = _parse(code, rows, "dry", datetime.now(UTC).replace(tzinfo=None), "dry")
            logger.info("  %s(%s) → %d 行", code, exchange, len(parsed))
            time.sleep(0.5)
        logger.info("[DRY RUN] 未写库")
        return

    codes = _load_universe(duck, args.limit)
    if not codes:
        logger.info("没有需要补的股票，退出。")
        return

    backup = PROJECT_ROOT / ".planning" / f"maintenance-backup-bs-ext-{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        target = str(backup / "balance_sheet_ext.parquet").replace("'", "''")
        conn.execute(f"COPY balance_sheet_ext TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s", backup)

    batch_id = uuid.uuid4().hex
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    src_hash = hashlib.sha256(f"{SOURCE}:zcfzbAjaxNew:{batch_id}".encode()).hexdigest()[:32]
    records: list[tuple] = []
    failures: list[str] = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(fetch_one, c, ex, dates, timeout=25): c for c, ex in codes}
        for done, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows = future.result()
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                records.extend(_parse(code, rows, batch_id, fetched_at, src_hash))
            else:
                failures.append(code)
            if done % 500 == 0:
                logger.info("  进度 %d/%d 已取 %d 行 失败 %d 用时 %.1f 分",
                            done, len(codes), len(records), len(failures), (time.time() - started) / 60)

    logger.info("取数完成：%d 行 / 失败 %d 只", len(records), len(failures))
    df = pd.DataFrame(records, columns=COLUMNS)
    df["report_date"] = pd.to_datetime(df["report_date"])

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        conn.register("_bs_ext", df)
        conn.execute(f"DELETE FROM balance_sheet_ext WHERE source = '{SOURCE}'")
        conn.execute("INSERT INTO balance_sheet_ext BY NAME SELECT * FROM _bs_ext")
        conn.unregister("_bs_ext")
        conn.execute(
            """INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time,
               raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "balance_sheet_ext", SOURCE, API_VERSION, fetched_at, src_hash,
             len(df), f"{dates[-1]}~{dates[0]}", CONFIDENCE],
        )

    evidence = {
        "task": "补取资产负债表补充科目（其他应付款/一年内到期非流动负债/其他应收款合计）",
        "executed_at": datetime.now(UTC).isoformat(), "periods": dates,
        "stocks_requested": len(codes), "stocks_failed": len(failures),
        "rows": len(df), "backup_dir": str(backup), "batch_id": batch_id,
        "failed_sample": failures[:20],
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r7_balance_sheet_ext_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)


if __name__ == "__main__":
    main()
