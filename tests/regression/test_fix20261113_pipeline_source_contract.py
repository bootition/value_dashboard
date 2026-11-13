"""2026-11-13 修复：公告 general 截断口径（C）、空适配器链（D）、baostock 空记录（F）。

C（P1）：general 全文列表只用于"非财务公告登记"（可选簿记）。它截断时不得把
整轮 job 打成 partial —— 生产 `last_full_success_at` 因此冻结在 2026-10-30。
只有财务/分红类别截断才算 partial；general 只进 warnings（带条数与窗口）。

D（P1）：`tried_adapters` 为空（未注册/依赖缺失/全链冷却）此前返回
`error_kind="source_unsupported"`，而上层会把 source_unsupported 直接结算成
missing 并删 retry → 环境问题被伪装成"该票没数据"，永久不再重试。

F（P2）：baostock 无 skip、无错误、0 条记录时返回 `("none", None, None)`，
manager 于是打出误导性的 `WARNING ... 返回错误（none）: detail=none`。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.adapters.baostock_adapter import BaoStockAdapter
from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.manager import AdapterManager
from app.core.update import (
    _NON_RETRYABLE_ERROR_KINDS,
    IncrementalUpdater,
    _announcement_step_result,
)


def _announcement_request(category: str | None) -> FetchRequest:
    return FetchRequest(
        data_type="announcements",
        extra_params={"category": category, "page_size": 30, "max_pages": 5},
    )


class _CategoryAwareAdapter:
    """Returns one financial announcement; `truncated_categories` controls truncation."""

    def __init__(self, truncated_categories: set[str | None]) -> None:
        self.truncated_categories = truncated_categories

    def fetch(self, request: FetchRequest) -> FetchResult:
        category = request.extra_params.get("category")
        truncated = category in self.truncated_categories
        if category in {"semi_annual", "q1", "q3", "annual"}:
            rows = [{
                "announcement_id": f"notice-{category}",
                "stock_code": "600519",
                "title": "2026年第三季度报告",
                "announcement_time": "2026-10-30T00:00:00Z",
            }]
        elif category == "general" or category is None:
            rows = [{
                "announcement_id": "notice-general",
                "stock_code": "000001",
                "title": "关于召开股东大会的通知",
                "announcement_time": "2026-11-10T00:00:00Z",
            }]
        else:  # dividend
            rows = []
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="cninfo",
                fetch_time=datetime.now(UTC),
                raw_response_hash="a" * 64,
                confidence="strict",
                error=None,
                error_kind="truncated_ok" if truncated else "none",
                truncated=truncated,
                reason="分页上限截断" if truncated else None,
            ),
        )


def test_general_truncation_is_warning_only_not_partial(
    duckdb_store, sqlite_store,
) -> None:
    """C：general 截断 → status=available，truncated=False，warnings 带条数与窗口。"""
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter_mgr=_CategoryAwareAdapter({None}),
    )

    result = updater._check_new_announcements(persist=False)

    assert result["status"] == "available", "general 截断不得让整轮 job partial"
    assert result["truncated"] is False
    assert result["truncated_categories"] == []
    warnings = " ".join(result["warnings"])
    assert "general 窗口截断" in warnings
    assert "2026-" in warnings, "warnings 必须带窗口"
    assert "1 条" in warnings, "warnings 必须带已取条数"
    assert _announcement_step_result(result)["status"] == "success"


def test_financial_category_truncation_still_partial(duckdb_store, sqlite_store) -> None:
    """C：财务类别截断仍必须如实 partial（截断不丢数据≠可以静默）。"""
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter_mgr=_CategoryAwareAdapter({"annual", None}),
    )

    result = updater._check_new_announcements(persist=False)

    assert result["status"] == "partial"
    assert result["truncated"] is True
    assert result["truncated_categories"] == ["annual"]
    step = _announcement_step_result(result)
    assert step["status"] == "partial"
    assert step["reason"] == "announcements_truncated"
    assert any("general 窗口截断" in warning for warning in result["warnings"])


def test_dividend_category_truncation_still_partial(duckdb_store, sqlite_store) -> None:
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store,
        adapter_mgr=_CategoryAwareAdapter({"dividend"}),
    )

    result = updater._check_new_announcements(persist=False)

    assert result["status"] == "partial"
    assert result["truncated_categories"] == ["dividend"]


# ─── D：空适配器链不得伪装成 source_unsupported ────────────────────


def _manager_with(adapters: dict[str, Any]) -> AdapterManager:
    manager = AdapterManager()
    manager._initialized = True
    manager._adapters.update(adapters)
    return manager


def _empty_result(error_kind: str, *, reason: str | None = None) -> FetchResult:
    return FetchResult(
        data=[],
        metadata=SourceMetadata(
            source="akshare_eastmoney",
            fetch_time=datetime.now(UTC),
            raw_response_hash="",
            confidence="missing",
            error=None,
            error_kind=error_kind,  # type: ignore[arg-type]
            reason=reason,
        ),
    )


def test_unregistered_adapter_chain_is_retryable_not_unsupported() -> None:
    """D：未注册/依赖缺失 → source_unavailable + adapter_not_registered，可重试。"""
    manager = _manager_with({})  # 一个适配器都没注册

    result = manager.fetch_with_sources(
        FetchRequest(data_type="dividends", stock_codes=["601123"]),
        ["akshare_eastmoney"],
    )

    assert result.data == []
    assert result.metadata.error_kind == "source_unavailable"
    assert result.metadata.error_kind not in _NON_RETRYABLE_ERROR_KINDS
    message = result.metadata.error or ""
    assert "adapter_not_registered" in message
    assert result.metadata.reason
    assert "None" not in message


def test_all_circuits_tripped_is_retryable_with_cooldown_marker() -> None:
    """D：全熔断冷却 → source_unavailable + adapter_cooldown（可重试）。"""

    class _Adapter:
        name = "akshare_eastmoney"

        def can_handle(self, request: FetchRequest) -> bool:
            return True

        def fetch(self, request: FetchRequest) -> FetchResult:
            raise AssertionError("熔断中的适配器不得被调用")

    manager = _manager_with({"akshare_eastmoney": _Adapter()})
    for _ in range(5):
        manager._record_circuit_failure("akshare_eastmoney", "dividends")

    result = manager.fetch_with_sources(
        FetchRequest(data_type="dividends", stock_codes=["600519"]),
        ["akshare_eastmoney"],
    )

    assert result.metadata.error_kind == "source_unavailable"
    assert result.metadata.error_kind not in _NON_RETRYABLE_ERROR_KINDS
    assert "adapter_cooldown" in (result.metadata.error or "")


def test_empty_candidate_list_is_retryable(monkeypatch) -> None:
    """D：候选源为空（链配置缺失）同样不得伪装成 source_unsupported。"""
    import app.core.adapters.manager as manager_module

    monkeypatch.setattr(manager_module, "ADAPTER_PRIORITY", {})
    manager = _manager_with({})

    result = manager.fetch_with_sources(
        FetchRequest(data_type="dividends", stock_codes=["600519"]),
        [],
    )

    assert result.metadata.error_kind == "source_unavailable"
    assert "adapter_not_registered" in (result.metadata.error or "")


# ─── F：baostock 空记录 → legal_empty，且 manager 打 INFO ──────────


def test_baostock_empty_result_is_classified_legal_empty() -> None:
    kind, error, reason = BaoStockAdapter._classified_source_fields({}, [], 0)

    assert kind == "legal_empty"
    assert error is None
    assert reason and "0 条" in reason


def test_baostock_non_empty_result_stays_none() -> None:
    kind, error, reason = BaoStockAdapter._classified_source_fields({}, [], 12)

    assert (kind, error, reason) == ("none", None, None)


def test_baostock_skip_and_error_classification_unchanged() -> None:
    assert BaoStockAdapter._classified_source_fields({"900948": "unsupported"}, [], 0)[0] == (
        "source_unsupported"
    )
    kind, error, _reason = BaoStockAdapter._classified_source_fields(
        {}, ["query failed"], 0,
    )
    assert kind == "source_unavailable"
    assert error == "query failed"


def test_manager_logs_legal_empty_as_info_not_misleading_warning(caplog) -> None:
    """F：合法空必须 INFO，且不得再出现"返回错误（none）"的误导 WARNING。"""

    class _LegalEmptyAdapter:
        name = "baostock"

        def can_handle(self, request: FetchRequest) -> bool:
            return True

        def fetch(self, request: FetchRequest) -> FetchResult:
            return _empty_result("legal_empty", reason="baostock 查询成功但返回 0 条记录")

    manager = _manager_with({"baostock": _LegalEmptyAdapter()})
    with caplog.at_level(logging.INFO, logger="app.core.adapters.manager"):
        result = manager.fetch_with_sources(
            FetchRequest(data_type="dividends", stock_codes=["600519"]), ["baostock"],
        )

    assert result.metadata.error_kind == "legal_empty"
    assert result.metadata.error is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("合法空" in message for message in messages), messages
    assert not any("返回错误" in message for message in messages), messages


def test_manager_does_not_warn_error_none_for_unlabelled_empty(caplog) -> None:
    """F：适配器漏标 error_kind 时也不得打出"返回错误（none）"的 WARNING。"""

    class _UnlabelledAdapter:
        name = "baostock"

        def can_handle(self, request: FetchRequest) -> bool:
            return True

        def fetch(self, request: FetchRequest) -> FetchResult:
            return _empty_result("none")

    manager = _manager_with({"baostock": _UnlabelledAdapter()})
    with caplog.at_level(logging.INFO, logger="app.core.adapters.manager"):
        manager.fetch_with_sources(
            FetchRequest(data_type="dividends", stock_codes=["600519"]), ["baostock"],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert not any("返回错误（none）" in message for message in messages), messages
    assert any("未标注 error_kind" in message for message in messages), messages
