"""历史研究统计域构建（P4，reports/68 §5/§6）

序列（最新重述回看口径，PRD §8.4）：
- PE(D) = close(D) × shares(D) / TTM归母净利(D 对应报告期)；PE≤0 不参与统计
- PB(D) = close(D) × shares(D) / 归母权益(D 对应报告期)
- TTM股息率(D) = D 前 12 个月已除权且已公告现金分红 / close(D)
- 利差(D) = TTM股息率(D) − 国债收益率(≤D 最近点，最大陈旧 5 日)

统计（单股时间序列，非横截面；PRD §10.7）：
- 窗口 1/3/5/10 年与全部，默认 10 年
- 经验分位带 P10/P20/P50/P80/窗口最大有效值；μ、σ、当前 z-score
- 最小样本：120/360/600/1200/1200；不足返回 null + 原因码
- PE/PB 窗口内历史股本连续可验证覆盖 ≥90% 有行情交易日，否则不可用

发布：staging → 原子发布 research_statistics（版本递增、输入指纹）；失败保留上一版。
"""

from __future__ import annotations

import hashlib
import logging
import statistics
import uuid
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = [
    "StatisticsBuilder",
    "WINDOW_MIN_SAMPLES",
    "STAT_METRICS",
    "COVERAGE_THRESHOLD_PCT",
]

WINDOW_MIN_SAMPLES: dict[int, int] = {1: 120, 3: 360, 5: 600, 10: 1200, 99: 1200}
WINDOW_YEARS = (1, 3, 5, 10, 99)
COVERAGE_THRESHOLD_PCT = 90.0  # reports/68 §3.5：历史股本连续可验证覆盖 ≥90%
MAX_STALENESS_DAYS = 5
DEFAULT_TENOR = 10.0
STAT_METRICS = ("pe_ttm", "pb_mrq", "ttm_dividend_yield", "spread_10y")


