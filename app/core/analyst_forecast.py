"""卖方一致预期（EPS 预测）每日快照（schema v48 / 依据 S3 §2.2 #13）。

## 为什么是「每天一行」而不是回补

`akshare.stock_profit_forecast_em(symbol="")` 一次返回**当期快照**（实测 2,802 行 / 1.71s），
接口没有任何历史参数（探源实测：`.planning/2026-11-18-indicator-feasibility-audit/out/source_probe.md`
§1.B6「一致预期 / EPS 预测 → 能（仅当期快照）」）。「昨天的一致预期」在免费源里根本不存在，
所以这张表的价值只能靠**从今天起每天打点**累积。由此两条硬约束：

1. `snapshot_date` 一律取北京时间的**今天**（`cn_today()`，AGENTS R5），不是 UTC 日期；
2. 允许显式指定其它日期，但**必须**由调用方 `allow_backfill_date=True` 显式确认，
   且 `confidence` 降级为 `approximate` —— 把今天的横截面写成历史日期就是伪造历史，
   不能因为「反正没人看得出」而静默发生（依据 S1 E-STATUS 同款）。

## 单位纪律（D40/D46 同款）

- `eps_forecast` 是**元/股**（茅台 2025E = 65.851755），不是万元、不是每 10 股；
- `analyst_count` 是「研报数」（家/篇），整数；
- 本表没有收益率字段，因此不存在「百分数 vs 小数」歧义。
- EPS 缺失的年度**不写 0**（D49 同款）：0 会被下游 PEG 读成「预期零增长」，
  而「该年度没有卖方覆盖」是 NULL。`eps_forecast IS NULL` 的行直接不落库，
  只计数进证据（`dropped_null_eps`）。

## 源端重复行（2026-11-18 实测）

`stock_profit_forecast_em` 内部把 6 页结果拼起来时**页间重叠**：2,802 行里 442 只股票
各出现 2 次、载荷逐字相同（442/442 组无冲突）。所以「唯一只数」= **2,360**（= 覆盖率
42.3%，探源报告 §1.B6 的 50.3% 是行数口径）。本模块按 `stock_code` 去重并把这 884 行
计入证据；载荷**不一致**的重复才是真冲突（同一代码两套 EPS），按 AGENTS R6 计数、
超阈值整批拒绝。

## 写入纪律（D19/D36）

pandas `connection.register()` + 单条 `INSERT ... SELECT ... ON CONFLICT`，**禁用
executemany**。整批在**一个事务**里：先按范围删旧快照、再插入，所以「同日重跑 = 等价覆盖」，
不是行数翻倍。`replace_scope` 两档：

- `snapshot_date`（全量抓取，`limit=0`）：删掉该 `snapshot_date` 的**全部**旧行再插，
  源端当天掉出覆盖的股票不会留下陈旧行；
- `codes`（`--limit N` 试点）：只删本次写入的股票代码，不动同日其它股票。

## 与自动更新的边界

`refresh_daily()` 是 `update.py::_refresh_analyst_forecast()` 的实现：水位落
`data_refresh_state`（值为北京日期字符串），每日最多 1 次；异常由调用方接住，
只让本步骤 failed/partial，**不影响 readiness**（readiness 只看数据日期，不看 job 步骤状态）。
"""

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，本块登记全部原文小节号）
# S1  reports/133 E-STATUS / R7 —— 状态静默不可见属同类事故；R7 未来函数（用当期一致预期回测历史）。
# S2  reports/138 §3.4 —— dry-run 的全部价值就是写库前能看见真实读数。
# S3  reports/139 §2.2 #13 / source_probe §1.B6 —— schema v48 卖方一致预期每日快照。
# 注：`note`/`task` 等可执行字符串里的 reports/NNN（步骤与任务名的对外文案）按纪律保留。


from __future__ import annotations

import hashlib
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from app.core.financial_period import cn_today
from app.core.storage.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)

__all__ = [
    "REFRESH_INTERVAL_DAYS",
    "REQUEST_SLEEP_SECONDS",
    "SOURCE",
    "STATE_KEY",
    "TABLE",
    "build_snapshot",
    "fetch_forecast",
    "forecast_as_of",
    "normalize",
    "refresh_daily",
    "response_hash",
    "snapshot_evidence",
    "write_snapshot",
]

