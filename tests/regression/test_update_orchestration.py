"""事故出处：reports/117 / 2026-11-12。

2026-11-12 生产日志修复：更新编排回归（契约 §2/§3/§5）。

覆盖（对应工单条目）：
1. 除权窗口按股票计算（不再用全市场 min()）
2. 指标步骤状态合并：rejected 不被 success 覆盖
3. _listing_info_gap_codes 包含股本缺口股票
4. _fetch_listing_info 对股本仍缺失的股票写 missing_list
5. retry 分类：legal_empty / source_unsupported 直接进 missing，不占重试
6. 价格失败写 failed_codes + retry(error_kind=source_unavailable)
7. 公告步骤无论结果都进 steps（截断时 status=partial）
8. missing_count 用可行动口径
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import (
    IncrementalUpdater,
    _announcement_step_result,
    _merge_indicator_steps,
)
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite, sql_now

# 本轮收敛：`missing_list` 的 2 处（写在 `sqlite_store.transaction()` 里）→
# `SeedSqlite.join(conn).missing_list()`。
# **保留内联**的是 3 处 `retry_list`：两处 `last_attempt` 要库侧 `CURRENT_TIMESTAMP`
# （"刚尝试过"），一处靠 `cursor.lastrowid` 回捞 id 后喂给 `_mark_retry_failed` ——
# 后者正是工厂刻意不做的事（见 `tests/support/seed_sqlite.py`「尚未覆盖」）。


def _fetch_result(
    rows: list[dict],
    *,
    source: str = "local_cache",
    error: str | None = None,
    adjust: str = "listing_info",
) -> FetchResult:
    payload = (adjust + str(len(rows)) + str(error)).encode()
    return FetchResult(
        data=rows,
        metadata=SourceMetadata(
            source=source,
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(payload).hexdigest(),
            confidence="strict",
            error=error,
        ),
        raw_response=payload,
    )


def _seed_stock(
    duckdb_store,
    code: str,
    *,
    listed: bool = True,
    listing_date: str | None = "2020-01-01",
    is_st: bool | None = False,
    is_suspended: bool | None = False,
    total_shares: float | None = 100.0,
    circ_shares: float | None = 80.0,
) -> None:
    # `exchange` 显式写 'SZSE'（工厂默认按代码段推断，会把 600001/600002 判成 SHSE）。
    seed = Seed(duckdb_store)
    seed.stock(code, name="x", exchange="SZSE", is_listed=listed, listing_date=listing_date,
               is_st=is_st, is_suspended=is_suspended, total_shares=total_shares,
               circ_shares=circ_shares)


def _seed_local_price_rows(duckdb_store) -> None:
    """两只股票的本地日线：000001 到 2026-10-10（新），000002 停在 2015-01-05（陈旧）。"""
    seed = Seed(duckdb_store)
    for table in ("price_daily_raw", "price_daily_qfq"):
        seed.prices("000001", [("2026-10-10", 10)], table=table)
        seed.prices("000002", [("2015-01-05", 10)], table=table)


def _stub_flow(updater: IncrementalUpdater) -> None:
    """Stub every network/domain step so the orchestration can run offline."""
    updater.run_incremental_check = lambda **kwargs: {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
        "missing_count": 0,
    }
    updater._check_new_announcements = lambda persist=False, **kwargs: {
        "status": "available", "checked_remote": True,
        "affected_stock_codes": [], "affected_announcements": {},
        "all_new_announcements": {}, "errors": [], "warnings": [],
    }
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "success", "succeeded_codes": codes,
    }
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success"}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {
        "status": "skipped", "success": 0,
    }
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}
    updater._refresh_financial_detail_backfill = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub", "succeeded_codes": [],
    }
    updater._refresh_buyback = lambda: {
        "status": "skipped", "reason": "test_stub", "changed_codes": [],
    }
    updater._refresh_funding = lambda: {
        "status": "skipped", "reason": "test_stub", "changed_codes": [],
    }
    updater._refresh_business_overview = lambda **kwargs: {"status": "skipped", "reason": "test_stub"}
    updater._refresh_treasury_curve = lambda: {"status": "skipped", "reason": "test_stub"}
    updater._refresh_index_valuation = lambda: {"status": "skipped", "reason": "test_stub"}
    updater._refresh_capital_history = lambda **kwargs: {"status": "skipped", "reason": "test_stub"}
    updater._refresh_research_statistics = lambda **kwargs: {
        "status": "skipped", "reason": "test_stub",
    }
    updater._cleanup_unretryable_tasks = lambda: 0
    updater._cleanup_completed_announcement_retries = lambda: 0
    updater._cleanup_redundant_index_valuation_retries = lambda: 0
    updater._resolve_complete_missing_records = lambda: 0
    updater._resolve_legal_empty_funding_missing = lambda: 0
    updater._share_capital_fingerprint = lambda: "same"


# ─── 1. 除权窗口按股票 ────────────────────────────────────────────────


def test_xdxr_window_uses_each_stocks_own_local_date(duckdb_store, sqlite_store) -> None:
    """一只 qfq 停在 2015 年的股票不得把全体拖进全历史重拉。

    构造：
    - 000001 本地价格到 2026-10-10，除权 2026-10-05（已被本地覆盖）→ 不需要
    - 000002 本地价格停在 2015-01-05，除权 2016-06-01 → 需要全历史重拉
    - 000003 无任何本地价格，除权 2026-09-01 → 回退窗口起点后需要
    """
    _seed_stock(duckdb_store, "000001")
    _seed_stock(duckdb_store, "000002")
    _seed_stock(duckdb_store, "000003")
    _seed_local_price_rows(duckdb_store)
    seed = Seed(duckdb_store)
    seed.xdxr("000001", "2026-10-05")
    seed.xdxr("000002", "2016-06-01")
    seed.xdxr("000003", "2026-09-01")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    fallback_start = "2026-06-01"
    rows = {
        row["stock_code"]: row
        for row in duckdb_store.read_query(
            """SELECT stock.stock_code, raw.latest_raw_date, qfq.latest_qfq_date
               FROM stock_meta stock
               LEFT JOIN (SELECT stock_code, MAX(trade_date) AS latest_raw_date
                          FROM price_daily_raw GROUP BY stock_code) raw
                 ON raw.stock_code = stock.stock_code
               LEFT JOIN (SELECT stock_code, MAX(trade_date) AS latest_qfq_date
                          FROM price_daily_qfq GROUP BY stock_code) qfq
                 ON qfq.stock_code = stock.stock_code"""
        )
    }
    windows = {
        code: IncrementalUpdater._local_price_window_start(row, fallback_start)
        for code, row in rows.items()
    }
    assert windows["000002"] == "2015-01-05", "陈旧股用自己的日期，不用全市场 min()"

    codes = updater._get_xdxr_codes_since(fallback_start, stock_windows=windows)

    assert codes == {"000002", "000003"}
    assert "000001" not in codes, "本地已覆盖的除权事件不得触发全历史重拉"

    # 旧行为（全市场 min = 2015-01-05）会把 000001 也拖进来 —— 这就是 3.9× 回归。
    legacy = updater._get_xdxr_codes_since(min(windows.values()))
    assert legacy == {"000001", "000002", "000003"}

    # 只算某一只时结论不受其它股票影响（不再互相拖累）。
    assert updater._get_xdxr_codes_since(
        fallback_start, stock_windows={"000001": windows["000001"]},
    ) == set()


def test_xdxr_window_can_derive_dates_in_sql(duckdb_store, sqlite_store) -> None:
    """不传 stock_windows 时，一条 JOIN SQL 自行求每只股票的本地最新日期。"""
    _seed_stock(duckdb_store, "000001")
    _seed_stock(duckdb_store, "000002")
    _seed_local_price_rows(duckdb_store)
    seed = Seed(duckdb_store)
    seed.xdxr("000001", "2026-10-05")
    seed.xdxr("000002", "2016-06-01")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    codes = updater._get_xdxr_codes_since(
        "2026-06-01", stock_codes=["000001", "000002"],
    )

    assert codes == {"000002"}


# ─── 2. 指标步骤状态合并 ──────────────────────────────────────────────


def test_merge_indicator_steps_never_lets_success_mask_rejected() -> None:
    merged = _merge_indicator_steps(
        {"status": "rejected", "reason": "publish_gate_failed", "success": 0, "failed": 5575},
        {"status": "success", "success": 5575, "failed": 0},
    )

    assert merged["status"] == "rejected"
    assert merged["reason"] == "publish_gate_failed"
    assert merged["failed"] == 5575
    assert len(merged["sub_steps"]) == 2


def test_indicator_step_keeps_rejected_when_treasury_refresh_succeeds(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """第 2 轮 `indicators: success` 假绿的复现：国债利差成功不得覆盖 rejected。"""
    _seed_stock(duckdb_store, "000001")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_flow(updater)
    updater.run_incremental_check = lambda **kwargs: {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"},
        "needs_update": True, "blocked": False, "missing_count": 0,
    }
    updater._check_new_announcements = lambda persist=True, **kwargs: {
        "status": "available", "checked_remote": True,
        "affected_stock_codes": ["000001"], "affected_announcements": {},
        "all_new_announcements": {}, "errors": [], "warnings": [],
    }
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "success", "succeeded_codes": list(codes), "pending_codes": [],
    }
    updater._refresh_treasury_curve = lambda: {"status": "success", "curve_changed": True}
    updater._stale_financial_snapshot_codes = lambda: []
    updater._stale_price_snapshot_codes = lambda: []

    class FakeCalculator:
        def __init__(self, **kwargs) -> None:
            pass

        def compute_snapshot_for_all(self, *, progress_cb=None) -> dict:
            return {"status": "rejected", "reason": "publish_gate_failed"}

        def compute_snapshot_for_codes(self, codes, *, progress_cb=None) -> dict:
            return {"status": "rejected", "reason": "publish_gate_failed", "success": 0}

        def refresh_treasury_spreads(self, codes) -> dict:
            return {"status": "success", "success": len(codes)}

    monkeypatch.setattr("app.core.indicators.calculator.IndicatorCalculator", FakeCalculator)

    report = updater.run_incremental_update()

    indicators = report["steps"]["indicators"]
    assert indicators["status"] == "rejected", "success 子步骤不得覆盖 rejected"
    assert indicators["reason"] == "publish_gate_failed"


# ─── 3. 股本缺口纳入自动通道 ──────────────────────────────────────────


def test_listing_info_gap_codes_include_share_capital_gaps(
    duckdb_store, sqlite_store,
) -> None:
    _seed_stock(duckdb_store, "000001")                      # 元数据与股本都完整
    _seed_stock(duckdb_store, "000002", total_shares=None)    # 股本 NULL
    _seed_stock(duckdb_store, "000003", circ_shares=200.0)    # circ > total
    _seed_stock(duckdb_store, "000004", is_st=None)          # 既有三列缺口
    _seed_stock(duckdb_store, "000005", listed=False, total_shares=None)  # 未上市不计
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    gaps = updater._listing_info_gap_codes()

    assert "000002" in gaps and "000003" in gaps, "股本缺口必须进入自动补抓通道"
    assert "000004" in gaps, "既有元数据缺口语义必须保留"
    assert "000001" not in gaps and "000005" not in gaps


# ─── 4. _fetch_listing_info 登记股本缺失 ──────────────────────────────


def test_fetch_listing_info_records_share_capital_missing(duckdb_store, sqlite_store) -> None:
    class ListingAdapter:
        def fetch(self, request):
            return _fetch_result([
                {"stock_code": "000001", "listing_date": "2020-01-01",
                 "is_st": False, "is_suspended": False,
                 "total_shares": None, "circ_shares": None},
                {"stock_code": "000002", "listing_date": "2020-01-01",
                 "is_st": False, "is_suspended": False,
                 "total_shares": 100.0, "circ_shares": 80.0},
            ])

    _seed_stock(duckdb_store, "000001", total_shares=None, circ_shares=None)
    _seed_stock(duckdb_store, "000002", total_shares=None, circ_shares=None)
    from app.core.init import DataInitializer

    initializer = DataInitializer(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=ListingAdapter(),
    )

    result = initializer._fetch_listing_info(["000001", "000002"])

    missing = sqlite_store.query(
        "SELECT stock_code, reason_code FROM missing_list "
        "WHERE field_name = 'share_capital' AND resolved_at IS NULL"
    )
    assert [row["stock_code"] for row in missing] == ["000001"]
    assert missing[0]["reason_code"] == "source_incomplete"
    assert result["share_capital_missing"] == 1
    # 三列元数据齐全 → listing_info 本身成功；股本缺口走独立 missing 通道，
    # 不压低 listing_info 刷新节流（否则 26 只长期缺股本的票会每天触发全市场重抓）。
    assert result["status"] == "success"
    assert result["share_capital_missing_codes"] == ["000001"]


def test_fetch_listing_info_skips_exempt_new_stock_share_capital(
    duckdb_store, sqlite_store,
) -> None:
    """90 天豁免期内的新股（两者皆 NULL）不登记 share_capital 缺口。"""
    today = datetime.now(UTC).date().isoformat()

    class ListingAdapter:
        def fetch(self, request):
            return _fetch_result([
                {"stock_code": "301688", "listing_date": today,
                 "is_st": False, "is_suspended": False,
                 "total_shares": None, "circ_shares": None},
            ])

    _seed_stock(
        duckdb_store, "301688", listing_date=today,
        total_shares=None, circ_shares=None,
    )
    from app.core.init import DataInitializer

    initializer = DataInitializer(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=ListingAdapter(),
    )

    result = initializer._fetch_listing_info(["301688"])

    assert result["share_capital_missing"] == 0
    assert sqlite_store.query(
        "SELECT * FROM missing_list WHERE field_name = 'share_capital'"
    ) == []


# ─── 5. retry 分类 ───────────────────────────────────────────────────


def test_legal_empty_and_unsupported_settle_into_missing_not_retry(
    duckdb_store, sqlite_store,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._record_failure(
        "601091", "dividends", "ths", "no dividend history", error_kind="legal_empty",
    )
    updater._record_failure(
        "920201", "dividends", "ths", "BSE unsupported", error_kind="source_unsupported",
    )
    updater._record_failure(
        "601123", "price_daily", "ths", "timeout", error_kind="source_unavailable",
    )

    tasks = updater._check_retry_tasks()

    assert [task["stock_code"] for task in tasks] == ["601123"], "真故障才进重试"
    assert tasks[0]["error_kind"] == "source_unavailable"
    remaining = sqlite_store.query("SELECT stock_code FROM retry_list ORDER BY stock_code")
    assert [row["stock_code"] for row in remaining] == ["601123"]
    missing = {
        row["stock_code"]: row["reason_code"]
        for row in sqlite_store.query(
            "SELECT stock_code, reason_code FROM missing_list WHERE resolved_at IS NULL"
        )
    }
    assert missing == {"601091": "source_empty", "920201": "source_unsupported"}


def test_settled_non_retryable_rows_do_not_consume_retry_budget(
    duckdb_store, sqlite_store,
) -> None:
    """retry_count=4/5 的合法空任务立即结算，不得再被推成 retry_exhausted。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    with sqlite_store.transaction() as conn:
        # 保留内联：`last_attempt` 要库侧 `CURRENT_TIMESTAMP`（"刚尝试过"），本轮不收敛。
        conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count, max_retries,
                    last_attempt, extra_json, error_kind)
               VALUES ('301689', 'dividends', 'ths', 'no dividend', 4, 5,
                       CURRENT_TIMESTAMP, '{}', 'legal_empty')"""
        )

    assert updater._check_retry_tasks() == []
    assert sqlite_store.query("SELECT * FROM retry_list") == []
    assert sqlite_store.query(
        "SELECT reason_code FROM missing_list WHERE stock_code = '301689'"
    ) == [{"reason_code": "source_empty"}]


def test_skipped_codes_settle_as_source_unsupported(duckdb_store, sqlite_store) -> None:
    """manager 的 metadata.skipped_codes 直接进 missing，不写 retry（零尝试假失败）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._record_failure("603435", "dividends", "manager", "无可用适配器 (tried: [])")

    updater._settle_skipped_codes("dividends", {"603435": "BSE has no eastmoney source"})

    assert sqlite_store.query("SELECT * FROM retry_list") == []
    assert sqlite_store.query(
        "SELECT stock_code, field_name, reason_code FROM missing_list"
    ) == [{"stock_code": "603435", "field_name": "dividends",
           "reason_code": "source_unsupported"}]


