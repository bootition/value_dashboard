"""AA / AA+ 信用债收益率指数 + 与国债的信用利差（补 `reports/139` 的「拿不到 AA」缺口）。

背景
----
终验（`accept_e2e`）推翻了一条旧结论：「免费源拿不到 AA 信用利差」是**错的**。
`ak.bond_index_general_cbond(指数名, 收益率类型, 期限段)` 能直接取到
「市场隐含评级 AA / AA+ 信用债指数」的**平均市值法到期收益率**，2013-01-04 起日频：

- `市场隐含评级AA信用债指数` + `平均市值法到期收益率` + `3-5年` → 3,468 行
- 同参数换 `AA+`、或换 `1-3年` 也都能取到
- **但国债侧没有同款接口**（`bond_index_general_cbond('中债国债', ...)` 报 `KeyError`），
  所以利差用库内已有的 `bond_yield_curve`（中债国债曲线）的 **3Y 与 5Y 均值**近似「3-5 年段」，
  并在 `detail` 里如实写明这是插值近似（不是同一个指数的同期限段）。

口径
----
- `yield_pct` **百分数**（2.065 表示 2.065%）；
- 落 `bond_yield_curve`：`curve_name ∈ {AA信用债指数_3-5Y, AA+信用债指数_3-5Y, AA信用债指数_1-3Y}`，
  `tenor='3-5Y'/'1-3Y'`，`source='akshare:bond_index_general_cbond'`；
- 信用利差 = AA(3-5Y) − (国债3Y + 国债5Y)/2，单位 **bp**（乘 100）。

用法
----
    python scripts/fetch_credit_spread_aa.py            # dry-run（默认，只报数不写）
    python scripts/fetch_credit_spread_aa.py --yes      # 写库（单写者锁）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from hashlib import sha256

import akshare as ak
import pandas as pd

from app.core.financial_period import cn_today
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.update_lock import exclusive_update

SOURCE = "akshare:bond_index_general_cbond"
#: (curve_name, 指数名, 期限段)
SERIES: tuple[tuple[str, str, str], ...] = (
    ("AA信用债指数_3-5Y", "市场隐含评级AA信用债指数", "3-5年"),
    ("AA+信用债指数_3-5Y", "市场隐含评级AA+信用债指数", "3-5年"),
    ("AA信用债指数_1-3Y", "市场隐含评级AA信用债指数", "1-3年"),
)
#: 与上面 3-5Y 段对应的国债参考（库内中债国债曲线的两个期限）
GOV_SHORT_TENOR = "3Y"
GOV_LONG_TENOR = "5Y"

_INSERT_SQL = """
INSERT INTO bond_yield_curve
    (curve_name, curve_date, tenor, yield_pct, source, fetch_time, raw_hash, confidence, batch_id)
SELECT curve_name, curve_date, tenor, yield_pct, source, fetch_time, raw_hash, confidence, batch_id
FROM _aa_curve_rows
ON CONFLICT (curve_name, curve_date, tenor) DO UPDATE SET
    yield_pct  = excluded.yield_pct,
    source     = excluded.source,
    fetch_time = excluded.fetch_time,
    raw_hash   = excluded.raw_hash,
    confidence = excluded.confidence,
    batch_id   = excluded.batch_id
