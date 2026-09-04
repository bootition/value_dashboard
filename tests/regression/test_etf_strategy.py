"""ETF 轮动工作台核心引擎回归（2026-09-05）

覆盖 requirements.md 定稿口径：
- 信号区 20/80 阈值与 unavailable
- 摊余成本持仓（买入含费、卖出净收、已实现盈亏、超额卖出报错）
- 手动预算、单档=预算÷10、默认 5% 网格、10 档上限
- 卖出计划：触发时持仓市值÷10 锁定单档，首档锚点=触发价，
  之后锚点=最近卖出价，第 10 档后 clear_tail
"""

from __future__ import annotations

import pytest

from app.core.etf_strategy import (
    MAX_TRANCHES,
    add_cash_flow,
    add_etf_trade,
    ensure_sell_plan,
    get_setting,
    grid_state,
    latest_close,
    position_summary,
    set_setting,
    signal_zone,
    upsert_etf_meta,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _seed_meta(sqlite: SQLiteStore, *, budget: float = 1000.0, step_pct: float = 5.0) -> None:
    upsert_etf_meta(
        sqlite, etf_code="512880", name="证券ETF",
        track_index_code="SW801790", track_index_name="非银金融",
        primary_metric="pb", industry_group="金融", budget=budget, step_pct=step_pct,
    )


def test_signal_zone_boundaries() -> None:
    assert signal_zone(None) == "unavailable"
    assert signal_zone(19.99) == "buy"
    assert signal_zone(20.0) == "neutral"
    assert signal_zone(80.0) == "neutral"
    assert signal_zone(80.01) == "sell"


def test_meta_validation(sqlite_store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        upsert_etf_meta(sqlite_store, etf_code="512880", name="证券ETF", primary_metric="ps")
    with pytest.raises(ValueError):
        upsert_etf_meta(sqlite_store, etf_code="512880", name="证券ETF", step_pct=0)
    with pytest.raises(ValueError):
        upsert_etf_meta(sqlite_store, etf_code="512880", name="证券ETF", budget=-1)


def test_position_summary_average_cost_with_fees(sqlite_store: SQLiteStore) -> None:
    _seed_meta(sqlite_store)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-01-05",
                  direction="buy", price=1.0, shares=100, fee=0.1)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-01-06",
                  direction="buy", price=0.9, shares=100, fee=0.1)

    position = position_summary(sqlite_store, "512880")
    assert position["shares"] == pytest.approx(200)
    assert position["cost_basis"] == pytest.approx(190.2)
    assert position["avg_cost"] == pytest.approx(0.951)
    assert position["buy_count"] == 2
    assert position["last_buy_price"] == pytest.approx(0.9)

    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-02-01",
                  direction="sell", price=1.2, shares=100, fee=0.1)
    position = position_summary(sqlite_store, "512880")
    assert position["shares"] == pytest.approx(100)
    assert position["cost_basis"] == pytest.approx(95.1)
    assert position["realized_pnl"] == pytest.approx(24.8)
    assert position["sell_count"] == 1
    assert position["last_sell_price"] == pytest.approx(1.2)


def test_position_summary_rejects_oversell(sqlite_store: SQLiteStore) -> None:
    _seed_meta(sqlite_store)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-01-05",
                  direction="buy", price=1.0, shares=100)
    with pytest.raises(ValueError, match="超过持仓"):
        add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-01-06",
                      direction="sell", price=1.1, shares=101)


def test_grid_buy_side(sqlite_store: SQLiteStore) -> None:
    _seed_meta(sqlite_store, budget=1000.0, step_pct=5.0)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-01-05",
                  direction="buy", price=1.0, shares=100, fee=0.1)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-02-05",
                  direction="buy", price=0.95, shares=100, fee=0.1)

    state = grid_state(sqlite_store, etf_code="512880", current_price=0.9, signal="buy")

    assert state["tranche_amount"] == pytest.approx(100.0)
    assert state["used_budget"] == pytest.approx(195.2)
    assert state["budget_left"] == pytest.approx(804.8)
    assert state["remaining_buys"] == 8
    assert state["next_buy_price"] == pytest.approx(0.95 * 0.95)
    assert state["remaining_sells"] == 10
    assert state["next_sell_price"] is None, "未触发 80% 分位不得生成卖出档"


