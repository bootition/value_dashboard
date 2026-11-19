"""`_update_prices_incremental` 搬家后的行为守卫（reports/140 §2.5 第 4 块）。

## 这个文件钉的是什么

`IncrementalUpdater._update_prices_incremental`（502 行）已切成三段搬进
`app/core/update_parts/prices_step.py`：

* `_plan_price_scope` —— 选谁（目标集 SQL、增量窗口、除权重拉集合）；
* `_run_price_pipeline` —— 怎么抓 / 怎么攒 / 什么时候落盘；
* `_finalize_price_step` —— 怎么结算（qfq 滞后降级、待重算名单、步骤报告）。

`update.py` 只留同名的三行委托。这里用**行为级**断言（不读源码文本）钉住三件事：

1. `update.py` 里的同名方法必须还是"薄委托 → 模块函数"，不许把逻辑搬回 god class
   （`source_guard.resolve_body` 顺委托壳追一层，拿到真正的方法体）；
2. 搬出去的实现**不许**再拿 `self` 当入参：状态一律走显式 `owner`，这样
   `update_parts` 永远不需要 `from app.core.update import ...`（循环依赖）；
3. 两个 `skipped` 早退（`no_stock_meta` / `prices_up_to_date`）的**字面值**与
   "原样透传给调用方"的语义不变——它们被 `_run_incremental_update_flow` 直接
   `report_step("prices", ...)`，改了文案就等于改了 job_logs 里的步骤状态。

全流程的真跑由既有回归覆盖（`test_incremental_price_lineage.py`、
`test_price_batch_flush.py`、`test_price_full_history_batch.py`、
`test_qfq_refetch_pending.py`、`test_qfq_recompute_trigger.py`）；本文件只补
"搬家接缝"这一层，两个早退路径因此不走真库。
"""

from __future__ import annotations

import logging
import time

from app.core.update import IncrementalUpdater
from app.core.update_parts import prices_step
from tests.support import source_guard

LOGGER = logging.getLogger("tests.price_step_delegate")


class _DuckStub:
    """只回答"目标集 SQL"一个问题的 DuckDB 替身。"""

    def __init__(self, rows: list[dict] | None, *, boom: bool) -> None:
        self._rows = rows
        self._boom = boom
        self.queries: list[tuple[str, list]] = []

    def read_query(self, sql: str, params: list | None = None) -> list[dict]:
        self.queries.append((sql, list(params or ())))
        if self._boom:
            raise RuntimeError("stock_meta 不可用")
        return list(self._rows or ())


class _SqliteStub:
    def query(self, *_args, **_kwargs) -> list[dict]:
        return []


class _OwnerStub:
    """`prices_step` 需要的 owner 面（鸭子类型，不继承 IncrementalUpdater）。"""

    def __init__(self, rows: list[dict] | None = None, *, boom: bool = False) -> None:
        self.duck = _DuckStub(rows, boom=boom)
        self.sqlite = _SqliteStub()
        self.price_full_refetch_on_stale = False
        self.incremental_window_days = 30

    def _get_latest_local_price_date(self) -> None:
        return None

    def _latest_expected_trading_date(self, today: str) -> str:
        return today

    def _load_qfq_refetch_pending(self) -> list[str]:
        return []


def _run(owner: _OwnerStub) -> dict:
    return prices_step.update_prices_incremental(
        owner,
        0,
        logger=LOGGER,
        source_error_kind=lambda _result: "source_unavailable",
        clock=time.monotonic,
    )


def test_update_py_method_is_a_thin_delegate_to_the_moved_module() -> None:
    """方法体必须还在 `update_parts/prices_step.py`，不能被搬回 god class。"""
    body = source_guard.resolve_body(IncrementalUpdater._update_prices_incremental)

    assert body is prices_step.update_prices_incremental, (
        "_update_prices_incremental 不再是『同名委托 → update_parts.prices_step』："
        "要么 502 行逻辑被搬回了 update.py（god class 反弹），要么委托被改坏"
    )


def test_moved_function_takes_an_explicit_owner_not_self() -> None:
    """搬出去的实现不许把 god class 实例当 `self` 用（那是反向耦合的入口）。"""
    params = source_guard.signature_of(prices_step.update_prices_incremental)

    assert "self" not in params, (
        "prices_step.update_prices_incremental 仍在用 `self` 当入参："
        "状态必须显式走 `owner`，否则 update_parts 迟早要 import app.core.update"
    )
    assert list(params)[:2] == ["owner", "max_stocks"], (
        f"参数契约变了（{list(params)}）：owner + max_stocks 是搬移前的"
        "self + max_stocks，顺序不能动"
    )


def test_missing_stock_meta_still_returns_the_same_skipped_report() -> None:
    """目标集 SQL 失败时的 `skipped/no_stock_meta` 早退文案与来源不变。"""
    owner = _OwnerStub(boom=True)

    assert _run(owner) == {"status": "skipped", "reason": "no_stock_meta"}
    assert owner.duck.queries, "目标集查询根本没发生：选股段被改道了"


def test_up_to_date_prices_still_return_the_same_skipped_report() -> None:
    """没有待更新股票时的 `skipped/prices_up_to_date` 早退文案不变。"""
    assert _run(_OwnerStub(rows=[])) == {
        "status": "skipped",
        "reason": "prices_up_to_date",
    }
