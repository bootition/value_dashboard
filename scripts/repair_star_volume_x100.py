"""修复科创板成交量被放大 100 倍（reports/131 §9.2 P0-1）。

问题
----
`app/core/adapters/tencent_adapter.py` 旧实现**无条件**把腾讯 K 线的
`bar[5] * 100`（注释假定腾讯成交量单位是「手」）。主板/创业板确实如此，
但腾讯对**科创板（688/689）直接返回股数**，于是科创板成交量被放大 100 倍：

- 688111 金山办公 2026-07-30 = 12,675,928 股（换手 2.7%）；
  2026-07-31 = 2,873,692,500 股（换手 619%），单日跳 227 倍；
- 2026-11-13 全市场横向：科创板 ``volume/circ_shares`` 中位 1.9396（194%），
  其他板块 0.0169（1.7%）——相差约 115 倍，物理上不可能；
- 影响 ``price_daily_raw`` / ``price_daily_qfq`` 各 4.5 万行 / 612 只，
  下游 ``indicator_snapshot.avg_volume`` 533 只偏大 100 倍。

断点在 **2026-07-31**：当天价格主链整体切到腾讯。适配器守卫已同步修好
（科创板不乘 100 + 换手 >500% 量级自检兜底），本脚本负责**修历史**。

判定（不是"按日期一刀切"，每只股票/每张表独立判）
--------------------------------------------------
对每只 688/689 股票、在 ``--since`` 之后：

1. **定位断点**：找 ``since`` 之后第一根"成交量 / 上一交易日成交量 > --row-jump(50)"
   的 bar（单位切换当天必然出现这种跳变）。找不到就把 ``since`` 当断点。
2. **确认是单位错而不是真实放量**（窗口中位数判据，四选一）：
   - 基线可信（断点前 20 行的 ``volume/circ_shares`` 中位数 ≤ ``--clean-baseline-turnover``(0.5)）
     → 要求 **后窗/前窗成交量中位数 > --ratio-threshold(10)**；
   - 基线自身就被放大（断点前中位换手 > 0.5）→ 该股该表已被证明有单位问题，
     只要求后窗中位换手 > ``--polluted-baseline-turnover``(0.2)；
   - 无基线（断点后才上市）→ 要求后窗中位换手 > ``--no-baseline-turnover``(3.0)；
   - 无流通股本且无基线 → 无法判定，跳过并在报告里列出。
3. **逐行护栏**：若该行 ``turnover/volume/close`` 落在 [0.5, 2]（成交量与成交额
   自洽），说明这一行单位是对的（例如当天来自别的源），**不除**。

通过 1+2 的股票，把断点之后（且 ≥ ``--since``）的 ``volume`` 除以 100。

幂等性（自证）
--------------
修复后同一套判据必须**再也找不到断点、也过不了中位数确认**：

- 第 1 步的 50 倍跳变随 ``/100`` 一起消失（688111 由 227 倍变成 2.27 倍）；
- 第 2 步的比值/换手中位数同步降 100 倍（>10 的比值 → ~1；
  0.2/0.5/3.0 的换手门槛 → 真实 1%~3%）；
- 第 3 步逐行护栏只会更安全（修复后 vwap/close ≈ 100，不再判"自洽"，
  但股票级判据已经不触发）。

因此**重复执行 --yes 的影响行数必然是 0**（脚本会把 0 行打印出来；``--report``
可对最近一次备份做一次真实值审计）。备份（parquet + plan.json）落在
``.planning/maintenance-backup-star-volume-<时间戳>/``，可用 ``--restore <目录>``
回滚。

用法
----
    # 1) dry-run（默认，只读、不写库）
    python scripts/repair_star_volume_x100.py

    # 2) 正式修复（备份 + 单写者锁 + 单事务）
    python scripts/repair_star_volume_x100.py --yes

    # 3) 顺带重算 indicator_snapshot.avg_volume（见下）
    python scripts/repair_star_volume_x100.py --yes --recompute-avg-volume

    # 4) 回滚（把备份里的原始 volume 写回）
    python scripts/repair_star_volume_x100.py --restore .planning/maintenance-backup-star-volume-20261117_101500 --yes

``avg_volume`` 说明：``indicator_snapshot.avg_volume`` 是行情主链"最近 20 个
连续交易日 raw volume 的均值"（``app/core/indicators/calculator.py``
``_calc_technical``）。2026-07-31 至今（≈71 个交易日）远多于 20 日，所以受影响
股票最近 20 行**全部**在被修区间内时，``avg_volume / 100`` 与重算结果等价——
脚本只对"最近 20 行都落在修复集里"的股票做这一步；不满足的股票不猜数，
在输出里列出并提示主控跑 ``vd data compute_indicators`` 走完整重算。
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    resolve_and_validate_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("repair_star_volume_x100")

TABLES: tuple[str, ...] = ("price_daily_raw", "price_daily_qfq")
STAR_PREFIXES: tuple[str, ...] = ("688", "689")
DEFAULT_SINCE = "2026-07-31"
#: 放大倍数（腾讯 bar[5] 被多乘的那个 100）
INFLATION = 100.0

#: 断点前预读天数：只需覆盖 20 个交易日的基线窗口，多留余量以跨长假
LOOKBACK_DAYS = 400

#: 逐行成交额自洽区间：turnover/volume/close 落在此区间 → 该行单位正确
VWAP_LOW, VWAP_HIGH = 0.5, 2.0


# ─── 数据结构 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Bar:
    trade_date: str
    volume: float
    turnover: float | None
    close: float | None


@dataclass
class FixItem:
    """一只股票 + 一张表的修复计划。"""

    stock_code: str
    boundary: str
    rows: int
    reason: str
    ratio: float | None
    pre_median: float | None
    post_median: float
    pre_turnover: float | None
    post_turnover: float | None
    guarded_rows: int = 0

    def as_dict(self, table: str) -> dict[str, Any]:
        return {
            "table": table,
            "stock_code": self.stock_code,
            "boundary": self.boundary,
            "rows": self.rows,
            "reason": self.reason,
            "ratio_post_over_pre": self.ratio,
            "pre_median_volume": self.pre_median,
            "post_median_volume": self.post_median,
            "pre_median_turnover": self.pre_turnover,
            "post_median_turnover": self.post_turnover,
            "guarded_rows": self.guarded_rows,
        }


@dataclass
class TablePlan:
    table: str
    items: list[FixItem] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stock_count(self) -> int:
        return len(self.items)

    @property
    def row_count(self) -> int:
        return sum(item.rows for item in self.items)

    @property
    def guarded_rows(self) -> int:
        return sum(item.guarded_rows for item in self.items)


@dataclass
class Options:
    since: str = DEFAULT_SINCE
    row_jump: float = 50.0
    ratio_threshold: float = 10.0
    baseline_window: int = 20
    clean_baseline_turnover: float = 0.5
    polluted_baseline_turnover: float = 0.2
    no_baseline_turnover: float = 3.0


# ─── 读数 ──────────────────────────────────────────────────────────────


def _load_circ_shares(duck: DuckDBStore) -> dict[str, float]:
    rows = duck.read_query(
        "SELECT stock_code, circ_shares FROM stock_meta "
        "WHERE circ_shares IS NOT NULL AND circ_shares > 0"
    )
    return {str(row["stock_code"]): float(row["circ_shares"]) for row in rows}


def _load_series(duck: DuckDBStore, table: str, since: str) -> dict[str, list[Bar]]:
    """读 688/689 的日线序列（含 ``since`` 之前一个基线窗口）。

    只读 SELECT；表名来自模块常量白名单，不接受外部输入。
    """
    if table not in TABLES:
        raise ValueError(f"unknown table: {table}")
    start = (date.fromisoformat(since) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    prefixes = " OR ".join(f"stock_code LIKE '{prefix}%'" for prefix in STAR_PREFIXES)
    rows = duck.read_query(
        f"""SELECT stock_code, trade_date, volume, turnover, close
            FROM {table}
            WHERE ({prefixes})
              AND trade_date >= CAST(? AS DATE)
              AND volume IS NOT NULL
            ORDER BY stock_code, trade_date""",
        [start],
    )
    series: dict[str, list[Bar]] = {}
    for row in rows:
        trade_date = row["trade_date"]
        series.setdefault(str(row["stock_code"]), []).append(
            Bar(
                trade_date=trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date),
                volume=float(row["volume"]),
                turnover=float(row["turnover"]) if row["turnover"] is not None else None,
                close=float(row["close"]) if row["close"] is not None else None,
            )
        )
    return series


# ─── 判定 ──────────────────────────────────────────────────────────────


def _is_turnover_consistent(bar: Bar) -> bool:
    """该行成交量与成交额自洽（→ 单位正确，不许再除 100）。"""
    if not bar.turnover or not bar.close or bar.volume <= 0 or bar.close <= 0:
        return False
    implied_vwap = bar.turnover / bar.volume
    ratio = implied_vwap / bar.close
    return VWAP_LOW <= ratio <= VWAP_HIGH


def _locate_boundary(sequence: Sequence[Bar], since: str, row_jump: float) -> str:
    """``since`` 之后第一根"成交量 / 上一交易日成交量 > row_jump"的 bar 的日期。"""
    for index, bar in enumerate(sequence):
        if bar.trade_date < since or index == 0:
            continue
        previous = sequence[index - 1].volume
        if previous > 0 and bar.volume / previous > row_jump:
            return bar.trade_date
    return since


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _classify(
    *,
    pre_median: float | None,
    post_median: float,
    circ: float | None,
    options: Options,
) -> tuple[bool, str]:
    """窗口中位数判据：是不是单位错（而不是真实放量）。"""
    pre_turnover = (pre_median / circ) if (pre_median and circ) else None
    post_turnover = (post_median / circ) if circ else None
    if pre_median and (pre_turnover is None or pre_turnover <= options.clean_baseline_turnover):
        return post_median / pre_median > options.ratio_threshold, "ratio"
    if pre_median and post_turnover is not None:
        return post_turnover > options.polluted_baseline_turnover, "baseline-polluted"
    if post_turnover is not None:
        return post_turnover > options.no_baseline_turnover, "no-baseline"
    return False, "undecided"


def build_table_plan(
    table: str,
    series: dict[str, list[Bar]],
    circ_shares: dict[str, float],
    options: Options,
) -> TablePlan:
    plan = TablePlan(table=table)
    for stock_code in sorted(series):
        sequence = series[stock_code]
        pre = [bar for bar in sequence if bar.trade_date < options.since][-options.baseline_window:]
        post = [bar for bar in sequence if bar.trade_date >= options.since]
        if not post:
            continue
        boundary = _locate_boundary(sequence, options.since, options.row_jump)
        segment = [bar for bar in post if bar.trade_date >= boundary][: options.baseline_window]
        if not segment:
            continue
        pre_median = _median([bar.volume for bar in pre])
        post_median = statistics.median([bar.volume for bar in segment])
        circ = circ_shares.get(stock_code)
        hit, reason = _classify(
            pre_median=pre_median, post_median=post_median, circ=circ, options=options
        )
        pre_turnover = (pre_median / circ) if (pre_median and circ) else None
        post_turnover = (post_median / circ) if circ else None
        ratio = (post_median / pre_median) if pre_median else None
        if not hit:
            plan.skipped.append(
                {
                    "stock_code": stock_code,
                    "reason": reason,
                    "rows": len(post),
                    "boundary": boundary,
                    "ratio_post_over_pre": round(ratio, 3) if ratio else None,
                    "pre_median_turnover": round(pre_turnover, 4) if pre_turnover else None,
                    "post_median_turnover": round(post_turnover, 4) if post_turnover else None,
                }
            )
            continue
        target = [bar for bar in post if bar.trade_date >= boundary]
        guarded = sum(1 for bar in target if _is_turnover_consistent(bar))
        plan.items.append(
            FixItem(
                stock_code=stock_code,
                boundary=boundary,
                rows=len(target) - guarded,
                reason=reason,
                ratio=round(ratio, 3) if ratio else None,
                pre_median=pre_median,
                post_median=post_median,
                pre_turnover=pre_turnover,
                post_turnover=post_turnover,
                guarded_rows=guarded,
            )
        )
    return plan


def build_plan(duck: DuckDBStore, options: Options) -> list[TablePlan]:
    circ_shares = _load_circ_shares(duck)
    return [
        build_table_plan(table, _load_series(duck, table, options.since), circ_shares, options)
        for table in TABLES
    ]


# ─── 只读体检（修复前/后同一口径，用于验收对照）───────────────────────


def turnover_health(duck: DuckDBStore) -> list[dict[str, Any]]:
    """科创板 vs 其他板块的 ``volume/circ_shares`` 中位数（只读 SELECT）。"""
    rows = duck.read_query(
        """WITH latest AS (SELECT MAX(trade_date) AS d FROM price_daily_raw)
           SELECT CASE WHEN p.stock_code LIKE '688%' OR p.stock_code LIKE '689%'
                       THEN 'STAR' ELSE 'OTHER' END AS board,
                  COUNT(*) AS n_rows,
                  ROUND(MEDIAN(p.volume / m.circ_shares), 4) AS median_turnover_ratio
           FROM price_daily_raw p
           JOIN stock_meta m USING (stock_code)
           JOIN latest ON p.trade_date = latest.d
           WHERE m.circ_shares > 0 AND p.volume IS NOT NULL AND p.volume > 0
           GROUP BY 1 ORDER BY 1"""
    )
    return [dict(row) for row in rows]


# ─── 写库 ──────────────────────────────────────────────────────────────


def _backup_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = root / f"maintenance-backup-star-volume-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _create_keys_table(conn: Any, table: str, plan: TablePlan) -> None:
    conn.execute(f"CREATE OR REPLACE TEMP TABLE _star_fix_{table}(stock_code VARCHAR, boundary VARCHAR)")
    if plan.items:
        conn.executemany(
            f"INSERT INTO _star_fix_{table} VALUES (?, ?)",
            [(item.stock_code, item.boundary) for item in plan.items],
        )


_VWAP_GUARD_SQL = (
    "NOT (t.turnover IS NOT NULL AND t.close IS NOT NULL AND t.close > 0 AND t.volume > 0 "
    "AND (t.turnover / t.volume) / t.close BETWEEN {low} AND {high})"
)


def _backup_table(conn: Any, table: str, plan: TablePlan, target: Path) -> int:
    """``COPY`` 受影响行（与 UPDATE 完全同一谓词）到 parquet。"""
    quoted = str(target).replace("'", "''")
    guard = _VWAP_GUARD_SQL.format(low=VWAP_LOW, high=VWAP_HIGH)
    if not plan.items:
        conn.execute(f"COPY (SELECT * FROM {table} WHERE FALSE) TO '{quoted}' (FORMAT PARQUET)")
        return 0
    conn.execute(
        f"""COPY (
                SELECT t.* FROM {table} t
                JOIN _star_fix_{table} k
                  ON k.stock_code = t.stock_code AND t.trade_date >= CAST(k.boundary AS DATE)
                WHERE t.volume IS NOT NULL AND {guard}
                ORDER BY t.stock_code, t.trade_date
            ) TO '{quoted}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )
    return conn.execute(
        f"""SELECT COUNT(*) FROM {table} t
            JOIN _star_fix_{table} k
              ON k.stock_code = t.stock_code AND t.trade_date >= CAST(k.boundary AS DATE)
            WHERE t.volume IS NOT NULL AND {guard}"""
    ).fetchone()[0]


