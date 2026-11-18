"""库内自建个股 beta（`stock_beta`）—— 零外部源、默认 dry-run。

口径（唯一事实来源，改动必须同步本段 + `.planning/2026-11-18-indicator-feasibility-audit/out/data_beta-build.md`）
----------------------------------------------------------------------------------------------------
* **个股收益**：`price_daily_qfq.close`（**前复权**）。用 raw 会因除权跳水产生假收益，
  用 qfq 才能让日收益可比；`close IS NULL / <= 0` 一律剔除。
* **基准**（`--benchmark auto` 默认）：
  1. 首选 `index_valuation.extra->>'index_close'`（`index_code='000300'` 沪深300 日收盘）——
     **实测该序列只有月末点**（265 点 / 2005-04-29~2026-11-17，12 点/年），
     相对 A股交易日历的密度 **5.1%**，远不足以支撑 250 日日收益回归 → `auto` 判定"不可用"。
  2. 次选（`auto` 的第二级，本次实际使用）：**全市场市值加权日收益 `CAPW_MARKET`**
     = `Σ(mcap_i × r_i)/Σ mcap_i`，权重取 `indicator_snapshot.total_market_cap`（当代市值、窗口内恒定）。
     实测对沪深300「近 36 个月月频」**β=1.045 / r²=0.870 / corr=0.933** → 是合格的沪深300 代理。
  3. 最后兜底：**全市场等权日收益 `EW_MARKET`** = 逐日 `close/prev_close-1` 的算术平均。
     注意等权的日波动率约为沪深300 的 1.7 倍，用它当基准会把 β **系统性压缩约 0.59 倍**
     （实测等权对沪深300 近 36 个月 β=0.592 / r²=0.640）→ 只作最后兜底，不做默认。
  4. **禁止逐日混拼**（HS300 有的日子用 HS300、缺的日子用代理）：
     三者日波动率不同量级（实测沪深300≈1.4% vs 等权≈2.5%），混拼会把 β 的量纲弄脏。
     → 要么整段 HS300（`exact`），要么整段 CAPW 代理（`derived`），要么整段 EW（`approximate`）。
     **实际用了哪一种，写进 `source` 与 `benchmark` 两列，不允许只写 000300 冒充。**
* **窗口**：`window_days=250`（1 年）与 `500`（2 年）各出一行；
  `n_obs = 该窗口内【个股收益 ∩ 基准收益】同时存在的观测数`，`n_obs < window_days*0.8` 跳过并计数。
* **窗口右端（`--anchor latest` 默认）**：取该股 qfq 序列的**最后 N 个可比收益**，
  与 `indicator_snapshot` 现有技术指标（`period_return` 等）"按当前可得数据算、用最近报告期贴标签"
  的既有约定一致。`--anchor report_date` 可改为右端钉在 `report_date`（防前视，代价是与既有约定不一致）。
* **report_date**：`indicator_snapshot` 中该股的 `report_date`（每股 1 行），便于直接 join 指标。
* **单位纪律（D40/D46 同源）**：`beta`/`r2` 无量纲；**`alpha` 是「日收益截距」，单位＝百分数**
  （`alpha=0.021` 表示 0.021%/日，年化 ≈ ×250）；`n_obs` 为计数。**不做任何静默换算。**
* **OLS**（numpy 向量化，不逐位调库）：
  `beta = cov(x,y)/var(x)`，`alpha = mean(y) - beta*mean(x)`（日收益口径），`r2 = corr(x,y)**2`。
* **数据卫生**：剔除"相邻两个交易日日历跨度 > `MAX_GAP_DAYS=20` 天"的收益
  （长期停牌复牌、退市重组等断点不是日收益；A股最长连续休市 ≈ 9~10 天，20 天阈值不会误杀正常假期）。
* **raw_hash**：`sha256(stock_code|report_date|benchmark|window_days|n_obs|round(beta,6))`。

安全边界
--------
- **默认 dry-run**：只读、只算、只打印证据，不写库、不落备份；
- **`--yes` 才写**：写前 `any_write_lock_active` 探锁（有写者 → 立即 `[ABORT]` 退出码 3，**不排队**），
  写窗用 `exclusive_update` 单写者串行（拿不到锁 → 跳过退出，绝不死等）；
- **写前备份**：`stock_beta` 整表 `COPY ... (FORMAT PARQUET)` 到
  `.planning/maintenance-backup-stock-beta-<ts>/`（`--backup-dir` 可覆盖）；
- **批量写入**：pandas `register` + **单条** `INSERT ... SELECT ... ON CONFLICT`（D19/D36，禁 `executemany`）；
- **逐股隔离（R6）**：单只算炸只跳过该只并计数，绝不整批失败、绝不把 NULL 伪造成 0；
- **不建表/不改 schema**：`stock_beta` 由 `app/core/storage/schema.py`（v48）负责，本脚本只写行。

用法
----
    # 1) 看计划（默认 dry-run，只统计不写）
    .venv/Scripts/python.exe scripts/compute_stock_beta.py

    # 2) 小批试点：50 只的分布证据（仍然 dry-run）
    .venv/Scripts/python.exe scripts/compute_stock_beta.py --limit 50

    # 3) 真写（显式确认；写锁被占则跳过退出）
    .venv/Scripts/python.exe scripts/compute_stock_beta.py --yes

    # 4) 换基准重跑时清掉旧基准行（每(股,报告期,窗口)只留一行）
    .venv/Scripts/python.exe scripts/compute_stock_beta.py --yes --purge-other-benchmarks

    # 5) 写后交叉验证（独立重算 vs 表内值，误差 < 1e-6 才算过）
    .venv/Scripts/python.exe scripts/compute_stock_beta.py --verify 600519,000001,601318,300750,688111
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
logger = logging.getLogger("compute_stock_beta")

TABLE = "stock_beta"
HS300 = "000300"
CAPW_MARKET = "CAPW_MARKET"
EW_MARKET = "EW_MARKET"

# 基准来源标识：写进 `source` 列，让下游一眼看出这一行的 β 是拿什么当市场因子的。
SOURCE_HS300 = "self:price_daily_qfq+index_valuation:000300:index_close"
SOURCE_CAPW = "self:price_daily_qfq+CAPW_MARKET:price_daily_qfq_x_indicator_snapshot.total_market_cap"
SOURCE_EW = "self:price_daily_qfq+EW_MARKET:price_daily_qfq_equal_weight"

CONFIDENCE_BY_BENCH = {HS300: "exact", CAPW_MARKET: "derived", EW_MARKET: "approximate"}
SOURCE_BY_BENCH = {HS300: SOURCE_HS300, CAPW_MARKET: SOURCE_CAPW, EW_MARKET: SOURCE_EW}

MIN_BREADTH = 300          # EW 基准单日最少成分股数（早期市场样本太弱的日子直接判无效）
MAX_GAP_DAYS = 20          # 相邻交易日日历跨度上限（超期＝停牌断点，剔除该收益）
HS300_MIN_DENSITY = 0.60   # HS300 日收盘相对交易日历的最低密度，低于此整段退化 EW
N_OBS_RATIO = 0.8          # 写入门槛：n_obs >= window_days * 0.8

EW_BENCH_SQL = f"""
WITH px AS (
    SELECT stock_code, trade_date, close,
           LAG(close) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_close
    FROM price_daily_qfq
    WHERE close IS NOT NULL AND close > 0
)
SELECT trade_date,
       AVG(close / prev_close - 1.0) AS bench_ret,
       COUNT(*)                      AS breadth
