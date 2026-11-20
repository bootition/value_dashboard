"""告警分级 / 字段级遮蔽 / 体检分层调度 / 废弃字段 —— reports/120 §5 J9/J10/J5 回归。

本文件守住四件事：

1. **分级（J9-1）**：每条规则有 `severity ∈ {block, disclose, inventory}`；
   结构性可空字段（亏损无 PE、无分红无股息率、无商誉）只披露不阻断。
   `level_counts` / `p1_codes` **只统计 block** —— 否则 32 条
   FIELD_COVERAGE_LOW + 54 条 NULL_REASON_MISSING 会把真问题埋掉。
2. **字段级遮蔽（J9-2）**：`EXT_PERIOD_MISMATCH` 只遮扩展域、
   `DEDUCTED_PROFIT_GAP` 只遮扣非四兄弟；`/api/screening/indicators` 把它们
   移到 `unavailable`，筛选引擎命中即拒绝执行（409），**不静默返回空集**。
3. **分层调度（J10）**：cheap 档不跑归档交叉核验（~35s），状态页只跑 cheap
   但会透传 expensive 档"上次运行摘要"。
4. **废弃字段（J5）**：`dividend_yield` 标 deprecated、不再列出，但旧规则
   仍可执行并带回 `warnings`。

全部用 tmp_path 里的临时 DuckDB/SQLite（S1 隔离），不触碰 data/ 正式库。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.core.data_quality import (
    DEDUCTED_PROFIT_INDICATOR_FIELDS,
    build_data_quality_status,
    indicator_trust,
    mask_untrusted_values,
    masked_fields_for_codes,
)
from app.core.quality import asset_health
from app.core.quality.asset_health import (
    RULE_CODES,
    SEVERITY_BLOCK,
    SEVERITY_DISCLOSE,
    SEVERITY_INVENTORY,
    is_structurally_nullable,
    load_expensive_summary,
    run_asset_health,
    run_doctor,
)
from app.core.screening import engine as engine_module
from app.core.screening.engine import (
    DEPRECATED_COLUMNS,
    EXTENDED_COLUMNS,
    MaskedFieldError,
    ScreeningEngine,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import CURRENT_TIMESTAMP, Seed

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAMPLE = 100


# ─────────────────────────────────────────────────────────────────────────────
# 夹具
# ─────────────────────────────────────────────────────────────────────────────


def _payload(duck: DuckDBStore, sqlite: SQLiteStore | None = None, **kwargs: Any) -> dict:
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("allow_expensive", False)
    return run_asset_health(duck, sqlite, **kwargs)


def _findings(payload: dict, code: str) -> list[dict]:
    return [item for item in payload["findings"] if item["code"] == code]


def _bulk_domain(
    duck: DuckDBStore,
    table: str,
    columns: list[str],
    report_date: date,
    *,
    constants: dict[str, Any] | None = None,
    null_every: dict[str, int] | None = None,
    count: int = DEFAULT_SAMPLE,
) -> None:
    constants = constants or {}
    null_every = null_every or {}
    head = ["stock_code", "report_date", *constants]
    selects = ["'C' || lpad(i::VARCHAR, 6, '0')", "?", *("?" for _ in constants)]
    for column in columns:
        step = null_every.get(column)
        selects.append(
            f"CASE WHEN i % {step} = 0 THEN NULL ELSE 1.0 END" if step else "1.0"
        )
    # 留（第 4 类·多站点缺口，本轮不补 API）：表名与列清单都是本函数**参数**
    # （`_bulk_snapshot` / `_bulk_extended` 分别喂 `indicator_snapshot` /
    # `indicator_ext`），`SELECT ... FROM range(...)` 一次生成 `count` 行也不是
    # "播种一行"的语义 —— 按域方法表达不了，本轮如实登记为下一轮候选。
    duck.write_query(
        f"INSERT INTO {table} ({', '.join([*head, *columns])}) "
        f"SELECT {', '.join(selects)} FROM range(0, ?) AS t(i)",
        [report_date, *constants.values(), count],
    )


def _bulk_snapshot(
    duck: DuckDBStore, report_date: date, *, null_every: dict[str, int] | None = None,
) -> None:
    from app.core.screening.engine import SNAPSHOT_COLUMNS

    _bulk_domain(
        duck, "indicator_snapshot", sorted(SNAPSHOT_COLUMNS), report_date,
        constants={"calculated_at": NOW}, null_every=null_every,
    )


def _bulk_extended(
    duck: DuckDBStore, report_date: date, *, null_every: dict[str, int] | None = None,
) -> None:
    _bulk_domain(
        duck, "indicator_ext", sorted(EXTENDED_COLUMNS), report_date,
        constants={"calculated_at": NOW, "source": "fixture", "data_version": 1},
        null_every=null_every,
    )


def _stub_request(duck: DuckDBStore, sqlite: SQLiteStore) -> SimpleNamespace:
    state = SimpleNamespace(duck=duck, sqlite=sqlite)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _indicators_payload(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    from app.web.api.screening import list_available_indicators

    return list_available_indicators(_stub_request(duck, sqlite))


def _patch_warning_codes(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> None:
    """把"当前 warning codes"注入指标列表（不跑全量数据质量构建）。"""
    import app.core.data_quality as data_quality_module

    monkeypatch.setattr(data_quality_module, "read_warning_codes", lambda *a, **k: list(codes))


def _seed_screenable(duck: DuckDBStore, values: dict[str, Any] | None = None) -> None:
    """最小可筛选夹具：1 只票 + 1 行快照。"""
    values = values or {}
    seed = Seed(duck)
    # `name='One'`、`exchange='SZSE'` 逐字传（工厂默认值按代码段推断，与本文件夹具
    # 不是同一口径）；`calculated_at` 仍是本文件那个固定的 `NOW`，不是库侧时钟。
    seed.stock(
        "000001", name="One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot(
        "000001",
        "2025-12-31",
        calculated_at=NOW,
        latest_close=10,
        roe=values.get("roe", 12.0),
        dividend_yield=values.get("dividend_yield", 0.02),
    )
    seed.balance("000001", "2025-12-31", total_assets=100)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 告警分级（J9-1）
# ─────────────────────────────────────────────────────────────────────────────


def test_every_rule_declares_tier_and_severity() -> None:
    """每条规则（当前 14 条，以 RULE_CODES 为准）都不能漏登记调度/分级。

    断言直接对着 ``_RULES`` 元组表（唯一真源，``spec[3]``=tier、``spec[4]``=severity），
    不经过 name→tier/severity 的派生映射 —— 那两个映射没有消费方。
    """
    specs = {spec[0]: spec for spec in asset_health._RULES}
    assert set(specs) == set(RULE_CODES)
    assert {spec[4] for spec in asset_health._RULES} <= {
        SEVERITY_BLOCK, SEVERITY_DISCLOSE, SEVERITY_INVENTORY,
    }
    assert {spec[3] for spec in asset_health._RULES} <= {"cheap", "expensive"}
    assert specs["EXT_PERIOD_MISMATCH"][4] == SEVERITY_BLOCK
    assert specs["DEDUCTED_PROFIT_GAP"][4] == SEVERITY_BLOCK
    assert specs["ZERO_REFERENCE_ASSET"][4] == SEVERITY_INVENTORY
    assert specs["CROSSCHECK_BELOW_THRESHOLD"][3] == "expensive"


def test_structural_null_allowlist_covers_documented_fields() -> None:
    """结构性可空白名单必须覆盖"亏损/无分红/无商誉即合法为空"的那批字段。"""
    for field in (
        "pe_ttm", "pcf_ttm", "payout_ratio", "dividend_yield",
        "ttm_dividend_yield", "goodwill_ratio",
        "div_yield_spread_1y", "div_yield_spread_10y",
        "leverage_financial", "leverage_operating", "leverage_total",
    ):
        assert is_structurally_nullable(field), field
    # 真正的"该有值却没有"的字段不许混进白名单
    for field in ("roe", "revenue_yoy", "free_cash_flow", "net_margin"):
        assert not is_structurally_nullable(field), field


def test_structural_null_field_is_disclose_not_block(duckdb_store: DuckDBStore) -> None:
    """股息率 25% 覆盖：披露，但不进 P1（无分红公司本来就没有股息率）。"""
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"dividend_yield": 4})

    payload = _payload(duckdb_store)

    findings = [
        item for item in _findings(payload, "FIELD_COVERAGE_LOW")
        if item["field"] == "dividend_yield"
    ]
    assert len(findings) == 1
    assert findings[0]["severity"] == SEVERITY_DISCLOSE
    assert "结构性可空" in findings[0]["detail"]
    assert payload["level_counts"]["P1"] == 0
    assert "FIELD_COVERAGE_LOW" not in payload["p1_codes"]
    assert payload["severity_counts"]["disclose"] >= 1


def test_non_structural_field_stays_block(duckdb_store: DuckDBStore) -> None:
    """ROE 只有 50% 覆盖 = 真的坏了 → 必须仍是 block（P1）。"""
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"roe": 2})

    payload = _payload(duckdb_store)

    findings = [
        item for item in _findings(payload, "FIELD_COVERAGE_LOW")
        if item["field"] == "roe"
    ]
    assert len(findings) == 1
    assert findings[0]["severity"] == SEVERITY_BLOCK
    assert payload["level_counts"]["P1"] == 1
    assert payload["p1_codes"] == ["FIELD_COVERAGE_LOW"]
    assert payload["healthy"] is False


def test_disclose_findings_do_not_enter_warning_codes(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """P3 清单（inventory）与 disclose 都不得触发 warning_codes / healthy=False。"""
    status = build_data_quality_status(duckdb_store, sqlite_store)
    summary = status["asset_health"]

    assert summary["severity_counts"]["inventory"] > 0  # 零引用资产清单
    assert "ZERO_REFERENCE_ASSET" not in status["warning_codes"]
    assert "ZERO_REFERENCE_ASSET" not in summary["p1_codes"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. 字段级遮蔽（J9-2）
# ─────────────────────────────────────────────────────────────────────────────


def test_indicator_trust_masks_extended_domain_only() -> None:
    trust = indicator_trust(["EXT_PERIOD_MISMATCH"])

    assert trust["untrusted_all"] is False
    assert set(trust["masked_fields"]) == set(EXTENDED_COLUMNS)
    for snapshot_field in ("pe_ttm", "pb_mrq", "roe", "annualized_volatility"):
        assert snapshot_field not in trust["masked_fields"]

    masked = mask_untrusted_values(
        {"pe_ttm": 12.5, "free_cash_flow": 1e9, "roe": 0.15}, trust,
    )
    assert masked == {"pe_ttm": 12.5, "free_cash_flow": None, "roe": 0.15}


def test_indicator_trust_masks_deducted_profit_family() -> None:
    trust = indicator_trust(["DEDUCTED_PROFIT_GAP"])

    assert trust["untrusted_all"] is False
    assert set(trust["masked_fields"]) == set(DEDUCTED_PROFIT_INDICATOR_FIELDS)
    assert "pe_ttm" not in trust["masked_fields"]
    assert "revenue_yoy" not in trust["masked_fields"]

    masked = mask_untrusted_values(
        {"deducted_profit_yoy": 12.0, "net_profit_yoy": 8.0,
         "income.deducted_net_profit": 1e8},
        trust,
    )
    assert masked == {
        "deducted_profit_yoy": None,
        "net_profit_yoy": 8.0,
        "income.deducted_net_profit": None,
    }


def test_no_masking_without_the_two_codes() -> None:
    """其它资产健康码一律不遮蔽字段（披露 ≠ 不可信）。"""
    for code in (
        "FIELD_COVERAGE_LOW", "FIELD_COVERAGE_DROP", "MANUAL_CHAIN_STALE",
        "SNAPSHOT_PERIOD_GAP", "NULL_REASON_MISSING", "CROSSCHECK_BELOW_THRESHOLD",
        "SCHEMA_DDL_DRIFT", "ZERO_REFERENCE_ASSET", "CHAIN_PROGRESS_STALLED",
        "PACKAGE_STALE", "STATIC_TABLE_STALE",
    ):
        assert masked_fields_for_codes([code]) == [], code


def test_indicators_endpoint_moves_masked_extended_fields_to_unavailable(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_warning_codes(monkeypatch, ["EXT_PERIOD_MISMATCH"])

    payload = _indicators_payload(duckdb_store, sqlite_store)

    names = {item["name"] for item in payload["indicators"]}
    unavailable = {item["name"]: item for item in payload["unavailable"]}
    # 扩展域：整族被移到 unavailable，且原因明确
    assert "free_cash_flow" not in names
    assert "free_cash_flow" in unavailable
    assert unavailable["free_cash_flow"]["reason"] == "masked_untrusted"
    assert set(unavailable) >= set(EXTENDED_COLUMNS)
    # 快照域不受影响
    assert {"pe_ttm", "roe", "net_margin"} <= names
    assert payload["masked_fields"] == sorted(EXTENDED_COLUMNS)


def test_indicators_endpoint_masks_deducted_profit_only(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_warning_codes(monkeypatch, ["DEDUCTED_PROFIT_GAP"])

    payload = _indicators_payload(duckdb_store, sqlite_store)

    names = {item["name"] for item in payload["indicators"]}
    unavailable = {item["name"]: item for item in payload["unavailable"]}
    # reports/143 §2：CAGR 不再逐个暴露 `*_cagr3` / `*_cagr5`，
    # 只暴露基名 `deducted_profit_cagr`（参数下拉选 3/5 年）。
    # 遮蔽必须**向上传播**到基名，否则用户能选中一个实际会被 409 拒绝的入口。
    assert "deducted_profit_yoy" not in names
    assert unavailable["deducted_profit_yoy"]["reason"] == "masked_untrusted"

    for concrete in ("deducted_profit_cagr3", "deducted_profit_cagr5"):
        assert concrete not in names, f"{concrete} 不应再单独暴露"
    assert "deducted_profit_cagr" not in names, "基名必须随具体列一起被遮蔽"
    assert unavailable["deducted_profit_cagr"]["reason"] == "masked_untrusted"
    assert "deducted_profit_cagr3" in unavailable["deducted_profit_cagr"]["message"]

    assert "roe" in names and "revenue_yoy" in names


def test_engine_refuses_masked_field_condition(duckdb_store: DuckDBStore) -> None:
    """引用不可信字段 → 拒绝执行（不返回空集）。"""
    _seed_screenable(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)

    with pytest.raises(MaskedFieldError) as excinfo:
        engine.run(
            {"conditions": {"logic": "AND", "rules": [
                {"field": "free_cash_flow", "op": ">", "value": 1},
            ]}},
            min_listing_years=0,
            masked_fields=sorted(EXTENDED_COLUMNS),
        )

    detail = excinfo.value.as_detail()
    assert detail["reason_code"] == "masked_untrusted"
    assert detail["masked_fields"] == ["free_cash_flow"]


def test_engine_refuses_masked_rank_base_field(duckdb_store: DuckDBStore) -> None:
    """排名列（`fcf_margin_market_rank`）不能绕过遮蔽。"""
    _seed_screenable(duckdb_store)
    engine = ScreeningEngine(duck=duckdb_store)

    with pytest.raises(MaskedFieldError) as excinfo:
        engine.run(
            {"conditions": {"logic": "AND", "rules": [
                {"field": "fcf_margin_market_rank", "op": ">", "value": 1},
            ]}},
            min_listing_years=0,
            masked_fields=["fcf_margin"],
        )

    assert excinfo.value.fields == ["fcf_margin_market_rank"]


def test_engine_runs_normally_when_only_unrelated_fields_are_masked(
    duckdb_store: DuckDBStore,
) -> None:
    """遮蔽是字段级的：只遮扣非时，按 ROE 筛选必须照常跑出结果。"""
    _seed_screenable(duckdb_store, {"roe": 12.0})
    engine = ScreeningEngine(duck=duckdb_store)

    result = engine.run(
        {"conditions": {"logic": "AND", "rules": [
            {"field": "roe", "op": ">", "value": 5},
        ]}, "columns": ["stock_code", "roe"]},
        min_listing_years=0,
        masked_fields=list(DEDUCTED_PROFIT_INDICATOR_FIELDS),
    )

    assert [row["stock_code"] for row in result["results"]] == ["000001"]


def test_engine_without_explicit_masked_fields_derives_them_from_database(
    duckdb_store: DuckDBStore,
) -> None:
    """调用方没传时引擎自查（自选刷新/CLI 也有保护）。

    造一个"扩展域报告期落后快照"的库 → 扩展字段应被判为不可信。
    """
    _seed_screenable(duckdb_store)
    _bulk_extended(duckdb_store, date(2026, 6, 30))

    engine = ScreeningEngine(duck=duckdb_store)

    with pytest.raises(MaskedFieldError):
        engine.run(
            {"conditions": {"logic": "AND", "rules": [
                {"field": "free_cash_flow", "op": ">", "value": 1},
            ]}},
            min_listing_years=0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 体检分层调度（J10）
# ─────────────────────────────────────────────────────────────────────────────


def test_cheap_tier_never_runs_archive_crosscheck(
    duckdb_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cheap 档（状态页/每轮）绝不能触发 ~35s 的归档交叉核验。"""
    calls: list[str] = []

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        calls.append("crosscheck")
        raise AssertionError("cheap 档不允许执行归档交叉核验")

    monkeypatch.setattr(
        "app.core.quality.archive_crosscheck.run_crosscheck", _boom,
    )

    payload = _payload(duckdb_store, tier="cheap")

    assert calls == []
    assert payload["tier"] == "cheap"
    rule = next(
        item for item in payload["rules"] if item["code"] == "CROSSCHECK_BELOW_THRESHOLD"
    )
    assert rule["status"] == "skipped"
    assert rule["tier"] == "expensive"
    assert "CROSSCHECK_BELOW_THRESHOLD" in {item["code"] for item in payload["skipped"]}