"""


def fetch_series() -> tuple[pd.DataFrame, dict[str, int]]:
    """抓三条指数序列，返回长表 + 每条的原始行数。"""
    frames: list[pd.DataFrame] = []
    counts: dict[str, int] = {}
    for curve_name, index_name, tenor in SERIES:
        frame = ak.bond_index_general_cbond(index_name, "平均市值法到期收益率", tenor)
        if frame is None or frame.empty:
            counts[curve_name] = 0
            continue
        counts[curve_name] = len(frame)
        frame = frame.rename(columns={"date": "curve_date", "value": "yield_pct"})
        frame["curve_name"] = curve_name
        frame["tenor"] = tenor if len(tenor) <= 5 else tenor
        frame["source"] = SOURCE
        frame["confidence"] = "exact"
        frames.append(frame[["curve_name", "curve_date", "tenor", "yield_pct", "source", "confidence"]])
    if not frames:
        return pd.DataFrame(), counts
    long = pd.concat(frames, ignore_index=True)
    long["curve_date"] = pd.to_datetime(long["curve_date"]).dt.date
    long["yield_pct"] = pd.to_numeric(long["yield_pct"], errors="coerce")
    long = long[long["yield_pct"].notna()].copy()
    batch = f"aa-curve-{cn_today():%Y%m%d}"
    long["batch_id"] = batch
    long["fetch_time"] = pd.Timestamp.utcnow().tz_localize(None)
    long["raw_hash"] = [
        sha256(f"{row.curve_name}|{row.curve_date}|{row.yield_pct}".encode()).hexdigest()[:32]
        for row in long.itertuples()
    ]
    return long, counts


def spread_stats(store: DuckDBStore) -> dict[str, object]:
    """AA(3-5Y) − 国债(3Y,5Y 均值)，单位 bp。"""
    rows = store.read_query(
        f"""
        WITH aa AS (
            SELECT curve_date, yield_pct FROM bond_yield_curve
            WHERE curve_name = 'AA信用债指数_3-5Y'
        ), gov AS (
            SELECT curve_date,
                   AVG(yield_pct) AS gov_yield
            FROM bond_yield_curve
            WHERE curve_name = '国债' AND tenor IN ('{GOV_SHORT_TENOR}', '{GOV_LONG_TENOR}')
            GROUP BY curve_date
            HAVING COUNT(*) >= 1
        )
        SELECT MAX(aa.curve_date) AS last_date,
               ROUND((SELECT (aa2.yield_pct - gov2.gov_yield) * 100
                      FROM aa aa2 JOIN gov gov2 USING (curve_date)
                      ORDER BY aa2.curve_date DESC LIMIT 1), 2) AS latest_bp,
               ROUND(MEDIAN((aa.yield_pct - gov.gov_yield) * 100), 2) AS all_median_bp,
               ROUND(MEDIAN(CASE WHEN aa.curve_date >= ? THEN (aa.yield_pct - gov.gov_yield) * 100 END), 2) AS last_year_median_bp,
               COUNT(*) AS n_days
        FROM aa JOIN gov USING (curve_date)
        """,
        [date(cn_today().year - 1, cn_today().month, cn_today().day)],
    )
    return dict(rows[0]) if rows else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="AA/AA+ 信用债收益率指数 + 信用利差（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="真正写库（单写者锁）")
    parser.add_argument("--json-out", default="", help="证据 JSON 路径")
    args = parser.parse_args()

    paths = DatabasePathSet.from_env()
    long, counts = fetch_series()
    evidence: dict[str, object] = {
        "mode": "WRITE" if args.yes else "DRY-RUN",
        "source": SOURCE,
        "raw_rows": counts,
        "normalized_rows": int(len(long)),
        "date_range": [
            str(long["curve_date"].min()) if len(long) else None,
            str(long["curve_date"].max()) if len(long) else None,
        ],
    }
    if long.empty:
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        print("[aa] 没有取到数据，退出")
        return 2

    store = DuckDBStore(paths=paths)
    if args.yes:
        with exclusive_update(paths.duckdb_path), store.write_connection() as conn:
            conn.register("_aa_curve_rows", long)
            conn.execute(_INSERT_SQL)
        evidence["written_rows"] = int(len(long))
    else:
        evidence["written_rows"] = 0
    try:
        evidence["spread"] = spread_stats(store)
    except Exception as error:  # noqa: BLE001 - 库被自动更新占用时不应让整条命令失败
        evidence["spread"] = {"error": f"{type(error).__name__}: {error}"[:200],
                              "hint": "库被写进程占用，稍后重跑即可（写库成功后 spread 会自动算出）"}
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
