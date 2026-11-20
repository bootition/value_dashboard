"""金融股专用三表补采 + exchange_gain 回填的回归测试（2026-11-15，reports/122）。

守住的底线
----------
1. **spec 注册正确**：`FINANCIAL_SECTOR` 的写入模式/判据/端点/列集合与
   `financial_sector_items` 实际表结构逐列一致（少一列就写不进去，多一列就崩）；
2. **period_floor 生效**：只补 2025-03-31 之后，绝不去覆盖 CSMAR 覆盖期；
3. **字段映射正确**：银行 14 个科目 + 派生「吸收存款及同业存放」、券商 9 个科目，
   且券商 `CUSTOMER_DEPOSIT`（客户资金存款）不得写进「吸收存款」；
   同报告期的资产负债表行与利润表行必须合并成一行（否则撞 PK）；
4. **只跑金融股**：股票池谓词把制造业挡在门外，判据只认 `eastmoney_f10` 本源行；
5. **dry-run 不写库**：抽样探测跑完，`financial_sector_items` / `fetch_batch` 零变化；
6. **exchange_gain 源无字段时安全跳过**：解析为空 → 不 UPDATE、不 INSERT、
   不写 fetch_batch；源有值时只填 NULL，绝不覆盖既有非空值。

全部使用临时 DuckDB/SQLite 夹具，网络一律 monkeypatch，**绝不触碰正式库**。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.supplement import (
    EXCHANGE_GAIN,
    FINANCIAL_SECTOR,
    FINANCIAL_SECTOR_COLUMNS,
    SPECS,
    SupplementFetcher,
)
from scripts import fetch_exchange_gain, fetch_financial_sector
from tests.support.seed import Seed

FETCH_TIME = datetime(2026, 11, 15, 12, 0, 0)


# ─── 冷热分库后的测试夹具 ─────────────────────────────────────────────


def _financial_sector_ddl() -> str:
    """按补采 spec 的列集合造最小表（列名/列序与 schema 一致）。

    2026-11-15 裁决：``financial_sector_items`` **不属于冷库**（2025Q2+ 由本
    spec 补采 eastmoney_f10 热数据），DDL 已恢复在 ``app/core/storage/schema.py``
    —— 正常路径下 ``duckdb_store`` 夹具已建好该表，本夹具的
    ``CREATE TABLE IF NOT EXISTS`` 因此是幂等的兜底（保证单独跑本文件时
    补采逻辑也有表可写）。
    """
    types = {
        "stock_code": "VARCHAR",
        "report_date": "DATE",
        "report_type": "VARCHAR",
        "source": "VARCHAR",
        "fetch_time": "TIMESTAMP",
        "raw_response_hash": "VARCHAR",
        "confidence": "VARCHAR",
        "batch_id": "VARCHAR",
    }
    columns = ", ".join(
        f"{column} {types.get(column, 'DOUBLE')}"
        + (" NOT NULL" if column in {"stock_code", "report_date", "source",
                                     "fetch_time", "raw_response_hash",
                                     "confidence", "batch_id"} else "")
        for column in FINANCIAL_SECTOR_COLUMNS
    )
    return (
        "CREATE TABLE IF NOT EXISTS financial_sector_items ("
        f"{columns}, PRIMARY KEY (stock_code, report_date))"
    )


@pytest.fixture(autouse=True)
def _ensure_financial_sector_table(duckdb_store):
    duckdb_store.execute_script(_financial_sector_ddl())
    return duckdb_store


# ─── 夹具 ─────────────────────────────────────────────────────────────


def _seed_stock(
    duck,
    code: str,
    name: str,
    *,
    sw_level1: str | None = "银行",
    csrc_l1: str | None = "金融业",
    exchange: str = "SSE",
    is_listed: bool = True,
) -> None:
    Seed(duck).stock(
        code,
        name=name,
        exchange=exchange,
        listing_date="2010-01-05",
        is_listed=is_listed,
        is_st=False,
        is_suspended=False,
        sw_level1=sw_level1,
        csrc_l1=csrc_l1,
        total_shares=100,
        circ_shares=80,
    )


def _seed_income(
    duck,
    code: str,
    report_date: str,
    *,
    exchange_gain: float | None = None,
    revenue: float = 1000.0,
) -> None:
    Seed(duck).income(
        code,
        report_date,
        report_type="quarterly",
        revenue=revenue,
        exchange_gain=exchange_gain,
    )


def _seed_financial_item(
    duck,
    code: str,
    report_date: str,
    *,
    source: str,
    customer_deposits: float | None = None,
) -> None:
    duck.write_query(
        # 留（第 4 类·多站点缺口）：`financial_sector_items` 无工厂方法，且 `fetch_time` 用本文件
        """INSERT INTO financial_sector_items
               (stock_code, report_date, report_type, customer_deposits, source,
                fetch_time, raw_response_hash, confidence, batch_id)
           VALUES (?, CAST(? AS DATE), 'quarterly', ?, ?, ?, 'hash', 'strict', 'b')""",
        [code, report_date, customer_deposits, source, FETCH_TIME],
    )


@pytest.fixture
def fetcher(duckdb_store, sqlite_store) -> SupplementFetcher:
    return SupplementFetcher(duck=duckdb_store, sqlite=sqlite_store)


def _count(duck, table: str) -> int:
    return int(duck.read_query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


# ─── 1. spec 注册 ─────────────────────────────────────────────────────


def test_financial_sector_spec_registered(duckdb_store) -> None:
    spec = SPECS[FINANCIAL_SECTOR]

    assert spec.write_mode == "replace_source_rows"
    assert spec.pending_mode == "latest_period"
    assert spec.judge_table == "financial_sector_items"
    assert spec.period_floor == date(2025, 3, 31)
    assert spec.endpoints == ("zcfzbAjaxNew", "lrbAjaxNew")
    assert spec.resolve_company_type is True
    assert spec.delete_scope == "batch_periods"
    assert spec.org_field_maps is not None
    assert set(spec.org_field_maps) == {"银行", "证券", "保险"}

    # 列集合必须与真实表结构逐列一致（不许加列，也不许漏列）。
    # 2026-11-17（reports/133 R1）：schema v44 新增
    # `financial_sector_items.insurance_contract_liability` 后，**裸 spec** 只描述
    # 源侧默认列（33 列），专用列由 `_adapt_spec_to_schema()` 在运行期按真实表结构
    # 自动接线。所以这里必须断言"适配后的 spec"与表结构一致，而不是断言裸 spec——
    # 后者会把"设计上的自动适配"误判成"漏列"，也是原先写死 `== 33` 变成假失败的原因。
    table_columns = {
        str(row["column_name"])
        for row in duckdb_store.read_query(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'financial_sector_items'"""
        )
    }
    fetcher = SupplementFetcher(duck=duckdb_store)
    available = fetcher._available_columns(spec.columns)
    adapted = fetcher._adapt_spec_to_schema(spec, available)
    assert set(adapted.columns["financial_sector_items"]) == table_columns
    # 业务列（工行/中信实测能对上的那批）与 CAS25 专用列都在
    assert "deposits_and_interbank" in table_columns
    assert "fee_commission_income_net" in table_columns
    assert "insurance_contract_liability" in table_columns


