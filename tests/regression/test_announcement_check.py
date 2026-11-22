from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.cninfo_adapter import CNINFOAdapter
from app.core.update import IncrementalUpdater, classify_announcement
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite, sql_now


class EmptySQLite:
    def query(self, sql: str) -> list[dict[str, int]]:
        return [{"cnt": 0}]


def test_announcement_check_does_not_mark_a_filing_seen_before_refresh(duckdb_store, sqlite_store) -> None:
    class AnnouncementAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{"announcement_id": "notice-1", "announcement_time": "2026-07-28T00:00:00Z",
                       "title": "2026年半年度报告", "stock_code": "000001"}],
                metadata=SourceMetadata(source="cninfo", fetch_time=datetime.now(UTC), raw_response_hash="a" * 64, confidence="strict"),
            )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=AnnouncementAdapter())

    first = updater._check_new_announcements(persist=False)
    second = updater._check_new_announcements(persist=False)
    persisted = updater._check_new_announcements(persist=True)

    assert first["status"] == "available"
    assert first["affected_stock_codes"] == ["000001"]
    assert second["affected_stock_codes"] == ["000001"]
    assert persisted["affected_stock_codes"] == ["000001"]
    assert sqlite_store.query("SELECT * FROM announcement_registry") == []


def test_category_query_title_variant_triggers_financial_refresh(duckdb_store, sqlite_store) -> None:
    """半年度类别接口返回的公告无论标题写法都必须进入财务刷新集合。"""
    class CategoryAnnouncementAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{"announcement_id": "notice-half-year", "announcement_time": "2026-08-25T08:00:00Z",
                       "title": "2026年半年报", "stock_code": "603365"}],
                metadata=SourceMetadata(source="cninfo", fetch_time=datetime.now(UTC), raw_response_hash="a" * 64, confidence="strict"),
            )

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=CategoryAnnouncementAdapter())
    result = updater._check_new_announcements(persist=True)
    assert result["affected_stock_codes"] == ["603365"]
    assert result["affected_announcements"]["603365"][0]["title"] == "2026年半年报"


