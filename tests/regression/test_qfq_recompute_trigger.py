"""J6 回归：qfq 补齐后到必须触发技术指标重算（reports/120 §3 事实 4）。

背景
----
`annualized_volatility / period_return / max_drawdown` 曾只有 17.8% 覆盖，
手动 `compute_indicators` 一次就回到 99.6% —— 数据本身没问题，缺的是
"**复权价补齐后自动重算**"的触发：

- 价格步骤"成功"只代表 raw 落库成功；这批价格域指标全部读 `qfq`；
- 下一轮补 qfq 时 raw 已经最新 → `_stale_price_snapshot_codes()`（比的是
  raw 日期）判"没过期"、价格增量路径又按"最新价没变"跳过 →
  重算集合为空，指标永久停在旧值。

钉死的不变量
------------
1. `qfq 最新日 < raw 最新日` 的股票必须进 `data_refresh_state['tech_recompute_pending']`；
2. 下一轮指标计算把它**强制**并入重算集合（与指纹/价格变化无关）；
3. 重算成功后从名单移除；被拒/失败/隔离的继续保留；
4. 名单有上限（2000）与 TTL（3 天），不会无限增长。

全部用 tmp_path 里的临时 DuckDB/SQLite（S1 隔离），不触碰 data/ 正式库。
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

from app.core.update import IncrementalUpdater

PENDING_KEY = "tech_recompute_pending"


def _seed_stock(duckdb_store, code: str = "600519") -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, is_listed, listing_date, total_shares, circ_shares)
           VALUES (?, 'x', 'SHSE', TRUE, '2020-01-01', 100, 80)""",
        [code],
    )


def _seed_prices(
    duckdb_store, code: str, *, raw_dates: list[str], qfq_dates: list[str],
) -> None:
    duckdb_store.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES "
        + ", ".join("(?, ?, ?)" for _ in raw_dates),
        [value for trade_date in raw_dates for value in (code, trade_date, 9.0)],
    )
    duckdb_store.write_query(
        "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES "
        + ", ".join("(?, ?, ?)" for _ in qfq_dates),
        [value for trade_date in qfq_dates for value in (code, trade_date, 9.0)],
    )


def _updater(duckdb_store, sqlite_store) -> IncrementalUpdater:
    return IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)


def _read_state(sqlite_store, key: str = PENDING_KEY):
    rows = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = ?", [key],
    )
    assert rows, f"{key} 未写入 data_refresh_state"
    return json.loads(rows[0]["value"])


def _write_state(sqlite_store, value: str, key: str = PENDING_KEY) -> None:
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        [key, value, datetime.now(UTC).isoformat()],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. raw 有新日、qfq 落后 → 进名单
# ─────────────────────────────────────────────────────────────────────────────


def test_qfq_lagging_behind_raw_is_detected(duckdb_store, sqlite_store) -> None:
    """判据以**该股自己的 raw 最新日**为准，不是全市场目标交易日。"""
    _seed_stock(duckdb_store, "600519")
    _seed_stock(duckdb_store, "000001")
    _seed_prices(
        duckdb_store, "600519",
        raw_dates=["2026-11-11", "2026-11-12", "2026-11-13"],
        qfq_dates=["2026-11-11", "2026-11-12"],          # qfq 落后一天
    )
    _seed_prices(
        duckdb_store, "000001",
        raw_dates=["2026-11-12", "2026-11-13"],
        qfq_dates=["2026-11-12", "2026-11-13"],          # 两侧齐平
    )

    lag_codes = _updater(duckdb_store, sqlite_store)._qfq_lag_codes_vs_raw(
        ["600519", "000001"],
    )

    assert lag_codes == ["600519"]


def test_qfq_lag_codes_are_written_to_pending_state(
    duckdb_store, sqlite_store,
) -> None:
    """价格步骤收尾：qfq<raw 的股票写进 data_refresh_state 待重算名单。"""
    _seed_stock(duckdb_store, "600519")
    _seed_prices(
        duckdb_store, "600519",
        raw_dates=["2026-11-12", "2026-11-13"],
        qfq_dates=["2026-11-12"],
    )
    updater = _updater(duckdb_store, sqlite_store)

    lag_codes = updater._qfq_lag_codes_vs_raw(["600519"])
    marked = updater._mark_tech_recompute_pending(lag_codes)

    assert marked == 1
    assert updater._load_tech_recompute_pending() == ["600519"]
    stored = _read_state(sqlite_store)
    # 值必须是 JSON 数组（运维可直接 SELECT 出来看），且带标记时间供 TTL 判定
    assert isinstance(stored, list) and stored
    assert stored[0]["code"] == "600519"
    assert stored[0]["marked_at"]


def test_price_step_and_flow_are_wired_to_pending_set() -> None:
    """接线守卫：价格步骤收尾写名单、指标步骤读名单并清名单。

    纯行为测试跑不到真实网络抓取，所以这里用源码守卫钉住"调用存在"，
    防止后续重构把它悄悄删掉（删掉就等于 J6 复发，且测试全绿）。
    """
    price_source = inspect.getsource(IncrementalUpdater._update_prices_incremental)
    assert "_qfq_lag_watermarks" in price_source
    assert "_mark_tech_recompute_pending" in price_source

    flow_source = inspect.getsource(IncrementalUpdater._run_incremental_update_flow)
    assert "_load_tech_recompute_pending" in flow_source
    assert "_finalize_tech_recompute" in flow_source
    # 强制并入的是**完整重算**集合，不是"价格域增量"集合
    assert "*tech_pending_codes" in flow_source


# ─────────────────────────────────────────────────────────────────────────────
# 2. 下一轮重算 → 名单清空（失败/隔离保留）
# ─────────────────────────────────────────────────────────────────────────────