def test_period_floor_excludes_csmar_coverage() -> None:
    explicit = SupplementFetcher.resolve_periods(
        FINANCIAL_SECTOR,
        periods=["2025-03-31", "2025-06-30", "2026-09-30"],
    )
    assert explicit == ["2025-06-30", "2026-09-30"], "2025-03-31 及更早绝不能进目标期"

    default = SupplementFetcher.resolve_periods(
        FINANCIAL_SECTOR, today=date(2026, 11, 15),
    )
    assert default[0] == "2026-09-30"
    assert default[-1] == "2025-06-30", "CSMAR 截止后的第一季 2025Q2 必须被覆盖"
    assert all(date.fromisoformat(item) > date(2025, 3, 31) for item in default)
    assert len(default) == 6


# ─── 2. 字段映射（构造假响应，不联网）─────────────────────────────────


def _parse(fetcher, code: str, rows: list[dict]):
    spec = SPECS[FINANCIAL_SECTOR]
    parsed = fetcher._parse_rows(
        spec, code, rows,
        batch_id="test", fetched_at=FETCH_TIME, src_hash="hash",
    )
    return parsed["financial_sector_items"]


def test_parse_rows_bank_mapping_and_cross_endpoint_merge(fetcher) -> None:
    """工行 2026Q3 真实字段值（两张表 → 必须合并成一行）。"""
    balance = {
        "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "银行",
        "CASH_DEPOSIT_PBC": 3_491_400_000_000,
        "DEPOSIT_INTERBANK": 464_693_000_000,
        "LOAN_ADVANCE": 31_424_817_000_000,
        "LOAN_PBC": 201_084_000_000,
        "IOFI_DEPOSIT": 4_794_250_000_000,
        "ACCEPT_DEPOSIT": 39_753_110_000_000,
        "LEND_FUND": 632_193_000_000,
        "BUY_RESALE_FINASSET": 1_151_023_000_000,
        "BORROW_FUND": 533_125_000_000,
        "SELL_REPO_FINASSET": 3_134_484_000_000,
        # 通用模板字段：本 spec 不该吃进来
        "MONETARYFUNDS": 1.0,
    }
    income = {
        "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "银行",
        "INTEREST_INCOME": 996_537_000_000,
        "INTEREST_EXPENSE": 477_483_000_000,
        "INTEREST_NI": 519_054_000_000,
        "FEE_COMMISSION_NI": 92_280_000_000,
    }

    rows = _parse(fetcher, "601398", [balance, income])

    assert len(rows) == 1, "同一报告期的资产负债表行 + 利润表行必须合并，否则撞主键"
    row = rows[0]
    assert row["cash_and_cb_balance"] == 3_491_400_000_000
    assert row["due_from_banks"] == 464_693_000_000
    assert row["loans_and_advances"] == 31_424_817_000_000
    assert row["borrowing_from_cb"] == 201_084_000_000
    assert row["interbank_deposits"] == 4_794_250_000_000
    assert row["customer_deposits"] == 39_753_110_000_000
    # 小计 = 吸收存款 + 同业存放（2025-03-31 与 CSMAR 逐值核对差值 0）
    assert row["deposits_and_interbank"] == (
        39_753_110_000_000 + 4_794_250_000_000
    )
    assert row["interest_income"] == 996_537_000_000
    assert row["interest_expense"] == 477_483_000_000
    assert row["net_interest_income"] == 519_054_000_000
    assert row["fee_commission_income_net"] == 92_280_000_000
    assert row["interbank_lending"] == 632_193_000_000
    assert row["reverse_repo_assets"] == 1_151_023_000_000
    assert row["interbank_borrowing"] == 533_125_000_000
    assert row["repo_liabilities"] == 3_134_484_000_000
    # 元数据
    assert row["source"] == "eastmoney_f10"
    assert row["report_type"] == "quarterly"
    assert row["confidence"] == "strict"
    # 不在银行映射里的字段保持不出现（DataFrame 落库时会补 NULL）
    assert "monetary_funds" not in row


