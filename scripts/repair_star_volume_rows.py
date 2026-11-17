"""按**逐行硬证据**修复科创板成交量量纲（reports/135 §P0-1 收尾，2026-11-17）。

为什么需要第二个脚本
--------------------
`repair_star_volume_x100.py` 用「断点前后 20 行成交量中位数之比」判定单位错。
这个判据在**前窗已被污染**时失效（实测 688565–688569：前 20 行里 10 行已 ×100，
中位数被拉低，`post/pre` 只有 2–4，两个分支都不触发 → 被跳过）。
它也只能处理"某日之后整段"，处理不了"只有某几天错"和"整段历史错"。

本脚本改用**逐行硬证据**，不再依赖任何滑动窗口：

判据 A（成交额恒等式，只对 `price_daily_raw` 有效）
    单行满足 ``turnover / (volume * close) ∈ [0.005, 0.05]``
    ⇒ 该行 volume 是真实股数的约 100 倍。
    原理：成交额是真的（钱不会撒谎），``成交额 ≈ 成交量 × 成交均价``；
    若 volume 被放大 100 倍而 turnover 没被放大，比值就会掉到约 0.01。
    实测正常行为 0.98–1.02，污染行为 0.0097–0.0105，中间**没有任何观测值**。

判据 B（双表比值，只对 `price_daily_qfq` 有效）
    ``qfq.volume / raw.volume ≈ 100`` ⇒ qfq 侧 ×100；
    ``≈ 0.01``                          ⇒ qfq 侧被误除过 100。
    前置事实（本脚本 `--audit` 会重新验证）：全库 qfq/raw 成交量比值只落在
    1.00（664,040 行）与 ~100（87,336 行）两簇，**5–59 区间一行都没有** ——
    即 qfq 从不按复权因子缩放成交量。因此 ~100 只能是单位错，不会是"10 送 1"。

为什么必须跑两遍
----------------
688139 是 raw 与 qfq **同时** ×100（两表彼此一致、都与成交额矛盾）。
第一遍（判据 A）只修 raw，修完 raw/qfq 变成 100 → 第二遍（判据 B）才修到 qfq。
脚本按「修 raw → 重算比值 → 修 qfq → 终检」单次执行完成这两遍。

终检（不通过就以退出码 4 失败）
------------------------------
修完后逐股复查：raw 侧不允许再有成交额比值落在污染区间；
raw 与 qfq 的成交量比值不允许再有 >50 或 <0.02 的行。
"修完还留不一致却报成功"是本项目反复出现过的事故形态，故此处硬失败。

用法
----
    python scripts/repair_star_volume_rows.py              # dry-run + 审计
    python scripts/repair_star_volume_rows.py --audit       # 只看分布，不给修复计划
    python scripts/repair_star_volume_rows.py --yes         # 写库（备份 + 写锁 + 单事务）
    python scripts/repair_star_volume_rows.py --yes --recompute-avg-volume
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

STAR_PREFIXES = ("688", "689")

# 判据 A：成交额恒等式比值落在这些区间即判定 volume ×100 / ÷100。
# 区间刻意留得窄：实测正常 0.92–1.09，污染 0.0097–0.0105，中间无观测。
VWAP_POLLUTED = (0.005, 0.05)
VWAP_CLEAN = (0.5, 2.0)

# 判据 B：qfq 与 raw 的成交量比值。
CROSS_X100 = (50.0, 200.0)
CROSS_DIV100 = (0.005, 0.02)

STAR_FILTER = " AND ".join(f"stock_code LIKE '{p}%'" for p in STAR_PREFIXES)


def _star_where(alias: str) -> str:
    return "(" + " OR ".join(f"{alias}.stock_code LIKE '{p}%'" for p in STAR_PREFIXES) + ")"


def audit(duck: DuckDBStore) -> None:
    """打印两侧量纲证据分布（判据本身的现场验证）。"""
    logger.info("=== 判据 A：raw 成交额恒等式比值分布 ===")
    for row in duck.read_query(
        f"""
        SELECT CASE
                 WHEN turnover/(volume*close) BETWEEN 0.005 AND 0.05 THEN '~0.01 (raw x100)'
                 WHEN turnover/(volume*close) BETWEEN 0.5   AND 2.0  THEN '~1.00 (正常)'
                 ELSE '其他' END AS bucket,
               COUNT(*) AS n
        FROM price_daily_raw
        WHERE {_star_where('price_daily_raw')}
          AND turnover IS NOT NULL AND volume > 0 AND close > 0
        GROUP BY 1 ORDER BY n DESC
        """
    ):
        logger.info("  %-22s %8d", row["bucket"], row["n"])

    logger.info("=== 判据 B：qfq/raw 成交量比值分布 ===")
    for row in duck.read_query(
        f"""
        WITH x AS (
            SELECT q.volume / r.volume AS ratio
            FROM price_daily_raw r JOIN price_daily_qfq q USING(stock_code, trade_date)
            WHERE {_star_where('r')} AND r.volume > 0 AND q.volume > 0
        )
        SELECT CASE
                 WHEN ratio BETWEEN 0.995 AND 1.005 THEN '~1.00 (一致)'
                 WHEN ratio BETWEEN 90    AND 110   THEN '~100 (qfq x100)'
                 WHEN ratio BETWEEN 0.009 AND 0.011 THEN '~0.01 (qfq /100)'
                 ELSE '其他' END AS bucket,
               COUNT(*) AS n
        FROM x GROUP BY 1 ORDER BY n DESC
        """
    ):
        logger.info("  %-22s %8d", row["bucket"], row["n"])


def plan_raw(duck: DuckDBStore) -> list[dict]:
    """判据 A：成交额证伪的 raw 行。"""
    return duck.read_query(
        f"""
        SELECT stock_code,
               COUNT(*) AS rows,
               MIN(trade_date) AS first_date,
               MAX(trade_date) AS last_date
        FROM price_daily_raw
        WHERE {_star_where('price_daily_raw')}
          AND turnover IS NOT NULL AND volume > 0 AND close > 0
          AND turnover/(volume*close) BETWEEN ? AND ?
        GROUP BY 1 ORDER BY rows DESC
        """,
        list(VWAP_POLLUTED),
    )


def plan_qfq(duck: DuckDBStore) -> list[dict]:
    """判据 B：与 raw 相差约 100 倍的 qfq 行（在 raw 修完之后计算）。"""
    return duck.read_query(
        f"""
        SELECT r.stock_code,
               COUNT(*) FILTER (WHERE q.volume/r.volume BETWEEN ? AND ?) AS x100_rows,
               COUNT(*) FILTER (WHERE q.volume/r.volume BETWEEN ? AND ?) AS div100_rows,
               MIN(r.trade_date) AS first_date,
               MAX(r.trade_date) AS last_date
        FROM price_daily_raw r JOIN price_daily_qfq q USING(stock_code, trade_date)
        WHERE {_star_where('r')} AND r.volume > 0 AND q.volume > 0
        GROUP BY 1
        HAVING x100_rows > 0 OR div100_rows > 0
        ORDER BY x100_rows DESC, div100_rows DESC
        """,
        [*CROSS_X100, *CROSS_DIV100],
    )


def terminal_check(duck: DuckDBStore) -> dict:
    """终检：两侧都不允许再有量纲离群行。"""
    raw_bad = duck.read_query(
        f"""
        SELECT COUNT(*) AS n FROM price_daily_raw
        WHERE {_star_where('price_daily_raw')}
          AND turnover IS NOT NULL AND volume > 0 AND close > 0
          AND turnover/(volume*close) BETWEEN ? AND ?
        """,
        list(VWAP_POLLUTED),
    )[0]["n"]
    cross_bad = duck.read_query(
        f"""
        SELECT COUNT(*) AS n
        FROM price_daily_raw r JOIN price_daily_qfq q USING(stock_code, trade_date)
        WHERE {_star_where('r')} AND r.volume > 0 AND q.volume > 0
          AND (q.volume/r.volume > 50 OR q.volume/r.volume < 0.02)
        """
    )[0]["n"]
    return {"raw_vwap_outliers": int(raw_bad), "cross_unit_outliers": int(cross_bad)}


def recompute_avg_volume(conn, codes: list[str]) -> int:
    """受影响股票的 20 日均量重算（更换量纲后必须同步，否则快照仍带旧量纲）。"""
    if not codes:
        return 0
    slots = ", ".join("?" for _ in codes)
    # DuckDB UPDATE 不给 rowcount；用同一谓词先数目标行数。
    updated = int(
        conn.execute(
            f"SELECT COUNT(*) FROM indicator_snapshot "
            f"WHERE stock_code IN ({slots}) AND avg_volume IS NOT NULL",
            codes,
        ).fetchone()[0]
        or 0
    )
    conn.execute(
        f"""
        WITH w AS (
            SELECT p.stock_code, AVG(p.volume) AS avg_v
            FROM (
                SELECT stock_code, volume,
                       ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
                FROM price_daily_raw
                WHERE stock_code IN ({slots})
            ) p
            WHERE p.rn <= 20
            GROUP BY p.stock_code
        )
        UPDATE indicator_snapshot AS s SET avg_volume = w.avg_v
        FROM w WHERE s.stock_code = w.stock_code AND s.avg_volume IS NOT NULL
        """,
        codes,
    )
    return updated


def main() -> None:
    ap = argparse.ArgumentParser(description="按逐行硬证据修复科创板成交量量纲")
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run）")
    ap.add_argument("--audit", action="store_true", help="只打印量纲分布，不给修复计划")
    ap.add_argument(
        "--recompute-avg-volume",
        action="store_true",
        help="同时重算受影响股票的 indicator_snapshot.avg_volume",
    )
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    duck = DuckDBStore(paths=paths)
    audit(duck)
    if args.audit:
        return

    raw_plan = plan_raw(duck)
    logger.info("=== raw 侧待修：%d 只 / %d 行（成交额证伪）===",
                len(raw_plan), sum(int(r["rows"]) for r in raw_plan))
    for row in raw_plan:
        logger.info("  %s  %5d 行  %s ~ %s",
                    row["stock_code"], row["rows"], row["first_date"], row["last_date"])

    qfq_plan = plan_qfq(duck)
    logger.info("=== qfq 侧待修（按修复前比值预估）：%d 只 / %d 行 ===",
                len(qfq_plan),
                sum(int(r["x100_rows"]) + int(r["div100_rows"]) for r in qfq_plan))
    for row in qfq_plan[:10]:
        logger.info("  %s  x100=%d  div100=%d",
                    row["stock_code"], row["x100_rows"], row["div100_rows"])
    if len(qfq_plan) > 10:
        logger.info("  …… 其余 %d 只略", len(qfq_plan) - 10)

    pre = terminal_check(duck)
    logger.info("修复前终检：%s", pre)

    if not args.yes:
        logger.info("[DRY RUN] 未写库；加 --yes 执行")
        return

    backup_dir = (
        PROJECT_ROOT / ".planning" / f"maintenance-backup-star-volume-rows-{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    affected: set[str] = {str(r["stock_code"]) for r in raw_plan}
    stats: dict[str, int] = {}

    try:
        with exclusive_update(paths.duckdb_path), duck.write_connection() as conn:
            for table in ("price_daily_raw", "price_daily_qfq"):
                target = str(backup_dir / f"{table}.parquet").replace("'", "''")
                conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            logger.info("[BACKUP] %s", backup_dir)

            def _count(sql: str, params: list) -> int:
                """DuckDB 的 UPDATE 不返回 rowcount（实测 -1），只能先按同一谓词数一遍。"""
                return int(conn.execute(sql, params).fetchone()[0] or 0)

            raw_where = f"""
                {_star_where('price_daily_raw')}
                  AND turnover IS NOT NULL AND volume > 0 AND close > 0
                  AND turnover/(volume*close) BETWEEN ? AND ?
            """
            # —— 第一遍：判据 A 修 raw ——
            stats["raw_rows_fixed"] = _count(
                f"SELECT COUNT(*) FROM price_daily_raw WHERE {raw_where}",
                list(VWAP_POLLUTED),
            )
            conn.execute(
                f"UPDATE price_daily_raw SET volume = volume / 100.0 WHERE {raw_where}",
                list(VWAP_POLLUTED),
            )

            # —— 第二遍：判据 B 修 qfq（此时 raw 已正确，比值才有意义）——
            qfq_join = f"""
                FROM price_daily_raw AS r
                JOIN price_daily_qfq AS q
                  ON q.stock_code = r.stock_code AND q.trade_date = r.trade_date
                WHERE {_star_where('r')} AND r.volume > 0 AND q.volume > 0
                  AND q.volume / r.volume BETWEEN ? AND ?
            """
            stats["qfq_rows_div100"] = _count("SELECT COUNT(*) " + qfq_join, list(CROSS_X100))
            conn.execute(
                "UPDATE price_daily_qfq AS q SET volume = q.volume / 100.0 "
                "FROM price_daily_raw AS r "
                "WHERE q.stock_code = r.stock_code AND q.trade_date = r.trade_date "
                f"AND {_star_where('r')} AND r.volume > 0 AND q.volume > 0 "
                "AND q.volume / r.volume BETWEEN ? AND ?",
                list(CROSS_X100),
            )
            stats["qfq_rows_x100"] = _count("SELECT COUNT(*) " + qfq_join, list(CROSS_DIV100))
            conn.execute(
                "UPDATE price_daily_qfq AS q SET volume = q.volume * 100.0 "
                "FROM price_daily_raw AS r "
                "WHERE q.stock_code = r.stock_code AND q.trade_date = r.trade_date "
                f"AND {_star_where('r')} AND r.volume > 0 AND q.volume > 0 "
                "AND q.volume / r.volume BETWEEN ? AND ?",
                list(CROSS_DIV100),
            )

            for row in qfq_plan:
                affected.add(str(row["stock_code"]))

            if args.recompute_avg_volume:
                target_codes = sorted(affected)
                stats["avg_volume_targets"] = len(target_codes)
                stats["avg_volume_rows"] = recompute_avg_volume(conn, target_codes)

        post = terminal_check(duck)
        logger.info("修复结果：%s", stats)
        logger.info("修复后终检：%s", post)
        if post["raw_vwap_outliers"] or post["cross_unit_outliers"]:
            logger.error(
                "终检未通过：仍有 raw 成交额离群 %d 行、双表量纲离群 %d 行。"
                "不报成功——请检查判据区间或是否存在第三种错法。",
                post["raw_vwap_outliers"], post["cross_unit_outliers"],
            )
            raise SystemExit(4)
        logger.info("终检通过：raw 与 qfq 量纲一致，且与成交额自洽。")
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        raise SystemExit(3) from error


if __name__ == "__main__":
    main()