class StatisticsBuilder:
    """历史研究统计构建器（单写者，staging→原子发布）。"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError(
                "StatisticsBuilder requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("StatisticsBuilder stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite

    # ─── 序列构建 ─────────────────────────────────────────────────

    def build_series(
        self,
        stock_code: str,
        tenor: float = DEFAULT_TENOR,
    ) -> list[dict[str, Any]]:
        """构建单股逐价格日的研究序列（内存算法，单股毫秒级）。"""
        price_rows = self.duck.read_query(
            """SELECT trade_date, close FROM price_daily_raw
               WHERE stock_code = ? AND close IS NOT NULL
               ORDER BY trade_date ASC""",
            [stock_code],
        )
        if not price_rows:
            return []

        # 报告期财务（累计利润 + 归母权益）
        financial_rows = self.duck.read_query(
            """SELECT ic.report_date, ic.parent_net_profit, ic.revenue,
                      bs.total_equity_parent
               FROM income_statement ic
               LEFT JOIN balance_sheet bs
                 ON bs.stock_code = ic.stock_code AND bs.report_date = ic.report_date
               WHERE ic.stock_code = ?
               ORDER BY ic.report_date ASC""",
            [stock_code],
        )
        ttm_by_report: dict[str, float | None] = {}
        equity_by_report: dict[str, float | None] = {}
        report_dates = [str(r["report_date"])[:10] for r in financial_rows]
        for index, row in enumerate(financial_rows):
            rdate = str(row["report_date"])[:10]
            ttm_by_report[rdate] = self._ttm_profit(financial_rows, index)
            equity_by_report[rdate] = row.get("total_equity_parent")

        # 股本 step 函数
        capital_rows = self.duck.read_query(
            """SELECT effective_date, total_shares FROM share_capital_history
               WHERE stock_code = ? ORDER BY effective_date""",
            [stock_code],
        )
        capital_dates = [str(r["effective_date"])[:10] for r in capital_rows]
        capital_values = [float(r["total_shares"]) for r in capital_rows]

        # 分红
        dividend_rows = self.duck.read_query(
            """SELECT ex_date, announcement_date, dividend_per_share
               FROM dividends
               WHERE stock_code = ? AND dividend_per_share > 0
                 AND announcement_date IS NOT NULL
               ORDER BY ex_date ASC""",
            [stock_code],
        )

        # 国债曲线（tenor）
        curve_rows = self.duck.read_query(
            """SELECT curve_date, yield_pct FROM treasury_yield_curve
               WHERE tenor_years = ? ORDER BY curve_date ASC""",
            [tenor],
        )
        curve_dates = [str(r["curve_date"])[:10] for r in curve_rows]
        curve_values = [float(r["yield_pct"]) for r in curve_rows]

        series: list[dict[str, Any]] = []
        for price in price_rows:
            d = str(price["trade_date"])[:10]
            close = float(price["close"])
            if close <= 0:
                continue

            def _latest(pairs_dates: list[str], pairs_values: list[float]) -> float | None:
                idx = bisect_right(pairs_dates, d) - 1
                return pairs_values[idx] if idx >= 0 else None

            shares = _latest(capital_dates, capital_values)
            ttm_profit = _latest(report_dates, [ttm_by_report.get(rd) for rd in report_dates])
            equity = _latest(report_dates, [equity_by_report.get(rd) for rd in report_dates])
            curve = _latest(curve_dates, curve_values)
            if curve is not None:
                curve_day = curve_dates[bisect_right(curve_dates, d) - 1]
                if (date.fromisoformat(d) - date.fromisoformat(curve_day)).days > MAX_STALENESS_DAYS:
                    curve = None

            ttm_div_yield = self._ttm_div_yield(d, close, dividend_rows)

            item: dict[str, Any] = {"price_date": d, "close": close}
            if shares and ttm_profit and ttm_profit > 0:
                pe = (close * shares) / ttm_profit
                item["pe_ttm"] = pe if pe <= 1000 else None
            else:
                item["pe_ttm"] = None
            if shares and equity:
                pb = (close * shares) / equity
                item["pb_mrq"] = pb if pb <= 200 else None
            else:
                item["pb_mrq"] = None
            item["ttm_dividend_yield"] = ttm_div_yield
            item["spread_10y"] = ttm_div_yield - curve if ttm_div_yield is not None and curve is not None else None
            series.append(item)
        return series

    @staticmethod
    def _ttm_profit(rows: list[dict[str, Any]], index: int) -> float | None:
        """报告期 TTM 归母净利（复用 calculator 语义：年报直取，季报差分）。"""
        current = rows[index]
        rdate = str(current["report_date"])[:10]
        current_value = current.get("parent_net_profit")
        if current_value is None:
            return None
        if rdate.endswith("12-31"):
            return float(current_value)
        year = int(rdate[:4])
        annual_value = None
        prev_cumulative = None
        for row in rows[:index]:
            rd = str(row["report_date"])[:10]
            if rd == f"{year - 1}-12-31":
                annual_value = row.get("parent_net_profit")
            if rd == f"{year - 1}{rdate[4:]}":
                prev_cumulative = row.get("parent_net_profit")
        if annual_value is None or prev_cumulative is None:
            return None
        return float(annual_value) + float(current_value) - float(prev_cumulative)

    @staticmethod
    def _ttm_div_yield(
        price_date: str,
        close: float,
        dividend_rows: list[dict[str, Any]],
    ) -> float | None:
        """D 前 12 个月已除权且已公告现金分红 / close（%）。"""
        d = date.fromisoformat(price_date)
        cutoff = d - timedelta(days=365)
        total = 0.0
        found = False
        for row in dividend_rows:
            ex = row.get("ex_date")
            ann = row.get("announcement_date")
            dps = row.get("dividend_per_share")
            if ex is None or ann is None or dps is None:
                continue
            if isinstance(ex, str):
                ex = date.fromisoformat(ex[:10])
            if isinstance(ann, str):
                ann = date.fromisoformat(ann[:10])
            if ex > d or ann > d or ex < cutoff:
                continue
            total += float(dps)
            found = True
        return (total / close) * 100.0 if found and close > 0 else None

    # ─── 窗口统计 ─────────────────────────────────────────────────

    @staticmethod
    def window_stats(
        series: list[dict[str, Any]],
        metric: str,
        window_years: int,
        min_samples: int,
        *,
        coverage_pct: float | None = None,
    ) -> dict[str, Any]:
        """单窗口统计：经验分位带 + μ/σ + 当前 z-score。"""
        if coverage_pct is not None and coverage_pct < COVERAGE_THRESHOLD_PCT:
            return {"reason": "coverage_below_threshold", "coverage_pct": coverage_pct}
        today = date.today()
        start = today.replace(year=today.year - window_years) if window_years != 99 else None
        values: list[float] = []
        dates: list[str] = []
        current: float | None = None
        for item in series:
            d = date.fromisoformat(item["price_date"])
            if start is not None and d < start:
                continue
            value = item.get(metric)
            if value is None:
                continue
            values.append(float(value))
            dates.append(item["price_date"])
            current = float(value)
        if len(values) < min_samples:
            return {
                "reason": "insufficient_samples",
                "samples": len(values),
                "min_samples": min_samples,
            }
        values_sorted = sorted(values)
        n = len(values_sorted)

        def pct(p: float) -> float:
            return values_sorted[min(n - 1, int(p / 100.0 * n))]

        mean = statistics.fmean(values_sorted)
        sigma = statistics.stdev(values_sorted) if n > 1 else 0.0
        zscore = (current - mean) / sigma if sigma > 0 else None
        return {
            "samples": n,
            "min_date": dates[0],
            "max_date": dates[-1],
            "p10": pct(10), "p20": pct(20), "p50": pct(50),
            "p80": pct(80), "max": values_sorted[-1],
            "mean": mean, "sigma": sigma, "zscore": zscore,
            "current": current,
            "reason": None,
        }

    # ─── 全量构建 + 原子发布 ──────────────────────────────────────

    def rebuild_all(
        self,
        stock_codes: list[str] | None = None,
        *,
        max_stocks: int = 0,
        progress_cb: Any = None,
    ) -> dict[str, Any]:
        """为全部（或给定）上市股票构建统计并原子发布。

        单写者语义：全量 staging → 校验 → 原子替换 research_statistics。
        """
        if stock_codes is None:
            rows = self.duck.read_query(
                "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
            )
            stock_codes = [row["stock_code"] for row in rows]
        if max_stocks > 0:
            stock_codes = stock_codes[:max_stocks]

        fingerprint = self._input_fingerprint()
        version = self._next_version()
        records: list[dict[str, Any]] = []
        failed: list[str] = []
        for code in stock_codes:
            try:
                series = self.build_series(code)
                stats = self._stats_for_stock(code, series)
                for row in stats:
                    row["version"] = version
                    row["input_fingerprint"] = fingerprint
                    row["published_at"] = datetime.now(timezone.utc)
                records.extend(stats)
            except Exception as error:
                logger.warning("构建 %s 历史统计失败: %s", code, error)
                failed.append(code)
            if progress_cb is not None:
                progress_cb(code, {"status": "done"})

        if not records:
            return {"status": "failed", "reason": "no_records", "failed": failed}

        staging_table = f"research_statistics_staging_{uuid.uuid4().hex}"
        self.duck.write_query(
            f'CREATE TABLE "{staging_table}" AS SELECT * FROM research_statistics WHERE FALSE'
        )
        try:
            self._write_records(staging_table, records)
            with self.duck.transaction() as conn:
                count = conn.execute(f'SELECT COUNT(*) FROM "{staging_table}"').fetchone()[0]
                if count != len(records):
                    raise RuntimeError(
                        f"statistics staging mismatch: {count} != {len(records)}"
                    )
                conn.execute("DELETE FROM research_statistics")
                conn.execute(
                    f'INSERT INTO research_statistics BY NAME SELECT * FROM "{staging_table}"'
                )
        finally:
            self.duck.write_query(f'DROP TABLE IF EXISTS "{staging_table}"')

        return {
            "status": "success" if not failed else "partial",
            "targeted": len(stock_codes),
            "records": len(records),
            "version": version,
            "failed": failed[:20],
            "fingerprint": fingerprint,
        }

    def _stats_for_stock(
        self,
        stock_code: str,
        series: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not series:
            return []
        # 覆盖核验（PE/PB 依赖股本）：窗口内有行情日中股本可验证的比例
        coverage = self._capital_coverage(stock_code)
        records: list[dict[str, Any]] = []
        for metric in STAT_METRICS:
            for window in WINDOW_YEARS:
                min_samples = WINDOW_MIN_SAMPLES[window]
                cap = coverage if metric in ("pe_ttm", "pb_mrq") else None
                stats = self.window_stats(series, metric, window, min_samples, coverage_pct=cap)
                if stats.get("reason") is not None:
                    records.append({
                        "stock_code": stock_code, "metric": metric,
                        "window_years": window, "method": "percentile",
                        "value": None, "samples": stats.get("samples"),
                        "coverage_pct": stats.get("coverage_pct"),
                        "reason": stats.get("reason"),
                    })
                    records.append({
                        "stock_code": stock_code, "metric": metric,
                        "window_years": window, "method": "zscore",
                        "value": None, "samples": stats.get("samples"),
                        "coverage_pct": stats.get("coverage_pct"),
                        "reason": stats.get("reason"),
                    })
                    continue
                # percentile = 当前值在窗口内经验分布中的分位（0-100）
                rank = sum(1 for v in series_values(series, metric, window) if v <= stats["current"])
                total = stats["samples"]
                percentile = (rank / total) * 100.0 if total else None
                records.append({
                    "stock_code": stock_code, "metric": metric,
                    "window_years": window, "method": "percentile",
                    "value": percentile,
                    "samples": stats["samples"],
                    "coverage_pct": coverage,
                    "min_date": stats["min_date"], "max_date": stats["max_date"],
                    "reason": None,
                })
                records.append({
                    "stock_code": stock_code, "metric": metric,
                    "window_years": window, "method": "zscore",
                    "value": stats["zscore"],
                    "samples": stats["samples"],
                    "coverage_pct": coverage,
                    "min_date": stats["min_date"], "max_date": stats["max_date"],
                    "reason": None,
                })
        return records

    def _capital_coverage(self, stock_code: str) -> float:
        """窗口内有行情日的股本可验证覆盖（近似：主链首点至最新点视为连续）。"""
        price_count = self.duck.read_query(
            "SELECT COUNT(*) AS c FROM price_daily_raw "
            "WHERE stock_code = ? AND close IS NOT NULL",
            [stock_code],
        )[0]["c"]
        capital_count = self.duck.read_query(
            "SELECT COUNT(*) AS c FROM share_capital_history WHERE stock_code = ?",
            [stock_code],
        )[0]["c"]
        if not price_count:
            return 0.0
        if not capital_count:
            return 0.0
        # 主链覆盖从首个锚点至窗口末；前段（上市初期无记录）按缺失计
        first_capital = self.duck.read_query(
            "SELECT MIN(effective_date) AS d FROM share_capital_history WHERE stock_code = ?",
            [stock_code],
        )[0]["d"]
        first_price = self.duck.read_query(
            "SELECT MIN(trade_date) AS d FROM price_daily_raw "
            "WHERE stock_code = ? AND close IS NOT NULL",
            [stock_code],
        )[0]["d"]
        if not first_capital or not first_price:
            return 100.0 if capital_count else 0.0
        before = self.duck.read_query(
            "SELECT COUNT(*) AS c FROM price_daily_raw "
            "WHERE stock_code = ? AND close IS NOT NULL AND trade_date < ?",
            [stock_code, str(first_capital)[:10]],
        )[0]["c"]
        return max(0.0, min(100.0, (price_count - before) / price_count * 100.0))

    def _input_fingerprint(self) -> str:
        """输入指纹：价格/财务/股本/曲线最新日期与行数摘要。"""
        parts: list[str] = []
        date_columns = {
            "price_daily_raw": "trade_date",
            "income_statement": "report_date",
            "balance_sheet": "report_date",
            "share_capital_history": "effective_date",
            "treasury_yield_curve": "curve_date",
        }
        for table, date_column in date_columns.items():
            row = self.duck.read_query(
                f"SELECT COUNT(*) AS c, MAX({date_column}) AS latest FROM {table}"
            )[0]
            parts.append(f"{table}:{row['c']}:{str(row['latest'])[:10]}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _next_version(self) -> int:
        row = self.duck.read_query(
            "SELECT COALESCE(MAX(version), 0) AS v FROM research_statistics"
        )[0]
        return int(row["v"]) + 1

    def _write_records(self, staging_table: str, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        placeholders = ", ".join(["?"] * len(records[0]))
        columns = ", ".join(records[0].keys())
        with self.duck.write_connection() as conn:
            conn.executemany(
                f'INSERT INTO "{staging_table}" ({columns}) VALUES ({placeholders})',
                [[row.get(col) for col in records[0]] for row in records],
            )


def series_values(
    series: list[dict[str, Any]],
    metric: str,
    window_years: int,
) -> list[float]:
    """窗口内有效值序列（供分位计算）。"""
    today = date.today()
    start = today.replace(year=today.year - window_years) if window_years != 99 else None
    out: list[float] = []
    for item in series:
        d = date.fromisoformat(item["price_date"])
        if start is not None and d < start:
            continue
        value = item.get(metric)
        if value is not None:
            out.append(float(value))
    return out