def test_parse_rows_broker_mapping_respects_semantics(fetcher) -> None:
    """中信证券 2026Q3：券商科目逐项对上；客户资金存款 ≠ 吸收存款。"""
    broker = {
        "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "证券",
        "SETTLE_EXCESS_RESERVE": 106_134_489_173.99,
        "CUSTOMER_EXCESS_RESERVE": 79_060_495_081.72,
        "REFUND_DEPOSIT_PAY": 122_673_817_237.52,
        "AGENT_TRADE_SECURITY": 611_783_202_191.23,
        "AGENT_UNDERWRITE_SECURITY": 404_476_000.0,
        "INTEREST_NI": 3_222_707_820.47,
        "FEE_COMMISSION_NI": 32_631_281_103.21,
        "BUY_RESALE_FINASSET": 50_817_689_042.58,
        "SELL_REPO_FINASSET": 418_477_984_308.56,
        "BORROW_FUND": 16_158_546_362.26,
        # 券商模板的「客户资金存款」是资产科目，绝不能写进「吸收存款」
        "CUSTOMER_DEPOSIT": 437_112_096_702.52,
    }

    rows = _parse(fetcher, "600030", [broker])

    assert len(rows) == 1
    row = rows[0]
    assert row["settlement_reserve"] == 106_134_489_173.99
    assert row["customer_settlement_reserve"] == 79_060_495_081.72
    assert row["margin_deposits_paid"] == 122_673_817_237.52
    assert row["client_securities_deposits"] == 611_783_202_191.23
    assert row["underwriting_securities"] == 404_476_000.0
    assert row["net_interest_income"] == 3_222_707_820.47
    assert row["fee_commission_income_net"] == 32_631_281_103.21
    assert row["reverse_repo_assets"] == 50_817_689_042.58
    assert row["repo_liabilities"] == 418_477_984_308.56
    assert row["interbank_borrowing"] == 16_158_546_362.26
    assert row.get("customer_deposits") is None, "券商的客户资金存款是资产，不是吸收存款"
    assert row.get("deposits_and_interbank") is None, "小计派生只对银行生效"


