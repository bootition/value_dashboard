"""事故出处：reports/118 / 2026-11-13。

2026-11-13 P1（3）：自建 `AdapterManager()` 让熔断隔离失效。

`buyback.py` / `index_valuation.py` / `treasury.py` / `funding.py` / `capital.py` /
`business.py` / `hk_dividends.py` / `pdf/manager.py` 各自在构造时 `AdapterManager()`
→ 每次调用都是全新熔断状态，死源在这些域上永不熔断。

`update.py` 里凡是构造这些 updater 的地方都必须注入本更新进程共享的
`self.adapter_mgr`，让熔断桶（adapter:data_type）跨域共享同一份状态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.adapters.manager import AdapterManager
from app.core.index_valuation import IndexValuationUpdater
from app.core.update import IncrementalUpdater
from tests.support import source_guard

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


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


def test_default_adapter_mgr_is_single_instance(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """未注入时每个 updater 只创建**一份** manager，且该 updater 全分支共用它。

    2026-11-19 审计 W1：原断言是 `updater.adapter_mgr is updater.adapter_mgr`
    —— 对任何对象恒真（只要取属性不抛异常），信息量为零，却冒充"单实例"守卫。
    真正要钉的两条性质：

    1. **同一 updater 内**：三条互不相干的刷新分支下发给子 updater 的必须是
       **同一个**实例。写成 `AdapterManager()` 会让熔断桶按域分叉，死源永不熔断
       （reports/118 P1(3) 的原始事故）。
    2. **不同 updater 之间**必须是**不同**实例。若有人把 manager 改成进程级
       单例，一只票的源封禁会把全市场一起熔断 —— 第 2 条断言会红。
    """
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    sibling = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    manager = updater.adapter_mgr

    assert isinstance(manager, AdapterManager)
    # 同一 updater 多次取用必须是同一个实例（"每次访问都新建"→ 红）
    assert updater.adapter_mgr is manager

    # 三条不同刷新分支下发的必须是同一个实例（"某分支自建 AdapterManager"→ 红）
    injected: list[Any] = []
    for target, refresh in (
        ("app.core.buyback.BuybackUpdater", updater._refresh_buyback),
        ("app.core.capital.CapitalHistoryUpdater", updater._refresh_capital_history),
        ("app.core.business.BusinessOverviewUpdater", updater._refresh_business_overview),
    ):
        captured: dict[str, Any] = {}
        monkeypatch.setattr(target, _Recorder(captured))
        refresh()
        injected.append(captured.get("adapter"))
    assert [item is manager for item in injected] == [True, True, True], (
        f"三条刷新分支下发的 manager 必须是同一实例，实际 {[item is manager for item in injected]}"
    )

    # 不同 updater 不得共享熔断状态（"进程级单例"→ 红）
    assert sibling.adapter_mgr is not manager, (
        "两个 updater 共用了同一个 AdapterManager = 熔断状态全局串台"
        "（事故：一只票的源封禁把全市场一起熔断）"
    )


def test_retry_path_updater_constructions_inject_shared_manager(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """重试路径里的四处构造也必须注入共享 manager。

    行为断言（不再是 `inspect.getsource(_retry_failed_tasks)` 里搜
    `adapter=self.adapter_mgr` 文本）：真跑一遍 `_retry_failed_tasks`，
    四处构造全部被记录替身接住，逐个检查 kwargs 里的 `adapter` 是不是
    **同一个** 共享实例。自建 `AdapterManager()` 会让熔断状态分叉 → 变红。
    """
    updater, shared = _updater(duckdb_store, sqlite_store)
    targets = {
        "company_profile": "app.core.business.BusinessOverviewUpdater",
        "share_capital_history": "app.core.capital.CapitalHistoryUpdater",
        "treasury_yield_curve": "app.core.treasury.TreasuryCurveUpdater",
        "ipo_funding": "app.core.funding.FundingUpdater",
    }
    records = {
        data_type: source_guard.constructed_with(monkeypatch, target, label=data_type)
        for data_type, target in targets.items()
    }
    tasks = [
        {"id": index + 1, "stock_code": "600519", "data_type": data_type}
        for index, data_type in enumerate(targets)
    ]

    updater._retry_failed_tasks(tasks)

    for record in records.values():
        record.assert_count(1)
        record.assert_injected("adapter", shared)


def test_index_valuation_updater_has_no_adapter_injection_point(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """已知缺口（不在本文件归属内）：IndexValuationUpdater 自建 3 个专用适配器。

    它刻意"不经 AdapterManager 以便多源并存"（乐咕/中证/申万各自限速），因此
    update.py 无法注入共享 manager，也就是说该域**没有熔断保护**。若将来
    `IndexValuationUpdater.__init__` 增加 `adapter`/`adapter_mgr` 参数，本断言
    会失败——届时请把构造点改为注入 `self.adapter_mgr`。

    断言方式：参数契约用 `inspect.signature`；两处运行期构造用记录替身接住，
    检查"只传 duck/sqlite、没传 adapter"；构造点**总数**用 AST 跨模块统计
    （不再是 `inspect.getsource(update_module).count(...)`，那种写法既绑死
    "构造点必须留在 update.py"，也看不到刷在别的模块里的新构造点）。
    """
    parameters = source_guard.signature_of(IndexValuationUpdater.__init__)

    assert "adapter" not in parameters
    assert "adapter_mgr" not in parameters

    updater, _shared = _updater(duckdb_store, sqlite_store)
    record = source_guard.constructed_with(
        monkeypatch, "app.core.index_valuation.IndexValuationUpdater",
    )

    updater._refresh_index_valuation()
    updater._retry_failed_tasks(
        [{"id": 1, "stock_code": "000300", "data_type": "index_valuation"}],
    )

    record.assert_count(2)
    record.assert_not_passing("adapter")
    record.assert_not_passing("adapter_mgr")
    for index, kwargs in enumerate(record.calls):
        assert set(kwargs) == {"duck", "sqlite"}, (
            f"第 {index + 1} 处 IndexValuationUpdater 构造的入参变了: {sorted(kwargs)}"
        )

    sites = source_guard.count_construction_sites(APP_ROOT, "IndexValuationUpdater")
    assert sites == 3, (
        f"全仓 IndexValuationUpdater 构造点 {sites} != 3（update.py 两处 + cli 一处）："
        "新增构造点请回来复核是否需要注入共享 adapter_mgr"
    )