def test_status_page_runs_cheap_only_but_passes_expensive_summary(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """状态页：只跑 cheap；expensive 的"上次运行"必须能从缓存透传。"""
    fresh = build_data_quality_status(duckdb_store, sqlite_store)
    assert fresh["asset_health"]["expensive"]["status"] == "not_run"
    assert not any(
        rule["code"] == "CROSSCHECK_BELOW_THRESHOLD" and rule["status"] == "warn"
        for rule in fresh["asset_health"]["rules"]
    )

    # 全量 doctor 跑一次 → 摘要落库
    doctor = run_doctor(duckdb_store, sqlite_store, now=NOW, allow_expensive=True)
    assert json.loads(
        sqlite_store.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [asset_health.EXPENSIVE_SUMMARY_STATE_KEY],
        )[0]["value"]
    )["status"] == "ok"

    # 状态页下一次构建就能透传（不必重跑 expensive）
    after = build_data_quality_status(duckdb_store, sqlite_store)
    expensive = after["asset_health"]["expensive"]
    assert expensive["status"] == "ok"
    assert expensive["generated_at"] == doctor["generated_at"]
    assert expensive["run_id"] == doctor["run_id"]
    stored_cached_at = sqlite_store.query(
        "SELECT updated_at FROM data_refresh_state WHERE key = ?",
        [asset_health.EXPENSIVE_SUMMARY_STATE_KEY],
    )[0]["updated_at"]
    assert expensive["cached_at"] == stored_cached_at, (
        "cached_at 必须是缓存行的写入时间（不是本次构建时间，否则过期缓存会装成新数据）"
    )
    assert expensive["crosscheck"] is not None
    assert expensive["crosscheck"]["tier"] == "expensive"
    # cheap 档本身依然把 expensive 规则标 skipped
    assert any(
        rule["code"] == "CROSSCHECK_BELOW_THRESHOLD" and rule["status"] == "skipped"
        for rule in after["asset_health"]["rules"]
    )


