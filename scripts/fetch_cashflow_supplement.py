"""补取现金流量表补充资料（折旧摊销）与资本支出，覆盖 CSMAR 截止后的报告期。

为什么要这个脚本
----------------
CSMAR C17 数据截止 2025-03-31，导致 `indicator_ext` 里的
折旧摊销 / 资本支出 / 自由现金流 / FCF率 / EBITDA / 经营杠杆 / 综合杠杆
这 7 列在**最新报告期覆盖率为 0%**，无法上筛选界面（否则就是
「能选中但永远筛不出结果」的死条件，2026-09-17 扣非空洞的同类问题）。

东方财富 F10 财务分析接口提供同一批科目，且更新到最新报告期。实测：
- `xjllbAjaxNew` 每次最多接受 **5 个报告期**（传更多仍只回 5 期）；
- 而 CSMAR 缺的正好是最近 5 期（2025-06-30 ~ 2026-06-30）→ **每只股票 1 次请求**；
- 返回字段名与 CSMAR 科目一一对应（见下方映射）。

取数字段映射（实测确认）
------------------------
折旧摊销族 → `cash_flow_indirect`
    FA_IR_DEPR             固定资产折旧、油气资产折耗、生产性生物资产折旧
    IR_DEPR                投资性房地产折旧及摊销
    USERIGHT_ASSET_AMORTIZE 使用权资产折旧及摊销
    IA_AMORTIZE            无形资产摊销
    LPE_AMORTIZE           长期待摊费用摊销
    ASSET_IMPAIRMENT       资产减值准备
    FAIRVALUE_CHANGE_LOSS  公允价值变动损失
    INVEST_LOSS            投资损失
    DISPOSAL_LONGASSET_LOSS 处置长期资产的损失
    NETCASH_OPERATE        经营活动现金流量净额（用于与主链交叉核验）

资本支出 → `cash_flow_activity`
    CONSTRUCT_LONG_ASSET   购建固定资产、无形资产和其他长期资产支付的现金

纪律
----
- 只写入 `source='eastmoney_f10'`；CSMAR 行保持不动，两者按 (stock, report_date) 共存；
- 只补 CSMAR 未覆盖的报告期（默认 > 2025-03-31），不覆盖已有 CSMAR 行；
- 断点续传：已存在同源最新期的股票直接跳过；
- dry-run 默认，写库需 `--yes`；写前备份，全程单写者锁。

用法
----
    python scripts/fetch_cashflow_supplement.py                    # dry-run，抽样 5 只
    python scripts/fetch_cashflow_supplement.py --limit 500        # 只跑 500 只
    python scripts/fetch_cashflow_supplement.py --yes              # 备份后写库
    python scripts/fetch_cashflow_supplement.py --yes --concurrency 6
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
API_VERSION = "eastmoney-f10-xjllb/1"
BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
# CSMAR 截止期；只补这之后报告期
CSMAR_CUTOFF = date(2025, 3, 31)
PERIODS_PER_REQUEST = 5
MAX_RETRY = 3

# 折旧摊销族 → cash_flow_indirect 列名
INDIRECT_FIELD_MAP: dict[str, str] = {
    "FA_IR_DEPR": "fixed_asset_depreciation",
    "IR_DEPR": "investment_property_depreciation",
    "USERIGHT_ASSET_AMORTIZE": "right_of_use_asset_depreciation",
    "IA_AMORTIZE": "intangible_asset_amortization",
    "LPE_AMORTIZE": "long_term_prepaid_amortization",
    "ASSET_IMPAIRMENT": "asset_impairment_provision",
    "FAIRVALUE_CHANGE_LOSS": "fair_value_change_loss",
    "INVEST_LOSS": "investment_loss",
    "DISPOSAL_LONGASSET_LOSS": "disposal_long_term_asset_loss",
    "NETCASH_OPERATE": "cf_from_operating_indirect",
}
# 资本支出 → cash_flow_activity 列名
ACTIVITY_FIELD_MAP: dict[str, str] = {
    "CONSTRUCT_LONG_ASSET": "capex",
}


_EXCHANGE_PREFIX = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}


def _em_symbol(code: str, exchange: str | None = None) -> str:
    """证券代码 → 东方财富 symbol。

    优先用 stock_meta.exchange 判定市场。**不要用代码前缀猜**：
    北交所新代码 920xxx 以 9 开头，按"9→沪市"的旧规则会被错判为上交所
    （2026-09-19 实测：336 只北交所股票因此取数失败）。
    """
    code = code.strip().zfill(6)
    prefix = _EXCHANGE_PREFIX.get((exchange or "").upper())
    if prefix is None:  # exchange 缺失时的兜底：北交所优先
        if code.startswith(("920", "43", "83", "87")):
            prefix = "BJ"
        elif code.startswith(("60", "68", "90", "9")):
            prefix = "SH"
        else:
            prefix = "SZ"
    return f"{prefix}{code}"


def _recent_quarter_ends(today: date | None = None, n: int = PERIODS_PER_REQUEST) -> list[str]:
    """最近 n 个已过去的季末报告期（含当季，若已过季末）。"""
    today = today or date.today()
    ends: list[date] = []
    year = today.year
    for y in (year, year - 1):
        for m, d in ((12, 31), (9, 30), (6, 30), (3, 31)):
            ends.append(date(y, m, d))
    ends = sorted({e for e in ends if e <= today}, reverse=True)
    return [e.isoformat() for e in ends[:n]]


_LOCAL = threading.local()


def _session() -> requests.Session:
    """每线程一个 Session：requests.Session 不是线程安全的。"""
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _LOCAL.session = session
    return session


def fetch_one(code: str, exchange: str, dates: list[str], *, timeout: float) -> list[dict]:
    """拉取单只股票的现金流量表（累计口径）；失败返回空列表。"""
    session = _session()
    symbol = _em_symbol(code, exchange)
    params = {
        "companyType": "4",           # 通用企业；银行/保险/券商为其它取值，失败时会回退重试
        "reportDateType": "0",
        "reportType": "1",            # 1=按报告期（累计）
        "dates": ",".join(dates),
        "code": symbol,
    }
    for attempt in range(MAX_RETRY):
        try:
            resp = session.get(f"{BASE}/xjllbAjaxNew", params=params, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data") or []
            if rows:
                return rows
            # 空结果可能是 companyType 不对（金融股）→ 抓页面里的 hidctype 重试一次
            if attempt == 0:
                page = session.get(f"{BASE}/Index",
                                   params={"type": "web", "code": symbol.lower()},
                                   timeout=timeout)
                import re
                match = re.search(r'id="hidctype"[^>]*value="([^"]+)"', page.text)
                if match and match.group(1) != "4":
                    params["companyType"] = match.group(1)
                    continue
            return []
        except Exception as error:  # noqa: BLE001 - 逐股失败不得中断整轮
            if attempt == MAX_RETRY - 1:
                logger.debug("取数失败 %s: %s", code, error)
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


def _parse(code: str, rows: list[dict], batch_id: str, fetched_at: datetime, src_hash: str) -> tuple[list, list]:
    indirect_rows, activity_rows = [], []
    for row in rows:
        raw_date = str(row.get("REPORT_DATE") or "")[:10]
        if not raw_date:
            continue
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if report_date <= CSMAR_CUTOFF:
            continue  # 只补 CSMAR 未覆盖的报告期
        month = f"{report_date.month:02d}"
        report_type = {"12": "annual", "06": "semi_annual", "03": "quarterly", "09": "quarterly"}.get(month, "quarterly")

        dep = {col: _to_float(row.get(em)) for em, col in INDIRECT_FIELD_MAP.items()}
        if any(v is not None for v in dep.values()):
            indirect_rows.append((
                code, report_date, report_type,
                *[dep[c] for c in (
                    "fixed_asset_depreciation", "investment_property_depreciation",
                    "right_of_use_asset_depreciation", "intangible_asset_amortization",
                    "long_term_prepaid_amortization", "asset_impairment_provision",
                    "fair_value_change_loss", "investment_loss",
                    "disposal_long_term_asset_loss", "cf_from_operating_indirect",
                )],
                SOURCE, fetched_at, src_hash, CONFIDENCE, batch_id, "{}",
            ))
        capex = _to_float(row.get("CONSTRUCT_LONG_ASSET"))
        if capex is not None:
            activity_rows.append((code, report_date, report_type, capex,
                                  SOURCE, fetched_at, src_hash, CONFIDENCE, batch_id))
    return indirect_rows, activity_rows


INDIRECT_COLS = (
    "stock_code", "report_date", "report_type", "fixed_asset_depreciation",
    "investment_property_depreciation", "right_of_use_asset_depreciation",
    "intangible_asset_amortization", "long_term_prepaid_amortization",
    "asset_impairment_provision", "fair_value_change_loss", "investment_loss",
    "disposal_long_term_asset_loss", "cf_from_operating_indirect",
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id", "raw_data",
)
ACTIVITY_COLS = ("stock_code", "report_date", "report_type", "capex",
                 "source", "fetch_time", "raw_response_hash", "confidence", "batch_id")


def _load_universe(duck: DuckDBStore, limit: int) -> list[tuple[str, str]]:
    """需要补数的股票 (code, exchange)：主链有财务数据、且尚无本源的近期数据。"""
    rows = duck.read_query(
        """SELECT DISTINCT m.stock_code, COALESCE(m.exchange, '') AS exchange
           FROM stock_meta m
           JOIN income_statement i ON i.stock_code = m.stock_code
           WHERE m.is_listed
           ORDER BY m.stock_code"""
    )
    universe = [(r["stock_code"], r["exchange"]) for r in rows]
    have = {r["stock_code"] for r in duck.read_query(
        f"SELECT DISTINCT stock_code FROM cash_flow_activity WHERE source = '{SOURCE}'"
    )}
    pending = [item for item in universe if item[0] not in have]
    logger.info("待补股票 %d 只（全市场 %d，已有本源的 %d）", len(pending), len(universe), len(have))
    return pending[:limit] if limit else pending


def main() -> None:
    ap = argparse.ArgumentParser(description="补取现金流量表补充资料与资本支出（覆盖 CSMAR 截止后的报告期）")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    ap.add_argument("--concurrency", type=int, default=4, help="并发数（源侧限速，建议 ≤6）")
    ap.add_argument("--sample", type=int, default=5, help="dry-run 时抽样只数")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))
    if any_write_lock_active(paths.duckdb_path):
        logger.error("检测到写锁生效，拒绝并发写入。请等待更新结束后重试。")
        raise SystemExit(3)

    duck = DuckDBStore(paths=paths)
    dates = _recent_quarter_ends()
    logger.info("目标报告期（最近 %d 期）: %s", len(dates), dates)

    if not args.yes:
        codes = _load_universe(duck, args.sample)
        for code, exchange in codes:
            rows = fetch_one(code, exchange, dates, timeout=20)
            parsed_ind, parsed_act = _parse(code, rows, "dry", datetime.now(UTC).replace(tzinfo=None), "dry")
            logger.info("  %s → 间接法 %d 行 / 资本支出 %d 行", code, len(parsed_ind), len(parsed_act))
            time.sleep(0.5)
        logger.info("[DRY RUN] 未写库；加 --yes 执行全量（预计 %d 只）", len(_load_universe(duck, 0)))
        return

    codes = _load_universe(duck, args.limit)
    if not codes:
        logger.info("没有需要补的股票，退出。")
        return
    from app.core.storage.schema import init_duckdb_schema
    init_duckdb_schema(duck)

    backup_dir = PROJECT_ROOT / ".planning" / f"maintenance-backup-etf-cashflow-{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for table in ("cash_flow_indirect", "cash_flow_activity"):
            target = str(backup_dir / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] 已备份到 %s", backup_dir)

    batch_id = uuid.uuid4().hex
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    src_hash = hashlib.sha256(f"{SOURCE}:xjllbAjaxNew:{batch_id}".encode()).hexdigest()[:32]

    indirect_rows: list[tuple] = []
    activity_rows: list[tuple] = []
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
            if not rows:
                failures.append(code)
            else:
                ind, act = _parse(code, rows, batch_id, fetched_at, src_hash)
                indirect_rows.extend(ind)
                activity_rows.extend(act)
            if done % 250 == 0:
                elapsed = time.time() - started
                logger.info("  进度 %d/%d  已取 折旧 %d / capex %d  失败 %d  用时 %.1f 分",
                            done, len(codes), len(indirect_rows), len(activity_rows),
                            len(failures), elapsed / 60)

    logger.info("取数完成：折旧摊销 %d 行 / 资本支出 %d 行 / 失败 %d 只", 
                len(indirect_rows), len(activity_rows), len(failures))

    ind_df = pd.DataFrame(indirect_rows, columns=INDIRECT_COLS)
    act_df = pd.DataFrame(activity_rows, columns=ACTIVITY_COLS)
    for frame in (ind_df, act_df):
        frame["report_date"] = pd.to_datetime(frame["report_date"])

    with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
        if not ind_df.empty:
            conn.register("_em_ind", ind_df)
            conn.execute(
                f"DELETE FROM cash_flow_indirect WHERE source = '{SOURCE}' "
                "AND stock_code IN (SELECT DISTINCT stock_code FROM _em_ind)"
            )
            conn.execute("INSERT INTO cash_flow_indirect BY NAME SELECT * FROM _em_ind")
            conn.unregister("_em_ind")
        if not act_df.empty:
            conn.register("_em_act", act_df)
            conn.execute(
                f"DELETE FROM cash_flow_activity WHERE source = '{SOURCE}' "
                "AND stock_code IN (SELECT DISTINCT stock_code FROM _em_act)"
            )
            conn.execute("INSERT INTO cash_flow_activity BY NAME SELECT * FROM _em_act")
            conn.unregister("_em_act")
        conn.execute(
            """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time,
                raw_response_hash, row_count, report_date_range, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "cash_flow_supplement", SOURCE, API_VERSION, fetched_at,
             src_hash, len(ind_df) + len(act_df), f"{dates[-1]}~{dates[0]}", CONFIDENCE],
        )

    evidence = {
        "task": "补取现金流量表补充资料（折旧摊销）与资本支出",
        "executed_at": datetime.now(UTC).isoformat(),
        "periods": dates,
        "stocks_requested": len(codes),
        "stocks_failed": len(failures),
        "indirect_rows": len(ind_df),
        "activity_rows": len(act_df),
        "backup_dir": str(backup_dir),
        "batch_id": batch_id,
        "failed_sample": failures[:20],
    }
    ev_dir = PROJECT_ROOT / ".planning" / "2026-09-17-datapackage-research" / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev_path = ev_dir / f"r5_cashflow_supplement_{datetime.now():%Y%m%d_%H%M%S}.json"
    ev_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("证据 JSON: %s", ev_path)
    logger.info("下一步: vd data compute-extended-indicators 重新构建 indicator_ext")


if __name__ == "__main__":
    main()