def test_financial_refresh_marks_skipped_but_current_period_as_succeeded(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """本地已有 2026-06-30 三表时，重新抓取全部 skipped 应视为成功，不是 pending。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    # 冻结"当前应发布报告期"：本用例固定 2026-06-30 的本地数据，若不冻结则
    # 每到三季报季（11 月起 expected=09-30）就会误判为 pending。只影响测试注入点，
    # 生产日期推导逻辑不变。
    monkeypatch.setattr(
        updater, "_current_expected_financial_period", lambda now=None: "2026-06-30",
    )
    updater._fetch_financial_trio = lambda code: (
        [
            {"status": "success", "data_type": data_type, "skipped": True,
             "latest_local": "2026-06-30"}
            for data_type in ("balance_sheet", "income_statement", "cash_flow")
        ],
        {},
    )

    result = updater._refresh_financials(["000001"])

    assert result["status"] == "success"
    assert result["succeeded_codes"] == ["000001"]
    assert result["pending_codes"] == []


def test_partial_trio_refresh_stays_pending(duckdb_store, sqlite_store) -> None:
    """只有一张表推进到新报告期时不得把公告标记为成功入册。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._fetch_financial_trio = lambda code: (
        [
            {"status": "success", "data_type": "balance_sheet", "skipped": False,
             "latest_fetched": "2026-06-30"},
            {"status": "success", "data_type": "income_statement", "skipped": True,
             "latest_local": "2026-03-31"},
            {"status": "success", "data_type": "cash_flow", "skipped": True,
             "latest_local": "2026-03-31"},
        ],
        {},
    )

    result = updater._refresh_financials(["603365"])

    assert result["status"] == "success"
    assert result["succeeded_codes"] == []
    assert result["pending_codes"] == ["603365"]


def test_failed_financial_refresh_keeps_announcement_pending_and_records_retry(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False, **kwargs: pending
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "partial", "succeeded_codes": [], "failed_codes": codes,
    }
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query("SELECT * FROM announcement_registry") == []
    assert sqlite_store.query(
        "SELECT data_type FROM retry_list WHERE stock_code = '000001'"
    ) == [{"data_type": "announcements"}]


def test_successful_financial_refresh_marks_announcement_seen(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False, **kwargs: pending
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "success", "succeeded_codes": codes, "failed_codes": [],
    }
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query(
        "SELECT announcement_id, stock_code FROM announcement_registry"
    ) == [{"announcement_id": "notice-1", "stock_code": "000001"}]


def test_non_financial_announcement_is_registered_without_financial_refresh(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": [],
        "affected_announcements": {},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-2", "title": "关于召开临时股东大会的通知"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False, **kwargs: pending
    updater._refresh_financials = lambda codes: (_ for _ in ()).throw(AssertionError("financial refresh must not run"))
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query(
        "SELECT announcement_id, stock_code FROM announcement_registry"
    ) == [{"announcement_id": "notice-2", "stock_code": "000001"}]


def test_announcement_retry_cleanup_requires_registered_ids(
    duckdb_store, sqlite_store,
) -> None:
    """Q1 完整但中报公告未入册时，retry 不得被误清。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    SeedSqlite(sqlite_store).retry(
        "000001", "announcements", adapter="cninfo",
        error="financial refresh failed; announcement remains pending",
        retry_count=0,
        last_attempt=sql_now(),  # 原 SQL 的 `CURRENT_TIMESTAMP`：库侧"刚试过"
        extra_json='{"announcement_ids": ["a1", "a2"]}',
    )
    assert updater._cleanup_completed_announcement_retries() == 0
    updater._mark_announcements_seen("000001", [
        {"announcement_id": "a1", "title": "x", "announcement_time": ""},
        {"announcement_id": "a2", "title": "x", "announcement_time": ""},
    ])
    assert updater._cleanup_completed_announcement_retries() == 1
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM retry_list WHERE data_type='announcements'"
    ) == [{"c": 0}]


def test_global_announcement_source_retry_cleared_after_recovery(
    duckdb_store, sqlite_store,
) -> None:
    """CNINFO 全局故障恢复后，stock_code='' 的源级 retry 不得永久残留。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    # 两行同一张 `retry_list`：源级（`stock_code=''`）与个股级各一条，靠工厂的一次一事务
    # 分开写 —— 唯一索引 `(stock_code, data_type, adapter, extra_json)` 上两者不冲突。
    seed = SeedSqlite(sqlite_store)
    seed.retry(
        "", "announcements", adapter="cninfo", error="all_boards_failed",
        retry_count=0, last_attempt=sql_now(),  # 库侧时钟（原 SQL：CURRENT_TIMESTAMP）
    )
    seed.retry(
        "000001", "announcements", adapter="cninfo", error="pending",
        retry_count=0, last_attempt=sql_now(),
    )

    updater._resolve_announcement_source_retries()

    rows = sqlite_store.query(
        "SELECT stock_code FROM retry_list WHERE data_type = 'announcements'"
    )
    assert rows == [{"stock_code": "000001"}]


def test_legal_empty_placement_funding_missing_is_resolved(
    duckdb_store, sqlite_store,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    # 原来卡在缺口 B（`missing_list` 无 SQLite 侧方法）；`resolved_at` 都留 NULL
    # —— "未解决"正是 `_resolve_legal_empty_funding_missing()` 要认的行。
    seed_sqlite = SeedSqlite(sqlite_store)
    seed_sqlite.missing_list("000001", "placement_funding", "source_empty")
    seed_sqlite.missing_list("920000", "placement_funding", "source_empty")
    assert updater._resolve_legal_empty_funding_missing() == 1
    rows = sqlite_store.query(
        "SELECT stock_code, resolved_at FROM missing_list ORDER BY stock_code"
    )
    assert rows[0]["resolved_at"] is not None
    assert rows[1]["resolved_at"] is None


def test_b_share_detection_uses_prefix_and_name_suffix() -> None:
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.duck = type("FakeDuck", (), {
        "read_query": lambda self, sql, params: (
            [{"name": "深中集B"}] if params and params[0] == "000001" else []
        ),
    })()
    assert updater._is_b_share_stock("200011") is True
    assert updater._is_b_share_stock("201872") is True
    assert updater._is_b_share_stock("900901") is True
    assert updater._is_b_share_stock("000001") is True  # name suffix B
    assert updater._is_b_share_stock("600519") is False


def test_classify_announcement_financial_keywords() -> None:
    assert classify_announcement("2026年半年度报告") == "financial"
    assert classify_announcement("2025年年度报告") == "financial"
    assert classify_announcement("2026年第一季度报告") == "financial"
    assert classify_announcement("2026年第三季度报告") == "financial"
    assert classify_announcement("2026年半年度业绩预告") == "financial"
    assert classify_announcement("2026年年度业绩快报") == "financial"
    # CNINFO 真实标题变体（2026-09-08 红队：603365 漏报根因）
    assert classify_announcement("2026年半年报") == "financial"
    assert classify_announcement("2026年半年报摘要") == "financial"


def test_classify_announcement_dividend_and_other() -> None:
    assert classify_announcement("2025年度权益分派实施公告") == "dividend"
    assert classify_announcement("关于召开临时股东大会的通知") == "other"
    assert classify_announcement("高管辞职公告") == "other"
    assert classify_announcement(None) == "other"


def test_announcement_refreshes_dividends_and_corporate_actions(duckdb_store, sqlite_store) -> None:
    """批量抓取无数据时回退逐股，分红与除权除息两个数据域都要覆盖。"""
    import hashlib

    class EmptyBatchAdapter:
        def fetch(self, request):
            raw = b"{}"
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="tdx", fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw).hexdigest(),
                    confidence="approximate",
                ),
                raw_response=raw,
            )

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = EmptyBatchAdapter()
    updater.market_actions_batch_size = 50
    calls: list[tuple[str, str]] = []
    updater.refetch_one = lambda code, data_type: calls.append((code, data_type)) or {"status": "success"}

    result = updater._refresh_market_actions(["000001"])

    assert result["status"] == "success"
    assert calls == [("000001", "dividends"), ("000001", "xdxr")]


