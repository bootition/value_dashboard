"""事故出处：reports/131 / 2026-11-16。

reports/131 §9.2 P0-7 / P0-8 / P0-9 回归：跨年口径断点与股本指纹。

三条事故各锁一组断言，全部只读原始财务数据（不擅改原始值）：

**P0-7 资产减值损失符号在 FY2018 翻转**
  财会〔2018〕15 号起「资产减值损失/信用减值损失」按损失以负号列示，2017 及
  以前按损失为正，2018 是**混合年**（同年不同公司混用两种约定）。逐年汇总时
  正负相消成"假低值"。
  → 派生字段 ``income.impairment_loss_positive`` /
    ``income.credit_impairment_loss_positive`` 三年都读作"损失为正"，
    且 ``income_statement`` 原始列一行不改。

**P0-8 FY2020 预收款项 → 合同负债重分类**
  CAS14（2020-01-01 起施行）把与客户合同相关的预收款重分类为合同负债，
  ``prepayments_received`` 单列一夜 −96.6%。
  → 派生字段 ``balance.advance_from_customers`` 跨 2020 连续；单列断崖仍在
    （原始数据如实保留），因此引用原始列必须收到 ``field_semantics_break`` 警告。

**P0-9 689009 九号公司市值/每股族错 10 倍**
  股本链后来被 ×10 校正，而 indicator_ext 是校正之前构建的；旧输入指纹里
  没有 ``share_capital_history``，派生表于是永远不重建。
  → 指纹含「尾段 400 天股本摘要」；股本变化时 ``rebuild_decision`` 给出
    ``force_codes``（按变更股重建），``build(force_codes=...)`` 只重写这些股票。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.dsl.ast_nodes import FIELD_METADATA
from app.core.dsl.codegen import CodeGen
from app.core.dsl.derived_fields import (
    ADVANCE_RECLASS_CUTOFF,
    DERIVED_FIELDS,
    IMPAIRMENT_SIGN_CUTOFF,
    SERIES_BREAKS,
    series_break_warnings,
)
from app.core.dsl.engine import expand_shorthand
from app.core.dsl.parser import parse
from app.core.dsl.validator import Validator
from app.core.indicators.extended import (
    ExtendedIndicatorBuilder,
    input_state,
    rebuild_decision,
    share_capital_drift_codes,
    share_capital_state,
)
from app.core.screening.engine import (
    NORMALIZED_FIELDS,
    RANKABLE_INDICATORS,
    ScreeningEngine,
    series_break_field_warnings,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore

# ─────────────────────────────────────────────────────────────────────────────
# 共同夹具
# ─────────────────────────────────────────────────────────────────────────────


def _seed_statement_rows(
    duck: DuckDBStore,
    stock_code: str,
    report_date: str,
    *,
    revenue: float = 1000.0,
    asset_impairment_loss: float | None = None,
    credit_impairment_loss: float | None = None,
    prepayments_received: float | None = None,
    contract_liabilities: float | None = None,
    total_assets: float = 2000.0,
) -> None:
    """在**同一天**写 income/balance/cash_flow 三表（保证报告期对齐）。"""
    with duck.transaction() as conn:
        conn.execute(
            """INSERT INTO income_statement
                   (stock_code, report_date, revenue, parent_net_profit, net_profit,
                    income_tax, financial_expenses, total_profit,
                    asset_impairment_loss, credit_impairment_loss)
               VALUES (?, ?, ?, 300, 300, 50, 20, 370, ?, ?)""",
            [stock_code, report_date, revenue, asset_impairment_loss, credit_impairment_loss],
        )
        conn.execute(
            """INSERT INTO balance_sheet
                   (stock_code, report_date, total_assets, total_liabilities,
                    total_equity, total_equity_parent,
                    prepayments_received, contract_liabilities)
               VALUES (?, ?, ?, 800, 1200, 1100, ?, ?)""",
            [stock_code, report_date, total_assets, prepayments_received, contract_liabilities],
        )
        conn.execute(
            "INSERT INTO cash_flow (stock_code, report_date, cf_from_operating) VALUES (?, ?, 380)",
            [stock_code, report_date],
        )


def _seed_snapshot(duck: DuckDBStore, stock_code: str, report_date: str) -> None:
    duck.write_query(
        "INSERT INTO indicator_snapshot (stock_code, report_date, calculated_at, latest_close) "
        "VALUES (?, ?, CURRENT_TIMESTAMP, 10)",
        [stock_code, report_date],
    )


def _seed_screenable_stock(
    duck: DuckDBStore,
    stock_code: str = "600519",
    report_date: str = "2019-12-31",
    **statement_kwargs: float | None,
) -> None:
    """最小可筛选夹具（stock_meta + 快照 + 三表同日均齐）。"""
    duck.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES (?, 'Seed', 'SSE', '1990-01-01', false, false)""",
        [stock_code],
    )
    _seed_snapshot(duck, stock_code, report_date)
    _seed_statement_rows(duck, stock_code, report_date, **statement_kwargs)