def test_parse_rows_insurance_and_unsupported_industry(fetcher) -> None:
    insurer = {
        "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "保险",
        "EARNED_PREMIUM": 167_437_000_000,
        # 保险控股集团（平安）资产负债表里的银行科目不该被写进保险行
        "ACCEPT_DEPOSIT": 3_689_364_000_000,
    }
    rows = _parse(fetcher, "601628", [insurer])
    assert len(rows) == 1
    assert rows[0]["earned_premiums"] == 167_437_000_000
    assert rows[0].get("customer_deposits") is None

    # 信托/金控/期货走通用模板 → 没有本表科目 → 不产出任何行（计 no_data）
    unsupported = {
        "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "通用",
        "MONETARYFUNDS": 123.0, "TOTAL_ASSETS": 456.0,
    }
    assert _parse(fetcher, "600816", [unsupported]) == []


def test_parse_rows_skips_csmar_period(fetcher) -> None:
    csmar_period = {
        "REPORT_DATE": "2025-03-31 00:00:00", "ORG_TYPE": "银行",
        "ACCEPT_DEPOSIT": 36_431_832_000_000,
    }
    assert _parse(fetcher, "601398", [csmar_period]) == [], "CSMAR 覆盖期不许重写"


# ─── 3. 股票池与续传判据 ───────────────────────────────────────────────


def test_pending_codes_limited_to_financial_universe(fetcher, duckdb_store) -> None:
    _seed_stock(duckdb_store, "601398", "工商银行", sw_level1="银行")
    _seed_stock(duckdb_store, "600030", "中信证券", sw_level1="非银金融")
    _seed_stock(duckdb_store, "000002", "万科A", sw_level1="房地产", csrc_l1="房地产业")
    # CSRC 兜底：申万缺失但证监会门类为金融业
    _seed_stock(duckdb_store, "600816", "建元信托", sw_level1=None)
    for code in ("601398", "600030", "000002", "600816"):
        _seed_income(duckdb_store, code, "2026-09-30", revenue=100.0)

    pending = fetcher.pending_codes(
        FINANCIAL_SECTOR, ["2026-06-30", "2026-09-30"],
    )

    codes = {code for code, _ in pending}
    assert codes == {"601398", "600030", "600816"}
    assert "000002" not in codes, "非金融股绝不能进补采队列"

    # 只有 eastmoney_f10 的已有行才算"已补"；CSMAR 行不算（否则永远补不到新期）
    _seed_financial_item(duckdb_store, "601398", "2026-09-30", source="eastmoney_f10")
    _seed_financial_item(duckdb_store, "600030", "2026-09-30", source="csmar")
    pending_after = {
        code for code, _ in fetcher.pending_codes(
            FINANCIAL_SECTOR, ["2026-06-30", "2026-09-30"],
        )
    }
    assert "601398" not in pending_after, "本源已追平目标期就该出队"
    assert "600030" in pending_after, "CSMAR 行不能冒充本源行"