def test_grid_buy_stops_at_ten_tranches(sqlite_store: SQLiteStore) -> None:
    _seed_meta(sqlite_store, budget=1000.0, step_pct=5.0)
    for i in range(MAX_TRANCHES):
        add_etf_trade(sqlite_store, etf_code="512880",
                      trade_date=f"2026-01-{min(i + 1, 28):02d}",
                      direction="buy", price=1.0 - i * 0.05, shares=10)
    state = grid_state(sqlite_store, etf_code="512880", current_price=0.5, signal="buy")
    assert state["remaining_buys"] == 0
    assert state["next_buy_price"] is None


def test_sell_plan_locks_tranche_and_anchors(sqlite_store: SQLiteStore) -> None:
    _seed_meta(sqlite_store)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-01-05",
                  direction="buy", price=1.0, shares=200)

    # 首次触发：单档 = 触发时持仓市值 ÷ 10；锚点 = 触发价
    state = grid_state(sqlite_store, etf_code="512880", current_price=1.2,
                       signal="sell", today="2026-03-01")
    assert state["sell_tranche_amount"] == pytest.approx(1.2 * 200 / 10)
    assert state["next_sell_price"] == pytest.approx(1.2 * 1.05)

    # 卖出两档后：锚点切换为最近卖出价
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-03-02",
                  direction="sell", price=1.26, shares=20, amount=25.2)
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-03-03",
                  direction="sell", price=1.323, shares=20, amount=26.46)
    state = grid_state(sqlite_store, etf_code="512880", current_price=1.4, signal="sell")
    assert state["remaining_sells"] == 8
    # 引擎对外保留 4 位小数（1.323 × 1.05 = 1.38915 → 1.3892）
    assert state["next_sell_price"] == pytest.approx(1.3892, abs=1e-3)
    assert state["sell_tranches_done"] == 2

    # 十档全部卖出后：剩尾仓必须一次性清空
    for i in range(8):
        add_etf_trade(sqlite_store, etf_code="512880",
                      trade_date=f"2026-03-{min(i + 4, 28):02d}",
                      direction="sell", price=1.4, shares=5, amount=7.0)
    state = grid_state(sqlite_store, etf_code="512880", current_price=1.5, signal="sell")
    assert state["clear_tail"] is True
    assert state["next_sell_price"] is None


def test_settings_and_cash_flows_roundtrip(sqlite_store: SQLiteStore) -> None:
    assert get_setting(sqlite_store, "total_assets") is None
    set_setting(sqlite_store, "total_assets", "4100.99")
    assert get_setting(sqlite_store, "total_assets") == "4100.99"
    add_cash_flow(sqlite_store, flow_date="2026-03-23", direction="in", amount=493.72)
    rows = sqlite_store.query("SELECT COUNT(*) AS c FROM etf_cash_flows")
    assert rows[0]["c"] == 1


def test_latest_close_reads_etf_daily(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        """INSERT INTO etf_daily
           (etf_code, trade_date, close_price, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('512880', '2026-09-03', 1.234, 'ths', CURRENT_TIMESTAMP, 'h', 'strict', 'b')"""
    )
    assert latest_close(duckdb_store, "512880") == pytest.approx(1.234)
    assert latest_close(duckdb_store, "999999") is None


def test_ensure_sell_plan_idempotent(sqlite_store: SQLiteStore) -> None:
    first = ensure_sell_plan(sqlite_store, etf_code="512880", trigger_date="2026-03-01",
                             trigger_price=1.2, shares=200)
    second = ensure_sell_plan(sqlite_store, etf_code="512880", trigger_date="2026-03-02",
                              trigger_price=1.3, shares=180)
    assert first["created"] is True
    assert second["created"] is False
    assert second["tranche_amount"] == pytest.approx(24.0), "卖出计划必须锁定首次触发的单档金额"
