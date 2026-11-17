"""事故出处：reports/118 / 2026-11-13。

2026-11-13 T1 回归：熔断降级语义（中性结果 / 疑似风控空表升级）。

背景（Lead 复审裁决）：
- P1-a：legal_empty / per_symbol_data_error / truncated_ok 必须**不增不减**
  熔断计数；只有"真正拿到数据且无 error"才清零，否则风控后的死源会持续
  返回空表/截断/个股格式错，把失败计数清零 → 永不熔断。
- P1-b：>=10 只股票的批量请求整批 0 行 + legal_empty 视为疑似风控空表，
  升级为 source_unavailable（宁可多试一次，也不永久写 missing）。

全部使用脚本化假适配器，不联网、不访问数据库。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.manager import (
    _SUSPICIOUS_EMPTY_BATCH_THRESHOLD,
    AdapterManager,
)


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


def _dividend_request(count: int = 1) -> FetchRequest:
    codes = [f"{600000 + index:06d}" for index in range(count)]
    return FetchRequest(data_type="dividends", stock_codes=codes)


def _legal_empty(reason: str | None = "无已实施分红记录（合法空）") -> FetchResult:
    return _result(error=None, error_kind="legal_empty", reason=reason)


# ─── P1-a：中性结果不增不减 ─────────────────────────────────────────


def test_legal_empty_never_increments_or_resets_circuit() -> None:
    """legal_empty（带/不带 reason）连续 5 次：失败计数不增不减、不 trip。"""
    adapter = _ScriptedAdapter([
        _legal_empty("无已实施分红记录（合法空）"),
        _legal_empty("无已实施分红记录（合法空）"),
        _legal_empty(None),
        _legal_empty(None),
        _legal_empty("窗口内无公告"),
    ])
    manager = _manager_with({"akshare_eastmoney": adapter})
    key = "akshare_eastmoney:dividends"
    # 预先制造 2 次真失败，验证 legal_empty 既不加也不清零
    manager._record_circuit_failure("akshare_eastmoney", "dividends")
    manager._record_circuit_failure("akshare_eastmoney", "dividends")
    assert manager._circuit_breaker[key]["failures"] == 2

    results = [manager.fetch_with_sources(_dividend_request(1), ["akshare_eastmoney"])
               for _ in range(5)]

    assert manager._circuit_breaker[key]["failures"] == 2, "legal_empty 不得增删失败计数"
    assert manager._circuit_breaker[key]["tripped_until"] is None
    assert not manager._is_circuit_tripped("akshare_eastmoney", "dividends")
    assert all(result.metadata.error_kind == "legal_empty" for result in results)

    # 全新管理器：连续 5 次 legal_empty 仍不得熔断
    fresh = _manager_with({
        "akshare_eastmoney": _ScriptedAdapter([_legal_empty(None)], name="akshare_eastmoney"),
    })
    for _ in range(5):
        fresh.fetch_with_sources(_dividend_request(1), ["akshare_eastmoney"])
    assert not fresh._is_circuit_tripped("akshare_eastmoney", "dividends")
    assert fresh._circuit_breaker.get(key, {}).get("failures", 0) == 0


def test_per_symbol_and_truncated_are_neutral() -> None:
    """per_symbol_data_error / truncated_ok 同样不增不减。"""
    per_symbol = _result(
        error="missing ex_date", error_kind="per_symbol_data_error",
        reason="missing ex_date",
    )
    per_symbol_manager = _manager_with({
        "akshare_eastmoney": _ScriptedAdapter([per_symbol], name="akshare_eastmoney"),
    })
    key = "akshare_eastmoney:dividends"
    per_symbol_manager._record_circuit_failure("akshare_eastmoney", "dividends")
    per_symbol_manager._record_circuit_failure("akshare_eastmoney", "dividends")
    for _ in range(5):
        per_symbol_manager.fetch_with_sources(_dividend_request(1), ["akshare_eastmoney"])
    assert per_symbol_manager._circuit_breaker[key]["failures"] == 2
    assert not per_symbol_manager._is_circuit_tripped("akshare_eastmoney", "dividends")

    truncated = _result(
        [{"announcement_id": "a1"}],
        source="cninfo",
        error=None,
        error_kind="truncated_ok",
        reason="CNINFO 公告被截断",
        truncated=True,
    )
    truncated_manager = _manager_with({
        "cninfo": _ScriptedAdapter([truncated], name="cninfo"),
    })
    truncated_key = "cninfo:announcements"
    truncated_manager._record_circuit_failure("cninfo", "announcements")
    truncated_manager._record_circuit_failure("cninfo", "announcements")
    for _ in range(5):
        truncated_manager.fetch_with_sources(
            FetchRequest(data_type="announcements", stock_codes=["000001"]),
            ["cninfo"],
        )
    assert truncated_manager._circuit_breaker[truncated_key]["failures"] == 2
    assert not truncated_manager._is_circuit_tripped("cninfo", "announcements")


def test_true_success_resets_failure_counter() -> None:
    """只有拿到数据且无 error 才清零。"""
    success = _result([{"stock_code": "600519", "ex_date": "2026-06-26"}])
    manager = _manager_with({
        "akshare_eastmoney": _ScriptedAdapter([success], name="akshare_eastmoney"),
    })
    manager._record_circuit_failure("akshare_eastmoney", "dividends")
    manager._record_circuit_failure("akshare_eastmoney", "dividends")

    result = manager.fetch_with_sources(_dividend_request(1), ["akshare_eastmoney"])

    assert result.data == success.data
    state = manager._circuit_breaker["akshare_eastmoney:dividends"]
    assert state["failures"] == 0
    assert state["tripped_until"] is None


# ─── P1-b：疑似风控空表升级 ─────────────────────────────────────────


def test_large_empty_batch_is_upgraded_to_source_unavailable() -> None:
    """批 50 全空 + legal_empty → source_unavailable + reason 标记。"""
    adapter = _ScriptedAdapter([_legal_empty("无已实施分红记录（合法空）")])
    manager = _manager_with({"akshare_eastmoney": adapter})

    result = manager.fetch_with_sources(_dividend_request(50), ["akshare_eastmoney"])

    assert result.data == []
    assert result.metadata.error_kind == "source_unavailable"
    assert result.metadata.error is not None
    marker = "batch_suspicious_empty: requested=50, rows=0"
    assert marker in (result.metadata.reason or "")
    assert marker in (result.metadata.error or "")
    assert "无已实施分红记录" in (result.metadata.reason or "")


def test_single_code_and_small_batch_empty_stay_legal_empty() -> None:
    """单票 / <10 只批量：真实合法空（新股无分红）保持不变。"""
    for count in (1, _SUSPICIOUS_EMPTY_BATCH_THRESHOLD - 1):
        manager = _manager_with({
            "akshare_eastmoney": _ScriptedAdapter(
                [_legal_empty("无已实施分红记录（合法空）")],
                name="akshare_eastmoney",
            ),
        })
        result = manager.fetch_with_sources(
            _dividend_request(count), ["akshare_eastmoney"],
        )
        assert result.metadata.error_kind == "legal_empty", count
        assert result.metadata.error is None, count
        assert "batch_suspicious_empty" not in (result.metadata.reason or ""), count


def test_large_batch_with_one_row_is_not_upgraded() -> None:
    """批 50 有 1 行 → 不升级（仍然返回数据）。"""
    adapter = _ScriptedAdapter([
        _result([{"stock_code": "600519", "ex_date": "2026-06-26"}], error_kind="legal_empty"),
    ])
    manager = _manager_with({"akshare_eastmoney": adapter})

    result = manager.fetch_with_sources(_dividend_request(50), ["akshare_eastmoney"])

    assert len(result.data) == 1
    assert result.metadata.error_kind == "legal_empty"
    assert "batch_suspicious_empty" not in (result.metadata.reason or "")


def test_upgraded_suspicious_empty_counts_toward_circuit() -> None:
    """升级后 5 次即触发熔断（且同批多个源各自计数）。"""
    manager = _manager_with({
        "akshare_eastmoney": _ScriptedAdapter(
            [_legal_empty("无已实施分红记录")], name="akshare_eastmoney",
        ),
        "baostock": _ScriptedAdapter(
            [_legal_empty("无已实施分红记录")], name="baostock",
        ),
    })

    for _ in range(5):
        manager.fetch_with_sources(
            _dividend_request(50), ["akshare_eastmoney", "baostock"],
        )

    for key in ("akshare_eastmoney:dividends", "baostock:dividends"):
        state = manager._circuit_breaker[key]
        assert state["failures"] == 5, key
        assert state["tripped_until"] is not None, key
    assert manager._is_circuit_tripped("akshare_eastmoney", "dividends")
    assert manager._is_circuit_tripped("baostock", "dividends")