def test_incremental_check_exposes_announcement_capability_gap() -> None:
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.sqlite = EmptySQLite()
    updater._check_new_trading_days = lambda: []
    updater._get_latest_local_price_date = lambda: "2026-07-20"
    updater._check_retry_tasks = lambda: []

    report = updater.run_incremental_check()

    assert report["announcement_check"]["status"] == "unavailable"
    assert report["needs_update"] is True
    assert report["blocked"] is True


def test_refetch_one_incremental_writes_new_report_period(duckdb_store, sqlite_store) -> None:
    """本地已有 2026Q1，数据源返回 2026Q2 → 只写入新报告期，Q1 不变。"""
    import hashlib

    raw_response = b'{"num":1}'

    class FinancialAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{
                    "stock_code": "000001", "report_date": "2026-06-30",
                    "total_assets": 200.0,
                }],
                metadata=SourceMetadata(
                    source="sina", fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="strict",
                ),
                raw_response=raw_response,
            )

    Seed(duckdb_store).balance("000001", "2026-03-31", total_assets=100.0)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FinancialAdapter())

    result = updater.refetch_one("000001", "balance_sheet", incremental=True)

    assert result["status"] == "success"
    assert result["skipped"] is False
    rows = duckdb_store.read_query(
        "SELECT report_date, total_assets FROM balance_sheet WHERE stock_code = '000001' ORDER BY report_date"
    )
    assert [(str(r["report_date"]), r["total_assets"]) for r in rows] == [
        ("2026-03-31", 100.0), ("2026-06-30", 200.0),
    ]