FROM px
WHERE prev_close IS NOT NULL AND prev_close > 0
GROUP BY trade_date
HAVING COUNT(*) >= {MIN_BREADTH}
ORDER BY trade_date
"""

# 全市场**市值加权**日收益（沪深300 代理）。权重＝`indicator_snapshot.total_market_cap`（当期值，
# 窗口内恒定）→ 大市值股主导。实测与沪深300「近 36 个月月频」β=1.045 / r²=0.870 / corr=0.933，
# 远优于全市场等权（β=0.592 / r²=0.640 / corr=0.800）。**已知偏差**：权重用当期市值套历史，
# 1990s~2000s 早期日期存在前视权重失真 → 只用于 250/500 日窗口（近 1~2 年）。
CAPW_BENCH_SQL = f"""
WITH w AS (
    SELECT stock_code, total_market_cap AS mcap
    FROM indicator_snapshot
    WHERE total_market_cap IS NOT NULL AND total_market_cap > 0
), px AS (
    SELECT stock_code, trade_date, close,
           LAG(close) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_close
    FROM price_daily_qfq
    WHERE close IS NOT NULL AND close > 0
)
SELECT px.trade_date,
       SUM(w.mcap * (px.close / px.prev_close - 1.0)) / SUM(w.mcap) AS bench_ret,
       COUNT(*)                                                      AS breadth
