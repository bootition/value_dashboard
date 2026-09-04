"""ETF 轮动策略工作台核心引擎（2026-09-05）

口径（requirements.md）：
- 网格间距默认 5%（3/4/5 可配）；买卖最多各 10 档。
- 买入：每只 ETF 手动填预算，单档金额 = 预算 ÷ 10；加仓只从该预算出；
  下一买入价 = 最近一次买入价 × (1 − step)；预算用完或满 10 档停止。
- 卖出：主指标分位 >80% 触发首档；单档金额 = 触发时该 ETF 持仓市值 ÷ 10
  （触发时一次性锁定）；下一卖出价 = 最近一次卖出价 × (1 + step)；
  第 10 档把剩余持仓一次性清空。
- 成本：买入成本含手续费；卖出净收扣手续费；摊余成本法计算持仓成本。
- 信号区：主指标近 10 年分位 <20% 买入观察区、>80% 卖出观察区，其余中性。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.core.storage.sqlite_store import SQLiteStore

DEFAULT_STEP_PCT = 5.0
MAX_TRANCHES = 10
SELL_TRANCHES = 10

__all__ = [
    "DEFAULT_STEP_PCT",
    "MAX_TRANCHES",
    "SELL_TRANCHES",
    "signal_zone",
    "load_etf_meta",
    "upsert_etf_meta",
    "add_etf_trade",
    "add_cash_flow",
    "get_setting",
    "set_setting",
    "position_summary",
    "ensure_sell_plan",
    "grid_state",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _valid_date(value: Any) -> str:
    s = str(value)[:10]
    date.fromisoformat(s)  # 非法日期直接抛 ValueError
    return s


def signal_zone(percentile: float | None) -> str:
    """分位 → 信号区。None（数据不可得）如实 unavailable。"""
    if percentile is None:
        return "unavailable"
    if percentile < 20:
        return "buy"
    if percentile > 80:
        return "sell"
    return "neutral"


# ─── ETF 元数据（用户维护） ────────────────────────────────────────────

def load_etf_meta(sqlite: SQLiteStore) -> list[dict[str, Any]]:
    return sqlite.query(
        """SELECT etf_code, name, track_index_code, track_index_name,
                  primary_metric, industry_group, budget, step_pct, enabled, note
           FROM etf_meta ORDER BY etf_code"""
    )


def upsert_etf_meta(
    sqlite: SQLiteStore,
    *,
    etf_code: str,
    name: str,
    track_index_code: str | None = None,
    track_index_name: str | None = None,
    primary_metric: str = "pe",
    industry_group: str | None = None,
    budget: float = 0.0,
    step_pct: float = DEFAULT_STEP_PCT,
    enabled: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    if primary_metric not in {"pe", "pb"}:
        raise ValueError("primary_metric 必须是 pe 或 pb")
    if step_pct <= 0 or step_pct > 20:
        raise ValueError("step_pct 必须在 (0, 20] 之间")
    if budget < 0:
        raise ValueError("budget 不得为负")
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO etf_meta
               (etf_code, name, track_index_code, track_index_name, primary_metric,
                industry_group, budget, step_pct, enabled, note, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(etf_code) DO UPDATE SET
                 name=excluded.name, track_index_code=excluded.track_index_code,
                 track_index_name=excluded.track_index_name,
                 primary_metric=excluded.primary_metric,
                 industry_group=excluded.industry_group, budget=excluded.budget,
                 step_pct=excluded.step_pct, enabled=excluded.enabled,
                 note=excluded.note, updated_at=excluded.updated_at""",
            [etf_code, name, track_index_code, track_index_name, primary_metric,
             industry_group, budget, step_pct, 1 if enabled else 0, note, _now()],
        )
    return {"etf_code": etf_code, "updated": True}


# ─── 交易与资金流水 ────────────────────────────────────────────────────

def add_etf_trade(
    sqlite: SQLiteStore,
    *,
    etf_code: str,
    trade_date: str,
    direction: str,
    price: float,
    shares: float,
    amount: float | None = None,
    fee: float = 0.0,
    note: str | None = None,
) -> dict[str, Any]:
    """录入一笔交易：amount 缺省按 price×shares；手续费默认 0。"""
    if direction not in {"buy", "sell"}:
        raise ValueError("direction 必须是 buy 或 sell")
    if price <= 0 or shares <= 0:
        raise ValueError("price/shares 必须为正数")
    if fee < 0:
        raise ValueError("fee 不得为负")
    if direction == "sell":
        held = position_summary(sqlite, etf_code)["shares"]
        if shares > held + 1e-9:
            raise ValueError(f"{etf_code} 卖出份额超过持仓（流水不一致）")
    resolved_amount = amount if amount is not None else price * shares
    trade_date = _valid_date(trade_date)
    with sqlite.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO etf_trades
               (etf_code, trade_date, direction, price, shares, amount, fee, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [etf_code, trade_date, direction, price, shares, resolved_amount, fee, note, _now()],
        )
    return {"id": cursor.lastrowid, "etf_code": etf_code}


