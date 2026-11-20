"""扩展指标覆盖率门禁「按字段基准期」回归（reports/145）。

事故
----
用户截图：筛选页「当前报告期不可用指标」里，折旧摊销、EBITDA、EBITDA 对负债、
企业价值倍数（EV/EBITDA）、经营杠杆、综合杠杆全都标「覆盖率 0.1%」。

根因
----
`_extended_field_coverage()` 原来只统计 `MAX(report_date)` 这一期。但**一季报 /
三季报天生不披露现金流量表补充资料**，折旧摊销族在 Q1/Q3 结构性地只剩个位数
（正式库实测：03-31 平均 174 只、09-30 平均 211 只有值，而 06-30 是 2,017、
12-31 是 2,140）。于是每当最新期落在季报，EBITDA 全家被整族隐藏 —— 而它们在
最近一个半年报/年报里覆盖率 74%–99.7%，完全可用。

不是数据坏了，也不是脏数据，是**门禁拿错了基准期**。

本文件钉死三件事：

1. 最新期缺该字段、上一期有 → 字段必须**暴露**，并带上 `basis_report_date`；
2. 最近若干期都缺 → 仍然进 `unavailable`（不能因为放宽就放过真没数据的字段）；
3. `basis_report_date` 与最新期相同时**不**下发（避免前端给所有字段都加提示）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router


def _client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def _seed_ext_period(
    duck: DuckDBStore,
    report_date: str,
    *,
    ebitda_value: float | None,
    codes: int = 10,
) -> None:
    """铺一期 indicator_ext：`ebitda_value=None` 模拟季报未披露折旧摊销。"""
    for index in range(codes):
        code = f"9{index:05d}"
        # 留（第 6 类·库侧相对时间）：`indicator_ext.calculated_at` 写库侧 `now()`，
        duck.write_query(
            """INSERT INTO indicator_ext
                   (stock_code, report_date, ebitda, calculated_at, source, data_version)
               VALUES (?, CAST(? AS DATE), ?, now(), 'fixture', 1)""",
            [code, report_date, ebitda_value],
        )


def _ebitda_entry(body: dict) -> dict | None:
    for item in body["indicators"]:
        if item["name"] == "ebitda":
            return item
    return None


def _ebitda_unavailable(body: dict) -> dict | None:
    for item in body["unavailable"]:
        if item["name"] == "ebitda":
            return item
    return None


def test_field_falls_back_to_last_period_with_data(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """最新季报没有折旧摊销 → 用最近半年报的覆盖率放行，并标出基准期。"""
    _seed_ext_period(duckdb_store, "2026-06-30", ebitda_value=100.0)   # 半年报：全覆盖
    _seed_ext_period(duckdb_store, "2026-09-30", ebitda_value=None)    # 三季报：全空

    body = _client(duckdb_store, sqlite_store).get("/api/screening/indicators").json()

    entry = _ebitda_entry(body)
    assert entry is not None, "EBITDA 不该因为最新季报缺折旧摊销而被隐藏"
    assert entry["basis_report_date"] == "2026-06-30"
    assert entry["basis_label"] == "2026 半年报"
    assert entry["latest_report_date"] == "2026-09-30"
    assert entry["coverage_pct"] == 100.0
    assert _ebitda_unavailable(body) is None


def test_field_hidden_when_no_period_has_data(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """最近几期都没有该字段 → 仍然不可用（放宽不能放过真没数据的字段）。"""
    _seed_ext_period(duckdb_store, "2026-06-30", ebitda_value=None)
    _seed_ext_period(duckdb_store, "2026-09-30", ebitda_value=None)

    body = _client(duckdb_store, sqlite_store).get("/api/screening/indicators").json()

    assert _ebitda_entry(body) is None, "确实没数据的字段不该被放出来"
    unavailable = _ebitda_unavailable(body)
    assert unavailable is not None
    assert unavailable["reason"] == "not_available_for_latest_period"
    assert "覆盖率" in unavailable["message"]


def test_no_basis_note_when_latest_period_has_data(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """最新期本身就有数据 → 不下发 basis_report_date（前端不该加多余提示）。"""
    _seed_ext_period(duckdb_store, "2026-06-30", ebitda_value=100.0)
    _seed_ext_period(duckdb_store, "2026-09-30", ebitda_value=100.0)

    body = _client(duckdb_store, sqlite_store).get("/api/screening/indicators").json()

    entry = _ebitda_entry(body)
    assert entry is not None
    assert "basis_report_date" not in entry
    assert "basis_label" not in entry


def test_partial_coverage_uses_threshold() -> None:
    """覆盖率门槛语义不变：低于阈值的期不能当基准期。"""
    from app.web.api.screening import _EXTENDED_MIN_COVERAGE_PCT

    assert _EXTENDED_MIN_COVERAGE_PCT == 50.0, "阈值变了要同步本文件构造的比例"
