"""事故出处：reports/118 / 2026-11-13。

2026-11-13 P0（B）/ P1（E）：qfq 死角与 B 股假 retry_exhausted。

B（P0）：按股票的除权窗口是"event_date >= min(本地 raw 最新, 本地 qfq 最新)"。
若除权事件 D 晚于该股价格推进才写进 xdxr（上一轮 market_actions 失败/公告被
截断/事件晚一轮），条件恒假 → 该股 qfq 永久保留旧复权因子，前复权序列在 D 处
跳空，MA/RSI/回撤/复权收益全错，且任何 retry 都不会触发全量重拉。
修复：**只要某股新写入了除权事件**（新增或数值修正），就把代码写进持久化
待重拉集合 `data_refresh_state['qfq_refetch_pending']`；价格步骤强制把该股
并入目标集并全历史重拉 qfq，成功才移除、失败保留。

E（P1）：B 股（200/201/900 前缀或名称带 B）没有免费分红/除权源，必须直接
登记 missing(source_unsupported)，不写 retry、不占重试次数——否则
900948/xdxr(retry_count=4/5) 下一轮以 retry_exhausted 假名义永久消失。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater


def _result(rows: list[dict], *, adjust: str = "listing_info") -> FetchResult:
    payload = (adjust + str(len(rows))).encode()
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


def _seed_stock(
    duckdb_store, code: str, *, name: str = "x", listing_date: str = "2020-01-01",
) -> None:
    duckdb_store.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, is_listed, listing_date, total_shares, circ_shares)
           VALUES (?, ?, 'SHSE', true, ?, 100, 80)""",
        [code, name, listing_date],
    )


def _seed_prices(duckdb_store, code: str, dates: list[str], close: float = 9.0) -> None:
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES "
            + ", ".join("(?, ?, ?)" for _ in dates),
            [value for trade_date in dates for value in (code, trade_date, close)],
        )


class _MarketActionAdapter:
    """Returns one dividend and one xdxr row per requested code; counts calls."""

    def __init__(self, xdxr_date: str = "2026-11-10") -> None:
        self.xdxr_date = xdxr_date
        self.calls: list[str] = []

    def fetch(self, request):
        self.calls.append(request.data_type)
        codes = list(request.stock_codes)
        if request.data_type == "dividends":
            rows = [
                {
                    "stock_code": code,
                    "ex_date": self.xdxr_date,
                    "announcement_date": "2026-10-20",
                    "dividend_per_share": 1.0,
                }
                for code in codes
            ]
        elif request.data_type == "xdxr":
            rows = [
                {
                    "stock_code": code,
                    "event_date": self.xdxr_date,
                    "category": 1,
                    "fenhong": 1.0,
                }
                for code in codes
            ]
        else:
            rows = []
        return _result(rows, adjust=request.data_type)


# ─── B: 新写入除权事件 → 待重拉集合 ─────────────────────────────────


def test_new_xdxr_event_marks_code_for_qfq_refetch(duckdb_store, sqlite_store) -> None:
    _seed_stock(duckdb_store, "600519")
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_MarketActionAdapter(),
    )

    report = updater._refresh_market_actions(["600519"])

    assert report["status"] == "success"
    assert report["new_xdxr_event_codes"] == ["600519"]
    assert updater._load_qfq_refetch_pending() == ["600519"]
    assert report["qfq_refetch_pending"] == 1


def test_identical_xdxr_rewrite_does_not_remark_for_refetch(
    duckdb_store, sqlite_store,
) -> None:
    """同一事件被重复 upsert（数值相同）不算"新写入"，避免每轮都全历史重拉。"""
    _seed_stock(duckdb_store, "600520")
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_MarketActionAdapter(),
    )
    updater._refresh_market_actions(["600520"])
    assert updater._load_qfq_refetch_pending() == ["600520"]
    updater._clear_qfq_refetch_pending(["600520"])

    report = updater._refresh_market_actions(["600520"])

    assert report["new_xdxr_event_codes"] == []
    assert updater._load_qfq_refetch_pending() == []


