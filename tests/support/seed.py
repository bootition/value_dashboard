"""测试种子工厂：把重复的内联 SQL 播种收敛成**按域的方法**（不是万能 `insert_row`）。

## 为什么要有这个模块（`reports/140` §17.7 第 1 条）

测试体 34,472 行里 3,855 行（11.2%）是内联 SQL 播种，`tests/regression/*.py`
共 755 处 `INSERT INTO`。抽头部文件逐处归类后可以看清：重复的**不是数据本身**，
而是两类样板 ——

1. **provenance 样板**：`source/fetch_time/raw_response_hash/confidence/batch_id`
   被 schema 的 NOT NULL 逼着逐字抄（`'csmar', '2026-09-19 00:00:00', 'h',
   'strict', 'b'`）。测试真正关心的从来不是这几列，但抄错一列就直接
   `ConstraintException`，或者更糟：抄成别的 `source` 让断言莫名其妙地红。
2. **arrange 脚手架**：「建一只股票 + 建几期财务 + 建股本/价格」这条链在不同
   用例里复制了 N 遍。`test_indicator_ext.py` 里 29 处 `INSERT INTO
   balance_sheet` 有 20 处只写了 `stock_code, report_date, total_assets`。

本模块只收敛这两类。

## 边界：什么该进工厂、什么必须留在测试里

**该进工厂（"怎么放进去"）**

* 表的必填列口径（provenance 五件套、`share_capital_history` 用 `raw_hash`
  而不是 `raw_response_hash`、`business_breakdown.type` 的枚举）与日期列写法；
* 主键形状（如 `(stock_code, report_date)`）→ 重复播种行为**统一**；
* 序列类表的批量写入：`shares/prices/headcounts/fundings/dividends/segments/
  treasury_yield_curve` 收 `[(key, value…)]`，**边界值仍逐字出现在调用点**。

**必须留在测试里（"放什么进去"）**

* **数值本身**。工厂不给业务列设默认值：不写就是 NULL。像
  `revenue=1.0, selling_expenses=100.0`（"1 元收入 100 元费用"）、
  `total_equity=0.01`（权益乘数失真）这类极端构造，是**用例的全部意义**，
  抽象进工厂就等于把边界数据藏起来，测试会变成"跑通即可"的空壳。
* **制造缺失/错误的方式**。工厂**不提供** `seed.broken_stock()` 之类的
  "帮你造坏数据"开关：缺间接法、负分部收入、窗口外融资事件怎么写，由测试
  自己说清楚，读代码的人才看得出这条用例在钉哪条口径。
* **断言**，以及"为什么这几个数能钉死某条口径"的注释。

## 默认值口径（不能随便编）

provenance 的**形状**照生产代码取，不是随手编的短字符串：

* `batch_id` = 32 位 hex（生产 `uuid.uuid4().hex`，`app/core/business.py:151`）；
* `raw_hash` / `raw_response_hash` = 64 位 sha256 hex
  （`app/core/adapters/base.py:290`）；
* `confidence ∈ {"strict", "approximate", "missing"}`
  （`ConfidenceLevel`，`app/core/adapters/base.py:52`）；
* `source` = `csmar`（财务三表的真实来源）。

取值是**确定性**的（`fetch_time` 用固定字面量、hash 由表名+主键算出来），
不是 `now()` / `uuid4()`：同一测试重复跑结果一致，断言不会随"这一刻"漂移。

### 例外：`Seed(store, trace_now=True)`（"刚抓的"要真时间）

确定性有一个**显式**出口：构造时给 `trace_now=True`，`fetch_time` 写**这一刻**
（`datetime.now()`，与 SQL 侧 `CURRENT_TIMESTAMP` 同一台机器同一个时钟），
其余溯源列照旧。默认 `False`。

为什么需要它：`treasury_yield_curve` / `source_audit` 有一类用例的语义**就是**
"这条是刚抓的""这条比快照晚"（新鲜度判定、曲线输入指纹、审计与快照的先后）。
固定字面量会让这些用例要么对着一个早就过期的时间戳跑，要么被迫退回内联
`INSERT ... CURRENT_TIMESTAMP`。开关放在**构造参数**上而不是每个方法一个
`fetched_now=`：它描述的是"这把这批种子要不要真时间"，与方法无关，而实例
本来就可以随手多造一个（`Seed(duck, trace_now=True)`）。

要钉**某一刻**（"未来时间戳""一天前的审计""比快照晚 1 秒"）就显式传
`fetch_time=...`：调用点给的列**永远覆盖**工厂默认值，工厂不替你编。

### `overwrite=True` ≠ `if_exists="ignore"`（语义相反，不能互换）

* `overwrite=True` → `INSERT OR REPLACE`（DuckDB 的 upsert）：**覆盖**已有主键行，
  本次没提供的列保留旧值（`test_seed_factory.py` 不变式 3 钉的就是这个粒度）；
* `if_exists="ignore"` → `INSERT OR IGNORE`（等价 `ON CONFLICT DO NOTHING`）：
  主键已存在就**整行跳过**，库里仍是上一次的值。它是"确保存在"而不是
  "改成我的值"。

所以"同一行再播一次但不许动旧值"只能用 `if_exists="ignore"`；拿
`overwrite=True` 凑会静默改掉被断言守护的旧值（比如"已核验的分红金额不得被
稀疏响应清零"那类用例的种子）。两者同时传等于调用点没说清要哪个，
`_insert_many` 直接拒绝（`SeedError`），不猜。

## 为什么不是 `insert_row(table, **kwargs)`

万能口只是把 SQL 换个地方写：它既不知道表的主键，也不知道哪些列 NOT NULL，
于是列名拼错（`report_dte=`）会**静默**插进一行全 NULL 的种子 —— 测试继续
"通过"，但对被测逻辑毫无覆盖，这是最难查的一类假绿。本模块的每个方法都绑定
**一张表 + 该表的领域语义**，并且：

1. 列名先对照**真实 schema** 校验（`DESCRIBE`，不维护第二份列清单，schema
   加列不会让工厂过期）：拼错 → :class:`UnknownSeedColumn`，当场红；
2. 主键冲突 → :class:`DuplicateSeed`（除非显式 `overwrite=True`），
   不允许"静默保留上一次的值"——那会让"我改了参数"变成静默无效。

## 用法

```python
from tests.support.seed import Seed

seed = Seed(duckdb_store)
seed.income("600519", REPORT_DATE, revenue=1000.0, net_profit=300.0)
seed.balance("600519", REPORT_DATE, total_assets=2000.0, total_equity=1200.0)
seed.shares("600519", [("2000-01-01", 100.0), ("2026-01-01", 200.0)])
# 分红：`announcement_date` 可以是 None（未证公告日），也可以晚于 ex_date（as-of 口径）
seed.dividends("600519", [("2025-06-30", None, 1.0), ("2026-06-30", "2026-07-15", 2.0)])
# "刚抓的"曲线/审计：显式打开真时间
Seed(duckdb_store, trace_now=True).treasury_yield_curve("2026-08-31", 10.0, 3.0)
# 调用点自己已经开了写事务/写连接时：`join(conn)`，方法不再自己开事务
with duckdb_store.transaction() as conn:
    Seed(duckdb_store).join(conn).stock("600519", name="贵州茅台")
# 批量脚手架（一次事务 + executemany）：池子类用例不要逐行调
Seed(duckdb_store).stocks([("600000", "stock-0"), ("600001", "stock-1")])
# 某一列交给库侧时钟（白名单）：新鲜度判据要的正是"库的现在"
seed.snapshot("600519", REPORT_DATE, calculated_at=CURRENT_TIMESTAMP)
```

### 三个入口的口径差别（也是"为什么工厂进不去"的三种情况）

| 情形 | 入口 | 事务 / 写锁 |
| --- | --- | --- |
| 方法调用时没有事务 | `seed.income(...)` | 工厂自己 `transaction()`：拿锁 → begin → commit |
| 已经在 `store.transaction()` / `write_connection()` 里 | `seed.join(conn).income(...)` | **不拿锁、不 begin、不 commit**，提交权归调用方 |
| 相对现在的时间 / 6000 行池子 | `sql_now(...)` / `stocks()` / `prices_bulk()` / `snapshots()` | 批量仍是**一次**事务 + `executemany` |

## 尚未覆盖（刻意不做）

SQLite 侧的 `job_logs` / `retry_list` / `watchlist` 不在本模块：本类构造时只拿
`DuckDBStore`，跨库播种会逼出一个 `insert_row` 式的万能口。需要时按同样形状
另立 SQLite 播种器（已落地：`tests/support/seed_sqlite.py`）。

`dividends_quarantine` / `source_audit_quarantine` / `raw_response_archive` /
`fetch_batch` 也**不做**方法：它们要么只在一种"整批搬家"的构造里出现（隔离/归档
用例，且几乎都写在调用点自己的 `write_connection()` 事务里 —— 这类"整批搬家"
本身就是被断言的对象），要么是**故意造错**的载荷（空 BLOB、`'not-a-sha'` 的 hash）。
`join()` 之后"写在调用点事务里"不再是进工厂的障碍，所以真正的理由只剩上一条：
这些表的写入是**测试的论点**（哪一行进隔离、哪一行留下），不是"造一行"。
"只被一处用到的表"补方法只会多一层没有第二个用户的抽象 —— 与
:class:`SeedError` 的边界一致：工厂收敛的是**重复样板**，不是"所有 INSERT"。
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from typing import Any, Final, Literal

import duckdb

from app.core.storage.duckdb_store import DuckDBStore

#: 生产口径的溯源默认值（形状见模块 docstring）。
DEFAULT_SOURCE: Final = "csmar"
DEFAULT_CONFIDENCE: Final = "strict"
#: 固定时间戳而不是 ``now()``：重复跑同一测试得到同一份种子。
DEFAULT_FETCH_TIME: Final = "2026-09-19 00:00:00"
#: `treasury_yield_curve` 的生产来源：`app/core/treasury.py` 落的是适配器
#: `metadata.source`，国债曲线只有 `czb_mof`（财政部/中债）一家，写 csmar 是错的。
DEFAULT_TREASURY_SOURCE: Final = "czb_mof"
_DEFAULT_BATCH_ID: Final = hashlib.sha256(b"value-dashboard/test-seed").hexdigest()[:32]

#: 各表需要的溯源列组合。列名必须与**该表 schema** 一致 —— 财务附表写
#: ``raw_response_hash``，而股本/事件类写 ``raw_hash``；``share_capital_history``
#: 没有 ``fetch_time``/``confidence``，``company_employee_history`` 没有 ``confidence``。
_TRACE_CRAWL: Final = ("source", "fetch_time", "raw_response_hash", "confidence", "batch_id")
_TRACE_EVENT: Final = ("source", "fetch_time", "raw_hash", "confidence", "batch_id")
_TRACE_RAW_HASH: Final = ("source", "raw_hash", "batch_id")
_TRACE_NO_CONFIDENCE: Final = ("source", "fetch_time", "batch_id")
#: `source_audit` 的溯源列名**不同**：批次叫 ``fetch_batch_id``，不是 ``batch_id``
#: （`app/core/storage/schema.py:420`）。抄错列名在这里是 `UnknownSeedColumn`，
#: 不是静默 NULL —— 这正是它需要独立常量、不能复用 `_TRACE_CRAWL` 的原因。
_TRACE_AUDIT: Final = ("source", "fetch_time", "raw_response_hash", "confidence", "fetch_batch_id")

#: 不计入"行身份 hash"的溯源列：它们由工厂按批给或由调用点单独给，与业务身份无关。
_PROVENANCE_KEYS: Final = frozenset({"source", "confidence", "batch_id", "fetch_batch_id"})


#: 重复播种时对已有主键的策略：``raise``（默认）/ ``ignore``（仅确保存在）。
IfExists = Literal["raise", "ignore"]


class SeedError(Exception):
    """播种器错误的基类。"""


class UnknownSeedColumn(SeedError):
    """传了该表 schema 里不存在的列名（多半是拼错）。"""


class DuplicateSeed(SeedError):
    """主键已存在。默认抛错而不是静默覆盖/静默忽略。"""


#: 允许出现在种子里的**库侧求值**表达式（白名单，闭集合）。
#:
#: 为什么需要哨兵：有些列的语义就是"库侧当前时刻"（`calculated_at`、
#: `fetch_time`、`updated_at` 与 `CURRENT_TIMESTAMP` 比较的新鲜度判据），
#: 未绑定时写 Python 的 `datetime.now()` 会带进本机时区与微秒；绑定了调用方
#: 事务时又没法再 `UPDATE ... CURRENT_TIMESTAMP` 回去。让调用点显式说
#: "这一列交给 SQL 函数"，两种场景才是同一口径。
#:
#: 为什么是白名单而不是 `seed.raw_sql("...")`：任意 SQL 字符串既是注入面，
#: 又退化成万能口 —— 列里能塞进子查询，工厂的列校验就形同虚设。这里只收
#: :data:`CURRENT_TIMESTAMP` / :data:`CURRENT_DATE` 两个无参函数，以及
#: :func:`sql_now` 生成的 `datetime('now'[, '<±N 单位>'])`（修饰符也过白名单，
#: 不接受任意文本）。
_ALLOWED_SQL_VALUES: Final = frozenset({"CURRENT_TIMESTAMP", "CURRENT_DATE"})
#: `datetime('now', '<modifier>')` 的修饰符白名单：`±N <单位>`，单位是固定的七个词。
_SQL_MODIFIER: Final = re.compile(
    r"[+-]?\d{1,4} (?:second|minute|hour|day|month|year)s?", re.ASCII
)
#: 完整白名单形状：两个无参函数 + `datetime('now'[, '<±N 单位>'])`。
_ALLOWED_SQL_EXPRESSION: Final = re.compile(
    r"(?:CURRENT_TIMESTAMP|CURRENT_DATE|datetime\('now'(?:, '[+-]?\d{1,4} "
    r"(?:second|minute|hour|day|month|year)s?')?\))",
    re.ASCII,
)


class SqlValue:
    """一列"交给库侧求值"的哨兵（白名单表达式，见 :data:`_ALLOWED_SQL_EXPRESSION`）。

    只由本模块的工厂函数造出来：调用点写 :data:`CURRENT_TIMESTAMP` /
    :data:`CURRENT_DATE` / ``sql_now("-200 days")``，不写 SQL 字符串本身。
    """

    __slots__ = ("sql",)

    def __init__(self, sql: str) -> None:
        if not _ALLOWED_SQL_EXPRESSION.fullmatch(sql):
            raise SeedError(
                f"SqlValue 只接受白名单表达式 {sorted(_ALLOWED_SQL_VALUES)} 或 "
                f"datetime('now'[, '±N <单位>'])；收到 {sql!r}。"
                "任意 SQL 字符串不接受（注入面 + 万能口）：要么用已有方法显式传值，"
                "要么给工厂补一个带列校验的方法。"
            )
        self.sql = sql

    def __repr__(self) -> str:
        return f"SqlValue({self.sql!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SqlValue) and other.sql == self.sql

    def __hash__(self) -> int:
        return hash((SqlValue, self.sql))


#: 种子列取**库侧当前时刻**：`CURRENT_TIMESTAMP`（TIMESTAMP 列按库的时钟写）。
CURRENT_TIMESTAMP: Final = SqlValue("CURRENT_TIMESTAMP")
#: 种子列取**库侧今天**：`CURRENT_DATE`（DATE 列）。
CURRENT_DATE: Final = SqlValue("CURRENT_DATE")


def sql_now(modifier: str | None = None) -> SqlValue:
    """`datetime('now'[, modifier])`：库侧"现在"的**字符串**形状 + 相对偏移。

    需要相对量（"200 天前解决掉的缺失""50 天前建的计划"）时用它，别在
    Python 里算一个字符串再当字面量写 —— 那样"相对现在"只在用例里那条
    时间口径与库时钟一致时才成立。`modifier` 只收 ``±N second|minute|hour|
    day|month|year``（复数可选），其余一律 :class:`SeedError`。
    """
    if modifier is None:
        return SqlValue("datetime('now')")
    if not _SQL_MODIFIER.fullmatch(modifier):
        raise SeedError(
            f"sql_now() 的修饰符只接受 '±N second|minute|hour|day|month|year'，"
            f"收到 {modifier!r}"
        )
    return SqlValue(f"datetime('now', '{modifier}')")


def exchange_of(code: str) -> str:
    """按代码段推断交易所，取值与生产 `stock_meta.exchange` 一致（SHSE/SZSE/BSE）。"""
    if code.startswith(("60", "68", "90", "51", "58")):
        return "SHSE"
    if code.startswith(("4", "8")):
        return "BSE"
    return "SZSE"


def _row_hash(table: str, key: Sequence[Any]) -> str:
    """确定性 64 位 sha256（形状同生产 ``raw_hash``，但每行互不相同）。"""
    material = "|".join([table, *(str(part) for part in key)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Seed:
    """按域播种 DuckDB 表的工厂。一个实例 = 一个 store（无状态，可重复构造）。

    默认**每次方法调用自开一个事务**（`store.transaction()`：拿写锁 → begin →
    commit → 释放）。在调用点已经持有 `store.transaction()` /
    `store.write_connection()` 的块里必须改用 :meth:`join`：DuckDB 的写锁是
    **应用级文件锁**，嵌套获取会自己等自己（实测 ~30s 后
    `DuckDBWriteLockError`），所以在那种块里只能继续写内联 SQL —— 这正是
    :meth:`join` 要消掉的最后一类"工厂进不去"。
    """

    def __init__(
        self,
        store: DuckDBStore,
        *,
        trace_now: bool = False,
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        """`trace_now=True` 把溯源 `fetch_time` 写成**这一刻**（默认固定字面量）。

        只影响 `fetch_time`，hash/`batch_id`/`confidence` 仍确定性；给
        "这条是刚抓的""这条比快照晚"这类语义用（见模块 docstring 的例外小节）。
        要指定某个具体时刻就直接传 `fetch_time=...`，逐行覆盖工厂默认值。

        `conn=` 是 :meth:`join` 的实现面：绑定后**既不拿写锁也不开/提交事务**，
        提交权完全归调用方（构造参数留在这里是为了让 `join()` 一行写完，直接
        传 `conn` 也是一样的语义）。
        """
        self._store = store
        self._trace_now = trace_now
        self._conn = conn
        self._schema_columns: dict[str, frozenset[str]] = {}

    def join(self, conn: duckdb.DuckDBPyConnection) -> Seed:
        """绑定到调用点**已存在**的连接：之后的每个方法都不自己开/提交事务。

        语义边界（读代码时按这三条看）：

        1. **不申请写锁**：不走 `store.transaction()` / `write_connection()`，
           所以可以安全地写在 `with store.transaction() as conn:` 里面；
        2. **不提交、不回滚**：写成功只是"在调用方的事务里写成功了"，异常向上抛，
           是否落库由调用方的 `with` 决定（`join` 出来的实例说自己 commit 了，
           那是谎话 —— 调用方 rollback 时一行都不会留）；
        3. **其余口径一模一样**：同样的列校验（绑定时用**这条**连接
           `DESCRIBE`，不去开读连接抢锁）、同样的 `UnknownSeedColumn` /
           `DuplicateSeed`、同样的 `SqlValue` 哨兵。

        典型的块内用法：

        ```python
        with duckdb_store.transaction() as conn:
            Seed(duckdb_store).join(conn).stock("600519", name="贵州茅台")
            seed.join(conn).income("600519", REPORT_DATE, revenue=1000.0)
        ```
        """
        return Seed(self._store, trace_now=self._trace_now, conn=conn)


    # ── 元数据 ────────────────────────────────────────────────────────
    def stock(
        self,
        code: str,
        *,
        name: str | None = None,
        exchange: str | None = None,
        overwrite: bool = False,
        if_exists: IfExists = "raise",
        **cols: Any,
    ) -> None:
        """`stock_meta`：`name`/`exchange` 是 NOT NULL，缺省按代码前缀推断真实值。

        `if_exists="ignore"` 只给 :meth:`screenable` 这类「**确保股票存在**」的
        脚手架用；直接播种仍然默认 `raise`，改参数不会静默无效。
        """
        self._insert(
            "stock_meta",
            {
                "stock_code": code,
                "name": name or code,
                "exchange": exchange or exchange_of(code),
                **cols,
            },
            key=("stock_code",),
            overwrite=overwrite,
            if_exists=if_exists,
        )

    def snapshot(self, code: str, period: str, *, overwrite: bool = False, **cols: Any) -> None:
        """`indicator_snapshot`（主键 `stock_code, report_date`）。"""
        self._insert(
            "indicator_snapshot",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            overwrite=overwrite,
        )

    def extended(
        self,
        code: str,
        period: str,
        *,
        calculated_at: Any,
        source: str,
        data_version: int,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`indicator_ext`（扩展指标域，主键 `stock_code, report_date`）。

        三个 NOT NULL 列显式写在签名里：它们**不是** provenance 五件套
        （`data_version` 在这张表是整数版本号、`source` 写的是"哪一版口径算出来
        的"而不是抓取来源），工厂不替调用点编。放在签名里而不是靠 DuckDB 兜底，
        是为了让漏传在**调用点**就是 `TypeError`，而不是一句没有上下文的
        `NOT NULL constraint failed`。
        """
        self._insert(
            "indicator_ext",
            {
                "stock_code": code,
                "report_date": period,
                "calculated_at": calculated_at,
                "source": source,
                "data_version": data_version,
                **cols,
            },
            key=("stock_code", "report_date"),
            overwrite=overwrite,
        )

    # ── 财务三表族（主键 stock_code, report_date）─────────────────────
    def income(self, code: str, period: str, *, overwrite: bool = False, **cols: Any) -> None:
        """`income_statement`。业务列无默认值：不写就是 NULL（口径要求如实缺失）。"""
        self._insert(
            "income_statement",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            overwrite=overwrite,
        )

    def balance(self, code: str, period: str, *, overwrite: bool = False, **cols: Any) -> None:
        """`balance_sheet`。同上，业务列无默认值。"""
        self._insert(
            "balance_sheet",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            overwrite=overwrite,
        )

    def cash_flow(self, code: str, period: str, *, overwrite: bool = False, **cols: Any) -> None:
        """`cash_flow`（直接法现金流量表）。"""
        self._insert(
            "cash_flow",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            overwrite=overwrite,
        )

    def cash_indirect(
        self, code: str, period: str, *, overwrite: bool = False, **cols: Any
    ) -> None:
        """`cash_flow_indirect`：带全套 provenance（这是重复最多的样板之一）。"""
        self._insert(
            "cash_flow_indirect",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            trace=_TRACE_CRAWL,
            overwrite=overwrite,
        )

    def cash_activity(
        self, code: str, period: str, *, overwrite: bool = False, **cols: Any
    ) -> None:
        """`cash_flow_activity`（资本支出等，带 provenance）。"""
        self._insert(
            "cash_flow_activity",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            trace=_TRACE_CRAWL,
            overwrite=overwrite,
        )

    def balance_ext(self, code: str, period: str, *, overwrite: bool = False, **cols: Any) -> None:
        """`balance_sheet_ext`（其他应收/应付合计等，带 provenance）。"""
        self._insert(
            "balance_sheet_ext",
            {"stock_code": code, "report_date": period, **cols},
            key=("stock_code", "report_date"),
            trace=_TRACE_CRAWL,
            overwrite=overwrite,
        )

    # ── 股本 / 价格 ──────────────────────────────────────────────────
    def shares(
        self,
        code: str,
        rows: Iterable[tuple[str, float]],
        *,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`share_capital_history`：`rows` = `[(effective_date, total_shares), …]`。

        时点股本序列（D23：历史市值误用当前股本）就靠这个列表**逐字**表达。
        """
        self._insert_many(
            "share_capital_history",
            [
                {
                    "stock_code": code,
                    "effective_date": effective_date,
                    "total_shares": total_shares,
                    **cols,
                }
                for effective_date, total_shares in rows
            ],
            key=("stock_code", "effective_date"),
            trace=_TRACE_RAW_HASH,
            overwrite=overwrite,
        )

    def prices(
        self,
        code: str,
        rows: Iterable[tuple[str, float]],
        *,
        table: str = "price_daily_raw",
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`price_daily_raw` / `price_daily_qfq`：`rows` = `[(trade_date, close), …]`。"""
        if table not in ("price_daily_raw", "price_daily_qfq"):
            raise SeedError(f"prices() 只接受原始/前复权日线表，收到 {table!r}")
        self._insert_many(
            table,
            [
                {"stock_code": code, "trade_date": trade_date, "close": close, **cols}
                for trade_date, close in rows
            ],
            key=("stock_code", "trade_date"),
            overwrite=overwrite,
        )

    # ── 事件 / 结构化数据 ────────────────────────────────────────────
    def headcounts(
        self,
        code: str,
        rows: Iterable[tuple[str, float] | tuple[str, float, str]],
        *,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`company_employee_history`：`rows` = `[(report_date, count[, source]), …]`。

        第三个元素是**该观测点的 source**（主键含它）；不写则用默认 source。
        """
        payload = []
        for row in rows:
            report_date, count, *rest = row
            payload.append(
                {
                    "stock_code": code,
                    "report_date": report_date,
                    "employee_count": count,
                    "source": rest[0] if rest else DEFAULT_SOURCE,
                    **cols,
                }
            )
        self._insert_many(
            "company_employee_history",
            payload,
            key=("stock_code", "report_date", "source"),
            trace=_TRACE_NO_CONFIDENCE,
            overwrite=overwrite,
        )

    def fundings(
        self,
        code: str,
        rows: Iterable[tuple[str, str, float] | tuple[str, str, float, dict[str, Any]]],
        *,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`funding_events`：`rows` = `[(event_type, list_date, issue_shares[, cols]), …]`。

        第四项（可选）是**该行独有**的列：同一只股票的多笔事件里
        `raise_funds` / `raise_funds_net` / `issue_price` / `source` / `derived`
        逐行不同（IPO 只剩净额、增发/配股只有总额 —— 这个口径差异正是用例的意义），
        而 `**cols` 只能给全批同值。逐行值优先于 `**cols`，两处都没给的列是 NULL。

        第三项可以写 `None`（"发行数量缺失"是 `fail-closed` 用例的前提），
        工厂不把它补成 0。

        该表无主键（事件流可追加）→ 重复播种**不报错**，这是它的真实语义。
        """
        payload = []
        for row in rows:
            event_type, list_date, issue_shares, *rest = row
            payload.append(
                {
                    "stock_code": code,
                    "event_type": event_type,
                    "list_date": list_date,
                    "issue_shares": issue_shares,
                    **cols,
                    **(rest[0] if rest else {}),
                }
            )
        self._insert_many(
            "funding_events",
            payload,
            key=(),
            trace=_TRACE_EVENT,
            overwrite=overwrite,
        )

    def buyback(
        self,
        code: str,
        start_date: str,
        buyback_shares: float,
        *,
        announce_date: str | None = None,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`buyback_events`（无主键，事件可追加）。"""
        self._insert(
            "buyback_events",
            {
                "stock_code": code,
                "start_date": start_date,
                "announce_date": announce_date or start_date,
                "buyback_shares": buyback_shares,
                **cols,
            },
            key=(),
            trace=_TRACE_EVENT,
            overwrite=overwrite,
        )

    def xdxr(
        self,
        code: str,
        event_date: str,
        *,
        category: int = 1,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`xdxr` 公司行为行（主键 `stock_code, event_date, category`）。

        只登记「这天发生过公司行为」的用例（xdxr → qfq 全历史重拉）**只传
        `category`**：`fenhong`/`songzhuangu`/`peigu`/`peigujia` 四个金额列
        一律留 NULL。正因为如此 :meth:`bonus` 不能拿来做这些用例 —— 它强制写一个
        `songzhuangu`，会把测试从没要过的业务值静默塞进种子。
        """
        self._insert(
            "xdxr",
            {"stock_code": code, "event_date": event_date, "category": category, **cols},
            key=("stock_code", "event_date", "category"),
            overwrite=overwrite,
        )

    def bonus(
        self, code: str, event_date: str, ratio: float, *, overwrite: bool = False, **cols: Any
    ) -> None:
        """`xdxr` 送转行（`category=1`，`songzhuangu` 是**每股**比例，见 D53）。"""
        self.xdxr(code, event_date, category=1, songzhuangu=ratio, overwrite=overwrite, **cols)

    def segments(
        self,
        code: str,
        period: str,
        rows: Iterable[tuple[str, float, float]],
        *,
        type_: int = 2,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`business_breakdown`：`rows` = `[(item_name, amount, ratio), …]`。

        分业务收入可为负 —— 负段照原样传进来，工厂不做任何"帮我取绝对值"的加工。
        """
        self._insert_many(
            "business_breakdown",
            [
                {
                    "stock_code": code,
                    "report_date": period,
                    "type": type_,
                    "item_name": item_name,
                    "amount": amount,
                    "ratio": ratio,
                    **cols,
                }
                for item_name, amount, ratio in rows
            ],
            key=("stock_code", "report_date", "type", "item_name"),
            trace=_TRACE_EVENT,
            overwrite=overwrite,
        )

    # ── 分红 / 国债曲线 / 来源审计 ────────────────────────────────────
    def dividends(
        self,
        code: str,
        rows: Iterable[tuple[str | date, str | date | None, float | None]],
        *,
        overwrite: bool = False,
        if_exists: IfExists = "raise",
        **cols: Any,
    ) -> None:
        """`dividends`：`rows` = `[(ex_date, announcement_date_or_None, dividend_per_share), …]`。

        三件事必须能在调用点逐字表达，所以行是三元组而不是"一行一次调用"：

        * `announcement_date` **可以是 None** —— "未证公告日"是
          `DIVIDEND_DATES_UNVERIFIED` 那整组用例的前提（股东回报指标必须整项缺失），
          工厂**不**替它填 `ex_date`（`buyback()` 的 `announce_date or start_date`
          那种兜底在这里等于把用例的前提抹掉）；
        * `announcement_date` **可以晚于 `ex_date`** —— as-of 口径用例要的正是
          "公告在后"这种真实时序；
        * `dividend_per_share` 逐行不同（"只统计最近一年"要看得出每一行）。

        其余列（`stock_dividend` / `transfer_share` / `rights_issue` …）走 `**cols`
        （全批同值）—— 本方法覆盖的调用点都只种现金分红；需要逐行不同的送转
        就在 `**cols` 里写 `None` 并把送转单独 `xdxr()`，别让工厂悄悄补 0。

        `if_exists="ignore"` 是 `ON CONFLICT DO NOTHING`（**保留**已有行），
        与 `overwrite=True`（`INSERT OR REPLACE`，**覆盖**）语义相反，不能互换：
        "已核验的分红不许被稀疏响应清零"那类用例的种子正是靠这个区别立住的。
        """
        self._insert_many(
            "dividends",
            [
                {
                    "stock_code": code,
                    "ex_date": ex_date,
                    "announcement_date": announcement_date,
                    "dividend_per_share": dividend_per_share,
                    **cols,
                }
                for ex_date, announcement_date, dividend_per_share in rows
            ],
            key=("stock_code", "ex_date"),
            overwrite=overwrite,
            if_exists=if_exists,
        )

    def treasury_yield_curve(
        self,
        curve_date: str | date,
        tenor_years: float,
        yield_pct: float,
        *,
        overwrite: bool = False,
        if_exists: IfExists = "raise",
        **cols: Any,
    ) -> None:
        """`treasury_yield_curve`：主键 `(curve_date, tenor_years)`，带全套溯源。

        溯源形状就是 :data:`_TRACE_EVENT`（`source / fetch_time / raw_hash /
        confidence / batch_id`），`source` 默认 :data:`DEFAULT_TREASURY_SOURCE`
        （`czb_mof`）—— 这张表只有国债曲线一家来源，写 csmar 是错的。

        时间戳的口径看用例：要"刚抓的/比快照新"就用 `Seed(store, trace_now=True)`，
        要一个具体时刻就显式传 `fetch_time=...`，默认是固定字面量（确定性）。
        逐行值（`yield_pct`、`tenor_years`）照旧由调用点写死 —— 收益率是业务值，
        工厂不给默认。
        """
        self._insert(
            "treasury_yield_curve",
            {
                "curve_date": curve_date,
                "tenor_years": tenor_years,
                "yield_pct": yield_pct,
                "source": DEFAULT_TREASURY_SOURCE,
                **cols,
            },
            key=("curve_date", "tenor_years"),
            trace=_TRACE_EVENT,
            overwrite=overwrite,
            if_exists=if_exists,
        )

    def source_audit(
        self,
        code: str,
        field_name: str,
        *,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`source_audit`：一行"来源审计"记录，溯源列名与别处**不同**。

        两个必有列（`stock_code` / `field_name`）在签名里，其余**逐字由调用点传**：

        * 批次列叫 `fetch_batch_id`（不是 `batch_id`）—— 抄错在这里是
          `UnknownSeedColumn`，不是静默 NULL；
        * `raw_response_hash` 也要调用点给形状：正常行写 `"a" * 64`，而
          "溯源被篡改"的用例会故意写 `raw_response_hash="not-a-sha"` ——
          工厂**不**替你编，也不会去校验它像不像 sha256（校验是生产逻辑、
          正是那些用例要测的东西）；
        * `report_date` / `value` / `reason_code` / `api_version` 都是业务列，
          不写就是 NULL；`created_at` 由 schema 的 `DEFAULT CURRENT_TIMESTAMP` 兜底。

        该表的主键只有自增 `id`（唯一索引 `idx_source_audit_stock_date_field`
        不是主键）→ 同一 `(stock, field)` 多条审计是**真实语义**（每次抓取一条），
        所以 `key=()`，重复播种不抛：与 `funding_events` 同一取舍。
        """
        self._insert(
            "source_audit",
            {"stock_code": code, "field_name": field_name, **cols},
            key=(),
            trace=_TRACE_AUDIT,
            overwrite=overwrite,
        )

    # ── 复合脚手架 ───────────────────────────────────────────────────
    def screenable(
        self,
        code: str,
        period: str,
        *,
        income: dict[str, Any] | None = None,
        balance: dict[str, Any] | None = None,
        cash_flow: dict[str, Any] | None = None,
        shares: Sequence[tuple[str, float]] | None = None,
        price: float | None = None,
    ) -> None:
        """一只「指标构建/筛选能看见」的最小股票，一条链建完。

        收敛的是**脚手架**（`stock_meta` + 当期 `income` × `balance` +
        `share_capital_history` + 报告期收盘价），业务数值仍由调用点逐字传入：

        ```python
        seed.screenable("600519", REPORT_DATE,
                        income={"revenue": 1000.0, "net_profit": 100.0},
                        balance={"total_assets": 2000.0, "total_equity": 1200.0},
                        shares=[("2000-01-01", 100.0)], price=10.0)
        ```

        可选段为 `None` 时**什么都不插**（不是插 0）：缺失输入必须保持缺失，
        这正是「缺失 → NULL，不得估算」那组用例要的前提。

        `stock_meta` 是**确保存在**语义（已存在则不动）：同一只股票的多个报告期
        可以反复调用本方法，这也是「一只股票 × N 期」这类脚手架的常态。
        """
        self.stock(code, if_exists="ignore")
        self.income(code, period, **(income or {}))
        self.balance(code, period, **(balance or {}))
        if cash_flow is not None:
            self.cash_flow(code, period, **cash_flow)
        if shares is not None:
            self.shares(code, shares)
        if price is not None:
            self.prices(code, [(period, price)])

    # ── 批量脚手架（一次事务 + executemany）───────────────────────────
    #
    # 逐行调 `stock()` / `prices()` 每行都是一次 `store.transaction()`：一次
    # **写锁 + begin + commit + 释放锁**。`_seed_listed_stocks(100)` /
    # `_seed_pool_for_truncation(6000)` 这类池子用逐行调用会把用例拖慢一个量级
    # （而且放在调用点事务里就必然自撞写锁）。批量入口只省"怎么放进去"，
    # 每行的业务值仍然逐字出现在调用点。

    def stocks(
        self,
        rows: Iterable[
            tuple[str, str | None] | tuple[str, str | None, dict[str, Any]]
        ],
        *,
        overwrite: bool = False,
        if_exists: IfExists = "raise",
        **cols: Any,
    ) -> None:
        """`stock_meta` 批量：`rows` = `[(code, name[, cols]), …]`。

        第三项（可选）是**该行独有**的列（`exchange` / `csrc_l1` / `listing_date` …
        逐行不同时用），优先级高于 `**cols`（全批同值）。`name` 写 `None` 时与
        :meth:`stock` 一致按代码兜底，`exchange` 仍按代码段推断真实交易所。

        列校验与重复行为跟 :meth:`stock` **逐条相同**（同一张 `stock_meta`、
        同一个主键 `stock_code`、同样的 `UnknownSeedColumn`/`DuplicateSeed`），
        区别只有"一次事务、一次 `executemany`"。
        """
        payload = []
        for row in rows:
            code, name, *rest = row
            payload.append(
                {
                    "stock_code": code,
                    "name": name or code,
                    "exchange": exchange_of(code),
                    **cols,
                    **(rest[0] if rest else {}),
                }
            )
        self._insert_many(
            "stock_meta", payload, key=("stock_code",), overwrite=overwrite, if_exists=if_exists
        )

    def prices_bulk(
        self,
        rows: Iterable[tuple[str, str, float] | tuple[str, str, float, dict[str, Any]]],
        *,
        table: str = "price_daily_raw",
        overwrite: bool = False,
        if_exists: IfExists = "raise",
        **cols: Any,
    ) -> None:
        """日线批量（**多只股票一个事务**）：`rows` = `[(code, trade_date, close[, cols]), …]`。

        与 :meth:`prices` 的分工：`prices(code, rows)` 是"一只股票一条序列"，
        本方法是"一批行（可以跨股票）"。第四项同样是逐行列（`volume` /
        `turnover_rate` …），优先级高于 `**cols`。
        """
        if table not in ("price_daily_raw", "price_daily_qfq"):
            raise SeedError(f"prices_bulk() 只接受原始/前复权日线表，收到 {table!r}")
        payload = []
        for row in rows:
            code, trade_date, close, *rest = row
            payload.append(
                {
                    "stock_code": code,
                    "trade_date": trade_date,
                    "close": close,
                    **cols,
                    **(rest[0] if rest else {}),
                }
            )
        self._insert_many(
            table, payload, key=("stock_code", "trade_date"), overwrite=overwrite,
            if_exists=if_exists,
        )

    def snapshots(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        overwrite: bool = False,
        if_exists: IfExists = "raise",
    ) -> None:
        """`indicator_snapshot` 批量：`rows` = `[{列: 值}, …]`，必须含主键两列。

        为什么宁可逐行给字典也不要"逐行调 `snapshot()`"：`_seed_pool_for_truncation`
        一次要种 6000 行快照，逐行 = 6000 次写锁。这里仍然**按这张表的口径**校验
        （`stock_code` / `report_date` 必给、其余列逐一过 schema），不是万能口：
        少给主键当场 `SeedError`，拼错列名当场 `UnknownSeedColumn`，一行都不落。
        """
        payload = [dict(row) for row in rows]
        for row in payload:
            missing = [column for column in ("stock_code", "report_date") if column not in row]
            if missing:
                raise SeedError(
                    f"snapshots() 每行必须给主键列，缺 {missing}（收到 {row}）"
                )
        self._insert_many(
            "indicator_snapshot",
            payload,
            key=("stock_code", "report_date"),
            overwrite=overwrite,
            if_exists=if_exists,
        )

    # ── 内部：列校验 / 溯源默认值 / 写入 ─────────────────────────────
    @contextlib.contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """写入用的连接：**绑定了就是调用方那条**，不拿锁、不 begin、不 commit。"""
        if self._conn is not None:
            yield self._conn
        else:
            with self._store.transaction() as conn:
                yield conn

    def _columns_of(self, table: str) -> frozenset[str]:
        cached = self._schema_columns.get(table)
        if cached is not None:
            return cached
        if self._conn is not None:
            # 绑定时**只能**用调用方那条连接：另开读连接会与调用方的写锁打架
            # （同一张表的 read_only 与 read_write 连接在 Windows/DuckDB 上互斥）。
            described = self._conn.execute(f"DESCRIBE {table}").fetchall()
        else:
            with self._store.read_connection() as conn:
                described = conn.execute(f"DESCRIBE {table}").fetchall()
        columns = frozenset(row[0] for row in described)
        if not columns:
            raise SeedError(f"表 {table} 不存在（DESCRIBE 返回空）")
        self._schema_columns[table] = columns
        return columns

    def _trace_values(
        self, table: str, row: dict[str, Any], trace: Sequence[str]
    ) -> dict[str, Any]:
        key = [row[column] for column in row if column not in _PROVENANCE_KEYS]
        values: dict[str, Any] = {}
        for column in trace:
            if column == "source":
                values["source"] = DEFAULT_SOURCE
            elif column == "fetch_time":
                # 唯一受 `trace_now` 影响的列：`datetime.now()` 与 SQL 侧
                # `CURRENT_TIMESTAMP` 落在同一个系统时钟上（TIMESTAMP 列按本机
                # 时区存裸值），所以"刚抓的"两种写法可以互相比较。
                values["fetch_time"] = datetime.now() if self._trace_now else DEFAULT_FETCH_TIME
            elif column in ("raw_hash", "raw_response_hash"):
                values[column] = _row_hash(table, key)
            elif column == "confidence":
                values["confidence"] = DEFAULT_CONFIDENCE
            elif column == "batch_id":
                values["batch_id"] = _DEFAULT_BATCH_ID
            elif column == "fetch_batch_id":
                values["fetch_batch_id"] = _DEFAULT_BATCH_ID
            else:  # pragma: no cover - 常量写错才会到这里
                raise SeedError(f"未知溯源列 {column!r}")
        return values

    def _prepare(self, table: str, row: dict[str, Any], trace: Sequence[str]) -> dict[str, Any]:
        # 调用点显式给的列一律覆盖工厂默认值（`**row` 在后）。
        payload = {**self._trace_values(table, row, trace), **row}
        columns = self._columns_of(table)
        unknown = sorted(set(payload) - columns)
        if unknown:
            raise UnknownSeedColumn(f"{table} 没有列 {unknown}；可用列：{sorted(columns)}")
        return payload

    def _insert(
        self,
        table: str,
        row: dict[str, Any],
        *,
        key: Sequence[str],
        trace: Sequence[str] = (),
        overwrite: bool = False,
        if_exists: IfExists = "raise",
    ) -> None:
        self._insert_many(
            table, [row], key=key, trace=trace, overwrite=overwrite, if_exists=if_exists
        )

    def _insert_many(
        self,
        table: str,
        rows: Iterable[dict[str, Any]],
        *,
        key: Sequence[str],
        trace: Sequence[str] = (),
        overwrite: bool = False,
        if_exists: IfExists = "raise",
    ) -> None:
        payloads = [self._prepare(table, row, trace) for row in rows]
        if not payloads:
            return
        # 列集合取**并集**：逐行 cols（`fundings` 的第四项）允许只在部分行出现，
        # 没给的行按 NULL 写 —— 与"未传的业务列就是 NULL"同一条口径。
        # 直接拿第一行的列名会让后面行多出来的值整行错位（executemany 按位置绑定）。
        columns: list[str] = []
        for payload in payloads:
            for column in payload:
                if column not in columns:
                    columns.append(column)
        if overwrite and if_exists == "ignore":
            raise SeedError(
                f"{table}: overwrite=True（INSERT OR REPLACE＝覆盖旧值）与 "
                "if_exists='ignore'（ON CONFLICT DO NOTHING＝保留旧值）语义相反，"
                "不知道你想要哪个；删掉一个再来。"
            )
        if overwrite:
            verb = "INSERT OR REPLACE"
        elif if_exists == "ignore":
            verb = "INSERT OR IGNORE"
        else:
            verb = "INSERT"
        try:
            with self._transaction() as conn:
                self._emit(conn, table, verb, columns, payloads)
        except duckdb.ConstraintException as exc:
            raise DuplicateSeed(
                f"{table} 主键 {key or '(无主键)'} 冲突（{payloads}）；"
                "同一测试重复播种请显式传 overwrite=True"
            ) from exc

    def _emit(
        self,
        conn: duckdb.DuckDBPyConnection,
        table: str,
        verb: str,
        columns: Sequence[str],
        payloads: Sequence[dict[str, Any]],
    ) -> None:
        """把一批行写进 `conn`（调用方已保证 "一次事务"）。

        没有 :class:`SqlValue` 时走 `executemany`（一次绑定 N 行）；有哨兵时
        逐行组装 SQL —— 哨兵列**内联**成白名单表达式，其余列仍是参数绑定，
        所以白名单之外的东西没有进入 SQL 文本的路径。
        """
        has_sentinel = any(
            isinstance(value, SqlValue) for payload in payloads for value in payload.values()
        )
        if not has_sentinel:
            placeholders = ", ".join("?" for _ in columns)
            sql = f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            conn.executemany(
                sql, [[payload.get(column) for column in columns] for payload in payloads]
            )
            return
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
            sql = f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join(slots)})"
            conn.execute(sql, params)
