"""strict 模式：派生字段豁免 + 源数据字段安全底线（2026-11-23，用户实测缺陷）。

用户原话：「你应该审查一下筛选机制。我靠，为什么无法筛选？」

界面提示（截图）::

    严格可信模式无匹配：以下指标没有任何 strict 血缘记录，相应条件永远无法满足
    —— roce_10y_median、total_market_cap

正式库实测（`data/valuedashboard.duckdb`，read_only）：
- `roce_10y_median`：`source_audit` **0 条**记录（本仓计算器产出，没有抓取事件）；
- `total_market_cap`：338,733 条，**全部** `source='derived_calculator'` + `approximate`；
- 114 个对外可筛选字段里，**只有 2 个**有 strict 血缘 → 旧实现下另外 112 个在
  strict 模式下永久空集，"点了严格模式一条都筛不出来"。

本文件钉四件事：
1. **核心**：条件含 `roce_10y_median` / `total_market_cap` 时，strict 模式**必须出结果**；
2. **安全底线**：源数据字段（这里是 `latest_close`，只有 baostock approximate 血缘）
   在没有 strict 血缘时**仍然不返回**，且派生豁免**不能**把它一起放过；
3. 派生字段豁免后，警告文案不再谎报"永远无法满足"，并说清"该怎么办"；
4. SQL 派生字段（`income.impairment_loss_positive`）豁免**自己**，但其**输入列**
   `income.asset_impairment_loss` 仍逐值校验 strict 血缘。
"""

from __future__ import annotations

from app.core.screening.engine import (
    DERIVED_AUDIT_SOURCE,
    ScreeningEngine,
)
from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import CURRENT_TIMESTAMP, Seed

# 三只股票：000003 的派生列留空，证明"值为 NULL"与"被 strict 卡住"是两件事。
# `report_date` 固定 2025-12-31：strict 谓词要求「同股 + 同报告期」。
PERIOD = "2025-12-31"


def _seed_derived_and_source_fields(duck: DuckDBStore) -> Seed:
    """播种 3 只股票 + 快照（roce_10y_median / total_market_cap / latest_close）。

    审计血缘**照正式库的形状**播：
    - `total_market_cap`：只有 `derived_calculator` 的 approximate 行（真库 33.8 万条同形状）；
    - `roce_10y_median`：**一条都不播**（真库 0 条）→ 只能靠派生域注册表豁免；
    - `latest_close`：`baostock` approximate（真库没有 strict 行）→ 源数据字段，必须卡住。
    """
    seed = Seed(duck)
    seed.stocks(
        [
            ("000001", "Derived One", {"csrc_l1": "Industry", "csrc_l2": "Machinery"}),
            ("000002", "Derived Two", {"csrc_l1": "Industry", "csrc_l2": "Machinery"}),
            ("000003", "Derived Three", {"csrc_l1": "Industry", "csrc_l2": "Electronics"}),
        ],
        exchange="SZSE",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
    )
    seed.snapshots(
        [
            {
                "stock_code": "000001", "report_date": PERIOD,
                "roce_10y_median": 0.15, "total_market_cap": 1e10, "latest_close": 10.0,
            },
            {
                "stock_code": "000002", "report_date": PERIOD,
                "roce_10y_median": 0.05, "total_market_cap": 2e10, "latest_close": 20.0,
            },
            {
                "stock_code": "000003", "report_date": PERIOD,
                "roce_10y_median": None, "total_market_cap": None, "latest_close": 30.0,
            },
        ]
    )
    for code, market_cap, close in (
        ("000001", 1e10, 10.0),
        ("000002", 2e10, 20.0),
        ("000003", 3e10, 30.0),
    ):
        # 派生生产者（本项目计算器）：只有它写得出 derived_calculator 行。
        seed.source_audit(
            code, "total_market_cap", report_date=PERIOD, value=market_cap,
            source=DERIVED_AUDIT_SOURCE, fetch_batch_id=f"derived-{code}",
            fetch_time=CURRENT_TIMESTAMP, raw_response_hash="d" * 64,
            confidence="approximate",
        )
        # 抓取型源数据字段：只有 approximate 血缘 → strict 模式必须排除。
        seed.source_audit(
            code, "latest_close", report_date=PERIOD, value=close,
            source="baostock", fetch_batch_id=f"price-{code}",
            fetch_time=CURRENT_TIMESTAMP, raw_response_hash="e" * 64,
            confidence="approximate",
        )
    return seed


def _rule(*rules: dict) -> dict:
    return {
        "conditions": {"logic": "AND", "rules": list(rules)},
        "sort": [{"field": "stock_code", "direction": "asc"}],
        "columns": ["stock_code"],
    }


# ─── 1. 核心：派生字段在 strict 模式下必须出结果 ────────────────────────────