def add_cash_flow(
    sqlite: SQLiteStore,
    *,
    flow_date: str,
    direction: str,
    amount: float,
    note: str | None = None,
) -> dict[str, Any]:
    if direction not in {"in", "out"}:
        raise ValueError("direction 必须是 in 或 out")
    if amount <= 0:
        raise ValueError("amount 必须为正数")
    flow_date = _valid_date(flow_date)
    with sqlite.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO etf_cash_flows (flow_date, direction, amount, note, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [flow_date, direction, amount, note, _now()],
        )
    return {"id": cursor.lastrowid, "flow_date": flow_date}


# ─── 设置（总资产等） ──────────────────────────────────────────────────

def get_setting(sqlite: SQLiteStore, key: str, default: str | None = None) -> str | None:
    rows = sqlite.query("SELECT value FROM etf_settings WHERE key = ?", [key])
    return rows[0]["value"] if rows else default


def set_setting(sqlite: SQLiteStore, key: str, value: str) -> None:
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO etf_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            [key, value, _now()],
        )


# ─── 持仓汇总（摊余成本法） ────────────────────────────────────────────

def position_summary(sqlite: SQLiteStore, etf_code: str) -> dict[str, Any]:
    trades = sqlite.query(
        """SELECT id, trade_date, direction, price, shares, amount, fee
           FROM etf_trades WHERE etf_code = ? ORDER BY trade_date, id""",
        [etf_code],
    )
    shares = 0.0
    cost_basis = 0.0
    realized_pnl = 0.0
    buy_count = 0
    sell_count = 0
    total_buy_amount = 0.0
    total_buy_fee = 0.0
    total_sell_amount = 0.0
    total_sell_fee = 0.0
    last_buy_price: float | None = None
    last_sell_price: float | None = None
    first_buy_date: str | None = None

    for row in trades:
        amount = float(row["amount"])
        fee = float(row["fee"] or 0)
        price = float(row["price"])
        trade_shares = float(row["shares"])
        if row["direction"] == "buy":
            if first_buy_date is None:
                first_buy_date = str(row["trade_date"])[:10]
            shares += trade_shares
            cost_basis += amount + fee
            buy_count += 1
            total_buy_amount += amount
            total_buy_fee += fee
            last_buy_price = price
        else:
            if trade_shares > shares + 1e-9:
                raise ValueError(f"{etf_code} 卖出份额超过持仓（流水不一致）")
            avg_cost = cost_basis / shares if shares > 0 else 0.0
            sell_shares = min(trade_shares, shares)
            cost_out = avg_cost * sell_shares
            shares -= sell_shares
            cost_basis -= cost_out
            realized_pnl += (amount - fee) - cost_out
            sell_count += 1
            total_sell_amount += amount
            total_sell_fee += fee
            last_sell_price = price

    avg_cost = cost_basis / shares if shares > 0 else None
    return {
        "etf_code": etf_code,
        "shares": shares,
        "cost_basis": cost_basis,
        "avg_cost": avg_cost,
        "realized_pnl": realized_pnl,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_buy_amount": total_buy_amount,
        "total_buy_fee": total_buy_fee,
        "total_sell_amount": total_sell_amount,
        "total_sell_fee": total_sell_fee,
        "last_buy_price": last_buy_price,
        "last_sell_price": last_sell_price,
        "first_buy_date": first_buy_date,
    }


# ─── 卖出计划（触发 80% 分位时锁定） ───────────────────────────────────

