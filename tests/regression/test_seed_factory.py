"""种子工厂 `tests/support/seed.py` 的常驻契约测试。

工厂存在的**主要理由**是把「拼错列名 → 静默插进一行全 NULL 种子 → 用例继续
绿着但什么都没覆盖」这类事故变成**当场红**：万能 `insert_row(table, **kwargs)`
不知道表有哪些列，`report_dte=` 会被当成"调用点忘了写 report_date"，于是
业务列全 NULL，被测算逻辑拿 NULL 当"缺失输入"，用例照样通过 —— 这是最难查的
一类假绿（`reports/140` §17.7）。上一轮工厂只有一次性脚本验证、没有常驻回归，
本文件把它钉死。

覆盖的不变式（每条都对应一类具体事故）：

1. **未知列名必须抛 `UnknownSeedColumn`，且一行都不许写** —— 拼错列名静默变 NULL。
2. **有主键的表重复播种必须抛 `DuplicateSeed`** —— "我改了参数"静默无效
   （库里还是上一次的值，断言却对着新参数写）。
3. **`overwrite=True` 必须真的覆盖** —— 免得 `INSERT OR REPLACE` 退化成
   `INSERT OR IGNORE`，"显式要求覆盖"变成空操作。
4. **无主键事件流可重复追加**（`funding_events` / `buyback_events`）——
   这两张表的真实语义就是"同一事件可多次观测"（东财 F10 会把一次增发按发行
   对象拆成多条），不能被"重复就抛"一刀切（`app/core/storage/schema.py:689`）。
5. **未传的业务列必须是 NULL** —— 工厂偷偷补默认值会让「故意缺失」那组用例假绿
   （"缺间接法 → NULL 不得估算"的整个前提就是这里缺着）。
6. **`screenable()` 是"确保股票存在"** —— `stock_meta` 用 `if_exists="ignore"`，
   同一只股票 × N 期要能反复调用；而 `income/balance` 仍按主键抛（不许静默）。
7. **默认值形状照生产口径** —— `batch_id` 32 hex、`raw_hash` 64 hex、
   `confidence ∈ {strict, approximate, missing}`。形状错的溯源值会让下游
   "有没有溯源"的判据（长度/正则/枚举）静默失效。
8. **确定性** —— 同一 (表, 主键) 播种两次必须得到同一份 hash/时间戳。
   `uuid4()` / `now()` 会让"同一份种子"每次不同，测试变成随机变量。
"""

from __future__ import annotations

import re

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import DuplicateSeed, Seed, SeedError, UnknownSeedColumn

STOCK = "600519"
REPORT_DATE = "2024-12-31"

#: 生产口径的形状（`app/core/business.py:151` 的 `uuid4().hex` 有 32 位；
#: `app/core/adapters/base.py:290` 的 sha256 有 64 位）。
HASH32 = re.compile(r"[0-9a-f]{32}")
HASH64 = re.compile(r"[0-9a-f]{64}")
#: `ConfidenceLevel`（`app/core/adapters/base.py:52`）的全部取值。
CONFIDENCE_LEVELS = frozenset({"strict", "approximate", "missing"})


def _count(store: DuckDBStore, table: str) -> int:
    return store.read_query(f"SELECT COUNT(*) AS cnt FROM {table}")[0]["cnt"]


def _one(store: DuckDBStore, table: str) -> dict:
    rows = store.read_query(f"SELECT * FROM {table}")
    assert len(rows) == 1, f"{table} 期望恰好 1 行，实际 {len(rows)} 行"
    return rows[0]