def test_zero_attempt_failure_is_rechecked_once(duckdb_store, sqlite_store) -> None:
    """历史 `无可用适配器 (tried: [])` 行重置为一次干净重试，且只重置一次。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    with sqlite_store.transaction() as conn:
        # 保留内联：同上（`last_attempt` 用库侧时钟），本轮不收敛。
        conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count, max_retries,
                    last_attempt, extra_json)
               VALUES ('601123', 'dividends', 'manager',
                       '无可用适配器 (tried: [])', 4, 5, CURRENT_TIMESTAMP, '{}')"""
        )

    updater._cleanup_unretryable_tasks()

    row = sqlite_store.query(
        "SELECT retry_count, error FROM retry_list WHERE stock_code = '601123'"
    )
    assert row and row[0]["retry_count"] == 0, "零尝试假失败必须重查一次"
    assert row[0]["error"].startswith("[zero_attempt_recheck]")

    # 第二次清理不得再次重置（避免死循环）。
    sqlite_store.execute("UPDATE retry_list SET retry_count = 3 WHERE stock_code = '601123'")
    updater._cleanup_unretryable_tasks()
    assert sqlite_store.query(
        "SELECT retry_count FROM retry_list WHERE stock_code = '601123'"
    )[0]["retry_count"] == 3