def ensure_sell_plan(
    sqlite: SQLiteStore,
    *,
    etf_code: str,
    trigger_date: str,
    trigger_price: float,
    shares: float,
) -> dict[str, Any]:
    """首档卖出触发时锁定计划：单档金额 = 触发时持仓市值 ÷ 10。"""
    if trigger_price <= 0 or shares <= 0:
        raise ValueError("trigger_price/shares 必须为正数")
    trigger_date = _valid_date(trigger_date)
    rows = sqlite.query(
        "SELECT tranche_amount, tranches_done FROM etf_sell_plans WHERE etf_code = ?",
        [etf_code],
    )
    if rows:
        return {**rows[0], "created": False}
    tranche_amount = trigger_price * shares / SELL_TRANCHES
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO etf_sell_plans
               (etf_code, trigger_date, trigger_price, tranche_amount, tranches_done, updated_at)
               VALUES (?, ?, ?, ?, 0, ?)
               ON CONFLICT(etf_code) DO NOTHING""",
            [etf_code, trigger_date, trigger_price, tranche_amount, _now()],
        )
    return {
        "trigger_date": trigger_date,
        "trigger_price": trigger_price,
        "tranche_amount": tranche_amount,
        "tranches_done": 0,
        "created": True,
    }


# ─── 网格状态（信号 + 网格下一档） ─────────────────────────────────────

def grid_state(
    sqlite: SQLiteStore,
    *,
    etf_code: str,
    current_price: float | None,
    signal: str,
    today: str | None = None,
    persist_sell_plan: bool = True,
) -> dict[str, Any]:
    """汇总一只 ETF 的持仓、预算、网格与信号。

    signal: buy / sell / neutral / unavailable（来自 signal_zone）。
    current_price 为最新收盘价；无价格时盈亏与市值置 None。
    """
    meta_rows = sqlite.query("SELECT * FROM etf_meta WHERE etf_code = ?", [etf_code])
    if not meta_rows:
        raise KeyError(f"ETF 未配置: {etf_code}")
    meta = meta_rows[0]
    position = position_summary(sqlite, etf_code)
    step = float(meta["step_pct"]) / 100.0
    budget = float(meta["budget"] or 0)
    tranche_amount = budget / MAX_TRANCHES if budget > 0 else 0.0

    # 买入侧
    remaining_buys = max(0, MAX_TRANCHES - position["buy_count"])
    used_budget = position["total_buy_amount"] + position["total_buy_fee"]
    budget_left = max(0.0, budget - used_budget)
    next_buy_price = None
    if position["last_buy_price"] is not None and remaining_buys > 0:
        next_buy_price = position["last_buy_price"] * (1 - step)

    # 卖出侧（档数以实际卖出的交易笔数为准，卖出计划只锁定单档金额与首档锚点）
    plan_rows = sqlite.query(
        "SELECT trigger_date, trigger_price, tranche_amount, tranches_done FROM etf_sell_plans WHERE etf_code = ?",
        [etf_code],
    )
    sell_plan = plan_rows[0] if plan_rows else None
    remaining_sells = max(0, SELL_TRANCHES - position["sell_count"])
    next_sell_price = None
    sell_tranche_amount = sell_plan["tranche_amount"] if sell_plan else 0.0
    if position["shares"] > 0 and signal == "sell":
        if sell_plan is None and current_price is not None:
            if persist_sell_plan:
                plan_date = today or datetime.now(UTC).astimezone().date().isoformat()
                sell_plan = ensure_sell_plan(
                    sqlite, etf_code=etf_code, trigger_date=plan_date,
                    trigger_price=current_price, shares=position["shares"],
                )
                sell_tranche_amount = float(sell_plan["tranche_amount"])
            else:
                sell_tranche_amount = current_price * position["shares"] / SELL_TRANCHES
        if sell_plan and position["sell_count"] < SELL_TRANCHES:
            anchor = (
                position["last_sell_price"]
                if position["last_sell_price"] is not None
                else float(sell_plan["trigger_price"])
            )
            next_sell_price = anchor * (1 + step)
        elif (
            sell_plan is None
            and current_price is not None
            and position["sell_count"] < SELL_TRANCHES
        ):
            # 只读预览：不落计划时以当前价作为首档锚点估算
            next_sell_price = current_price * (1 + step)

    market_value = position["shares"] * current_price if current_price is not None else None
    unrealized_pnl = (
        market_value - position["cost_basis"]
        if market_value is not None and position["cost_basis"] is not None
        else None
    )

    return {
        "etf_code": etf_code,
        "name": meta["name"],
        "track_index_code": meta["track_index_code"],
        "track_index_name": meta["track_index_name"],
        "primary_metric": meta["primary_metric"],
        "industry_group": meta["industry_group"],
        "step_pct": float(meta["step_pct"]),
        "budget": budget,
        "tranche_amount": round(tranche_amount, 4),
        "used_budget": round(used_budget, 4),
        "budget_left": round(budget_left, 4),
        "signal": signal,
        "position": position,
        "current_price": current_price,
        "market_value": round(market_value, 2) if market_value is not None else None,
        "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
        "remaining_buys": remaining_buys,
        "next_buy_price": round(next_buy_price, 4) if next_buy_price is not None else None,
        "remaining_sells": remaining_sells,
        "next_sell_price": round(next_sell_price, 4) if next_sell_price is not None else None,
        "sell_tranche_amount": round(sell_tranche_amount, 4),
        "sell_tranches_done": min(position["sell_count"], SELL_TRANCHES),
        "clear_tail": position["shares"] > 0 and position["sell_count"] >= SELL_TRANCHES,
        "enabled": bool(meta["enabled"]),
    }


def latest_close(duck: object, etf_code: str) -> float | None:
    """最新收盘价（优先 ths 源；只读）。"""
    rows = duck.read_query(
        """SELECT close_price FROM etf_daily
           WHERE etf_code = ? AND close_price IS NOT NULL
           ORDER BY trade_date DESC, source LIMIT 1""",
        [etf_code],
    )
    return float(rows[0]["close_price"]) if rows else None
