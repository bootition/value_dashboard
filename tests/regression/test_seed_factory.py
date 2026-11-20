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
from datetime import datetime, timedelta

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import (
    CURRENT_TIMESTAMP,
    DEFAULT_FETCH_TIME,
    DuplicateSeed,
    Seed,
    SeedError,
    SqlValue,
    UnknownSeedColumn,
    sql_now,
)

STOCK = "600519"
REPORT_DATE = "2024-12-31"
#: `indicator_ext` 的 `calculated_at` / `source` / `data_version` 是 NOT NULL 且
#: 不是 provenance 五件套 —— 由调用点显式给（工厂方法签名里就是这么要求的）。
EXT_CALCULATED_AT = "2026-09-19 00:00:00"

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
        pytest.param(
            # 扩展指标域（`indicator_ext`）是为压缩 asset_health 的覆盖率用例才补的：
            # 它与 `indicator_snapshot` 同主键但不同表，工厂必须**分开**校验列。
            lambda seed: seed.extended(
                STOCK, REPORT_DATE, calculated_at=EXT_CALCULATED_AT, source="fixture",
                data_version=1, free_cash_flo=1.0,
            ),
            "indicator_ext",
            "free_cash_flo",
            id="extended:free_cash_flo",
        ),
        pytest.param(
            # 分红：主机列是 ex_date，最容易被写成 `dividend_per_share` 的近似拼法。
            lambda seed: seed.dividends(
                STOCK, [("2025-06-30", None, 1.0)], dividend_per_shar=1.0,
            ),
            "dividends",
            "dividend_per_shar",
            id="dividends:dividend_per_shar",
        ),
        pytest.param(
            lambda seed: seed.treasury_yield_curve("2026-08-31", 10.0, 3.0, yield_pc=3.0),
            "treasury_yield_curve",
            "yield_pc",
            id="treasury_yield_curve:yield_pc",
        ),
        pytest.param(
            # `source_audit` 的批次列叫 `fetch_batch_id`（不是别处的 `batch_id`），
            # 这张表最常见的拼错就是漏掉 `_id` —— 必须当场红，不能静默 NULL。
            lambda seed: seed.source_audit(STOCK, "revenue", fetch_batch="b1"),
            "source_audit",
            "fetch_batch",
            id="source_audit:fetch_batch",
        ),
        pytest.param(
            # 逐行 cols（`fundings` 第四项）与全批 `**cols` 都要走同一套列校验。
            lambda seed: seed.fundings(
                STOCK, [("ipo", "2025-01-01", 1000.0, {"raise_fund": 1.0})],
            ),
            "funding_events",
            "raise_fund",
            id="fundings:per_row_raise_fund",
        ),
        pytest.param(
            # 报告期公告日只有两列业务列，`announce_date` 是最容易被缩写成
            # `announce_dt` 的那一列 —— 拼错后该行公布日就成 NULL（NOT NULL 会拦，
            # 但拦在库里而不是调用点）。
            lambda seed: seed.report_dates(
                STOCK, [(REPORT_DATE, "2025-04-20")], announce_dt="2025-04-20",
            ),
            "financial_report_dates",
            "announce_dt",
            id="report_dates:announce_dt",
        ),
        pytest.param(
            # `etf_daily` 的收盘列叫 `close_price`（不是日线的 `close`）：
            # 抄成 `close` 会静默把价格写成 NULL，`latest_close` 类断言当场空转。
            lambda seed: seed.etf_daily("510300", [("2026-09-01", 1.0)], close=1.0),
            "etf_daily",
            "close",
            id="etf_daily:close",
        ),
        pytest.param(
            # 港股分红的原文计划列叫 `plan_explain`（拼错后虽然是主键列会 NOT NULL，
            # 但报错必须点在调用点的列名上）。
            lambda seed: seed.hk_dividends(
                "00941", [("2026-08-25", "每股派港币0.2元", 0.2, None)],
                plan_explan="每股派港币0.2元",
            ),
            "hk_dividends",
            "plan_explan",
            id="hk_dividends:plan_explan",
        ),
        pytest.param(
            # 指数估值的口径列叫 `pe_metric`（不是 `metric`/`pe_type`）。
            lambda seed: seed.index_valuation(
                "000300", [("2026-07-31", 14.6)], source="legulegu", metric="ttm",
            ),
            "index_valuation",
            "metric",
            id="index_valuation:metric",
        ),
        pytest.param(
            # 公司概览的行业列叫 `csrc_industry`：少一个 `r` 是最常见的拼错。
            lambda seed: seed.company_profile(STOCK, name="贵州茅台", csrc_industy="制造业"),
            "company_profile",
            "csrc_industy",
            id="company_profile:csrc_industy",
        ),
        pytest.param(
            # 金融股专用科目：银行模板列叫 `customer_deposits`（复数 s），
            # 拼成单数后该科目静默 NULL —— 跨行业比较会当成"这家银行没有存款"。
            lambda seed: seed.financial_sector_item(
                "601398", "2026-09-30", customer_deposit=1.0,
            ),
            "financial_sector_items",
            "customer_deposit",
            id="financial_sector_item:customer_deposit",
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
        pytest.param(
            lambda seed: seed.extended(
                STOCK, REPORT_DATE, calculated_at=EXT_CALCULATED_AT, source="fixture",
                data_version=1, free_cash_flow=1.0,
            ),
            "indicator_ext",
            id="extended",
        ),
        pytest.param(
            lambda seed: seed.dividends(STOCK, [("2025-06-30", "2025-05-01", 1.0)]),
            "dividends",
            id="dividends",
        ),
        pytest.param(
            lambda seed: seed.treasury_yield_curve("2026-08-31", 10.0, 3.0),
            "treasury_yield_curve",
            id="treasury_yield_curve",
        ),
        pytest.param(
            lambda seed: seed.report_dates(STOCK, [(REPORT_DATE, "2025-04-20")]),
            "financial_report_dates",
            id="report_dates",
        ),
        pytest.param(
            lambda seed: seed.etf_daily("510300", [("2026-09-01", 1.0)]),
            "etf_daily",
            id="etf_daily",
        ),
        pytest.param(
            lambda seed: seed.hk_dividends(
                "00941", [("2026-08-25", "每股派港币0.2元", 0.2, None)]
            ),
            "hk_dividends",
            id="hk_dividends",
        ),
        pytest.param(
            # `source` 进主键：同一 (指数, 交易日) 换源是**另一行**，
            # 所以只有同一 source 再播一次才是重复。
            lambda seed: seed.index_valuation(
                "000300", [("2026-07-31", 14.6)], source="legulegu"
            ),
            "index_valuation",
            id="index_valuation",
        ),
        pytest.param(
            lambda seed: seed.company_profile(STOCK, name="贵州茅台"),
            "company_profile",
            id="company_profile",
        ),
        pytest.param(
            # 主键 `(stock_code, report_date)`：同一金融股同一报告期只能一行，
            # 重复播种必须抛而不是静默留旧值（补充管线按源删插，撞 PK 就是事故）。
            lambda seed: seed.financial_sector_item(
                "601398", "2026-09-30", customer_deposits=1.0
            ),
            "financial_sector_items",
            id="financial_sector_item",
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
    反面 `if_exists="ignore"`（`ON CONFLICT DO NOTHING`）**保留**旧值 ——
    "确保存在"不是"改成我的值"，两者不能互换（下面用 `dividends` 各钉一遍）。
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

    # 反面同理：`if_exists="ignore"`（`ON CONFLICT DO NOTHING`）与 `overwrite=True`
    # **相反** —— ignore 保留旧值，那是"确保存在"，不是"改成我的值"。收敛
    # "已核验的分红不得被稀疏响应清零"这类种子时，拿 overwrite 凑会静默改掉
    # 被断言守护的值；两者同时传等于调用点没说清要哪个 → `SeedError`，一行不落。
    seed.dividends(STOCK, [("2025-06-30", None, 1.0)])
    seed.dividends(STOCK, [("2025-06-30", None, 9.9)], if_exists="ignore")
    assert duckdb_store.read_query("SELECT dividend_per_share FROM dividends") == [
        {"dividend_per_share": 1.0}
    ]

    seed.dividends(STOCK, [("2025-06-30", None, 2.0)], overwrite=True)
    assert duckdb_store.read_query("SELECT dividend_per_share FROM dividends") == [
        {"dividend_per_share": 2.0}
    ]

    with pytest.raises(SeedError, match="语义相反"):
        seed.dividends(
            STOCK, [("2025-06-30", None, 3.0)], overwrite=True, if_exists="ignore"
        )
    assert duckdb_store.read_query("SELECT dividend_per_share FROM dividends") == [
        {"dividend_per_share": 2.0}
    ]


# ── 不变式 4：无主键事件流可重复追加 ────────────────────────────────
def test_keyless_event_streams_accept_repeated_seeding(duckdb_store: DuckDBStore) -> None:
    """`funding_events` / `buyback_events` / `source_audit` 无主键 —— 重复播种必须都留下。

    这是表的真实语义，不是工厂疏忽：东财 F10 会把一次增发按发行对象拆成多条
    同 `list_date` 记录，单股原子替换靠 `DELETE→INSERT`、不靠主键去重
    （`app/core/storage/schema.py:689` 的注释）；`source_audit` 的主键只有自增
    `id`，同一 `(stock, field)` 每次抓取各留一条审计 —— "重复就抛"会把这些
    用例逼回内联 SQL。
    """
    seed = Seed(duckdb_store)
    fundings = [("a_placement", "2026-02-05", 1000.0), ("a_placement", "2026-02-05", 2000.0)]

    seed.fundings(STOCK, fundings)
    seed.fundings(STOCK, fundings)
    seed.buyback(STOCK, "2026-03-01", 500.0)
    seed.buyback(STOCK, "2026-03-01", 500.0)
    seed.source_audit(STOCK, "revenue", raw_response_hash="a" * 64)
    seed.source_audit(STOCK, "revenue", raw_response_hash="b" * 64)

    assert _count(duckdb_store, "funding_events") == 4
    assert _count(duckdb_store, "buyback_events") == 2
    assert _count(duckdb_store, "source_audit") == 2


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

    # 分红：「未证公告日」= `announcement_date` 真的缺着（不是被工厂填成 ex_date）。
    # 股东回报指标整项缺失那一组用例的全部前提就在这一行。
    seed.dividends(STOCK, [("2025-06-30", None, 1.0)])
    dividend = _one(duckdb_store, "dividends")
    assert dividend["dividend_per_share"] == 1.0
    assert dividend["announcement_date"] is None
    assert dividend["stock_dividend"] is None
    assert dividend["transfer_share"] is None

    # 审计：业务列（报告期/值/原因码）不写就是 NULL；`created_at` 由 schema 的
    # `DEFAULT CURRENT_TIMESTAMP` 兜底 —— 工厂不替它编，也不塞一个假时间。
    seed.source_audit(STOCK, "revenue")
    audit = _one(duckdb_store, "source_audit")
    assert audit["report_date"] is None
    assert audit["value"] is None
    assert audit["reason_code"] is None
    assert audit["api_version"] is None
    assert audit["created_at"] is not None

    # 融资事件：金额/价格不在 `rows` 里就是 NULL（`raise_funds` 缺失 → fail-closed）。
    seed.fundings(STOCK, [("ipo", "2025-01-01", 1000.0)])
    funding = _one(duckdb_store, "funding_events")
    assert funding["issue_shares"] == 1000.0
    assert funding["issue_price"] is None
    assert funding["raise_funds"] is None
    assert funding["raise_funds_net"] is None

    # 报告期公告日：只有日期列，`source` 落到财务域的真实来源（csmar）而不是
    # `_TRACE_EVENT` 那套 hash 默认值 —— 该表 schema 里就没有 hash 列。
    seed.report_dates(STOCK, [(REPORT_DATE, "2025-04-20")])
    report_date = _one(duckdb_store, "financial_report_dates")
    assert str(report_date["announce_date"]) == "2025-04-20"
    assert report_date["source"] == "csmar"
    assert HASH32.fullmatch(report_date["batch_id"])

    # ETF 行情：`close_price` 之外的价格/成交列不写就是 NULL（不是 0.0）——
    # "只有收盘价"的种子必须让"缺量能"的判据真的看见缺口。
    seed.etf_daily("510300", [("2026-09-01", 1.5)])
    etf = _one(duckdb_store, "etf_daily")
    assert etf["close_price"] == 1.5
    assert etf["open_price"] is None
    assert etf["high_price"] is None
    assert etf["volume"] is None
    assert etf["turnover"] is None
    assert etf["track_pe_ttm_five_year_percentile"] is None
    assert etf["source"] == "ths"

    # 港股分红：`dividend_per_share_cny` 允许真的缺着（不按汇率替你换算），
    # 公告日/报告期/过户日/派息日不写就是 NULL。
    seed.hk_dividends("00941", [("2026-08-25", "每股派港币0.2元", 0.2, None)])
    hk = _one(duckdb_store, "hk_dividends")
    assert hk["dividend_per_share_hkd"] == 0.2
    assert hk["dividend_per_share_cny"] is None
    assert hk["announcement_date"] is None
    assert hk["report_period"] is None
    assert hk["transfer_end_date"] is None
    assert hk["dividend_date"] is None
    assert hk["source"] == "eastmoney_hk_dividend"

    # 指数估值：只给"交易日 + PE"就是一张干净的估值曲线，其余口径列全 NULL
    # （`pe_metric` 缺着比编一个 `ttm` 更诚实：口径未知 ≠ ttm）。
    seed.index_valuation("000300", [("2026-07-31", 14.6)], source="legulegu")
    index_row = _one(duckdb_store, "index_valuation")
    assert index_row["pe_ttm"] == 14.6
    assert index_row["pe_metric"] is None
    assert index_row["pb"] is None
    assert index_row["div_yield"] is None
    assert index_row["extra"] is None
    assert index_row["source"] == "legulegu"

    # 公司概览：业务事实列一个都不补（"这次的响应没带简介"必须留成 NULL）；
    # `updated_at` 由 schema 的 `DEFAULT CURRENT_TIMESTAMP` 兜底 —— 工厂不编。
    seed.company_profile(STOCK)
    profile = _one(duckdb_store, "company_profile")
    assert profile["code"] is None
    assert profile["name"] is None
    assert profile["org_name"] is None
    assert profile["profile"] is None
    assert profile["scope"] is None
    assert profile["employee_num"] is None
    assert profile["csrc_industry"] is None
    assert profile["trade_market"] is None
    assert profile["source"] == "eastmoney_f10"
    assert profile["updated_at"] is not None

    # 金融股专用科目：银行模板只给"吸收存款"，保险/证券那一段必须真的是 NULL
    # （不是 0.0）—— 「这家银行没有已赚保费」与「已赚保费为 0」是两件事，
    # 后者会让跨行业聚合把不适用科目当成真实 0 计入。
    seed.financial_sector_item("601398", "2026-09-30", customer_deposits=100.0)
    sector = _one(duckdb_store, "financial_sector_items")
    assert sector["customer_deposits"] == 100.0
    assert sector["earned_premiums"] is None
    assert sector["insurance_contract_liability"] is None
    assert sector["fee_commission_income_net"] is None
    assert sector["settlement_reserve"] is None
    assert sector["report_type"] is None
    # `source` 默认东财 F10：这张表 2025Q2+ 的热数据只有这条管线写。
    assert sector["source"] == "eastmoney_f10"
    assert HASH64.fullmatch(sector["raw_response_hash"])
    assert HASH32.fullmatch(sector["batch_id"])


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

    # `trace_now=True` 是唯一的**显式**出口：只有它把 `fetch_time` 写成"这一刻"。
    # 新鲜度类用例（"刚抓的""比快照晚"）靠这个开关收敛，否则只能退回内联
    # `INSERT ... CURRENT_TIMESTAMP`。作用域仅限 fetch_time —— hash/batch_id 照旧。
    Seed(duckdb_store, trace_now=True).treasury_yield_curve("2026-08-31", 10.0, 3.0)
    fresh = _one(duckdb_store, "treasury_yield_curve")
    assert abs(datetime.now() - fresh["fetch_time"]) < timedelta(minutes=5)
    assert HASH64.fullmatch(fresh["raw_hash"])
    assert HASH32.fullmatch(fresh["batch_id"])

    # 而默认（不开开关）仍是那个固定字面量：同一份种子换实例重复播也一致。
    assert first["fetch_time"] == datetime.fromisoformat(DEFAULT_FETCH_TIME)


# ── 附加：表名收口 ───────────────────────────────────────────────────
def test_prices_rejects_a_table_outside_the_price_pair(duckdb_store: DuckDBStore) -> None:
    """`prices()` 只认原始/前复权两张日线表 —— 收个任意表名等于把万能口放回来。"""
    seed = Seed(duckdb_store)

    with pytest.raises(SeedError, match="只接受原始/前复权日线表"):
        seed.prices(STOCK, [("2024-12-31", 10.0)], table="income_statement")

    assert _count(duckdb_store, "income_statement") == 0


# ── A：`join(conn)` —— 调用点已开事务时，工厂不许自己再开（写锁自撞）──────
class _NoWriteLockStore:
    """把"工厂又去申请写锁/事务"变成**立刻失败**的假 store（而不是死等 30s）。

    DuckDB 的写锁是应用级文件锁：在 `store.transaction()` 块里再调
    `store.transaction()` 会自己等自己（实测 ~30s 后 `DuckDBWriteLockError`）。
    用真实 store 验证"没自撞"要付 30s 的代价，用这个假 store 付 0s，
    而且失败信息直指原因。它把每次 store 侧调用都记进 `calls`，用来证明
    `join()` 之后一条 store 调用都没发生。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def transaction(self, *args: object, **kwargs: object) -> None:
        self.calls.append("transaction")
        raise AssertionError("join(conn) 之后不许再申请写事务/写锁")

    def write_connection(self, *args: object, **kwargs: object) -> None:
        self.calls.append("write_connection")
        raise AssertionError("join(conn) 之后不许再申请写连接/写锁")

    def read_connection(self, *args: object, **kwargs: object) -> None:
        self.calls.append("read_connection")
        raise AssertionError("join(conn) 之后不许另开读连接（DESCRIBE 用绑定连接）")


def test_join_never_acquires_the_write_lock(duckdb_store: DuckDBStore) -> None:
    """`join(conn)` 绑定后 MUST 一次 `transaction()` / `write_connection()` 都不申请。

    这是缺口 A 的全部意义：18+ 处内联播种之所以留在
    `with store.write_connection():` / `store.transaction():` 块里，就是因为
    工厂自带事务会**自撞写锁**。假 store 在 store 侧任何调用上直接抛
    `AssertionError`，所以这条用例同时钉住两件事：方法真的写进了库（断言在
    真 store 上读得到），且过程中没有任何一次锁申请。
    """
    no_lock = _NoWriteLockStore()
    with duckdb_store.write_connection() as conn:
        Seed(no_lock).join(conn).stock(STOCK, name="贵州茅台")  # type: ignore[arg-type]
        Seed(no_lock).join(conn).income(STOCK, REPORT_DATE, revenue=1000.0)  # type: ignore[arg-type]

    assert no_lock.calls == []
    assert duckdb_store.read_query(
        "SELECT name FROM stock_meta WHERE stock_code = ?", [STOCK]
    ) == [{"name": "贵州茅台"}]
    assert _count(duckdb_store, "income_statement") == 1


def test_joined_seed_defers_commit_to_the_caller(duckdb_store: DuckDBStore) -> None:
    """绑定的 façade **不提交**：调用方回滚，工厂写进去的行必须一起消失。

    "绑定了就当我 commit 了"是这类 façade 最容易撒的谎：调用方后半段抛错、
    `with` 回滚，而种子留在库里 —— 之后每条断言都对着一个"上一轮成功写的"
    世界跑。这条用例用真事务 + 真回滚钉死它。
    """

    class _Boom(Exception):
        pass

    # 一个 `with` 里同时"期待 Boom"和"持有真事务"：抛错时 `transaction()` 的
    # finally 走 rollback —— 这正是要验证的那条路径。
    with pytest.raises(_Boom), duckdb_store.transaction() as conn:
        seed = Seed(duckdb_store).join(conn)
        seed.stock(STOCK, name="贵州茅台")
        seed.balance(STOCK, REPORT_DATE, total_assets=2000.0)
        raise _Boom

    assert _count(duckdb_store, "stock_meta") == 0
    assert _count(duckdb_store, "balance_sheet") == 0


def test_joined_seed_keeps_column_validation_and_duplicate_detection(
    duckdb_store: DuckDBStore,
) -> None:
    """绑定后口径不变：拼错列名照样 `UnknownSeedColumn`，重复主键照样 `DuplicateSeed`。

    "在调用方事务里"不是放松校验的理由 —— 恰恰相反：块里通常后面还有别的写，
    一行错种子落进去就是"整块都脏"。
    """
    with duckdb_store.write_connection() as conn:
        seed = Seed(duckdb_store).join(conn)

        with pytest.raises(UnknownSeedColumn, match="report_dte"):
            seed.balance(STOCK, REPORT_DATE, report_dte=REPORT_DATE, total_assets=1.0)

        seed.balance(STOCK, REPORT_DATE, total_assets=2000.0)
        with pytest.raises(DuplicateSeed, match="balance_sheet"):
            seed.balance(STOCK, REPORT_DATE, total_assets=3000.0)

    assert _count(duckdb_store, "balance_sheet") == 1
    assert _one(duckdb_store, "balance_sheet")["total_assets"] == 2000.0


# ── C：库侧时钟哨兵（白名单表达式）────────────────────────────────────
def _within_a_minute_of_the_database_clock(store: DuckDBStore) -> int:
    """数出 `indicator_snapshot.calculated_at` 落在库侧"现在 ±1 分钟"的行数。

    为什么不是 `calculated_at = CURRENT_TIMESTAMP`：DuckDB 的 `CURRENT_TIMESTAMP`
    在每条语句里各自求值（微秒精度），两条语句永远不会"相等"；把等号当判据，
    用例只会在**写死字面量**时才绿 —— 那恰好是这条要排除的写法。
    """
    return store.read_query(
        "SELECT COUNT(*) AS c FROM indicator_snapshot "
        "WHERE calculated_at BETWEEN CURRENT_TIMESTAMP - INTERVAL 1 MINUTE "
        "AND CURRENT_TIMESTAMP + INTERVAL 1 MINUTE"
    )[0]["c"]


def test_sql_now_sentinel_writes_the_database_clock(duckdb_store: DuckDBStore) -> None:
    """`calculated_at=CURRENT_TIMESTAMP` 必须落成**库的现在**，不是 Python 字面量。

    新鲜度判据（`WHERE calculated_at > now() - INTERVAL ...`）要的是库侧时钟：
    写死字面量会让"刚算过"在跑测试的第二天悄悄变成"很久没算"；写 Python 的
    `datetime.now()` 又会带进本机时区。这条钉住哨兵真的走了 SQL 函数。
    """
    Seed(duckdb_store).snapshot(STOCK, REPORT_DATE, calculated_at=CURRENT_TIMESTAMP)

    row = duckdb_store.read_query(
        "SELECT calculated_at FROM indicator_snapshot"
    )[0]
    assert isinstance(row["calculated_at"], datetime)
    assert abs(datetime.now() - row["calculated_at"].replace(tzinfo=None)) < timedelta(minutes=5)
    # 库侧自比：`CURRENT_TIMESTAMP` 在 DuckDB 里是**语句级**求值（微秒精度），
    # 所以用窗口而不是等号 —— 等号只会钉住"两条语句跑在同一微秒"这个假条件。
    assert _within_a_minute_of_the_database_clock(duckdb_store) == 1


@pytest.mark.parametrize(
    "expression",
    [
        "now()",
        "CURRENT_TIMESTAMP; DROP TABLE stock_meta",
        "datetime('now', 'localtime')",
        "1=1",
        "(SELECT MAX(fetch_time) FROM treasury_yield_curve)",
        "strftime('%s','now')",
    ],
)
def test_sql_sentinel_rejects_anything_outside_the_whitelist(expression: str) -> None:
    """哨兵 MUST 只收白名单表达式 —— 任意 SQL 字符串既是注入面又是万能口。

    白名单之外的东西进不了 SQL 文本（`SqlValue.__init__` 当场抛），所以"某一列
    交给库函数"这件事永远不会退化成 `seed.raw_sql("...")`。
    """
    with pytest.raises(SeedError):
        SqlValue(expression)


def test_sql_now_modifier_helper_rejects_arbitrary_text() -> None:
    """`sql_now()` 的修饰符也只收 `±N second|minute|hour|day|month|year`。"""
    assert sql_now("-200 days").sql == "datetime('now', '-200 days')"
    assert sql_now().sql == "datetime('now')"
    for bad in ("+1 day') OR 1=1 --", "localtime", "-200 days' ); DELETE FROM x --", "1 day ago"):
        with pytest.raises(SeedError):
            sql_now(bad)


# ── D：批量脚手架（一次事务 + executemany），与逐行等价 ────────────────
def test_stocks_bulk_equals_row_by_row_seeding(duckdb_store: DuckDBStore) -> None:
    """`stocks([...])` 必须与逐行 `stock()` 得到**完全相同**的表内容。

    批量入口只省"怎么写进去"（一次事务 + `executemany`），不许顺带改口径：
    名字兜底、交易所推断、逐行列优先级都得与单行路径一致，否则池子类用例
    （6000 行）与其余用例会长在两套语义上。
    """
    codes = ["600000", "600001", "000001", "830001"]
    rows = [
        (code, f"per-row-{code}", {"listing_date": "2020-01-01"}) for code in codes
    ]
    select = (
        "SELECT stock_code, name, exchange, CAST(listing_date AS VARCHAR) AS listing_date "
        "FROM stock_meta ORDER BY stock_code"
    )

    seed = Seed(duckdb_store)
    for code in codes:
        seed.stock(code, name=f"per-row-{code}", listing_date="2020-01-01")
    row_by_row = duckdb_store.read_query(select)

    # 同一个库上再播一遍会撞主键：批量路径用 overwrite=True 覆盖同一批主键。
    Seed(duckdb_store).stocks(rows, overwrite=True)
    assert duckdb_store.read_query(select) == row_by_row

    duckdb_store.write_query("DELETE FROM stock_meta")
    Seed(duckdb_store).stocks(rows)
    assert duckdb_store.read_query(select) == row_by_row
    # 交易所按代码段推断：批量路径不许把它写死成某一个值。
    assert {row["exchange"] for row in duckdb_store.read_query(select)} == {"SHSE", "SZSE", "BSE"}


def test_bulk_scaffolds_validate_columns_and_primary_keys(duckdb_store: DuckDBStore) -> None:
    """批量入口同样过列校验与主键判据：拼错列名一行都不落，重复主键当场抛。

    `executemany` 最容易丢的就是这两道闸（"批量嘛，先塞进去再说"）：拼错的列名
    在 `executemany` 里会整批失败、或者更糟——被当成新列名忽略；而重复主键要么
    整批回滚、要么静默留旧值，两种都不该发生。
    """
    seed = Seed(duckdb_store)

    with pytest.raises(UnknownSeedColumn, match="nmae"):
        seed.stocks([("600000", "a"), ("600001", "b")], nmae="x")
    assert _count(duckdb_store, "stock_meta") == 0

    with pytest.raises(SeedError, match="主键列"):
        seed.snapshots([{"stock_code": "600000"}])

    seed.prices_bulk([("600000", "2024-12-31", 10.0), ("600001", "2024-12-31", 20.0)])
    assert _count(duckdb_store, "price_daily_raw") == 2
    with pytest.raises(DuplicateSeed, match="price_daily_raw"):
        seed.prices_bulk([("600000", "2024-12-31", 11.0)])


def test_snapshots_bulk_seeds_many_rows_with_the_database_clock(
    duckdb_store: DuckDBStore,
) -> None:
    """`snapshots([...])` 是 6000 行池子的批量面：每行只给列，哨兵照样生效。

    逐行调 `snapshot()` = 6000 次写锁；这条同时钉住"批量 + 库侧时钟"能组合
    （`calculated_at=CURRENT_TIMESTAMP` 逐行求值），这正是截断类用例的前提。
    """
    Seed(duckdb_store).snapshots(
        [
            {
                "stock_code": f"{600000 + i:06d}",
                "report_date": "2026-06-30",
                "pe_ttm": 1.0 + i,
                "calculated_at": CURRENT_TIMESTAMP,
            }
            for i in range(50)
        ]
    )

    assert _count(duckdb_store, "indicator_snapshot") == 50
    assert _within_a_minute_of_the_database_clock(duckdb_store) == 50