#: 表名（schema v48，DDL 唯一真源：app/core/storage/schema.py）
TABLE = "analyst_forecast_snapshot"
#: 数据源标识（落 `source` 列，与探源报告逐字一致）
SOURCE = "akshare:stock_profit_forecast_em"
#: `data_refresh_state` 水位键（`update.py::_ANALYST_FORECAST_STATE_KEY` 引用本常量）
STATE_KEY = "analyst_forecast_last_refresh"
#: 低频域刷新间隔：一致预期是「每天一个快照」，间隔恒为 1 天
REFRESH_INTERVAL_DAYS = 1
#: 源侧限速：≤2 req/s（探源报告 §0 的限速纪律）
REQUEST_SLEEP_SECONDS = 0.5
#: 失败重试次数（不含首次；≤2 次，AGENTS 硬约束）
MAX_RETRIES = 2

#: 接口列名（照抄，禁止按列位置取值；列数随年度滚动会变）
_CODE_COLUMN = "代码"
_NAME_COLUMN = "名称"
_COUNT_COLUMN = "研报数"
#: 前瞻年度列形如「2025预测每股收益」：年度必须从列名解析，不能硬编码 2025..2028
_EPS_COLUMN_PATTERN = re.compile(r"^(?P<year>20\d{2})预测每股收益$")
_CODE_PATTERN = re.compile(r"^\d{6}$")

#: 逐股隔离阈值（AGENTS R6）：结构性问题（股票代码非法）达到 max(50, 1%) 或 ≥1/3 才整批拒绝，
#: 否则只跳过坏的股票并计数。
REJECT_DROP_FLOOR = 50
REJECT_DROP_FRACTION = 1 / 3

_INSERT_SQL = f"""
INSERT INTO {TABLE}
    (snapshot_date, stock_code, fiscal_year, eps_forecast, analyst_count,
     source, fetch_time, raw_hash, confidence, batch_id)
SELECT snapshot_date, stock_code, fiscal_year, eps_forecast, analyst_count,
       source, fetch_time, raw_hash, confidence, batch_id
FROM _analyst_forecast_rows
ON CONFLICT (snapshot_date, stock_code, fiscal_year) DO UPDATE SET
    eps_forecast  = excluded.eps_forecast,
    analyst_count = excluded.analyst_count,
    source        = excluded.source,
    fetch_time    = excluded.fetch_time,
    raw_hash      = excluded.raw_hash,
    confidence    = excluded.confidence,
    batch_id      = excluded.batch_id
"""


