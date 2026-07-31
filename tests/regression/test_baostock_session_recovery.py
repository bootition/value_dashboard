from __future__ import annotations

import pytest

from app.core.adapters.baostock_adapter import BaoStockAdapter
from app.core.adapters.base import FetchRequest


class _Result:
    def __init__(self, error_code: str, error_msg: str, rows: list[list[str]] | None = None) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self._rows = rows or []
        self.fields = ["date", "code", "close"] if rows is not None else None

    def next(self) -> bool:
        if self._rows:
            self._current = self._rows.pop(0)
            return True
        return False

    def get_row_data(self) -> list[str]:
        return getattr(self, "_current", [])


def test_session_expiry_triggers_reconnect_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"login": 0, "logout": 0, "query": 0}

    class _LoginResult:
        error_code = "0"
        error_msg = "success"

    import baostock as bs

    def fake_login():
        calls["login"] += 1
        return _LoginResult()

    def fake_logout():
        calls["logout"] += 1

    def fake_query(code, fields, start_date=None, end_date=None, frequency=None, adjustflag=None):
        calls["query"] += 1
        if calls["query"] == 1:
            return _Result("10002001", "用户未登录")
        return _Result("0", "success", [["2026-07-31", "10.0", "10.5", "10.1", "10.2", "1000", "10000", "0.5"]])

    monkeypatch.setattr(bs, "login", fake_login)
    monkeypatch.setattr(bs, "logout", fake_logout)
    monkeypatch.setattr(bs, "query_history_k_data_plus", fake_query)

    adapter = BaoStockAdapter(rate_limit=0, reuse_session=True)
    result = adapter.fetch(
        FetchRequest(data_type="price_daily", stock_codes=["600519"], adjust="raw")
    )

    assert result.metadata.error is None
    assert len(result.data) == 1
    assert result.data[0]["stock_code"] == "600519"
    # one failed attempt + one retry, with a reconnect login in between
    assert calls["query"] == 2
    assert calls["login"] == 2  # initial + reconnect
    assert calls["logout"] >= 1


def test_non_session_error_does_not_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"login": 0}

    class _LoginResult:
        error_code = "0"
        error_msg = "success"

    import baostock as bs

    def fake_login():
        calls["login"] += 1
        return _LoginResult()

    def fake_query(*args, **kwargs):
        return _Result("10002002", "数据不存在")

    monkeypatch.setattr(bs, "login", fake_login)
    monkeypatch.setattr(bs, "query_history_k_data_plus", fake_query)

    adapter = BaoStockAdapter(rate_limit=0, reuse_session=True)
    result = adapter.fetch(
        FetchRequest(data_type="price_daily", stock_codes=["600519"], adjust="raw")
    )

    assert result.metadata.error is None
    assert b"ERROR sh.600519" in (result.raw_response or b"")
    assert calls["login"] == 1  # no reconnect for non-session errors