FROM px
JOIN w USING (stock_code)
WHERE px.prev_close IS NOT NULL AND px.prev_close > 0
GROUP BY px.trade_date
HAVING COUNT(*) >= {MIN_BREADTH}
ORDER BY px.trade_date
"""

HS300_BENCH_SQL = f"""
SELECT trade_date,
       CAST(json_extract_string(extra, '$.index_close') AS DOUBLE) AS index_close
FROM index_valuation
WHERE index_code = '{HS300}'
  AND json_extract_string(extra, '$.index_close') IS NOT NULL
  AND CAST(json_extract_string(extra, '$.index_close') AS DOUBLE) > 0
ORDER BY trade_date
"""

CALENDAR_SQL = "SELECT DISTINCT trade_date FROM price_daily_qfq ORDER BY trade_date"

REPORT_DATE_SQL = "SELECT stock_code, report_date FROM indicator_snapshot"


def _read_df(duck: DuckDBStore, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """只读查询 → DataFrame（DuckDBStore.read_query 只回 dict 列表，这里要 fetchdf）。"""
    with duck.read_connection() as conn:
        return conn.execute(sql, params or []).fetchdf()


# --------------------------------------------------------------------------- #
# 路径解析（与 scripts/backfill_technical_indicators.py 同一套隔离纪律）
# --------------------------------------------------------------------------- #
def _is_inside(child: Path, parent: Path) -> bool:
    child_str = os.path.normcase(str(Path(child).resolve())).rstrip("\\/")
    parent_str = os.path.normcase(str(Path(parent).resolve())).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def resolve_target_paths(explicit_db: str | None = None) -> DatabasePathSet:
    """不给 `--db` 只能走正式档位；`--db` 仅 VD_ENV=test/staging 演练用。"""
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


# --------------------------------------------------------------------------- #
# 基准序列
# --------------------------------------------------------------------------- #
def _hs300_series(duck: DuckDBStore, calendar: pd.DatetimeIndex) -> dict[str, Any]:
    """沪深300 日收盘 → 日收益（带密度体检）。"""
    frame = _read_df(duck, HS300_BENCH_SQL)
    if frame.empty:
        return {"ok": False, "reason": "index_valuation 无 000300.index_close", "n": 0}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values("trade_date")
    dates = pd.DatetimeIndex(frame["trade_date"])
    in_range = calendar[(calendar >= dates.min()) & (calendar <= dates.max())]
    density = float(len(dates.intersection(in_range)) / len(in_range)) if len(in_range) else 0.0
    info = {
        "ok": density >= HS300_MIN_DENSITY,
        "n": int(len(dates)),
        "first": str(dates.min().date()),
        "last": str(dates.max().date()),
        "density_vs_calendar": round(density, 4),
        "required_density": HS300_MIN_DENSITY,
        "reason": "ok" if density >= HS300_MIN_DENSITY else
                  f"日收盘密度 {density:.1%} < {HS300_MIN_DENSITY:.0%}（实测 000300 只有月末点）",
    }
    if not info["ok"]:
        return info
    close = frame["index_close"].to_numpy(dtype=float)
    ret = np.zeros(len(close), dtype=float)
    ret[1:] = close[1:] / close[:-1] - 1.0
    info["series"] = pd.Series(ret[1:], index=dates[1:])
    return info


def _agg_series(duck: DuckDBStore, sql: str, label: str) -> dict[str, Any]:
    """把「逐日 bench_ret + breadth」聚合成基准序列（等权 / 市值加权共用）。"""
    frame = _read_df(duck, sql)
    if frame.empty:
        return {"ok": False, "reason": f"{label} 无有效交易日（breadth >= {MIN_BREADTH}）"}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    dates = pd.DatetimeIndex(frame["trade_date"])
    return {
        "ok": True,
        "n": int(len(dates)),
        "first": str(dates.min().date()),
        "last": str(dates.max().date()),
        "breadth_median": int(frame["breadth"].median()),
        "breadth_min": int(frame["breadth"].min()),
        "reason": f"ok（{label}，单日最少 {MIN_BREADTH} 只）",
        "series": pd.Series(frame["bench_ret"].to_numpy(dtype=float), index=dates),
    }


def _ew_series(duck: DuckDBStore) -> dict[str, Any]:
    """全市场等权日收益（最后兜底基准）。"""
    return _agg_series(duck, EW_BENCH_SQL, "等权")


def _capw_series(duck: DuckDBStore) -> dict[str, Any]:
    """全市场市值加权日收益（沪深300 代理，优先于等权）。"""
    return _agg_series(duck, CAPW_BENCH_SQL, "市值加权")


def build_benchmark(duck: DuckDBStore, mode: str) -> dict[str, Any]:
    """按 `--benchmark` 组装基准日收益序列；返回 {code, source, confidence, series, evidence}。"""
    calendar = pd.DatetimeIndex(
        pd.to_datetime([row["trade_date"] for row in duck.read_query(CALENDAR_SQL)])
    )
    hs = _hs300_series(duck, calendar) if mode in {"auto", "hs300"} else {"ok": False, "reason": "未尝试"}
    capw = {"ok": False, "reason": "未尝试"}
    if hs.get("ok"):
        chosen, ev = HS300, hs
    elif mode == "hs300":
        raise SystemExit(
            f"[ABORT] --benchmark hs300 但沪深300 日收盘不可用：{hs.get('reason')}\n"
            "  改用 --benchmark auto（会退化为市值加权代理）或 capw / ew。"
        )
    else:
        if mode in {"auto", "capw"}:
            capw = _capw_series(duck)
        if capw.get("ok"):
            chosen, ev = CAPW_MARKET, capw
        elif mode == "capw":
            raise SystemExit(f"[ABORT] --benchmark capw 不可用：{capw.get('reason')}")
        else:
            chosen, ev = EW_MARKET, _ew_series(duck)
    return {
        "code": chosen,
        "source": SOURCE_BY_BENCH[chosen],
        "confidence": CONFIDENCE_BY_BENCH[chosen],
        "series": ev["series"],
        "evidence": {"requested_mode": mode, "chosen": chosen,
                     "hs300_probe": {k: v for k, v in hs.items() if k != "series"},
                     "capw_probe": {k: v for k, v in capw.items() if k != "series"},
                     "chosen_series": {k: v for k, v in ev.items() if k != "series"}},
    }


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def load_tail_closes(duck: DuckDBStore, max_window: int) -> pd.DataFrame:
    """每股最近 `max_window+1` 根 qfq 收盘（一次 SQL 走完，不逐股查）。"""
    sql = f"""
    WITH ranked AS (
        SELECT stock_code, trade_date, close,
               row_number() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
        FROM price_daily_qfq
        WHERE close IS NOT NULL AND close > 0
    )
    SELECT stock_code, trade_date, close
    FROM ranked
    WHERE rn <= {int(max_window) + 1}
    ORDER BY stock_code, trade_date
    """
    frame = _read_df(duck, sql)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def load_report_dates(duck: DuckDBStore) -> dict[str, date]:
    return {
        str(row["stock_code"]): row["report_date"]
        for row in duck.read_query(REPORT_DATE_SQL)
    }


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """返回 (beta, alpha_daily, r2)。x=基准日收益, y=个股日收益，均为小数口径。"""
    mx, my = x.mean(), y.mean()
    dx, dy = x - mx, y - my
    var_x = float(dx @ dx)
    if var_x <= 0.0:
        raise ValueError("基准收益方差为 0，无法回归")
    beta = float(dx @ dy) / var_x
    alpha = float(my - beta * mx)
    var_y = float(dy @ dy)
    r2 = float((dx @ dy) ** 2 / (var_x * var_y)) if var_y > 0.0 else 0.0
    return beta, alpha, r2


def _raw_hash(stock_code: str, report_date: Any, benchmark: str,
              window_days: int, n_obs: int, beta: float) -> str:
    payload = "|".join([
        str(stock_code), str(report_date), str(benchmark),
        str(int(window_days)), str(int(n_obs)), f"{beta:.6f}",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_beta_rows(
    tail: pd.DataFrame,
    benchmark: dict[str, Any],
    report_dates: dict[str, date],
    windows: list[int],
    *,
    limit: int = 0,
    codes: list[str] | None = None,
    anchor: str = "latest",
    batch_id: str = "",
    fetch_time: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐股 numpy 向量化算 β；R6 逐股隔离（单只失败只跳过并计数）。"""
    bench: pd.Series = benchmark["series"]
    bench_cal = pd.DatetimeIndex(bench.index)
    bench_vals = bench.to_numpy(dtype=float)
    fetch_time = fetch_time or datetime.now(UTC).replace(tzinfo=None)

    # 一次向量化把每根 bar 的基准收益对齐好，避免 5,575 次 reindex
    positions = bench_cal.get_indexer(pd.DatetimeIndex(tail["trade_date"]))
    tail = tail.assign(bench_ret=np.where(positions >= 0, bench_vals[positions], np.nan))

    wanted = set(codes) if codes else None
    groups = tail.groupby("stock_code", sort=True)
    rows: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "candidates": 0, "no_report_date": 0, "too_short": 0, "failed": 0,
        "failed_codes": [], "skipped_short_codes": [],
        "per_window": {int(w): {"written": 0, "skipped_short": 0} for w in windows},
        "max_calendar_gap_days": 0,
    }
    if wanted:
        stats["candidates"] = len(wanted & set(tail["stock_code"].unique()))
    else:
        stats["candidates"] = int(groups.ngroups)
        if limit:
            stats["candidates"] = min(limit, int(groups.ngroups))

    seen = 0
    for code, block in groups:
        if limit and seen >= limit:
            break
        if wanted is not None and code not in wanted:
            continue
        seen += 1
        report_date = report_dates.get(str(code))
        if report_date is None:
            stats["no_report_date"] += 1
            continue
        try:
            hard_cap = pd.Timestamp(report_date) if anchor == "report_date" else None
            block = block.sort_values("trade_date")
            if hard_cap is not None:
                block = block[block["trade_date"] <= hard_cap]
            dates = block["trade_date"].to_numpy(dtype="datetime64[ns]")
            closes = block["close"].to_numpy(dtype=float)
            bvals = block["bench_ret"].to_numpy(dtype=float)
            if closes.size < 3:
                stats["too_short"] += 1
                continue
            rets = closes[1:] / closes[:-1] - 1.0
            gaps = (dates[1:] - dates[:-1]) / np.timedelta64(1, "D")
            stats["max_calendar_gap_days"] = int(max(stats["max_calendar_gap_days"], int(gaps.max())))
            valid = np.isfinite(rets) & np.isfinite(bvals[1:]) & (gaps <= MAX_GAP_DAYS)
            rets, bvals = rets[valid], bvals[1:][valid]
        except Exception as error:  # noqa: BLE001 - R6 逐股隔离
            stats["failed"] += 1
            if len(stats["failed_codes"]) < 20:
                stats["failed_codes"].append(f"{code}:{error}")
            logger.warning("个股数据整理失败 %s: %s", code, error)
            continue

        for window in windows:
            take = min(int(window), rets.size)
            n_obs = int(take)
            key = int(window)
            if n_obs < int(window * N_OBS_RATIO):
                stats["per_window"][key]["skipped_short"] += 1
                if len(stats["skipped_short_codes"]) < 20:
                    stats["skipped_short_codes"].append(f"{code}:{n_obs}<{int(window * N_OBS_RATIO)}")
                continue
            try:
                x = bvals[-take:]
                y = rets[-take:]
                beta, alpha_daily, r2 = _ols(x, y)
                if not np.isfinite(beta) or not np.isfinite(alpha_daily):
                    raise ValueError("beta/alpha 非有限值")
            except Exception as error:  # noqa: BLE001 - R6 逐股隔离
                stats["failed"] += 1
                if len(stats["failed_codes"]) < 20:
                    stats["failed_codes"].append(f"{code}/{window}:{error}")
                logger.warning("beta 回归失败 %s/%s: %s", code, window, error)
                continue
            # alpha 单位＝百分数（日收益截距 ×100），见文件头「单位纪律」
            alpha_pct = alpha_daily * 100.0
            rows.append({
                "stock_code": str(code),
                "report_date": report_date,
                "benchmark": benchmark["code"],
                "window_days": key,
                "beta": beta,
                "alpha": alpha_pct,
                "r2": r2,
                "n_obs": n_obs,
                "source": benchmark["source"],
                "fetch_time": fetch_time,
                "raw_hash": _raw_hash(code, report_date, benchmark["code"], key, n_obs, beta),
                "confidence": benchmark["confidence"],
                "batch_id": batch_id,
            })
            stats["per_window"][key]["written"] += 1
    return rows, stats


