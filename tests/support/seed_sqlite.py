"""SQLite 侧种子工厂：按域播种**操作型**表（与 DuckDB 版同原则、两套实现）。

## 为什么要有这个模块（`reports/140` §17.7 第 1 条）

`tests/support/seed.py` 把 DuckDB 分析库的内联播种收敛成按域方法之后，SQLite
操作库那一半仍是内联 SQL，而且它的样板与 DuckDB 侧**不是同一批**：

1. **请求唯一键五件套**：`retry_list` 由 `uq_retry_list_request`（
   `stock_code, data_type, adapter, extra_json`，`app/core/storage/schema.py`
   v10 迁移建的）去重。抄这行时最容易漏的恰恰是 `extra_json`：SQLite 的唯一索引
   把 NULL 当作**互不相等**，于是"漏写 `extra_json`"不会报错，只会让"同一条重试"
   静默变成两条 —— 后续断言数 `COUNT(*)` 的用例从此假绿。
2. **状态脚手架**：`job_logs`（`status='running'` + `started_at` + `details_json`）、
   `auto_update_state`（`id=1` 单行 + `state/paused/current_stage`）、
   `trading_dates`（一行一个交易日）在用例里复制 N 遍，值与表口径无关。

本模块只收敛这两类。

## 为什么与 DuckDB 版分开，而不是合并成一个跨库万能口

1. **`INSERT OR REPLACE` 在两边不是同一件事。** SQLite 的 REPLACE 是"删掉冲突行
   再插新行"：本次没提供的列回到 schema 默认值（多数是 NULL）；DuckDB 的
   `INSERT OR REPLACE` 是 upsert，只改本次提供的列
   （`tests/regression/test_seed_factory.py` 的不变式 3 钉的就是这个粒度）。
   一个"跨库 `overwrite=True`"必然在一边**静默改语义**：SQLite 侧会把
   `selling_expenses` 抹成 NULL 而调用点毫无察觉。
2. **去重键不在同一层，形状还刁钻。** DuckDB 的主键写在 `CREATE TABLE ... PRIMARY
   KEY` 里；SQLite 侧最关键的两张队列表靠**索引**去重：`uq_retry_list_request`
   把 `extra_json` 也算进键，`manual_overrides` 是**部分唯一索引**
   （`WHERE status = 'published' AND rolled_back_at IS NULL`）—— 也就是
   "active 行可以重复、published 行不许重复"。跨库口子既不知道表，也不可能知道
   "这个唯一索引只覆盖某个 status"，只能退化成 `insert_row(table, **kw)`：
   列名拼错 → 静默插一行全 NULL 种子 → 被测逻辑走"缺失"分支 → 用例照绿。
3. **列校验手段不同。** DuckDB 用 `DESCRIBE`，SQLite 用 `PRAGMA table_info`；
   SQLite 还多 INTEGER 布尔（`paused`/`enabled`）与 TEXT 时间戳。
   合成一个类，`_columns_of()` 里就长出 `if isinstance(store, DuckDBStore)` 分支
   —— 两个实现挤进一个类，谁都不再是"某一个库的口径"。
4. **构造参数不同。** DuckDB 工厂只拿 `DuckDBStore`，本工厂只拿 `SQLiteStore`。
   要共用就得两边都可选（`Seed(duck=None, sqlite=None)`），于是每个测试都要构造
   一个自己根本不用的库，或者到处 `if seed.duck is not None:` —— 这正是万能口的入口。
5. **共享的是设计原则，不是代码。** 按域分方法、用真实 schema 校验列名、
   重复默认抛、默认值照生产口径且确定性：原则共用 ≠ 实现合并。硬合并还会让
   DuckDB 侧凭空背一条不属于它的规则（"published 行唯一"只有 SQLite 有）。

## 边界：什么该进工厂、什么必须留在测试里

（与 DuckDB 版同一把尺子）

* **该进工厂（"怎么放进去"）**：表的必填口径与枚举默认值（`retry_list` 的
  `retry_count=0 / max_retries=5 / error_kind='unknown' / extra_json='{}'`；
  `job_logs.status='running'`；`manual_overrides.status='active'`）、
  单行表的形状（`auto_update_state.id = 1`）、序列类表的批量写入
  （`trading_dates` 收 `[(trade_date, …)]`）。
* **必须留在测试里（"放什么进去"）**：**数值本身**（`retry_count=2`、
  `max_retries=2` 是 `retry_exhausted` 用例的全部意义）、**制造错误的方式**、
  以及下面三类（硬要求「不该收敛的要留」）：需要 `cursor.lastrowid` 回捞 id 的行、
  依赖 SQL `CURRENT_TIMESTAMP` 的行、故意造错的行（例如"维护锁下必须写不进去"）。

## 默认值口径（不能随便编）

* `job_logs.status = 'running'`（生产 `IncrementalUpdater._start_job` 的写入值）、
  `details_json = '{}'`（生产每次都写 JSON，从不留 NULL —— 留 NULL 会让状态页
  的 `json.loads()` 崩在种子上）；
* `retry_list`: `retry_count=0`、`extra_json='{}'`（生产 `_record_retry` 逐条都写
  `0` 与 `'{}'`），`max_retries=5`、`error_kind='unknown'`（schema 默认值，
  `error_kind` 见 2026-11-12 契约 §5）；
* `watchlist.group_name = 'default'`（schema 默认值）；
* `manual_overrides.status = 'active'`（schema 默认值，枚举 active/rolled_back/published）；
* `auto_update_state`: `id=1`（`CHECK (id = 1)` 单行表）、`state='enabled'`、
  `paused=0`、`current_stage='idle'`（生产 `AutoUpdateController.__init__` 的初始值）；
* 时间戳列（`started_at` / `added_at` / `created_at` / `updated_at`）用**固定字面量**
  :data:`DEFAULT_SEED_TIME`，形状与 schema 的 `DEFAULT CURRENT_TIMESTAMP` 一致
  （`YYYY-MM-DD HH:MM:SS`），不用 `now()`：同一测试重复跑得到同一份种子
  （与 DuckDB 版 `DEFAULT_FETCH_TIME` 同一取舍）；
* **业务列没有默认值**：`error` / `report_date` / `original_value` / `finished_at` …
  不写就是 NULL。「缺 x → NULL 不得估算」那类用例的前提就是这里真的缺着。

## 为什么不是 `insert_sqlite_row(table, **kwargs)`

万能口只是把 SQL 换个地方写：它不知道表的主键，也不知道哪条**索引**才是去重键，
于是列名拼错（`grup_name=`）会静默插进一行全 NULL 的种子。本模块每个方法都绑定
**一张表 + 该表的领域语义**，并且：

1. 列名先对照 `PRAGMA table_info(<表>)` 校验（不维护第二份列清单：schema 加列
   不会让工厂过期）：拼错 → :class:`UnknownSqliteColumn`，当场红，且**一行都不落**；
2. 主键/唯一索引冲突 → :class:`DuplicateSqliteSeed`（除非显式 `overwrite=True`），
   不允许"静默保留上一次的值"—— 那会让"我改了参数"变成静默无效；
3. 非唯一性的约束失败（NOT NULL / CHECK / 外键）→ :class:`SqliteSeedConstraint`，
   **不冒充**"主键冲突"：把 NOT NULL 报成重复会逼人去改主键，白查一轮
   （DuckDB 侧一条 `ConstraintException` 全当归一类，这里分得开是因为 SQLite 的
   报错文本自带 `UNIQUE constraint failed` 前缀）。

## 用法

```python
from tests.support.seed_sqlite import SeedSqlite

seed = SeedSqlite(sqlite_store)
seed.trading_dates(["2026-08-06"])
seed.job_log("incremental_update", started_at="2026-08-04T10:33:17+00:00")
seed.retry("000001", "price_daily", adapter="manager", error="paired failure")
seed.watchlist("600519", group_name="current")
# 用户态三张表（20 面板的"已发布 DSL / 已存规则 / 缺失清单"）
seed.dsl_expression("value_score", 1, "pe_ttm / pb_mrq", status="published",
                    direction="higher_is_better", content_hash="locked-hash")
seed.screening_rule("dsl-rule", 1, rule_json='{"conditions": {}}')
seed.missing_list("000001", "pe_ttm", "source_incomplete")
# 调用点已开事务时：join(conn) —— 方法不再自己开/提交事务
with sqlite_store.transaction() as conn:
    SeedSqlite(sqlite_store).join(conn).missing_list("000002", "price.close", "source_empty")
```

## 尚未覆盖（刻意不做）

* `auto_update_state` 的 DDL **不在** `app/core/storage/schema.py`，而在
  `app/core/auto_update.py::AutoUpdateController._ensure_state_table()`（懒建表）：
  列仍由 :meth:`SeedSqlite.auto_update_state` 用 `PRAGMA` 校验，不抄第二份清单。
* `screening_results` / `dsl_dependencies`：它们的播种是"建一棵外键引用树 +
  逐层回捞自增 id"（先插 `dsl_expressions` 拿 `lastrowid`，再插依赖/结果行的
  `*_id`），是测试的**结构性断言对象**
  （`test_personalized_restore_obeys_actual_foreign_key_topology`），不是"造一行"，
  所以留在测试里内联。`dsl_expressions` / `screening_rules` 本身**可以**用
  :meth:`dsl_expression` / :meth:`screening_rule` 播种（它们不依赖回捞的 id），
  需要 id 的只是**引用它们的那一层**。
* 需要 `lastrowid` 回捞的行（`retry_list` + 随后 `WHERE id = ?`）留在测试里内联：
  让工厂返回 id 会把"这一行后面还要被 UPDATE/DELETE 指着"的意图藏进返回值里。
  :meth:`join` **不改变**这条：绑定了连接也不会让方法开始返回 id。
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from app.core.storage.sqlite_store import SQLiteStore

#: 固定时间戳而不是 ``now()``：重复跑同一测试得到同一份种子。
#: 形状与 schema 的 ``DEFAULT CURRENT_TIMESTAMP`` 一致（``YYYY-MM-DD HH:MM:SS``）。
DEFAULT_SEED_TIME: Final = "2026-09-19 00:00:00"

#: `job_logs.status` 的起始值（生产先写 running，再 UPDATE 成终态）。
DEFAULT_JOB_STATUS: Final = "running"
#: 生产每次写 job_logs 都带 JSON 载荷，从不留 NULL。
DEFAULT_DETAILS_JSON: Final = "{}"

#: `retry_list` 的生产默认口径（`_record_retry` 写 0/'{}'，其余是 schema 默认值）。
DEFAULT_RETRY_COUNT: Final = 0
DEFAULT_MAX_RETRIES: Final = 5
DEFAULT_EXTRA_JSON: Final = "{}"
DEFAULT_ERROR_KIND: Final = "unknown"

#: `watchlist.group_name` / `manual_overrides.status` 的 schema 默认值。
DEFAULT_GROUP_NAME: Final = "default"
DEFAULT_OVERRIDE_STATUS: Final = "active"

#: `auto_update_state` 单行表的初始形状（生产 `AutoUpdateController.__init__`）。
DEFAULT_AUTO_UPDATE_STATE: Final = "enabled"
DEFAULT_AUTO_UPDATE_STAGE: Final = "idle"

#: `dsl_expressions.status` / `screening_rules.status` 的 schema 默认值：
#: 生产先存草稿（`draft`），校验/预览/发布是后续显式 UPDATE 的状态机。
DEFAULT_DSL_STATUS: Final = "draft"
DEFAULT_RULE_STATUS: Final = "draft"
#: `screening_rules.locked_indicators` 的最小合法 JSON：**没有锁任何指标**。
#: 生产在没有 DSL 指标参与时也写 `'{}'`（不是 NULL —— 该列 NOT NULL，且下游
#: `json.loads()` 直接读它）。
DEFAULT_LOCKED_INDICATORS: Final = "{}"


class SqliteSeedError(Exception):
    """播种器错误的基类。"""


class UnknownSqliteColumn(SqliteSeedError):
    """传了该表 schema 里不存在的列名（多半是拼错）。"""


class DuplicateSqliteSeed(SqliteSeedError):
    """主键/唯一索引已存在。默认抛错而不是静默覆盖/静默忽略。"""


class SqliteSeedConstraint(SqliteSeedError):
    """非唯一性约束失败（NOT NULL / CHECK / 外键）。不冒充"主键冲突"。"""


#: SQLite 侧允许"交给库侧求值"的列值（闭集合，与 DuckDB 侧
#: :data:`tests.support.seed._ALLOWED_SQL_VALUES` 同一把尺子）：两个无参函数，
#: 加上 :func:`sql_now` 生成的 `datetime('now'[, '<±N 单位>'])`。
#:
#: 为什么需要它：`missing_list.resolved_at` / `data_refresh_state.updated_at`
#: 这类列的语义就是"**库的**这一刻"（`datetime('now')` 是 UTC，与本机 +08:00
#: 差 8 小时）。写 Python 的 `datetime.now()` 会把时区口径悄悄换掉，"解决于
#: 何时"的判据就随运行机器的时区漂移。
_ALLOWED_SQL_VALUES: Final = frozenset({"CURRENT_TIMESTAMP", "CURRENT_DATE"})
#: 完整白名单形状：两个无参函数 + `datetime('now'[, '<±N 单位>'])`（修饰符闭集合）。
_ALLOWED_SQL_EXPRESSION: Final = re.compile(
    r"(?:CURRENT_TIMESTAMP|CURRENT_DATE|datetime\('now'(?:, '[+-]?\d{1,4} "
    r"(?:second|minute|hour|day|month|year)s?')?\))",
    re.ASCII,
)
_SQL_MODIFIER: Final = re.compile(
    r"[+-]?\d{1,4} (?:second|minute|hour|day|month|year)s?", re.ASCII
)


class SqlValue:
    """一列"交给库侧求值"的哨兵（只收白名单表达式，见 :data:`_ALLOWED_SQL_VALUES`）。

    任意 SQL 字符串不接受：那既是注入面，又让工厂的列校验退化成"能塞什么都行"。
    """

    __slots__ = ("sql",)

    def __init__(self, sql: str) -> None:
        if not _ALLOWED_SQL_EXPRESSION.fullmatch(sql):
            raise SqliteSeedError(
                f"SqlValue 只接受白名单表达式 {sorted(_ALLOWED_SQL_VALUES)} 或 "
                f"datetime('now'[, '±N <单位>'])；收到 {sql!r}。"
            )
        self.sql = sql

    def __repr__(self) -> str:
        return f"SqlValue({self.sql!r})"


#: 列取库侧当前时刻 / 今天。
CURRENT_TIMESTAMP: Final = SqlValue("CURRENT_TIMESTAMP")
CURRENT_DATE: Final = SqlValue("CURRENT_DATE")


def sql_now(modifier: str | None = None) -> SqlValue:
    """`datetime('now'[, modifier])`：库侧"现在"的字符串形状 + 相对偏移。

    相对量（"200 天前解决掉的缺失""50 天前建的计划"）必须由**库**算：
    在 Python 里算成字面量，"相对现在"只在跑测试那一刻成立。
    `modifier` 只收 ``±N second|minute|hour|day|month|year``，其余一律
    :class:`SqliteSeedError`。
    """
    if modifier is None:
        return SqlValue("datetime('now')")
    if not _SQL_MODIFIER.fullmatch(modifier):
        raise SqliteSeedError(
            f"sql_now() 的修饰符只接受 '±N second|minute|hour|day|month|year'，"
            f"收到 {modifier!r}"
        )
    return SqlValue(f"datetime('now', '{modifier}')")


class SeedSqlite:
    """按域播种 SQLite 操作库的工厂。一个实例 = 一个 store（无状态，可重复构造）。

    默认**每次方法调用自开一个事务**（`store.transaction()`）。在调用点已经
    `with sqlite_store.transaction() as conn:` 的块里必须改用 :meth:`join`：
    SQLite 侧虽然不会像 DuckDB 那样死等文件锁，但"工厂自己 commit"会让调用点
    的原子块**提前落库**（后半段抛错时前半段收不回来），所以那种块里原先只能
    继续写内联 SQL。
    """

    def __init__(
        self, store: SQLiteStore, *, conn: sqlite3.Connection | None = None
    ) -> None:
        """`conn=` 是 :meth:`join` 的实现面：绑定后**不开/提交事务**，提交权归调用方。"""
        self._store = store
        self._conn = conn
        self._table_columns: dict[str, frozenset[str]] = {}

    def join(self, conn: sqlite3.Connection) -> SeedSqlite:
        """绑定到调用点**已存在**的连接：之后的每个方法都不自己开/提交事务。

        三条边界与 DuckDB 侧 :func:`tests.support.seed.Seed.join` 同源：

        1. **不申请新事务**：不碰 `store.transaction()`，所以可以写在
           `with sqlite_store.transaction() as conn:` 里面；
        2. **不提交、不回滚**：写进调用方的事务，异常向上抛，落不落库由调用方
           的 `with` 决定；
        3. **其余口径一模一样**：列校验改用**这条**连接跑 `PRAGMA table_info`
           （不去开读连接），`UnknownSqliteColumn` / `DuplicateSqliteSeed` /
           `SqliteSeedConstraint` 分类不变。

        ```python
        with sqlite_store.transaction() as conn:
            SeedSqlite(sqlite_store).join(conn).missing_list("000001", "pe_ttm", "source_incomplete")
        ```
        """
        return SeedSqlite(self._store, conn=conn)

    # ── 任务日志 ──────────────────────────────────────────────────────
    def job_log(
        self,
        job_type: str,
        *,
        status: str = DEFAULT_JOB_STATUS,
        started_at: str | None = DEFAULT_SEED_TIME,
        finished_at: str | None = None,
        details_json: str | None = DEFAULT_DETAILS_JSON,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`job_logs`：主键只有自增 `id`，**重复播种不报错**（真实语义）。

        这张表是**日志流**：同一 `job_type` 本来就有多条历史（`status='running'`
        那条 + 各次终态）。所以它像 DuckDB 侧的 `funding_events`，不该被
        "重复就抛"一刀切。真正挡拼错列名的是 :class:`UnknownSqliteColumn`。

        默认 `status='running'` / `details_json='{}'` 照生产 `_start_job`；
        `finished_at` 留 NULL —— "还在跑"就是没有完成时刻。
        """
        self._insert(
            "job_logs",
            {
                "job_type": job_type,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "details_json": details_json,
                **cols,
            },
            key=(),
            overwrite=overwrite,
        )

    # ── 重试队列 ──────────────────────────────────────────────────────
    def retry(
        self,
        stock_code: str,
        data_type: str,
        *,
        adapter: str,
        error: str | None = None,
        retry_count: int = DEFAULT_RETRY_COUNT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        next_retry_at: str | None = None,
        last_attempt: str | None = None,
        extra_json: str | None = DEFAULT_EXTRA_JSON,
        error_kind: str = DEFAULT_ERROR_KIND,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`retry_list`：去重靠 `uq_retry_list_request(stock_code, data_type, adapter,
        extra_json)` 唯一索引，所以键里**包含 `extra_json`**。

        `extra_json` 默认 `'{}'` 不是随手填的：唯一索引把 NULL 当作互不相等，
        漏写它会让"同一条重试"静默变成两条。生产 `_record_retry` 也逐条写 `'{}'`。
        需要 `adjust=qfq` 这类请求补充参数时显式传 `json.dumps({...})`。

        `retry_count`/`max_retries` 的**具体值**是该进用例还是进工厂，看用例：
        "第 2 次失败即转 missing"必须逐字写 `retry_count=2, max_retries=2`。
        """
        self._insert(
            "retry_list",
            {
                "stock_code": stock_code,
                "data_type": data_type,
                "adapter": adapter,
                "error": error,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "next_retry_at": next_retry_at,
                "last_attempt": last_attempt,
                "extra_json": extra_json,
                "error_kind": error_kind,
                **cols,
            },
            key=("stock_code", "data_type", "adapter", "extra_json"),
            overwrite=overwrite,
        )

    # ── 自选列表 ──────────────────────────────────────────────────────
    def watchlist(
        self,
        stock_code: str,
        *,
        group_name: str = DEFAULT_GROUP_NAME,
        source_rule_id: int | None = None,
        source_result_id: int | None = None,
        added_at: str | None = DEFAULT_SEED_TIME,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`watchlist`：去重靠 `uq_watchlist_stock_group(stock_code, group_name)`。

        `group_name` 不吃 NULL（虽然列可空）：生产只写 `'default'` 或用户分组名，
        而唯一索引把 `(x, NULL)` 视作与 `(x, 'default')` 不同行 —— 想造
        "同一只股票在两个分组"就显式传两个 `group_name`。
        """
        self._insert(
            "watchlist",
            {
                "stock_code": stock_code,
                "group_name": group_name,
                "source_rule_id": source_rule_id,
                "source_result_id": source_result_id,
                "added_at": added_at,
                **cols,
            },
            key=("stock_code", "group_name"),
            overwrite=overwrite,
        )

    # ── 交易日历 ──────────────────────────────────────────────────────
    def trading_dates(
        self, dates: Iterable[str], *, overwrite: bool = False, **cols: Any
    ) -> None:
        """`trading_dates`：`dates` = `["2026-08-06", …]`，主键 `trade_date`。

        日历是**数值输入的持久依赖**（schema v11：技术指标要用），不是缓存。
        批量写法与 DuckDB 侧的 `shares()/prices()` 同形：日期列表逐字出现在调用点
        —— 哪几天交易日、缺哪一天，常常正是用例的本意。
        """
        self._insert_many(
            "trading_dates",
            [{"trade_date": trade_date, **cols} for trade_date in dates],
            key=("trade_date",),
            overwrite=overwrite,
        )

    # ── 人工覆写 ──────────────────────────────────────────────────────
    def override(
        self,
        stock_code: str,
        field_name: str,
        override_value: float,
        *,
        reason: str,
        report_date: str | None = None,
        original_value: float | None = None,
        correction_template: str | None = None,
        status: str = DEFAULT_OVERRIDE_STATUS,
        rolled_back_at: str | None = None,
        rolled_back_to: int | None = None,
        created_at: str | None = DEFAULT_SEED_TIME,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`manual_overrides`：主键只是自增 `id`，去重靠**部分唯一索引**。

        索引 `uq_manual_overrides_published` 只覆盖
        `status = 'published' AND rolled_back_at IS NULL` 的行（schema v2）：
        `published` 的同一 (股票, 字段, 报告期) 不允许两条，而 `active`/`rolled_back`
        的历史行本来就可以共存。这个口径写在这里，是因为 `DuplicateSqliteSeed`
        只在 `status='published'` 时才可能出现 —— 拿 `status='active'` 播种两次
        不报错**不是**工厂漏了检查。

        `override_value` / `reason` 是 NOT NULL，所以是必填参数：工厂不让 NULL
        走到库里再被 `SqliteSeedConstraint` 拦（那种红得晚一轮，且不好读）。
        """
        self._insert(
            "manual_overrides",
            {
                "stock_code": stock_code,
                "field_name": field_name,
                "report_date": report_date,
                "original_value": original_value,
                "override_value": override_value,
                "reason": reason,
                "correction_template": correction_template,
                "status": status,
                "rolled_back_at": rolled_back_at,
                "rolled_back_to": rolled_back_to,
                "created_at": created_at,
                **cols,
            },
            key=("stock_code", "field_name", "report_date"),
            overwrite=overwrite,
        )

    # ── DSL 表达式 / 筛选规则 / 缺失列表（2016 面板的"用户态"三张表）─────
    def dsl_expression(
        self,
        name: str,
        version: int,
        expression_text: str,
        *,
        status: str = DEFAULT_DSL_STATUS,
        description: str | None = None,
        direction: str | None = None,
        historical_capable: bool | None = None,
        content_hash: str | None = None,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`dsl_expressions`：唯一键 `UNIQUE(name, version)`（不是主键 —— 主键只有自增 `id`）。

        为什么这五个"口径列"在签名里而不是全靠 `**cols`：

        * `status` 是**发布态**判据（`draft/validated/single_previewed/previewed/
          published`），筛选只认 `published`；默认 `'draft'` 是 schema 默认值，
          与生产"先存草稿再发布"一致；
        * `content_hash` 是**锁定用的指纹**（规则保存时把 `(version, content_hash)`
          锁进 `screening_rules.locked_indicators`）：`'locked-hash'` / `'old-hash'`
          这类值是用例的**论点**，所以工厂不给它编一个像 sha256 的默认值 ——
          不写就是 NULL，写什么由调用点说了算（"陈旧指纹"用例正要有差别的值）；
        * `direction` / `historical_capable` / `description` 同理：不写＝NULL。

        `ast_json` / `created_at` 不在签名里：前者由生产在校验时回填、后者由
        schema 的 `DEFAULT CURRENT_TIMESTAMP` 兜底，需要钉某一刻时显式
        `created_at=...` 即可（列校验照旧）。

        去重键是 `(name, version)`：同一个表达式发两个版本是**真实语义**
        （`version=1` 与 `version=2` 并存），所以重复播种的判据只在"同名同版本"
        上 —— 那时抛 :class:`DuplicateSqliteSeed`，不静默留旧值。
        """
        self._insert(
            "dsl_expressions",
            {
                "name": name,
                "version": version,
                "expression_text": expression_text,
                "status": status,
                "description": description,
                "direction": direction,
                "historical_capable": historical_capable,
                "content_hash": content_hash,
                **cols,
            },
            key=("name", "version"),
            overwrite=overwrite,
        )

    def screening_rule(
        self,
        name: str,
        version: int,
        rule_json: str,
        *,
        locked_indicators: str = DEFAULT_LOCKED_INDICATORS,
        status: str = DEFAULT_RULE_STATUS,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`screening_rules`：`UNIQUE(name, version)`，三列 NOT NULL。

        `rule_json` 与 `locked_indicators` 都是 **TEXT 里的 JSON**（不是
        SQLite 的 JSON 函数参数），且都是 NOT NULL：前者是规则本体，后者是
        "这条规则当时把哪一版指标锁死了"的快照。工厂只给 `locked_indicators`
        一个 `'{}'`（**没有锁任何指标**的最小合法 JSON，生产在没有 DSL 指标时
        也写它），因为真正的锁定内容是用例要断言的东西，必须逐字来自调用点。

        `status` 默认 `'draft'`；`'saved'`（已保存待跑）与 `'published'` 是
        调用点显式写的状态，工厂不猜用例要哪个。

        `created_at` 同 :meth:`dsl_expression`：不传就交给 schema 默认。
        """
        self._insert(
            "screening_rules",
            {
                "name": name,
                "version": version,
                "rule_json": rule_json,
                "locked_indicators": locked_indicators,
                "status": status,
                **cols,
            },
            key=("name", "version"),
            overwrite=overwrite,
        )

    def missing_list(
        self,
        stock_code: str,
        field_name: str,
        reason_code: str,
        *,
        resolved_at: str | None = None,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`missing_list`：一行"某个字段缺了、原因是什么"。

        三列 NOT NULL（`stock_code` / `field_name` / `reason_code`）在签名里：
        "缺哪个字段、为什么缺"是这批用例的**论点**，漏传只会被 NOT NULL 拦在
        库里；写成关键字参数能让漏传在调用点就是 `TypeError`。

        `resolved_at` 默认 `None`＝**未解决**。去重靠**部分唯一索引**
        `uq_missing_list_stock_field_open(stock_code, field_name) WHERE
        resolved_at IS NULL`（schema v15）：同股票同字段可以有**一条**未解决行，
        已解决的历史行不占槽位 —— 所以"重复播种抛 `DuplicateSqliteSeed`"
        只在两条都未解决时成立，`resolved_at` 非 NULL 的历史行可以共存。
        这正是 GC 用例（"200 天前解决掉的要清掉"）的前提。

        `detected_at` 不在签名里：不传就走 schema 的 `DEFAULT CURRENT_TIMESTAMP`
        （那一列本来就是"库里发现的时刻"），要钉某一刻时就显式传
        `detected_at=...`（或 :func:`tests.support.seed.sql_now` 的等价物）。

        注：任务清单里提到的 `detail` 列在当前 schema **不存在**
        （`app/core/storage/schema.py` 的 `missing_list` 只有 `id/stock_code/
        field_name/reason_code/resolved_at/detected_at`），所以本方法不给它开口子：
        传 `detail=...` 会当场 `UnknownSqliteColumn` —— 这比插一行全 NULL 好。
        """
        self._insert(
            "missing_list",
            {
                "stock_code": stock_code,
                "field_name": field_name,
                "reason_code": reason_code,
                "resolved_at": resolved_at,
                **cols,
            },
            key=("stock_code", "field_name"),
            overwrite=overwrite,
        )

    # ── 数据域刷新状态 ────────────────────────────────────────────────
    def refresh_state(
        self, key: str, value: str, *, updated_at: str = DEFAULT_SEED_TIME,
        updated_now: bool = False, overwrite: bool = False, **cols: Any,
    ) -> None:
        """`data_refresh_state`：主键 `key`，`value` 是 JSON 字符串，`updated_at` NOT NULL。

        生产用 `(key, value, updated_at)` + `ON CONFLICT(key) DO UPDATE` 写
        "某个低频域上次刷新到什么时候"（如 `csrc_industry_last_refresh`）。
        想表达"很久没刷过"就把 `updated_at` 写成一个旧字面量 —— 这是用例的本意，
        工厂不给它编相对时间。

        **例外：`updated_now=True`（"刚刷新过"）** —— 与 DuckDB 侧
        `Seed(store, trace_now=True)` 同一取舍（放在**开关**上而不是每个方法一个
        `fresh=` 参数）：`updated_at` 写**这一刻**（UTC，形状同
        `DEFAULT CURRENT_TIMESTAMP` 的 `YYYY-MM-DD HH:MM:SS`，与 SQLite 的
        `datetime('now')` 是同一个时钟）。为什么必须显式：默认的固定字面量
        会让"刚刷新过 → 应当节流"的用例**静默**变成"过期 → 应当刷新"，
        反过来把被测逻辑的成功当成失败（或者更糟：断言对着错误的路径绿）。
        要"很久没刷过"就照旧写死一个旧字面量（或传 `updated_at=...`）。

        `updated_now=True` 与显式 `updated_at=` 同时给是调用点没说清要哪个 →
        :class:`SqliteSeedError`（不猜）。
        """
        if updated_now and updated_at != DEFAULT_SEED_TIME:
            raise SqliteSeedError(
                "refresh_state(): updated_now=True 与显式 updated_at= 冲突，"
                "不知道你要'此刻'还是'这一秒'；删掉一个再来。"
            )
        if updated_now:
            # UTC + 秒级：与 schema 的 `DEFAULT CURRENT_TIMESTAMP`（SQLite 的
            # `datetime('now')` 是 UTC）同形同钟，不会被 +08:00 的本机时钟带偏。
            updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        self._insert(
            "data_refresh_state",
            {"key": key, "value": value, "updated_at": updated_at, **cols},
            key=("key",),
            overwrite=overwrite,
        )

    # ── 自动更新状态（单行表）─────────────────────────────────────────
    def auto_update_state(
        self,
        *,
        state: str = DEFAULT_AUTO_UPDATE_STATE,
        paused: int = 0,
        current_stage: str = DEFAULT_AUTO_UPDATE_STAGE,
        progress_json: str | None = None,
        last_error: str | None = None,
        last_success_at: str | None = None,
        updated_at: str | None = DEFAULT_SEED_TIME,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`auto_update_state`：`CHECK (id = 1)` 的**单行**表 → `id` 固定为 1。

        `id` 是表口径（不是业务变量），所以不可传；重复播报 → `DuplicateSqliteSeed`
        （想改状态就 `overwrite=True`，或像生产那样用 `ON CONFLICT(id) DO UPDATE`）。

        `paused` 是 SQLite 的 INTEGER 布尔（`0`/`1`），不是 Python `bool`：
        写 `True` 会被 sqlite3 存成 `1` 读回来也是 `1`，断言里最好照 `0/1` 写。
        """
        self._insert(
            "auto_update_state",
            {
                "id": 1,
                "state": state,
                "paused": paused,
                "current_stage": current_stage,
                "progress_json": progress_json,
                "last_error": last_error,
                "last_success_at": last_success_at,
                "updated_at": updated_at,
                **cols,
            },
            key=("id",),
            overwrite=overwrite,
        )

    # ── 内部：列校验 / 写入 ───────────────────────────────────────────
    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """写入用的连接：**绑定了就是调用方那条**，不开事务、不 commit。"""
        if self._conn is not None:
            yield self._conn
        else:
            with self._store.transaction() as conn:
                yield conn

    def _columns_of(self, table: str) -> frozenset[str]:
        cached = self._table_columns.get(table)
        if cached is not None:
            return cached
        if self._conn is not None:
            # 绑定时用调用方那条连接：另开读连接会看到未提交前的旧 schema 快照，
            # 更会把"校验"变成一次跨连接的额外往返。
            described = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        else:
            with self._store.connection() as conn:
                described = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = frozenset(row["name"] for row in described)
        if not columns:
            raise SqliteSeedError(f"表 {table} 不存在（PRAGMA table_info 返回空）")
        self._table_columns[table] = columns
        return columns

    def _prepare(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        columns = self._columns_of(table)
        unknown = sorted(set(row) - columns)
        if unknown:
            raise UnknownSqliteColumn(f"{table} 没有列 {unknown}；可用列：{sorted(columns)}")
        return row

    def _insert(
        self,
        table: str,
        row: dict[str, Any],
        *,
        key: Sequence[str],
        overwrite: bool = False,
    ) -> None:
        self._insert_many(table, [row], key=key, overwrite=overwrite)

    def _insert_many(
        self,
        table: str,
        rows: Iterable[dict[str, Any]],
        *,
        key: Sequence[str],
        overwrite: bool = False,
    ) -> None:
        payloads = [self._prepare(table, row) for row in rows]
        if not payloads:
            return
        columns = list(payloads[0])
        placeholders = ", ".join("?" for _ in columns)
        verb = "INSERT OR REPLACE" if overwrite else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        try:
            with self._transaction() as conn:
                if any(
                    isinstance(value, SqlValue)
                    for payload in payloads
                    for value in payload.values()
                ):
                    self._emit_with_sentinels(conn, table, verb, columns, payloads)
                else:
                    conn.executemany(sql, [list(payload.values()) for payload in payloads])
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "UNIQUE constraint failed" in message or "PRIMARY KEY" in message:
                raise DuplicateSqliteSeed(
                    f"{table} 主键/唯一索引 {key or '(无自然键)'} 冲突（{payloads}）；"
                    "同一测试重复播种请显式传 overwrite=True"
                ) from exc
            raise SqliteSeedConstraint(
                f"{table} 非唯一性约束失败（{message}）；"
                "NOT NULL/CHECK/外键不是'重复播种'，不要用 overwrite=True 绕"
            ) from exc

    def _emit_with_sentinels(
        self,
        conn: sqlite3.Connection,
        table: str,
        verb: str,
        columns: Sequence[str],
        payloads: Sequence[dict[str, Any]],
    ) -> None:
        """逐行组装 SQL：:class:`SqlValue` 列**内联**白名单表达式，其余仍参数绑定。

        只在这批行里真的有哨兵时才走这条路（其余情况一律 `executemany`，
        一次绑定 N 行）；白名单是闭集合，所以"内联"没有注入面。
        """
        for payload in payloads:
            slots: list[str] = []
            params: list[Any] = []
            for column in columns:
                value = payload.get(column)
                if isinstance(value, SqlValue):
                    slots.append(value.sql)
                else:
                    slots.append("?")
                    params.append(value)
            conn.execute(
                f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join(slots)})",
                params,
            )
