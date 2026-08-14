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
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

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
    # 2026-08-13 补全（STATUS 缺口 #7 待办①）：cross_status/error 落盘，
    # 失败/空结果也记录，批次审查可见；due 游标按状态区分（error 行带
    # 重试冷却，绝不把失败行当作已核验）。
    _CROSS_CACHE_TTL_DAYS = 7  # 东财事件日终低频变化，7 天内复用
    # 缓存行状态：verified=取到事件并参与核验；empty=取到空事件集（无交叉
    # 证据，不再重试）；error=源异常（含风控），冷却后可重试。
    _CROSS_CACHE_OK_STATUSES = {"verified", "empty"}
    _CROSS_ERROR_RETRY_DELAY_SECONDS = 1800  # error 行 30 分钟内不重试
    CROSS_ERROR_ABORT_THRESHOLD = 8          # 连续 N 次交叉错误中止批次

    def _ensure_cross_cache_table(self) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS capital_cross_cache (
                        stock_code    TEXT PRIMARY KEY,
                        events_json   TEXT NOT NULL,
                        verified_points INTEGER NOT NULL,
                        total_points  INTEGER NOT NULL,
                        fetched_at    TEXT NOT NULL,
                        cross_status  TEXT,
                        error         TEXT
                    )"""
                )
                columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(capital_cross_cache)"
                ).fetchall()}
                if "cross_status" not in columns:
                    conn.execute(
                        "ALTER TABLE capital_cross_cache ADD COLUMN cross_status TEXT"
                    )
                if "error" not in columns:
                    conn.execute(
                        "ALTER TABLE capital_cross_cache ADD COLUMN error TEXT"
                    )
        except Exception as e:
            logger.warning("创建东财交叉缓存表失败: %s", e)

    def _load_cross_cache(
        self, stock_code: str,
    ) -> tuple[list[dict[str, Any]], bool, str]:
        """返回 (events, fresh, status)。fresh=False 表示需重新请求。

        仅 verified/empty 行（含旧版无状态行=verified）视为可复用；
        error 行永不复用（无事件），由其冷却策略决定何时重试。
        """
        try:
            rows = self.sqlite.query(
                "SELECT events_json, fetched_at, cross_status FROM capital_cross_cache "
                "WHERE stock_code = ?",
                [stock_code],
            )
        except Exception:
            return [], False, ""
        if not rows:
            return [], False, ""
        fetched_at = rows[0].get("fetched_at") or ""
        status = rows[0].get("cross_status") or "verified"  # 旧版行无状态 → verified
        if status not in self._CROSS_CACHE_OK_STATUSES:
            return [], False, status
        try:
            fetched = datetime.fromisoformat(fetched_at)
        except ValueError:
            return [], False, status
        if (datetime.now(UTC) - fetched).days > self._CROSS_CACHE_TTL_DAYS:
            return [], False, status
        try:
            events = json.loads(rows[0]["events_json"])
        except (json.JSONDecodeError, TypeError):
            return [], False, status
        return events, True, status

    def _save_cross_cache(
        self,
        stock_code: str,
        events: list[dict[str, Any]],
        verified_points: int,
        total_points: int,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        # 不降级：empty/error 绝不覆盖既有 verified 证据（防瞬时风控抹掉核验）
        if status in {"empty", "error"}:
            try:
                existing = self.sqlite.query(
                    "SELECT cross_status FROM capital_cross_cache WHERE stock_code = ?",
                    [stock_code],
                )
            except Exception:
                existing = []
            if existing and (existing[0].get("cross_status") or "verified") == "verified":
                return
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO capital_cross_cache
                       (stock_code, events_json, verified_points, total_points,
                        fetched_at, cross_status, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(stock_code) DO UPDATE SET
                         events_json=excluded.events_json,
                         verified_points=excluded.verified_points,
                         total_points=excluded.total_points,
                         fetched_at=excluded.fetched_at,
                         cross_status=excluded.cross_status,
                         error=excluded.error""",
                    [stock_code, json.dumps(events, ensure_ascii=False),
                     verified_points, total_points,
                     datetime.now(UTC).isoformat(), status,
                     (error or "")[:500]],
                )
        except Exception as e:
            logger.warning("保存东财交叉缓存失败: %s", e)

    def _stale_verified_events(self, stock_code: str) -> list[dict[str, Any]]:
        """回退用：读取任何 verified 状态缓存行的事件（忽略 TTL）。

        交叉源瞬时失败/空响应时，旧核验证据仍胜过无证据——宁可披露
        陈旧核验，也不抹掉 verified 标志。
        """
        try:
            rows = self.sqlite.query(
                "SELECT events_json, cross_status FROM capital_cross_cache "
                "WHERE stock_code = ?",
                [stock_code],
            )
        except Exception:
            return []
        if not rows:
            return []
        status = rows[0].get("cross_status") or "verified"
        if status != "verified":
            return []
        try:
            return json.loads(rows[0]["events_json"])
        except (json.JSONDecodeError, TypeError):
            return []

    def _has_eastmoney_cross_source(self, stock_code: str) -> bool:
        """东财 F10 RPT_F10_EH_EQUITY 是否覆盖该股票（北交所不覆盖）。

        2026-08-13 实测：北交所 920xxx 全部返回空/异常，触发适配器熔断
        殃及沪深；东财 F10 口径不含北交所（与业务概览 missing 一致）。
        """
        try:
            rows = self.duck.read_query(
                "SELECT exchange FROM stock_meta WHERE stock_code = ?",
                [stock_code],
            )
        except Exception:
            return True
        if not rows:
            return True
        return str(rows[0].get("exchange") or "").upper() != "BSE"

    def _cross_cache_covered(self, stock_code: str) -> bool:
        """该股票是否已有可复用交叉缓存（用于 due 游标）。

        verified/empty 行（含旧版无状态行）视为已覆盖；error 行在重试冷却
        窗口内暂不入选（避免对风控源持续轰炸），冷却结束后重新成为 due。
        """
        try:
            rows = self.sqlite.query(
                "SELECT cross_status, fetched_at FROM capital_cross_cache "
                "WHERE stock_code = ?",
                [stock_code],
            )
        except Exception:
            return False
        if not rows:
            return False
        status = rows[0].get("cross_status") or "verified"
        if status in self._CROSS_CACHE_OK_STATUSES:
            return True
        fetched_at = rows[0].get("fetched_at") or ""
        try:
            fetched = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
        # 冷却期内视为"暂不入选"
        return (datetime.now(UTC) - fetched).total_seconds() \
            < self._CROSS_ERROR_RETRY_DELAY_SECONDS

    # ─── 单股回填 + 交叉核验 ──────────────────────────────────────

    def update_stock(
        self,
        stock_code: str,
        *,
        cross_check: bool = True,
        cross_only: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """抓取主链并（可选）东财交叉核验，单股事务原子替换。

        - 主链空（含新股无记录）→ 保留旧值 + missing（不误报 retry）。
        - 主链错误（源异常）→ 保留旧值 + retry。
        - 交叉核验失败（东财不可用）→ 不阻塞主链，verified 记录为 False 并注明；
          交叉尝试结果（含失败原因）一律落盘缓存表（STATUS 缺口 #7 待办①）。
        - cross_only=True（核验补强模式）：主链不重新抓取，直接读库中已有
          股本链与东财交叉数据重算 verified 标志；库中无链则跳过（不报错）。
        """
        if cross_only:
            main_rows = self.duck.read_query(
                "SELECT effective_date, total_shares, change_reason, is_anchor "
                "FROM share_capital_history WHERE stock_code = ? "
                "ORDER BY effective_date",
                [stock_code],
            )
            if not main_rows:
                return {
                    "status": "skipped", "stock_code": stock_code,
                    "reason": "no_main_chain", "retained": True,
                }
            main_data: list[dict[str, Any]] = [
                {
                    "effective_date": str(row["effective_date"])[:10],
                    "total_shares": float(row["total_shares"]),
                    "change_reason": row.get("change_reason"),
                    "is_anchor": bool(row.get("is_anchor")),
                }
                for row in main_rows
            ]
        else:
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
            main_data = result.data

        cross_events: list[dict[str, Any]] = []
        cross_status = "unverified"
        cross_error: str | None = None
        if cross_check and self._has_eastmoney_cross_source(stock_code):
            # 优先复用本地缓存（中断/失败后不重复请求东财）
            cached_events, fresh, cached_status = self._load_cross_cache(stock_code)
            if fresh and cached_status == "verified":
                cross_events = cached_events
                cross_status = "cached"
            elif fresh and cached_status == "empty":
                cross_events = []
                cross_status = "cached_empty"
            else:
                cross_result = self.adapter.fetch(FetchRequest(
                    data_type="share_capital_history",
                    stock_codes=[stock_code],
                    extra_params={"cross_source": "eastmoney"},
                ))
                if cross_result.metadata.error:
                    cross_error = cross_result.metadata.error
                    stale = self._stale_verified_events(stock_code)
                    if stale:
                        cross_events = stale
                        cross_status = "cached_stale"
                    else:
                        cross_status = f"cross_unavailable: {cross_error[:80]}"
                elif cross_result.data:
                    cross_events = cross_result.data
                    cross_status = "verified"
                else:
                    stale = self._stale_verified_events(stock_code)
                    if stale:
                        cross_events = stale
                        cross_status = "cached_stale"
                    else:
                        cross_status = "cross_empty"
        elif cross_check:
            # 无东财交叉源（北交所：RPT_F10_EH_EQUITY 不覆盖）→ 如实记录
            # 不再请求，防无效请求引发适配器熔断殃及沪深股票（2026-08-13）。
            cross_status = "no_cross_source"
            self._save_cross_cache(stock_code, [], 0, 0, status="empty",
                                   error="no_cross_source:bse")

        rows = self._verify_and_build(stock_code, main_data, cross_events)
        if not rows:
            self._record_missing(stock_code, "no_valid_records")
            return {
                "status": "failed", "stock_code": stock_code,
                "reason": "no_valid_records", "retained": True,
            }

        verified_points = sum(1 for r in rows if r.get("verified"))
        if cross_check:
            # 核验结果立即落盘：成功（verified/empty）与失败（error 含原因）都记录，
            # 批次审查可见（STATUS 缺口 #7 待办①）；cached*/cross_empty 复用不重写。
            if cross_status == "verified":
                self._save_cross_cache(
                    stock_code, cross_events, verified_points, len(rows),
                    status="verified",
                )
            elif cross_status == "cross_empty":
                self._save_cross_cache(
                    stock_code, [], verified_points, len(rows), status="empty",
                )
            elif cross_status.startswith("cross_unavailable"):
                self._save_cross_cache(
                    stock_code, [], 0, 0, status="error",
                    error=cross_error or cross_status,
                )

        # cross_only 且无新交叉证据（error/empty/cached_empty）→ 不重写股本链，
        # 保留库中既有 verified 标志（防瞬时源失败抹掉核验成果）。
        if cross_only and not cross_events:
            verified_now = self.duck.read_query(
                "SELECT COUNT(*) AS c FROM share_capital_history "
                "WHERE stock_code = ? AND verified",
                [stock_code],
            )
            if cross_status.startswith("cross_unavailable"):
                return {
                    "status": "failed", "stock_code": stock_code,
                    "error": cross_error or cross_status, "retained": True,
                    "cross_status": cross_status,
                }
            return {
                "status": "skipped", "stock_code": stock_code,
                "reason": "no_cross_evidence", "retained": True,
                "cross_status": cross_status,
                "verified_points": verified_now[0]["c"] if verified_now else 0,
            }

        batch_id = uuid.uuid4().hex
        raw_material = json.dumps(
            {"main": main_data, "cross": cross_events},
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

        for _index, _row in enumerate(rows):
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
        cross_only: bool = False,
        batch_size: int = 0,
        batch_cooldown_seconds: float = 0.0,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """逐股回填/核验，带批量节奏控制与连续交叉错误中止（STATUS 缺口 #7 待办②）。

        - batch_size>0 时按批处理，批间睡眠 batch_cooldown_seconds 秒
          （仅当还有后续批次时才睡，避免末批无谓等待）。
        - 连续 CROSS_ERROR_ABORT_THRESHOLD 次交叉错误（含异常）→ 中止剩余，
          防止对风控源持续轰炸；已处理结果照常返回。
        """
        results: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        skipped: list[str] = []
        aborted_reason: str | None = None
        consecutive_cross_errors = 0
        total = len(stock_codes)
        batch_count = 0
        for index, code in enumerate(stock_codes):
            if batch_size > 0 and index % batch_size == 0 and index > 0:
                batch_count += 1
                if batch_cooldown_seconds > 0 and index < total:
                    time.sleep(batch_cooldown_seconds)
            # P4-11 修复（reports/73）：per-stock 异常隔离，单股失败不中断整批
            try:
                outcome = self.update_stock(
                    code, cross_check=cross_check, cross_only=cross_only,
                )
            except Exception as error:
                logger.warning("回填 %s 历史股本异常: %s", code, error)
                self._record_retry(code, str(error)[:500])
                outcome = {
                    "status": "failed", "stock_code": code,
                    "error": str(error), "retained": True,
                }
            results[code] = outcome
            if outcome["status"] == "skipped":
                skipped.append(code)  # cross_only 无交叉证据：良性跳过，不算失败
            elif outcome["status"] != "success":
                failed.append(code)
            if progress_cb is not None:
                progress_cb(code, outcome)
            # 连续交叉错误/空响应计数（cached*/verified 中断序列）
            cs = str(outcome.get("cross_status") or "")
            if cs.startswith("cross_unavailable"):
                consecutive_cross_errors += 1
                if consecutive_cross_errors >= self.CROSS_ERROR_ABORT_THRESHOLD:
                    aborted_reason = (
                        f"连续 {consecutive_cross_errors} 次交叉源错误，中止剩余批次"
                    )
                    break
            elif cs == "cross_empty":
                # 空响应多为"无事件"合法结果；连续大量空响应疑似静默风控
                consecutive_cross_errors += 1
                if consecutive_cross_errors >= self.CROSS_ERROR_ABORT_THRESHOLD * 2:
                    aborted_reason = (
                        f"连续 {consecutive_cross_errors} 次交叉源空响应疑似风控，"
                        "中止剩余批次"
                    )
                    break
            else:
                consecutive_cross_errors = 0
        status = "success" if not failed else ("failed" if len(failed) == len(stock_codes) else "partial")
        report: dict[str, Any] = {
            "status": status,
            "targeted": len(stock_codes),
            "succeeded": len(stock_codes) - len(failed) - len(skipped),
            "skipped": len(skipped),
            "failed": failed,
            "results": results,
        }
        if aborted_reason is not None:
            report["aborted"] = True
            report["abort_reason"] = aborted_reason
            report["remaining_skipped"] = total - len(results)
        return report

    def update_all(
        self,
        max_stocks: int = 0,
        *,
        cross_check: bool = True,
        cross_only: bool = False,
        batch_size: int = 0,
        batch_cooldown_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """有界续传（P4-2 修复，reports/73）：只处理"缺失/陈旧"的上市股票。

        陈旧判定：无股本链记录，或最新锚点早于该股最新价格日（价格史尚未被
        股本覆盖）——已成功的股票下一轮自然不再入选，实现真正游标续传，
        不再永远重复前 20 只。
        cross_only=True：仅对已有主链的股票做东财交叉核验（主链不重新抓取）。
        """
        codes = self._due_stock_codes(cross_only=cross_only)
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
        return self.update_many(
            codes, cross_check=cross_check, cross_only=cross_only,
            batch_size=batch_size, batch_cooldown_seconds=batch_cooldown_seconds,
        )

    def _due_stock_codes(self, *, cross_only: bool = False) -> list[str]:
        """上市股票中"主链缺失/陈旧 或 无交叉核验缓存"的部分（续传游标）。

        P4-2 修复：主链按价格史判定陈旧；交叉核验缓存落盘后
        （capital_cross_cache），已核验股票不再入选。due = 主链 due ∪ 无缓存，
        保证"主链已全量但缓存为空"时交叉核验仍会执行；一旦缓存写入，
        中断/失败后恢复只续未核验部分，绝不重跑已核验股票。
        error 行在冷却窗口内暂不入选（防对风控源轰炸），冷却后重新 due。
        cross_only=True 时主链陈旧项不入选（只补交叉核验）。
        """
        main_due: list[str] = []
        if not cross_only:
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
                "SELECT stock_code, cross_status, fetched_at FROM capital_cross_cache"
            )
        except Exception:
            cached_rows = []
        covered_codes: set[str] = set()
        cooling_codes: set[str] = set()
        for row in cached_rows:
            status = row.get("cross_status") or "verified"
            if status in self._CROSS_CACHE_OK_STATUSES:
                covered_codes.add(row["stock_code"])
                continue
            # error 行：冷却窗口内暂不入选，冷却结束后重新 due
            fetched_at = row.get("fetched_at") or ""
            try:
                fetched = datetime.fromisoformat(fetched_at)
            except ValueError:
                continue
            if (datetime.now(UTC) - fetched).total_seconds() \
                    < self._CROSS_ERROR_RETRY_DELAY_SECONDS:
                cooling_codes.add(row["stock_code"])
        no_cache = [
            code for code in listed
            if code not in covered_codes and code not in cooling_codes
        ]

        due = list(dict.fromkeys([*main_due, *no_cache]))
        return due

    # ─── 交叉核验审计（STATUS 缺口 #7 待办③，只读）─────────────────

    def cross_audit(self) -> dict[str, Any]:
        """交叉核验缓存审计视图：按状态汇总 + 失败原因样本 + 队列剩余。"""
        try:
            cache_rows = self.sqlite.query(
                "SELECT stock_code, cross_status, error, fetched_at, verified_points "
                "FROM capital_cross_cache"
            )
        except Exception as e:
            return {"status": "error", "error": str(e)}
        by_status: dict[str, int] = {}
        verified_points_total = 0
        error_samples: list[dict[str, str]] = []
        empty_no_source = 0
        for row in cache_rows:
            status = row.get("cross_status") or "verified"  # 旧版行 → verified
            by_status[status] = by_status.get(status, 0) + 1
            verified_points_total += int(row.get("verified_points") or 0)
            if status == "error" and len(error_samples) < 10:
                error_samples.append({
                    "stock_code": row["stock_code"],
                    "error": (row.get("error") or "")[:120],
                    "fetched_at": row.get("fetched_at") or "",
                })
            if status == "empty" and "no_cross_source" in (row.get("error") or ""):
                empty_no_source += 1
        listed = self._listed_stock_codes()
        covered = by_status.get("verified", 0) + by_status.get("empty", 0)
        cooling = by_status.get("error", 0)
        chain_verified = self.duck.read_query(
            "SELECT COUNT(DISTINCT stock_code) AS c FROM share_capital_history "
            "WHERE verified"
        )
        chain_rows = self.duck.read_query(
            "SELECT COUNT(*) AS c FROM share_capital_history WHERE verified"
        )
        return {
            "status": "ok",
            "listed": len(listed),
            "cache_rows": len(cache_rows),
            "by_status": by_status,
            "covered": covered,
            "empty_no_cross_source": empty_no_source,
            "error_rows": cooling,
            "unattempted": len(listed) - covered - cooling,
            "chain_verified_stocks": chain_verified[0]["c"] if chain_verified else 0,
            "chain_verified_rows": chain_rows[0]["c"] if chain_rows else 0,
            "cache_verified_points": verified_points_total,
            "error_samples": error_samples,
        }

    def _listed_stock_codes(self) -> list[str]:
        rows = self.duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
        )
        return [row["stock_code"] for row in rows]

    def _coverage_all(self, window_years: int = 10) -> dict[str, Any]:
        """全部上市股票十年覆盖汇总（只读 check-only，单次 SQL 聚合）。

        覆盖口径与 coverage_report 一致：窗口内有行情价格日中，存在
        ≤当日最近股本点的天数占比（股本为 step 函数，等价于
        trade_date >= 该股最早股本点）。
        """
        try:
            row = self.duck.read_query(
                f"""WITH first_cap AS (
                        SELECT stock_code, MIN(effective_date) AS first_cap
                        FROM share_capital_history GROUP BY stock_code
                    ),
                    price_agg AS (
                        SELECT p.stock_code,
                               COUNT(*) AS price_days,
                               COUNT(*) FILTER (
                                   WHERE p.trade_date
                                         >= COALESCE(f.first_cap, DATE '9999-12-31')
                               ) AS covered_days
                        FROM price_daily_raw p
                        LEFT JOIN first_cap f ON f.stock_code = p.stock_code
                        WHERE p.trade_date
                              >= CURRENT_DATE - INTERVAL '{window_years} years'
                          AND p.close IS NOT NULL
                        GROUP BY p.stock_code
                    ),
                    agg AS (
                        SELECT m.stock_code,
                               COALESCE(pa.price_days, 0) AS price_days,
                               COALESCE(pa.covered_days, 0) AS covered_days
                        FROM stock_meta m
                        LEFT JOIN price_agg pa ON pa.stock_code = m.stock_code
                        WHERE m.is_listed IS TRUE
                    )
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (
                               WHERE price_days > 0
                                 AND covered_days * 100.0 / price_days
                                     >= {COVERAGE_THRESHOLD_PCT}
                           ) AS covered,
                           AVG(CASE WHEN price_days > 0
                                    THEN covered_days * 100.0 / price_days
                                    ELSE 0 END) AS avg_coverage_pct
                    FROM agg"""
            )
        except Exception as e:
            return {"status": "error", "error": str(e)}
        if not row:
            return {"status": "skipped", "reason": "no_listed_stocks"}
        total = int(row[0]["total"] or 0)
        covered = int(row[0]["covered"] or 0)
        return {
            "status": "ok",
            "window_years": window_years,
            "total": total,
            "covered": covered,
            "below_threshold": total - covered,
            "avg_coverage_pct": round(float(row[0]["avg_coverage_pct"] or 0), 2),
        }

    # ─── 覆盖核验（只读） ─────────────────────────────────────────

    def coverage_report(self, stock_code: str, window_years: int = 10) -> dict[str, Any]:
        """窗口内有行情交易日的历史股本覆盖（与 statistics.coverage_for 同口径）。

        2026-08-12 决策（用户）：PE/PB 统计先行按 CNINFO 主链口径可用
        （主链点存在即覆盖）；verified（东财交叉）仅作披露字段，
        不再阻断覆盖判定。
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
                     datetime.now(UTC).isoformat()],
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
                [datetime.now(UTC).isoformat(), stock_code, "share_capital_history"],
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
