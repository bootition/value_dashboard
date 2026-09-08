"""ETF 工作台重置（重新开始，2026-09-05 用户决定清空全部持仓）

清空范围（只动 ETF 操作域，不动行情/指数数据）：
- etf_trades / etf_cash_flows / etf_sell_plans / etf_settings 清空；
- 同步默认池：池内 34 只重置 budget=0、启用、层级/跟踪指数按池定义校正；
- 池外旧持仓（酒/医疗/医疗器械/中药/互联中概/红利50/养殖等）启用位清零，
  不再参与观察，记录保留可追溯；
- DuckDB 的 etf_daily 与 index_valuation 不动。
"""

from __future__ import annotations

from typing import Any

from app.core.etf_pool import DEFAULT_ETF_POOL
from app.core.storage.sqlite_store import SQLiteStore

__all__ = ["reset_portfolio"]

_COUNTS_SQL = {
    "trades": "SELECT COUNT(*) AS c FROM etf_trades",
    "cash_flows": "SELECT COUNT(*) AS c FROM etf_cash_flows",
    "sell_plans": "SELECT COUNT(*) AS c FROM etf_sell_plans",
    "settings": "SELECT COUNT(*) AS c FROM etf_settings",
}


def reset_portfolio(sqlite: SQLiteStore, *, dry_run: bool = True) -> dict[str, Any]:
    """预览或执行 ETF 工作台重置。默认 dry_run，执行必须显式传 dry_run=False。"""
    counts = {name: int(sqlite.query(sql)[0]["c"]) for name, sql in _COUNTS_SQL.items()}
    pool_codes = {item["etf_code"] for item in DEFAULT_ETF_POOL}
    non_pool_rows = sqlite.query(
        "SELECT etf_code, name FROM etf_meta WHERE enabled = 1 AND etf_code NOT IN "
        f"({', '.join('?' for _ in pool_codes)})",
        list(pool_codes),
    )
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "current": counts,
        "meta_total": int(sqlite.query("SELECT COUNT(*) AS c FROM etf_meta")[0]["c"]),
        "pool_synced": len(pool_codes),
        "non_pool_to_disable": [
            {"etf_code": row["etf_code"], "name": row["name"]} for row in non_pool_rows
        ],
    }
    if dry_run:
        report["status"] = "preview"
        return report

    now = _now_iso()
    with sqlite.transaction() as conn:
        for table in ("etf_trades", "etf_cash_flows", "etf_sell_plans", "etf_settings"):
            conn.execute(f"DELETE FROM {table}")
        for item in DEFAULT_ETF_POOL:
            conn.execute(
                """INSERT INTO etf_meta
                   (etf_code, name, category, track_index_code, track_index_name,
                    primary_metric, industry_group, budget, step_pct, enabled, note, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 5, 1, NULL, ?)
                   ON CONFLICT(etf_code) DO UPDATE SET
                     name=excluded.name, category=excluded.category,
                     track_index_code=excluded.track_index_code,
                     track_index_name=excluded.track_index_name,
                     primary_metric=excluded.primary_metric,
                     industry_group=excluded.industry_group,
                     budget=0, enabled=1, note=NULL, updated_at=excluded.updated_at""",
                [
                    item["etf_code"], item["name"], item["category"],
                    item["track_index_code"], item["track_index_name"],
                    item["primary_metric"], item["industry_group"], now,
                ],
            )
        if pool_codes:
            conn.execute(
                f"""UPDATE etf_meta SET enabled = 0, updated_at = ?
                    WHERE etf_code NOT IN ({', '.join('?' for _ in pool_codes)})""",
                [now, *pool_codes],
            )
    report["status"] = "reset_done"
    return report


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
