"""抓取中债收益率曲线 → `bond_yield_curve`（schema v48）+ 信用利差只读模板。

默认 dry-run；`--yes` 才写正式库，写前检查 `update_lock.any_write_lock_active`，
写窗口用 `exclusive_update` 单写者串行；拿不到锁就**跳过退出**（不排队、不抢锁）。

数据源与口径（唯一真源：`.planning/2026-11-18-indicator-feasibility-audit/out/source_probe.md` §1.B 3 / §5.3）
-----------------------------------------------------------------------------------------
`ak.bond_china_yield(start_date, end_date)` 每个窗口只回 **3 条曲线**：

| 源「曲线名称」（照抄）                     | 落库 `curve_name` | 说明                     |
|--------------------------------------------|-------------------|--------------------------|
| 中债国债收益率曲线                         | `国债`            | 与 `treasury_yield_curve` 交叉验证用 |
| 中债商业银行普通债收益率曲线(AAA)          | `商金债AAA`       | 信用利差分子（10Y）      |
| 中债中短期票据收益率曲线(AAA)              | `中短票AAA`       | 只有 AAA                |

* **没有 AA 曲线**（实测穷举 2026-11 全窗口只有上述 3 条）→ 只能做 AAA 级利差，
  指标元数据里**禁止**再叫「AAA/AA 信用利差」（`reports/139` §5.3 的裁决）。
* 曲线是宽表：`3月/6月/1年/3年/5年/7年/10年/30年` 八列 → 拆成长表
  `(curve_name, curve_date, tenor, yield_pct)`；`tenor` 归一成 `3M/6M/1Y/3Y/5Y/7Y/10Y/30Y`。
* 单位是**百分数**（2.4422 = 2.4422%），**不除以 100**（KB D40/D46 单位纪律）。
* 早年被截断的期限是**源的空值**（2007 年的中短票只有 3M/6M/1Y 有值，10Y 为 NaN）
  → 该单元格**整行不写**，不落 NULL、更不写 0（NULL/0 会被下游当成真实收益率）。

抓取纪律
--------
* 每 **30 天**一段，避免单次窗口过大；窗口不重叠、按时间正序。
* 限速：每次请求后 `time.sleep(--sleep)`，默认 **0.5s（≤2 req/s）**；`--sleep < 0.5` 直接拒绝。
* 失败重试 **≤2 次**（共 3 次尝试），仍失败只记该窗口并继续（R6 逐窗口隔离，禁止一票否决）。
* 抓取与写库**分离**：先在内存里攒完（约 8~9 千行，几十 MB 以内），再抢锁一次性
  `register + INSERT ... SELECT ... ON CONFLICT` 写入（KB D19/D36/D47）。

信用利差（**只读，不建表**）
---------------------------
`spread_AAA_10Y = 商金债AAA(10Y) − 国债(10Y)`，见本文件 `SPREAD_ROW_SQL` / `SPREAD_STATS_SQL`；
`--spread` 直接跑统计（最近 1 年的中位数，单位 bp），`--show-sql` 只打印模板。

用法
----
    python scripts/fetch_bond_yield_curve.py                    # dry-run（仍会真实请求，注意限速）
    python scripts/fetch_bond_yield_curve.py --yes              # 增量写库（默认从库内 max−7天续传）
    python scripts/fetch_bond_yield_curve.py --yes --full       # 1998/2007→今天 全量回填
    python scripts/fetch_bond_yield_curve.py --yes --limit 4    # 试点：只拉前 4 个 30 天窗口
    python scripts/fetch_bond_yield_curve.py --spread           # 只读：AAA−国债 10Y 利差统计
    python scripts/fetch_bond_yield_curve.py --show-sql         # 只打印信用利差 SQL 模板
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import akshare as ak  # noqa: E402

from app.core.financial_period import cn_today  # noqa: E402
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    VdEnv,
    data_root,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    any_write_lock_active,
    exclusive_update,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_bond_yield_curve")

TABLE = "bond_yield_curve"
SOURCE = "akshare:bond_china_yield"
CONFIDENCE = "strict"
COL_CURVE = "曲线名称"
COL_DATE = "日期"

# 源曲线全名 → 落库短名（短名口径 = schema.py bond_yield_curve DDL 注释）
CURVE_MAP: dict[str, str] = {
    "中债国债收益率曲线": "国债",
    "中债商业银行普通债收益率曲线(AAA)": "商金债AAA",
    "中债中短期票据收益率曲线(AAA)": "中短票AAA",
}
# 源期限列名 → 落库 tenor（原样字符串，只做 中文单位→英文单位 归一）
TENOR_MAP: dict[str, str] = {
    "3月": "3M",
    "6月": "6M",
    "1年": "1Y",
    "3年": "3Y",
    "5年": "5Y",
    "7年": "7Y",
    "10年": "10Y",
    "30年": "30Y",
}
OUTPUT_COLUMNS = [
    "curve_name", "curve_date", "tenor", "yield_pct",
    "source", "fetch_time", "raw_hash", "confidence", "batch_id",
]

# 实测：2006 全年窗口返回 0 行，2007-01-04 起有数据 → 默认起点 2007-01-01
DEFAULT_START = date(2007, 1, 1)
WINDOW_DAYS = 30
MIN_SLEEP_S = 0.5  # ≤2 req/s 硬约束
MAX_RETRIES = 2
RESUME_OVERLAP_DAYS = 7

UPSERT_SQL = f"""
INSERT INTO {TABLE}
    (curve_name, curve_date, tenor, yield_pct, source, fetch_time, raw_hash, confidence, batch_id)