# ─── 4. dry-run / 写库边界 ────────────────────────────────────────────


def _fake_icbc_rows() -> list[dict]:
    return [
        {
            "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "银行",
            "ACCEPT_DEPOSIT": 100.0, "IOFI_DEPOSIT": 20.0, "LOAN_ADVANCE": 300.0,
        },
        {
            "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "银行",
            "INTEREST_NI": 40.0, "FEE_COMMISSION_NI": 5.0,
        },
    ]


def test_financial_sector_dry_run_does_not_write(
    fetcher, duckdb_store, monkeypatch,
) -> None:
    _seed_stock(duckdb_store, "601398", "工商银行", sw_level1="银行")
    _seed_income(duckdb_store, "601398", "2026-09-30", revenue=100.0)
    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        lambda self, spec, code, exchange, dates, timeout=25.0: _fake_icbc_rows(),
    )
    batches_before = _count(duckdb_store, "fetch_batch")
    rows_before = _count(duckdb_store, "financial_sector_items")

    report = fetch_financial_sector.run(
        fetcher, limit=5, write=False, sample=1, timeout=1.0,
    )

    assert report["status"] == "dry_run"
    assert report["pending_stocks"] == 1
    assert report["sample"][0]["parsed_rows"] == 1
    assert set(report["sample"][0]["filled_columns"]) >= {
        "customer_deposits", "deposits_and_interbank", "loans_and_advances",
        "net_interest_income", "fee_commission_income_net",
    }
    assert _count(duckdb_store, "financial_sector_items") == rows_before
    assert _count(duckdb_store, "fetch_batch") == batches_before


def test_financial_sector_write_replaces_only_batch_periods(
    fetcher, duckdb_store, monkeypatch,
) -> None:
    _seed_stock(duckdb_store, "601398", "工商银行", sw_level1="银行")
    _seed_income(duckdb_store, "601398", "2026-09-30", revenue=100.0)
    # 上一轮已补的 2025-06-30 行：本轮抓 2026Q3 时不能被误删
    _seed_financial_item(
        duckdb_store, "601398", "2025-06-30",
        source="eastmoney_f10", customer_deposits=1.0,
    )
    # CSMAR 历史行：任何情况下都不许删
    _seed_financial_item(
        duckdb_store, "601398", "2025-03-31",
        source="csmar", customer_deposits=2.0,
    )
    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        lambda self, spec, code, exchange, dates, timeout=25.0: _fake_icbc_rows(),
    )

    report = fetch_financial_sector.run(
        fetcher, limit=5, write=True, concurrency=1, retry_days=0, timeout=1.0,
    )

    assert report["status"] == "success"
    assert report["refresh"]["updated"] == 1
    assert report["refresh"]["rows_written"]["financial_sector_items"] == 1

    rows = duckdb_store.read_query(
        """SELECT report_date, source, customer_deposits,
                  deposits_and_interbank, net_interest_income
           FROM financial_sector_items WHERE stock_code = '601398'
           ORDER BY report_date"""
    )
    assert [str(row["report_date"])[:10] for row in rows] == [
        "2025-03-31", "2025-06-30", "2026-09-30",
    ], "只替换本批报告期：CSMAR 期与上一轮东财期都必须保留"
    assert rows[0]["source"] == "csmar"
    assert rows[1]["source"] == "eastmoney_f10"
    assert rows[2]["source"] == "eastmoney_f10"
    assert rows[2]["customer_deposits"] == 100.0
    assert rows[2]["deposits_and_interbank"] == 120.0
    assert rows[2]["net_interest_income"] == 40.0


