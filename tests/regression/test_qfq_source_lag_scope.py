"""reports/151 §1：源侧 qfq 发布滞后不得让价格步骤每轮跑满全市场。

事故：2026-11-18 起每轮 ``prices`` 都跑满 5417 只（50–90 分钟），而 11-17 的
12 轮都是 ``skipped(prices_up_to_date)``（3 分 40 秒）——13–24 倍退化。

根因：选股 SQL 的 ``qfq.latest_qfq_date < ?target`` 对"最新交易日"在
**当晚 raw 已发、qfq 未发**的那几个小时里恒真（腾讯 raw/qfq 同日先后发布，
实测 11-17 20:15 之后两者齐平、11-20 18:13 两者都到 11-20），于是全市场每轮
重选。腾讯请求被 `BaseAdapter._wait_rate_limit` 的全局锁串行化
（0.2s/请求 × 2 请求/只 ⇒ ≤150 只/分钟，官方实测 107 只/分钟），一轮固定
36–50 分钟。

修法（`prices_step._defer_qfq_source_lag`）：`qfq < raw` 只作**候选**，再由
"落后几根 K 线 + 本交易日已尝试次数"裁决。本文件锁住三个方向的边界：

1. 源侧只差 1 根且本交易日已尝试满 → **跳过**（本轮的 13–24 倍退化在这里）；
2. qfq 真落后自己 raw ≥ ``price_qfq_lag_bars`` 根 → **必须重选**（不是源侧节奏）；
3. raw 出现新交易日 → **必须重选**（水位变了，任何延迟都不许挡）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.core.financial_period import cn_today
from app.core.update import IncrementalUpdater
from tests.support.seed import Seed


def _result(rows: list[dict], *, adjust: str = "price_daily"):
    payload = (adjust + str(len(rows))).encode()
    from app.core.adapters.base import FetchResult, SourceMetadata

    return FetchResult(
        data=rows,
        metadata=SourceMetadata(
            source="local_cache",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(payload).hexdigest(),
            confidence="strict",
        ),
        raw_response=payload,
    )


def _seed_stock(duckdb_store, code: str) -> None:
    Seed(duckdb_store).stock(
        code,
        name="x",
        exchange="SHSE",
        is_listed=True,
        listing_date="2020-01-01",
        total_shares=100,
        circ_shares=80,
    )


def _seed_prices(duckdb_store, code: str, *, raw: list[str], qfq: list[str]) -> None:
    for table, dates in (("price_daily_raw", raw), ("price_daily_qfq", qfq)):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES "
            + ", ".join("(?, ?, ?)" for _ in dates),
            [value for trade_date in dates for value in (code, trade_date, 9.0)],
        )


class _SourceLagAdapter:
    """raw 到 ``raw_last``、qfq 只到 ``qfq_last``（= 源侧当晚还没发 qfq）。"""

    def __init__(self, raw_last: str, qfq_last: str) -> None:
        self.raw_last = raw_last
        self.qfq_last = qfq_last
        self.calls: list[tuple[str, str]] = []

    def fetch(self, request):
        code = str(request.stock_codes[0])
        self.calls.append((request.adjust, code))
        trade_date = self.raw_last if request.adjust != "qfq" else self.qfq_last
        return _result(
            [{"trade_date": trade_date, "close": 9.0, "volume": 100.0}],
            adjust=request.adjust,
        )

    def recover_after_timeout(self) -> None:  # pragma: no cover - 接口对齐
        return None


def _updater(duckdb_store, sqlite_store, adapter, *, target: str):
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter,
    )
    updater._latest_expected_trading_date = lambda today: target
    updater._get_latest_local_price_date = lambda: target
    return updater


# ─── 1. 源侧只差 1 根 + 本交易日已尝试满 → 跳过（退化被修掉的地方）────


def test_source_side_qfq_one_bar_behind_is_skipped_after_attempt_cap(
    duckdb_store, sqlite_store,
) -> None:
    """源侧 qfq 晚一天：抓过 ``price_qfq_catchup_max_attempts`` 次后不再重选。

    这正是 11-18 之后每轮跑满 5417 只的形态：raw 已到目标日、qfq 只差 1 根、
    血缘齐备。旧实现每轮都入选 → 每轮 36–50 分钟。
    """
    _seed_stock(duckdb_store, "600601")
    _seed_prices(duckdb_store, "600601", raw=["2026-11-10"], qfq=["2026-11-09"])
    adapter = _SourceLagAdapter(raw_last="2026-11-10", qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter, target="2026-11-10")
    updater.price_qfq_catchup_max_attempts = 1

    # 第一轮：raw 落后目标日 → 硬缺口，必须抓（并记下这次尝试）
    first = updater._update_prices_incremental(max_stocks=0)
    assert first["status"] == "success"
    assert ("raw", "600601") in adapter.calls
    assert first["qfq_catchup_attempts_marked"] == 1
    assert updater._load_qfq_catchup_attempts()["600601"]["n"] == 1
    calls_after_first = len(adapter.calls)

    # 第二轮：raw 已到 11-10、qfq 只差 1 根（源侧未发）→ 不应重选
    second = updater._update_prices_incremental(max_stocks=0)
    assert second["status"] == "skipped"
    assert second["reason"] == "prices_up_to_date"
    assert len(adapter.calls) == calls_after_first, "源侧滞后不得再发起任何请求"


def test_deferred_codes_are_reported_and_not_marked_partial(
    duckdb_store, sqlite_store,
) -> None:
    """被延迟的源侧滞后必须可见，但不得把"源侧还没发"报成本轮 partial。"""
    _seed_stock(duckdb_store, "600602")
    _seed_prices(duckdb_store, "600602", raw=["2026-11-10"], qfq=["2026-11-09"])
    adapter = _SourceLagAdapter(raw_last="2026-11-10", qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter, target="2026-11-10")
    updater.price_qfq_catchup_max_attempts = 1
    updater._update_prices_incremental(max_stocks=0)

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["status"] == "skipped"
    # 早退报告不带 step 细节，但延迟集合必须能从 sqlite 侧复算出来
    attempts = updater._load_qfq_catchup_attempts()
    assert attempts["600602"] == {
        "raw": "2026-11-10", "qfq": "2026-11-09",
        "date": cn_today().strftime("%Y-%m-%d"), "n": 1,
    }


# ─── 2. qfq 真落后自己 raw ≥ N 根 → 必须重选（不许被延迟逻辑吞掉）────


def test_real_qfq_lag_of_two_bars_is_always_reselected(
    duckdb_store, sqlite_store,
) -> None:
    """qfq 落后自己的 raw 2 根 K 线 = 真滞后，试满次数也必须继续重选。"""
    _seed_stock(duckdb_store, "600603")
    _seed_prices(
        duckdb_store, "600603",
        raw=["2026-11-06", "2026-11-09", "2026-11-10"],
        qfq=["2026-11-06"],
    )
    adapter = _SourceLagAdapter(raw_last="2026-11-10", qfq_last="2026-11-06")
    updater = _updater(duckdb_store, sqlite_store, adapter, target="2026-11-10")
    updater.price_qfq_catchup_max_attempts = 1
    # 先把"本交易日已尝试过这个水位"灌满（模拟上一轮抓过但 qfq 没前进）
    for _ in range(3):
        updater._mark_qfq_catchup_attempts(
            {"600603": ("2026-11-10", "2026-11-06")},
            on_date=cn_today().strftime("%Y-%m-%d"),
        )
    assert updater._load_qfq_catchup_attempts()["600603"]["n"] >= 3
    assert updater._raw_bars_beyond_qfq(["600603"]) == {"600603": 2}

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["total"] == 1, "真滞后必须在目标集里"
    assert ("qfq", "600603") in adapter.calls


# ─── 3. raw 有新交易日 → 必须重选 ─────────────────────────────────────


def test_new_raw_trading_day_always_reselects(duckdb_store, sqlite_store) -> None:
    """水位变了（raw 前进一天）→ 任何延迟记录都必须作废，立刻重选。"""
    _seed_stock(duckdb_store, "600604")
    _seed_prices(duckdb_store, "600604", raw=["2026-11-10"], qfq=["2026-11-09"])
    adapter = _SourceLagAdapter(raw_last="2026-11-11", qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter, target="2026-11-10")
    updater.price_qfq_catchup_max_attempts = 1
    for _ in range(3):
        updater._mark_qfq_catchup_attempts(
            {"600604": ("2026-11-10", "2026-11-09")},
            on_date=cn_today().strftime("%Y-%m-%d"),
        )

    # 目标日前进到 11-11（新交易日收盘）→ raw 落后 → 必须抓
    updater._latest_expected_trading_date = lambda today: "2026-11-11"
    report = updater._update_prices_incremental(max_stocks=0)

    assert report["status"] == "success"
    assert ("raw", "600604") in adapter.calls


def test_changed_qfq_watermark_invalidates_attempt_cap(
    duckdb_store, sqlite_store,
) -> None:
    """qfq 自己前进了（水位变化）→ 旧尝试记录对不上 → 立刻重选。"""
    _seed_stock(duckdb_store, "600605")
    _seed_prices(
        duckdb_store, "600605", raw=["2026-11-10"], qfq=["2026-11-09"],
    )
    adapter = _SourceLagAdapter(raw_last="2026-11-10", qfq_last="2026-11-10")
    updater = _updater(duckdb_store, sqlite_store, adapter, target="2026-11-10")
    updater.price_qfq_catchup_max_attempts = 1
    for _ in range(3):
        updater._mark_qfq_catchup_attempts(
            {"600605": ("2026-11-10", "2026-11-08")},
            on_date=cn_today().strftime("%Y-%m-%d"),
        )

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["total"] == 1
    assert ("qfq", "600605") in adapter.calls