def test_refetch_one_incremental_skips_when_source_has_no_new_period(duckdb_store, sqlite_store) -> None:
    """数据源仍返回旧报告期（财报延迟）→ skipped，不写入、不覆盖旧值。"""
    import hashlib

    raw_response = b'{"num":1}'

    class FinancialAdapter:
        def fetch(self, request):
            return FetchResult(
                data=[{
                    "stock_code": "000001", "report_date": "2026-03-31",
                    "total_assets": 99.0,
                }],
                metadata=SourceMetadata(
                    source="sina", fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(raw_response).hexdigest(), confidence="strict",
                ),
                raw_response=raw_response,
            )

    Seed(duckdb_store).balance("000001", "2026-03-31", total_assets=100.0)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FinancialAdapter())

    result = updater.refetch_one("000001", "balance_sheet", incremental=True)

    assert result["status"] == "success"
    assert result["skipped"] is True
    rows = duckdb_store.read_query(
        "SELECT total_assets FROM balance_sheet WHERE stock_code = '000001' AND report_date = '2026-03-31'"
    )
    assert rows == [{"total_assets": 100.0}]  # 旧值保留，不被 99.0 覆盖


def test_refresh_financials_marks_pending_when_source_lags(duckdb_store, sqlite_store) -> None:
    """三表均无新报告期（数据源延迟）→ 股票进入 pending_codes，不标记 seen。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._fetch_financial_trio = lambda code: (
        [
            {"status": "success", "data_type": data_type, "skipped": True}
            for data_type in ("balance_sheet", "income_statement", "cash_flow")
        ],
        {},
    )

    result = updater._refresh_financials(["000001"])

    assert result["status"] == "success"
    assert result["succeeded_codes"] == []
    assert result["pending_codes"] == ["000001"]


def test_pending_financial_refresh_keeps_announcement_pending_and_retries(duckdb_store, sqlite_store) -> None:
    """数据源延迟时，公告保持 pending 并进入重试列表（下次启动重试）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    check = {
        "new_trading_days": [], "retry_tasks": [], "latest_local_price_date": "2026-07-20",
        "announcement_check": {"status": "available"}, "needs_update": True, "blocked": False,
    }
    pending = {
        "status": "available", "affected_stock_codes": ["000001"],
        "affected_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
        "all_new_announcements": {"000001": [{"announcement_id": "notice-1", "title": "2026年半年度报告"}]},
    }
    updater.run_incremental_check = lambda **kwargs: check
    updater._check_new_announcements = lambda persist=False, **kwargs: pending
    updater._refresh_financials = lambda codes, **kwargs: {
        "status": "success", "succeeded_codes": [], "failed_codes": [],
        "pending_codes": codes,
    }
    updater._refresh_market_actions = lambda codes, **kwargs: {"status": "success", "success": len(codes)}
    updater._update_prices_incremental = lambda max_stocks, detail_cb=None: {"status": "skipped", "success": 0}
    # P0-2: run_incremental_update 新增 universe 步骤；测试聚焦公告链路，跳过它
    updater._refresh_universe_metadata = lambda: {"status": "skipped", "steps": {}}

    updater.run_incremental_update()

    assert sqlite_store.query("SELECT * FROM announcement_registry") == []
    retry = sqlite_store.query(
        "SELECT data_type, error FROM retry_list WHERE stock_code = '000001'"
    )
    assert retry == [{
        "data_type": "announcements",
        "error": "financial data source not yet ready; announcement remains pending",
    }]


