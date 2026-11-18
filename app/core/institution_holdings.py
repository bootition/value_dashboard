"""机构持仓域（schema v49 / reports/139 §11.3）—— 「按报告期的机构持股比例」的唯一实现。

## 为什么是这个源（探源结论，见 `.planning/.../out/inst_holdings_probe.md`）

reports/139 §2.2 说「机构持仓」是 8 类外部序列里唯一还能免费拿到的一类，但三条候选路径
实测差别极大：

| 候选 | 实测 | 能否当主源 |
|---|---|---|
| `ak.stock_institute_hold_detail(stock, quarter)` | 有 `持股机构类型/持股数/占流通股比例`，历史到 2005Q4；但**每股只返回 1–4 行**（只有 ETF/社保/QFII 进得了榜），是「部分持有者快照」 | ❌ 会把机构持股总量严重低估 |
| `ak.fund_portfolio_hold_em(symbol, date)` | 按**基金**全量（510050 一年 4 季 = 40 行），要按股票聚合得遍历 ~5,000 只基金 × 每季 | ❌ 成本 ≈5,000 req/季（@2 req/s ≈ 42 分钟/季），且没有「占流通股比例」 |
| `ak.stock_report_fund_hold(symbol, date)` | **全市场 × 1 报告期 = 1 次取全**，6 类持有人；但 akshare 1.18.81 **整表列错位** | ✅ **采用其底层接口** |

**列错位的真实成因（本轮实测，比 accept_final 的「平移一格」更精确）**：东财
`dataapi/zlsj/list` 返回 **22** 个字段，akshare 的 `big_df.columns` 却按 **23** 个名字硬套，
于是从第 3 列起整体左移一格 —— 「股票代码」列拿到的是 `HOLDCHA_VALUE`（持股市值变动额，
2.87e9）、「股票简称」列拿到的是 `SECURITY_CODE`（300750）、「持有基金家数」列拿到的是
`ORG_TYPE`（'01'）。所以**修复成本其实是 0**：直接读底层 22 个具名 JSON 字段即可，
不需要任何平移，还能额外拿到 akshare 丢掉的 `FREESHARES_RATIO`（占流通股比例）
与 `TOTALSHARES_RATIO`（占总股本比例）。

## 口径与单位（D40/D46 同款，唯一换算点）

- `shares` = **股**。源端 `TOTAL_SHARES` 已经是股（300750 2025Q2 基金合计 593,448,886 股），
  **禁止再 ×10000** —— 注意 `stock_institute_hold_detail.持股数` 才是**万股**，
  两个源单位不同，所以本模块只认 `TOTAL_SHARES`，不去碰那个字段。
- `ratio_of_float` = **百分数**（13.95651927 表示占流通股 13.96%，不是 0.1396）。
  源端 `FREESHARES_RATIO` 同为百分数，落库不缩放。
- `holder_count` = 持有该股的该类型**机构家数**（源端 `HOULD_NUM`）。
- **比例缺失不写 0**（D49 同款）：实测 2020Q2 有 12 行 `FREESHARES_RATIO = 0`
  而 `TOTAL_SHARES > 0`（全是当日新上市股，流通股本尚未登记）。持股 1.15 亿却占流通 0%
  是不可能的读数，0 是**源端缺数**的占位值 → 置 NULL 并计 `ratio_zero_filled_null`，
  该行 `confidence` 降级为 `approximate`。下游要「占流通股比例」时必须过滤
  `ratio_of_float IS NOT NULL`，不许把 NULL 当 0 参与加总。

## 写入纪律（D19/D36 / AGENTS R6）

- pandas `connection.register()` + 单条 `INSERT ... SELECT ... ON CONFLICT`，**禁用 executemany**；
- **逐 (报告期 × 持有人类型) 一个独立事务**：先按范围 `DELETE`、再插入、再回读自证。
  一类持有人抓失败不影响其它类（R6 的「逐股/逐期隔离」），也不会留下半张表。
- `replace_scope` 两档（同 `analyst_forecast` 的范式）：
  * `period`（全量抓取）：删掉该 (report_date, holder_type) 的**全部**旧行 —— 源端本期
    掉出覆盖的股票不会留下陈旧行；
  * `codes`（`--limit` / `--stock` 试点）：只删本次真正写入的股票代码，
    **不动**同一报告期其它股票的已有行。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from app.core.analyst_forecast import should_reject_batch
from app.core.storage.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)

__all__ = [
    "HOLDER_TYPES",
    "MAX_RETRIES",
    "REQUEST_SLEEP_SECONDS",
    "SOURCE",
    "TABLE",
    "fetch_holder_period",
    "holdings_evidence",
    "latest_quarter_end",
    "normalize",
    "quarter_end",
    "quarters_back",
    "raw_hash",
    "select_top_stocks",
    "write_holdings",
]

#: 表名（schema v49，DDL 唯一真源：app/core/storage/schema.py）
TABLE = "institution_holdings"
#: 数据源标识（落 `source` 列）。写的是**底层接口**，因为 akshare 包装器列错位。
SOURCE = "eastmoney:zlsj_holder_list"
#: 限速：≤2 req/s（探源报告 §0 的限速纪律；东财 datacenter 口径）
REQUEST_SLEEP_SECONDS = 0.5
#: 失败重试次数（不含首次；≤2 次，AGENTS 硬约束）
MAX_RETRIES = 2
#: 每页行数（源端 pageSize 上限实测 500 可用；11 页 ≈ 5,311 行 = 全市场）
PAGE_SIZE = 500

#: 6 类持有人（源端 `ORG_TYPE` → 落库 `holder_type`；顺序固定，便于日志与证据对齐）
HOLDER_TYPES: tuple[tuple[str, str], ...] = (
    ("1", "基金"),
    ("2", "QFII"),
    ("3", "社保"),
    ("4", "券商"),
    ("5", "保险"),
    ("6", "信托"),
)
_HOLDER_NAME_BY_CODE = dict(HOLDER_TYPES)

#: 东财数据中心「主力数据-机构持仓」列表接口（akshare `stock_report_fund_hold` 的底层）
_URL = "http://data.eastmoney.com/dataapi/zlsj/list"

#: 源端原始字段 → 本域语义列（**按名字取，禁止按位置取**：列数错了才会出现错位那种事故）
_COLUMN_MAP = {
    "stock_code": "SECURITY_CODE",
    "report_date_raw": "REPORT_DATE",
    "holder_type_raw": "ORG_TYPE_NAME",
    "shares": "TOTAL_SHARES",
    "ratio_of_float": "FREESHARES_RATIO",
    "holder_count": "HOULD_NUM",
    "hold_value": "HOLD_VALUE",
}
_CODE_PATTERN = r"^\d{6}$"

_INSERT_SQL = f"""
INSERT INTO {TABLE}
    (stock_code, report_date, holder_type, shares, ratio_of_float, holder_count,
     source, fetch_time, raw_hash, confidence, batch_id)
