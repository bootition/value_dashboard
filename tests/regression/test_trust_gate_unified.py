"""统一可信度门禁回归：详情页与筛选页必须给出一致答案。

背景（实测现场）：`LINEAGE_INVALID` 曾让 `indicator_trust()["untrusted_all"]`
为 True、详情页 34 个指标全部 null，而 `/api/screening/run` 照常返回排名。
本文件锁定统一口径：

- `untrusted_all=True` → 筛选 409，`detail.reason="snapshot_values_untrusted"`；
- `untrusted_all=False` 但 `masked_fields` 非空 → 仍由引擎按字段遮蔽 409；
- `untrusted_all=False` 且无遮蔽 → 正常出结果；
- 同一份 fixture 下，详情页与筛选页的"可信/不可信"结论必须相同。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.data_quality import (
    SNAPSHOT_UNTRUSTED_REASON,
    indicator_trust,
    screenability_block_reason,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router as screening_router
from app.web.api.stock_detail import router as stock_detail_router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import CURRENT_DATE, CURRENT_TIMESTAMP, Seed

STOCK_CODE = "000001"


def _build_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(screening_router)
    app.include_router(stock_detail_router)
    return TestClient(app)


def _seed_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    Seed(duck).stock(
        STOCK_CODE, name="Trust Test", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duck, STOCK_CODE)
    insert_matching_trading_calendar(duck, sqlite)
    # 快照域数值：详情页与筛选页读的是同一行。`insert_minimum_screenable_data()` 刚刚
    # 建过同一个 `(stock_code, '2025-12-31')` 行，所以必须 `overwrite=True`
    # （INSERT OR REPLACE）—— 语义等同原 SQL 的 `ON CONFLICT ... DO UPDATE`，
    # 而且九列逐字写全，不会把 conftest 写的那三列替换成 NULL。
    # `CURRENT_DATE` / `CURRENT_TIMESTAMP` 走哨兵：仍是库侧时钟，不是 Python 的 now()。
    Seed(duck).snapshot(
        STOCK_CODE, "2025-12-31",
        latest_close=10,
        latest_price_date=CURRENT_DATE,
        calculated_at=CURRENT_TIMESTAMP,
        pe_ttm=12.5, pb_mrq=1.5, roe=0.2, dividend_yield=0.02,
        overwrite=True,
    )


def _poison_lineage(duck: DuckDBStore) -> None:
    """制造真实 LINEAGE_INVALID：非法摘要 + 孤儿批次。"""
    # 留（第 6 类·库侧相对时间）：`CURRENT_TIMESTAMP - INTERVAL '1 day'` 是"库侧昨天"，
    # 这条审计行要比归档行**早**才算合法溯源史。**本轮实证不可收敛**：DuckDB 侧工厂的
    # `SqlValue` 哨兵只有裸 `CURRENT_TIMESTAMP`/`CURRENT_DATE`，而 `sql_now("-1 day")`
    # 生成的 `datetime('now', '-1 day')` 是 SQLite 语法（DuckDB 无此函数，CatalogException）。
    duck.write_query(
        """INSERT INTO source_audit
           (stock_code, field_name, report_date, value, source, fetch_batch_id,
            fetch_time, raw_response_hash, confidence)
           VALUES (?, 'revenue', DATE '2025-12-31', 1, 'tampered', 'missing-batch',
                   CURRENT_TIMESTAMP - INTERVAL '1 day', 'not-a-sha', 'strict')""",
        [STOCK_CODE],
    )


def _patch_warning_codes(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> None:
    """注入当前 warning codes（两个页面都在调用时从同一模块读取）。"""
    import app.core.data_quality as data_quality_module

    monkeypatch.setattr(
        data_quality_module, "read_warning_codes", lambda *a, **k: list(codes),
    )


def _save_rule(client: TestClient, field: str = "pe_ttm") -> dict:
    response = client.post(
        "/api/screening/rules/save",
        json={
            "name": f"trust-gate-{field}",
            "rule_json": {
                "conditions": {
                    "logic": "AND",
                    "rules": [{"field": field, "op": ">", "value": 0}],
                },
                "columns": ["stock_code", "name", field],
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def _run(client: TestClient, rule: dict) -> object:
    return client.post(
        "/api/screening/run",
        json={
            "rule_id": rule["rule_id"],
            "rule_version": rule["version"],
            "min_listing_years": 0,
        },
    )


def _detail_metrics(body: dict) -> list[dict]:
    return [
        metric
        for category in body["indicators"].values()
        for metric in category.values()
    ]


def test_screenability_block_reason_is_the_single_translation() -> None:
    assert screenability_block_reason(indicator_trust(["LINEAGE_INVALID"])) == (
        SNAPSHOT_UNTRUSTED_REASON
    )
    assert SNAPSHOT_UNTRUSTED_REASON == "snapshot_values_untrusted"
    # 字段级遮蔽不是"整页不可信"，不得升级成全局阻断原因。
    assert screenability_block_reason(indicator_trust(["EXT_PERIOD_MISMATCH"])) is None
    assert screenability_block_reason(indicator_trust([])) is None


def test_run_rejects_untrusted_snapshot_with_reason(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    _patch_warning_codes(monkeypatch, ["LINEAGE_INVALID"])
    client = _build_client(duckdb_store, sqlite_store)
    rule = _save_rule(client)

    response = _run(client, rule)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "snapshot_values_untrusted"
    assert detail["reason_code"] == "snapshot_values_untrusted"
    assert "LINEAGE_INVALID" in detail["warning_codes"]


def test_indicators_exposes_untrusted_all_and_warning_codes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    _patch_warning_codes(monkeypatch, ["LINEAGE_INVALID"])
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/screening/indicators")

    assert response.status_code == 200
    body = response.json()
    assert body["untrusted_all"] is True
    assert "LINEAGE_INVALID" in body["warning_codes"]


def test_masked_fields_still_use_field_level_409(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """untrusted_all=False + masked_fields 非空：维持引擎 MaskedFieldError。"""
    _seed_screenable(duckdb_store, sqlite_store)
    _patch_warning_codes(monkeypatch, ["DEDUCTED_PROFIT_GAP"])
    client = _build_client(duckdb_store, sqlite_store)
    rule = _save_rule(client, field="deducted_profit_yoy")

    response = _run(client, rule)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "masked_untrusted"
    assert "deducted_profit_yoy" in detail["masked_fields"]
    # 不得被误升级为整页不可信
    assert detail.get("reason") != "snapshot_values_untrusted"

    indicators = client.get("/api/screening/indicators").json()
    assert indicators["untrusted_all"] is False
    assert "deducted_profit_yoy" in indicators["masked_fields"]


def test_trusted_snapshot_runs_normally(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_screenable(duckdb_store, sqlite_store)
    _patch_warning_codes(monkeypatch, [])
    client = _build_client(duckdb_store, sqlite_store)
    rule = _save_rule(client)

    response = _run(client, rule)

    assert response.status_code == 200
    body = response.json()
    assert [row["stock_code"] for row in body["results"]] == [STOCK_CODE]


def test_detail_and_screening_agree_when_trusted(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """同一份 fixture：详情页"可信" ⇔ 筛选页正常返回。"""
    _seed_screenable(duckdb_store, sqlite_store)
    client = _build_client(duckdb_store, sqlite_store)

    detail = client.get(f"/api/stock/{STOCK_CODE}/indicators")
    assert detail.status_code == 200
    detail_body = detail.json()
    detail_trusted = not detail_body["trust"]["untrusted_all"]
    assert detail_trusted is True
    assert detail_body["indicators"]["valuation"]["pe_ttm"]["value"] == 12.5
    assert detail_body["indicators"]["valuation"]["pe_ttm"]["untrusted"] is False
    assert detail_body["latest_close"] == 10

    rule = _save_rule(client)
    run = _run(client, rule)
    assert run.status_code == 200
    assert (run.status_code == 200) is detail_trusted


def test_detail_and_screening_agree_when_untrusted(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """同一份 fixture：详情页"整页 null" ⇔ 筛选页 409 snapshot_values_untrusted。"""
    _seed_screenable(duckdb_store, sqlite_store)
    _poison_lineage(duckdb_store)
    client = _build_client(duckdb_store, sqlite_store)

    detail = client.get(f"/api/stock/{STOCK_CODE}/indicators")
    assert detail.status_code == 200
    detail_body = detail.json()
    detail_trusted = not detail_body["trust"]["untrusted_all"]
    assert detail_trusted is False
    assert "LINEAGE_INVALID" in detail_body["trust"]["warning_codes"]
    metrics = _detail_metrics(detail_body)
    assert metrics
    assert all(metric["value"] is None for metric in metrics)
    assert all(metric["untrusted"] is True for metric in metrics)
    assert detail_body["latest_close"] is None

    rule = _save_rule(client)
    run = _run(client, rule)
    assert run.status_code == 409
    detail_payload = run.json()["detail"]
    assert detail_payload["reason"] == "snapshot_values_untrusted"
    assert "LINEAGE_INVALID" in detail_payload["warning_codes"]
    assert (run.status_code == 200) is detail_trusted
