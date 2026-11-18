"""抓取国家维度宏观序列（GDP / CPI）→ `macro_series`（schema v48）。

默认 dry-run；`--yes` 才写正式库，写前检查 `update_lock.any_write_lock_active`，
写窗口用 `exclusive_update` 单写者串行；拿不到锁就**跳过退出**（不排队、不抢锁）。

数据源与口径（唯一真源：`.planning/2026-11-18-indicator-feasibility-audit/out/source_probe.md` §1.B）
-----------------------------------------------------------------------------------------
1) `ak.macro_china_gdp()` —— 83 行 / 9 列，0.19s 一次拿全历史（2006Q1 → 2026Q3）。
   * `季度` 列形如 `2026年第1-3季度`（**累计口径**：1-2/1-3/1-4 是累计值，不是单季值）。
     本脚本原样落「累计值 + 该累计期末日」，**不自行差分**；要单季值必须下游自己差。
   * `国内生产总值-绝对值` 单位是**亿元** → `indicator='gdp_quarterly'`, `unit='CNY_100M'`
     （实测 2025 全年 1,401,879.2 亿，量级校验通过）。
   * `国内生产总值-同比增长` 是百分数 → `indicator='gdp_yoy'`, `unit='percent'`。
2) `ak.macro_china_cpi()` —— 226 行 / 13 列，0.14s（2008-01 → 2026-10）。
   * `全国-同比增长`   → `indicator='cpi_yoy'`,   `unit='percent'`
   * `全国-环比增长`   → `indicator='cpi_mom'`,   `unit='percent'`
   * `全国-当月`       → `indicator='cpi_index'`, `unit='index_yoy_same_month_100'`
     ⚠️ **口径纠偏（必须读）**：`全国-当月` 不是百分数，是「上年同月=100」的**指数**
     （2026-10 = 100.7，2008-01 = 107.0781，且恒有 `当月 − 100 = 同比增长`）。
     任务书原写「取『全国-当月』存 cpi_mom（unit=percent）」——那会把 100.7 当 100.7%
     落库（100 倍级单位错误且不报错，正是 KB D40/D46 的单位纪律禁止项），因此本脚本：
     ① `cpi_mom` 取真正的**环比**列 `全国-环比增长`（0.0 / 0.2 …，单位 %）；
     ② 同时把 `全国-当月` 以 `cpi_index` + 显式 unit 落库，**不丢源数据、不改标签**。
     下游若要「CPI 定基指数」请用 `cpi_index`，**不要**把 `cpi_mom` 当指数用。
   * 月份 → 期末日取**自然月末**（2026 年 10 月 = 2026-10-31）。

幂等
----
主键 `(indicator, period_date, source)` upsert；`raw_hash` 是**整行源数据摘要**
（源行全部列拼串后 SHA-256），任一列变化即 hash 变化，可审计「这行是哪次抓取写的」。

批量写入（KB D19/D36）
----------------------
pandas `DataFrame` → `connection.register()` → 单条 `INSERT ... SELECT ... ON CONFLICT`，
**不用 executemany**（DuckDB 1.5.5 对 date 参数 executemany 有 ~450KB/行 的内存病理）。

用法
----
    python scripts/fetch_macro_series.py                 # dry-run，打印计划 + 抽查
    python scripts/fetch_macro_series.py --yes           # 全量写库（幂等，可重跑）
    python scripts/fetch_macro_series.py --yes --limit 12  # 试点：每个指标只写最新 12 期
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import akshare as ak  # noqa: E402

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
logger = logging.getLogger("fetch_macro_series")

TABLE = "macro_series"
SOURCE_GDP = "akshare:macro_china_gdp"
SOURCE_CPI = "akshare:macro_china_cpi"
CONFIDENCE = "strict"  # 官方统计口径原值，不做任何换算

COL_QUARTER = "季度"
COL_GDP_VALUE = "国内生产总值-绝对值"
COL_GDP_YOY = "国内生产总值-同比增长"
COL_CPI_MONTH = "月份"
COL_CPI_INDEX = "全国-当月"
COL_CPI_YOY = "全国-同比增长"
COL_CPI_MOM = "全国-环比增长"

# indicator -> (源列, unit, freq)
GDP_SPECS: tuple[tuple[str, str, str], ...] = (
    ("gdp_quarterly", COL_GDP_VALUE, "CNY_100M"),
    ("gdp_yoy", COL_GDP_YOY, "percent"),
)
CPI_SPECS: tuple[tuple[str, str, str], ...] = (
    ("cpi_yoy", COL_CPI_YOY, "percent"),
    ("cpi_mom", COL_CPI_MOM, "percent"),
    ("cpi_index", COL_CPI_INDEX, "index_yoy_same_month_100"),
)
UNIT_LABEL = {
    "CNY_100M": "亿元",
    "percent": "百分数(%)",
    "index_yoy_same_month_100": "指数(上年同月=100)",
}
OUTPUT_COLUMNS = [
    "indicator", "period_date", "value", "unit",
    "source", "fetch_time", "raw_hash", "confidence", "batch_id",
]

_RE_QUARTER = re.compile(r"^(\d{4})年第(\d)(?:-(\d))?季度$")
_RE_MONTH = re.compile(r"^(\d{4})年(\d{1,2})月份$")
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}

UPSERT_SQL = f"""
INSERT INTO {TABLE}
    (indicator, period_date, value, unit, source, fetch_time, raw_hash, confidence, batch_id)
