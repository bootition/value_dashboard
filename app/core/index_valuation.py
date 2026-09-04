"""指数估值低频域更新服务（2026-08-25 建域；2026-09-05 v21 多指数四源）

独立数据域：index_valuation 单张 DuckDB 表（PK: index_code+trade_date+source）。
- 宽基/红利：乐咕月末 PE/PB 主源 + 中证官网近 20 日交叉（中证/上证系）。
- 申万一级行业：申万研究指数分析日报日度 PE/PB 主源（source=sws）。
- ERP 与 ETF 分位共用此表；主源优先、同日期交叉核验披露。

写入语义：
- update_broad：逐指数主源全历史 upsert + 交叉源近 20 日 upsert。
- update_sw_industries：单窗口一次抓取 31 个申万一级行业并原子 upsert。
- 每日节流（UTC+8）；主源失败保留旧值并登记 retry；交叉失败不阻断主源。
- 申万非交易日空响应 = 合法 missing，不触发熔断。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from app.core.adapters.base import FetchRequest
from app.core.adapters.index_valuation_adapter import (
    CSINDEX_COVERED_CODES,
    LEGULEGU_INDEX_CODES,
    CSIndexIndexAdapter,
    LeguleguIndexAdapter,
    SwsIndexAdapter,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = [
    "IndexValuationUpdater",
    "DEFAULT_INDEX_CODES",
    "BROAD_INDEX_CODES",
    "SW_INDUSTRY_GROUP",
]

# 兼容 2026-08-25 默认行为：只更新沪深300
DEFAULT_INDEX_CODES = ["000300"]
# 多指数 ERP 默认宽基/红利组（乐咕支持的全部 12 个）
BROAD_INDEX_CODES: tuple[str, ...] = LEGULEGU_INDEX_CODES
# 申万一级行业更新在 retry_list 中的分组代码（非单指数）
SW_INDUSTRY_GROUP = "SW_ALL"

REFRESH_MARKER_KEY = "index_valuation_last_refresh"

_CN_TZ = timezone(timedelta(hours=8))


def _cn_today() -> date:
    """本地（UTC+8）今天。"""
    return datetime.now(_CN_TZ).date()


class IndexValuationUpdater:
    """指数估值低频更新执行器（乐咕+申万主源，中证官网交叉核验）。"""

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
                "IndexValuationUpdater requires both stores or validated paths"
            )
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("IndexValuationUpdater stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite
        # 独立适配器实例（各自限速），不经 AdapterManager 以便多源并存。
        # 乐咕对连发请求敏感（2026-09-05 正式库回填实测约 4 次后 403），
        # 单请求间隔提高到 2s 并严格排队。
        self._primary = LeguleguIndexAdapter(rate_limit=2.0)
        self._cross = CSIndexIndexAdapter(rate_limit=1.0)
        self._sws = SwsIndexAdapter(rate_limit=0.5)

    # ─── 宽基/红利组更新 ──────────────────────────────────────────────

    def update_broad(self, index_codes: list[str] | None = None) -> dict[str, Any]:
        """抓取乐咕 12 个宽基/红利指数（PE+PB）+ 中证官网交叉。"""
        codes = index_codes or list(BROAD_INDEX_CODES)
        return self.update_daily(codes)

    def update_daily(self, index_codes: list[str] | None = None) -> dict[str, Any]:
        """抓取主源（全历史 PE+PB）+ 交叉源（近 20 日）并原子 upsert。

        - 主源失败 → 保留旧值，登记 retry，整次返回 failed。
        - 交叉源失败/无覆盖 → 不阻断主源落库，结果中披露 cross_error/missing。
        """
        codes = index_codes or DEFAULT_INDEX_CODES
        report: dict[str, Any] = {"status": "success", "indexes": {}}
        for index_code in codes:
            primary_result = self._primary.fetch(FetchRequest(
                data_type="index_valuation", stock_codes=[index_code],
            ))
            index_report: dict[str, Any] = {"primary_rows": 0, "cross_rows": 0}
            if primary_result.metadata.error:
                index_report["primary_error"] = primary_result.metadata.error
                self._record_retry(index_code, primary_result.metadata.error)
                report["status"] = "failed"
                report["indexes"][index_code] = index_report
                continue

            batch_id = uuid.uuid4().hex
            fetch_time = datetime.now(UTC)
            with self.duck.transaction() as conn:
                if primary_result.data:
                    conn.executemany(
                        """INSERT INTO index_valuation
                           (index_code, trade_date, pe_ttm, pe_metric, pb, div_yield,
                            source, fetch_time, raw_hash, confidence, batch_id, extra)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(index_code, trade_date, source) DO UPDATE SET
                             pe_ttm=excluded.pe_ttm, pe_metric=excluded.pe_metric,
                             pb=excluded.pb, div_yield=excluded.div_yield,
                             fetch_time=excluded.fetch_time, raw_hash=excluded.raw_hash,
                             confidence=excluded.confidence, batch_id=excluded.batch_id,
                             extra=excluded.extra""",
                        [
                            [
                                row.get("index_code", index_code),
                                row.get("trade_date"),
                                row.get("pe_ttm"),
                                row.get("pe_metric"),
                                row.get("pb"),
                                row.get("div_yield"),
                                primary_result.metadata.source,
                                fetch_time,
                                primary_result.metadata.raw_response_hash,
                                primary_result.metadata.confidence,
                                batch_id,
                                row.get("extra"),
                            ]
                            for row in primary_result.data
                        ],
                    )
                    index_report["primary_rows"] = len(primary_result.data)
                else:
                    self._record_missing(index_code, "source_empty")

            # 交叉源：仅中证/上证系指数；失败不阻断主源
            if index_code not in CSINDEX_COVERED_CODES:
                index_report["cross_skipped"] = True
            else:
                try:
                    cross_result = self._cross.fetch(FetchRequest(
                        data_type="index_valuation", stock_codes=[index_code],
                    ))
                    if cross_result.metadata.error:
                        index_report["cross_error"] = cross_result.metadata.error
                    elif cross_result.data:
                        with self.duck.transaction() as conn:
                            conn.executemany(
                                """INSERT INTO index_valuation
                                   (index_code, trade_date, pe_ttm, pe_metric, pb, div_yield,
                                    source, fetch_time, raw_hash, confidence, batch_id, extra)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                   ON CONFLICT(index_code, trade_date, source) DO UPDATE SET
                                     pe_ttm=excluded.pe_ttm, pe_metric=excluded.pe_metric,
                                     pb=excluded.pb, div_yield=excluded.div_yield,
                                     fetch_time=excluded.fetch_time, raw_hash=excluded.raw_hash,
                                     confidence=excluded.confidence, batch_id=excluded.batch_id,
                                     extra=excluded.extra""",
                                [
                                    [
                                        row.get("index_code", index_code),
                                        row.get("trade_date"),
                                        row.get("pe_ttm"),
                                        row.get("pe_metric"),
                                        row.get("pb"),
                                        row.get("div_yield"),
                                        cross_result.metadata.source,
                                        fetch_time,
                                        cross_result.metadata.raw_response_hash,
                                        cross_result.metadata.confidence,
                                        batch_id,
                                        row.get("extra"),
                                    ]
                                    for row in cross_result.data
                                ],
                            )
                        index_report["cross_rows"] = len(cross_result.data)
                except Exception as error:  # noqa: BLE001
                    index_report["cross_error"] = f"{type(error).__name__}: {error}"

            report["indexes"][index_code] = index_report
        return report

    # ─── 申万一级行业更新 ─────────────────────────────────────────────

    def update_sw_industries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """抓取申万一级行业某窗口的日度 PE/PB 并原子 upsert。

        默认窗口为近 30 天；回填可传更早区间（akshare 按年翻页，2006+ 可用）。
        空响应（非交易日）按合法 missing 返回，不登记 retry。
        """
        request = FetchRequest(
            data_type="index_valuation",
            start_date=start_date,
            end_date=end_date,
        )
        result = self._sws.fetch(request)
        if result.metadata.error:
            self._record_retry(SW_INDUSTRY_GROUP, result.metadata.error, adapter="sws")
            return {"status": "failed", "error": result.metadata.error}
        if not result.data:
            self._record_missing(SW_INDUSTRY_GROUP, "source_empty")
            return {"status": "missing", "rows": 0}

        batch_id = uuid.uuid4().hex
        fetch_time = datetime.now(UTC)
        # 2026-09-05：118,591 行 executemany 在 DuckDB 1.5.5 下因 date/datetime
        # 绑定按 ~450KB/行堆积事务内存（reports/110 同款缺陷），直接 OOM/卡死。
        # 改用 pandas 向量化注册 + 单条 INSERT SELECT，同事务原子性不变。
        import pandas as pd

        frame = pd.DataFrame(result.data)
        for column in (
            "index_code", "trade_date", "pe_ttm", "pe_metric",
            "pb", "div_yield", "extra",
        ):
            if column not in frame.columns:
                frame[column] = None
        stage_view = f"_sw_index_valuation_stage_{uuid.uuid4().hex[:10]}"
        with self.duck.transaction() as conn:
            conn.register(stage_view, frame)
            conn.execute(
                f"""INSERT INTO index_valuation
                   (index_code, trade_date, pe_ttm, pe_metric, pb, div_yield,
                    source, fetch_time, raw_hash, confidence, batch_id, extra)
                   SELECT CAST(index_code AS VARCHAR),
                          CAST(trade_date AS DATE),
                          CAST(pe_ttm AS DOUBLE),
                          CAST(pe_metric AS VARCHAR),
                          CAST(pb AS DOUBLE),
                          CAST(div_yield AS DOUBLE),
                          CAST(? AS VARCHAR),
                          CAST(? AS TIMESTAMP),
                          ?,
                          ?,
                          CAST(? AS VARCHAR),
                          CAST(extra AS VARCHAR)
                   FROM {stage_view}
                   ON CONFLICT(index_code, trade_date, source) DO UPDATE SET
                     pe_ttm=excluded.pe_ttm, pe_metric=excluded.pe_metric,
                     pb=excluded.pb, div_yield=excluded.div_yield,
                     fetch_time=excluded.fetch_time, raw_hash=excluded.raw_hash,
                     confidence=excluded.confidence, batch_id=excluded.batch_id,
                     extra=excluded.extra""",
                [
                    result.metadata.source,
                    fetch_time,
                    result.metadata.raw_response_hash,
                    result.metadata.confidence,
                    batch_id,
                ],
            )
        return {
            "status": "success",
            "rows": len(result.data),
            "start_date": start_date,
            "end_date": end_date,
        }

    # ─── 低频自动集成入口 ─────────────────────────────────────────────

    def refresh_if_due(self) -> dict[str, Any]:
        """低频自动集成入口：每日最多一次（UTC+8）。

        当日已刷新则直接 skip；未刷新时依次更新宽基组（乐咕+中证交叉）
        与申万一级行业窗口，宽基组成功即记录 marker；申万失败不阻断宽基。
        """
        if self._refreshed_today():
            return {"status": "skipped", "reason": "refreshed_today"}
        broad_report = self.update_broad()
        try:
            sws_report = self.update_sw_industries()
        except Exception as error:  # noqa: BLE001
            sws_report = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        report = {
            "status": broad_report.get("status", "failed"),
            "broad": broad_report,
            "sws": sws_report,
        }
        if broad_report.get("status") == "success":
            self._mark_refreshed()
        return report

    def _refreshed_today(self) -> bool:
        value = self._last_refresh_value()
        if not value:
            return False
        try:
            refreshed = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return False
        return refreshed.astimezone(_CN_TZ).date() >= _cn_today()

    def _last_refresh_value(self) -> str | None:
        rows = self.sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?", [REFRESH_MARKER_KEY]
        )
        return rows[0].get("value") if rows else None

    def _mark_refreshed(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [REFRESH_MARKER_KEY, now, now],
            )

    # ─── retry / missing 维护 ─────────────────────────────────────

    def _record_retry(self, index_code: str, error: str, *, adapter: str = "legulegu") -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO retry_list
                       (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
                       VALUES (?, ?, ?, ?, 0, ?, '{}')
                       ON CONFLICT(stock_code, data_type, adapter, extra_json) DO UPDATE SET
                         error=excluded.error, last_attempt=excluded.last_attempt""",
                    [index_code, "index_valuation", adapter, error[:500],
                     datetime.now(UTC).isoformat()],
                )
        except Exception as e:
            logger.warning("记录指数估值失败信息失败: %s", e)

    def _record_missing(self, index_code: str, reason_code: str) -> None:
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [index_code, "index_valuation", reason_code],
                )
        except Exception as e:
            logger.warning("记录指数估值缺失信息失败: %s", e)

    # ─── 只读状态报告 ─────────────────────────────────────────────

    def status_report(self) -> dict[str, Any]:
        """返回指数估值域的覆盖与队列状态（只读，不抓取）。"""
        try:
            rows = self.duck.read_query(
                """SELECT source, index_code, COUNT(*) AS n,
                          MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
                   FROM index_valuation
                   GROUP BY source, index_code
                   ORDER BY source, index_code"""
            )
        except Exception as error:
            return {"status": "error", "error": str(error)}
        coverage = [
            {
                "source": row["source"],
                "index_code": row["index_code"],
                "rows": row["n"],
                "min_date": str(row["min_date"]),
                "max_date": str(row["max_date"]),
            }
            for row in rows
        ]
        retry_open = self.sqlite.query(
            "SELECT COUNT(*) AS count FROM retry_list WHERE data_type = 'index_valuation'",
        )[0]["count"]
        return {
            "status": "ok",
            "coverage": coverage,
            "retry_open": retry_open,
        }
