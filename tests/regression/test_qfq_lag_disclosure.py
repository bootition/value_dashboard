"""事故出处：reports/118 / 2026-11-13。

2026-11-13 P1（1）：价格步骤不得在 qfq 大面积滞后时仍报 success。

生产事实：job 207（15:33–17:29，5,572 只、0 失败）结束后
`price_freshness.qfq_current_count = 959/5,572` —— 4,613 只 qfq 停在 11-12，
而 raw 已到 11-13。raw/qfq 成对抓取时两侧都可能"成功但只到前一天"，步骤只按
fail_count 判定就会假绿；下一轮又把这几千只重新选中，形成"每轮补 qfq"稳态。

新语义：
* 步骤收尾用一条 SQL 统计目标集内 ``MAX(price_daily_qfq.trade_date) < target_date``
  的只数 → ``qfq_lag_count / qfq_lag_ratio / qfq_lag_codes[:20]`` 写进 step；
* ``failed == 0`` 但 ``qfq_lag_count > max(5, int(total * 0.01))`` →
  ``status="partial"``、``reason="qfq_lagged"``；
* 既有 success/failed/elapsed/rate 字段全部保留。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater
from tests.support.seed import Seed

TARGET_DATE = "2026-11-13"
LOCAL_DATE = "2026-11-12"


def _price_result(adjust: str, trade_date: str, close: float = 10.0) -> FetchResult:
    payload = f"{adjust}:{trade_date}".encode()
    return FetchResult(
        data=[{"trade_date": trade_date, "close": close, "volume": 100.0}],
        metadata=SourceMetadata(
            source="local_cache",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(payload).hexdigest(),
            confidence="strict",
        ),
        raw_response=payload,
    )


class _AdjustAwareAdapter:
    """raw 总能前进到 TARGET_DATE；qfq 只回传到 ``qfq_end``（模拟源侧 qfq 滞后）。"""

    def __init__(self, qfq_end: str) -> None:
        self.qfq_end = qfq_end
        self.requests: list[tuple[str, str | None]] = []

    def fetch(self, request):
        self.requests.append((request.adjust, request.start_date))
        trade_date = TARGET_DATE if request.adjust == "raw" else self.qfq_end
        return _price_result(request.adjust, trade_date)


def _seed_stock(duckdb_store, code: str) -> None:
    Seed(duckdb_store).stock(code, name="x", exchange="SHSE", is_listed=True)
    for table in ("price_daily_raw", "price_daily_qfq"):
        Seed(duckdb_store).prices(code, [(LOCAL_DATE, 9.0)], table=table)


def _run_price_step(duckdb_store, sqlite_store, adapter, codes: list[str]) -> dict:
    for code in codes:
        _seed_stock(duckdb_store, code)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter)
    updater._latest_expected_trading_date = lambda today: TARGET_DATE
    updater._get_latest_local_price_date = lambda: LOCAL_DATE
    return updater._update_prices_incremental(max_stocks=0)


def test_qfq_lag_is_downgraded_to_partial_with_reason(
    duckdb_store, sqlite_store,
) -> None:
    """raw 全成功 + qfq 只到昨天（10 只）→ 0 失败但必须 partial(qfq_lagged)。"""
    codes = [f"6001{index:02d}" for index in range(10)]

    report = _run_price_step(
        duckdb_store, sqlite_store, _AdjustAwareAdapter(qfq_end=LOCAL_DATE), codes,
    )

    assert report["failed"] == 0, "两侧都返回了非空数据 → 不算失败（这就是假绿来源）"
    assert report["success"] == len(codes)
    assert report["status"] == "partial"
    assert report["reason"] == "qfq_lagged"
    assert report["qfq_lag_count"] == len(codes)
    assert report["qfq_lag_ratio"] == 1.0
    assert report["qfq_lag_threshold"] == max(5, int(len(codes) * 0.01))
    assert len(report["qfq_lag_codes"]) == len(codes)
    # 既有字段必须原样保留（状态页/吞吐基线依赖它们）
    for key in ("success", "failed", "total", "elapsed_seconds", "rate_per_minute",
                "xdxr_full_refetch", "raw_full_refetch"):
        assert key in report


def test_qfq_current_keeps_success(duckdb_store, sqlite_store) -> None:
    """qfq 与 raw 都到达目标日 → 仍必须是 success（无假阴）。"""
    codes = [f"6002{index:02d}" for index in range(10)]

    report = _run_price_step(
        duckdb_store, sqlite_store, _AdjustAwareAdapter(qfq_end=TARGET_DATE), codes,
    )

    assert report["status"] == "success"
    assert report["reason"] is None
    assert report["qfq_lag_count"] == 0
    assert report["qfq_lag_ratio"] == 0.0


def test_qfq_lag_below_threshold_stays_success(duckdb_store, sqlite_store) -> None:
    """少量滞后（≤ max(5, 1%)）属正常波动，不得把整步打成 partial。"""
    codes = [f"6003{index:02d}" for index in range(3)]

    report = _run_price_step(
        duckdb_store, sqlite_store, _AdjustAwareAdapter(qfq_end=LOCAL_DATE), codes,
    )

    assert report["qfq_lag_count"] == 3
    assert report["qfq_lag_threshold"] == 5
    assert report["status"] == "success"


def test_qfq_lag_summary_handles_missing_qfq_rows(duckdb_store, sqlite_store) -> None:
    """qfq 完全没有本地行（NULL）也必须计入滞后，而不是被 SQL 漏掉。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    Seed(duckdb_store).stock("600401", name="x", exchange="SHSE", is_listed=True)

    summary = updater._qfq_lag_summary(["600401"], TARGET_DATE)

    assert summary["qfq_lag_count"] == 1
    assert summary["qfq_lag_codes"] == ["600401"]


