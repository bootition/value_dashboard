from __future__ import annotations

import pytest

from app.core.adapters.base import FetchRequest
from app.core.adapters.tdx_adapter import TDXAdapter


def test_bars_session_does_not_yield_twice_when_body_raises(monkeypatch) -> None:
    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    adapter = TDXAdapter(rate_limit=0)
    monkeypatch.setattr(adapter, "_connect_for_bars", lambda request: client)
    request = FetchRequest(data_type="price_daily", stock_codes=["600519"])

    with pytest.raises(RuntimeError, match="body failed"), adapter._bars_session(request):
        raise RuntimeError("body failed")

    assert client.closed is True
