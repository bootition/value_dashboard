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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# Windows spawn 进程池的 worker 全局状态（见 _statistics_worker_init）。
_WORKER_STORE: DuckDBStore | None = None
_WORKER_SQLITE: SQLiteStore | None = None
_WORKER_BUILDER: StatisticsBuilder | None = None

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
        # 国债曲线对同一 tenor 完全相同，却在逐股 build_series 中被重复
        # 查询/解析数千次；进程内缓存后，统计域重建只读一次曲线。
        self._curve_cache: dict[float, tuple[list[date], list[float]]] = {}
        self._batch_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._batch_price_days: dict[str, list[str]] = {}
        self._batch_capital_rows: dict[str, list[dict[str, Any]]] = {}
        self._batch_sentinel = object()

    # ─── 序列构建 ─────────────────────────────────────────────────

    def prime_batch(self, stock_codes: list[str]) -> None:
        """预取一批股票的统计输入，避免逐股重复打开 DuckDB 查询。

        只做批量取数，不改任何指标口径；build_series / _stats_for_stock
        命中缓存时直接复用，未命中股票仍走原逐股查询。
        """
        if not stock_codes:
            return
        placeholders = ", ".join("?" for _ in stock_codes)
        params: list[Any] = list(stock_codes)
        self._batch_cache = {"price": {}, "financial": {}, "capital": {}, "dividend": {}}
        self._batch_price_days = {}
        self._batch_capital_rows = {}
        for row in self.duck.read_query(
            f"""SELECT stock_code, trade_date, close FROM price_daily_raw
                WHERE stock_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY stock_code, trade_date ASC""",
            params,
        ):
            self._batch_cache["price"].setdefault(str(row["stock_code"]), []).append(row)
        for row in self.duck.read_query(
            f"""SELECT ic.stock_code, ic.report_date, ic.parent_net_profit,
                       ic.revenue, bs.total_equity_parent
                FROM income_statement ic
                LEFT JOIN balance_sheet bs
                  ON bs.stock_code = ic.stock_code
                 AND bs.report_date = ic.report_date
                WHERE ic.stock_code IN ({placeholders})
                ORDER BY ic.stock_code, ic.report_date ASC""",
            params,
        ):
            self._batch_cache["financial"].setdefault(str(row["stock_code"]), []).append(row)
        for row in self.duck.read_query(
            f"""SELECT stock_code, effective_date, total_shares
                FROM share_capital_history
                WHERE stock_code IN ({placeholders})
                ORDER BY stock_code, effective_date ASC""",
            params,
        ):
            self._batch_cache["capital"].setdefault(str(row["stock_code"]), []).append(row)
        for row in self.duck.read_query(
            f"""SELECT stock_code, ex_date, announcement_date, dividend_per_share
                FROM dividends
                WHERE stock_code IN ({placeholders}) AND dividend_per_share > 0
                  AND announcement_date IS NOT NULL
                ORDER BY stock_code, ex_date ASC""",
            params,
        ):
            self._batch_cache["dividend"].setdefault(str(row["stock_code"]), []).append(row)
        for row in self.duck.read_query(
            f"""SELECT stock_code, trade_date FROM price_daily_raw
                WHERE stock_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY stock_code, trade_date ASC""",
            params,
        ):
            self._batch_price_days.setdefault(str(row["stock_code"]), []).append(
                str(row["trade_date"])[:10]
            )
        for row in self.duck.read_query(
            f"""SELECT stock_code, effective_date FROM share_capital_history
                WHERE stock_code IN ({placeholders})
                ORDER BY stock_code, effective_date ASC""",
            params,
        ):
            self._batch_capital_rows.setdefault(str(row["stock_code"]), []).append(row)

    def build_series(
        self,
        stock_code: str,
        tenor: float = DEFAULT_TENOR,
    ) -> list[dict[str, Any]]:
        """构建单股逐价格日的研究序列（内存算法）。

        reports/73 修复阶段性能优化：日期一次性预解析为 date 对象，
        分红预解析并按 ex_date 排序（单指针滑窗，不再逐日全表扫描）。
        """
        price_rows = self._batch_cache.get("price", {}).get(stock_code, self._batch_sentinel)
        if price_rows is self._batch_sentinel:
            price_rows = self.duck.read_query(
                """SELECT trade_date, close FROM price_daily_raw
                   WHERE stock_code = ? AND close IS NOT NULL
                   ORDER BY trade_date ASC""",
                [stock_code],
            )
        if not price_rows:
            return []

        # 报告期财务（累计利润 + 归母权益）
        financial_rows = self._batch_cache.get("financial", {}).get(stock_code, self._batch_sentinel)
        if financial_rows is self._batch_sentinel:
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
        ttm_by_report: dict[date, float | None] = {}
        equity_by_report: dict[date, float | None] = {}
        report_dates: list[date] = []
        for index, row in enumerate(financial_rows):
            rdate = date.fromisoformat(str(row["report_date"])[:10])
            report_dates.append(rdate)
            ttm_by_report[rdate] = self._ttm_profit(financial_rows, index)
            equity_by_report[rdate] = row.get("total_equity_parent")

        # 股本 step 函数
        capital_rows = self._batch_cache.get("capital", {}).get(stock_code, self._batch_sentinel)
        if capital_rows is self._batch_sentinel:
            capital_rows = self.duck.read_query(
                """SELECT effective_date, total_shares FROM share_capital_history
                   WHERE stock_code = ? ORDER BY effective_date""",
                [stock_code],
            )
        capital_dates = [date.fromisoformat(str(r["effective_date"])[:10]) for r in capital_rows]
        capital_values = [float(r["total_shares"]) for r in capital_rows]

        # 分红（预解析 + 排序，遍历时按 ex_date 滑窗）
        dividend_rows = self._batch_cache.get("dividend", {}).get(stock_code, self._batch_sentinel)
        if dividend_rows is self._batch_sentinel:
            dividend_rows = self.duck.read_query(
                """SELECT ex_date, announcement_date, dividend_per_share
                   FROM dividends
                   WHERE stock_code = ? AND dividend_per_share > 0
                     AND announcement_date IS NOT NULL
                   ORDER BY ex_date ASC""",
                [stock_code],
            )
        dividend_ex: list[date] = []
        dividend_ann: list[date] = []
        dividend_dps: list[float] = []
        for row in dividend_rows:
            dividend_ex.append(date.fromisoformat(str(row["ex_date"])[:10]))
            dividend_ann.append(date.fromisoformat(str(row["announcement_date"])[:10]))
            dividend_dps.append(float(row["dividend_per_share"]))

        # 国债曲线（tenor）：同 tenor 全市场共享，缓存避免逐股重复读取。
        cached_curve = self._curve_cache.get(tenor)
        if cached_curve is None:
            curve_rows = self.duck.read_query(
                """SELECT curve_date, yield_pct FROM treasury_yield_curve
                   WHERE tenor_years = ? ORDER BY curve_date ASC""",
                [tenor],
            )
            cached_curve = (
                [date.fromisoformat(str(r["curve_date"])[:10]) for r in curve_rows],
                [float(r["yield_pct"]) for r in curve_rows],
            )
            self._curve_cache[tenor] = cached_curve
        curve_dates, curve_values = cached_curve

        ttm_profit_values = [ttm_by_report.get(rd) for rd in report_dates]
        equity_values = [equity_by_report.get(rd) for rd in report_dates]

        series: list[dict[str, Any]] = []
        div_lo = 0
        for price in price_rows:
            d = date.fromisoformat(str(price["trade_date"])[:10])
            close = float(price["close"])
            if close <= 0:
                continue

            # B023: 循环变量 d 通过默认参数绑定，避免晚绑定误读
            def _latest(
                pairs_dates: list[date],
                pairs_values: list[float],
                _anchor: date = d,
            ) -> float | None:
                idx = bisect_right(pairs_dates, _anchor) - 1
                return pairs_values[idx] if idx >= 0 else None

            shares = _latest(capital_dates, capital_values)
            ttm_profit = _latest(report_dates, ttm_profit_values)
            equity = _latest(report_dates, equity_values)
            curve = _latest(curve_dates, curve_values)
            if curve is not None:
                curve_day = curve_dates[bisect_right(curve_dates, d) - 1]
                if (d - curve_day).days > MAX_STALENESS_DAYS:
                    curve = None

            # TTM 分红：ex_date ∈ [d-365, d] 且 announcement <= d（单指针滑窗）
            cutoff = d - timedelta(days=365)
            while div_lo < len(dividend_ex) and dividend_ex[div_lo] < cutoff:
                div_lo += 1
            total_dps = 0.0
            found = False
            for i in range(div_lo, len(dividend_ex)):
                ex = dividend_ex[i]
                if ex > d:
                    break
                if dividend_ann[i] <= d:
                    total_dps += dividend_dps[i]
                    found = True
            ttm_div_yield = (total_dps / close) * 100.0 if found and close > 0 else None

            item: dict[str, Any] = {"price_date": str(d), "close": close}
            if shares and ttm_profit and ttm_profit > 0:
                pe = (close * shares) / ttm_profit
                item["pe_ttm"] = pe if pe <= 1000 else None
            else:
                item["pe_ttm"] = None
            if shares and equity and equity > 0:
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
        parallel: int = 0,
        merge: bool = False,
    ) -> dict[str, Any]:
        """为全部（或给定）上市股票构建统计并原子发布。

        单写者语义：全量 staging → 校验 → 原子替换 research_statistics。
        parallel>1 且目标 ≥200 时用进程池并行构建（只读分析；
        发布仍在主进程原子完成）。
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
        published_at = datetime.now(UTC)

        def _finish(code: str, stats: list[dict[str, Any]]) -> None:
            for row in stats:
                row["version"] = version
                row["input_fingerprint"] = fingerprint
                row["published_at"] = published_at
            records.extend(stats)

        if parallel > 1 and len(stock_codes) >= 200:
            import concurrent.futures

            completed = 0
            chunk_size = max(50, min(250, 20000 // max(parallel, 1)))
            chunks = [
                stock_codes[index:index + chunk_size]
                for index in range(0, len(stock_codes), chunk_size)
            ]
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=parallel,
                initializer=_statistics_worker_init,
                initargs=(str(self.duck.db_path), str(self.sqlite.db_path)),
            ) as executor:
                future_to_chunk = {
                    executor.submit(_statistics_worker_build_chunk, chunk): chunk
                    for chunk in chunks
                }
                for future in concurrent.futures.as_completed(future_to_chunk):
                    for code, stats, error in future.result():
                        if error is not None:
                            logger.warning("构建 %s 历史统计失败: %s", code, error)
                            failed.append(code)
                        else:
                            _finish(code, stats)
                        completed += 1
                        if progress_cb is not None:
                            progress_cb(code, {
                                "status": "done",
                                "done": completed,
                                "total": len(stock_codes),
                            })
        else:
            for completed, code in enumerate(stock_codes, start=1):
                try:
                    series = self.build_series(code)
                    _finish(code, self._stats_for_stock(code, series))
                except Exception as error:
                    logger.warning("构建 %s 历史统计失败: %s", code, error)
                    failed.append(code)
                if progress_cb is not None:
                    progress_cb(code, {
                        "status": "done",
                        "done": completed,
                        "total": len(stock_codes),
                    })

        if not records:
            return {"status": "failed", "reason": "no_records", "failed": failed}

        staging_table = f"research_statistics_staging_{uuid.uuid4().hex}"
        self._cleanup_staging_tables()
        self.duck.write_query(
            f'CREATE TABLE "{staging_table}" AS SELECT * FROM research_statistics WHERE FALSE'
        )
        success_codes = [code for code in stock_codes if code not in set(failed)]
        try:
            self._write_records(staging_table, records)
            if merge:
                self._publish_statistics_merge(
                    staging_table, records, success_codes,
                )
            else:
                with self.duck.transaction() as conn:
                    count = conn.execute(
                        f'SELECT COUNT(*) FROM "{staging_table}"'
                    ).fetchone()[0]
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
            "failed_count": len(failed),
            "success_codes": success_codes,
            "fingerprint": fingerprint,
        }

    def _publish_statistics_merge(
        self, staging_table: str, records: list[dict[str, Any]],
        stock_codes: list[str],
    ) -> None:
        """Replace research_statistics rows only for successfully rebuilt stocks."""
        if not stock_codes or not records:
            return
        placeholders = ", ".join("?" for _ in stock_codes)
        with self.duck.transaction() as conn:
            previous_total = conn.execute(
                "SELECT COUNT(*) FROM research_statistics"
            ).fetchone()[0]
            affected_count = conn.execute(
                f"SELECT COUNT(*) FROM research_statistics "
                f"WHERE stock_code IN ({placeholders})",
                stock_codes,
            ).fetchone()[0]
            conn.execute(
                f"DELETE FROM research_statistics WHERE stock_code IN ({placeholders})",
                stock_codes,
            )
            conn.execute(
                f'INSERT INTO research_statistics BY NAME SELECT * FROM "{staging_table}"'
            )
            row_count = conn.execute(
                "SELECT COUNT(*) FROM research_statistics"
            ).fetchone()[0]
            expected = previous_total - affected_count + len(records)
            if row_count != expected:
                raise RuntimeError(
                    f"statistics merge mismatch: rows={row_count}, expected={expected}"
                )
    _STOCK_STATE_TABLE = "research_statistics_stock_state"

    def _ensure_stock_state_table(self) -> None:
        """Per-stock input fingerprint store for incremental rebuild."""
        with self.sqlite.transaction() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self._STOCK_STATE_TABLE} (
                    stock_code TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def _stock_fingerprints(self, stock_codes: list[str]) -> dict[str, str]:
        """Compute one fingerprint per stock from the statistics input tables.

        Treasury curve and verified-share counts are global inputs, so a
        treasury-curve change marks every stock changed (correct: spread_10y
        depends on the curve).
        """
        rows = self.duck.read_query(
            """SELECT m.stock_code,
                      (SELECT COALESCE(CAST(MAX(p.trade_date) AS VARCHAR), '')
                         FROM price_daily_raw p WHERE p.stock_code = m.stock_code) AS price_date,
                      (SELECT COUNT(*) FROM price_daily_raw p
                         WHERE p.stock_code = m.stock_code) AS price_cnt,
                      (SELECT COALESCE(CAST(MAX(bs.report_date) AS VARCHAR), '')
                         FROM balance_sheet bs WHERE bs.stock_code = m.stock_code) AS bs_date,
                      (SELECT COUNT(*) FROM balance_sheet bs
                         WHERE bs.stock_code = m.stock_code) AS bs_cnt,
                      (SELECT COALESCE(CAST(MAX(ic.report_date) AS VARCHAR), '')
                         FROM income_statement ic WHERE ic.stock_code = m.stock_code) AS ic_date,
                      (SELECT COUNT(*) FROM income_statement ic
                         WHERE ic.stock_code = m.stock_code) AS ic_cnt,
                      (SELECT COALESCE(CAST(MAX(cap.effective_date) AS VARCHAR), '')
                         FROM share_capital_history cap WHERE cap.stock_code = m.stock_code) AS cap_date,
                      (SELECT COUNT(*) FROM share_capital_history cap
                         WHERE cap.stock_code = m.stock_code) AS cap_cnt,
                      (SELECT COALESCE(CAST(MAX(d.ex_date) AS VARCHAR), '')
                         FROM dividends d WHERE d.stock_code = m.stock_code) AS div_date,
                      (SELECT COUNT(*) FROM dividends d
                         WHERE d.stock_code = m.stock_code) AS div_cnt
               FROM stock_meta m
               WHERE m.stock_code IN ({placeholders})
               ORDER BY m.stock_code""".replace(
                "{placeholders}", ", ".join("?" for _ in stock_codes)
            ),
            stock_codes,
        )
        curve = self.duck.read_query(
            "SELECT COALESCE(CAST(MAX(curve_date) AS VARCHAR), '') AS latest, "
            "COUNT(*) AS c FROM treasury_yield_curve"
        )[0]
        verified = self.duck.read_query(
            "SELECT COUNT(*) AS c FROM share_capital_history WHERE verified"
        )[0]
        result: dict[str, str] = {}
        for row in rows:
            parts = [
                "price", str(row["price_date"] or ""), str(row["price_cnt"] or 0),
                "balance", str(row["bs_date"] or ""), str(row["bs_cnt"] or 0),
                "income", str(row["ic_date"] or ""), str(row["ic_cnt"] or 0),
                "capital", str(row["cap_date"] or ""), str(row["cap_cnt"] or 0),
                "dividends", str(row["div_date"] or ""), str(row["div_cnt"] or 0),
                "treasury", str(curve.get("latest") or ""), str(curve.get("c") or 0),
                "verified", str(verified.get("c") or 0),
            ]
            result[str(row["stock_code"])] = hashlib.sha256(
                "|".join(parts).encode("utf-8")
            ).hexdigest()[:16]
        return result

    def _load_stock_state(self) -> dict[str, str]:
        try:
            rows = self.sqlite.query(
                f"SELECT stock_code, fingerprint FROM {self._STOCK_STATE_TABLE}"
            )
        except Exception:
            return {}
        return {str(row["stock_code"]): str(row["fingerprint"]) for row in rows}

    def _save_stock_state(self, fingerprints: dict[str, str]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.sqlite.transaction() as conn:
            conn.executemany(
                f"""INSERT INTO {self._STOCK_STATE_TABLE}
                    (stock_code, fingerprint, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(stock_code) DO UPDATE SET
                      fingerprint=excluded.fingerprint,
                      updated_at=excluded.updated_at""",
                [
                    (stock_code, fingerprint, now)
                    for stock_code, fingerprint in fingerprints.items()
                ],
            )

    def rebuild_incremental(
        self,
        *,
        parallel: int = 0,
        progress_cb: Any = None,
    ) -> dict[str, Any]:
        """Rebuild only stocks whose per-stock statistics inputs changed.

        First call has no stored state and falls back to a full rebuild, after
        which per-stock fingerprints make subsequent runs incremental.
        """
        self._ensure_stock_state_table()
        rows = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        stock_codes = [str(row["stock_code"]) for row in rows]
        if not stock_codes:
            return {"status": "skipped", "reason": "no_listed_stocks", "targeted": 0}
        current = self._stock_fingerprints(stock_codes)
        stored = self._load_stock_state()
        changed = [
            code for code in stock_codes
            if stored.get(code) != current.get(code)
        ]
        if not changed:
            return {
                "status": "skipped",
                "reason": "fingerprint_unchanged",
                "targeted": 0,
                "fingerprint": self._input_fingerprint(),
            }
        report = self.rebuild_all(
            stock_codes=changed,
            parallel=parallel,
            progress_cb=progress_cb,
            merge=True,
        )
        succeeded = [
            code for code in report.get("success_codes", [])
            if code in current
        ]
        if succeeded:
            self._save_stock_state({
                code: current[code] for code in succeeded
            })
        report["changed_codes"] = len(changed)
        report["incremental"] = True
        return report

    def _cleanup_staging_tables(self) -> None:
        """Drop orphaned research_statistics_staging_* tables (reports/76 P3-2).

        A killed rebuild leaves its staging table behind; sweep them before
        the next atomic publish so the formal schema stays clean.
        """
        tables = self.duck.read_query(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name LIKE 'research_statistics_staging_%'
            """
        )
        if not tables:
            return
        with self.duck.write_connection() as connection:
            for table in tables:
                connection.execute(f'DROP TABLE IF EXISTS "{table["table_name"]}"')

    def _stats_for_stock(
        self,
        stock_code: str,
        series: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """单股全指标×全窗口统计（reports/73 修复阶段性能优化）。

        单次遍历 series 填充所有窗口的值桶（避免每窗口重复遍历）；
        percentile 用 bisect_right 替代线性计数；覆盖每股一批查询
        （价格日 + 股本链）后内存滑动计算各窗口，避免 per-window N+1。
        """
        if not series:
            return []
        today = date.today()
        window_starts: dict[int, date | None] = {
            w: (today.replace(year=today.year - w) if w != 99 else None)
            for w in WINDOW_YEARS
        }

        # 覆盖计算输入（每股一次查询，供 PE/PB 窗口门槛）
        if self._batch_price_days:
            price_days = self._batch_price_days.get(stock_code, [])
        else:
            price_day_rows = self.duck.read_query(
                "SELECT trade_date FROM price_daily_raw "
                "WHERE stock_code = ? AND close IS NOT NULL ORDER BY trade_date",
                [stock_code],
            )
            price_days = [str(row["trade_date"])[:10] for row in price_day_rows]
        capital_rows = self._batch_capital_rows.get(stock_code, self._batch_sentinel)
        if capital_rows is self._batch_sentinel:
            capital_rows = self.duck.read_query(
                "SELECT effective_date, verified FROM share_capital_history "
                "WHERE stock_code = ? ORDER BY effective_date",
                [stock_code],
            )
        capital_days = [str(row["effective_date"])[:10] for row in capital_rows]

        def coverage_for(window_years: int) -> float | None:
            """窗口内主链股本延续覆盖。

            2026-08-12 决策（用户）：PE/PB 统计先行按 CNINFO 主链口径可用
            （主链点存在即覆盖），东财交叉核验（verified）后续补强；
            verified 仅作披露，不再阻断统计发布。
            """
            if not price_days:
                return 0.0
            start = window_starts[window_years]
            covered = 0
            total = 0
            point_index = 0
            for price_day in price_days:
                if start is not None and date.fromisoformat(price_day) < start:
                    continue
                total += 1
                while (
                    point_index + 1 < len(capital_days)
                    and capital_days[point_index + 1] <= price_day
                ):
                    point_index += 1
                if (
                    point_index < len(capital_days)
                    and capital_days[point_index] <= price_day
                ):
                    covered += 1
            return round(covered / total * 100.0, 2) if total else 0.0

        records: list[dict[str, Any]] = []
        for metric in STAT_METRICS:
            # 单次遍历：按窗口归桶
            buckets: dict[int, list[float]] = {w: [] for w in WINDOW_YEARS}
            bucket_dates: dict[int, list[str]] = {w: [] for w in WINDOW_YEARS}
            current: dict[int, float | None] = {w: None for w in WINDOW_YEARS}
            for item in series:
                day = date.fromisoformat(item["price_date"])
                value = item.get(metric)
                if value is None:
                    continue
                for w in WINDOW_YEARS:
                    start = window_starts[w]
                    if start is not None and day < start:
                        continue
                    buckets[w].append(float(value))
                    bucket_dates[w].append(item["price_date"])
                    current[w] = float(value)

            for w in WINDOW_YEARS:
                min_samples = WINDOW_MIN_SAMPLES[w]
                cap = coverage_for(w) if metric in ("pe_ttm", "pb_mrq") else None
                values = buckets[w]
                if cap is not None and cap < COVERAGE_THRESHOLD_PCT:
                    reason: dict[str, Any] = {
                        "reason": "coverage_below_threshold", "coverage_pct": cap,
                    }
                elif len(values) < min_samples:
                    reason = {
                        "reason": "insufficient_samples",
                        "samples": len(values), "min_samples": min_samples,
                    }
                else:
                    reason = None

                if reason is not None:
                    records.append({
                        "stock_code": stock_code, "metric": metric,
                        "window_years": w, "method": "percentile",
                        "value": None, "samples": reason.get("samples"),
                        "coverage_pct": reason.get("coverage_pct"),
                        "reason": reason["reason"],
                    })
                    records.append({
                        "stock_code": stock_code, "metric": metric,
                        "window_years": w, "method": "zscore",
                        "value": None, "samples": reason.get("samples"),
                        "coverage_pct": reason.get("coverage_pct"),
                        "reason": reason["reason"],
                    })
                    continue

                values_sorted = sorted(values)
                n = len(values_sorted)
                mean = statistics.fmean(values_sorted)
                sigma = statistics.stdev(values_sorted) if n > 1 else 0.0
                cur = current[w]
                zscore = (cur - mean) / sigma if cur is not None and sigma > 0 else None
                # percentile = 当前值 ≤ 窗口内值的占比（bisect 等价线性计数）
                rank = bisect_right(values_sorted, cur)
                percentile = (rank / n) * 100.0 if n else None
                records.append({
                    "stock_code": stock_code, "metric": metric,
                    "window_years": w, "method": "percentile",
                    "value": percentile,
                    "samples": n, "coverage_pct": cap,
                    "min_date": bucket_dates[w][0], "max_date": bucket_dates[w][-1],
                    "reason": None,
                })
                records.append({
                    "stock_code": stock_code, "metric": metric,
                    "window_years": w, "method": "zscore",
                    "value": zscore,
                    "samples": n, "coverage_pct": cap,
                    "min_date": bucket_dates[w][0], "max_date": bucket_dates[w][-1],
                    "reason": None,
                })
        return records

    def _capital_coverage(self, stock_code: str, window_years: int = 99) -> float:
        """窗口内有行情日的主链股本覆盖。

        2026-08-12 决策（用户）：PE/PB 统计先行按 CNINFO 主链口径可用
        （主链点存在即覆盖）；东财交叉核验（verified）后续补强，
        verified 仅作披露，不再阻断覆盖判定。
        """
        window_start = None
        if window_years != 99:
            today = date.today()
            window_start = today.replace(
                year=today.year - window_years
            ).isoformat()
        price_rows = self.duck.read_query(
            """SELECT trade_date FROM price_daily_raw
               WHERE stock_code = ? AND close IS NOT NULL
                 AND (? IS NULL OR trade_date >= CAST(? AS DATE))
               ORDER BY trade_date""",
            [stock_code, window_start, window_start],
        )
        if not price_rows:
            return 0.0
        capital_rows = self.duck.read_query(
            """SELECT effective_date FROM share_capital_history
               WHERE stock_code = ? ORDER BY effective_date""",
            [stock_code],
        )
        if not capital_rows:
            return 0.0
        covered = 0
        point_index = 0
        for price in price_rows:
            price_day = str(price["trade_date"])[:10]
            while (
                point_index + 1 < len(capital_rows)
                and str(capital_rows[point_index + 1]["effective_date"])[:10] <= price_day
            ):
                point_index += 1
            if (
                point_index < len(capital_rows)
                and str(capital_rows[point_index]["effective_date"])[:10] <= price_day
            ):
                covered += 1
        return round(covered / len(price_rows) * 100.0, 2)

    def _input_fingerprint(self) -> str:
        """输入指纹：价格/财务/股本/曲线/分红最新日期与行数摘要。

        P4-9 修复（reports/73）：加入 dividends——仅分红变化也触发统计重建，
        避免发布域 TTM 股息率/利差陈旧。
        2026-08-13：share_capital_history 追加 verified 计数——交叉核验
        （verified 标志变化）也触发重建，防止披露口径与发布域脱节。
        """
        parts: list[str] = []
        # 价格表 1700 万级，保持 count+max 轻指纹；其余小表使用内容级
        # md5：同一报告日/除权日/生效日的值被修正（如财报差错更正、
        # 分红更正、股本链核验更新）时，统计域必须重建而不是等下一轮。
        price_row = self.duck.read_query(
            "SELECT COUNT(*) AS c, MAX(trade_date) AS latest FROM price_daily_raw"
        )[0]
        parts.append(f"price_daily_raw:{price_row['c']}:{str(price_row['latest'])[:10]}")

        def content_part(table: str, fields: list[str], sort_fields: list[str]) -> str:
            expression = " || ':' || ".join(
                f"COALESCE(CAST({field} AS VARCHAR), '')" for field in fields
            )
            order_by = ", ".join(sort_fields)
            row = self.duck.read_query(
                f"""SELECT COUNT(*) AS c, COALESCE(md5(string_agg(
                       {expression}, '|' ORDER BY {order_by}
                   )), '') AS fp FROM {table}"""
            )[0]
            return f"{table}:{row['c']}:{row['fp']}"

        # 排序必须带 stock_code 作为最终 tie-breaker：多只股票可能共享
        # 同一报告日/除权日/生效日，仅按日期排序会让 string_agg 在 DuckDB
        # 并行执行中产生非确定顺序，导致指纹逐次变化、统计域每轮误重建。
        parts.append(content_part(
            "income_statement",
            ["report_date", "parent_net_profit"],
            ["report_date", "stock_code"],
        ))
        parts.append(content_part(
            "balance_sheet",
            ["report_date", "total_equity_parent"],
            ["report_date", "stock_code"],
        ))
        parts.append(content_part(
            "share_capital_history",
            ["effective_date", "total_shares", "verified"],
            ["effective_date", "stock_code"],
        ))
        parts.append(content_part(
            "dividends",
            ["ex_date", "announcement_date", "dividend_per_share"],
            ["ex_date", "announcement_date", "stock_code"],
        ))
        parts.append(content_part(
            "treasury_yield_curve",
            ["curve_date", "tenor_years", "yield_pct"],
            ["curve_date", "tenor_years"],
        ))
        verified = self.duck.read_query(
            "SELECT COUNT(*) AS c FROM share_capital_history WHERE verified"
        )[0]
        parts.append(f"share_capital_verified:{verified['c']}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _next_version(self) -> int:
        row = self.duck.read_query(
            "SELECT COALESCE(MAX(version), 0) AS v FROM research_statistics"
        )[0]
        return int(row["v"]) + 1

    # P4-4 修复（reports/73）：固定规范列集合，避免首记录为 reason 行时
    # 成功行的 min_date/max_date 丢失、列顺序脆弱。
    _PUBLISH_COLUMNS = (
        "stock_code", "metric", "window_years", "method", "value", "samples",
        "coverage_pct", "min_date", "max_date", "reason",
        "version", "input_fingerprint", "published_at",
    )

    def _write_records(self, staging_table: str, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        placeholders = ", ".join(["?"] * len(self._PUBLISH_COLUMNS))
        columns = ", ".join(self._PUBLISH_COLUMNS)
        with self.duck.write_connection() as conn:
            conn.executemany(
                f'INSERT INTO "{staging_table}" ({columns}) VALUES ({placeholders})',
                [[row.get(col) for col in self._PUBLISH_COLUMNS] for row in records],
            )


# ─── 进程池 worker（reports/73 修复阶段：全量重建并行化）──────────────────


def _statistics_worker_init(duck_path: str, sqlite_path: str) -> None:
    """进程池 initializer：在工作进程内建立只读 store（Windows spawn 安全）。"""
    from pathlib import Path

    from app.core.storage.path_policy import DatabasePathSet, VdEnv

    global _WORKER_STORE, _WORKER_SQLITE, _WORKER_BUILDER
    run_root = Path(duck_path).parent
    paths = DatabasePathSet(
        env=VdEnv.FORMAL,
        duckdb_path=Path(duck_path),
        sqlite_path=Path(sqlite_path),
        run_root=run_root,
    ).validate()
    _WORKER_STORE = DuckDBStore(paths=paths)
    _WORKER_SQLITE = SQLiteStore(paths=paths)
    _WORKER_BUILDER = StatisticsBuilder(duck=_WORKER_STORE, sqlite=_WORKER_SQLITE)


def _statistics_worker_build(code: str) -> tuple[str, list[dict[str, Any]]] | tuple[str, None, str]:
    """工作函数：单股 build_series + _stats_for_stock（只读，可 pickle）。"""
    global _WORKER_BUILDER
    try:
        builder = _WORKER_BUILDER
        if builder is None:
            builder = StatisticsBuilder(duck=_WORKER_STORE, sqlite=_WORKER_SQLITE)
            _WORKER_BUILDER = builder
        series = builder.build_series(code)
        return code, builder._stats_for_stock(code, series)
    except Exception as error:
        return code, None, str(error)


def _statistics_worker_build_chunk(
    codes: list[str],
) -> list[tuple[str, list[dict[str, Any]] | None, str | None]]:
    """批量工作函数：先批量取数，再逐股复用同一份缓存计算。"""
    global _WORKER_BUILDER
    results: list[tuple[str, list[dict[str, Any]] | None, str | None]] = []
    try:
        builder = _WORKER_BUILDER
        if builder is None:
            builder = StatisticsBuilder(duck=_WORKER_STORE, sqlite=_WORKER_SQLITE)
            _WORKER_BUILDER = builder
        builder.prime_batch(codes)
        for code in codes:
            try:
                series = builder.build_series(code)
                results.append((code, builder._stats_for_stock(code, series), None))
            except Exception as error:
                results.append((code, None, str(error)))
    except Exception as error:
        return [(code, None, str(error)) for code in codes]
    return results


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