# --------------------------------------------------------------------------- #
# 写库（pandas register + 单条 INSERT ... SELECT ... ON CONFLICT）
# --------------------------------------------------------------------------- #
WRITE_COLUMNS = [
    "stock_code", "report_date", "benchmark", "window_days", "beta", "alpha", "r2",
    "n_obs", "source", "fetch_time", "raw_hash", "confidence", "batch_id",
]

UPSERT_SQL = f"""
INSERT INTO {TABLE}
    (stock_code, report_date, benchmark, window_days, beta, alpha, r2,
     n_obs, source, fetch_time, raw_hash, confidence, batch_id)
SELECT stock_code, report_date, benchmark, window_days, beta, alpha, r2,
       n_obs, source, fetch_time, raw_hash, confidence, batch_id
FROM _beta_rows
ON CONFLICT (stock_code, report_date, benchmark, window_days) DO UPDATE SET
    beta       = excluded.beta,
    alpha      = excluded.alpha,
    r2         = excluded.r2,
    n_obs      = excluded.n_obs,
    source     = excluded.source,
    fetch_time = excluded.fetch_time,
    raw_hash   = excluded.raw_hash,
    confidence = excluded.confidence,
    batch_id   = excluded.batch_id
"""


def _to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=WRITE_COLUMNS)
    frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.date
    frame["fetch_time"] = pd.to_datetime(frame["fetch_time"])
    frame["window_days"] = frame["window_days"].astype("int32")
    frame["n_obs"] = frame["n_obs"].astype("int32")
    return frame