# ─────────────────────────────────────────────────────────────────────────────
# general 公告页上限 5 → 20（2026-11-22，覆盖 vs 时间的折中）
#
# 背景：正式库 `announcement_registry` 的单日登记量只有 CNINFO 当日 general
# 全文列表的十分之一量级（登记表三百条量级 vs 源侧千条量级），因为
# `announcement_general_max_pages` 只有 5（5×30=150 条/窗口）。用户裁决"先做 20 页"
# （20×30=600 条；千条量级的一天要 ~45 页才全量，那是没做的部分）。
# 口径与折中理由写在 `config/default.yaml` 该键上方注释里。
#
# 这里钉住两件事：① 上限**走配置**而不是代码里写死的第二份数（历史上
# `_load_update_config(..., default=40)` / `getattr(..., 40)` 与配置的 5 分裂）；
# ② 上限放大**没有**削弱截断告警 —— 单日/单选仍超限时照样 `truncated_ok`
# + warnings，绝不把"抓了 600 条"说成"抓全了"。
# ─────────────────────────────────────────────────────────────────────────────


def test_general_announcement_page_cap_is_20_from_config_and_truncation_only_warns(
    duckdb_store, sqlite_store,
) -> None:
    """`max_pages` 取配置的 20；general 截断只进 `warnings`，不把整轮打成 partial。

    `config/default.yaml` 与 `app/resources/config/default.yaml` 都声明 20，所以
    `__init__` 经 `_load_update_config` 读到的就是 20（真实生效值）。适配器收到的
    `max_pages` 必须是它 —— 而不是某个写死在调用点的数字。
    """

    class GeneralTruncatedAdapter:
        """6 个类别查询：财务/分红给空、general 给"截断的 600 条"。"""

        def __init__(self) -> None:
            self.max_pages_seen: dict[str | None, int] = {}

        def fetch(self, request):
            category = request.extra_params.get("category")
            self.max_pages_seen[category] = int(
                request.extra_params.get("max_pages") or 0
            )
            if category is not None:
                return FetchResult(
                    data=[],
                    metadata=SourceMetadata(
                        source="cninfo", fetch_time=datetime.now(UTC),
                        raw_response_hash="a" * 64, confidence="strict",
                    ),
                )
            # general 全文列表：单日仍超 20 页 → 适配器如实声明截断（契约 §1）
            return FetchResult(
                data=[
                    {
                        "announcement_id": f"general-{index}",
                        "stock_code": "000001",
                        "title": "关于召开临时股东大会的通知",
                        "_announcement_category": None,
                    }
                    for index in range(600)
                ],
                metadata=SourceMetadata(
                    source="cninfo", fetch_time=datetime.now(UTC),
                    raw_response_hash="a" * 64, confidence="approximate",
                    error_kind="truncated_ok",
                    reason="announcements truncated: total=1300 fetched=600",
                    truncated=True,
                ),
            )

    adapter = GeneralTruncatedAdapter()
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter,
    )
    # 2026-11-22 起 general 默认关闭（详情页时间线已删）→ 本用例显式打开，
    # 验证"打开时页上限取配置 20、截断只进 warning"这条契约仍然成立。
    updater.announcement_general_enabled = True

    result = updater._check_new_announcements(persist=False, lookback_days=1)

    assert adapter.max_pages_seen[None] == 20, (
        f"general 页上限必须取配置的 20，实际 {adapter.max_pages_seen[None]}"
    )
    assert {
        pages for category, pages in adapter.max_pages_seen.items() if category is not None
    } == {450}, "财务/分红类别仍用 announcement_financial_max_pages=450，不受本项影响"

    # ② 截断仍然"如实"：warning 带条数与边界，且不升级成 partial
    warnings = result["warnings"]
    assert len(warnings) == 1, warnings
    assert "general 窗口截断" in warnings[0]
    assert "已取 600 条" in warnings[0], warnings[0]
    assert "只影响非财务公告登记" in warnings[0], warnings[0]
    assert result["status"] == "available", result["status"]
    assert result["truncated"] is False
    assert result["truncated_categories"] == []
    # 20 页 × 30 条 = 600 条必须真的进了登记候选（不是只改了个数字）
    assert len(result["all_new_announcements"]["000001"]) == 600