SELECT indicator, period_date, value, unit, source, fetch_time, raw_hash, confidence, batch_id
FROM _macro_rows
ON CONFLICT (indicator, period_date, source) DO UPDATE SET
    value      = excluded.value,
    unit       = excluded.unit,
    fetch_time = excluded.fetch_time,
    raw_hash   = excluded.raw_hash,
    confidence = excluded.confidence,
    batch_id   = excluded.batch_id
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


# --------------------------------------------------------------------------- 解析
def quarter_end(text: Any) -> date | None:
    """`2026年第1-3季度` → 2026-09-30（累计口径的**期末日**，不差分）。"""
    if not isinstance(text, str):
        return None
    match = _RE_QUARTER.match(text.strip())
    if match is None:
        return None
    year = int(match.group(1))
    quarter = int(match.group(3) or match.group(2))
    if quarter not in _QUARTER_END:
        return None
    month, day = _QUARTER_END[quarter]
    return date(year, month, day)


def month_end(text: Any) -> date | None:
    """`2026年10月份` → 2026-10-31（自然月末）。"""
    if not isinstance(text, str):
        return None
    match = _RE_MONTH.match(text.strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def row_digest(values: Sequence[Any]) -> str:
    """整行源数据摘要：所有源列 `列=值` 拼接后 SHA-256（任一列变动即变 hash）。"""
    payload = "|".join(values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _spec_frame(
    raw: pd.DataFrame,
    *,
    specs: Sequence[tuple[str, str, str]],
    period_col: str,
    period_parser: Any,
    source: str,
    fetch_time: pd.Timestamp,
    batch_id: str,
    label: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """把宽表源数据按 `specs` 折成长表（每行 = 一个 indicator 的一个观测）。"""
    problems: dict[str, Any] = {
        "source_rows": int(len(raw)),
        "period_unparsed": 0,
        "value_null": 0,
        "column_missing": [],
    }
    missing = [col for col in [period_col, *[spec[1] for spec in specs]] if col not in raw.columns]
    if missing:
        problems["column_missing"] = missing
        logger.error("[%s] 源列缺失: %s（实际列=%s）", label, missing, list(raw.columns))
        return pd.DataFrame(columns=OUTPUT_COLUMNS), problems

    periods = raw[period_col].map(period_parser)
    periods = pd.Series(periods, index=raw.index)
    bad = int(periods.isna().sum())
    problems["period_unparsed"] = bad
    if bad:
        samples = raw.loc[periods.isna(), period_col].head(3).tolist()
        logger.warning("[%s] %d 行期间列无法解析，已逐行跳过（样例=%s）", label, bad, samples)

    digests = raw.apply(
        lambda row: row_digest([f"{col}={row[col]!r}" for col in raw.columns]),
        axis=1,
    )

    pieces: list[pd.DataFrame] = []
    for indicator, column, unit in specs:
        values = pd.to_numeric(raw[column], errors="coerce")
        nulls = int(values.isna().sum())
        problems["value_null"] += nulls
        frame = pd.DataFrame(
            {
                "indicator": indicator,
                "period_date": periods,
                "value": values,
                "unit": unit,
                "source": source,
                "fetch_time": fetch_time,
                "raw_hash": digests,
                "confidence": CONFIDENCE,
                "batch_id": batch_id,
            }
        )
        frame = frame[frame["period_date"].notna() & frame["value"].notna()]
        pieces.append(frame)

    if not pieces:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), problems
    out = pd.concat(pieces, ignore_index=True)
    return out, problems


def fetch_gdp(fetch_time: pd.Timestamp, batch_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    raw = ak.macro_china_gdp()
    return _spec_frame(
        raw,
        specs=GDP_SPECS,
        period_col=COL_QUARTER,
        period_parser=quarter_end,
        source=SOURCE_GDP,
        fetch_time=fetch_time,
        batch_id=batch_id,
        label="GDP",
    )


def fetch_cpi(fetch_time: pd.Timestamp, batch_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    raw = ak.macro_china_cpi()
    return _spec_frame(
        raw,
        specs=CPI_SPECS,
        period_col=COL_CPI_MONTH,
        period_parser=month_end,
        source=SOURCE_CPI,
        fetch_time=fetch_time,
        batch_id=batch_id,
        label="CPI",
    )


# --------------------------------------------------------------------------- 计划 / 写库
def trim_to_limit(frame: pd.DataFrame, limit: int) -> pd.DataFrame:
    """`--limit N`：**每个 indicator 只保留最新 N 期**（试点用，不改口径）。"""
    if limit <= 0 or frame.empty:
        return frame
    frame = frame.sort_values(["indicator", "period_date"])
    return frame.groupby("indicator", group_keys=False, sort=False).tail(limit).reset_index(drop=True)


def dedupe(frame: pd.DataFrame) -> pd.DataFrame:
    """同一批里同主键只能出现一次（否则 ON CONFLICT 会因批内重复报错）。"""
    if frame.empty:
        return frame
    return frame.drop_duplicates(subset=["indicator", "period_date", "source"], keep="last")


def plan_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for indicator, group in frame.groupby("indicator", sort=True):
        unit = str(group["unit"].iloc[0])
        rows.append(
            {
                "indicator": indicator,
                "unit": unit,
                "unit_label": UNIT_LABEL.get(unit, unit),
                "rows": int(len(group)),
                "period_min": group["period_date"].min().isoformat(),
                "period_max": group["period_date"].max().isoformat(),
            }
        )
    return rows


def existing_counts(duck: DuckDBStore) -> dict[str, int]:
    try:
        rows = duck.read_query(f"SELECT indicator, COUNT(*) AS n FROM {TABLE} GROUP BY 1")
    except Exception as error:  # noqa: BLE001 - 未迁移的库应可 dry-run
        logger.warning("%s 不可读（可能未建表，按空处理）: %s", TABLE, error)
        return {}
    return {str(row["indicator"]): int(row["n"]) for row in rows}


def write_rows(duck: DuckDBStore, frame: pd.DataFrame) -> int:
    frame = frame[OUTPUT_COLUMNS].copy()
    frame["period_date"] = pd.to_datetime(frame["period_date"]).dt.date
    frame["fetch_time"] = pd.to_datetime(frame["fetch_time"])
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce").astype("float64")
    with duck.write_connection() as conn:
        conn.execute("SET preserve_insertion_order=false")
        conn.register("_macro_rows", frame)
        try:
            conn.execute(UPSERT_SQL)
        finally:
            conn.unregister("_macro_rows")
    return int(len(frame))


def spot_check(duck: DuckDBStore, probes: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for indicator, period in probes:
        found = duck.read_query(
            f"""SELECT indicator, period_date, value, unit, source
                FROM {TABLE} WHERE indicator = ? AND period_date = ?""",
            [indicator, period],
        )
        rows.append(
            {
                "indicator": indicator,
                "period_date": period,
                "value": found[0]["value"] if found else None,
                "unit": found[0]["unit"] if found else None,
            }
        )
    return rows


PROBES: tuple[tuple[str, str], ...] = (
    ("gdp_quarterly", "2025-12-31"),
    ("gdp_yoy", "2025-12-31"),
    ("cpi_yoy", "2026-10-31"),
    ("cpi_index", "2026-10-31"),
    ("cpi_mom", "2026-10-31"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 GDP/CPI 宏观序列（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="确认写库（默认只打印计划）")
    parser.add_argument("--limit", type=int, default=0, help="每个指标只写最新 N 期（试点）")
    parser.add_argument("--db", default=None, help="仅 VD_ENV=test/staging 下可用的演练库路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    try:
        paths = resolve_target_paths(args.db)
    except PathIsolationError as error:
        print(f"[ABORT] 无法解析目标库路径：{error}", file=sys.stderr)
        return 2

    write = bool(args.yes)
    if write and any_write_lock_active(paths.duckdb_path):
        print(
            "[SKIP] 检测到写锁（自动更新或其它维护进程正在写库）；单写者串行："
            "本脚本不排队、不抢锁，直接跳过退出。请稍后重跑。",
            file=sys.stderr,
        )
        return 3

    duck = DuckDBStore(paths=paths)
    batch_id = f"macro-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    fetch_time = pd.Timestamp.now(tz="UTC").tz_localize(None)  # naive UTC（KB D19）

    frames: list[pd.DataFrame] = []
    problems: dict[str, Any] = {}
    for label, fetcher in (("GDP", fetch_gdp), ("CPI", fetch_cpi)):
        try:
            frame, problem = fetcher(fetch_time, batch_id)
        except Exception as error:  # noqa: BLE001 - 单源失败只跳过该源（R6 逐源隔离）
            logger.error("[%s] 抓取失败，跳过（%s: %s）", label, type(error).__name__, error)
            problems[label] = {"error": f"{type(error).__name__}: {error}"}
            continue
        problems[label] = problem
        frames.append(frame)
        logger.info(
            "[%s] 源返回 %d 行 → 折出 %d 条观测（期间不可解析 %d，空值 %d）",
            label, problem["source_rows"], len(frame),
            problem["period_unparsed"], problem["value_null"],
        )

    if not frames:
        print("[ABORT] 两个源都失败，未写任何数据。", file=sys.stderr)
        return 1

    combined = dedupe(pd.concat(frames, ignore_index=True))
    combined = trim_to_limit(combined, args.limit)
    planned = plan_rows(combined)

    before = existing_counts(duck)
    written = 0
    write_error: str | None = None
    if write:
        try:
            with exclusive_update(paths.duckdb_path):
                written = write_rows(duck, combined)
        except UpdateLockError as error:
            print(f"[SKIP] 另一个更新进程持有写锁：{error}", file=sys.stderr)
            return 3
        except Exception as error:  # noqa: BLE001 - 写失败必须显式报出而不是静默
            write_error = f"{type(error).__name__}: {error}"
            logger.error("写库失败：%s", write_error)
            return 4

    after = existing_counts(duck)
    probes = spot_check(duck, PROBES) if write else []

    payload = {
        "task": "抓取 GDP/CPI → macro_series",
        "executed_at": datetime.now(UTC).isoformat(),
        "dry_run": not write,
        "database": str(paths.duckdb_path),
        "batch_id": batch_id,
        "limit": args.limit,
        "rows_planned": int(len(combined)),
        "rows_written": written,
        "plan": planned,
        "problems": problems,
        "existing_counts_before": before,
        "existing_counts_after": after,
        "spot_checks": probes,
        "write_error": write_error,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}")
    print(f"批次: {batch_id}  待写观测: {len(combined)} 行  实际写入: {written} 行")
    for row in planned:
        print(
            f"  {row['indicator']:<14} {row['unit']:<26} {row['rows']:>4} 行  "
            f"{row['period_min']} → {row['period_max']}  ({row['unit_label']})"
        )
    for label, problem in problems.items():
        if "error" in problem:
            print(f"  !! {label} 抓取失败: {problem['error']}")
    print("库内行数（before → after）:")
    if not (set(before) | set(after)):
        print("  （表内暂无数据）")
    else:
        for indicator in sorted(set(before) | set(after)):
            print(
                f"  {indicator:<14} {before.get(indicator, 0):>6} → {after.get(indicator, 0):>6}"
            )
    if write:
        print("抽查（写后读回）:")
        for item in probes:
            print(
                f"  {item['indicator']:<14} {item['period_date']}  value={item['value']}"
                f"  unit={item['unit']}"
            )
    else:
        print("抽查（本次抓取，未写库）:")
        for indicator, period in PROBES:
            hit = combined[
                (combined["indicator"] == indicator)
                & (combined["period_date"] == date.fromisoformat(period))
            ]
            if hit.empty:
                print(f"  {indicator:<14} {period}  源未提供该期")
                continue
            row = hit.iloc[0]
            print(f"  {indicator:<14} {period}  value={row['value']}  unit={row['unit']}")
        print("[DRY RUN] 未写库（加 --yes 才会写；写前会抢 exclusive_update 单写者锁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
