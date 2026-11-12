"""2026-11-12 T1 回归：manager 熔断桶隔离 / 错误分类 / deadline / 可诊断日志。

契约：.planning/2026-11-12-update-log-review/contracts.md §1。
全部使用脚本化假适配器，不联网、不访问数据库。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.manager import AdapterManager


def _result(
    data: list[dict[str, Any]] | None = None,
    *,
    source: str = "akshare_eastmoney",
    error: str | None = None,
    error_kind: str = "none",
    reason: str | None = None,
    truncated: bool = False,
) -> FetchResult:
    rows = list(data or [])
    return FetchResult(
        data=rows,
        metadata=SourceMetadata(
            source=source,  # type: ignore[arg-type]
            fetch_time=datetime.now(UTC),
            raw_response_hash="",
            confidence="approximate" if rows else "missing",
            error=error,
            error_kind=error_kind,  # type: ignore[arg-type]
            reason=reason,
            truncated=truncated,
        ),
    )


class _ScriptedAdapter:
    """按顺序返回预设结果；用尽后重复最后一个。"""

    def __init__(self, results: list[Any], name: str = "akshare_eastmoney") -> None:
        self.name = name
        self._results = list(results)
        self.calls = 0

    def can_handle(self, request: FetchRequest) -> bool:
        return True

    def fetch(self, request: FetchRequest) -> FetchResult:
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        outcome = self._results[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


def _manager_with(adapters: dict[str, Any]) -> AdapterManager:
    manager = AdapterManager()
    manager._initialized = True
    manager._adapters.update(adapters)
    return manager


def _price_request() -> FetchRequest:
    return FetchRequest(data_type="price_daily", stock_codes=["600519"], adjust="raw")


def _dividend_request() -> FetchRequest:
    return FetchRequest(data_type="dividends", stock_codes=["600519"])


def test_per_symbol_data_error_never_trips_circuit() -> None:
    """验收 1：5 次 per_symbol_data_error 不得熔断。"""
    failure = _result(
        error="缺少 ex_date", error_kind="per_symbol_data_error", reason="缺少 ex_date",
    )
    adapter = _ScriptedAdapter([failure], name="akshare_eastmoney")
    manager = _manager_with({"akshare_eastmoney": adapter})

    results = [
        manager.fetch_with_sources(_dividend_request(), ["akshare_eastmoney"])
        for _ in range(5)
    ]

    assert adapter.calls == 5
    assert all(result.metadata.error_kind == "per_symbol_data_error" for result in results)
    assert manager._circuit_breaker.get("akshare_eastmoney:dividends", {}).get("failures", 0) == 0
    assert not manager._is_circuit_tripped("akshare_eastmoney", "dividends")


def test_circuit_buckets_are_isolated_by_adapter_and_data_type() -> None:
    """验收 2：price_daily 熔断不得影响 dividends 桶的尝试。"""
    outage = _result(error="source down", error_kind="source_unavailable")
    adapter = _ScriptedAdapter([outage], name="akshare_eastmoney")
    manager = _manager_with({"akshare_eastmoney": adapter})

    for _ in range(5):
        manager.fetch_with_sources(_price_request(), ["akshare_eastmoney"])
    assert manager._is_circuit_tripped("akshare_eastmoney", "price_daily")
    assert not manager._is_circuit_tripped("akshare_eastmoney", "dividends")

    calls_before = adapter.calls
    result = manager.fetch_with_sources(_dividend_request(), ["akshare_eastmoney"])

    assert adapter.calls == calls_before + 1, "不同 data_type 必须继续尝试"
    assert result.metadata.error_kind == "source_unavailable"
    assert "akshare_eastmoney:price_daily" in manager._circuit_breaker
    assert manager._circuit_breaker["akshare_eastmoney:dividends"]["failures"] == 1


def test_entry_deadline_returns_without_request_or_circuit_count() -> None:
    """验收 3：预算出生即耗尽 → 不发请求、不计熔断、error_kind=deadline。"""
    adapter = _ScriptedAdapter([_result([{"trade_date": "2026-11-12"}])])
    manager = _manager_with({"akshare_eastmoney": adapter})
    request = FetchRequest(
        data_type="price_daily",
        stock_codes=["600519"],
        extra_params={"deadline_monotonic": time.monotonic() - 1.0},
    )

    result = manager.fetch_with_sources(request, ["akshare_eastmoney"])

    assert adapter.calls == 0, "deadline 已耗尽时不得调用适配器"
    assert result.data == []
    assert result.metadata.error_kind == "deadline"
    assert result.metadata.error is None
    assert result.metadata.reason
    assert manager._circuit_breaker == {}


def test_all_circuits_tripped_warning_is_diagnosable(
    caplog: Any,
) -> None:
    """验收 4：全链熔断时报错串可诊断，且不含 None / "tried: []"。"""
    manager = _manager_with({
        "akshare_eastmoney": _ScriptedAdapter([_result([])], name="akshare_eastmoney"),
        "baostock": _ScriptedAdapter([_result([])], name="baostock"),
    })
    for name in ("akshare_eastmoney", "baostock"):
        for _ in range(5):
            manager._record_circuit_failure(name, "dividends")

    with caplog.at_level(logging.WARNING, logger="app.core.adapters.manager"):
        result = manager.fetch_with_sources(_dividend_request(), [
            "akshare_eastmoney", "baostock",
        ])

    message = result.metadata.error or ""
    assert result.metadata.error_kind == "source_unavailable"
    assert result.metadata.source == "akshare_eastmoney", "必须用首个候选源名，不许写 local_cache"
    assert "dividends" in message
    assert "akshare_eastmoney" in message and "baostock" in message
    assert "cooldown" in message
    assert "None" not in message
    assert "tried: []" not in message
    assert any("熔断" in record.getMessage() for record in caplog.records)


def test_legal_empty_is_logged_as_info_and_not_counted(
    caplog: Any,
) -> None:
    """全合法空 → INFO 日志，无 error，不计熔断。"""
    adapter = _ScriptedAdapter([
        _result(error=None, error_kind="legal_empty", reason="无已实施分红记录"),
    ])
    manager = _manager_with({"akshare_eastmoney": adapter})

    with caplog.at_level(logging.INFO, logger="app.core.adapters.manager"):
        result = manager.fetch_with_sources(_dividend_request(), ["akshare_eastmoney"])

    assert result.metadata.error_kind == "legal_empty"
    assert result.metadata.error is None
    assert "无已实施分红记录" in (result.metadata.reason or "")
    assert manager._circuit_breaker == {}
    legal_logs = [
        record.getMessage() for record in caplog.records if "合法缺失" in record.getMessage()
    ]
    assert legal_logs, caplog.text
    assert "data_type=dividends" in legal_logs[0]
    assert "None" not in legal_logs[0]


def test_source_unavailable_counts_and_success_resets() -> None:
    failure = _result(error="timeout", error_kind="source_unavailable")
    success = _result([{"trade_date": "2026-11-12"}])
    adapter = _ScriptedAdapter([failure, failure, failure, failure, success])
    manager = _manager_with({"akshare_eastmoney": adapter})

    for _ in range(4):
        manager.fetch_with_sources(_price_request(), ["akshare_eastmoney"])
    state = manager._circuit_breaker["akshare_eastmoney:price_daily"]
    assert state["failures"] == 4
    assert state["tripped_until"] is None

    result = manager.fetch_with_sources(_price_request(), ["akshare_eastmoney"])

    assert result.data
    state = manager._circuit_breaker["akshare_eastmoney:price_daily"]
    assert state["failures"] == 0
    assert state["tripped_until"] is None


def test_adapter_exception_counts_as_source_unavailable(caplog: Any) -> None:
    adapter = _ScriptedAdapter([RuntimeError("boom")])
    manager = _manager_with({"akshare_eastmoney": adapter})

    with caplog.at_level(logging.ERROR, logger="app.core.adapters.manager"):
        result = manager.fetch_with_sources(_price_request(), ["akshare_eastmoney"])

    assert result.metadata.error_kind == "source_unavailable"
    assert result.metadata.error is not None and "boom" in result.metadata.error
    assert manager._circuit_breaker["akshare_eastmoney:price_daily"]["failures"] == 1
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_truncated_partial_is_returned_and_flagged(caplog: Any) -> None:
    partial = _result(
        [{"announcement_id": "a1"}],
        source="cninfo",
        error=None,
        error_kind="truncated_ok",
        reason="CNINFO 公告被截断",
        truncated=True,
    )
    manager = _manager_with({"cninfo": _ScriptedAdapter([partial], name="cninfo")})

    with caplog.at_level(logging.WARNING, logger="app.core.adapters.manager"):
        result = manager.fetch_with_sources(
            FetchRequest(data_type="announcements", stock_codes=["000001"]),
            ["cninfo"],
        )

    assert result.data == partial.data
    assert result.metadata.error_kind == "truncated_ok"
    assert result.metadata.truncated is True
    assert result.metadata.error is None
    assert manager._circuit_breaker == {}
    assert any("partial" in record.getMessage() for record in caplog.records)


def test_close_clears_circuit_breaker() -> None:
    manager = _manager_with({"akshare_eastmoney": _ScriptedAdapter([_result([])])})
    for _ in range(5):
        manager._record_circuit_failure("akshare_eastmoney", "price_daily")
    assert manager._circuit_breaker

    manager.close()

    assert manager._circuit_breaker == {}
    assert manager._adapters == {}


def test_source_metadata_defaults_keep_old_constructors_working() -> None:
    metadata = SourceMetadata(
        source="tencent",
        fetch_time=datetime.now(UTC),
        raw_response_hash="0" * 64,
        confidence="strict",
        error="legacy error",
    )

    assert metadata.error_kind == "none"
    assert metadata.reason is None
    assert metadata.skipped_codes == {}
    assert metadata.missing_columns == []
    assert metadata.truncated is False


def test_unmigrated_error_with_empty_data_still_trips_circuit() -> None:
    """Lead 2026-11-12 裁决：未迁移适配器 error_kind="none" + error 非空 + 空数据 = 真源故障。

    这类结果必须计入熔断，否则 tdx/sina/ths 等真实源故障会被无限重捶。
    """
    legacy_failure = _result(error="boom")
    assert legacy_failure.metadata.error_kind == "none"
    adapter = _ScriptedAdapter([legacy_failure], name="tdx")
    manager = _manager_with({"tdx": adapter})

    results = [
        manager.fetch_with_sources(_price_request(), ["tdx"]) for _ in range(5)
    ]

    state = manager._circuit_breaker["tdx:price_daily"]
    assert state["failures"] == 5
    assert state["tripped_until"] is not None
    # 归一化为 source_unavailable，日志/T2 的 error_kind 路由保持一致
    assert all(result.metadata.error_kind == "source_unavailable" for result in results)
    assert all("boom" in (result.metadata.error or "") for result in results)


def test_declared_non_failure_kinds_never_trip_even_with_reason_or_error() -> None:
    """legal_empty / source_unsupported：无论 error/reason 是否为空都不计数。"""
    cases = (
        ("legal_empty", None),
        ("legal_empty", "legacy stray error"),
        ("source_unsupported", None),
        ("source_unsupported", "capability missing"),
    )
    for kind, error in cases:
        declared = _result(error=error, error_kind=kind, reason="declared reason")
        adapter = _ScriptedAdapter([declared], name="akshare_eastmoney")
        manager = _manager_with({"akshare_eastmoney": adapter})

        results = [
            manager.fetch_with_sources(_dividend_request(), ["akshare_eastmoney"])
            for _ in range(5)
        ]

        assert not manager._is_circuit_tripped("akshare_eastmoney", "dividends")
        assert (
            manager._circuit_breaker
            .get("akshare_eastmoney:dividends", {})
            .get("failures", 0)
        ) == 0, (kind, error)
        assert all(result.metadata.error is None for result in results), (kind, error)