def test_corrected_xdxr_value_counts_as_new_write(duckdb_store, sqlite_store) -> None:
    """同一天的分红金额被修正同样改变前复权因子，必须再次重拉。"""
    _seed_stock(duckdb_store, "600521")
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_MarketActionAdapter(),
    )
    updater._refresh_market_actions(["600521"])
    updater._clear_qfq_refetch_pending(["600521"])

    corrected_rows = {"fenhong": 2.0}

    class _CorrectedAdapter(_MarketActionAdapter):
        def fetch(self, request):
            result = super().fetch(request)
            if request.data_type == "xdxr":
                for row in result.data:
                    row.update(corrected_rows)
            return result

    updater.adapter_mgr = _CorrectedAdapter()
    report = updater._refresh_market_actions(["600521"])

    assert report["new_xdxr_event_codes"] == ["600521"]
    assert updater._load_qfq_refetch_pending() == ["600521"]


def test_late_xdxr_event_forces_full_qfq_refetch_even_when_prices_are_ahead(
    duckdb_store, sqlite_store,
) -> None:
    """P0 核心：除权事件晚于价格推进时，日期窗口恒假，但必须仍全历史重拉 qfq。

    构造：本地 raw/qfq 已到 2026-11-10，除权事件日 2026-11-05（早于本地最新日）。
    日期窗口 ``event_date >= window_start(2026-11-10)`` 恒假 → 旧逻辑不会重拉。
    """
    requests: list[tuple[str, str | None]] = []

    class PriceAdapter:
        def fetch(self, request):
            requests.append((request.adjust, request.start_date))
            return _result(
                [{"trade_date": "2026-11-10", "close": 10.0, "volume": 100.0}],
                adjust=request.adjust,
            )

    _seed_stock(duckdb_store, "600522")
    _seed_prices(duckdb_store, "600522", ["2026-11-05", "2026-11-10"])
    duckdb_store.write_query(
        "INSERT INTO xdxr (stock_code, event_date, category, fenhong) "
        "VALUES ('600522', '2026-11-05', 1, 1.0)"
    )
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=PriceAdapter(),
    )
    updater._latest_expected_trading_date = lambda today: "2026-11-10"
    updater._get_latest_local_price_date = lambda: "2026-11-10"

    # ① 日期窗口本身看不到这个事件（这就是死角）
    window = updater._local_price_window_start(
        {"latest_raw_date": "2026-11-10", "latest_qfq_date": "2026-11-10"},
        "2026-10-01",
    )
    assert window == "2026-11-10"
    assert updater._get_xdxr_codes_since(
        "2026-10-01", stock_windows={"600522": window},
    ) == set()

    # ② 没有待重拉集合时：只做增量（qfq 带 start_date），不会全历史重拉
    updater._update_prices_incremental(max_stocks=0)
    assert dict(requests)["qfq"] == "2026-11-10"
    requests.clear()

    # ③ 事件被"新写入" → 待重拉集合 → 即使价格已最新也强制全历史重拉 qfq
    assert updater._mark_qfq_refetch_pending(["600522"]) == 1
    report = updater._update_prices_incremental(max_stocks=0)

    assert report["xdxr_full_refetch"] == 1
    assert dict(requests)["qfq"] is None, "必须全历史重拉（start_date=None）"
    assert updater._load_qfq_refetch_pending() == [], "成功重拉后必须移出待重拉集合"
    assert report["qfq_refetch_pending_cleared"] == 1


def test_pending_code_is_targeted_even_when_prices_and_lineage_are_current(
    duckdb_store, sqlite_store,
) -> None:
    """待重拉的票若"价格已最新且有溯源"，也必须被强制拉进本轮目标集。"""
    requested: list[str] = []

    class PriceAdapter:
        def fetch(self, request):
            requested.extend(request.stock_codes)
            return _result(
                [{"trade_date": "2026-11-10", "close": 10.0, "volume": 100.0}],
                adjust=request.adjust,
            )

    _seed_stock(duckdb_store, "600523")
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=PriceAdapter(),
    )
    updater._latest_expected_trading_date = lambda today: "2026-11-10"
    updater._get_latest_local_price_date = lambda: "2026-11-10"
    # 先跑一轮把价格与 latest_close 溯源都写齐（此后该股"完全新鲜"）
    updater._update_prices_incremental(max_stocks=0)
    requested.clear()
    assert updater._mark_qfq_refetch_pending(["600523"]) == 1

    report = updater._update_prices_incremental(max_stocks=0)

    assert requested, "待重拉集合中的股票必须进入本轮目标集"
    assert "600523" in requested
    assert report["success"] == 1
    assert updater._load_qfq_refetch_pending() == []