def test_next_round_recompute_clears_pending(duckdb_store, sqlite_store) -> None:
    _seed_stock(duckdb_store, "600519")
    _seed_stock(duckdb_store, "000001")
    updater = _updater(duckdb_store, sqlite_store)
    updater._mark_tech_recompute_pending(["600519", "000001"])
    pending = updater._load_tech_recompute_pending()
    assert pending == ["600519", "000001"]

    step = {"status": "success", "success": 2, "failed": 0}
    cleared = updater._finalize_tech_recompute(step, pending)

    assert cleared == 2
    assert updater._load_tech_recompute_pending() == []


def test_failed_or_quarantined_codes_stay_pending(duckdb_store, sqlite_store) -> None:
    """失败/发布门禁隔离的股票没重算成功 → 必须留在名单里等下一轮。"""
    _seed_stock(duckdb_store, "600519")
    _seed_stock(duckdb_store, "000001")
    updater = _updater(duckdb_store, sqlite_store)
    updater._mark_tech_recompute_pending(["600519", "000001"])

    step = {
        "status": "partial",
        "failed": 1,
        "failed_codes": ["000001"],
        "quarantined_codes": ["600519"],
    }
    cleared = updater._finalize_tech_recompute(
        step, updater._load_tech_recompute_pending(),
    )

    assert cleared == 0
    assert updater._load_tech_recompute_pending() == ["600519", "000001"]


def test_rejected_recompute_clears_nothing(duckdb_store, sqlite_store) -> None:
    """整批被拒（发布门禁 hard_block）时一只都不能清，否则信号被吃掉。"""
    _seed_stock(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)
    updater._mark_tech_recompute_pending(["600519"])

    cleared = updater._finalize_tech_recompute(
        {"status": "rejected", "reason": "publish_gate_hard_block"},
        updater._load_tech_recompute_pending(),
    )

    assert cleared == 0
    assert updater._load_tech_recompute_pending() == ["600519"]


def test_skipped_step_keeps_pending(duckdb_store, sqlite_store) -> None:
    """skipped = 这一轮没算 → 名单里必须保留（否则"必须重算"的信号丢失）。"""
    _seed_stock(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)
    updater._mark_tech_recompute_pending(["600519"])

    cleared = updater._finalize_tech_recompute(
        {"status": "skipped", "reason": "no_changed_stocks"},
        updater._load_tech_recompute_pending(),
    )

    assert cleared == 0
    assert updater._load_tech_recompute_pending() == ["600519"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. 名单边界：TTL / 上限 / 幂等
# ─────────────────────────────────────────────────────────────────────────────


def test_expired_entries_are_dropped(duckdb_store, sqlite_store) -> None:
    """TTL：源长期滞后也不能让名单无限期带病运行（默认 3 天）。"""
    _seed_stock(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)
    stale_stamp = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    fresh_stamp = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _write_state(
        sqlite_store,
        json.dumps([
            {"code": "600519", "marked_at": stale_stamp},
            {"code": "000001", "marked_at": fresh_stamp},
        ]),
    )

    assert updater._load_tech_recompute_pending() == ["000001"]


def test_remark_refreshes_timestamp_without_duplicating(
    duckdb_store, sqlite_store,
) -> None:
    """同一只股票连续两轮仍滞后 → 续期（避免 TTL 误删），不重复入列。"""
    _seed_stock(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)
    first_stamp = (datetime.now(UTC) - timedelta(days=2, hours=20)).isoformat()
    _write_state(
        sqlite_store, json.dumps([{"code": "600519", "marked_at": first_stamp}]),
    )

    added = updater._mark_tech_recompute_pending(["600519"])

    assert added == 0
    stored = _read_state(sqlite_store)
    assert [entry["code"] for entry in stored] == ["600519"]
    assert stored[0]["marked_at"] > first_stamp


def test_same_qfq_watermark_is_not_remarked(duckdb_store, sqlite_store) -> None:
    """同一 qfq 水位上已经重算过 → 不再入列（否则源滞后一周 = 每轮重算 2000 只）。"""
    _seed_stock(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)

    assert updater._mark_tech_recompute_pending({"600519": "2026-11-12"}) == 1
    pending = updater._load_tech_recompute_pending()
    assert pending == ["600519"]
    assert updater._finalize_tech_recompute(
        {"status": "success", "success": 1, "failed": 0}, pending,
    ) == 1
    assert updater._load_tech_recompute_pending() == []

    # qfq 仍停在 11-12（源没补上来）→ 不该再算一次
    assert updater._mark_tech_recompute_pending({"600519": "2026-11-12"}) == 0
    assert updater._load_tech_recompute_pending() == []

    # qfq 推进到 11-13 但 raw 已到 11-14 → 有新数据，必须再算
    assert updater._mark_tech_recompute_pending({"600519": "2026-11-13"}) == 1
    assert updater._load_tech_recompute_pending() == ["600519"]


def test_pending_set_is_capped(duckdb_store, sqlite_store) -> None:
    """上限：超出 limit 时丢最早的，不让名单无限增长。"""
    _seed_stock(duckdb_store, "600519")
    updater = _updater(duckdb_store, sqlite_store)
    limit = updater._TECH_RECOMPUTE_PENDING_LIMIT
    stamp = datetime.now(UTC).isoformat()
    overflow = [
        {"code": f"9{index:05d}", "marked_at": stamp} for index in range(limit + 5)
    ]
    _write_state(sqlite_store, json.dumps(overflow))

    updater._mark_tech_recompute_pending(["600519"])

    stored = _read_state(sqlite_store)
    assert len(stored) == limit
    assert stored[-1]["code"] == "600519"
    # 最早的 6 条被丢弃
    assert stored[0]["code"] == f"9{6:05d}"