def _apply_table(conn: Any, table: str, plan: TablePlan) -> int:
    """把受影响行的 volume 除以 100（同一事务内，逐股一次）。返回实际更新行数。"""
    guard = _VWAP_GUARD_SQL.format(low=VWAP_LOW, high=VWAP_HIGH)
    total = 0
    for item in plan.items:
        updated = conn.execute(
            f"""UPDATE {table} AS t SET volume = t.volume / {INFLATION}
                WHERE t.stock_code = ?
                  AND t.trade_date >= CAST(? AS DATE)
                  AND t.volume IS NOT NULL
                  AND {guard}""",
            [item.stock_code, item.boundary],
        ).fetchall()[0][0]
        total += int(updated)
    return total


def _recompute_avg_volume(
    conn: Any,
    stock_codes: Sequence[str],
    window: int,
    clean_turnover: float,
) -> tuple[int, list[str]]:
    """对"最近 window 行均已不再异常"的股票，把 avg_volume 除以 100。

    ``indicator_snapshot.avg_volume`` 是"最近 20 个连续交易日 raw volume 均值"。
    只有当最近 window 行的 ``volume/circ_shares`` 全部回到物理上可能的区间
    （≤ clean_turnover）时，``avg_volume / 100`` 才与该窗口全部被修等价；
    否则不猜数，交给 ``vd data compute_indicators`` 完整重算。

    返回 (已处理股票数, 需要完整重算的股票列表)。
    """
    handled = 0
    needs_full: list[str] = []
    for stock_code in stock_codes:
        row = conn.execute(
            """SELECT COUNT(*), COUNT(*) FILTER (WHERE turn > ?)
               FROM (
                   SELECT p.volume / m.circ_shares AS turn
                   FROM price_daily_raw p
                   JOIN stock_meta m USING (stock_code)
                   WHERE p.stock_code = ? AND p.volume IS NOT NULL AND m.circ_shares > 0
                   ORDER BY p.trade_date DESC
                   LIMIT ?
               ) AS recent""",
            [clean_turnover, stock_code, window],
        ).fetchone()
        total, abnormal = int(row[0]), int(row[1])
        if total == 0 or abnormal > 0:
            needs_full.append(stock_code)
            continue
        conn.execute(
            """UPDATE indicator_snapshot SET avg_volume = avg_volume / ?
               WHERE stock_code = ? AND avg_volume IS NOT NULL""",
            [INFLATION, stock_code],
        )
        handled += 1
    return handled, needs_full