# ── 不变式 1：未知列名当场红，且不落库 ───────────────────────────────
@pytest.mark.parametrize(
    ("call", "table", "typo"),
    [
        pytest.param(
            lambda seed: seed.income(STOCK, REPORT_DATE, revenue=1000.0, report_dte=REPORT_DATE),
            "income_statement",
            "report_dte",
            id="income:report_dte",
        ),
        pytest.param(
            lambda seed: seed.balance(
                STOCK, REPORT_DATE, total_assets=1.0, total_liabilities=2.0, total_equity=3.0,
                totl_profit=4.0,
            ),
            "balance_sheet",
            "totl_profit",
            id="balance:totl_profit",
        ),
        pytest.param(
            # 财务附表写 `raw_response_hash`，抄成事件类的 `raw_hash` 是最常见的拼错之一。
            lambda seed: seed.cash_indirect(STOCK, REPORT_DATE, fixed_asset_depreciation=60.0,
                                            raw_hash="h"),
            "cash_flow_indirect",
            "raw_hash",
            id="cash_indirect:raw_hash",
        ),
        pytest.param(
            lambda seed: seed.stock(STOCK, nam="贵州茅台"),
            "stock_meta",
            "nam",
            id="stock:nam",
        ),
        pytest.param(
            lambda seed: seed.snapshot(STOCK, REPORT_DATE, latest_clos=10.0),
            "indicator_snapshot",
            "latest_clos",
            id="snapshot:latest_clos",
        ),
        pytest.param(
            lambda seed: seed.shares(STOCK, [("2024-12-31", 100.0)], total_share=100.0),
            "share_capital_history",
            "total_share",
            id="shares:total_share",
        ),
    ],
)
def test_misspelled_column_raises_unknown_seed_column_and_writes_no_row(
    duckdb_store: DuckDBStore, call, table: str, typo: str
) -> None:
    """拼错列名 MUST 抛 `UnknownSeedColumn`，且事务里一行都不许落。

    "一行都不许落"不是洁癖：半行种子会让下一条断言对着一个**部分为 NULL
    的股票**跑，看起来有数据，实际业务列全缺 —— 比直接报错难查得多。
    """
    seed = Seed(duckdb_store)

    with pytest.raises(UnknownSeedColumn) as excinfo:
        call(seed)

    # 报错必须点名拼错的列并列出可用列，否则拼错的人只能靠猜。
    assert typo in str(excinfo.value)
    assert "可用列" in str(excinfo.value)
    assert _count(duckdb_store, table) == 0


# ── 不变式 2：有主键的表重复播种必须抛 ───────────────────────────────
@pytest.mark.parametrize(
    ("call", "table"),
    [
        pytest.param(
            lambda seed: seed.income(STOCK, REPORT_DATE, revenue=1000.0),
            "income_statement",
            id="income",
        ),
        pytest.param(
            lambda seed: seed.balance(STOCK, REPORT_DATE, total_assets=1000.0),
            "balance_sheet",
            id="balance",
        ),
        pytest.param(
            lambda seed: seed.cash_indirect(STOCK, REPORT_DATE, fixed_asset_depreciation=60.0),
            "cash_flow_indirect",
            id="cash_indirect",
        ),
        pytest.param(
            lambda seed: seed.snapshot(STOCK, REPORT_DATE, latest_close=10.0),
            "indicator_snapshot",
            id="snapshot",
        ),
        pytest.param(
            lambda seed: seed.shares(STOCK, [("2024-12-31", 100.0)]),
            "share_capital_history",
            id="shares",
        ),
        pytest.param(
            lambda seed: seed.stock(STOCK, name="一次性名字"),
            "stock_meta",
            id="stock",
        ),
    ],
)
def test_duplicate_seed_raises_and_keeps_first_value(
    duckdb_store: DuckDBStore, call, table: str
) -> None:
    """重复播种 MUST 抛 `DuplicateSeed` —— 不许静默保留上一次的值。

    静默保留会让"我改了参数"变成空操作：调用点写 `revenue=2000`，库里还是
    1000，断言却按 2000 的预期在写，红得莫名其妙（或者更糟：恰好绿）。
    """
    seed = Seed(duckdb_store)
    call(seed)

    with pytest.raises(DuplicateSeed, match=table):
        call(seed)

    assert _count(duckdb_store, table) == 1


# ── 不变式 3：overwrite=True 真的覆盖 ────────────────────────────────
def test_overwrite_true_replaces_the_existing_row_instead_of_ignoring_it(
    duckdb_store: DuckDBStore,
) -> None:
    """`overwrite=True` 必须换掉值，且不留下第二行。

    注意覆盖的**粒度**：DuckDB 的 `INSERT OR REPLACE` 是 upsert，只更新本次
    提供的列。`selling_expenses` 第二次没传 → 保留上一次的 100.0，不是 NULL。
    这条钉在这里是为了让人**看见**它：以为"覆盖 = 整行替换"的人会写出
    "我改了 revenue，旧费用还在"的种子而毫无察觉。需要整行干净重来就先把行删掉。
    """
    seed = Seed(duckdb_store)
    seed.income(STOCK, REPORT_DATE, revenue=1000.0, net_profit=300.0, selling_expenses=100.0)
    seed.income(STOCK, REPORT_DATE, revenue=2000.0, net_profit=500.0, overwrite=True)

    rows = duckdb_store.read_query(
        "SELECT revenue, net_profit, selling_expenses FROM income_statement"
    )
    assert rows == [{"revenue": 2000.0, "net_profit": 500.0, "selling_expenses": 100.0}]

    seed.stock(STOCK, name="旧名")
    seed.stock(STOCK, name="新名", overwrite=True)
    assert duckdb_store.read_query("SELECT name FROM stock_meta") == [{"name": "新名"}]


