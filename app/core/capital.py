"""历史总股本链回填与核验服务（P4，reports/68 §3）

主链：CNINFO p_stock2215 锚点 + 变动事件；东财 F10 仅作近邻交叉校验。
- 仅当 CNINFO 相邻锚点一致且中间无冲突事件，或来源明确持续有效时，
  才可将股本延续到中间交易日（reports/68 §3.3）。
- 双源冲突超过容差、锚点不一致或无可验证股本的日期必须 missing；
  禁止当前总股本、插值或默认值回填（reports/68 §3.4）。
- 默认十年窗口内历史总股本必须连续可验证覆盖至少 90% 的有行情交易日（§3.5）。

历史日 D 的股本取值：≤D 最近主链点的 total_shares。
verified：该点与其前一点之间的区间内，东财近邻（±10 天）事件与主链值一致，
或无东财事件；冲突时该区间 fail-closed（主链点 verified=false）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

from app.core.adapters.base import FetchRequest
from app.core.adapters.capital_history_adapter import CapitalHistoryAdapter
from app.core.adapters.manager import AdapterManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["CapitalHistoryUpdater", "CROSS_TOLERANCE", "CROSS_NEIGHBOR_DAYS",
           "COVERAGE_THRESHOLD_PCT"]

CROSS_TOLERANCE = 0.001        # 双源相对差容差（reports/68 §3.4）
CROSS_NEIGHBOR_DAYS = 10       # 东财事件近邻匹配窗口（阶段0 实测双源日期集不重叠）
RETRY_DATA_TYPE = "share_capital_history"
COVERAGE_THRESHOLD_PCT = 90.0  # reports/68 §3.5


class CapitalHistoryUpdater:
    """历史总股本链回填执行器（注入 stores 或 validated paths，fail-closed）。"""

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
        adapter: CapitalHistoryAdapter | AdapterManager | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError(
                "CapitalHistoryUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("CapitalHistoryUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        self.adapter = adapter or AdapterManager()
        self._ensure_cross_cache_table()

    # ─── 东财交叉数据本地缓存（reports/74 修复：中断/失败后只续未核验，
    #     绝不重跑已核验股票，避免反复全量请求东财）───────────────────
    _CROSS_CACHE_TTL_DAYS = 7  # 东财事件日终低频变化，7 天内复用

    def _ensure_cross_cache_table(self) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS capital_cross_cache (
                        stock_code    TEXT PRIMARY KEY,
                        events_json   TEXT NOT NULL,
                        verified_points INTEGER NOT NULL,
                        total_points  INTEGER NOT NULL,
                        fetched_at    TEXT NOT NULL
                    )"""
                )
        except Exception as e:
            logger.warning("创建东财交叉缓存表失败: %s", e)

    def _load_cross_cache(
        self, stock_code: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """返回 (events, fresh)。fresh=False 表示无缓存或已过期（需重新请求）。"""
        try:
            rows = self.sqlite.query(
                "SELECT events_json, fetched_at FROM capital_cross_cache "
                "WHERE stock_code = ?",
                [stock_code],
            )
        except Exception:
            return [], False
        if not rows:
            return [], False
        fetched_at = rows[0].get("fetched_at") or ""
        try:
            fetched = datetime.fromisoformat(fetched_at)
        except ValueError:
            return [], False
        if (datetime.now(timezone.utc) - fetched).days > self._CROSS_CACHE_TTL_DAYS:
            return [], False
        try:
            events = json.loads(rows[0]["events_json"])
        except (json.JSONDecodeError, TypeError):
            return [], False
        return events, True

    def _save_cross_cache(
        self,
        stock_code: str,
        events: list[dict[str, Any]],
        verified_points: int,
        total_points: int,
    ) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO capital_cross_cache
                       (stock_code, events_json, verified_points, total_points, fetched_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(stock_code) DO UPDATE SET
                         events_json=excluded.events_json,
                         verified_points=excluded.verified_points,
                         total_points=excluded.total_points,
                         fetched_at=excluded.fetched_at""",
                    [stock_code, json.dumps(events, ensure_ascii=False),
                     verified_points, total_points,
                     datetime.now(timezone.utc).isoformat()],
                )
        except Exception as e:
            logger.warning("保存东财交叉缓存失败: %s", e)

    def _cross_cache_covered(self, stock_code: str) -> bool:
        """该股票是否已有可复用交叉缓存（用于 due 游标）。"""
        try:
            rows = self.sqlite.query(
                "SELECT 1 FROM capital_cross_cache WHERE stock_code = ?",
                [stock_code],
            )
            return bool(rows)
        except Exception:
            return False

    # ─── 单股回填 + 交叉核验 ──────────────────────────────────────

    def update_stock(
        self,
        stock_code: str,
        *,
        cross_check: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """抓取主链并（可选）东财交叉核验，单股事务原子替换。

        - 主链空（含新股无记录）→ 保留旧值 + missing（不误报 retry）。
        - 主链错误（源异常）→ 保留旧值 + retry。
        - 交叉核验失败（东财不可用）→ 不阻塞主链，verified 记录为 False 并注明。
        """
        result = self.adapter.fetch(FetchRequest(
            data_type="share_capital_history",
            stock_codes=[stock_code],
        ))
        if result.metadata.error:
            self._record_retry(stock_code, result.metadata.error)
            return {
                "status": "failed", "stock_code": stock_code,
                "error": result.metadata.error, "retained": True,
            }

        if not result.data:
            self._record_missing(stock_code, "source_empty")
            return {
                "status": "failed", "stock_code": stock_code,
                "reason": "source_empty", "retained": True,
            }

        cross_events: list[dict[str, Any]] = []
        cross_status = "unverified"
        if cross_check:
            # 优先复用本地缓存（中断/失败后不重复请求东财）
            cached_events, fresh = self._load_cross_cache(stock_code)
            if fresh:
                cross_events = cached_events
                cross_status = "cached"
            else:
                cross_result = self.adapter.fetch(FetchRequest(
                    data_type="share_capital_history",
                    stock_codes=[stock_code],
                    extra_params={"cross_source": "eastmoney"},
                ))
                if cross_result.metadata.error:
                    cross_status = f"cross_unavailable: {cross_result.metadata.error[:80]}"
                elif cross_result.data:
                    cross_events = cross_result.data
                    cross_status = "verified"

        rows = self._verify_and_build(stock_code, result.data, cross_events)
        if not rows:
            self._record_missing(stock_code, "no_valid_records")
            return {
                "status": "failed", "stock_code": stock_code,
                "reason": "no_valid_records", "retained": True,
            }

        verified_points = sum(1 for r in rows if r.get("verified"))
        if cross_check and cross_events:
            # 核验结果立即落盘（无论主链是否成功，交叉数据都值得缓存；
            # 主链成功后缓存避免未来重跑）
            self._save_cross_cache(
                stock_code, cross_events, verified_points, len(rows),
            )

        batch_id = uuid.uuid4().hex
        raw_material = json.dumps(
            {"main": result.data, "cross": cross_events},
            ensure_ascii=False, default=str,
        )
        raw_hash = hashlib.sha256(raw_material.encode("utf-8")).hexdigest()
        with self.duck.transaction() as conn:
            conn.execute(
                "DELETE FROM share_capital_history WHERE stock_code = ?",
                [stock_code],
            )
            conn.executemany(
                """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, change_reason,
                    is_anchor, verified, source, raw_hash, batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'cninfo_capital', ?, ?)""",
                [
                    [
                        stock_code, row["effective_date"], row["total_shares"],
                        row.get("change_reason"), row.get("is_anchor"),
                        row.get("verified"), raw_hash, batch_id,
                    ]
                    for row in rows
                ],
            )

        self._resolve_missing(stock_code)
        self._resolve_retry(stock_code)
        return {
            "status": "success", "stock_code": stock_code,
            "batch_id": batch_id, "rows": len(rows),
            "cross_status": cross_status,
            "verified_points": sum(1 for r in rows if r.get("verified")),
        }

    def _verify_and_build(
        self,
        stock_code: str,
        main_events: list[dict[str, Any]],
        cross_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """主链点 + 区间交叉核验 → 输出（含 verified 标志）。"""
        rows = [
            {
                "effective_date": r["effective_date"],
                "total_shares": float(r["total_shares"]),
                "change_reason": r.get("change_reason"),
                "is_anchor": bool(r.get("is_anchor")),
                "verified": True,
            }
            for r in main_events
            if r.get("effective_date") and r.get("total_shares")
        ]
        if not rows:
            return []
        if not cross_events:
            # 东财不可用/无事件：主链单独成立（锚点即来源声明），但不标 verified
            for row in rows:
                row["verified"] = False
            return rows

        # 东财事件去重（同一生效日多条取最近一条）
        cross_by_date: dict[str, float] = {}
        for event in cross_events:
            d = event.get("effective_date")
            total = event.get("total_shares")
            if d and total:
                cross_by_date[d] = float(total)

        for index, row in enumerate(rows):
            # P4-7 修复（reports/73）：东财事件按 ±CROSS_NEIGHBOR_DAYS 近邻匹配
            # 主链点（阶段0 实测双源日期集不重叠，区间包含匹配无法核验）；
            # 有近邻点 → 数值比对判冲突；无近邻点 → 区间无法核验，fail-closed
            # （事件前后最近主链点均不可验证，保持保守语义）。
            conflict_indexes: set[int] = set()
            for event_date, event_total in sorted(cross_by_date.items()):
                event_day = date.fromisoformat(event_date)
                best_index: int | None = None
                best_gap = 10**9
                for i, point in enumerate(rows):
                    gap = abs(
                        (date.fromisoformat(point["effective_date"]) - event_day).days
                    )
                    if gap < best_gap:
                        best_index, best_gap = i, gap
                if best_index is None:
                    continue
                if best_gap > CROSS_NEIGHBOR_DAYS:
                    # 无近邻主链点：定位事件所在区间并 fail-closed。
                    # 事件落在 [point_lo, point_lo+1] → 两端均不可验证；
                    # 早于首点 → 影响首点；晚于末点 → 影响末点。
                    lower_index = None
                    for i, point in enumerate(rows):
                        if date.fromisoformat(point["effective_date"]) <= event_day:
                            lower_index = i
                        else:
                            break
                    if lower_index is None:
                        conflict_indexes.add(0)
                    elif lower_index + 1 < len(rows):
                        conflict_indexes.add(lower_index)
                        conflict_indexes.add(lower_index + 1)
                    else:
                        conflict_indexes.add(lower_index)
                    continue
                rel = abs(event_total - rows[best_index]["total_shares"]) / max(
                    event_total, rows[best_index]["total_shares"]
                )
                if rel >= CROSS_TOLERANCE:
                    conflict_indexes.add(best_index)
                    if best_index > 0:
                        conflict_indexes.add(best_index - 1)
            for i in conflict_indexes:
                rows[i]["verified"] = False
        return rows

    # ─── 批量 / 全量 ──────────────────────────────────────────────

    def update_many(
        self,
        stock_codes: list[str],
        *,
        cross_check: bool = True,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for code in stock_codes:
            # P4-11 修复（reports/73）：per-stock 异常隔离，单股失败不中断整批
            try:
                outcome = self.update_stock(code, cross_check=cross_check)
            except Exception as error:
                logger.warning("回填 %s 历史股本异常: %s", code, error)
                self._record_retry(code, str(error)[:500])
                outcome = {
                    "status": "failed", "stock_code": code,
                    "error": str(error), "retained": True,
                }
            results[code] = outcome
            if outcome["status"] != "success":
                failed.append(code)
            if progress_cb is not None:
                progress_cb(code, outcome)
        return {
            "status": "success" if not failed else ("failed" if len(failed) == len(stock_codes) else "partial"),
            "targeted": len(stock_codes),
            "succeeded": len(stock_codes) - len(failed),
            "failed": failed,
            "results": results,
        }

    def update_all(self, max_stocks: int = 0, *, cross_check: bool = True) -> dict[str, Any]:
        """有界续传（P4-2 修复，reports/73）：只处理"缺失/陈旧"的上市股票。

        陈旧判定：无股本链记录，或最新锚点早于该股最新价格日（价格史尚未被
        股本覆盖）——已成功的股票下一轮自然不再入选，实现真正游标续传，
        不再永远重复前 20 只。
        """
        codes = self._due_stock_codes()
        if not codes:
            return {"status": "skipped", "reason": "no_due_stocks"}
        try:
            priority = {
                row["stock_code"]
                for row in self.sqlite.query("SELECT DISTINCT stock_code FROM watchlist")
            }
        except Exception:
            priority = set()
        if priority:
            codes = sorted(codes, key=lambda code: (code not in priority, code))
        if max_stocks > 0:
            codes = codes[:max_stocks]
        return self.update_many(codes, cross_check=cross_check)

    def _due_stock_codes(self) -> list[str]:
        """上市股票中"主链缺失/陈旧 或 无交叉核验缓存"的部分（续传游标）。

        P4-2 修复：主链按价格史判定陈旧；交叉核验缓存落盘后
        （capital_cross_cache），已核验股票不再入选。due = 主链 due ∪ 无缓存，
        保证"主链已全量但缓存为空"时交叉核验仍会执行；一旦缓存写入，
        中断/失败后恢复只续未核验部分，绝不重跑已核验股票。
        """
        rows = self.duck.read_query(
            """SELECT m.stock_code
               FROM stock_meta m
               LEFT JOIN (
                   SELECT stock_code, MAX(effective_date) AS latest_cap
                   FROM share_capital_history GROUP BY stock_code
               ) h ON h.stock_code = m.stock_code
               LEFT JOIN (
                   SELECT stock_code, MAX(trade_date) AS latest_price
                   FROM price_daily_raw GROUP BY stock_code
               ) p ON p.stock_code = m.stock_code
               WHERE m.is_listed IS TRUE
                 AND (h.stock_code IS NULL
                      OR h.latest_cap < COALESCE(
                          p.latest_price, CURRENT_DATE - INTERVAL '7 days'))
               ORDER BY (h.stock_code IS NULL) DESC, m.stock_code"""
        )
        main_due = [row["stock_code"] for row in rows]

        # 无交叉缓存 → 也入选（确保交叉核验执行）
        listed_rows = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE"
        )
        listed = [row["stock_code"] for row in listed_rows]
        try:
            cached_rows = self.sqlite.query(
                "SELECT stock_code FROM capital_cross_cache"
            )
            cached_codes = {row["stock_code"] for row in cached_rows}
        except Exception:
            cached_codes = set()
        no_cache = [code for code in listed if code not in cached_codes]

        due = list(dict.fromkeys([*main_due, *no_cache]))
        return due

    def _listed_stock_codes(self) -> list[str]:
        rows = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        return [row["stock_code"] for row in rows]

    def _coverage_all(self, window_years: int = 10) -> dict[str, Any]:
        """全部上市股票十年覆盖汇总（只读 check-only）。"""
        codes = self._listed_stock_codes()
        reports = [self.coverage_report(code, window_years) for code in codes]
        if not reports:
            return {"status": "skipped", "reason": "no_listed_stocks"}
        covered = sum(1 for r in reports if r["coverage_pct"] >= COVERAGE_THRESHOLD_PCT)
        return {
            "status": "ok",
            "window_years": window_years,
            "total": len(reports),
            "covered": covered,
            "below_threshold": len(reports) - covered,
            "avg_coverage_pct": round(
                sum(r["coverage_pct"] for r in reports) / len(reports), 2
            ),
        }

    # ─── 覆盖核验（只读） ─────────────────────────────────────────

    def coverage_report(self, stock_code: str, window_years: int = 10) -> dict[str, Any]:
        """窗口内有行情交易日的历史股本可验证覆盖（reports/68 §3.5）。

        P4-6 修复（reports/73）：真实计算"窗口内被 verified 主链点延续覆盖的
        价格日占比"，不再有记录即报 100%；与 statistics._capital_coverage 同口径。
        """
        start = date.today().replace(year=date.today().year - window_years).isoformat()
        price_rows = self.duck.read_query(
            "SELECT trade_date FROM price_daily_raw "
            "WHERE stock_code = ? AND trade_date >= ? AND close IS NOT NULL "
            "ORDER BY trade_date",
            [stock_code, start],
        )
        if not price_rows:
            return {"stock_code": stock_code, "coverage_pct": 0.0, "price_days": 0,
                    "verified_days": 0, "verified_points": 0, "points": 0}
        history = self.duck.read_query(
            "SELECT effective_date, total_shares, verified FROM share_capital_history "
            "WHERE stock_code = ? ORDER BY effective_date",
            [stock_code],
        )
        covered = 0
        point_index = 0
        for price in price_rows:
            price_day = str(price["trade_date"])[:10]
            while (
                point_index + 1 < len(history)
                and str(history[point_index + 1]["effective_date"])[:10] <= price_day
            ):
                point_index += 1
            if (
                point_index < len(history)
                and str(history[point_index]["effective_date"])[:10] <= price_day
                and bool(history[point_index]["verified"])
            ):
                covered += 1
        verified_points = sum(1 for h in history if h["verified"])
        return {
            "stock_code": stock_code,
            "coverage_pct": round(covered / len(price_rows) * 100.0, 2),
            "price_days": len(price_rows),
            "verified_days": covered,
            "verified_points": verified_points,
            "points": len(history),
        }

    # ─── retry / missing ──────────────────────────────────────────

    def _record_retry(self, stock_code: str, error: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, '{}')
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [stock_code, RETRY_DATA_TYPE, "cninfo_capital", error[:500],
                     datetime.now(timezone.utc).isoformat()],
                )
        except Exception as e:
            logger.warning("记录历史股本失败信息失败: %s", e)

    def _record_missing(self, stock_code: str, reason_code: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [stock_code, "share_capital_history", reason_code],
                )
        except Exception as e:
            logger.warning("记录历史股本缺失信息失败: %s", e)

    def _resolve_missing(self, stock_code: str) -> None:
        try:
            self.sqlite.execute(
                """UPDATE missing_list SET resolved_at = ?
                   WHERE stock_code = ? AND field_name = ? AND resolved_at IS NULL""",
                [datetime.now(timezone.utc).isoformat(), stock_code, "share_capital_history"],
            )
        except Exception as e:
            logger.warning("解决历史股本缺失信息失败: %s", e)

    def _resolve_retry(self, stock_code: str) -> None:
        try:
            self.sqlite.execute(
                """DELETE FROM retry_list
                   WHERE stock_code = ? AND data_type = ? AND adapter = ?""",
                [stock_code, RETRY_DATA_TYPE, "cninfo_capital"],
            )
        except Exception as e:
            logger.warning("清理历史股本重试条目失败: %s", e)