# ─── 5. exchange_gain：源无字段时安全跳过 / 只填 NULL ──────────────────


def test_exchange_gain_spec_uses_fairvalue_field() -> None:
    spec = SPECS[EXCHANGE_GAIN]
    assert spec.write_mode == "update_existing"
    assert spec.fills_null_only is True
    assert spec.field_map == {"FAIRVALUE_CHANGE_INCOME": "exchange_gain"}
    assert set(fetch_exchange_gain.FIELD_CHOICES) == {
        "fairvalue_change_income", "exchange_income",
    }


def test_exchange_gain_skips_safely_when_source_has_no_field(
    fetcher, duckdb_store, monkeypatch,
) -> None:
    _seed_stock(duckdb_store, "600028", "中国石化", sw_level1="石油石化",
                csrc_l1="制造业")
    _seed_income(duckdb_store, "600028", "2026-09-30", exchange_gain=None)
    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        # 源只回了「汇兑收益」，没有本列语义的「公允价值变动收益」
        lambda self, spec, code, exchange, dates, timeout=25.0: [
            {"REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "通用",
             "EXCHANGE_INCOME": -1.0},
        ],
    )
    batches_before = _count(duckdb_store, "fetch_batch")

    report = fetch_exchange_gain.run(
        fetcher, write=True, limit=10, concurrency=1, retry_days=0, timeout=1.0,
    )

    assert report["refresh"]["updated"] == 0
    assert report["refresh"]["status"] == "skipped"
    assert report["refresh"]["reason"] == "no_applicable_fields"
    value = duckdb_store.read_query(
        "SELECT exchange_gain FROM income_statement WHERE stock_code = '600028'"
    )[0]["exchange_gain"]
    assert value is None, "源里没有该字段时必须保持 NULL，不能写坏数据"
    assert _count(duckdb_store, "fetch_batch") == batches_before, "空解析不得记批次"


def test_exchange_gain_fills_only_null_and_never_overwrites(
    fetcher, duckdb_store, monkeypatch,
) -> None:
    _seed_stock(duckdb_store, "600028", "中国石化", sw_level1="石油石化",
                csrc_l1="制造业")
    _seed_stock(duckdb_store, "601398", "工商银行", sw_level1="银行")
    # 600028 Q3 为空 → 应被回填
    _seed_income(duckdb_store, "600028", "2026-09-30", exchange_gain=None)
    # 601398：Q3 已有新浪口径的值（不许覆盖），Q2 为空 → 应被回填
    _seed_income(duckdb_store, "601398", "2026-09-30", exchange_gain=123.0)
    _seed_income(duckdb_store, "601398", "2026-06-30", exchange_gain=None)

    def fake_rows(self, spec, code, exchange, dates, timeout=25.0):  # noqa: ANN001
        values = {
            "600028": {"2026-09-30": -9_328_000_000.0},
            "601398": {"2026-09-30": 999.0, "2026-06-30": 777.0},
        }[code]
        return [
            {
                "REPORT_DATE": f"{report_date} 00:00:00", "ORG_TYPE": "通用",
                "FAIRVALUE_CHANGE_INCOME": value,
            }
            for report_date, value in values.items()
        ]

    monkeypatch.setattr(SupplementFetcher, "_fetch_rows", fake_rows)

    report = fetch_exchange_gain.run(
        fetcher, write=True, limit=10, concurrency=1, retry_days=0, timeout=1.0,
    )

    assert report["status"] == "success"
    values = {
        (str(row["stock_code"]), str(row["report_date"])[:10]): row["exchange_gain"]
        for row in duckdb_store.read_query(
            "SELECT stock_code, report_date, exchange_gain FROM income_statement"
        )
    }
    assert values[("600028", "2026-09-30")] == -9_328_000_000.0
    assert values[("601398", "2026-06-30")] == 777.0
    assert values[("601398", "2026-09-30")] == 123.0, "既有非空值绝不能被覆盖"
    assert _count(duckdb_store, "income_statement") == 3, "只 UPDATE，不 INSERT"