# ─── 6. 价格失败归因 ─────────────────────────────────────────────────


def test_price_failures_report_failed_codes_and_retry(
    duckdb_store, sqlite_store, caplog,
) -> None:
    class Adapter:
        def fetch(self, request):
            if request.stock_codes == ["600002"]:
                return _fetch_result([], error="source blocked", adjust=request.adjust)
            rows = [{"trade_date": "2026-08-05", "close": 10.0, "volume": 100.0}]
            return _fetch_result(rows, adjust=request.adjust)

    _seed_stock(duckdb_store, "600001")
    _seed_stock(duckdb_store, "600002")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=Adapter())
    updater._latest_expected_trading_date = lambda today: "2026-08-05"

    with caplog.at_level(logging.WARNING, logger="app.core.update"):
        report = updater._update_prices_incremental(max_stocks=0)

    assert report["failed_codes"] == ["600002"]
    assert report["failed"] == 1
    assert any("600002" in record.getMessage() for record in caplog.records), \
        "每只失败股票都要有 WARNING 日志"
    retry = sqlite_store.query(
        "SELECT stock_code, data_type, error_kind FROM retry_list"
    )
    assert retry == [{"stock_code": "600002", "data_type": "price_daily",
                      "error_kind": "source_unavailable"}]


# ─── 7. 公告步骤入册 ─────────────────────────────────────────────────


