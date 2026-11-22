"""负债质量三指标（段永平 / 巴菲特口径）—— 派生字段回归。

出处与口径（2026-11-22，用户直接采信，不再二次查证）：

1. **有息负债率** ``balance.interest_bearing_debt_ratio``
   = (短期借款 + 长期借款 + 应付债券) / 总资产 —— 段永平原话：「你要算他们的
   **有息负债**。预收款也是负债，但意义差很多」「有息负债大概就是银行借贷或
   债券类的东西，到期要还的」。
2. **净现金 / 总资产** ``balance.net_cash_to_assets``
   = (货币资金 − 有息负债) / 总资产 —— 段永平六个数之一：「净现金 > 0 = 手头的
   钱够还掉所有要付息的债」。
3. **有息负债 / 总负债** ``balance.interest_bearing_debt_to_liabilities``
   量化「预收款也是负债，但意义差很多」。

本文件锁三组断言（缺一不可）：

- **NULL vs 0**：分母缺失/≤0 → NULL；分子三项借款科目**全空** → NULL（不许写成
  假 0 —— 假 0 会让「什么都没采到」的股票在 `< 0.1` 这类"低负债"筛选里**满分通过**，
  这是本条纪律比"少算"更危险的地方）；任一项非空 → 缺失侧按 0 计（多数公司本就
  不发债）。
- **阈值筛选真实生效**：``/api/screening/run`` 的条件走同一套派生 SQL，筛出来的
  集合必须与手工按原始列算的结果一致。
- **与手工对账一致**：派生字段值 == 用 ``balance_sheet`` 原始列在 Python 里手算的值。

跨表口径（有息负债/净利润 = 巴菲特"3–4 年还清"）**没有**实现：派生层只能取本表列，
理由与改造清单写在 ``app/core/dsl/derived_fields.py`` 模块 docstring。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.dsl.ast_nodes import FIELD_METADATA
from app.core.dsl.derived_fields import DERIVED_FIELDS, INTEREST_BEARING_DEBT_ACCOUNTS
from app.core.screening.engine import (
    NORMALIZED_FIELDS,
    RANKABLE_INDICATORS,
    ScreeningEngine,
)
from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import CURRENT_TIMESTAMP, Seed

REPORT_DATE = "2025-12-31"

IBD_RATIO = "balance.interest_bearing_debt_ratio"
NET_CASH = "balance.net_cash_to_assets"
IBD_TO_LIAB = "balance.interest_bearing_debt_to_liabilities"
IBD = "balance.interest_bearing_debt"


def _seed_stock(
    duck: DuckDBStore,
    stock_code: str,
    *,
    balance: dict[str, Any],
    income: dict[str, Any] | None = None,
) -> None:
    """一只可筛选的股票：stock_meta + 快照 + 当期三表同日均齐。

    业务列逐字由调用点给出（``balance_sheet`` 的借款科目缺失时**不写**，
    保持 NULL —— 这正是"三项全空"用例的前提）。
    """
    seed = Seed(duck)
    seed.stock(
        stock_code, name="Seed", exchange="SSE",
        listing_date="1990-01-01", is_st=False, is_suspended=False,
    )
    seed.snapshot(
        stock_code, REPORT_DATE, calculated_at=CURRENT_TIMESTAMP, latest_close=10,
    )
    seed.income(
        stock_code, REPORT_DATE,
        **{
            "revenue": 1000.0, "net_profit": 300.0, "parent_net_profit": 300.0,
            "income_tax": 50.0, "financial_expenses": 20.0, "total_profit": 370.0,
            **(income or {}),
        },
    )
    seed.balance(stock_code, REPORT_DATE, **balance)
    seed.cash_flow(stock_code, REPORT_DATE, cf_from_operating=380.0)


def _run(
    duck: DuckDBStore,
    *,
    rules: list[dict],
    columns: list[str],
    sort: list[dict] | None = None,
) -> dict:
    engine = ScreeningEngine(duck=duck)
    return engine.run(
        {
            "conditions": {"logic": "AND", "rules": rules},
            "sort": sort or [],
            "columns": columns,
        },
        min_listing_years=0,
        masked_fields=[],
    )


def _values(duck: DuckDBStore, sql: str) -> list[Any]:
    return [row["result"] for row in duck.read_query(sql)]


# ─────────────────────────────────────────────────────────────────────────────
# 0. 登记面：筛选项 / 结果列 / 排名自动可用（无需第二处登记）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field_name", [IBD, IBD_RATIO, NET_CASH, IBD_TO_LIAB])
def test_fields_are_registered_for_screening_and_ranking(field_name: str) -> None:
    """派生字段进 ``NORMALIZED_FIELDS`` 即被 `conditions`/`columns`/排名同时接受。"""
    assert field_name in DERIVED_FIELDS
    assert field_name in NORMALIZED_FIELDS
    # RANKABLE_INDICATORS = … | NORMALIZED_FIELDS |，因此自动可排名
    assert field_name in RANKABLE_INDICATORS
    assert field_name in FIELD_METADATA


def test_unit_is_pct_and_doc_states_percent_and_finance_warning() -> None:
    """``unit="pct"`` + doc 必须写明「0.25 = 25%」与金融股不适用。

    为什么是 ``pct`` 而不是 ``ratio``：本库前端把 ``ratio`` 读作「本身即倍数」
    （``formatFieldValue`` 只 ``toFixed(2)``、**不 ×100**），把 ``pct`` 读作
    「小数比例存储的百分比」（``fmtPct`` = ×100 + ``%``）。资产负债率
    ``debt_ratio`` 走的就是 ``pct``（0.924 → 92.4%），三条负债质量指标与它同族，
    必须同为 ``pct``，否则全市场会出现「资产负债率显示 92.4%、有息负债率显示 0.05
    倍」的同族口径分叉。
    """
    for field_name in (IBD_RATIO, NET_CASH, IBD_TO_LIAB):
        field = DERIVED_FIELDS[field_name]
        assert field.unit == "pct", field_name
        assert "0.25 = 25%" in field.doc, field_name
        assert "金融业" in field.doc, field_name
        assert "金融" in field.doc and "不适用" in field.doc, field_name


def test_unit_class_matches_debt_ratio_family() -> None:
    """同族口径一致：三条负债质量指标与 ``debt_ratio`` 同一单位类别。

    这是防回退闸门：谁把这三条改回 ``ratio``（前端会显示成「0.05 倍」），
    这条立刻红，而不是等用户看到"资产负债率 92.4% / 有息负债率 0.05 倍"。
    """
    from app.core.screening.field_units import field_unit

    debt_ratio_class = field_unit("debt_ratio")
    assert debt_ratio_class == "pct", (
        f"debt_ratio 的单位类别变了（{debt_ratio_class}）：请重新对齐同族口径"
    )
    for field_name in (IBD_RATIO, NET_CASH, IBD_TO_LIAB):
        assert field_unit(field_name) == debt_ratio_class, (
            f"{field_name} 的单位类别 {field_unit(field_name)} 与资产负债率 "
            f"{debt_ratio_class} 不一致（前端一个显示 %、一个显示 倍）"
        )
        # 派生字段自己声明的单位必须与注册表归类一致（100 倍误差防线）
        assert DERIVED_FIELDS[field_name].unit == debt_ratio_class, field_name


def test_interest_bearing_accounts_are_exactly_the_three_documented_ones() -> None:
    """有息负债的成分科目只能是「短借 + 长借 + 应付债券」。

    段永平口径只数"银行借贷或债券类、到期要还的"。`notes_payable`（应付票据,
    经营性）、`lease_liabilities`（租赁负债, 使用权资产对价）、
    `balance_sheet_ext.non_current_liab_due_1y`（另一张低频表）都**不许**被就地
    塞进来 —— 那会把「有息负债率」悄悄换成另一种东西，并与主链
    `indicator_snapshot.interest_bearing_debt` 对不上账。要更宽的口径应另立字段。
    """
    assert INTEREST_BEARING_DEBT_ACCOUNTS == (
        "short_term_loans", "long_term_loans", "bonds_payable",
    )
    # 三条比率字段的 raw_columns 必须真的覆盖这个成分表（否则 SQL 只算了一部分）
    for field_name in (IBD_RATIO, NET_CASH, IBD_TO_LIAB):
        assert set(INTEREST_BEARING_DEBT_ACCOUNTS) <= set(
            DERIVED_FIELDS[field_name].raw_columns
        ), field_name


# ─────────────────────────────────────────────────────────────────────────────
# 1. NULL vs 0：缺席的科目不许变成 0
# ─────────────────────────────────────────────────────────────────────────────


def test_all_borrowing_components_missing_yields_null_not_zero(
    duckdb_store: DuckDBStore,
) -> None:
    """三项借款科目**全空** → NULL（不是 0）。

    假 0 会让这只股票在「有息负债率 < 0.1」（低负债）筛选里满分通过 ——
    而它其实只是明细没采到。绝对值和三个比率都必须 NULL。
    """
    _seed_stock(
        duckdb_store, "600519",
        balance={
            "total_assets": 1000.0, "total_liabilities": 600.0,
            "total_equity": 400.0, "total_equity_parent": 400.0,
            "monetary_funds": 200.0,
            # short_term_loans / long_term_loans / bonds_payable 全不写 → NULL
        },
    )

    rows = _run(
        duckdb_store,
        rules=[],
        columns=[IBD, IBD_RATIO, NET_CASH, IBD_TO_LIAB],
    )["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row[IBD] is None
    assert row[IBD_RATIO] is None
    assert row[NET_CASH] is None
    assert row[IBD_TO_LIAB] is None

    # 关键：NULL 不能被 `< 0.1` 当成 0 命中
    matched = _run(
        duckdb_store,
        rules=[{"field": IBD_RATIO, "op": "<", "value": 0.1}],
        columns=[IBD_RATIO],
    )
    assert matched["total"] == 0


def test_missing_component_counts_as_zero_when_at_least_one_present(
    duckdb_store: DuckDBStore,
) -> None:
    """只有短期借款一项有值 → 有息负债 = 该项本身，其余缺失按 0 计。

    `bonds_payable` 全市场只有约 4 成行非空（多数公司根本不发债），要求三项
    齐全就会把覆盖率压到 11.8%（S8 P1-5 的教训）。
    """
    _seed_stock(
        duckdb_store, "600519",
        balance={
            "total_assets": 1000.0, "total_liabilities": 500.0,
            "total_equity": 500.0, "total_equity_parent": 500.0,
            "monetary_funds": 400.0,
            "short_term_loans": 100.0,
            # long_term_loans / bonds_payable → NULL（按 0 计）
        },
    )
    row = _run(duckdb_store, rules=[], columns=[IBD, IBD_RATIO, NET_CASH])["results"][0]
    assert row[IBD] == pytest.approx(100.0)
    assert row[IBD_RATIO] == pytest.approx(0.1)
    assert row[NET_CASH] == pytest.approx((400.0 - 100.0) / 1000.0)


@pytest.mark.parametrize("total_assets", [None, 0.0, -100.0])
def test_ratio_is_null_when_denominator_missing_or_non_positive(
    duckdb_store: DuckDBStore, total_assets: float | None
) -> None:
    """分母（总资产）缺失 / = 0 / < 0 → NULL，不是 0，也不是除零异常。"""
    _seed_stock(
        duckdb_store, "600519",
        balance={
            "total_assets": total_assets,
            "total_liabilities": 500.0,
            "short_term_loans": 100.0, "long_term_loans": 50.0,
            "monetary_funds": 400.0,
        },
    )
    row = _run(
        duckdb_store, rules=[], columns=[IBD, IBD_RATIO, NET_CASH, IBD_TO_LIAB],
    )["results"][0]
    # 绝对值和「/总负债」与总资产无关，照常算得出来
    assert row[IBD] == pytest.approx(150.0)
    assert row[IBD_TO_LIAB] == pytest.approx(0.3)
    # 分母不可用的两个比率 → NULL
    assert row[IBD_RATIO] is None
    assert row[NET_CASH] is None


def test_net_cash_is_null_when_monetary_funds_missing(
    duckdb_store: DuckDBStore,
) -> None:
    """货币资金缺失 → NULL。绝不假定「现金 = 0」（那会把"没采到"读成"没钱"）。"""
    _seed_stock(
        duckdb_store, "600519",
        balance={
            "total_assets": 1000.0, "total_liabilities": 400.0,
            "total_equity": 600.0, "total_equity_parent": 600.0,
            "short_term_loans": 100.0,
            # monetary_funds 不写 → NULL
        },
    )
    row = _run(duckdb_store, rules=[], columns=[IBD_RATIO, NET_CASH])["results"][0]
    assert row[IBD_RATIO] == pytest.approx(0.1)  # 与货币资金无关，照常算
    assert row[NET_CASH] is None


def test_zero_total_liabilities_is_not_a_fake_zero_ratio(
    duckdb_store: DuckDBStore,
) -> None:
    """总负债 = 0（无负债公司）→ 有息负债/总负债 NULL，不是 0。"""
    _seed_stock(
        duckdb_store, "600519",
        balance={
            "total_assets": 1000.0, "total_liabilities": 0.0,
            "total_equity": 1000.0, "total_equity_parent": 1000.0,
            "monetary_funds": 500.0,
        },
    )
    row = _run(duckdb_store, rules=[], columns=[IBD, IBD_RATIO, IBD_TO_LIAB])["results"][0]
    assert row[IBD] is None  # 三项借款科目全空
    assert row[IBD_RATIO] is None
    assert row[IBD_TO_LIAB] is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. 阈值筛选真实生效（`/api/screening/run` 用的就是这套 _build_sql）
# ─────────────────────────────────────────────────────────────────────────────


def _seed_three_companies(duck: DuckDBStore) -> None:
    """低 / 中 / 高 有息负债率三只，外加一只"明细没采到"（必须被排除在外）。"""
    # 0.05 —— 低负债
    _seed_stock(duck, "600519", balance={
        "total_assets": 1000.0, "total_liabilities": 400.0,
        "total_equity": 600.0, "total_equity_parent": 600.0,
        "monetary_funds": 300.0, "short_term_loans": 50.0,
    })
    # 0.15 —— 中等
    _seed_stock(duck, "000001", balance={
        "total_assets": 1000.0, "total_liabilities": 600.0,
        "total_equity": 400.0, "total_equity_parent": 400.0,
        "monetary_funds": 100.0,
        "short_term_loans": 80.0, "long_term_loans": 40.0, "bonds_payable": 30.0,
    })
    # 0.35 —— 高负债
    _seed_stock(duck, "600000", balance={
        "total_assets": 1000.0, "total_liabilities": 900.0,
        "total_equity": 100.0, "total_equity_parent": 100.0,
        "monetary_funds": 50.0,
        "short_term_loans": 250.0, "bonds_payable": 100.0,
    })
    # 明细全空 —— 任何阈值都不该命中
    _seed_stock(duck, "600001", balance={
        "total_assets": 1000.0, "total_liabilities": 500.0,
        "total_equity": 500.0, "total_equity_parent": 500.0,
        "monetary_funds": 400.0,
    })


def test_threshold_condition_really_filters(duckdb_store: DuckDBStore) -> None:
    """`有息负债率 < 0.1` 只留低负债那只；`>= 0.3` 只留高负债那只。

    同时锁住「NULL 不参与比较」：明细全空的 600001 两侧都不出现。
    """
    _seed_three_companies(duckdb_store)

    low = _run(
        duckdb_store,
        rules=[{"field": IBD_RATIO, "op": "<", "value": 0.1}],
        columns=["stock_code", IBD_RATIO],
    )
    assert low["total"] == 1
    assert [r["stock_code"] for r in low["results"]] == ["600519"]
    assert low["results"][0][IBD_RATIO] == pytest.approx(0.05)

    high = _run(
        duckdb_store,
        rules=[{"field": IBD_RATIO, "op": ">=", "value": 0.3}],
        columns=["stock_code", IBD_RATIO],
    )
    assert [r["stock_code"] for r in high["results"]] == ["600000"]
    assert high["results"][0][IBD_RATIO] == pytest.approx(0.35)

    # 净现金 > 0：只有"现金够还掉全部有息债"的那只（600519：300 > 50）
    net_cash = _run(
        duckdb_store,
        rules=[{"field": NET_CASH, "op": ">", "value": 0.0}],
        columns=["stock_code", NET_CASH],
    )
    assert [r["stock_code"] for r in net_cash["results"]] == ["600519"]
    assert net_cash["results"][0][NET_CASH] == pytest.approx(0.25)


def test_field_is_sortable_cross_sectionally(duckdb_store: DuckDBStore) -> None:
    """能在 `sort` 上用（可排）：升序得到 低 → 中 → 高，NULL 沉底。"""
    _seed_three_companies(duckdb_store)
    result = _run(
        duckdb_store,
        rules=[],
        columns=["stock_code", IBD_RATIO],
        sort=[{"field": IBD_RATIO, "direction": "asc"}],
    )
    codes = [r["stock_code"] for r in result["results"] if r[IBD_RATIO] is not None]
    assert codes == ["600519", "000001", "600000"]
    assert result["results"][-1][IBD_RATIO] is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. 与手工对账一致（原始列 → Python 手算 == 派生字段）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "balance",
    [
        # 三项齐全 —— 600956 @2026-09-30（正式库真实行，全精度）
        {
            "total_assets": 94387914159.3, "total_liabilities": 62573796636.86,
            "short_term_loans": 3054621970.22, "long_term_loans": 36635480963.41,
            "bonds_payable": 1400000000.0, "monetary_funds": 4423359301.57,
        },
        # 应付债券缺失（按 0 计）—— 002462
        {
            "total_assets": 10839285926.05, "total_liabilities": 5020496935.97,
            "short_term_loans": 869250805.84, "long_term_loans": 10000000.0,
            "monetary_funds": 2272515078.04,
        },
        # 长期借款缺失（按 0 计）—— 000906
        {
            "total_assets": 54159796077.63, "total_liabilities": 42771181329.27,
            "short_term_loans": 9521819228.26, "bonds_payable": 2922282630.14,
            "monetary_funds": 6179050097.38,
        },
    ],
    ids=["all-three", "no-bonds", "no-long-term"],
)
def test_matches_manual_computation(
    duckdb_store: DuckDBStore, balance: dict[str, Any]
) -> None:
    """派生字段 == 用 `balance_sheet` 原始列手算（相对误差 1e-12）。

    三组数字取自正式库真实行（600956 / 002462 / 000906 @2026-09-30），但在这里
    **按原始列重播**：断言只依赖原始列的定义，与真库内容无关。手算式逐字写在
    下面，不从被测实现里取任何中间量。
    """
    _seed_stock(duckdb_store, "600519", balance={
        **balance, "total_equity": 1000.0, "total_equity_parent": 1000.0,
    })

    # ── 手算（口径：三项借款科目，缺失按 0 计；分母是总资产/总负债）──
    ibd = (
        (balance.get("short_term_loans") or 0.0)
        + (balance.get("long_term_loans") or 0.0)
        + (balance.get("bonds_payable") or 0.0)
    )
    total_assets = balance["total_assets"]
    total_liabilities = balance["total_liabilities"]
    cash = balance["monetary_funds"]
    expected = {
        IBD: ibd,
        IBD_RATIO: ibd / total_assets,
        NET_CASH: (cash - ibd) / total_assets,
        IBD_TO_LIAB: ibd / total_liabilities,
    }

    row = _run(
        duckdb_store, rules=[], columns=[IBD, IBD_RATIO, NET_CASH, IBD_TO_LIAB],
    )["results"][0]
    for field_name, want in expected.items():
        assert row[field_name] == pytest.approx(want, rel=1e-12), field_name


def test_numerator_includes_bonds_payable_visibly(duckdb_store: DuckDBStore) -> None:
    """`bonds_payable` 必须进分子：把它置空后比率必须**下降**。

    这条用例专治"漏掉应付债券"的变异（漏了它，比率从 0.3 掉到 0.1 而不是
    报错，属于静默错值 —— 只能用数值差锁住）。
    """
    base = {
        "total_assets": 1000.0, "total_liabilities": 500.0,
        "total_equity": 500.0, "total_equity_parent": 500.0,
        "monetary_funds": 100.0, "short_term_loans": 100.0,
    }
    _seed_stock(duckdb_store, "600519", balance={**base, "bonds_payable": 200.0})
    with_bonds = _run(duckdb_store, rules=[], columns=[IBD, IBD_RATIO])["results"][0]
    assert with_bonds[IBD] == pytest.approx(300.0)
    assert with_bonds[IBD_RATIO] == pytest.approx(0.3)

    _seed_stock(duckdb_store, "000001", balance=base)
    without_bonds = _run(
        duckdb_store,
        rules=[{"field": "stock_code", "op": "=", "value": "000001"}],
        columns=[IBD, IBD_RATIO],
    )["results"][0]
    assert without_bonds[IBD] == pytest.approx(100.0)
    assert without_bonds[IBD_RATIO] == pytest.approx(0.1)