def test_exchange_gain_dry_run_does_not_write(
    fetcher, duckdb_store, monkeypatch,
) -> None:
    _seed_stock(duckdb_store, "600028", "中国石化", sw_level1="石油石化",
                csrc_l1="制造业")
    _seed_income(duckdb_store, "600028", "2026-09-30", exchange_gain=None)
    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        lambda self, spec, code, exchange, dates, timeout=25.0: [
            {"REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE": "通用",
             "FAIRVALUE_CHANGE_INCOME": -9_328_000_000.0,
             "EXCHANGE_INCOME": None},
        ],
    )
    batches_before = _count(duckdb_store, "fetch_batch")

    report = fetch_exchange_gain.run(
        fetcher, write=False, limit=10, sample=1, timeout=1.0,
    )

    assert report["status"] == "dry_run"
    assert report["source_field"] == "FAIRVALUE_CHANGE_INCOME"
    assert report["sample"][0]["parsed_rows"] == 1
    assert report["sample"][0]["preview"][0]["exchange_gain"] == -9_328_000_000.0
    assert duckdb_store.read_query(
        "SELECT exchange_gain FROM income_statement WHERE stock_code = '600028'"
    )[0]["exchange_gain"] is None
    assert _count(duckdb_store, "fetch_batch") == batches_before


def test_supplement_batch_lineage_hash_is_sha256_and_archived(
    fetcher, duckdb_store, monkeypatch,
) -> None:
    """补采批次的血缘摘要必须是 64 位 SHA256，且**先归档再登记**（reports/124）。

    背景（2026-11-15 事故）：`refresh()` 原来写
    `src_hash = f"{spec.endpoint}:{batch_id}"[:32]`，产出
    `xjllbAjaxNew:451ef0802792468b909` 这种 **32 位非十六进制**串。数据质量规则
    按"32 位且形如 MD5 = legacy 历史批次"判定，其余形态一律算真血缘断裂 →
    `LINEAGE_INVALID` → `_SNAPSHOT_BLOCKING_WARNINGS` → **整个快照被遮蔽**
    （实测 13 个批次让 5,579 只股票的 34 个指标全部变 null）。

    本用例钉死三件事：① 摘要是 64 位十六进制；② 该摘要能在
    `raw_response_archive_valid_hash` 里查到（不是"有摘要无归档"）；③ 归档
    payload 是请求描述（含 batch_id / periods），不是空。
    """
    import re

    _seed_stock(duckdb_store, "601398", "工商银行", sw_level1="银行")
    _seed_income(duckdb_store, "601398", "2026-09-30", revenue=100.0)
    monkeypatch.setattr(
        SupplementFetcher, "_fetch_rows",
        lambda self, spec, code, exchange, dates, timeout=25.0: _fake_icbc_rows(),
    )

    report = fetch_financial_sector.run(
        fetcher, limit=5, write=True, sample=1, timeout=1.0,
    )
    assert report["status"] in {"success", "ok"}

    batch = duckdb_store.read_query(
        "SELECT batch_id, raw_response_hash FROM fetch_batch "
        "ORDER BY fetch_time DESC LIMIT 1"
    )[0]
    digest = str(batch["raw_response_hash"])
    assert re.fullmatch(r"[0-9a-f]{64}", digest), (
        f"批次摘要必须是 64 位 SHA256，实际={digest!r}（32 位非十六进制会被判成血缘断裂）"
    )
    archived = duckdb_store.read_query(
        "SELECT 1 FROM raw_response_archive_valid_hash WHERE raw_response_hash = ?",
        [digest],
    )
    assert archived, "摘要必须能在归档中查到，否则 batch_archive_gap_rows>0 → 全站遮蔽"
    payload = duckdb_store.read_query(
        "SELECT OCTET_LENGTH(payload) AS n FROM raw_response_archive_all "
        "WHERE raw_response_hash = ?",
        [digest],
    )[0]["n"]
    assert payload and payload > 0, "归档 payload 不能为空"
