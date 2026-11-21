"""**已作废的 no-op**：`idx_share_capital_history_stock` 自 schema v50 起已不存在。

历史（2026-08-14 红队 F1，见 reports/81 §1）：`share_capital_history` 上那条
(``stock_code``, ``effective_date``) 的 ART 索引与堆失步，DELETE 抛
``FatalException: Failed to delete all rows from index. Only deleted 0 out of 38 rows``
并让连接失效，整轮自动更新死在重试步。当时的修法是"DROP INDEX + CREATE INDEX 重建它"。

为什么现在改成 no-op（2026-11-21，schema v50 + 独立审查缺陷 2）：

1. **这条索引已经不在了**。v50（reports/151 逐索引 A/B 实测）把全部 30 个二级索引
   （含 ``idx_share_capital_history_stock``，它的列与主键完全相同）都删了；
   ``DROPPED_SECONDARY_INDEXES`` 是唯一清单，建库 DDL 里也不再有 ``CREATE INDEX``。
   跑旧脚本唯一的效果是**把 v50 亲手删掉的索引又建回来**，让"索引与堆失步"这个
   整类 FATAL 重新具备发生条件。
2. **它用的正是已登记的 D2 危险写法**。旧脚本先 ``DROP INDEX`` 再 ``CREATE INDEX``，
   两者落在同一个写事务里 —— 而这正是 DuckDB 1.5.5
   ``InternalException: BoundIndex::CreateDeltaIndex is not supported for this index type``
   （连接被 invalidate）的触发条件。文档里写着"必须分两个独立事务"，代码里却用
   ``with store.transaction()`` 包住两步，自相矛盾。

所以本脚本**不再触碰任何数据库**（连只读连接都不开），只打印"无需修复"并退出 0。
保留文件而不是删掉，是因为 reports/81 与知识库 D2 引用了这个路径；改成 no-op 后
"跑到它就是误操作"这句话仍然可查、可执行。

如果将来真的需要给某张表重建索引，正确做法写在 app/core/storage/duckdb_store.py 的
``reindex_table()``：**DROP 与 CREATE 逐条自动提交**（不同事务），且只在
``duckdb_indexes()`` 里真有该索引时才重建。但按 v50 的判决：**默认不要建**。

Usage（现在跑只会打印 no-op；不需要停服务，也不会打开库）：
    .venv\\Scripts\\python.exe scripts\\repair_share_capital_history_index.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 旧脚本重建的那条索引（保留常量：报告/知识库按名字引用它，且它仍在
#: ``DROPPED_SECONDARY_INDEXES`` 清单里，重建它 == 撤销 v50 判决）。
INDEX_NAME = "idx_share_capital_history_stock"

REASON = (
    "schema v50 起 DuckDB 库里已无任何二级索引（DROPPED_SECONDARY_INDEXES/DROPPED 全清单见 "
    "app/core/storage/schema.py）：该索引的列与真实查询形状不匹配，"
    "且重建它会在同一事务里 DROP+CREATE，正是知识库 D2 的 FATAL 写法。"
)


def repair() -> dict:
    """no-op：只回报状态，不打开数据库、不发任何 DDL。"""
    return {
        "script": "repair_share_capital_history_index.py",
        "status": "noop",
        "index": INDEX_NAME,
        "action_taken": None,
        "database_touched": False,
        "reason": REASON,
    }


def main() -> int:
    result = repair()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