def test_expensive_summary_marks_not_run_without_cache(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    summary = load_expensive_summary(sqlite_store)

    assert summary["status"] == "not_run"
    assert "vd data doctor" in summary["reason"]


def test_schedule_contract_is_declared_in_config() -> None:
    """分层调度必须有显式配置契约，而不是散落在调用方代码里。"""
    config = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text("utf-8"))

    assert config["asset_health"]["schedule"] == {
        "cheap": "every_update",
        "expensive": "daily",
    }
    assert asset_health.DEFAULT_SCHEDULE == {"cheap": "every_update", "expensive": "daily"}


def test_status_summary_exposes_schedule_and_tier(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    status = build_data_quality_status(duckdb_store, sqlite_store)

    summary = status["asset_health"]
    assert summary["tier"] == "cheap"
    assert summary["schedule"] == {"cheap": "every_update", "expensive": "daily"}
    assert {rule["tier"] for rule in summary["rules"]} == {"cheap", "expensive"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. 废弃字段（J5）：dividend_yield
# ─────────────────────────────────────────────────────────────────────────────


def test_dividend_yield_is_deprecated() -> None:
    assert DEPRECATED_COLUMNS["dividend_yield"] == "ttm_dividend_yield"
    # 不删列：引擎仍认得它（旧规则必须还能跑）
    from app.core.screening.engine import SNAPSHOT_COLUMNS

    assert "dividend_yield" in SNAPSHOT_COLUMNS


def test_indicators_endpoint_hides_deprecated_dividend_yield(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_warning_codes(monkeypatch, [])

    payload = _indicators_payload(duckdb_store, sqlite_store)

    names = {item["name"] for item in payload["indicators"]}
    unavailable = {item["name"]: item for item in payload["unavailable"]}
    assert "dividend_yield" not in names
    assert unavailable["dividend_yield"]["reason"] == "deprecated"
    assert unavailable["dividend_yield"]["replaced_by"] == "ttm_dividend_yield"
    assert "ttm_dividend_yield" in names
    assert payload["deprecated_fields"] == ["dividend_yield"]


def test_deprecated_field_still_runs_with_warning(duckdb_store: DuckDBStore) -> None:
    """旧规则引用 dividend_yield：仍可执行，但结果必须带 warnings。"""
    _seed_screenable(duckdb_store, {"dividend_yield": 0.02})
    engine = ScreeningEngine(duck=duckdb_store)

    result = engine.run(
        {
            "conditions": {"logic": "AND", "rules": [
                {"field": "dividend_yield", "op": ">", "value": 0.01},
            ]},
            "columns": ["stock_code", "dividend_yield"],
        },
        min_listing_years=0,
        masked_fields=[],
    )

    assert [row["stock_code"] for row in result["results"]] == ["000001"]
    # 2026-11-15（reports/124）：警告里必须带量纲倍数——两列存储量纲差 100 倍
    # （dividend_yield 存小数、ttm_dividend_yield 存百分数，正式库 3,735 行实测全为 100×）。
    # 只提示"改用"会诱导用户把 >0.01 直接换成 >0.01，命中几乎全市场。
    assert result["warnings"] == [
        "field_deprecated:dividend_yield, 建议改用 ttm_dividend_yield"
        "（注意量纲差 100 倍：ttm_dividend_yield = dividend_yield × 100）"
    ]


def test_non_deprecated_rule_has_no_warnings(duckdb_store: DuckDBStore) -> None:
    _seed_screenable(duckdb_store, {"roe": 12.0})
    engine = ScreeningEngine(duck=duckdb_store)

    result = engine.run(
        {
            "conditions": {"logic": "AND", "rules": [
                {"field": "roe", "op": ">", "value": 5},
            ]},
            "columns": ["stock_code"],
        },
        min_listing_years=0,
        masked_fields=[],
    )

    assert result["warnings"] == []
    assert engine_module.DEPRECATED_COLUMNS == {"dividend_yield": "ttm_dividend_yield"}


def test_low_coverage_extended_field_is_hidden_as_not_available(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖率低于阈值的字段必须进 `unavailable(reason=not_available_for_latest_period)`。

    背景（reports/122 §2.5 J7）：三季报不披露现金流量表补充资料，折旧摊销/EBITDA/
    EV-EBITDA/经营杠杆 在 2026-09-30 的覆盖率全部 <1%，若不隐藏，用户会在筛选里
    选到一堆永远为空的字段。**这条"隐藏"分支此前没有任何回归用例**
    （`grep not_available_for_latest_period tests/` = 0），审计把它列为 J7 的唯一缺口。

    本用例构造"扩展域 3 行里只有 1 行有值"（覆盖率 33% < 50% 阈值），断言：
    ① 该字段出现在 `unavailable` 且 reason 明确；② 不出现在可选 `indicators` 里。
    """
    from app.web.api.screening import _EXTENDED_MIN_COVERAGE_PCT

    assert _EXTENDED_MIN_COVERAGE_PCT == 50.0, "阈值变了就要同步本用例的构造比例"

    _seed_screenable(duckdb_store)
    _patch_warning_codes(monkeypatch, [])
    # 扩展域：3 只票，只有 1 只有 free_cash_flow → 覆盖率 33%
    for index, code in enumerate(("000001", "000002", "000003")):
        # `source='test'` / `data_version=1` 逐字传（工厂不替调用点编"哪一版口径"）；
        # `calculated_at` 原 SQL 是 `CURRENT_TIMESTAMP` → 显式传哨兵（迁移守则）。
        Seed(duckdb_store).extended(
            code,
            "2026-09-30",
            free_cash_flow=1.0 if index == 0 else None,
            source="test",
            calculated_at=CURRENT_TIMESTAMP,
            data_version=1,
        )

    payload = _indicators_payload(duckdb_store, sqlite_store)

    names = {item["name"] for item in payload["indicators"]}
    unavailable = {item["name"]: item for item in payload["unavailable"]}
    assert "free_cash_flow" not in names, "覆盖率 33% 的字段不该出现在可选列表"
    assert "free_cash_flow" in unavailable, "必须进 unavailable 并说明原因"
    assert unavailable["free_cash_flow"]["reason"] == "not_available_for_latest_period"


# ─── 前端契约（2026-11-17 独立审计问题 3 / 4）────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_QUALITY_TS = _REPO_ROOT / "frontend" / "src" / "types" / "data-quality.ts"
_SCREENING_FORMAT_TS = _REPO_ROOT / "frontend" / "src" / "utils" / "screening-format.ts"


def _ts_section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start + len(start_marker))
    return text[start:end]


def test_frontend_warning_contract_covers_every_asset_health_rule() -> None:
    """`data-quality.ts` 四处必须与后端 RULE_CODES 同步（含第 14 条）。

    该文件没有编译期穷尽护栏（union 不写新 code 时 Record 仍能通过），
    所以只能靠本契约测试守住。漏一处的现实后果：状态页显示裸英文码。
    """
    text = _DATA_QUALITY_TS.read_text(encoding="utf-8")
    union = _ts_section(text, "export type KnownWarningCode =", "export type WarningCode =")
    known = _ts_section(text, "export const KNOWN_WARNING_CODES = [", "] as const")
    affected = _ts_section(text, "const WARNING_TO_AFFECTED_FIELDS", "// 中文文案")
    labels = _ts_section(text, "export const WARNING_CODE_LABELS", "export function warningCodeLabel")

    missing: list[str] = []
    for code in RULE_CODES:
        if f"'{code}'" not in union:
            missing.append(f"KnownWarningCode 缺少 {code}")
        if f"'{code}'" not in known:
            missing.append(f"KNOWN_WARNING_CODES 缺少 {code}")
        if not re.search(rf"^\s*{re.escape(code)}\s*:", affected, flags=re.MULTILINE):
            missing.append(f"WARNING_TO_AFFECTED_FIELDS 缺少 {code}")
        if not re.search(rf"^\s*{re.escape(code)}\s*:", labels, flags=re.MULTILINE):
            missing.append(f"WARNING_CODE_LABELS 缺少 {code}")
    assert not missing, "前端 warning code 契约不完整: " + "；".join(missing)

    technical_fields = _ts_section(
        text, "export const TECHNICAL_INDICATOR_FIELDS = [", "] as const",
    )
    for field in ("period_return", "annualized_volatility", "max_drawdown"):
        assert f"'{field}'" in technical_fields
    assert re.search(
        r"TECHNICAL_COVERAGE_GAP:\s*TECHNICAL_INDICATOR_FIELDS", affected,
    )
    assert "技术指标覆盖率不足" in labels


def test_frontend_yoy_labels_distinguish_ttm_and_annual_calibers() -> None:
    """`*_yoy` / `*_yoy_fy` 必须分别标成 TTM 口径 / 年报口径，不能同名。"""
    text = _SCREENING_FORMAT_TS.read_text(encoding="utf-8")
    expected = {
        "revenue_yoy": "营业收入同比(TTM口径)",
        "net_profit_yoy": "净利润同比(TTM口径)",
        "deducted_profit_yoy": "扣非净利润同比(TTM口径)",
        "revenue_yoy_fy": "营业收入同比(年报口径)",
        "net_profit_yoy_fy": "净利润同比(年报口径)",
        "deducted_profit_yoy_fy": "扣非净利润同比(年报口径)",
    }
    missing = [
        f"{field} → {label}"
        for field, label in expected.items()
        if f"{field}: '{label}'" not in text
    ]
    assert not missing, "缺少同比口径中文名: " + "；".join(missing)

    pct_fields = _ts_section(text, "const PCT_FIELDS = new Set([", "])")
    for field in ("revenue_yoy_fy", "net_profit_yoy_fy", "deducted_profit_yoy_fy"):
        assert f"'{field}'" in pct_fields, f"{field} 必须登记为 pct（小数比例）"