SELECT curve_name, curve_date, tenor, yield_pct, source, fetch_time, raw_hash,
       confidence, batch_id
FROM _bond_rows
ON CONFLICT (curve_name, curve_date, tenor) DO UPDATE SET
    yield_pct  = excluded.yield_pct,
    source     = excluded.source,
    fetch_time = excluded.fetch_time,
    raw_hash   = excluded.raw_hash,
    confidence = excluded.confidence,
    batch_id   = excluded.batch_id
"""

# 信用利差：只读 SQL 模板（**不建表/不建视图**，避免派生表与"重建触发"耦合，KB D28/R3）
SPREAD_ROW_SQL = """
-- spread_AAA_10Y = 商金债AAA(10Y) − 国债(10Y)，单位 bp（1bp = 0.01 个百分点）
SELECT a.curve_date,
       a.yield_pct                      AS aaa_10y_pct,
       g.yield_pct                      AS gov_10y_pct,
       (a.yield_pct - g.yield_pct) * 100 AS spread_bp
FROM bond_yield_curve a
JOIN bond_yield_curve g
  ON g.curve_date = a.curve_date
 AND g.curve_name = '国债'
 AND g.tenor      = '10Y'
WHERE a.curve_name = '商金债AAA'
  AND a.tenor      = '10Y'
"""

SPREAD_STATS_SQL = f"""
WITH spread AS ({SPREAD_ROW_SQL})
SELECT COUNT(*)                          AS n_days,
       ROUND(MEDIAN(spread_bp), 2)       AS median_bp,
       ROUND(MIN(spread_bp), 2)          AS min_bp,
       ROUND(MAX(spread_bp), 2)          AS max_bp,
       MIN(curve_date)                   AS date_from,
       MAX(curve_date)                   AS date_to
FROM spread
WHERE curve_date >= (SELECT MAX(curve_date) FROM bond_yield_curve) - INTERVAL 365 DAY
"""

# 覆盖率交叉验证：bond 的国债 10Y vs 库内 treasury_yield_curve(tenor_years=10)
CROSSCHECK_SQL = """
SELECT COUNT(*)                                               AS n_days,
       ROUND(MEDIAN(ABS(b.yield_pct - t.yield_pct)) * 100, 2) AS median_abs_bp,
       ROUND(MAX(ABS(b.yield_pct - t.yield_pct)) * 100, 2)    AS max_abs_bp,
       SUM(CASE WHEN ABS(b.yield_pct - t.yield_pct) * 100 < 5 THEN 1 ELSE 0 END) AS within_5bp,
       MIN(b.curve_date)                                      AS date_from,
       MAX(b.curve_date)                                      AS date_to
FROM bond_yield_curve b
JOIN treasury_yield_curve t
  ON t.curve_date = b.curve_date
 AND t.tenor_years = 10.0
WHERE b.curve_name = '国债'
  AND b.tenor      = '10Y'