def test_strict_mode_returns_rows_for_derived_metrics(duckdb_store: DuckDBStore) -> None:
    _seed_derived_and_source_fields(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    rule = _rule(
        {"field": "roce_10y_median", "op": "is_not_null"},
        {"field": "total_market_cap", "op": ">", "value": 0},
    )

    permissive = engine.run(rule, min_listing_years=0)
    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert [row["stock_code"] for row in permissive["results"]] == ["000001", "000002"]
    # 修复前：strict 走过去 → 0 条（用户看到的正是这个空集）。
    assert [row["stock_code"] for row in strict["results"]] == ["000001", "000002"]
    assert strict["total"] == 2
    # 豁免必须是**显式披露**的，不能悄悄放过：UI/CSV 要能看到这两个字段没有抓取血缘。
    assert strict["strict_exempt_fields"] == ["roce_10y_median", "total_market_cap"]
    assert strict["strict_enforced_fields"] == []
    assert strict["strict_mode_warning"] is None


# ─── 2. 安全底线：源数据字段没有 strict 血缘 → 仍然不返回 ────────────────────


def test_strict_mode_still_blocks_source_field_without_strict_lineage(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_derived_and_source_fields(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    rule = _rule({"field": "latest_close", "op": ">", "value": 0})

    permissive = engine.run(rule, min_listing_years=0)
    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert permissive["total"] == 3
    assert strict["results"] == []
    assert strict["strict_exempt_fields"] == []
    assert strict["strict_enforced_fields"] == ["latest_close"]
    # 文案必须说清"是哪个字段卡住了"以及"怎么办"。
    warning = strict["strict_mode_warning"]
    assert warning is not None
    assert "latest_close" in warning
    assert "包含近似可信数据" in warning
    assert "永远无法满足" not in warning


def test_derived_exemption_never_rescues_unverified_source_field(
    duckdb_store: DuckDBStore,
) -> None:
    """派生字段豁免 ≠ 整条规则豁免：同一规则里的抓取字段仍逐个卡住。"""
    _seed_derived_and_source_fields(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    rule = _rule(
        {"field": "roce_10y_median", "op": ">=", "value": 0},
        {"field": "latest_close", "op": ">", "value": 0},
    )

    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert strict["results"] == []
    assert strict["strict_exempt_fields"] == ["roce_10y_median"]
    assert strict["strict_enforced_fields"] == ["latest_close"]


# ─── 3. 警告文案：不再把派生字段算进"永远无法满足" ──────────────────────────


def test_warning_does_not_misreport_exempt_derived_fields(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_derived_and_source_fields(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    # 派生字段条件本身不命中（ROCE 中位数不可能 > 100），但"空"与可信度无关。
    rule = _rule({"field": "roce_10y_median", "op": ">", "value": 100})

    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert strict["results"] == []
    warning = strict["strict_mode_warning"]
    assert warning is not None
    assert "永远无法满足" not in warning
    assert "与可信度模式无关" in warning
    assert "roce_10y_median" in warning
    # 不能再把"派生字段没有 strict 血缘"报成"没有血缘记录 / 被排除"。
    assert "没有任何 strict 血缘记录" not in warning


def test_warning_names_missing_source_field_and_suggests_action(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_derived_and_source_fields(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)
    rule = _rule(
        {"field": "latest_close", "op": ">", "value": 0},
        {"field": "total_market_cap", "op": ">", "value": 0},
    )

    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert strict["results"] == []
    warning = strict["strict_mode_warning"]
    assert warning is not None
    # 卡住结果的是源数据字段；total_market_cap 只能以"已豁免"的身份出现。
    assert "源数据字段" in warning
    assert "latest_close" in warning
    assert "total_market_cap" in warning
    assert "没有任何 strict 血缘记录" in warning
    assert "永远无法满足" not in warning


# ─── 4. SQL 派生字段：豁免自己，但输入列仍逐值校验 ──────────────────────────


def _seed_sql_derived_field(duck: DuckDBStore, *, confidence: str) -> None:
    """`income.impairment_loss_positive` 的输入是 `income.asset_impairment_loss`。

    派生字段本身在 `source_audit` 里**永远**没有记录；它的可信度只能由输入列决定。
    """
    seed = Seed(duck)
    seed.stock(
        "000001", name="Impairment One", exchange="SZSE",
        listing_date="2020-01-01", is_st=False, is_suspended=False, csrc_l1="Industry",
    )
    seed.snapshot("000001", PERIOD)
    seed.income("000001", PERIOD, asset_impairment_loss=-50.0)
    seed.source_audit(
        "000001", "asset_impairment_loss", report_date=PERIOD, value=-50.0,
        source="csmar", fetch_batch_id="csmar-1", fetch_time=CURRENT_TIMESTAMP,
        raw_response_hash="f" * 64, confidence=confidence,
    )


def test_sql_derived_field_is_exempt_but_its_inputs_stay_strict(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_sql_derived_field(duckdb_store, confidence="strict")
    engine = ScreeningEngine(duck=duckdb_store)
    rule = _rule({"field": "income.impairment_loss_positive", "op": ">", "value": 0})

    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert [row["stock_code"] for row in strict["results"]] == ["000001"]
    assert strict["strict_exempt_fields"] == ["income.impairment_loss_positive"]
    # 豁免只到派生字段一层：输入列被下沉进 strict 谓词，而且要值级一致。
    assert strict["strict_enforced_fields"] == ["income.asset_impairment_loss"]


def test_sql_derived_input_without_strict_lineage_still_blocks(
    duckdb_store: DuckDBStore,
) -> None:
    """输入列只有 approximate 血缘 → 派生字段照样不返回（豁免不是放水）。"""
    _seed_sql_derived_field(duckdb_store, confidence="approximate")
    engine = ScreeningEngine(duck=duckdb_store)
    rule = _rule({"field": "income.impairment_loss_positive", "op": ">", "value": 0})

    permissive = engine.run(rule, min_listing_years=0)
    strict = engine.run(rule, min_listing_years=0, strict_only=True)

    assert [row["stock_code"] for row in permissive["results"]] == ["000001"]
    assert strict["results"] == []
    warning = strict["strict_mode_warning"]
    assert warning is not None
    assert "income.asset_impairment_loss" in warning