def _write_plan_file(directory: Path, plans: Sequence[TablePlan], options: Options, env: str) -> Path:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": env,
        "options": options.__dict__,
        "tables": {
            plan.table: {
                "stock_count": plan.stock_count,
                "row_count": plan.row_count,
                "guarded_rows": plan.guarded_rows,
                "items": [item.as_dict(plan.table) for item in plan.items],
                "skipped": plan.skipped,
            }
            for plan in plans
        },
    }
    path = directory / "plan.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print_plan(plans: Sequence[TablePlan], *, preview: int = 5) -> None:
    for plan in plans:
        guarded = f"（另有 {plan.guarded_rows} 行被成交额自检保护，不除）" if plan.guarded_rows else ""
        logger.info(
            "%s：待修 %d 只 / %d 行%s；跳过 %d 只",
            plan.table, plan.stock_count, plan.row_count, guarded, len(plan.skipped),
        )
        for item in plan.items[:preview]:
            logger.info(
                "    %s 断点 %s 起 %d 行（%s，后/前中位比 %s，后窗中位换手 %s）",
                item.stock_code,
                item.boundary,
                item.rows,
                item.reason,
                f"{item.ratio:.1f}" if item.ratio else "-",
                f"{item.post_turnover * 100:.1f}%" if item.post_turnover else "-",
            )
        if plan.skipped:
            codes = ", ".join(f"{row['stock_code']}({row['reason']})" for row in plan.skipped[:preview])
            logger.info("    跳过样本：%s", codes)