def test_pending_survives_failure_and_is_bounded(duckdb_store, sqlite_store) -> None:
    """重拉失败必须保留待重拉；集合去重且有上限。"""

    class FailingAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="local_cache",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash="",
                    confidence="missing",
                    error="source blocked",
                    error_kind="source_unavailable",
                ),
            )

    for code in ("600524", "600525", "600526"):
        _seed_stock(duckdb_store, code)
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FailingAdapter(),
    )
    updater._latest_expected_trading_date = lambda today: "2026-11-10"
    updater._QFQ_REFETCH_PENDING_LIMIT = 2

    updater._mark_qfq_refetch_pending(["600524", "600525", "600524"])
    assert updater._load_qfq_refetch_pending() == ["600524", "600525"], "去重且保序"

    updater._mark_qfq_refetch_pending(["600526"])
    assert updater._load_qfq_refetch_pending() == ["600525", "600526"], "超上限丢最早"

    report = updater._update_prices_incremental(max_stocks=0)
    assert report["failed"] >= 1
    assert updater._load_qfq_refetch_pending() == ["600525", "600526"], "失败必须保留"


def test_unknown_codes_are_pruned_from_pending_set(duckdb_store, sqlite_store) -> None:
    _seed_stock(duckdb_store, "600527")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    updater._mark_qfq_refetch_pending(["600527", "999999"])

    assert updater._load_qfq_refetch_pending() == ["600527"]


# ─── E: B 股分红/除权直接 missing，不写 retry ───────────────────────


class _ExplodingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        raise AssertionError("B 股不得发起分红/除权请求")


def test_b_share_market_actions_write_missing_without_retry(
    duckdb_store, sqlite_store,
) -> None:
    _seed_stock(duckdb_store, "900948", name="伊泰B股")
    adapter = _ExplodingAdapter()
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter)
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count, max_retries,
                    last_attempt, extra_json)
               VALUES ('900948', 'xdxr', 'manager', 'old failure', 4, 5,
                       CURRENT_TIMESTAMP, '{}')"""
        )

    report = updater._refresh_market_actions(["900948"])

    assert adapter.calls == 0, "B 股不得到达适配器"
    assert report["skipped"] == 1 and report["failed"] == 0
    missing = {
        row["field_name"]: row["reason_code"]
        for row in sqlite_store.query(
            "SELECT field_name, reason_code FROM missing_list WHERE stock_code = '900948'"
        )
    }
    assert missing == {
        "dividends": "source_unsupported",
        "xdxr": "source_unsupported",
    }
    assert sqlite_store.query("SELECT * FROM retry_list") == []


def test_b_share_retry_row_settles_instead_of_retry_exhausting(
    duckdb_store, sqlite_store,
) -> None:
    """900948/xdxr retry_count=4/5 不得再消耗一次重试变成 retry_exhausted。"""
    _seed_stock(duckdb_store, "900948", name="伊泰B股")
    adapter = _ExplodingAdapter()
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter)
    with sqlite_store.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count, max_retries,
                    last_attempt, next_retry_at, extra_json)
               VALUES ('900948', 'xdxr', 'tdx', 'empty result', 4, 5,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}')"""
        )
        retry_id = cursor.lastrowid

    result = updater._retry_failed_tasks([{
        "id": retry_id, "stock_code": "900948", "data_type": "xdxr",
        "adapter": "tdx", "error": "empty result", "retry_count": 4,
        "extra_json": "{}", "error_kind": "unknown",
    }])

    assert adapter.calls == 0
    assert result["resolved_non_retryable"] == 1
    assert sqlite_store.query("SELECT * FROM retry_list") == []
    missing = sqlite_store.query(
        "SELECT field_name, reason_code FROM missing_list WHERE stock_code = '900948'"
    )
    assert missing == [{"field_name": "xdxr", "reason_code": "source_unsupported"}]
    assert sqlite_store.query(
        "SELECT * FROM missing_list WHERE reason_code = 'retry_exhausted'"
    ) == []


def test_pending_state_is_persisted_as_json_list(duckdb_store, sqlite_store) -> None:
    """待重拉集合必须是可读 JSON 数组（运维可直接查 data_refresh_state）。"""
    _seed_stock(duckdb_store, "600528")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    updater._mark_qfq_refetch_pending(["600528"])

    rows = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = 'qfq_refetch_pending'"
    )
    assert json.loads(rows[0]["value"]) == ["600528"]
