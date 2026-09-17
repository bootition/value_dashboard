"""ETF 工作台重置与初始化（2026-09-05 用户决定重新开始）

两类操作，均只动 ETF 操作域（SQLite），不动 DuckDB 行情/指数数据：
- ``reset_portfolio``：清空交易/资金/卖出计划/设置，同步默认池并停用池外
  旧持仓（保留元数据行，可追溯）。
- ``fresh_start``：真正回到“第一次使用”：删除全部 etf_meta 后重新植入
  默认三层观察池（34 只，budget=0、未录入任何持仓）。
- ``bootstrap_portfolio``：新用户初始化的一体化录入通道：策略总资产 +
  首批持仓（自动按买入流水落账）在同一个事务内完成。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.etf_pool import DEFAULT_ETF_POOL
from app.core.storage.sqlite_store import SQLiteStore

__all__ = ["reset_portfolio", "fresh_start", "bootstrap_portfolio"]

_COUNTS_SQL = {
    "trades": "SELECT COUNT(*) AS c FROM etf_trades",
    "cash_flows": "SELECT COUNT(*) AS c FROM etf_cash_flows",
    "sell_plans": "SELECT COUNT(*) AS c FROM etf_sell_plans",
    "settings": "SELECT COUNT(*) AS c FROM etf_settings",
    "meta": "SELECT COUNT(*) AS c FROM etf_meta",
}

_META_COLUMNS = (
    "etf_code", "name", "category", "track_index_code", "track_index_name",
    "primary_metric", "industry_group", "budget", "step_pct", "enabled",
    "note", "updated_at",
)


def _current_counts(sqlite: SQLiteStore) -> dict[str, int]:
    return {name: int(sqlite.query(sql)[0]["c"]) for name, sql in _COUNTS_SQL.items()}


def _seed_pool(conn: Any, now: str) -> None:
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


def reset_portfolio(sqlite: SQLiteStore, *, dry_run: bool = True) -> dict[str, Any]:
    """预览或执行 ETF 工作台重置（保留元数据，停用池外标的）。默认只预览。"""
    counts = _current_counts(sqlite)
    pool_codes = {item["etf_code"] for item in DEFAULT_ETF_POOL}
    non_pool_rows = sqlite.query(
        "SELECT etf_code, name FROM etf_meta WHERE enabled = 1 AND etf_code NOT IN "
        f"({', '.join('?' for _ in pool_codes)})",
        list(pool_codes),
    )
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "current": counts,
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
        _seed_pool(conn, now)
        if pool_codes:
            conn.execute(
                f"""UPDATE etf_meta SET enabled = 0, updated_at = ?
                    WHERE etf_code NOT IN ({', '.join('?' for _ in pool_codes)})""",
                [now, *pool_codes],
            )
    report["status"] = "reset_done"
    return report


def fresh_start(sqlite: SQLiteStore, *, dry_run: bool = True) -> dict[str, Any]:
    """真正的新用户初始化：清空所有 ETF 操作数据，重建默认观察池。

    DuckDB 的 etf_daily（行情）与 index_valuation（指数估值）保留，
    否则重新拉取行情无意义。
    """
    counts = _current_counts(sqlite)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "current": counts,
        "pool_to_seed": len(DEFAULT_ETF_POOL),
        "meta_to_delete": counts["meta"],
        "kept_readonly": ["etf_daily", "index_valuation"],
    }
    if dry_run:
        report["status"] = "preview"
        return report

    now = _now_iso()
    with sqlite.transaction() as conn:
        for table in ("etf_trades", "etf_cash_flows", "etf_sell_plans", "etf_settings", "etf_meta"):
            conn.execute(f"DELETE FROM {table}")
        _seed_pool(conn, now)
    report["status"] = "fresh_start_done"
    return report


def _normalize_position(position: dict[str, Any], index: int) -> dict[str, Any]:
    code = str(position.get("etf_code") or "").strip()
    if not code:
        raise ValueError(f"第 {index + 1} 个持仓缺少 etf_code")
    name = str(position.get("name") or "").strip()
    if not name:
        raise ValueError(f"ETF {code} 缺少名称")
    category = str(position.get("category") or "industry").strip().lower()
    if category not in {"industry", "strategy", "market"}:
        raise ValueError(f"ETF {code} 的 category 不合法: {category}")
    primary_metric = str(position.get("primary_metric") or "pe").strip().lower()
    if primary_metric not in {"pe", "pb"}:
        raise ValueError(f"ETF {code} 的 primary_metric 必须是 pe/pb")

    budget = float(position.get("budget") or 0)
    if budget < 0:
        raise ValueError(f"ETF {code} 的预算不得为负")
    step_pct = float(position.get("step_pct") or 5)
    if step_pct <= 0 or step_pct > 20:
        raise ValueError(f"ETF {code} 的网格间距必须在 (0, 20]")

    shares = float(position.get("shares") or 0)
    if shares < 0:
        raise ValueError(f"ETF {code} 的份额不得为负")
    if shares > 0:
        price = float(position.get("price") or 0)
        if price <= 0:
            raise ValueError(f"ETF {code} 的买入价格必须为正数")
        trade_date = str(position.get("trade_date") or "").strip()[:10]
        try:
            date.fromisoformat(trade_date)
        except ValueError as error:
            raise ValueError(f"ETF {code} 的买入日期不合法: {trade_date!r}") from error
        fee = float(position.get("fee") or 0)
        if fee < 0:
            raise ValueError(f"ETF {code} 的手续费不得为负")
    else:
        price = None
        trade_date = None
        fee = 0.0

    return {
        "etf_code": code,
        "name": name,
        "category": category,
        "track_index_code": position.get("track_index_code"),
        "track_index_name": position.get("track_index_name"),
        "primary_metric": primary_metric,
        "industry_group": position.get("industry_group"),
        "budget": budget,
        "step_pct": step_pct,
        "shares": shares,
        "price": price,
        "trade_date": trade_date,
        "fee": fee,
    }


def bootstrap_portfolio(
    sqlite: SQLiteStore,
    *,
    total_assets: float | None,
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """新用户初始化通道：总资产 + 首批持仓一次录入（单事务，原子）。"""
    if total_assets is not None and total_assets < 0:
        raise ValueError("策略总资产不得为负")
    normalized = [_normalize_position(position, i) for i, position in enumerate(positions)]

    now = _now_iso()
    with sqlite.transaction() as conn:
        if total_assets is not None:
            conn.execute(
                """INSERT INTO etf_settings (key, value, updated_at)
                   VALUES ('total_assets', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                 updated_at=excluded.updated_at""",
                [str(total_assets), now],
            )
        for position in normalized:
            conn.execute(
                f"""INSERT INTO etf_meta ({', '.join(_META_COLUMNS)})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
                    ON CONFLICT(etf_code) DO UPDATE SET
                      name=excluded.name, category=excluded.category,
                      track_index_code=excluded.track_index_code,
                      track_index_name=excluded.track_index_name,
                      primary_metric=excluded.primary_metric,
                      industry_group=excluded.industry_group,
                      budget=excluded.budget, step_pct=excluded.step_pct,
                      enabled=1, note=NULL, updated_at=excluded.updated_at""",
                [
                    position["etf_code"], position["name"], position["category"],
                    position["track_index_code"], position["track_index_name"],
                    position["primary_metric"], position["industry_group"],
                    position["budget"], position["step_pct"], now,
                ],
            )
            if position["shares"] > 0:
                conn.execute(
                    """INSERT INTO etf_trades
                       (etf_code, trade_date, direction, price, shares, amount, fee, note, created_at)
                       VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?)""",
                    [
                        position["etf_code"], position["trade_date"], position["price"],
                        position["shares"], position["price"] * position["shares"],
                        position["fee"], "初始化持仓", now,
                    ],
                )
    return {
        "status": "bootstrapped",
        "total_assets": total_assets,
        "positions_written": len(normalized),
        "trades_written": sum(1 for position in normalized if position["shares"] > 0),
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
