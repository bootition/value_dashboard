"""reports/151 §1 追加：追平尝试记录必须**跟着批次落盘**（否则裁决是死循环）。

## 事故（独立审计定位，2026-11-21）

`defer_qfq_source_lag` 的延迟判据要读
``data_refresh_state['price_qfq_catchup_attempts']``；而写这条记录的
`mark_qfq_catchup_attempts` 只在 `_finalize_price_step`（**整步跑完**）被调用：

```
价格步跑不到终点（超时被杀 / 进程重启 / 收尾异常）
  → 记录永远不落盘 → 下轮没有"已尝试满"的证据
  → defer_qfq_source_lag 永远判不出延迟 → 又重选全市场（5571 只）
  → 又跑不完 → 记录仍不落盘（自锁）
```

现场证据：`data_refresh_state` 里**根本不存在** `price_qfq_catchup_attempts`
这个 key；76MB 日志里「源侧 qfq 滞后」0 次命中 —— 上一轮的价格裁决一次都没生效。

## 本文件钉的是什么

1. 价格步**没跑到终点**（两个形态：结算段抛错 / 流水线中途被杀）时，
   已经落库的股票**仍然**留下了尝试记录 —— 这是死循环被切断的那一刻；
2. 同一交易日第二次运行：延迟真的生效（`deferred_qfq_source_lag_count > 0`），
   且**不再抓这些股票**；
3. 落盘是"每批一次"而不是"每只一次"（`qfq_catchup_attempt_flushes` 远小于
   股票数）—— 新落盘不能变成新的性能问题；
4. 抓取/落库失败的股票**不记账**（下轮照旧重试）；
5. 硬缺口（血缘缺失）即使记录已满也**永远入选**，延迟逻辑碰不到它。

5 条既有规格（`test_qfq_source_lag_scope.py`）继续守"水位变化 / 换交易日 /
真滞后 ≥ N 根必须重选"那一侧，不在本文件重复。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from app.core.financial_period import cn_today
from app.core.update import IncrementalUpdater
from app.core.update_parts import prices_step
from tests.support.seed import Seed

TARGET = "2026-11-10"


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


class _SourceLagAdapter:
    """raw 到 ``raw_last``、qfq 只到 ``qfq_last``（= 源侧当晚还没发 qfq）。"""

    def __init__(self, raw_last: str, qfq_last: str) -> None:
        self.raw_last = raw_last
        self.qfq_last = qfq_last
        self.calls: list[tuple[str, str]] = []

    def fetch(self, request):
        self.calls.append((request.adjust, str(request.stock_codes[0])))
        trade_date = self.raw_last if request.adjust != "qfq" else self.qfq_last
        return _result(
            [{"trade_date": trade_date, "close": 9.0, "volume": 100.0}],
            adjust=request.adjust,
        )

    def recover_after_timeout(self) -> None:  # pragma: no cover - 接口对齐
        return None


def _seed_one(duckdb_store, code: str) -> None:
    Seed(duckdb_store).stock(
        code,
        name="x",
        exchange="SHSE",
        is_listed=True,
        listing_date="2020-01-01",
        total_shares=100,
        circ_shares=80,
    )
    Seed(duckdb_store).prices_bulk([(code, "2026-11-09", 9.0)])
    Seed(duckdb_store).prices_bulk(
        [(code, "2026-11-09", 9.0)], table="price_daily_qfq",
    )


def _updater(duckdb_store, sqlite_store, adapter, *, target: str = TARGET):
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter,
    )
    updater._latest_expected_trading_date = lambda today: target
    updater._get_latest_local_price_date = lambda: target
    return updater


# ─── 1. 死循环被切断的那一刻：步末没跑到，记录也已经落盘 ────────────────


def test_attempts_are_persisted_even_when_step_never_reaches_finalize(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """形态 A：结算段抛错（= 整步没跑完）→ 已落库股票的尝试记录仍在。

    旧实现把记账放在 `_finalize_price_step` 里；这一步一旦跑不到，记录就永远
    不落盘。这里是**死循环的最小复现**：把结算段打爆，再检查状态表。
    """
    _seed_one(duckdb_store, "600701")
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 1

    def _boom(*_args, **_kwargs):
        raise RuntimeError("价格步被打断（模拟超时被杀 / 进程重启）")

    monkeypatch.setattr(prices_step, "_finalize_price_step", _boom)
    with pytest.raises(RuntimeError):
        updater._update_prices_incremental(max_stocks=0)

    attempts = updater._load_qfq_catchup_attempts()
    assert attempts, "价格步没跑到终点，尝试记录却一条都没有：死循环又回来了"
    assert attempts["600701"] == {
        "raw": TARGET,
        "qfq": "2026-11-09",
        "date": cn_today().strftime("%Y-%m-%d"),
        "n": 1,
    }


def test_attempts_are_persisted_when_pipeline_is_killed_mid_flight(
    duckdb_store, sqlite_store,
) -> None:
    """形态 B：流水线中途被 kill（`detail_cb` 抛错）→ 已落库的那只也记上了。

    中途被杀时批次是**不完整**的（走 `finally` 的收尾落盘）；记账必须同样
    跟到收尾落盘之后，否则"每批落盘"只是换了个地方自锁。
    """
    _seed_one(duckdb_store, "600702")
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 1

    def _kill(_step: str, _payload: dict) -> None:
        raise RuntimeError("进程被杀")

    with pytest.raises(RuntimeError):
        updater._update_prices_incremental(max_stocks=0, detail_cb=_kill)

    assert ("raw", "600702") in adapter.calls, "前置条件：这只还没被抓过"
    attempts = updater._load_qfq_catchup_attempts()
    assert attempts.get("600702", {}).get("n") == 1, (
        f"收尾落盘之后没有记账（attempts={attempts}）：下轮还得重扫全市场"
    )


# ─── 2. 验收：同一交易日第二次运行 → 真延迟、真不抓 ─────────────────────


def test_second_run_defers_after_aborted_step_and_fetches_nothing(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """**核心验收**：第一次跑（被中断）→ 第二次运行同一交易日必须延迟且不抓。"""
    _seed_one(duckdb_store, "600703")
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 1

    def _boom(*_args, **_kwargs):
        raise RuntimeError("价格步被打断")

    monkeypatch.setattr(prices_step, "_finalize_price_step", _boom)
    with pytest.raises(RuntimeError):
        updater._update_prices_incremental(max_stocks=0)
    monkeypatch.undo()
    calls_after_first = len(adapter.calls)
    assert calls_after_first > 0

    second = updater._update_prices_incremental(max_stocks=0)

    assert second["status"] == "skipped"
    assert second["reason"] == "prices_up_to_date"
    assert second["deferred_qfq_source_lag_count"] == 1, (
        "源侧 qfq 滞后没有被延迟：上一轮的裁决仍然一次都没生效"
    )
    assert second["deferred_qfq_source_lag_codes"] == ["600703"]
    assert len(adapter.calls) == calls_after_first, (
        "被延迟的股票仍然发起了抓取请求（延迟等于没做）"
    )


def test_deferred_reasons_are_reported_for_the_skip_report(
    duckdb_store, sqlite_store,
) -> None:
    """可观测性：全部延迟的早退报告也要带延迟只数 + 逐条原因。"""
    _seed_one(duckdb_store, "600704")
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 1
    updater._update_prices_incremental(max_stocks=0)

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["deferred_qfq_source_lag_count"] == 1
    assert report["qfq_catchup_defer_reasons"]["deferred"] == 1


# ─── 3. 落盘频率：一批一次，不是一只一次 ───────────────────────────────


def test_attempt_flush_is_one_write_per_batch_not_per_stock(
    duckdb_store, sqlite_store,
) -> None:
    """60 只股票 → 记账写入次数必须跟着**批次**走（远小于 60）。"""
    codes = [f"60{index:04d}" for index in range(60)]
    seed = Seed(duckdb_store)
    seed.stocks([(code, "x") for code in codes], is_listed=True,
                listing_date="2020-01-01", total_shares=100, circ_shares=80)
    seed.prices_bulk([(code, "2026-11-09", 9.0) for code in codes])
    seed.prices_bulk(
        [(code, "2026-11-09", 9.0) for code in codes], table="price_daily_qfq",
    )
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_fetch_batch_size = 20

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["success"] == 60
    assert report["qfq_catchup_attempts_marked"] == 60
    flushes = report["qfq_catchup_attempt_flushes"]
    # 增量路径的落盘批次 = ceil(60 / price_fetch_batch_size) = 3 次
    # （`write_batch_sizes` 只记全历史替换那条路，增量 upsert 不记，故这里用批大小推）。
    expected_persist_batches = -(-report["total"] // 20)
    assert flushes <= expected_persist_batches, (
        f"记账写入次数 {flushes} 超过了落盘批次数 {expected_persist_batches}："
        "有人把记账改成了每只一次 write（或每批写多次）"
    )
    assert flushes * 10 <= report["qfq_catchup_attempts_marked"], (
        f"记账写入 {flushes} 次 / {report['qfq_catchup_attempts_marked']} 只：没有跟着批次合并"
    )
    assert report["qfq_catchup_attempt_flush_seconds"] < report["elapsed_seconds"], (
        "记账落盘耗时不该吞掉整个价格步骤的时间"
    )


# ─── 4. 失败不记账（下轮必须重试）────────────────────────────────────────


def test_failed_fetch_is_not_marked_and_still_retried(
    duckdb_store, sqlite_store,
) -> None:
    """抓取失败的股票不记账 → 下一轮照旧入选（延迟逻辑不许把它当成"试过了"）。"""
    _seed_one(duckdb_store, "600705")

    class _BrokenAdapter(_SourceLagAdapter):
        def fetch(self, request):
            self.calls.append((request.adjust, str(request.stock_codes[0])))
            raise RuntimeError("source down")

    adapter = _BrokenAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 1

    first = updater._update_prices_incremental(max_stocks=0)

    assert first["failed"] == 1
    assert first["qfq_catchup_attempts_marked"] == 0
    assert updater._load_qfq_catchup_attempts() == {}

    calls_after_first = len(adapter.calls)
    second = updater._update_prices_incremental(max_stocks=0)

    assert len(adapter.calls) > calls_after_first, "失败过的股票必须重试"
    assert second["failed"] == 1


def test_attempts_below_cap_is_visible_in_defer_reasons(
    duckdb_store, sqlite_store,
) -> None:
    """次数没记满 → 不延迟，但必须能从报告的 reasons 里看出卡在"次数不够"。"""
    _seed_one(duckdb_store, "600706")
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 2
    updater._update_prices_incremental(max_stocks=0)  # n=1 < 2

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["status"] == "success"
    assert report["deferred_qfq_source_lag_count"] == 0
    assert report["qfq_catchup_defer_reasons"]["attempts_below_cap"] == 1
    assert report["success"] == 1, "次数没满必须继续抓"


# ─── 5. 硬缺口永远入选：延迟逻辑碰不到血缘缺失 ─────────────────────────


def test_missing_lineage_is_never_deferred_even_with_attempts_at_cap(
    duckdb_store, sqlite_store,
) -> None:
    """血缘缺失 = 硬缺口：记录已记满、gap 只有 1 根，也必须照旧入选重抓。"""
    _seed_one(duckdb_store, "600707")
    # 注意：这里**不**播种 source_audit / fetch_batch → raw/qfq 两个血缘都缺。
    adapter = _SourceLagAdapter(raw_last=TARGET, qfq_last="2026-11-09")
    updater = _updater(duckdb_store, sqlite_store, adapter)
    updater.price_qfq_catchup_max_attempts = 1
    updater._mark_qfq_catchup_attempts(
        {"600707": (TARGET, "2026-11-09")},
        on_date=cn_today().strftime("%Y-%m-%d"),
    )

    report = updater._update_prices_incremental(max_stocks=0)

    assert report["total"] == 1, "血缘缺失的硬缺口被延迟逻辑吞掉了"
    assert report["success"] == 1
    assert ("raw", "600707") in adapter.calls
    assert report["qfq_catchup_defer_reasons"].get("hard_gap") == 1