# ── 不变式 4：无主键事件流可重复追加 ────────────────────────────────
def test_keyless_event_streams_accept_repeated_seeding(duckdb_store: DuckDBStore) -> None:
    """`funding_events` / `buyback_events` 无主键 —— 同一行重复播种必须都留下。

    这是表的真实语义，不是工厂疏忽：东财 F10 会把一次增发按发行对象拆成多条
    同 `list_date` 记录，单股原子替换靠 `DELETE→INSERT`、不靠主键去重
    （`app/core/storage/schema.py:689` 的注释）。
    """
    seed = Seed(duckdb_store)
    fundings = [("a_placement", "2026-02-05", 1000.0), ("a_placement", "2026-02-05", 2000.0)]

    seed.fundings(STOCK, fundings)
    seed.fundings(STOCK, fundings)
    seed.buyback(STOCK, "2026-03-01", 500.0)
    seed.buyback(STOCK, "2026-03-01", 500.0)

    assert _count(duckdb_store, "funding_events") == 4
    assert _count(duckdb_store, "buyback_events") == 2


# ── 不变式 5：未传的业务列必须是 NULL ────────────────────────────────
def test_business_columns_not_passed_stay_null(duckdb_store: DuckDBStore) -> None:
    """工厂不给业务列设默认值 —— 不写就是 NULL。

    「缺间接法 → EBITDA 必须 NULL，不得估算」这类用例的全部前提，就是种子
    里那几个列**真的**是 NULL。工厂一旦补个 `0.0`，它们会变成"算出来是 0"
    另一件事，而且仍然绿。
    """
    seed = Seed(duckdb_store)
    seed.income(STOCK, REPORT_DATE, revenue=1000.0)
    seed.balance(STOCK, REPORT_DATE, total_assets=2000.0)
    seed.snapshot(STOCK, REPORT_DATE, latest_close=10.0)

    income = _one(duckdb_store, "income_statement")
    assert income["revenue"] == 1000.0
    assert income["net_profit"] is None
    assert income["selling_expenses"] is None
    assert income["total_profit"] is None
    assert income["raw_data"] is None

    balance = _one(duckdb_store, "balance_sheet")
    assert balance["total_assets"] == 2000.0
    assert balance["accounts_receivable"] is None
    assert balance["total_equity"] is None

    snapshot = _one(duckdb_store, "indicator_snapshot")
    assert snapshot["latest_close"] == 10.0
    assert snapshot["pe_ttm"] is None


# ── 不变式 6：screenable() 的"确保股票存在"语义 ─────────────────────
def test_screenable_keeps_stock_meta_when_the_same_stock_is_seeded_for_more_periods(
    duckdb_store: DuckDBStore,
) -> None:
    """同一只股票 × N 期可以反复调用 `screenable()`，`stock_meta` 不被第二遍打死。"""
    seed = Seed(duckdb_store)

    seed.screenable(
        STOCK,
        "2024-12-31",
        income={"revenue": 1000.0, "net_profit": 300.0},
        balance={"total_assets": 2000.0, "total_equity": 1200.0},
        shares=[("2000-01-01", 100.0)],
        price=10.0,
    )
    seed.screenable(
        STOCK,
        "2024-09-30",
        income={"revenue": 900.0, "net_profit": 250.0},
        balance={"total_assets": 1900.0, "total_equity": 1150.0},
        price=9.0,
    )

    assert _count(duckdb_store, "stock_meta") == 1
    assert _count(duckdb_store, "income_statement") == 2
    assert _count(duckdb_store, "share_capital_history") == 1
    # 第二次调用不该把第一次的价格按同一个主键再插一遍，也不该丢掉它。
    assert duckdb_store.read_query(
        "SELECT CAST(trade_date AS VARCHAR) AS d, close FROM price_daily_raw ORDER BY d"
    ) == [{"d": "2024-09-30", "close": 9.0}, {"d": "2024-12-31", "close": 10.0}]