def test_qfq_lag_summary_empty_target_is_neutral(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    assert updater._qfq_lag_summary([], TARGET_DATE) == {
        "qfq_lag_count": 0, "qfq_lag_ratio": 0.0, "qfq_lag_codes": [],
        # reports/151：源侧水位两个字段同样必须是中性 0（不参与降级）
        "raw_lag_count": 0, "qfq_below_target_count": 0,
    }


# ─── 2026-11-20（reports/151）：判据口径修正的两个方向 ────────────────────
class _FrozenSourceAdapter:
    """源侧整体还没发到 TARGET_DATE：raw/qfq 都只回到 ``end``（两侧齐平）。"""

    def __init__(self, end: str) -> None:
        self.end = end
        self.requests: list[tuple[str, str | None]] = []

    def fetch(self, request):
        self.requests.append((request.adjust, request.start_date))
        return _price_result(request.adjust, self.end)


class _RawAheadAdapter:
    """真滞后：raw 前进到 TARGET_DATE，qfq 仍停在更早的 ``qfq_end``。"""

    def __init__(self, qfq_end: str) -> None:
        self.qfq_end = qfq_end

    def fetch(self, request):
        return _price_result(
            request.adjust,
            TARGET_DATE if request.adjust == "raw" else self.qfq_end,
        )


def test_source_not_published_yet_is_not_qfq_lag(duckdb_store, sqlite_store) -> None:
    """源侧整市场都还没发到目标日（raw 与 qfq 齐平）→ 不得报 qfq 滞后。

    这是 2026-11-18 job 243 的形态：收盘后那一轮 ``qfq_lag_count=5,416/5,572``
    （97.2%）被判成 partial(qfq_lagged)。但"整市场都还没发"是源侧时间表，
    不是本地复权序列缺数据；把它算成滞后会让状态页长期假红。
    """
    codes = [f"6004{index:02d}" for index in range(10)]

    report = _run_price_step(
        duckdb_store, sqlite_store, _FrozenSourceAdapter(end=LOCAL_DATE), codes,
    )

    assert report["status"] == "success", "raw 与 qfq 齐平 → 不能降级 partial"
    assert report["reason"] is None
    assert report["qfq_lag_count"] == 0, "qfq 没有落后自己的 raw，不算滞后"
    assert report["qfq_lag_ratio"] == 0.0
    # 源侧水位必须仍然可见（可观测性不能丢）
    assert report["raw_lag_count"] == len(codes)
    assert report["qfq_below_target_count"] == len(codes)


def test_real_qfq_lag_still_updates_and_downgrades(duckdb_store, sqlite_store) -> None:
    """真滞后：qfq 落后自己的 raw → 必须计入滞后并降级（不能为了快放过）。"""
    codes = [f"6005{index:02d}" for index in range(10)]

    report = _run_price_step(
        duckdb_store, sqlite_store, _RawAheadAdapter(qfq_end=LOCAL_DATE), codes,
    )

    assert report["qfq_lag_count"] == len(codes), "raw 已到目标日而 qfq 没跟上"
    assert report["status"] == "partial"
    assert report["reason"] == "qfq_lagged"
    assert report["raw_lag_count"] == 0, "raw 已到目标日 → 源侧水位不再是瓶颈"
    assert report["qfq_below_target_count"] == len(codes)


def test_qfq_lag_summary_compares_with_own_raw(duckdb_store, sqlite_store) -> None:
    """``_qfq_lag_summary`` 的判据是"自己的 raw"，不是日历目标日。

    造两只股票：A 的 qfq 比自己的 raw 晚一天（真滞后）；B 的 raw/qfq 齐平
    但都停在目标日之前（源侧未发）。只有 A 允许计入 ``qfq_lag_count``。
    """
    Seed(duckdb_store).stock("600601", name="a", exchange="SHSE", is_listed=True)
    Seed(duckdb_store).stock("600602", name="b", exchange="SHSE", is_listed=True)
    Seed(duckdb_store).prices("600601", [(LOCAL_DATE, 9.0), (TARGET_DATE, 9.5)],
                             table="price_daily_raw")
    Seed(duckdb_store).prices("600601", [(LOCAL_DATE, 9.1)], table="price_daily_qfq")
    Seed(duckdb_store).prices("600602", [(LOCAL_DATE, 8.0)], table="price_daily_raw")
    Seed(duckdb_store).prices("600602", [(LOCAL_DATE, 8.1)], table="price_daily_qfq")

    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_FrozenSourceAdapter(LOCAL_DATE),
    )
    summary = updater._qfq_lag_summary(["600601", "600602"], TARGET_DATE)

    assert summary["qfq_lag_count"] == 1
    assert summary["qfq_lag_codes"] == ["600601"]
    assert summary["qfq_lag_ratio"] == 0.5
    assert summary["raw_lag_count"] == 1, "只有 B 的 raw 没到目标日"
    assert summary["qfq_below_target_count"] == 2