SELECT stock_code, report_date, holder_type, shares, ratio_of_float, holder_count,
       source, fetch_time, raw_hash, confidence, batch_id
FROM _institution_holdings_rows
ON CONFLICT (stock_code, report_date, holder_type) DO UPDATE SET
    shares         = excluded.shares,
    ratio_of_float = excluded.ratio_of_float,
    holder_count   = excluded.holder_count,
    source         = excluded.source,
    fetch_time     = excluded.fetch_time,
    raw_hash       = excluded.raw_hash,
    confidence     = excluded.confidence,
    batch_id       = excluded.batch_id
"""

_WRITE_COLUMNS = [
    "stock_code", "report_date", "holder_type", "shares", "ratio_of_float",
    "holder_count", "source", "fetch_time", "raw_hash", "confidence", "batch_id",
]


# ── 报告期工具（唯一实现；季度 → 四个季末之一）─────────────────────────────
def quarter_end(year: int, quarter: int) -> date:
    """`(2025, 2) -> date(2025, 6, 30)`。报告期只允许四个季末。"""
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"季度必须是 1..4，实际 {quarter!r}")
    return date(year, 3 * quarter, (31, 30, 30, 31)[quarter - 1])


def quarters_back(latest: date, count: int) -> list[date]:
    """从 `latest` 往前数 `count` 个季末（含 `latest` 本身），按时间升序返回。"""
    if count <= 0:
        return []
    quarter = (latest.month - 1) // 3 + 1
    year = latest.year
    found: list[date] = []
    for _ in range(count):
        found.append(quarter_end(year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return sorted(found)


def latest_quarter_end(today: date) -> date:
    """最近一个**已结束**的季末（北京日期入参；R5 由调用方用 `cn_today()` 给）。"""
    quarter = (today.month - 1) // 3 + 1
    candidate = quarter_end(today.year, quarter)
    if candidate > today:
        quarter -= 1
        if quarter == 0:
            return quarter_end(today.year - 1, 4)
        return quarter_end(today.year, quarter)
    return candidate


# ── 抓取 ────────────────────────────────────────────────────────────────────
def _request_page(type_code: str, report_date: date, page: int) -> dict[str, Any]:
    """取单页原始 JSON（不带重试；重试预算由 `fetch_holder_period` 统一管）。"""
    import requests

    params = {
        "date": report_date.isoformat(),
        "type": type_code,
        "zjc": "0",
        "sortField": "HOULD_NUM",
        "sortDirec": "1",
        "pageNum": str(page),
        "pageSize": str(PAGE_SIZE),
        "p": str(page),
        "pageNo": str(page),
    }
    response = requests.get(_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "data" not in payload:
        raise RuntimeError(f"响应结构漂移：没有 data 字段（前 200 字符={response.text[:200]!r}）")
    return payload


def fetch_holder_period(
    type_code: str,
    report_date: date,
    *,
    sleep_seconds: float = REQUEST_SLEEP_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> pd.DataFrame:
    """抓「某类持有人 × 某报告期」的**全市场**明细（约 11 页 ≈ 5,300 行）。

    抓取失败**不是**「这只股票没人持」——所以这里如实抛错，由调用方把这一
    (报告期 × 类型) 计为 failed 并跳过，不影响其它类型（R6）。
    """
    if type_code not in _HOLDER_NAME_BY_CODE:
        raise ValueError(f"未知持有人类型 {type_code!r}，合法值={sorted(_HOLDER_NAME_BY_CODE)}")
    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        attempts = max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                payload = _request_page(type_code, report_date, page)
                break
            except Exception as error:  # noqa: BLE001 - 源侧任何异常都纳入重试预算
                last_error = error
                logger.warning(
                    "机构持仓抓取失败(%s %s 第 %d 页, 第 %d/%d 次): %s",
                    report_date, _HOLDER_NAME_BY_CODE[type_code], page, attempt, attempts, error,
                )
                if attempt < attempts:
                    time.sleep(sleep_seconds)
        else:
            raise RuntimeError(
                f"机构持仓抓取连续失败 {attempts} 次（type={type_code} "
                f"{report_date} page={page}）: {last_error}"
            ) from last_error
        data = payload.get("data") or []
        rows.extend(data)
        total_pages = int(payload.get("pages") or 1)
        page += 1
        if page <= total_pages:
            time.sleep(sleep_seconds)  # 限速 ≤2 req/s
    if not rows:
        # 空表 ≠ 抓取失败：源端在这个报告期确实没有该类持有人在榜（如早年信托）
        logger.info(
            "机构持仓 %s %s 返回 0 行（源端该期无此类持有人）",
            report_date, _HOLDER_NAME_BY_CODE[type_code],
        )
    return pd.DataFrame.from_records(rows)


def raw_hash(frame: pd.DataFrame) -> str:
    """原始响应指纹（全字段、含被丢弃的行，便于事后复算「那天到底返回了什么」）。"""
    payload = frame.to_json(orient="records", force_ascii=False, date_format="iso")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 规范化 ──────────────────────────────────────────────────────────────────
def normalize(
    frame: pd.DataFrame,
    *,
    report_date: date,
    holder_type: str,
    fetch_time: datetime | None = None,
    batch_id: str | None = None,
    raw_hash_value: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """原始 22 字段 JSON 表 → `institution_holdings` 的列 + 证据。

    Args:
        frame: `fetch_holder_period` 的原始 DataFrame（列名照抄源端）。
        report_date: 本次请求的报告期（用于核对源端 `REPORT_DATE` 未被源端糊弄）。
        holder_type: 本次请求的持有人类型中文名（核实源端 `ORG_TYPE_NAME` 与请求一致）。
        fetch_time: naive UTC 抓取时刻（D19）。
        batch_id / raw_hash_value: 批量与指纹（None = 现算）。

    Returns:
        (rows, stats)。`rows` 的列与 `TABLE` 的 INSERT 列逐一对应；`stats` 是 dry-run
        与写库共用的**同一份**证据（R2：证据必须是真实读数）。
    """
    missing = [source for source in _COLUMN_MAP.values() if source not in frame.columns]
    if missing:
        raise RuntimeError(
            f"接口列名漂移：缺少原始字段 {missing}，实际列={[str(c) for c in frame.columns]}"
        )
    rows_in = int(len(frame))
    stats: dict[str, Any] = {
        "report_date": report_date.isoformat(),
        "holder_type": holder_type,
        "rows_in_response": rows_in,
        "source_columns": [str(column) for column in frame.columns],
    }

    resolved_batch = batch_id or f"institution_holdings_{report_date:%Y%m%d}_{uuid.uuid4().hex[:8]}"
    resolved_hash = raw_hash_value or raw_hash(frame)
    resolved_time = fetch_time or datetime.now(UTC).replace(tzinfo=None)
    stats.update({"batch_id": resolved_batch, "raw_hash": resolved_hash,
                  "fetch_time": resolved_time.isoformat(timespec="seconds")})
    if rows_in == 0:
        raise RuntimeError(
            f"{report_date} 的「{holder_type}」返回 0 行 —— 空结果不写库（不写 0 行占位，"
            "也不把「源端没有」当成「大家都持了 0 股」）"
        )

    working = pd.DataFrame({
        "stock_code": frame[_COLUMN_MAP["stock_code"]].astype("string").str.strip().str.zfill(6),
        "report_date_raw": frame[_COLUMN_MAP["report_date_raw"]].astype("string"),
        "holder_type_raw": frame[_COLUMN_MAP["holder_type_raw"]].astype("string").str.strip(),
        "shares": pd.to_numeric(frame[_COLUMN_MAP["shares"]], errors="coerce"),
        "ratio_of_float": pd.to_numeric(frame[_COLUMN_MAP["ratio_of_float"]], errors="coerce"),
        "holder_count": pd.to_numeric(frame[_COLUMN_MAP["holder_count"]], errors="coerce"),
        "hold_value": pd.to_numeric(frame[_COLUMN_MAP["hold_value"]], errors="coerce"),
    })

    # ① 结构性坏行：股票代码不是 6 位数字（R6 —— 超过阈值整批拒绝，否则只隔离并计数）
    valid_code = working["stock_code"].str.match(_CODE_PATTERN).fillna(False).to_numpy()
    dropped_invalid_code = int((~valid_code).sum())
    dropped_examples = working.loc[~valid_code, "stock_code"].head(5).tolist()
    if should_reject_batch(dropped_invalid_code, rows_in):
        raise RuntimeError(
            f"整批拒绝：{dropped_invalid_code}/{rows_in} 行股票代码非法"
            f"（阈值 max(50, 1%) 或 ≥1/3），疑似源端结构性损坏；样本={dropped_examples}"
        )
    working = working.loc[valid_code].copy()

    # ② 报告期 / 持有人类型必须与请求一致：错位事故的另一种形态（字段串行）
    period_ok = working["report_date_raw"].str.slice(0, 10).eq(report_date.isoformat())
    type_ok = working["holder_type_raw"].eq(holder_type)
    mismatch = ~(period_ok & type_ok)
    dropped_period_mismatch = int(mismatch.sum())
    if should_reject_batch(dropped_period_mismatch, rows_in):
        raise RuntimeError(
            f"整批拒绝：{dropped_period_mismatch}/{rows_in} 行的 REPORT_DATE/ORG_TYPE_NAME "
            f"与请求不符（请求 {report_date} / {holder_type}）—— 疑似源端字段串行"
        )
    working = working.loc[~mismatch].copy()

    # ③ 完全相同载荷的重复行去重；载荷冲突的重复才是真问题（R6 计数）
    payload_columns = ["shares", "ratio_of_float", "holder_count"]
    duplicate_rows = int(working.duplicated("stock_code", keep="first").sum())
    duplicate_conflicts = 0
    if duplicate_rows:
        duplicate_conflicts = int(
            working.groupby("stock_code")[payload_columns]
            .nunique(dropna=False).gt(1).any(axis=1).sum()
        )
        if should_reject_batch(duplicate_conflicts, int(working["stock_code"].nunique())):
            raise RuntimeError(
                f"整批拒绝：{duplicate_conflicts} 只股票的重复行载荷互相冲突"
                "（同一代码两套股数/比例），源端内部不一致，无法判断哪一套是真的"
            )
        working = working.loc[~working.duplicated("stock_code", keep="first")].copy()

    # ④ 比例缺失不写 0（D49）：持股 > 0 而占流通股比例恰为 0 是源端缺数，置 NULL
    ratio_zero = working["ratio_of_float"].eq(0) & working["shares"].gt(0)
    ratio_zero_filled_null = int(ratio_zero.sum())
    working.loc[ratio_zero, "ratio_of_float"] = pd.NA
    working["ratio_of_float"] = pd.to_numeric(working["ratio_of_float"], errors="coerce")
    working["holder_count"] = pd.to_numeric(
        working["holder_count"], errors="coerce"
    ).round().astype("Int64")

    dropped_null_shares = int(working["shares"].isna().sum())
    working = working.loc[working["shares"].notna()].copy()
    if working.empty:
        raise RuntimeError(
            f"{report_date} 的「{holder_type}」规范化后 0 行（股数全空），拒绝写空快照"
        )

    # ⑤ confidence：比例齐全 = exact；只有股数（比例源端缺失）= approximate
    working["confidence"] = working["ratio_of_float"].isna().map(
        {True: "approximate", False: "exact"}
    )
    working["report_date"] = pd.Timestamp(report_date).date()
    working["holder_type"] = holder_type
    working["source"] = SOURCE
    working["fetch_time"] = pd.Timestamp(resolved_time)
    working["raw_hash"] = resolved_hash
    working["batch_id"] = resolved_batch

    # `hold_value` 只用于 `--limit` 排序（按持仓市值取前 N 只），**不进库**；
    # `write_holdings` 落库前只取 `_WRITE_COLUMNS`。
    rows = working[[*_WRITE_COLUMNS, "hold_value"]].sort_values(["stock_code"]).reset_index(drop=True)
    stats.update({
        "rows": int(len(rows)),
        "stocks": int(rows["stock_code"].nunique()),
        "ratio_present": int(rows["ratio_of_float"].notna().sum()),
        "ratio_coverage_pct": round(
            100.0 * int(rows["ratio_of_float"].notna().sum()) / max(1, len(rows)), 2
        ),
        "shares_sum": float(rows["shares"].sum()),
        "duplicate_rows_dropped": duplicate_rows,
        "duplicate_conflicts": duplicate_conflicts,
        "dropped_invalid_code": dropped_invalid_code,
        "dropped_invalid_code_examples": dropped_examples,
        "dropped_period_mismatch": dropped_period_mismatch,
        "dropped_null_shares": dropped_null_shares,
        "ratio_zero_filled_null": ratio_zero_filled_null,
        "confidence_counts": rows["confidence"].value_counts().to_dict(),
    })
    return rows, stats


def select_top_stocks(rows: pd.DataFrame, limit: int) -> pd.DataFrame:
    """`--limit N` 试点：按**持仓市值**从大到小保留前 N 只（0 = 全部）。

    为什么按市值而不是按代码升序：试点要的是「有代表性的大票」，按代码升序会全是
    000xxx 的小市值股，看不出量级对不对（reports/132 I-CALIB：比例指标必须能对上标杆）。
    市值用源端 `HOLD_VALUE`（元），只用于排序，**不进库**。
    """
    if not limit or limit <= 0 or rows.empty:
        return rows
    if "hold_value" in rows.columns:
        ranked = rows.groupby("stock_code", as_index=False)["hold_value"].max()
        keep = ranked.sort_values(["hold_value", "stock_code"], ascending=[False, True])
        codes = keep["stock_code"].head(limit).tolist()
    else:  # pragma: no cover - 防御：调用方已保证 hold_value 存在
        codes = sorted(rows["stock_code"].unique())[:limit]
    return rows.loc[rows["stock_code"].isin(codes)].copy()


# ── 写库 ────────────────────────────────────────────────────────────────────
def write_holdings(
    duck: DuckDBStore,
    rows: pd.DataFrame,
    *,
    report_date: date,
    holder_type: str,
    replace_scope: str = "period",
) -> dict[str, Any]:
    """一个事务内「按范围删旧行 + 单条 INSERT ... SELECT ... ON CONFLICT」（D19/D36）。

    调用方必须已持有单写者锁（CLI 走 `exclusive_update`）。
    用 `DELETE` + `INSERT` 而不是只靠 `ON CONFLICT`：`ON CONFLICT` 清不掉「本期源端
    掉出覆盖」的陈旧行，两者混用会出现同一报告期内新旧混杂的假快照。
    """
    if replace_scope not in {"period", "codes"}:
        raise ValueError(f"未知 replace_scope: {replace_scope!r}")
    if rows.empty:
        raise ValueError("拒绝写入 0 行（空结果不写库，D49）")
    payload = rows.copy()
    deleted = 0
    with duck.transaction() as connection:
        connection.register("_institution_holdings_rows", payload[_WRITE_COLUMNS])
        try:
            if replace_scope == "period":
                deleted = int(
                    connection.execute(
                        f"DELETE FROM {TABLE} WHERE report_date = ? AND holder_type = ?",
                        [report_date, holder_type],
                    ).fetchone()[0]
                )
            else:
                deleted = int(
                    connection.execute(
                        f"DELETE FROM {TABLE} WHERE report_date = ? AND holder_type = ?\n"
                        "  AND stock_code IN (SELECT DISTINCT stock_code"
                        " FROM _institution_holdings_rows)",
                        [report_date, holder_type],
                    ).fetchone()[0]
                )
            connection.execute(_INSERT_SQL)
        finally:
            connection.unregister("_institution_holdings_rows")
    return {
        "report_date": report_date.isoformat(),
        "holder_type": holder_type,
        "rows_written": int(len(payload)),
        "rows_deleted": deleted,
        "replace_scope": replace_scope,
    }


def holdings_evidence(duck: DuckDBStore, report_dates: list[date]) -> dict[str, Any]:
    """回读写入范围的真实分布（写后自证；R2：不采信驱动返回值）。"""
    if not report_dates:
        return {"report_dates": [], "rows": 0, "stocks": 0, "by": []}
    placeholders = ", ".join("?" for _ in report_dates)
    params: list[Any] = [value.isoformat() for value in report_dates]
    totals = duck.read_query(
        f"SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stocks, "
        f"MIN(report_date) AS min_date, MAX(report_date) AS max_date "
        f"FROM {TABLE} WHERE report_date IN ({placeholders})",
        params,
    )
    by_scope = duck.read_query(
        f"SELECT report_date, holder_type, COUNT(*) AS rows, "
        f"COUNT(DISTINCT stock_code) AS stocks, "
        f"SUM(CASE WHEN ratio_of_float IS NOT NULL THEN 1 ELSE 0 END) AS ratio_rows "
        f"FROM {TABLE} WHERE report_date IN ({placeholders}) "
        f"GROUP BY report_date, holder_type ORDER BY report_date, holder_type",
        params,
    )
    total = totals[0] if totals else {}
    return {
        "report_dates": [value.isoformat() for value in report_dates],
        "rows": int(total.get("rows") or 0),
        "stocks": int(total.get("stocks") or 0),
        "min_date": str(total.get("min_date")) if total.get("min_date") else None,
        "max_date": str(total.get("max_date")) if total.get("max_date") else None,
        "by_scope": [
            {
                "report_date": str(row["report_date"]),
                "holder_type": str(row["holder_type"]),
                "rows": int(row["rows"]),
                "stocks": int(row["stocks"]),
                "ratio_rows": int(row["ratio_rows"] or 0),
            }
            for row in by_scope
        ],
    }
