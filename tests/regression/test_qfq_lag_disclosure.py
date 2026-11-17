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
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES (?, 'x', 'SHSE', true)",
        [code],
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES (?, ?, 9.0)",
            [code, LOCAL_DATE],
        )


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
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES ('600401', 'x', 'SHSE', true)"
    )

    summary = updater._qfq_lag_summary(["600401"], TARGET_DATE)

    assert summary["qfq_lag_count"] == 1
    assert summary["qfq_lag_codes"] == ["600401"]


def test_qfq_lag_summary_empty_target_is_neutral(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    assert updater._qfq_lag_summary([], TARGET_DATE) == {
        "qfq_lag_count": 0, "qfq_lag_ratio": 0.0, "qfq_lag_codes": [],
    }