def _log_health(duck: DuckDBStore, title: str) -> None:
    for row in turnover_health(duck):
        logger.info(
            "[%s] %s：%s 只，volume/circ_shares 中位 %s",
            title,
            row["board"],
            f"{row['n_rows']:,}",
            f"{float(row['median_turnover_ratio']):.4f}",
        )


def apply_plan(
    duck: DuckDBStore,
    paths: DatabasePathSet,
    plans: Sequence[TablePlan],
    *,
    options: Options,
    backup_root: Path,
    recompute_avg_volume: bool,
) -> Path:
    directory = _backup_dir(backup_root)
    try:
        with exclusive_update(paths.duckdb_path), duck.transaction() as conn:
            backed_up: dict[str, int] = {}
            for plan in plans:
                _create_keys_table(conn, plan.table, plan)
                backed_up[plan.table] = _backup_table(
                    conn, plan.table, plan, directory / f"{plan.table}.parquet"
                )
            _write_plan_file(directory, plans, options, paths.env.value)
            for plan in plans:
                written = _apply_table(conn, plan.table, plan)
                logger.info(
                    "[WRITE] %s：%d 行 volume / %g（备份 %d 行 → %s.parquet）",
                    plan.table, written, INFLATION, backed_up[plan.table], plan.table,
                )
            if recompute_avg_volume:
                raw_plan = next(plan for plan in plans if plan.table == "price_daily_raw")
                handled, needs_full = _recompute_avg_volume(
                    conn,
                    [item.stock_code for item in raw_plan.items],
                    options.baseline_window,
                    options.clean_baseline_turnover,
                )
                logger.info(
                    "[WRITE] indicator_snapshot.avg_volume：%d 只已除以 %g；%d 只需完整重算"
                    "（跑 `vd data compute_indicators`）",
                    handled, INFLATION, len(needs_full),
                )
                if needs_full:
                    logger.info("    需完整重算的股票：%s", ", ".join(needs_full[:20]))
    except UpdateLockError as error:
        logger.error("自动更新正在持库，稍后重试：%s", error)
        raise SystemExit(3) from error
    return directory


