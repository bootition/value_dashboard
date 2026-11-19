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
* 序列类表的批量写入：`shares/prices/headcounts/fundings/segments` 收
  `[(key, value…)]`，**边界值仍逐字出现在调用点**。

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
```

## 尚未覆盖（刻意不做）

SQLite 侧的 `job_logs` / `retry_list` / `watchlist` 不在本模块：本类构造时只拿
`DuckDBStore`，跨库播种会逼出一个 `insert_row` 式的万能口。需要时按同样形状
另立 SQLite 播种器。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any, Final, Literal

import duckdb

from app.core.storage.duckdb_store import DuckDBStore

#: 生产口径的溯源默认值（形状见模块 docstring）。
DEFAULT_SOURCE: Final = "csmar"
DEFAULT_CONFIDENCE: Final = "strict"
#: 固定时间戳而不是 ``now()``：重复跑同一测试得到同一份种子。
DEFAULT_FETCH_TIME: Final = "2026-09-19 00:00:00"
_DEFAULT_BATCH_ID: Final = hashlib.sha256(b"value-dashboard/test-seed").hexdigest()[:32]

#: 各表需要的溯源列组合。列名必须与**该表 schema** 一致 —— 财务附表写
#: ``raw_response_hash``，而股本/事件类写 ``raw_hash``；``share_capital_history``
#: 没有 ``fetch_time``/``confidence``，``company_employee_history`` 没有 ``confidence``。
_TRACE_CRAWL: Final = ("source", "fetch_time", "raw_response_hash", "confidence", "batch_id")
_TRACE_EVENT: Final = ("source", "fetch_time", "raw_hash", "confidence", "batch_id")
_TRACE_RAW_HASH: Final = ("source", "raw_hash", "batch_id")
_TRACE_NO_CONFIDENCE: Final = ("source", "fetch_time", "batch_id")


#: 重复播种时对已有主键的策略：``raise``（默认）/ ``ignore``（仅确保存在）。
IfExists = Literal["raise", "ignore"]


class SeedError(Exception):
    """播种器错误的基类。"""


class UnknownSeedColumn(SeedError):
    """传了该表 schema 里不存在的列名（多半是拼错）。"""


class DuplicateSeed(SeedError):
    """主键已存在。默认抛错而不是静默覆盖/静默忽略。"""


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
    """按域播种 DuckDB 表的工厂。一个实例 = 一个 store（无状态，可重复构造）。"""

    def __init__(self, store: DuckDBStore) -> None:
        self._store = store
        self._schema_columns: dict[str, frozenset[str]] = {}

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
        rows: Iterable[tuple[str, str, float]],
        *,
        overwrite: bool = False,
        **cols: Any,
    ) -> None:
        """`funding_events`：`rows` = `[(event_type, list_date, issue_shares), …]`。

        该表无主键（事件流可追加）→ 重复播种**不报错**，这是它的真实语义。
        """
        self._insert_many(
            "funding_events",
            [
                {
                    "stock_code": code,
                    "event_type": event_type,
                    "list_date": list_date,
                    "issue_shares": issue_shares,
                    **cols,
                }
                for event_type, list_date, issue_shares in rows
            ],
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

    # ── 内部：列校验 / 溯源默认值 / 写入 ─────────────────────────────
    def _columns_of(self, table: str) -> frozenset[str]:
        cached = self._schema_columns.get(table)
        if cached is not None:
            return cached
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
        key = [row[column] for column in row if column not in ("source", "confidence", "batch_id")]
        values: dict[str, Any] = {}
        for column in trace:
            if column == "source":
                values["source"] = DEFAULT_SOURCE
            elif column == "fetch_time":
                values["fetch_time"] = DEFAULT_FETCH_TIME
            elif column in ("raw_hash", "raw_response_hash"):
                values[column] = _row_hash(table, key)
            elif column == "confidence":
                values["confidence"] = DEFAULT_CONFIDENCE
            elif column == "batch_id":
                values["batch_id"] = _DEFAULT_BATCH_ID
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
        columns = list(payloads[0])
        placeholders = ", ".join("?" for _ in columns)
        if overwrite:
            verb = "INSERT OR REPLACE"
        elif if_exists == "ignore":
            verb = "INSERT OR IGNORE"
        else:
            verb = "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        try:
            with self._store.transaction() as conn:
                conn.executemany(sql, [list(payload.values()) for payload in payloads])
        except duckdb.ConstraintException as exc:
            raise DuplicateSeed(
                f"{table} 主键 {key or '(无主键)'} 冲突（{payloads}）；"
                "同一测试重复播种请显式传 overwrite=True"
            ) from exc