def test_announcement_step_result_maps_statuses() -> None:
    assert _announcement_step_result({
        "status": "available", "checked_remote": True,
        "affected_stock_codes": ["000001"], "all_new_announcements": {"000001": [{}]},
    })["status"] == "success"
    truncated = _announcement_step_result({
        "status": "partial", "truncated": True, "truncated_categories": ["annual"],
        "checked_remote": True, "affected_stock_codes": [],
    })
    assert truncated["status"] == "partial"
    assert truncated["reason"] == "announcements_truncated"
    assert _announcement_step_result({
        "status": "unavailable", "errors": ["boom"],
    })["status"] == "failed"


def test_announcement_check_propagates_source_truncation(duckdb_store, sqlite_store) -> None:
    """适配器标记 truncated 时检查结果必须是 partial + truncated，而不是 ERROR。"""
    truncated_metadata = SimpleNamespace(
        error=None, truncated=True, error_kind="truncated_ok", skipped_codes={},
    )

    class TruncatedAdapter:
        def fetch(self, request):
            return SimpleNamespace(
                data=[{"announcement_id": "a-1", "stock_code": "000001",
                       "title": "2026年半年报", "announcement_time": "2026-08-25"}],
                metadata=truncated_metadata,
                raw_response=b"x",
            )

    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=TruncatedAdapter(),
    )

    result = updater._check_new_announcements(persist=False)

    assert result["truncated"] is True
    assert result["status"] == "partial"
    assert result["reason"] == "announcements_truncated"