def test_screenable_omitted_segments_insert_nothing_not_zero(duckdb_store: DuckDBStore) -> None:
    """`shares=None` / `price=None` / `cash_flow=None` 时**什么都不插**（不是插 0）。"""
    Seed(duckdb_store).screenable(
        STOCK, REPORT_DATE, income={"revenue": 1000.0}, balance={"total_assets": 2000.0}
    )

    assert _count(duckdb_store, "income_statement") == 1
    assert _count(duckdb_store, "balance_sheet") == 1
    assert _count(duckdb_store, "cash_flow") == 0
    assert _count(duckdb_store, "share_capital_history") == 0
    assert _count(duckdb_store, "price_daily_raw") == 0


def test_screenable_second_call_for_the_same_period_still_raises(
    duckdb_store: DuckDBStore,
) -> None:
    """只有 `stock_meta` 是"确保存在"；同一个报告期再播一次财务必须抛。

    否则"我把 revenue 从 1000 改成 2000"会静默无效 —— `screenable()` 不是
    `upsert_screenable()`。
    """
    seed = Seed(duckdb_store)
    seed.screenable(STOCK, REPORT_DATE, income={"revenue": 1000.0}, balance={"total_assets": 1.0})

    with pytest.raises(DuplicateSeed, match="income_statement"):
        seed.screenable(
            STOCK, REPORT_DATE, income={"revenue": 2000.0}, balance={"total_assets": 1.0}
        )


# ── 不变式 7：默认值形状照生产口径 ───────────────────────────────────
def test_provenance_defaults_match_the_production_shapes(duckdb_store: DuckDBStore) -> None:
    """`batch_id` 32 hex、`raw_response_hash` 64 hex、`confidence` 在枚举内。

    形状错的溯源值不会报错，只会让下游"这条数据有没有溯源"的判据静默失效
    （按长度/正则/枚举判定时，短字符串会被当成"没有溯源"而回退估算）。
    """
    seed = Seed(duckdb_store)
    seed.cash_indirect(STOCK, REPORT_DATE, fixed_asset_depreciation=60.0)
    seed.fundings(STOCK, [("ipo", "2025-01-01", 1000.0)])

    indirect = _one(duckdb_store, "cash_flow_indirect")
    assert HASH32.fullmatch(indirect["batch_id"])
    assert HASH64.fullmatch(indirect["raw_response_hash"])
    assert indirect["confidence"] in CONFIDENCE_LEVELS
    assert indirect["confidence"] == "strict"
    assert indirect["source"] == "csmar"
    assert indirect["fetch_time"] is not None

    funding = _one(duckdb_store, "funding_events")
    assert HASH32.fullmatch(funding["batch_id"])
    assert HASH64.fullmatch(funding["raw_hash"])
    assert funding["confidence"] in CONFIDENCE_LEVELS


# ── 不变式 8：确定性 ─────────────────────────────────────────────────
def test_default_values_are_deterministic_for_the_same_row(duckdb_store: DuckDBStore) -> None:
    """同一 (表, 主键) 两次播种得到同一 hash、同一时间戳 —— 不是 `uuid4()` / `now()`。"""
    seed = Seed(duckdb_store)
    seed.cash_indirect(STOCK, REPORT_DATE, fixed_asset_depreciation=60.0)
    first = _one(duckdb_store, "cash_flow_indirect")

    duckdb_store.write_query("DELETE FROM cash_flow_indirect")
    # 换一个**新实例**再播一遍：hash 不能是构造时随机生成、实例内固定的值。
    Seed(duckdb_store).cash_indirect(STOCK, REPORT_DATE, fixed_asset_depreciation=60.0)
    second = _one(duckdb_store, "cash_flow_indirect")

    assert first["raw_response_hash"] == second["raw_response_hash"]
    assert first["batch_id"] == second["batch_id"]
    assert first["fetch_time"] == second["fetch_time"]

    # 但每行互不相同：hash 不能退化成一个常量（那样它就只是"有个字段"而已）。
    seed.cash_indirect("000001", REPORT_DATE, fixed_asset_depreciation=60.0)
    other = duckdb_store.read_query(
        "SELECT raw_response_hash FROM cash_flow_indirect WHERE stock_code = '000001'"
    )[0]
    assert other["raw_response_hash"] != first["raw_response_hash"]


# ── 附加：表名收口 ───────────────────────────────────────────────────
def test_prices_rejects_a_table_outside_the_price_pair(duckdb_store: DuckDBStore) -> None:
    """`prices()` 只认原始/前复权两张日线表 —— 收个任意表名等于把万能口放回来。"""
    seed = Seed(duckdb_store)

    with pytest.raises(SeedError, match="只接受原始/前复权日线表"):
        seed.prices(STOCK, [("2024-12-31", 10.0)], table="income_statement")

    assert _count(duckdb_store, "income_statement") == 0