def _run_screening(
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


# ─────────────────────────────────────────────────────────────────────────────
# P0-7：资产减值损失符号在 FY2018 翻转
# ─────────────────────────────────────────────────────────────────────────────


def test_impairment_loss_positive_reads_loss_positive_for_2017_2018_2019(
    duckdb_store: DuckDBStore,
) -> None:
    """2017/2018/2019 三段正负样本 → 派生字段三年都是"损失为正"。

    2018 是**混合年**：同年两只股票按相反约定披露（600519 用新约定记负号，
    000001 用旧约定记正号），派生字段必须都把"损失"读成正数。
    """
    # 2017：旧约定（损失为正）→ 100
    _seed_snapshot(duckdb_store, "600519", "2017-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2017-12-31", asset_impairment_loss=100.0)
    # 2018 混合年：新约定（损失以负号）→ -150
    _seed_snapshot(duckdb_store, "600519", "2018-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2018-12-31", asset_impairment_loss=-150.0)
    # 2019：新约定 → -200
    _seed_snapshot(duckdb_store, "600519", "2019-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2019-12-31", asset_impairment_loss=-200.0)
    # 2018 混合年的另一半：旧约定（损失为正）→ +120
    _seed_snapshot(duckdb_store, "000001", "2018-12-31")
    _seed_statement_rows(duckdb_store, "000001", "2018-12-31", asset_impairment_loss=120.0)

    derived_sql = CodeGen().generate_select(
        parse("income.impairment_loss_positive@LATEST"), stock_code="600519",
    )
    derived_values = sorted(row["result"] for row in duckdb_store.read_query(derived_sql))
    assert derived_values == [100.0, 150.0, 200.0]

    # 混合年的另一半（旧约定记正号）同样读作损失为正
    mixed_sql = CodeGen().generate_select(
        parse("income.impairment_loss_positive@LATEST"), stock_code="000001",
    )
    assert [row["result"] for row in duckdb_store.read_query(mixed_sql)] == [120.0]

    # 原始列一行不改：符号仍与当年披露约定一致（2017 正 / 2018 负 / 2019 负）
    raw_rows = duckdb_store.read_query(
        """SELECT CAST(report_date AS VARCHAR) AS d, asset_impairment_loss AS raw
           FROM income_statement WHERE stock_code = '600519' ORDER BY report_date"""
    )
    assert [row["raw"] for row in raw_rows] == [100.0, -150.0, -200.0]


def test_impairment_ttm_normalizes_each_term_not_only_the_latest_period(
    duckdb_store: DuckDBStore,
) -> None:
    """TTM 的每个加项都必须按**自己那一行**的年份归一，不能只归一最新一期。

    2019-06-30 的 TTM 资产减值损失 = FY2018 + 2019H1 − 2018H1。
    三个加项分属不同约定（2018 是混合年，本例的 2018H1 用旧约定记正号），
    只归一最新一期的实现会得出 120 + 90 − 70 之外的错误值。
    """
    rows = {
        "2018-06-30": 70.0,    # 2018 混合年：该股按旧约定记正号
        "2018-12-31": -120.0,  # 2018 混合年：年报按新约定记负号
        "2019-06-30": -90.0,   # 2019：新约定
    }
    for report_date, value in rows.items():
        _seed_snapshot(duckdb_store, "600519", report_date)
        _seed_statement_rows(duckdb_store, "600519", report_date, asset_impairment_loss=value)

    derived_ttm = CodeGen().generate_select(
        parse("income.impairment_loss_positive@TTM"), stock_code="600519",
    )
    raw_ttm = CodeGen().generate_select(
        parse("income.asset_impairment_loss@TTM"), stock_code="600519",
    )
    derived_values = sorted(
        row["result"] for row in duckdb_store.read_query(derived_ttm)
        if row["result"] is not None
    )
    raw_values = sorted(
        row["result"] for row in duckdb_store.read_query(raw_ttm)
        if row["result"] is not None
    )
    # 2018-06-30 那一期缺 2017 年同期的加项 → NULL（如实缺失），只比可算的两期
    # 每个加项各自取 ABS：120 + 90 − 70 = 140（损失为正）；2018 年报单期 = 120
    assert derived_values == pytest.approx([120.0, 140.0])
    # 原始口径：−120 + (−90) − (+70) = −280，正是"跨年不可直接比"的来源
    assert raw_values == pytest.approx([-280.0, -120.0])


def test_published_indicator_bubbles_up_raw_column_break_warning(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """已发布 DSL 指标内部引用断点原始列时，警告必须冒泡到筛选结果。

    用户看到的是指标名（`impair_ratio`），看不到它内部用了
    ``income.asset_impairment_loss`` —— 若不冒泡，跨 2018 的假低值会被
    包在一个"看起来正常"的复合指标里静默输出。
    """
    with sqlite_store.transaction() as conn:
        conn.execute(
            """INSERT INTO dsl_expressions
                   (name, version, expression_text, status, content_hash)
               VALUES ('impair_ratio', 1,
                       'income.asset_impairment_loss@LATEST / income.revenue@LATEST',
                       'published', 'impair-hash')"""
        )
    _seed_screenable_stock(duckdb_store, "600519", "2019-12-31", asset_impairment_loss=-150.0)

    result = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store).run(
        {
            "conditions": {"logic": "AND", "rules": [
                {"field": "impair_ratio", "op": "<", "value": 0},
            ]},
            "columns": ["stock_code"],
        },
        min_listing_years=0,
        masked_fields=[],
        locked_indicators={"impair_ratio": {"version": 1, "content_hash": "impair-hash"}},
    )

    assert [row["stock_code"] for row in result["results"]] == ["600519"]
    assert result["warnings"] == [
        "field_semantics_break:income.asset_impairment_loss, "
        f"{IMPAIRMENT_SIGN_CUTOFF} 起口径已变，跨断点不可直接比较"
        "（财会〔2018〕15 号起损失以负号列示，2017 及以前损失为正，2018 为混合年）；"
        "请改用 income.impairment_loss_positive"
    ]


def test_credit_impairment_loss_positive_uses_same_sign_rule(
    duckdb_store: DuckDBStore,
) -> None:
    """信用减值损失共用同一分段规则（2019 才算得出来，2018 及以前覆盖率极低）。"""
    _seed_snapshot(duckdb_store, "600519", "2018-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2018-12-31", credit_impairment_loss=-12.5)
    _seed_snapshot(duckdb_store, "600519", "2019-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2019-12-31", credit_impairment_loss=-34.5)

    sql = CodeGen().generate_select(
        parse("income.credit_impairment_loss_positive@LATEST"), stock_code="600519",
    )
    assert [row["result"] for row in duckdb_store.read_query(sql)] == [12.5, 34.5]


def test_impairment_positive_metadata_and_default_caliber() -> None:
    """派生字段在 DSL 侧登记齐全；裸名简写默认落到派生口径。"""
    for key in ("income.impairment_loss_positive",
                "income.credit_impairment_loss_positive",
                "balance.advance_from_customers",
                "balance.advance_from_customers_ratio"):
        meta = FIELD_METADATA[key]
        assert meta["derived"] is True
        assert meta["historical_capable"] is True

    # 裸名 → 派生口径（"默认用派生口径"的落地点）
    assert expand_shorthand("asset_impairment_loss") == "income.impairment_loss_positive@TTM"
    assert (
        expand_shorthand("credit_impairment_loss")
        == "income.credit_impairment_loss_positive@TTM"
    )
    assert expand_shorthand("prepayments_received") == "balance.advance_from_customers@LATEST"
    # 全路径引用保持原样（原始列保留可查）
    assert expand_shorthand("income.asset_impairment_loss@LATEST") == (
        "income.asset_impairment_loss@LATEST"
    )


def test_dsl_validator_warns_on_raw_impairment_column(sqlite_store: SQLiteStore) -> None:
    """引用原始列 → warning（不是 error）：列在各自区间内仍然正确。"""
    result = Validator(sqlite=sqlite_store).validate(parse("income.asset_impairment_loss@LATEST"))
    assert result["valid"] is True
    assert any(
        warning.startswith("field_semantics_break:income.asset_impairment_loss")
        and "income.impairment_loss_positive" in warning
        for warning in result["warnings"]
    )


def test_screening_uses_derived_impairment_and_warns_on_raw_column(
    duckdb_store: DuckDBStore,
) -> None:
    """筛选层：派生字段可筛可用，原始列仍可查但必须带断点警告。"""
    _seed_screenable_stock(
        duckdb_store, "600519", "2019-12-31",
        asset_impairment_loss=-150.0, credit_impairment_loss=-20.0,
    )

    derived = _run_screening(
        duckdb_store,
        rules=[{"field": "income.impairment_loss_positive", "op": ">", "value": 100}],
        columns=["stock_code", "income.impairment_loss_positive"],
    )
    assert [row["stock_code"] for row in derived["results"]] == ["600519"]
    assert derived["results"][0]["income.impairment_loss_positive"] == 150.0
    assert derived["warnings"] == []

    # 原始列：仍可执行（-150 < 0 命中），值原样返回，但警告必须出现
    raw = _run_screening(
        duckdb_store,
        rules=[{"field": "income.asset_impairment_loss", "op": "<", "value": 0}],
        columns=["stock_code", "income.asset_impairment_loss"],
    )
    assert [row["stock_code"] for row in raw["results"]] == ["600519"]
    assert raw["results"][0]["income.asset_impairment_loss"] == -150.0
    assert raw["warnings"] == [
        "field_semantics_break:income.asset_impairment_loss, "
        f"{IMPAIRMENT_SIGN_CUTOFF} 起口径已变，跨断点不可直接比较"
        "（财会〔2018〕15 号起损失以负号列示，2017 及以前损失为正，2018 为混合年）；"
        "请改用 income.impairment_loss_positive"
    ]


# ─────────────────────────────────────────────────────────────────────────────
# P0-8：FY2020 预收款项 → 合同负债重分类
# ─────────────────────────────────────────────────────────────────────────────


def test_advance_from_customers_is_continuous_while_single_column_cliffs(
    duckdb_store: DuckDBStore,
) -> None:
    """2019→2020：单列断崖（−97%），合并口径连续。"""
    samples = {
        "2019-12-31": (1000.0, None),      # 2019：全部记在预收款项
        "2020-12-31": (30.0, 900.0),       # 2020：合同负债承接了货款预收
        "2021-12-31": (40.0, 950.0),
    }
    for report_date, (prepaid, contract) in samples.items():
        _seed_snapshot(duckdb_store, "600519", report_date)
        _seed_statement_rows(
            duckdb_store, "600519", report_date,
            prepayments_received=prepaid, contract_liabilities=contract,
        )

    merged_sql = CodeGen().generate_select(
        parse("balance.advance_from_customers@LATEST"), stock_code="600519",
    )
    ratios_sql = CodeGen().generate_select(
        parse("balance.advance_from_customers_ratio@LATEST"), stock_code="600519",
    )
    rows = duckdb_store.read_query(
        """SELECT CAST(s.report_date AS VARCHAR) AS d, ic.prepayments_received AS single
           FROM indicator_snapshot s
           JOIN balance_sheet ic
             ON ic.stock_code = s.stock_code AND ic.report_date = s.report_date
           WHERE s.stock_code = '600519' ORDER BY s.report_date"""
    )
    merged = [row["result"] for row in duckdb_store.read_query(merged_sql)]
    ratios = [row["result"] for row in duckdb_store.read_query(ratios_sql)]

    # 单列断崖仍在（原始数据如实保留，不擅改）
    assert [row["single"] for row in rows] == [1000.0, 30.0, 40.0]
    # 合并口径连续：1000 / 930 / 990
    assert merged == [1000.0, 930.0, 990.0]
    # 占款率 = 合并口径 / 总资产（2000）
    assert ratios == [0.5, 0.465, 0.495]
    # 现金式校验："合并口径跨 2020 不再下跌"，单列下跌 > 90%
    assert merged[1] / merged[0] > 0.9
    assert rows[1]["single"] / rows[0]["single"] < 0.05


def test_advance_from_customers_is_null_when_both_columns_missing(
    duckdb_store: DuckDBStore,
) -> None:
    """两列都缺失 → NULL（不许写成 0 参与均值）；只有一侧有值 → 按 0 计另一侧。"""
    _seed_snapshot(duckdb_store, "600519", "2015-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2015-12-31")
    _seed_snapshot(duckdb_store, "600519", "2016-12-31")
    _seed_statement_rows(duckdb_store, "600519", "2016-12-31", contract_liabilities=70.0)

    merged_sql = CodeGen().generate_select(
        parse("balance.advance_from_customers@LATEST"), stock_code="600519",
    )
    assert [row["result"] for row in duckdb_store.read_query(merged_sql)] == [None, 70.0]


def test_screening_uses_merged_advance_and_warns_on_raw_prepayments(
    duckdb_store: DuckDBStore,
) -> None:
    """筛选层：合并口径可筛；原始 prepayments_received 带 2020 断点警告。"""
    _seed_screenable_stock(
        duckdb_store, "600519", "2020-12-31",
        prepayments_received=30.0, contract_liabilities=900.0,
    )

    merged = _run_screening(
        duckdb_store,
        rules=[{"field": "balance.advance_from_customers", "op": ">", "value": 500}],
        columns=["stock_code", "balance.advance_from_customers"],
    )
    assert [row["stock_code"] for row in merged["results"]] == ["600519"]
    assert merged["results"][0]["balance.advance_from_customers"] == 930.0
    assert merged["warnings"] == []

    raw = _run_screening(
        duckdb_store,
        rules=[{"field": "balance.prepayments_received", "op": "<", "value": 100}],
        columns=["stock_code", "balance.prepayments_received"],
    )
    assert raw["results"][0]["balance.prepayments_received"] == 30.0
    assert raw["warnings"] == [
        "field_semantics_break:balance.prepayments_received, "
        f"{ADVANCE_RECLASS_CUTOFF} 起口径已变，跨断点不可直接比较"
        "（CAS14 新收入准则把与客户合同相关的预收款重分类为合同负债，"
        "单列一夜 −96.6%，残留只剩租金等非合同类）；"
        "请改用 balance.advance_from_customers"
    ]


def test_series_break_fields_are_not_marked_deprecated() -> None:
    """断点原始列**不**进 DEPRECATED_COLUMNS：整列标废弃会让断点另一侧的历史查询全废。"""
    from app.core.screening.engine import DEPRECATED_COLUMNS

    assert DEPRECATED_COLUMNS == {"dividend_yield": "ttm_dividend_yield"}
    assert set(SERIES_BREAKS).isdisjoint(DEPRECATED_COLUMNS)
    # 四个断点列都在 DSL 字段表里，且各自登记了"跨断点该改用谁"
    for field, brk in SERIES_BREAKS.items():
        assert field in FIELD_METADATA
        assert FIELD_METADATA[field]["series_break"]["prefer"] == brk.prefer
        assert brk.prefer in DERIVED_FIELDS
    assert series_break_field_warnings(["income.asset_impairment_loss"]) == [
        SERIES_BREAKS["income.asset_impairment_loss"].warning()
    ]
    assert series_break_warnings([]) == []


def test_derived_fields_registered_in_screening_surface() -> None:
    """派生字段进筛选白名单/可排名集合（否则"能看不能筛"）。"""
    for key in DERIVED_FIELDS:
        assert key in NORMALIZED_FIELDS
        assert key in RANKABLE_INDICATORS
        assert f"{key}_market_rank" not in NORMALIZED_FIELDS


def test_indicators_endpoint_lists_derived_fields(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/api/screening/indicators 自动列出四个派生字段（DSL 登记 → 界面可见）。

    派生字段挂在 `NORMALIZED_FIELDS`（与 `income.revenue` 同一条注册链）上，
    因此指标列表/字段单位/排名后缀全部自动生效，不需要第二处硬编码。
    """
    from types import SimpleNamespace

    import app.core.data_quality as data_quality_module
    from app.web.api.screening import list_available_indicators

    monkeypatch.setattr(data_quality_module, "read_warning_codes", lambda *a, **k: [])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(duck=duckdb_store, sqlite=sqlite_store))
    )
    payload = list_available_indicators(request)
    by_name = {item["name"]: item for item in payload["indicators"]}

    # 单位期望值来自派生字段注册表本身（`unit=CNY → plain`、`unit=ratio → ratio`），
    # 不再写死 "plain"：`balance.advance_from_customers_ratio` 是倍数口径，
    # 一刀切断言"金额类"会让新增派生字段时立刻假红（reports/133 R1）。
    unit_expectation = {"CNY": "plain", "ratio": "ratio", "percent": "percent"}
    for key in DERIVED_FIELDS:
        assert key in by_name, f"{key} 必须出现在可选指标里"
        assert by_name[key]["rankable"] is True
        declared = DERIVED_FIELDS[key].unit
        assert by_name[key]["unit"] == unit_expectation[declared], (
            f"{key} 注册 unit={declared}，接口必须给 "
            f"{unit_expectation[declared]}，实际 {by_name[key]['unit']}"
        )
    # 原始列仍在（保留可查），且没有被标成 deprecated
    for key in SERIES_BREAKS:
        assert key in by_name


# ─────────────────────────────────────────────────────────────────────────────
# P0-9：689009 九号公司市值/每股族错 10 倍
# ─────────────────────────────────────────────────────────────────────────────

_DRIFT_STOCK = "689009"
_DRIFT_REPORT_DATE = "2024-12-31"
_DRIFT_CLOSE = 10.0
# 原始股本链（1/10 的 CDR 单位），与 reports/131 §9.2 P0-9 的现场一致
_DRIFT_SHARES_BEFORE = 73_584_850.0
_DRIFT_SHARES_AFTER = 735_848_500.0


def _seed_indicator_ext_inputs(duck: DuckDBStore) -> None:
    """一只股票 + 三表 + 收盘价 + 股本链（足以算出 market_cap_at_report）。"""
    _seed_statement_rows(
        duck, _DRIFT_STOCK, _DRIFT_REPORT_DATE,
        asset_impairment_loss=-15.0, prepayments_received=100.0,
    )
    with duck.transaction() as conn:
        conn.execute(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?)",
            [_DRIFT_STOCK, _DRIFT_REPORT_DATE, _DRIFT_CLOSE],
        )
        conn.execute(
            """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, change_reason,
                    source, raw_hash, batch_id)
               VALUES (?, '2024-01-01', ?, 'anchor', 'cninfo_capital', 'h', 'b')""",
            [_DRIFT_STOCK, _DRIFT_SHARES_BEFORE],
        )


def _market_cap(duck: DuckDBStore) -> float | None:
    rows = duck.read_query(
        "SELECT market_cap_at_report FROM indicator_ext WHERE stock_code = ?",
        [_DRIFT_STOCK],
    )
    assert rows, "indicator_ext 未生成该股票行"
    return rows[0]["market_cap_at_report"]


def test_share_capital_change_changes_fingerprint_and_force_codes(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet,
) -> None:
    """改股本 → 指纹变化、force_codes 命中该股、按变更股重建即修正。"""
    _seed_indicator_ext_inputs(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()

    # 建库时按 1/10 的股本算市值（复现 P0-9 现场）
    assert _market_cap(duckdb_store) == pytest.approx(_DRIFT_CLOSE * _DRIFT_SHARES_BEFORE)
    fingerprint_before = input_state(duckdb_store)["fingerprint"]
    shares_before = share_capital_state(duckdb_store)
    assert "share_capital_history:" in fingerprint_before
    assert shares_before["rows"] == 1
    assert share_capital_drift_codes(duckdb_store) == []  # 派生表与股本链是一致的

    # 股本链原位 ×10（正是 align_share_capital_tail 的 unit_rescale 处置）
    duckdb_store.write_query(
        """UPDATE share_capital_history
              SET total_shares = ?, change_reason = 'unit_rescale:10'
            WHERE stock_code = ?""",
        [_DRIFT_SHARES_AFTER, _DRIFT_STOCK],
    )

    shares_after = share_capital_state(duckdb_store)
    fingerprint_after = input_state(duckdb_store)["fingerprint"]
    # 行数没变、摘要必须变 —— 只靠 COUNT/MAX 做指纹是看不见这类改动的
    assert shares_after["rows"] == shares_before["rows"]
    assert shares_after["digest"] != shares_before["digest"]
    assert fingerprint_after != fingerprint_before

    # 陈旧探针：派生表还是 ×0.1 的旧值
    assert share_capital_drift_codes(duckdb_store) == [_DRIFT_STOCK]

    decision = rebuild_decision(duckdb_store, stored_fingerprint=fingerprint_before)
    assert decision["required"] is True
    assert decision["reason"] == "input_fingerprint_changed"
    assert decision["changed_inputs"] == ["share_capital_history"]
    assert decision["force_codes"] == [_DRIFT_STOCK]
    assert decision["scope"] == "codes"

    # 按变更股重建 → 市值被修正（不是整表重写）
    report = builder.build(force_codes=decision["force_codes"])
    assert report["status"] == "success"
    assert report["scope"] == "force_codes"
    assert report["codes"] == [_DRIFT_STOCK]
    assert _market_cap(duckdb_store) == pytest.approx(_DRIFT_CLOSE * _DRIFT_SHARES_AFTER)
    assert share_capital_drift_codes(duckdb_store) == []

    # 修完之后：指纹一致 → 不再要求重建
    settled = rebuild_decision(duckdb_store, stored_fingerprint=fingerprint_after)
    assert settled["required"] is False
    assert settled["reason"] == "up_to_date"
    assert settled["force_codes"] == []


def test_build_with_empty_force_codes_does_not_touch_the_table(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet,
) -> None:
    """``force_codes=[]`` 必须 skipped，绝不退化成全量重建（否则每轮白写全表）。"""
    _seed_indicator_ext_inputs(duckdb_store)
    builder = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths)
    builder.build()
    before = duckdb_store.read_query(
        "SELECT COUNT(*) AS n, MAX(calculated_at) AS at FROM indicator_ext"
    )[0]

    report = builder.build(force_codes=[])
    assert report["status"] == "skipped"
    assert report["reason"] == "no_target_codes"
    assert report["scope"] == "force_codes"
    assert report["rows_written"] == 0
    after = duckdb_store.read_query(
        "SELECT COUNT(*) AS n, MAX(calculated_at) AS at FROM indicator_ext"
    )[0]
    assert after["n"] == before["n"]
    assert after["at"] == before["at"]


def test_share_capital_window_digest_is_stable_and_ordered(
    duckdb_store: DuckDBStore,
) -> None:
    """摘要必须与插入顺序无关（否则指纹每轮都在"变化"→ 每轮全量重建）。"""
    with duckdb_store.transaction() as conn:
        conn.execute(
            """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, source, raw_hash, batch_id)
               VALUES ('600519', '2024-03-31', 100, 's', 'h1', 'b1'),
                      ('000001', '2024-06-30', 200, 's', 'h2', 'b2')"""
        )
    first = share_capital_state(duckdb_store)
    with duckdb_store.transaction() as conn:
        conn.execute(
            """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, source, raw_hash, batch_id)
               VALUES ('600519', '2023-12-31', 90, 's', 'h3', 'b3')"""
        )
    second = share_capital_state(duckdb_store)
    assert second["rows"] == first["rows"] + 1
    assert second["digest"] != first["digest"]
    # 同一份数据重复摘要 → 同值（ORDER BY 生效）
    assert share_capital_state(duckdb_store)["digest"] == second["digest"]
    assert first["window_days"] == 400
    assert date.fromisoformat(second["latest"]) == date(2024, 6, 30)
