"""筛选门禁的告警三级（block / disclose / inventory）回归（reports/150）。

背景（用户实测）：点「运行筛选」只弹出红字
「数据质量不满足筛选要求；请前往「数据状态」页查看警告」。
根因不是"数据坏了"，而是 `screening_readiness()` 把一批**披露级**告警
当成硬阻断：6 条里任意一条命中 → 整个筛选功能不可用。正式库实测阻断
来源单一 —— 交易日历「存在但个别标的覆盖不全」（197/5,580 只 = 3.5%），
`_has_complete_trading_calendar()` 对它零容忍并且把结论一律返回 False。

本文件锁三件事：
 1. **disclose 级不拦**：只有披露级告警时 `ready=True`、`warning_codes` 为空
    但 `disclosed_warnings` 如实带出（不丢失披露语义）。
 2. **block 级一条不少**：`_SCREENING_BLOCKING_WARNINGS` 必须只由 severity
    表派生，且仍是 `LIVE_SCHEMA_INCOMPATIBLE` + `TRADING_CALENDAR_UNAVAILABLE`；
    block 级仍然 409，`reason_code` 不变（前端 `api-error.ts` 映射不变）。
 3. **日历判定不许 fail-open**：门禁缓存必须存真实 verdict，
    不能把失败写成 True（旧实现写死 `{"result": True}`）。
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import data_quality as dq
from app.core.data_quality import (
    _SCREENING_BLOCKING_WARNINGS,
    _SCREENING_WARNING_SEVERITY,
    _trading_calendar_coverage,
    screening_readiness,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed


def _quality(warning_codes: list[str], *, ready: bool = True) -> dict:
    return {
        "minimum_data_readiness": {"ready": ready, "stock_count": 1, "missing": {}, "missing_counts": {}},
        "dates": {
            "price": "2026-11-20",
            "balance_sheet": {"latest_record": "2026-09-30", "latest_complete": "2026-09-30"},
            "income_statement": {"latest_record": "2026-09-30", "latest_complete": "2026-09-30"},
            "indicator_snapshot": {"latest_complete": "2026-09-30", "latest_price_date": "2026-11-19"},
        },
        "lineage": {"invalid_hash_rows": 0, "orphan_batch_rows": 0, "archive_gap_rows": 0, "hash_mismatch_rows": 0},
        "code_identity": {"raw_alias_rows": 0},
        "operations": {"unpublished_overrides": 0, "running_jobs": 0},
        "warning_codes": list(warning_codes),
        "asset_health": {},
    }


def _patch_decision(monkeypatch, *, warning_codes: list[str], calendar: dict | None = None,
                    readiness_ready: bool = True) -> None:
    monkeypatch.setattr(
        dq, "build_data_quality_status_cached",
        lambda duck, sqlite: _quality(warning_codes, ready=readiness_ready),
    )
    monkeypatch.setattr(
        dq, "_trading_calendar_coverage",
        lambda duck, sqlite: calendar or {"verdict": "ok", "calendar_days": 8000},
    )


# ── 1. 单点派生：block 清单只能来自 severity 表，且不含披露级 ──────────
def test_blocking_set_is_derived_from_severity_table() -> None:
    derived = frozenset(
        code for code, severity in _SCREENING_WARNING_SEVERITY.items()
        if severity == dq.SCREENING_SEVERITY_BLOCK
    )
    assert derived == _SCREENING_BLOCKING_WARNINGS
    # 安全底线：真的会让结果错的必须继续拦。
    assert "LIVE_SCHEMA_INCOMPATIBLE" in _SCREENING_BLOCKING_WARNINGS
    assert "TRADING_CALENDAR_UNAVAILABLE" in _SCREENING_BLOCKING_WARNINGS
    # 降级为披露级的 4 条快照域告警（硬阻断由 `_SNAPSHOT_BLOCKING_WARNINGS`
    # + `_require_trusted_snapshot()` 唯一负责，不在本表重复判断）。
    for code in ("FINANCIAL_SHELL_ROWS", "SNAPSHOT_STALE", "CODE_IDENTITY_ALIAS", "LINEAGE_INVALID"):
        assert code not in _SCREENING_BLOCKING_WARNINGS
        assert _SCREENING_WARNING_SEVERITY[code] == dq.SCREENING_SEVERITY_DISCLOSE


# ── 2. 核心：只有 disclose 级告警时 ready=True，且披露项不丢 ─────────────
@pytest.mark.parametrize("code", [
    "SNAPSHOT_STALE",
    "FINANCIAL_SHELL_ROWS",
    "CODE_IDENTITY_ALIAS",
    "LINEAGE_INVALID",
    "TRADING_CALENDAR_INCOMPLETE",
])
def test_disclose_only_warning_keeps_screening_ready(monkeypatch, code: str) -> None:
    _patch_decision(monkeypatch, warning_codes=[code])

    decision = screening_readiness(types.SimpleNamespace(), types.SimpleNamespace())

    assert decision["ready"] is True
    # 兼容语义：`warning_codes` 仍是"硬阻断码"，此刻必须为空。
    assert decision["warning_codes"] == []
    assert decision["blocking_warnings"] == []
    assert decision["disclosed_warnings"] == [code]
    (warning,) = [w for w in decision["warnings"] if w["code"] == code]
    assert warning["severity"] == "disclose"


# ── 3. 安全底线：block 级仍然 ready=False，且仍是 `warning_codes` ────────
@pytest.mark.parametrize("code", ["LIVE_SCHEMA_INCOMPATIBLE", "TRADING_CALENDAR_UNAVAILABLE"])
def test_block_warning_still_blocks(monkeypatch, code: str) -> None:
    _patch_decision(monkeypatch, warning_codes=[code])

    decision = screening_readiness(types.SimpleNamespace(), types.SimpleNamespace())

    assert decision["ready"] is False
    assert decision["warning_codes"] == [code]
    (warning,) = decision["warnings"]
    assert warning["severity"] == "block"


def test_entirely_unavailable_calendar_blocks(monkeypatch) -> None:
    _patch_decision(monkeypatch, warning_codes=[], calendar={"verdict": "unavailable", "calendar_days": 0})

    decision = screening_readiness(types.SimpleNamespace(), types.SimpleNamespace())

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


# ── 4. 日历判定本身：partial → 披露（不 fail-closed 到整库拦截）───────────
def _seed_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    Seed(duck).stock(
        "000001", name="Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duck)
    insert_matching_trading_calendar(duck, sqlite)


def _add_price_less_listed_stocks(duck: DuckDBStore, codes: tuple[str, ...]) -> None:
    """给库补几个**没有价格序列**的上市股：分母变大，单个坏标的占比下降。"""
    for code in codes:
        Seed(duck).stock(
            code, name=f"Extra{code}", exchange="SZSE", listing_date="2020-01-01",
            is_st=False, is_suspended=False,
        )


def _break_one_calendar_date(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    gap = duck.read_query(
        """SELECT CAST(trade_date AS VARCHAR) AS trade_date FROM price_daily_qfq
           WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           ORDER BY trade_date DESC LIMIT 1"""
    )[0]["trade_date"]
    sqlite.execute("DELETE FROM trading_dates WHERE trade_date = ?", [gap])


def test_partial_calendar_is_disclosed_not_blocked(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    _add_price_less_listed_stocks(duckdb_store, ("600002", "600003", "600004"))
    _break_one_calendar_date(duckdb_store, sqlite_store)

    coverage = _trading_calendar_coverage(duckdb_store, sqlite_store)
    assert coverage["verdict"] == "partial"
    assert coverage["affected_stocks"] == 1
    assert coverage["listed_stocks"] == 4

    decision = screening_readiness(duckdb_store, sqlite_store)
    assert decision["ready"] is True
    assert decision["warning_codes"] == []
    assert "TRADING_CALENDAR_INCOMPLETE" in decision["disclosed_warnings"]


def test_systemic_calendar_damage_still_blocks(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """受影响上市股 ≥ 1/3 → 视同"无法判定窗口"，仍然硬阻断（安全底线）。"""
    _seed_screenable(duckdb_store, sqlite_store)
    _break_one_calendar_date(duckdb_store, sqlite_store)
    # 1 只受影响 / 1 只上市 = 100% ≥ 1/3 → unavailable
    coverage = _trading_calendar_coverage(duckdb_store, sqlite_store)
    assert coverage["verdict"] == "unavailable"

    decision = screening_readiness(duckdb_store, sqlite_store)
    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


# ── 5. 门禁缓存不许 fail-open（旧实现把失败写成 {"result": True}）────────
def test_calendar_gate_cache_never_turns_failure_into_success(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    _break_one_calendar_date(duckdb_store, sqlite_store)

    first = _trading_calendar_coverage(duckdb_store, sqlite_store)
    assert first["verdict"] != "ok"
    # 第二次读的是刚刚写入的门禁缓存：结论必须与第一次**一致**。
    second = _trading_calendar_coverage(duckdb_store, sqlite_store)
    assert second["verdict"] == first["verdict"]

    cached = sqlite_store.query(
        "SELECT value FROM data_refresh_state WHERE key = 'trading_calendar_gate_cache'"
    )
    assert cached, "门禁缓存必须落盘（否则每次筛选都要重扫价格表）"
    import json as _json
    payload = _json.loads(cached[0]["value"])
    assert payload["status"]["verdict"] == first["verdict"]


# ── 6. HTTP 层：disclose 级 → 200 + 结果 + disclosed_warnings ────────────
def _make_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.state.startup_readiness = {}
    app.include_router(router)
    return TestClient(app)


def _save_rule(client: TestClient) -> dict:
    """规则用 `latest_close`：门禁 fixture 的 `indicator_snapshot` 只种了这一列
    （`pe_ttm` 为 NULL，用它做条件会得到 0 行，无法证明"放行后真的能出结果"）。"""
    response = client.post(
        "/api/screening/rules/save",
        json={
            "name": "severity-value",
            "rule_json": {
                "conditions": {"logic": "AND", "rules": [{"field": "latest_close", "op": ">", "value": 0}]},
                "columns": ["stock_code", "name", "latest_close"],
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def test_run_screening_returns_200_with_results_when_only_disclose_level(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """**核心用例**：只有披露级告警时 `run_screening` 必须 200 且有结果。"""
    _seed_screenable(duckdb_store, sqlite_store)
    _add_price_less_listed_stocks(duckdb_store, ("600002", "600003", "600004"))
    _break_one_calendar_date(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _save_rule(client)

    response = client.post(
        "/api/screening/run",
        json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["results"], "披露级告警不得让筛选返回空结果"
    assert body["total"] >= 1
    # 如实披露：200 响应里必须看得见披露项（前端显示成提示，不是错误）。
    assert "TRADING_CALENDAR_INCOMPLETE" in body["disclosed_warnings"]
    assert set(body["disclosed_warnings"]) == {w["code"] for w in body["screening_warnings"]}


def test_run_screening_still_409_when_block_level(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """安全底线：block 级（日历整体不可用）仍然 409，`reason_code` 不变。"""
    Seed(duckdb_store).stock(
        "000001", name="Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duckdb_store)
    # 故意不建交易日历 → TRADING_CALENDAR_UNAVAILABLE（block 级）。
    client = _make_client(duckdb_store, sqlite_store)
    rule = _save_rule(client)

    response = client.post(
        "/api/screening/run",
        json={"rule_id": rule["rule_id"], "rule_version": rule["version"], "min_listing_years": 0},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "screening_data_quality_not_ready"
    assert detail["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]