"""


# --------------------------------------------------------------------------- 路径 / 锁
def _is_inside(child: Path, parent: Path) -> bool:
    child_str = os.path.normcase(str(Path(child).resolve())).rstrip("\\/")
    parent_str = os.path.normcase(str(Path(parent).resolve())).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def resolve_target_paths(explicit_db: str | None = None) -> DatabasePathSet:
    """解析目标库：不给 --db 只能走正式档位；--db 仅 test/staging 演练。"""
    if explicit_db is None:
        return require_formal_maintenance_paths()
    env = os.environ.get("VD_ENV", "")
    if env not in {VdEnv.TEST.value, VdEnv.STAGING.value}:
        raise PathIsolationError(
            "--db 只允许在 VD_ENV=test/staging 下使用（演练/回归）；"
            "正式库请省略 --db，由 require_formal_maintenance_paths() 解析"
        )
    candidate = Path(explicit_db).expanduser().resolve()
    if _is_inside(candidate, data_root()):
        raise PathIsolationError(f"--db 不允许指向项目 data/ 下的文件: {candidate}")
    if candidate.name != "valuedashboard.duckdb":
        raise PathIsolationError(f"--db 必须指向 valuedashboard.duckdb: {candidate}")
    if not candidate.is_file():
        raise PathIsolationError(f"--db 目标不存在（先 init schema 建库）: {candidate}")
    return DatabasePathSet(
        env=VdEnv(env),
        duckdb_path=candidate,
        sqlite_path=candidate.with_name("valuedashboard.sqlite"),
        run_root=candidate.parent,
    ).validate()


# --------------------------------------------------------------------------- 抓取
def iter_windows(start: date, end: date, window_days: int) -> Iterator[tuple[date, date]]:
    """把 [start, end] 切成互不重叠的 window_days 天窗口。"""
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=window_days - 1), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def row_digest(values: list[str]) -> str:
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def fetch_window(
    start: date, end: date, *, sleep_s: float, max_retries: int
) -> tuple[pd.DataFrame | None, str | None]:
    """拉一个窗口；失败重试 ≤max_retries 次，最终失败返回 (None, 错误串)。"""
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = ak.bond_china_yield(
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
            )
            return raw, None
        except Exception as error:  # noqa: BLE001 - 逐窗口隔离，重试后仍失败只记录
            last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "窗口 %s~%s 第 %d/%d 次失败：%s",
                start, end, attempt + 1, max_retries + 1, last_error,
            )
        finally:
            time.sleep(sleep_s)  # 限速：请求之间恒 sleep（含重试）
    return None, last_error


def melt_window(
    raw: pd.DataFrame, *, fetch_time: pd.Timestamp, batch_id: str
) -> tuple[pd.DataFrame, list[str]]:
    """宽表（3 曲线 × 8 期限）→ 长表；NaN 单元格整行丢弃。"""
    warnings: list[str] = []
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), warnings
    missing = [col for col in (COL_CURVE, COL_DATE) if col not in raw.columns]
    if missing:
        warnings.append(f"窗口缺少标识列 {missing}（实际列={list(raw.columns)}）")
        return pd.DataFrame(columns=OUTPUT_COLUMNS), warnings

    tenor_cols = [col for col in raw.columns if col in TENOR_MAP]
    unknown = [col for col in raw.columns if col not in TENOR_MAP and col not in (COL_CURVE, COL_DATE)]
    if unknown:
        warnings.append(f"未登记的期限列，已跳过：{unknown}")
    if not tenor_cols:
        warnings.append(f"窗口无可用期限列（实际列={list(raw.columns)}）")
        return pd.DataFrame(columns=OUTPUT_COLUMNS), warnings

    melted = raw.melt(
        id_vars=[COL_CURVE, COL_DATE],
        value_vars=tenor_cols,
        var_name="_tenor_cn",
        value_name="yield_pct",
    )
    melted["yield_pct"] = pd.to_numeric(melted["yield_pct"], errors="coerce")
    dropped = int(melted["yield_pct"].isna().sum())
    melted = melted[melted["yield_pct"].notna()].copy()
    if dropped:
        logger.debug("窗口丢弃空收益率单元格 %d 个（源本来就没有该期限）", dropped)

    mapped = melted[COL_CURVE].map(CURVE_MAP)
    unknown_curves = sorted(set(melted.loc[mapped.isna(), COL_CURVE].astype(str)))
    if unknown_curves:
        warnings.append(f"未登记的曲线（按源名原样落库）：{unknown_curves}")
    melted["curve_name"] = mapped.fillna(melted[COL_CURVE]).astype(str)
    melted["curve_date"] = pd.to_datetime(melted[COL_DATE]).dt.date
    melted["tenor"] = melted["_tenor_cn"].map(TENOR_MAP)
    melted["source"] = SOURCE
    melted["fetch_time"] = fetch_time
    melted["confidence"] = CONFIDENCE
    melted["batch_id"] = batch_id
    melted["raw_hash"] = [
        row_digest([str(row[COL_CURVE]), str(row["curve_date"]), str(row["_tenor_cn"]),
                    f"{row['yield_pct']!r}"])
        for _, row in melted.iterrows()
    ]
    return melted[OUTPUT_COLUMNS], warnings


def fetch_range(
    start: date, end: date, *, sleep_s: float, limit: int
) -> dict[str, Any]:
    """按窗口顺序抓取；逐窗口隔离失败，返回帧 + 统计。"""
    windows = list(iter_windows(start, end, WINDOW_DAYS))
    if limit and limit > 0:
        windows = windows[:limit]
    batch_id = f"bond-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    fetch_time = pd.Timestamp.now(tz="UTC").tz_localize(None)  # naive UTC（KB D19）

    frames: list[pd.DataFrame] = []
    failed: list[dict[str, str]] = []
    warnings: list[str] = []
    requests = 0
    started = time.monotonic()
    for index, (window_start, window_end) in enumerate(windows, start=1):
        raw, error = fetch_window(
            window_start, window_end, sleep_s=sleep_s, max_retries=MAX_RETRIES
        )
        requests += 1
        if error is not None:
            failed.append({"window": f"{window_start}~{window_end}", "error": error})
        else:
            frame, window_warnings = melt_window(raw, fetch_time=fetch_time, batch_id=batch_id)
            warnings.extend(f"{window_start}~{window_end}: {item}" for item in window_warnings)
            if not frame.empty:
                frames.append(frame)
        if index % 20 == 0 or index == len(windows):
            elapsed = time.monotonic() - started
            logger.info(
                "进度 %d/%d 窗口  已攒 %d 行  失败 %d  用时 %.0fs",
                index, len(windows), sum(len(item) for item in frames), len(failed), elapsed,
            )

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=OUTPUT_COLUMNS)
    if not combined.empty:
        combined = combined.drop_duplicates(
            subset=["curve_name", "curve_date", "tenor"], keep="last"
        ).reset_index(drop=True)
    return {
        "frame": combined,
        "windows": len(windows),
        "requests": requests,
        "failed": failed,
        "warnings": warnings,
        "batch_id": batch_id,
        "elapsed_s": round(time.monotonic() - started, 1),
        "start": start,
        "end": end,
    }


# --------------------------------------------------------------------------- 写库 / 统计
def existing_stats(duck: DuckDBStore) -> dict[str, Any]:
    try:
        rows = duck.read_query(
            f"""SELECT COUNT(*) AS n, MIN(curve_date) AS d0, MAX(curve_date) AS d1
                FROM {TABLE}"""
        )
        curves = duck.read_query(
            f"SELECT curve_name, COUNT(*) AS n FROM {TABLE} GROUP BY 1 ORDER BY 1"
        )
    except Exception as error:  # noqa: BLE001 - 未迁移的库应可 dry-run
        logger.warning("%s 不可读（可能未建表，按空处理）: %s", TABLE, error)
        return {"rows": 0, "date_min": None, "date_max": None, "curves": {}}
    row = rows[0] if rows else {}
    return {
        "rows": int(row.get("n") or 0),
        "date_min": row.get("d0"),
        "date_max": row.get("d1"),
        "curves": {str(item["curve_name"]): int(item["n"]) for item in curves},
    }


def write_rows(duck: DuckDBStore, frame: pd.DataFrame) -> int:
    payload = frame[OUTPUT_COLUMNS].copy()
    payload["curve_date"] = pd.to_datetime(payload["curve_date"]).dt.date
    payload["fetch_time"] = pd.to_datetime(payload["fetch_time"])
    payload["yield_pct"] = pd.to_numeric(payload["yield_pct"], errors="coerce").astype("float64")
    with duck.write_connection() as conn:
        conn.execute("SET preserve_insertion_order=false")
        conn.register("_bond_rows", payload)
        try:
            conn.execute(UPSERT_SQL)
        finally:
            conn.unregister("_bond_rows")
    return int(len(payload))


def crosscheck(duck: DuckDBStore) -> dict[str, Any]:
    try:
        rows = duck.read_query(CROSSCHECK_SQL)
    except Exception as error:  # noqa: BLE001 - 交叉验证失败不阻塞主流程
        return {"error": f"{type(error).__name__}: {error}"}
    row = dict(rows[0]) if rows else {}
    n_days = int(row.get("n_days") or 0)
    within = int(row.get("within_5bp") or 0)
    row["within_5bp_pct"] = round(100.0 * within / n_days, 2) if n_days else None
    return row


def spread_stats(duck: DuckDBStore) -> dict[str, Any]:
    try:
        rows = duck.read_query(SPREAD_STATS_SQL)
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {error}"}
    return dict(rows[0]) if rows else {}


def spot_checks(duck: DuckDBStore) -> list[dict[str, Any]]:
    try:
        latest = duck.read_query(f"SELECT MAX(curve_date) AS d FROM {TABLE}")
        day = latest[0]["d"] if latest else None
        if day is None:
            return []
        rows = duck.read_query(
            f"""SELECT curve_name, curve_date, tenor, yield_pct
                FROM {TABLE} WHERE curve_date = ? AND tenor IN ('10Y', '1Y')
                ORDER BY curve_name, tenor""",
            [day],
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("抽查失败: %s", error)
        return []
    return [dict(row) for row in rows]


def plan_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if frame.empty:
        return rows
    for (curve, tenor), group in frame.groupby(["curve_name", "tenor"], sort=True):
        rows.append(
            {
                "curve_name": curve,
                "tenor": tenor,
                "rows": int(len(group)),
                "date_min": group["curve_date"].min().isoformat(),
                "date_max": group["curve_date"].max().isoformat(),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取中债收益率曲线（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="确认写库（默认只打印计划）")
    parser.add_argument("--start-date", default=None, help="起点 YYYY-MM-DD（默认 2007-01-01）")
    parser.add_argument("--end-date", default=None, help="终点 YYYY-MM-DD（默认今天，北京时间）")
    parser.add_argument("--full", action="store_true", help="忽略库内最新日期，从 --start-date 全量回填")
    parser.add_argument("--limit", type=int, default=0, help="只拉前 N 个 30 天窗口（试点）")
    parser.add_argument(
        "--sleep", type=float, default=MIN_SLEEP_S,
        help=f"每次请求后的等待秒数，下限 {MIN_SLEEP_S}（≤2 req/s 硬约束）",
    )
    parser.add_argument("--db", default=None, help="仅 VD_ENV=test/staging 下可用的演练库路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--spread", action="store_true", help="只跑信用利差统计（只读，不抓取）")
    parser.add_argument("--show-sql", action="store_true", help="只打印信用利差 SQL 模板")
    args = parser.parse_args(argv)

    if args.sleep < MIN_SLEEP_S:
        print(
            f"[ABORT] --sleep 不得小于 {MIN_SLEEP_S}s（抓取限速 ≤2 req/s 是硬约束）",
            file=sys.stderr,
        )
        return 2
    if args.show_sql:
        print("-- 信用利差逐日序列（只读模板，不建表）")
        print(SPREAD_ROW_SQL.strip())
        print("\n-- 最近 1 年中位数（bp）")
        print(SPREAD_STATS_SQL.strip())
        return 0

    try:
        paths = resolve_target_paths(args.db)
    except PathIsolationError as error:
        print(f"[ABORT] 无法解析目标库路径：{error}", file=sys.stderr)
        return 2
    duck = DuckDBStore(paths=paths)

    if args.spread:
        stats = spread_stats(duck)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"spread_AAA_10Y（商金债AAA 10Y − 国债 10Y，最近 1 年，单位 bp）: {stats}")
        return 0

    write = bool(args.yes)
    if write and any_write_lock_active(paths.duckdb_path):
        print(
            "[SKIP] 检测到写锁（自动更新或其它维护进程正在写库）；单写者串行："
            "本脚本不排队、不抢锁，直接跳过退出。请稍后重跑。",
            file=sys.stderr,
        )
        return 3

    end_date = date.fromisoformat(args.end_date) if args.end_date else cn_today()
    start_date = date.fromisoformat(args.start_date) if args.start_date else DEFAULT_START
    before = existing_stats(duck)
    if not args.full and before["date_max"] is not None and args.start_date is None:
        resumed = before["date_max"] - timedelta(days=RESUME_OVERLAP_DAYS)
        start_date = max(start_date, resumed)
    if start_date > end_date:
        print(f"[ABORT] --start-date({start_date}) 晚于 --end-date({end_date})", file=sys.stderr)
        return 2

    windows = len(list(iter_windows(start_date, end_date, WINDOW_DAYS)))
    if args.limit and args.limit > 0:
        windows = min(windows, args.limit)
    est_seconds = windows * (1.2 + args.sleep)
    print(
        f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}\n"
        f"区间: {start_date} ~ {end_date}  窗口: {windows} 个 × {WINDOW_DAYS} 天  "
        f"预计用时 ≈ {est_seconds / 60:.1f} 分钟（含 {args.sleep}s 限速）\n"
        f"库内现状: {before['rows']} 行  {before['date_min']} ~ {before['date_max']}  "
        f"曲线={before['curves']}"
    )

    result = fetch_range(start_date, end_date, sleep_s=args.sleep, limit=args.limit)
    frame: pd.DataFrame = result["frame"]
    written = 0
    if write and not frame.empty:
        try:
            with exclusive_update(paths.duckdb_path):
                written = write_rows(duck, frame)
        except UpdateLockError as error:
            print(f"[SKIP] 另一个更新进程持有写锁：{error}", file=sys.stderr)
            return 3
        except Exception as error:  # noqa: BLE001 - 写失败必须显式报出
            logger.error("写库失败：%s: %s", type(error).__name__, error)
            return 4

    after = existing_stats(duck)
    check = crosscheck(duck)
    spread = spread_stats(duck)
    probes = spot_checks(duck)

    payload = {
        "task": "抓取中债收益率曲线 → bond_yield_curve",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": not write,
        "database": str(paths.duckdb_path),
        "batch_id": result["batch_id"],
        "request_range": [str(result["start"]), str(result["end"])],
        "windows": result["windows"],
        "requests": result["requests"],
        "elapsed_s": result["elapsed_s"],
        "failed_windows": result["failed"],
        "warnings": result["warnings"][:20],
        "rows_planned": int(len(frame)),
        "rows_written": written,
        "plan": plan_rows(frame),
        "before": before,
        "after": after,
        "crosscheck_treasury_10y": check,
        "spread_aaa_10y_last_1y": spread,
        "spot_checks": probes,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print(
        f"窗口 {result['windows']} 个 / 请求 {result['requests']} 次 / 用时 {result['elapsed_s']}s / "
        f"失败 {len(result['failed'])} 窗口 / 待写 {len(frame)} 行 / 实际写入 {written} 行"
    )
    for item in result["failed"][:10]:
        print(f"  !! 失败窗口 {item['window']}: {item['error']}")
    for item in result["warnings"][:5]:
        print(f"  !  {item}")
    print("库内行数（before → after）:")
    for curve in sorted(set(before["curves"]) | set(after["curves"])):
        print(f"  {curve:<12} {before['curves'].get(curve, 0):>7} → {after['curves'].get(curve, 0):>7}")
    print(f"  合计         {before['rows']:>7} → {after['rows']:>7}   日期 {after['date_min']} ~ {after['date_max']}")
    print("覆盖率交叉验证（国债 10Y vs 库内 treasury_yield_curve）:")
    if "error" in check:
        print(f"  !! {check['error']}")
    else:
        print(
            f"  同日期 {check.get('n_days')} 天  "
            f"|差|中位 {check.get('median_abs_bp')}bp  最大 {check.get('max_abs_bp')}bp  "
            f"<5bp 占比 {check.get('within_5bp_pct')}%  "
            f"({check.get('date_from')} ~ {check.get('date_to')})"
        )
    print("信用利差 spread_AAA_10Y = 商金债AAA(10Y) − 国债(10Y)：")
    if "error" in spread:
        print(f"  !! {spread['error']}")
    else:
        print(
            f"  最近 1 年 中位 {spread.get('median_bp')}bp  "
            f"({spread.get('min_bp')} ~ {spread.get('max_bp')}bp, {spread.get('n_days')} 天, "
            f"{spread.get('date_from')} ~ {spread.get('date_to')})"
        )
    if probes:
        print("抽查（库内最新曲线日的 10Y / 1Y）:")
        for item in probes:
            print(
                f"  {item['curve_date']}  {item['curve_name']:<10} {item['tenor']:<4} "
                f"{item['yield_pct']}%"
            )
    if not write:
        print("[DRY RUN] 未写库（加 --yes 才会写；写前会抢 exclusive_update 单写者锁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