def restore_backup(duck: DuckDBStore, paths: DatabasePathSet, directory: Path) -> None:
    """用备份 parquet 把 volume 写回原值（写前先把当前值另存一份）。"""
    guard_dir = _backup_dir(directory.parent)
    try:
        with exclusive_update(paths.duckdb_path), duck.transaction() as conn:
            for table in TABLES:
                source = directory / f"{table}.parquet"
                if not source.exists():
                    logger.warning("备份缺少 %s，跳过", source)
                    continue
                quoted = str(source).replace("'", "''")
                conn.execute(
                    f"CREATE OR REPLACE TEMP TABLE _star_restore_{table} AS "
                    f"SELECT stock_code, trade_date, volume FROM read_parquet('{quoted}')"
                )
                current = str(guard_dir / f"{table}.before-restore.parquet").replace("'", "''")
                conn.execute(
                    f"""COPY (
                            SELECT t.* FROM {table} t
                            JOIN _star_restore_{table} r
                              ON t.stock_code = r.stock_code AND t.trade_date = r.trade_date
                            ORDER BY t.stock_code, t.trade_date
                        ) TO '{current}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
                )
                restored = int(
                    conn.execute(
                        f"""UPDATE {table} AS t SET volume = r.volume
                            FROM _star_restore_{table} r
                            WHERE t.stock_code = r.stock_code
                              AND t.trade_date = r.trade_date"""
                    ).fetchall()[0][0]
                )
                logger.info("[RESTORE] %s：%d 行 volume 复原（回滚前当前值 → %s）",
                            table, restored, current)
    except UpdateLockError as error:
        logger.error("自动更新正在持库，稍后重试：%s", error)
        raise SystemExit(3) from error
    logger.info("回滚前当前值备份目录：%s", guard_dir)
    logger.warning(
        "注意：备份只含 price_daily_* 的 volume。若当初跑过 --recompute-avg-volume，"
        "回滚后请执行 `vd data compute_indicators` 重算 indicator_snapshot.avg_volume。"
    )


# ─── CLI ───────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="修复科创板（688/689）成交量被腾讯适配器放大 100 倍的问题（reports/131 §9.2）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="断点日期：只修这一天及之后的行")
    parser.add_argument("--row-jump", type=float, default=50.0,
                        help="断点定位：相对上一交易日成交量 > 该倍数视为单位切换")
    parser.add_argument("--ratio-threshold", type=float, default=10.0,
                        help="基线可信时：后窗/前窗成交量中位数 > 该倍数判为 ×100")
    parser.add_argument("--baseline-window", type=int, default=20, help="窗口行数（前/后各取多少行）")
    parser.add_argument("--clean-baseline-turnover", type=float, default=0.5,
                        help="断点前中位换手 <= 该值视为基线可信")
    parser.add_argument("--polluted-baseline-turnover", type=float, default=0.2,
                        help="基线自身已被放大时：后窗中位换手 > 该值判为 ×100")
    parser.add_argument("--no-baseline-turnover", type=float, default=3.0,
                        help="断点后才上市（无基线）：后窗中位换手 > 该值判为 ×100")
    parser.add_argument("--recompute-avg-volume", action="store_true",
                        help="顺带重算 indicator_snapshot.avg_volume（仅整窗污染时精确）")
    parser.add_argument("--restore", metavar="DIR", default=None,
                        help="用指定备份目录把 volume 复原（同样需要 --yes）")
    parser.add_argument("--backup-root", default=str(PROJECT_ROOT / ".planning"),
                        help="备份根目录")
    parser.add_argument("--json-out", default=None, help="将判定结果另存为 JSON（只读）")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        paths = resolve_and_validate_paths()
    except PathIsolationError as error:
        logger.error("档位路径校验失败：%s", error)
        return 2
    try:
        date.fromisoformat(args.since)
    except ValueError:
        logger.error("--since 必须是 YYYY-MM-DD：%s", args.since)
        return 2

    duck = DuckDBStore(paths=paths)
    logger.info("档位 %s / 库 %s", paths.env.value, paths.duckdb_path)

    if args.restore:
        directory = Path(args.restore).expanduser()
        if not directory.is_dir():
            logger.error("备份目录不存在：%s", directory)
            return 2
        if not args.yes:
            logger.info("[DRY RUN] 将从 %s 回滚 volume；加 --yes 执行", directory)
            return 0
        restore_backup(duck, paths, directory)
        _log_health(duck, "回滚后")
        return 0

    options = Options(
        since=args.since,
        row_jump=args.row_jump,
        ratio_threshold=args.ratio_threshold,
        baseline_window=args.baseline_window,
        clean_baseline_turnover=args.clean_baseline_turnover,
        polluted_baseline_turnover=args.polluted_baseline_turnover,
        no_baseline_turnover=args.no_baseline_turnover,
    )
    _log_health(duck, "修复前")
    plans = build_plan(duck, options)
    _print_plan(plans)
    total_rows = sum(plan.row_count for plan in plans)
    if args.json_out:
        payload = {
            plan.table: {
                "stock_count": plan.stock_count,
                "row_count": plan.row_count,
                "items": [item.as_dict(plan.table) for item in plan.items],
                "skipped": plan.skipped,
            }
            for plan in plans
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("判定结果已写入 %s", args.json_out)
    if total_rows == 0:
        logger.info("没有需要修复的行（幂等：已修复过或本库无该问题）")
        return 0
    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行（写前会 COPY 备份受影响行到 %s/）", args.backup_root)
        return 0

    directory = apply_plan(
        duck,
        paths,
        plans,
        options=options,
        backup_root=Path(args.backup_root),
        recompute_avg_volume=args.recompute_avg_volume,
    )
    _log_health(duck, "修复后")
    logger.info("完成。备份与判定明细：%s", directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