def fetch_forecast(
    *,
    sleep_seconds: float = REQUEST_SLEEP_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> pd.DataFrame:
    """抓全市场一致预期快照（1 次请求 / 约 2,800 行）。

    限速 ≤2 req/s：重试前 `sleep(sleep_seconds)`；失败重试 ≤`max_retries` 次（默认 2）。
    抓取失败**不是**「该票没数据」（AGENTS R6），所以这里如实抛错，由调用方决定
    是跳过本轮（脚本 exit 4）还是记 `failed` 步骤（自动更新）。
    """
    import akshare as ak  # 延迟导入：update.py 只在真正到这一步时才付 import 成本

    attempts = max_retries + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            frame = ak.stock_profit_forecast_em(symbol="")
            if frame is None or frame.empty:
                raise RuntimeError("stock_profit_forecast_em 返回空表（源不可用，不是「没有覆盖」）")
            logger.info("一致预期抓取成功: %d 行 / %d 列（第 %d 次）", len(frame), len(frame.columns), attempt)
            return frame
        except Exception as error:  # noqa: BLE001 - 源侧任何异常都纳入重试预算
            last_error = error
            logger.warning("一致预期抓取失败(第 %d/%d 次): %s", attempt, attempts, error)
            if attempt < attempts:
                time.sleep(sleep_seconds)
    raise RuntimeError(f"一致预期抓取连续失败 {attempts} 次: {last_error}") from last_error


def response_hash(frame: pd.DataFrame) -> str:
    """原始响应指纹（全 13 列、含被丢弃的行，便于事后复算「那天到底返回了什么」）。"""
    payload = frame.to_json(orient="records", force_ascii=False, date_format="iso")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def should_reject_batch(dropped: int, total: int) -> bool:
    """逐股隔离 vs 整批拒绝的判据（AGENTS R6，唯一实现）。

    `dropped` 只数**结构性**坏行（股票代码不是 6 位数字）——EPS 为空是正常的稀疏，
    不进这个分母。
    """
    if dropped <= 0 or total <= 0:
        return False
    floor = max(REJECT_DROP_FLOOR, math.ceil(0.01 * total))
    return dropped >= floor or dropped >= total * REJECT_DROP_FRACTION


def _fiscal_year_columns(frame: pd.DataFrame) -> list[tuple[str, str]]:
    """从列名解析前瞻年度 → [(fiscal_year_label, column)]，按年度升序。"""
    found: list[tuple[str, str]] = []
    for column in frame.columns:
        match = _EPS_COLUMN_PATTERN.match(str(column))
        if match:
            found.append((f"{match.group('year')}E", str(column)))
    found.sort(key=lambda item: item[0])
    return found


def normalize(
    frame: pd.DataFrame,
    *,
    snapshot_date: date,
    limit: int = 0,
    confidence: str = "exact",
    fetch_time: datetime | None = None,
    batch_id: str | None = None,
    raw_hash: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """把宽表（股票 × 年度 EPS）熔成长表（股票 × 年度）+ 证据。

    Args:
        frame: 接口原始 DataFrame（列名照抄源端）。
        snapshot_date: 快照日（调用方已做「未来日期 / 回填」校验）。
        limit: 只保留按代码升序的前 N 只股票（0 = 全部）；小批试点用。
        confidence: `exact`（当天抓）/ `approximate`（回填日期）。
        fetch_time: naive UTC 抓取时刻（D19）。
        batch_id: 批次号（None = 自动生成）。
        raw_hash: 原始响应 SHA-256（None = 现算）。

    Returns:
        (long_frame, stats)。`long_frame` 的列与 `TABLE` 的 INSERT 列逐一对应；
        `stats` 是 dry-run 与写库共用的**同一份**证据。
    """
    if _CODE_COLUMN not in frame.columns:
        raise RuntimeError(
            f"接口列名漂移：缺少「{_CODE_COLUMN}」列，实际列={list(frame.columns)}"
        )
    eps_columns = _fiscal_year_columns(frame)
    if not eps_columns:
        raise RuntimeError(f"接口列名漂移：没有任何「YYYY预测每股收益」列，实际列={list(frame.columns)}")

    rows_in = int(len(frame))
    codes = frame[_CODE_COLUMN].astype("string").str.strip().str.zfill(6)
    valid_code = codes.str.match(_CODE_PATTERN).fillna(False).to_numpy()
    dropped_invalid_code = int((~valid_code).sum())
    dropped_examples = codes[~valid_code].astype("string").head(5).tolist()
    if should_reject_batch(dropped_invalid_code, rows_in):
        raise RuntimeError(
            f"整批拒绝：{dropped_invalid_code}/{rows_in} 行股票代码非法"
            f"（阈值 max({REJECT_DROP_FLOOR}, 1%) 或 ≥1/3），疑似源端结构性损坏；"
            f"样本={dropped_examples}"
        )

    working = frame.loc[valid_code].copy()
    working["stock_code"] = codes[valid_code].astype(str).to_numpy()
    working["analyst_count"] = pd.to_numeric(
        working.get(_COUNT_COLUMN), errors="coerce"
    ).round().astype("Int64")

    # 源端重复行：akshare 把 6 页结果拼接时**重叠**，实测 2,802 行里 442 只股票各出现 2 次，
    # 且载荷逐字相同（2026-11-18 实测：442/442 组无冲突）。不去重会让同一个
    # (snapshot_date, stock_code, fiscal_year) 写两次 —— ON CONFLICT 折叠后
    # 「计划行数 ≠ 回读行数」，写后自证会把正常写入误判成半截状态。
    # 载荷**不一致**的重复才是真冲突（同一只股票两个 EPS 互相矛盾），按 R6 计数。
    payload_columns = [
        column for column in [_COUNT_COLUMN, *[col for _, col in eps_columns]]
        if column in working.columns
    ]
    duplicate_mask = working.duplicated("stock_code", keep="first")
    duplicate_rows = int(duplicate_mask.sum())
    duplicate_conflicts = 0
    if payload_columns and duplicate_rows:
        duplicate_conflicts = int(
            working.groupby("stock_code")[payload_columns]
            .nunique(dropna=False)
            .gt(1)
            .any(axis=1)
            .sum()
        )
        if should_reject_batch(duplicate_conflicts, int(working["stock_code"].nunique())):
            raise RuntimeError(
                f"整批拒绝：{duplicate_conflicts} 只股票的重复行载荷互相冲突"
                f"（同一代码两套 EPS），源端内部不一致，无法判断哪一套是真的"
            )
        working = working[~duplicate_mask].copy()

    long = working[["stock_code", "analyst_count", *[column for _, column in eps_columns]]].melt(
        id_vars=["stock_code", "analyst_count"],
        value_vars=[column for _, column in eps_columns],
        var_name="column",
        value_name="eps_forecast",
    )
    long["analyst_count"] = pd.to_numeric(long["analyst_count"], errors="coerce").astype("Int64")
    label_by_column = {column: label for label, column in eps_columns}
    long["fiscal_year"] = long["column"].map(label_by_column)
    long["eps_forecast"] = pd.to_numeric(long["eps_forecast"], errors="coerce")

    dropped_null_eps_by_year = {
        label: int(((long["fiscal_year"] == label) & long["eps_forecast"].isna()).sum())
        for label, _ in eps_columns
    }
    long = long[long["eps_forecast"].notna()].copy()

    if limit and limit > 0:
        keep = sorted(long["stock_code"].unique())[:limit]
        long = long[long["stock_code"].isin(keep)].copy()

    if long.empty:
        raise RuntimeError("规范化后 0 行（源端全部年度 EPS 为空），拒绝写空快照")

    resolved_batch = batch_id or f"analyst_forecast_{snapshot_date:%Y%m%d}_{uuid.uuid4().hex[:8]}"
    resolved_hash = raw_hash or response_hash(frame)
    resolved_time = (fetch_time or datetime.now(UTC).replace(tzinfo=None))
    # 标量赋值 / 用 .to_numpy() 而不是 pd.Series(...)：`long` 的索引来自 melt+过滤，
    # 不是 0..n-1，Series 赋值会按索引对齐 → 整列变 NaT → DuckDB NOT NULL 约束失败。
    long["snapshot_date"] = pd.Timestamp(snapshot_date).date()
    long["source"] = SOURCE
    long["fetch_time"] = pd.Timestamp(resolved_time)
    long["raw_hash"] = resolved_hash
    long["confidence"] = confidence
    long["batch_id"] = resolved_batch
    columns = [
        "snapshot_date", "stock_code", "fiscal_year", "eps_forecast", "analyst_count",
        "source", "fetch_time", "raw_hash", "confidence", "batch_id",
    ]
    long = long[columns].sort_values(["stock_code", "fiscal_year"]).reset_index(drop=True)

    stats = {
        "rows_in_response": rows_in,
        "rows_after_dedupe": int(len(working)),
        "rows_long": int(len(long)),
        "stocks": int(long["stock_code"].nunique()),
        "duplicate_rows_dropped": duplicate_rows,
        "duplicate_conflicts": duplicate_conflicts,
        "fiscal_years": [label for label, _ in eps_columns],
        "rows_by_fiscal_year": {
            label: int((long["fiscal_year"] == label).sum()) for label, _ in eps_columns
        },
        "dropped_invalid_code": dropped_invalid_code,
        "dropped_invalid_code_examples": dropped_examples,
        "dropped_null_eps": int(sum(dropped_null_eps_by_year.values())),
        "dropped_null_eps_by_fiscal_year": dropped_null_eps_by_year,
        "limit": int(limit or 0),
        "source_columns": [str(column) for column in frame.columns],
        "batch_id": resolved_batch,
        "raw_hash": resolved_hash,
        "fetch_time": resolved_time.isoformat(timespec="seconds"),
    }
    return long, stats


def write_snapshot(
    duck: DuckDBStore,
    rows: pd.DataFrame,
    *,
    snapshot_date: date,
    replace_scope: str = "snapshot_date",
) -> dict[str, Any]:
    """一个事务内「按范围删旧快照 + 单条 INSERT ... SELECT」写库（D19/D36）。

    调用方必须已经持有单写者锁（CLI 用 `exclusive_update`；自动更新流程本身在锁内）。
    用 `DELETE` + `INSERT` 而不是只 `ON CONFLICT`：`ON CONFLICT` 无法清掉「今天源端
    掉出覆盖」的陈旧行；两者都保留才会出现同一 `snapshot_date` 内新旧混合的假快照。
    """
    if replace_scope not in {"snapshot_date", "codes"}:
        raise ValueError(f"未知 replace_scope: {replace_scope!r}")
    deleted = 0
    with duck.transaction() as connection:
        connection.register("_analyst_forecast_rows", rows)
        try:
            if replace_scope == "snapshot_date":
                deleted = int(
                    connection.execute(
                        f"DELETE FROM {TABLE} WHERE snapshot_date = ?", [snapshot_date]
                    ).fetchone()[0]
                )
            else:
                deleted = int(
                    connection.execute(
                        f"DELETE FROM {TABLE} WHERE snapshot_date = ?\n"
                        "  AND stock_code IN (SELECT DISTINCT stock_code FROM _analyst_forecast_rows)",
                        [snapshot_date],
                    ).fetchone()[0]
                )
            connection.execute(_INSERT_SQL)
        finally:
            connection.unregister("_analyst_forecast_rows")
    return {"rows_written": int(len(rows)), "rows_deleted": deleted, "replace_scope": replace_scope}


def snapshot_evidence(duck: DuckDBStore, snapshot_date: date) -> dict[str, Any]:
    """回读某天的快照分布（写后自证；R2：证据必须是真实读数，不采信驱动返回值）。"""
    total = duck.read_query(
        f"SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stocks "
        f"FROM {TABLE} WHERE snapshot_date = ?",
        [snapshot_date],
    )
    by_year = duck.read_query(
        f"SELECT fiscal_year, COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stocks "
        f"FROM {TABLE} WHERE snapshot_date = ? GROUP BY fiscal_year ORDER BY fiscal_year",
        [snapshot_date],
    )
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "rows": int(total[0]["rows"]) if total else 0,
        "stocks": int(total[0]["stocks"]) if total else 0,
        "by_fiscal_year": [
            {
                "fiscal_year": str(row["fiscal_year"]),
                "rows": int(row["rows"]),
                "stocks": int(row["stocks"]),
            }
            for row in by_year
        ],
    }


def forecast_as_of(
    duck: DuckDBStore,
    *,
    stock_code: str,
    as_of: date,
    max_age_days: int = 30,
) -> dict[str, float] | None:
    """取 `as_of` 当天或之前**最近一次**快照的 EPS 预测（防前视，供 #13 PEG 用）。

    为什么必须 as_of 而不是「最新一次」：一致预期是**每天都在变**的横截面，
    用今天的一致预期去回测 2019 年的 PEG 就是未来函数（依据 S1 R7 的同类）。
    这里按 `snapshot_date <= as_of` 取最近一期，并限制 `max_age_days`：
    隔太久（默认 >30 天）说明中间没打点，返回 None 而不是拿陈旧值冒充。

    返回 `{fiscal_year: eps_forecast}`，键形如 `"2026E"`（只含非空值，缺失年度不补 0，
    见模块 docstring）。
    """
    rows = duck.read_query(
        f"""
        WITH latest AS (
            SELECT MAX(snapshot_date) AS snapshot_date
            FROM {TABLE}
            WHERE stock_code = ? AND snapshot_date <= ?
        )
        SELECT snapshot_date, fiscal_year, eps_forecast
        FROM {TABLE}
        WHERE stock_code = ? AND snapshot_date = (SELECT snapshot_date FROM latest)
          AND eps_forecast IS NOT NULL
        ORDER BY fiscal_year
        """,
        [stock_code, as_of, stock_code],
    )
    if not rows:
        return None
    snapshot_date = rows[0]["snapshot_date"]
    age_days = (as_of - snapshot_date).days
    if age_days > max_age_days:
        logger.info(
            "一致预期快照过旧，按缺失处理: stock=%s snapshot=%s as_of=%s age=%sd > %sd",
            stock_code, snapshot_date, as_of, age_days, max_age_days,
        )
        return None
    return {str(row["fiscal_year"]): float(row["eps_forecast"]) for row in rows}


def build_snapshot(
    duck: DuckDBStore,
    *,
    snapshot_date: date | None = None,
    limit: int = 0,
    dry_run: bool = True,
    allow_backfill_date: bool = False,
    frame: pd.DataFrame | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """抓取 + 规范化 +（非 dry-run 时）写库，返回可核对的 JSON 证据。

    dry-run 也会**真的抓一次**（1.7s）：没有真实抓取结果，「会不会写 2,802 行」就只能靠猜，
    而 dry-run 的全部价值就是让写库前能看见真实读数（依据 S2 §3.4）。
    """
    today = cn_today(now)
    target = snapshot_date or today
    if target > today:
        raise ValueError(f"refuse: snapshot_date={target} 晚于北京时间今天 {today}（不写未来快照）")
    backfill = target != today
    if backfill and not allow_backfill_date:
        raise ValueError(
            f"refuse: snapshot_date={target} 不是北京时间今天 {today}。接口只返回**当期**快照，"
            "把今天的横截面写成历史日期是伪造历史；确实要这么做得加 --allow-backfill-date"
            "（confidence 会降级为 approximate）。"
        )
    confidence = "approximate" if backfill else "exact"

    raw = frame if frame is not None else fetch_forecast()
    raw_hash = response_hash(raw)
    rows, stats = normalize(
        raw,
        snapshot_date=target,
        limit=limit,
        confidence=confidence,
        raw_hash=raw_hash,
    )
    before = snapshot_evidence(duck, target)
    report: dict[str, Any] = {
        "task": "schema v48 一致预期每日快照（reports/139 §2.2 #13 / source_probe §1.B6）",
        "script": "scripts/fetch_analyst_forecast_snapshot.py",
        "mode": "dry_run" if dry_run else "write",
        "dry_run": bool(dry_run),
        "executed_at": datetime.now(UTC).isoformat(),
        "snapshot_date": target.isoformat(),
        "is_backfill": backfill,
        "confidence": confidence,
        "replace_scope": "snapshot_date" if not limit else "codes",
        "before": before,
        "plan": stats,
        "written": None,
        "after": before if dry_run else None,
    }
    if dry_run:
        logger.info(
            "[DRY RUN] 将写 %d 行 / %d 只 / 年度=%s（删旧范围=%s，该日现有 %d 行）",
            stats["rows_long"], stats["stocks"], ",".join(stats["fiscal_years"]),
            report["replace_scope"], before["rows"],
        )
        return report

    written = write_snapshot(
        duck, rows, snapshot_date=target, replace_scope=report["replace_scope"],
    )
    report["written"] = written
    report["after"] = snapshot_evidence(duck, target)
    logger.info(
        "一致预期快照已写库: written=%d / deleted=%d / 该日 %d 行 / %d 只",
        written["rows_written"], written["rows_deleted"],
        report["after"]["rows"], report["after"]["stocks"],
    )
    return report


def refresh_daily(
    duck: DuckDBStore,
    *,
    state_key: str = STATE_KEY,
    read_state: Callable[[str], str | None],
    write_state: Callable[[str, str], None],
    snapshot_date: date | None = None,
    limit: int = 0,
    allow_backfill_date: bool = False,
) -> dict[str, Any]:
    """自动更新入口：每天最多 1 次（`data_refresh_state` 水位 = 北京日期）。

    水位判据是**日期相等**而不是「距今 < N 天」：一致预期是日频横截面，
    「今天已经打过点」与「昨天打过点」必须区分（AGENTS R2：状态由事实派生）。
    """
    started = time.time()
    target = snapshot_date or cn_today()
    step: dict[str, Any] = {
        "status": "skipped",
        "reason": None,
        "snapshot_date": target.isoformat(),
        "rows": 0,
        "stocks": 0,
        "fiscal_years": [],
        "interval_days": REFRESH_INTERVAL_DAYS,
        "limit": int(limit or 0),
        "elapsed_seconds": 0.0,
    }
    stored = (read_state(state_key) or "").strip()
    if stored == target.isoformat():
        step["reason"] = "already_refreshed_today"
        return step
    try:
        report = build_snapshot(
            duck,
            snapshot_date=target,
            limit=limit,
            dry_run=False,
            allow_backfill_date=allow_backfill_date,
        )
        after = report["after"] or {}
        step.update({
            "status": "success",
            "rows": int(after.get("rows") or 0),
            "stocks": int(after.get("stocks") or 0),
            "fiscal_years": [row["fiscal_year"] for row in after.get("by_fiscal_year", [])],
            "rows_by_fiscal_year": {
                row["fiscal_year"]: row["rows"] for row in after.get("by_fiscal_year", [])
            },
            "rows_written": int((report.get("written") or {}).get("rows_written") or 0),
            "batch_id": report["plan"]["batch_id"],
            "raw_hash": report["plan"]["raw_hash"],
        })
        write_state(state_key, target.isoformat())
    except Exception as error:  # noqa: BLE001 - 只影响本步骤（partial），不影响 readiness
        logger.warning("一致预期快照写入失败(非致命): %s", error)
        step["status"] = "failed"
        step["reason"] = f"exception: {type(error).__name__}: {error}"
    finally:
        step["elapsed_seconds"] = round(time.time() - started, 2)
    logger.info(
        "一致预期快照步骤: status=%s rows=%s stocks=%s fiscal_years=%s elapsed_seconds=%s",
        step["status"], step["rows"], step["stocks"], step["fiscal_years"],
        step["elapsed_seconds"],
    )
    return step