def test_cninfo_single_day_still_truncates_at_the_20_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20 页上限下，单日 3,700+ 条照样截断：600 行 + `truncated_ok`，不静默当全量。

    5→20 只是把"丢多少"从约 85% 降到约 50%，不是"覆盖完整"。这条用例防止
    以后有人把上限调大顺手把截断标记删掉 —— 那样 registry 会重新变成假绿。
    """
    raw = {
        "secCode": "000001",
        "secName": "平安银行",
        "announcementId": "ann-1",
        "announcementTitle": "关于召开临时股东大会的通知",
        "announcementTime": 1785964800000,
        "adjunctUrl": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"announcements": [raw] * 30, "totalRecordNum": 3719},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = CNINFOAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_resolve_org_id", lambda code: "org-1")
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    result = adapter.fetch(FetchRequest(
        data_type="announcements",
        stock_codes=["000001"],
        start_date="2026-11-20",
        end_date="2026-11-20",
        extra_params={"max_pages": 20},
    ))

    assert len(result.data) == 600, "20 页 × 30 条：已抓到的 600 条不得丢弃"
    assert result.metadata.truncated is True
    assert result.metadata.error_kind == "truncated_ok"
    assert result.metadata.error is None, "截断不是错误，不得填 error（会污染熔断判定）"
    assert "total=3719" in (result.metadata.reason or "")
    assert "fetched=600" in (result.metadata.reason or "")
    client.close()


class _RecordingAdapter:
    """记录每次 `fetch` 请求里带的 `category`，用于验证"关 general"真的少发了一条请求。"""

    def __init__(self) -> None:
        self.categories: list[str | None] = []

    def fetch(self, request):
        category = (request.extra_params or {}).get("category")
        self.categories.append(category)
        # 每个类别都回一条，让链路完整走通
        return FetchResult(
            data=[{
                "announcement_id": f"id-{category or 'general'}",
                "announcement_time": "2026-11-20T00:00:00Z",
                "title": f"{category or 'general'} 公告",
                "stock_code": "000001",
            }],
            metadata=SourceMetadata(
                source="cninfo", fetch_time=datetime.now(UTC),
                raw_response_hash="b" * 64, confidence="strict",
            ),
        )


def _announcement_probe(duckdb_store, sqlite_store, *, enabled: bool):
    adapter = _RecordingAdapter()
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=adapter,
    )
    updater.announcement_general_enabled = enabled
    result = updater._check_new_announcements(persist=False)
    return adapter.categories, result


def test_general_announcements_are_not_fetched_by_default(duckdb_store, sqlite_store) -> None:
    """2026-11-22 用户裁决：非财务公告（general 全文列表）默认不抓。

    它唯一的消费者是详情页「公告时间线」（已删除）。关掉它每轮省约 2 分钟。
    **财务与分红/除权必须照抓** —— 这是本用例的核心断言，不是"少抓点"就完事。
    """
    categories, result = _announcement_probe(duckdb_store, sqlite_store, enabled=False)

    # general = category 为 None 的那次调用：必须**没有**
    assert None not in categories, categories
    # 财务四类 + dividend 必须都在
    for category in ("semi_annual", "q1", "q3", "annual", "dividend"):
        assert category in categories, (category, categories)
    # 分红公告仍然被识别（链路没被削弱）
    assert result["status"] == "available"
    assert any(
        item.get("_announcement_category") == "dividend"
        for items in result["all_new_announcements"].values()
        for item in items
    ), result["all_new_announcements"]


def test_general_announcements_are_fetched_when_enabled(duckdb_store, sqlite_store) -> None:
    """开关打开 → general 照旧抓（保留恢复能力，别把路彻底焊死）。"""
    categories, _ = _announcement_probe(duckdb_store, sqlite_store, enabled=True)
    assert None in categories, categories
    for category in ("semi_annual", "q1", "q3", "annual", "dividend"):
        assert category in categories


def test_general_cap_config_defaults_off_and_keeps_dividend_cap(duckdb_store, sqlite_store) -> None:
    """配置层：general 默认关闭；分红/财务仍是 450 页额度（两者互不影响）。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater.announcement_general_enabled is False
    assert updater.announcement_financial_max_pages == 450
