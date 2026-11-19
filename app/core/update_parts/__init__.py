"""`IncrementalUpdater`（god class）的拆分落点包（reports/140 §2.5）。

## 为什么有这个包

`app/core/update.py::IncrementalUpdater` 曾有 6,600+ 行 / 137 个方法，是全项目认知成本
最高的一堵墙。一次拆完＝必然出事，所以按"内聚、低耦合、有测试覆盖"的粒度一块一块搬：
每个模块搬走**一个关注点**，`update.py` 里只留薄薄的导入别名或委托方法。

## 铁律（本包每个模块都适用）

1. **不许反向依赖**：本包任何模块**禁止** `from app.core.update import ...` ——
   `update.py` 会 import 本包，反向 import 立刻构成循环。需要状态就显式传参
   （或传一个小 dataclass），不要去摸 `IncrementalUpdater` 的实例。
2. **行为零变更**：搬移**不改** SQL、**不改**超时/退避参数、**不改**日志文案、
   **不改**异常类型。搬完后 `update.py` 里原名保留为导入别名或委托方法，
   既有外部调用点与既有测试断言一行都不用动。
3. **一步一绿**：每搬一块立刻跑定向回归，绿了才搬下一块；不绿就用备份回滚这一块。

## 现有模块

- ``prices`` — ``PriceReplaceBuffer``：全历史价格替换的攒批缓冲
  （原 ``app/core/update.py::_PriceReplaceBuffer``）。
- ``retry`` — ``retry_list`` 的读写、退避排期与结算（``mark_retry_failed`` /
  ``check_retry_tasks`` / ``settle_non_retryable_*`` / ``cleanup_unretryable_tasks``）。
- ``retry_tasks`` — ``_retry_failed_tasks`` 的重试编排（306 行按 ``data_type``
  分派到各域 updater）；``update.py`` 只留同名薄委托。
"""
