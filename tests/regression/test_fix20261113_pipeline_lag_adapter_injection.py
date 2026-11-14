"""2026-11-13 P1（3）：自建 `AdapterManager()` 让熔断隔离失效。

`buyback.py` / `index_valuation.py` / `treasury.py` / `funding.py` / `capital.py` /
`business.py` / `hk_dividends.py` / `pdf/manager.py` 各自在构造时 `AdapterManager()`
→ 每次调用都是全新熔断状态，死源在这些域上永不熔断。

`update.py` 里凡是构造这些 updater 的地方都必须注入本更新进程共享的
`self.adapter_mgr`，让熔断桶（adapter:data_type）跨域共享同一份状态。
"""

from __future__ import annotations

import inspect
from typing import Any

import app.core.update as update_module
from app.core.adapters.manager import AdapterManager
from app.core.index_valuation import IndexValuationUpdater
from app.core.update import IncrementalUpdater


def _updater(duckdb_store, sqlite_store) -> tuple[IncrementalUpdater, AdapterManager]:
    shared = AdapterManager()
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=shared,
    )
    return updater, shared


class _Recorder:
    """Records constructor kwargs; every updater method returns success."""

    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    def __call__(self, **kwargs: Any) -> _Recorder:
        self._sink.update(kwargs)
        return self

    def refresh_all(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def refresh_if_due(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": "test_double"}

    def update_all(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": "test_double"}


def test_buyback_updater_receives_shared_adapter_manager(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater, shared = _updater(duckdb_store, sqlite_store)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("app.core.buyback.BuybackUpdater", _Recorder(captured))

    updater._refresh_buyback()

    assert captured.get("adapter") is shared, "回购域必须复用共享熔断状态"


def test_funding_updater_receives_shared_adapter_manager(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater, shared = _updater(duckdb_store, sqlite_store)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("app.core.funding.FundingUpdater", _Recorder(captured))

    updater._refresh_funding()

    assert captured.get("adapter") is shared


def test_capital_updater_receives_shared_adapter_manager(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater, shared = _updater(duckdb_store, sqlite_store)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("app.core.capital.CapitalHistoryUpdater", _Recorder(captured))

    updater._refresh_capital_history()

    assert captured.get("adapter") is shared


def test_business_overview_updater_receives_shared_adapter_manager(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater, shared = _updater(duckdb_store, sqlite_store)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("app.core.business.BusinessOverviewUpdater", _Recorder(captured))

    updater._refresh_business_overview()

    assert captured.get("adapter") is shared


def test_treasury_updater_receives_shared_adapter_manager(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    updater, shared = _updater(duckdb_store, sqlite_store)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("app.core.treasury.TreasuryCurveUpdater", _Recorder(captured))

    updater._refresh_treasury_curve()

    assert captured.get("adapter") is shared


def test_default_adapter_mgr_is_single_instance(duckdb_store, sqlite_store) -> None:
    """未注入时必须只创建一份 manager，且同一 updater 复用同一实例。"""
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)

    assert isinstance(updater.adapter_mgr, AdapterManager)
    assert updater.adapter_mgr is updater.adapter_mgr


def test_retry_path_updater_constructions_inject_shared_manager() -> None:
    """重试路径（_retry_failed_tasks）里的四处构造也必须注入共享 manager。"""
    source = inspect.getsource(IncrementalUpdater._retry_failed_tasks)
    for name in (
        "BusinessOverviewUpdater", "CapitalHistoryUpdater",
        "TreasuryCurveUpdater", "FundingUpdater",
    ):
        index = source.index(f"{name}(")
        window = source[index:index + 200]
        assert "adapter=self.adapter_mgr" in window, f"{name} 未注入共享 adapter_mgr"


def test_index_valuation_updater_has_no_adapter_injection_point() -> None:
    """已知缺口（不在本文件归属内）：IndexValuationUpdater 自建 3 个专用适配器。

    它刻意"不经 AdapterManager 以便多源并存"（乐咕/中证/申万各自限速），因此
    update.py 无法注入共享 manager，也就是说该域**没有熔断保护**。若将来
    `IndexValuationUpdater.__init__` 增加 `adapter`/`adapter_mgr` 参数，本断言
    会失败——届时请把 update.py 的两处构造改为注入 `self.adapter_mgr`。
    """
    parameters = inspect.signature(IndexValuationUpdater.__init__).parameters

    assert "adapter" not in parameters
    assert "adapter_mgr" not in parameters
    source = inspect.getsource(update_module)
    assert source.count("IndexValuationUpdater(") == 2, "新增构造点请同步复核注入"