def test_announcement_step_is_reported_in_steps(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    _stub_flow(updater)
    updater._check_new_announcements = lambda persist=True, **kwargs: {
        "status": "partial", "reason": "announcements_truncated",
        "truncated": True, "truncated_categories": ["annual"],
        "checked_remote": True, "affected_stock_codes": [],
        "affected_announcements": {}, "all_new_announcements": {},
        "errors": [], "warnings": [],
    }

    report = updater.run_incremental_update()

    announcements = report["steps"].get("announcements")
    assert announcements is not None, "公告步骤必须出现在 steps 里"
    assert announcements["status"] == "partial"
    assert announcements["reason"] == "announcements_truncated"
    assert announcements["truncated"] is True


# ─── 8. missing_count 口径 ────────────────────────────────────────────


def test_incremental_check_uses_actionable_missing_count(
    duckdb_store, sqlite_store,
) -> None:
    """已解决的历史缺失不得计入 missing_count（旧口径 11,823 vs 可行动 162）。"""
    with sqlite_store.transaction() as conn:
        # `join(conn)`：事务已在调用点手里，工厂只写不提交。
        seed = SeedSqlite(sqlite_store).join(conn)
        # 已解决的历史行：`resolved_at` 仍由库侧时钟给（`sql_now()` ＝ `datetime('now')`）。
        seed.missing_list("600519", "listing_info", "source_incomplete", resolved_at=sql_now())
        seed.missing_list("000001", "share_capital", "source_incomplete")
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    report = updater.run_incremental_check(include_announcements=False)

    assert report["missing_count"] == 1
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM missing_list")[0]["c"] == 2


def test_retry_exhaustion_keeps_legacy_field_name_for_unknown_kind(
    duckdb_store, sqlite_store,
) -> None:
    """error_kind=unknown 的耗尽行保持 field_name='retry_exhausted:<type>'（兼容旧查询）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    with sqlite_store.transaction() as conn:
        # 保留内联：`lastrowid` 回捞 —— 这一行后面要被 `_mark_retry_failed` 指着更新，
        # 工厂刻意不返回 id（见 `tests/support/seed_sqlite.py`「尚未覆盖」）。
        cursor = conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count,
                    last_attempt, next_retry_at, max_retries, extra_json)
               VALUES ('000001', 'balance_sheet', 'sina', 'boom', 1,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2, '{}')"""
        )
        retry_id = cursor.lastrowid

    updater._mark_retry_failed(retry_id, "still failing")

    assert sqlite_store.query(
        "SELECT * FROM missing_list WHERE field_name = 'retry_exhausted:balance_sheet'"
    )