def write_rows(
    duck: DuckDBStore, rows: list[dict[str, Any]], *, purge_other: bool = False,
    keep_benchmark: str | None = None,
) -> dict[str, Any]:
    """单事务：可选清掉别的 benchmark 旧行 → upsert 本次行（D19/D36 向量化范式）。"""
    frame = _to_frame(rows)
    with duck.write_connection() as conn:
        before = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
        purged = 0
        if purge_other and keep_benchmark:
            purged = int(conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE benchmark <> ?", [keep_benchmark],
            ).fetchone()[0])
            conn.execute(f"DELETE FROM {TABLE} WHERE benchmark <> ?", [keep_benchmark])
        conn.register("_beta_rows", frame)
        try:
            conn.execute(UPSERT_SQL)
        finally:
            conn.unregister("_beta_rows")
        after = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    return {"before": before, "after": after, "upserted": len(frame),
            "inserted": after - before, "purged_other_benchmarks": purged}


def backup_table(duck: DuckDBStore, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = str(backup_dir / f"{TABLE}.parquet").replace("'", "''")
    with duck.write_connection() as conn:
        conn.execute(f"COPY {TABLE} TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] %s → %s", TABLE, target)
    return backup_dir / f"{TABLE}.parquet"


# --------------------------------------------------------------------------- #
# 证据 / 验收
# --------------------------------------------------------------------------- #
def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    arr = np.asarray(values, dtype=float)
    return {
        "n": int(arr.size),
        "median": round(float(np.median(arr)), 4),
        "mean": round(float(arr.mean()), 4),
        "p05": round(float(np.percentile(arr, 5)), 4),
        "p95": round(float(np.percentile(arr, 95)), 4),
        "min": round(float(arr.min()), 4),
        "max": round(float(arr.max()), 4),
    }


def summarize(rows: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"rows": len(rows), "stats": stats, "by_window": {}}
    for window in sorted({row["window_days"] for row in rows}):
        subset = [row for row in rows if row["window_days"] == window]
        out["by_window"][str(window)] = {
            "rows": len(subset),
            "beta": _distribution([row["beta"] for row in subset]),
            "alpha_pct_daily": _distribution([row["alpha"] for row in subset]),
            "r2": _distribution([row["r2"] for row in subset]),
            "n_obs": _distribution([float(row["n_obs"]) for row in subset]),
        }
    return out


def verify_codes(
    duck: DuckDBStore, benchmark: dict[str, Any], report_dates: dict[str, date],
    codes: list[str], windows: list[int], anchor: str = "latest",
) -> dict[str, Any]:
    """独立重算（另一条取数路径：pandas merge 对齐）vs 表内值，误差 < 1e-6 才算过。"""
    placeholders = ", ".join("?" for _ in codes)
    tail = _read_df(
        duck,
        f"""SELECT stock_code, trade_date, close FROM price_daily_qfq
            WHERE stock_code IN ({placeholders}) AND close IS NOT NULL AND close > 0
            ORDER BY stock_code, trade_date""",
        list(codes),
    )
    tail["trade_date"] = pd.to_datetime(tail["trade_date"])
    bench = benchmark["series"].rename("bench_ret").reset_index()
    bench.columns = ["trade_date", "bench_ret"]
    tail = tail.merge(bench, on="trade_date", how="left").sort_values(["stock_code", "trade_date"])
    tail["ret"] = tail.groupby("stock_code")["close"].pct_change()
    stored = {
        (str(row["stock_code"]), int(row["window_days"])): row
        for row in duck.read_query(
            f"""SELECT stock_code, window_days, beta, alpha, r2, n_obs, benchmark, source, raw_hash
                FROM {TABLE} WHERE stock_code IN ({placeholders})""",
            list(codes),
        )
    }
    detail: list[dict[str, Any]] = []
    max_diff = 0.0
    for code, block in tail.groupby("stock_code", sort=True):
        block = block.dropna(subset=["ret", "bench_ret"])
        if anchor == "report_date" and code in report_dates:
            block = block[block["trade_date"] <= pd.Timestamp(report_dates[code])]
        for window in windows:
            take = block.tail(int(window))
            manual = None
            if len(take) >= int(window * N_OBS_RATIO):
                manual = _ols(take["bench_ret"].to_numpy(dtype=float),
                              take["ret"].to_numpy(dtype=float))
            row = stored.get((str(code), int(window)))
            diff = None
            if manual is not None and row is not None:
                diff = abs(float(row["beta"]) - manual[0])
                max_diff = max(max_diff, diff)
            detail.append({
                "stock_code": str(code), "window_days": int(window),
                "manual_beta": None if manual is None else round(manual[0], 6),
                "table_beta": None if row is None else round(float(row["beta"]), 6),
                "abs_diff": None if diff is None else f"{diff:.3e}",
                "n_obs_manual": int(len(take)) if manual is not None else 0,
                "n_obs_table": None if row is None else int(row["n_obs"]),
                "benchmark": None if row is None else str(row["benchmark"]),
                "source": None if row is None else str(row["source"]),
            })
    return {"max_abs_diff_beta": max_diff, "pass": bool(max_diff < 1e-6), "detail": detail}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="库内自建个股 beta（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="真正写库（缺省 = dry-run）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只（0 = 全部）")
    parser.add_argument("--codes", default="", help="只处理这些代码（逗号分隔）")
    parser.add_argument("--windows", default="250,500", help="窗口（逗号分隔），默认 250,500")
    parser.add_argument("--benchmark", default="auto", choices=["auto", "hs300", "capw", "ew"],
                        help="auto=有密集沪深300日线就用，否则市值加权代理，再否则全市场等权")
    parser.add_argument("--purge-other-benchmarks", action="store_true",
                        help="写入时删除 benchmark 与本次不同的旧行（每(股,报告期,窗口)只留一行）")
    parser.add_argument("--anchor", default="latest", choices=["latest", "report_date"],
                        help="窗口右端：latest=该股最后 N 个可比收益（默认）；report_date=钉在报告期")
    parser.add_argument("--verify", default="", help="写后交叉验证的股票代码（逗号分隔）")
    parser.add_argument("--db", default=None, help="临时库演练（仅 VD_ENV=test/staging）")
    parser.add_argument("--backup-dir", default=None, help="备份目录（缺省 .planning/maintenance-backup-stock-beta-<ts>）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    t_start = datetime.now()
    codes = [item.strip() for item in args.codes.split(",") if item.strip()]
    verify_list = [item.strip() for item in args.verify.split(",") if item.strip()]
    windows = sorted({int(item) for item in args.windows.split(",") if item.strip()})
    if not windows:
        print("[ABORT] --windows 不能为空", file=sys.stderr)
        return 2

    try:
        paths = resolve_target_paths(args.db)
    except PathIsolationError as error:
        print(f"[ABORT] 无法解析目标库路径：{error}", file=sys.stderr)
        return 2

    write = bool(args.yes)
    if write and any_write_lock_active(paths.duckdb_path):
        print("[ABORT] 检测到写锁（自动更新/其它维护进程正在写库）。单写者串行：等它结束再重跑，"
              "本脚本不排队、不抢锁。", file=sys.stderr)
        return 3

    duck = DuckDBStore(paths=paths)
    benchmark = build_benchmark(duck, args.benchmark)
    report_dates = load_report_dates(duck)
    tail = load_tail_closes(duck, max(windows))
    batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-beta"
    fetch_time = datetime.now(UTC).replace(tzinfo=None)

    rows, stats = compute_beta_rows(
        tail, benchmark, report_dates, windows,
        limit=args.limit, codes=codes or None, anchor=args.anchor,
        batch_id=batch_id, fetch_time=fetch_time,
    )
    summary = summarize(rows, stats)

    write_report: dict[str, Any] | None = None
    backup_path: Path | None = None
    if write and rows:
        backup_dir = (
            Path(args.backup_dir) if args.backup_dir
            else PROJECT_ROOT / ".planning" / f"maintenance-backup-stock-beta-{datetime.now():%Y%m%d_%H%M%S}"
        )
        try:
            with exclusive_update(paths.duckdb_path):
                try:
                    backup_path = backup_table(duck, backup_dir)
                except Exception as error:  # noqa: BLE001 - 备份失败必须中止写入
                    print(f"[ABORT] 写前备份失败，已中止：{error}", file=sys.stderr)
                    return 5
                write_report = write_rows(
                    duck, rows,
                    purge_other=bool(args.purge_other_benchmarks),
                    keep_benchmark=benchmark["code"],
                )
        except UpdateLockError:
            print("[SKIP] 写锁被其它进程持有，按要求跳过退出（不排队、不死等）。", file=sys.stderr)
            return 3
        summary["written"] = write_report
        summary["backup_path"] = str(backup_path)

    verification: dict[str, Any] | None = None
    if verify_list:
        verification = verify_codes(duck, benchmark, report_dates, verify_list,
                                    windows, anchor=args.anchor)

    payload = {
        "task": "库内自建个股 beta（stock_beta）",
        "executed_at": datetime.now(UTC).isoformat(),
        "elapsed_sec": round((datetime.now() - t_start).total_seconds(), 2),
        "dry_run": not write,
        "database": str(paths.duckdb_path),
        "network_requests": 0,
        "benchmark": {k: v for k, v in benchmark.items() if k != "series"},
        "windows": windows,
        "anchor": args.anchor,
        "limit": args.limit,
        "codes": codes,
        "batch_id": batch_id,
        "summary": summary,
        "verification": verification,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    bench_ev = benchmark["evidence"]
    print(f"模式: {'WRITE' if write else 'DRY RUN'}  数据库: {paths.duckdb_path}")
    print(f"外部请求: 0 次（本表纯库内派生，无网络、无限速/重试面）")
    print(f"基准: {benchmark['code']}  source={benchmark['source']}  confidence={benchmark['confidence']}")
    print(f"  沪深300 体检: {bench_ev['hs300_probe'].get('reason')}"
          f"（点数 {bench_ev['hs300_probe'].get('n')}, 密度 {bench_ev['hs300_probe'].get('density_vs_calendar')}）")
    print(f"  实选序列: {benchmark['code']} {bench_ev['chosen_series'].get('first')}"
          f"~{bench_ev['chosen_series'].get('last')} 共 {bench_ev['chosen_series'].get('n')} 个交易日")
    print(f"股票池: 候选 {stats['candidates']} 只  无报告期 {stats['no_report_date']}"
          f"  计算失败 {stats['failed']}  最大日历跨度 {stats['max_calendar_gap_days']} 天")
    for window in windows:
        got = summary["by_window"].get(str(window), {})
        beta = got.get("beta", {})
        short = stats["per_window"][int(window)]["skipped_short"]
        print(f"  window={window}: 写入 {got.get('rows', 0)} 行 / n_obs 不足跳过 {short} 只"
              f" | beta 中位数 {beta.get('median')} p05 {beta.get('p05')} p95 {beta.get('p95')}"
              f" | r2 中位数 {got.get('r2', {}).get('median')}")
    if stats["failed_codes"]:
        print(f"  失败样本(≤20): {stats['failed_codes']}")
    if stats["skipped_short_codes"]:
        print(f"  n_obs 不足样本(≤20): {stats['skipped_short_codes']}")
    if write_report:
        print(f"[WRITE] upsert {write_report['upserted']} 行  before {write_report['before']}"
              f" → after {write_report['after']}（新增 {write_report['inserted']}，"
              f"清理异基准旧行 {write_report['purged_other_benchmarks']}）  备份 {backup_path}")
    elif not write:
        print("[DRY RUN] 未写库；确认无误后加 --yes。")
    if verification:
        print(f"交叉验证: {'PASS' if verification['pass'] else 'FAIL'}"
              f"  max|Δbeta| = {verification['max_abs_diff_beta']:.3e}（门槛 1e-6）")
        for item in verification["detail"]:
            print(f"  {item['stock_code']} w={item['window_days']}: 手工 {item['manual_beta']}"
                  f" vs 表内 {item['table_beta']}  Δ={item['abs_diff']}"
                  f"  n_obs {item['n_obs_manual']}/{item['n_obs_table']}")
    print(f"耗时 {payload['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
