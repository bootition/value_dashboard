from __future__ import annotations

import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import resolve_rule_indicator_locks, router
from app.web.api.stock_detail import router as stock_detail_router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import CURRENT_TIMESTAMP, Seed
from tests.support.seed_sqlite import SeedSqlite

# 已收敛：`screening_rules` 1 处 + `dsl_expressions` 2 处 → `SeedSqlite`
# （后两处原本是事务内 `executemany`，改用 `join(conn)` 后提交权仍归调用方）；
# 本轮追加：`_make_screenable` 的 `stock_meta`/`indicator_snapshot` 与
# 技术覆盖（100 行 `executemany`）/同比口径（单行）两条用例的 `indicator_snapshot`
# → `Seed`，`calculated_at` 一律交回库侧 `CURRENT_TIMESTAMP` 哨兵。
# 仍保留内联的只有 `screening_runs`：工厂没有方法（见 `tests/support/seed_sqlite.py`
# 「尚未覆盖」），属第 4 类「多站点缺口，本轮不补 API」。


def _make_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(router)
    return TestClient(app)


def _make_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    # 「可筛选股票」脚手架：`name='One'`、`exchange='SZSE'` 逐字传（工厂默认值按
    # 代码段推断，与本文件夹具不是同一口径）。
    seed = Seed(duck)
    seed.stock(
        "000001", name="One", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    seed.snapshot("000001", "2025-12-31", pe_ttm=10)
    insert_minimum_screenable_data(duck)
    insert_matching_trading_calendar(duck, sqlite)


# ─── 3a: 草稿 revision 乐观锁原子条件更新 ──────────────────────────────────

def test_draft_stale_revision_conflicts(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    client = _make_client(duckdb_store, sqlite_store)
    draft = {"conditions": {"logic": "AND", "rules": []}}

    first = client.put("/api/screening/draft", json={"draft": draft, "revision": 0})
    assert first.status_code == 200
    assert first.json()["revision"] == 1

    stale = client.put("/api/screening/draft", json={"draft": draft, "revision": 0})
    assert stale.status_code == 409

    current = client.put(
        "/api/screening/draft",
        json={"draft": draft, "revision": first.json()["revision"]},
    )
    assert current.status_code == 200
    assert current.json()["revision"] == 2


def test_draft_concurrent_puts_with_same_revision_only_one_succeeds(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    client = _make_client(duckdb_store, sqlite_store)
    first = client.put(
        "/api/screening/draft",
        json={"draft": {"conditions": {"logic": "AND", "rules": []}}, "revision": 0},
    )
    assert first.status_code == 200
    revision = first.json()["revision"]

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def put_once() -> None:
        barrier.wait()
        response = client.put(
            "/api/screening/draft",
            json={"draft": {"conditions": {"logic": "OR", "rules": []}}, "revision": revision},
        )
        statuses.append(response.status_code)

    workers = [threading.Thread(target=put_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(statuses) == [200, 409]
    assert client.get("/api/screening/draft").json()["revision"] == revision + 1


# ─── 3b + 1d: /run 对未知 columns / min_listing_years / sort 返回 400 ───────

def _saved_empty_rule(client: TestClient) -> dict:
    response = client.post(
        "/api/screening/rules/save",
        json={"name": "api-regression", "rule_json": {"conditions": {"logic": "AND", "rules": []}}},
    )
    assert response.status_code == 200
    return response.json()


def test_run_unknown_request_columns_returns_400(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _saved_empty_rule(client)

    response = client.post(
        "/api/screening/run",
        json={
            "rule_id": rule["rule_id"],
            "rule_version": rule["version"],
            "min_listing_years": 0,
            "columns": ["stock_code", "does_not_exist"],
        },
    )

    assert response.status_code == 400
    assert "未知结果字段" in response.json()["detail"]
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM screening_runs")[0]["c"] == 0


def test_run_out_of_range_min_listing_years_returns_400(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = _saved_empty_rule(client)

    for bad_years in (-1, 101):
        response = client.post(
            "/api/screening/run",
            json={
                "rule_id": rule["rule_id"],
                "rule_version": rule["version"],
                "min_listing_years": bad_years,
            },
        )

        assert response.status_code == 400
        assert "0-100" in response.json()["detail"]


def test_run_non_dict_sort_returns_400_not_500(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule_json = {"conditions": {"logic": "AND", "rules": []}, "sort": ["pe_ttm"]}
    with sqlite_store.transaction() as conn:
        # `join(conn)`：事务已在调用点手里，工厂只写不提交。
        SeedSqlite(sqlite_store).join(conn).screening_rule(
            "legacy-bad-sort", 1, json.dumps(rule_json), status="saved"
        )

    response = client.post(
        "/api/screening/run",
        json={"rule_id": 1, "rule_version": 1, "min_listing_years": 0},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "排序项必须是对象"


# ─── 3c: watchlist executemany + 去重 ──────────────────────────────────────

def test_add_to_watchlist_deduplicates_requested_codes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _make_screenable(duckdb_store, sqlite_store)
    client = _make_client(duckdb_store, sqlite_store)
    rule = client.post(
        "/api/screening/rules/save",
        json={"name": "watchlist-value", "rule_json": {"conditions": {"logic": "AND", "rules": []}}},
    ).json()
    with sqlite_store.transaction() as conn:
        # 留（第 4 类·多站点缺口，本轮不补 API）：`screening_runs` 全仓 3 处播种、
        # 工厂没有方法；本轮租约禁止扩 `seed_sqlite.py`，如实登记为下一轮候选。
        conn.execute(
            """INSERT INTO screening_runs
               (run_id, rule_id, rule_version, result_json, columns_json, sort_json,
                data_date, base_pool_config, strict_only, confidence_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "watchlist-run", rule["rule_id"], rule["version"],
                '[{"stock_code":"000001","name":"One"}]', '["stock_code","name"]', '[]',
                "2025-12-31", '{}', False, '{}',
            ],
        )
    result_id = client.post(
        "/api/screening/save", json={"title": "watchlist result", "run_id": "watchlist-run"}
    ).json()["result_id"]

    response = client.post(
        "/api/screening/add_to_watchlist",
        json={"stock_codes": ["000001", "000001"], "group": "screening", "result_id": result_id},
    )

    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert sqlite_store.query(
        "SELECT stock_code, group_name FROM watchlist"
    ) == [{"stock_code": "000001", "group_name": "screening"}]


# ─── 3d: 规则锁只认最新 published 版本 ─────────────────────────────────────

def test_resolve_rule_indicator_locks_uses_max_published_version(
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as conn:
        seed = SeedSqlite(sqlite_store).join(conn)
        for version, status, content_hash in [
            (1, "published", "hash-v1"),
            (2, "published", "hash-v2"),
            (3, "draft", "hash-v3"),
        ]:
            seed.dsl_expression(
                "locked_ind", version, "pe_ttm", status=status, content_hash=content_hash
            )

    locks = resolve_rule_indicator_locks(
        sqlite_store,
        {"conditions": {"logic": "AND", "rules": [{"field": "locked_ind", "op": ">", "value": 0}]}},
        {},
    )

    assert locks == {"locked_ind": {"version": 2, "content_hash": "hash-v2"}}


# ─── 3e + 1h: /indicators DSL 单位语义与 sw1 不暴露 ────────────────────────

def test_indicators_derive_dsl_units_and_explain_plain_semantics(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with sqlite_store.transaction() as conn:
        seed = SeedSqlite(sqlite_store).join(conn)
        for name, expression_text, unit, content_hash in [
            ("asset_ratio", "balance.total_liabilities / balance.total_assets",
             "ratio", "ratio-hash"),
            ("pct_ind", "balance.capital_adequacy_ratio", "percent", "percent-hash"),
        ]:
            seed.dsl_expression(
                name, 1, expression_text, status="published",
                ast_json=json.dumps({"unit": unit}), content_hash=content_hash,
            )
    client = _make_client(duckdb_store, sqlite_store)

    body = client.get("/api/screening/indicators").json()

    by_name = {item["name"]: item for item in body["indicators"]}
    assert by_name["asset_ratio"]["dsl"] is True
    assert by_name["asset_ratio"]["unit"] == "ratio"
    assert by_name["pct_ind"]["unit"] == "percent"
    assert "不进行百分比换算" in body["unit_semantics"]["plain"]
    # 方案 C（2026-11-16）：排名不再作为扁平字段下发，改为条件级 `rank_mode`。
    # 任何 `*_rank` / `*_percentile` 派生字段都不得再出现在指标列表里
    # （155 个可排名指标 × 6 = 930 条会把主指标淹没）。
    rank_suffixes = (
        "_market_rank", "_market_percentile",
        "_industry_rank", "_industry_percentile",
        "_sw1_rank", "_sw1_percentile", "_sw2_rank", "_sw2_percentile",
    )
    leaked = [name for name in by_name if name.endswith(rank_suffixes)]
    assert not leaked, f"仍在下发排名派生字段：{leaked[:5]}"
    # 能力本身不能少：6 个口径必须通过 rank_modes 下发
    modes = {item["value"] for item in body["rank_modes"]}
    assert modes == {
        "market_rank", "market_percentile",
        "industry_rank", "industry_percentile",
        "sw2_rank", "sw2_percentile",
    }


# ─── 技术覆盖阈值单一真源（2026-11-17 独立审计问题 2）─────────────────────


def test_technical_coverage_threshold_is_single_source_for_both_consumers(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch,
) -> None:
    """把 config 的 technical_coverage_pct 改成 98：

    体检 `TECHNICAL_COVERAGE_GAP` 与 `/api/screening/indicators` 门禁必须同时
    读到 98。旧实现里 screening.py 硬编码 95.0，96% 覆盖率的字段仍可选中。
    """
    from app.core.config import Config
    from app.core.quality import asset_health
    from app.web.api import screening as screening_api

    monkeypatch.setattr(
        Config,
        "_instance",
        Config({"asset_health": {"technical_coverage_pct": 98.0}}),
    )
    assert asset_health.technical_coverage_threshold() == 98.0
    # 本地硬编码必须彻底删除，字段集必须是同一对象/同一元组（R1 单一真源）。
    assert not hasattr(screening_api, "_TECHNICAL_MIN_COVERAGE_PCT")
    assert not hasattr(screening_api, "_TECHNICAL_COVERAGE_FIELDS")
    assert screening_api.TECHNICAL_COVERAGE_FIELDS == asset_health.TECHNICAL_COVERAGE_FIELDS

    # 100 行快照，技术三列各 96 行有值 = 96%：默认 95 下可用，98 下必须隐藏。
    rows = [
        (f"{index:06d}", 0.1 if index < 96 else None,
         0.2 if index < 96 else None, -0.3 if index < 96 else None)
        for index in range(100)
    ]
    with duckdb_store.transaction() as connection:
        # `calculated_at` 原 SQL 写的是 `CURRENT_TIMESTAMP`：按迁移守则显式传哨兵，
        # 免得"刚算出来的"被静默写成工厂的固定过去时间。`join(conn)` 让提交权
        # 仍归调用方（工厂不在别人的事务里 begin/commit）。
        Seed(duckdb_store).join(connection).snapshots([
            {
                "stock_code": code,
                "report_date": "2026-09-30",
                "calculated_at": CURRENT_TIMESTAMP,
                "period_return": period_return,
                "annualized_volatility": annualized_volatility,
                "max_drawdown": max_drawdown,
            }
            for code, period_return, annualized_volatility, max_drawdown in rows
        ])

    client = _make_client(duckdb_store, sqlite_store)
    body = client.get("/api/screening/indicators").json()
    unavailable = {item["name"]: item for item in body["unavailable"]}
    for field in asset_health.TECHNICAL_COVERAGE_FIELDS:
        assert field in unavailable, f"{field} 在 98% 门槛下覆盖率 96% 必须隐藏"
        assert unavailable[field]["coverage_pct"] == 96.0
        assert "98%" in unavailable[field]["message"]

    payload = asset_health.run_asset_health(
        duckdb_store, sqlite_store, allow_expensive=False,
    )
    findings = [
        finding for finding in payload["findings"]
        if finding["code"] == "TECHNICAL_COVERAGE_GAP"
    ]
    assert {finding["field"] for finding in findings} == set(
        asset_health.TECHNICAL_COVERAGE_FIELDS
    )
    assert all(finding["threshold"] == 98.0 for finding in findings)


# ─── 同比双口径读路径（2026-11-17 独立审计问题 3）──────────────────────────


def test_yoy_fy_fields_share_pct_unit_with_ttm_yoy() -> None:
    from app.core.screening.field_units import field_unit

    for ttm, fy in (
        ("revenue_yoy", "revenue_yoy_fy"),
        ("net_profit_yoy", "net_profit_yoy_fy"),
        ("deducted_profit_yoy", "deducted_profit_yoy_fy"),
    ):
        assert field_unit(ttm) == "pct"
        assert field_unit(fy) == "pct", f"{fy} 与 {ttm} 同为小数比例，单位必须一致"


def test_stock_detail_indicators_expose_annual_yoy_caliber(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch,
) -> None:
    """`/api/stock/{code}/indicators` 必须同时下发 TTM 与年报两个同比口径。"""
    import app.core.data_quality as data_quality_module

    monkeypatch.setattr(data_quality_module, "read_warning_codes", lambda *a, **k: [])
    # `calculated_at` 原 SQL 是 `CURRENT_TIMESTAMP` → 显式传哨兵（迁移守则）。
    Seed(duckdb_store).snapshot(
        "600519",
        "2025-12-31",
        calculated_at=CURRENT_TIMESTAMP,
        revenue_yoy=0.25,
        revenue_yoy_fy=-0.5,
        net_profit_yoy=0.10,
        net_profit_yoy_fy=-0.40,
        deducted_profit_yoy=0.05,
        deducted_profit_yoy_fy=-0.30,
    )
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(stock_detail_router)
    client = TestClient(app)

    response = client.get("/api/stock/600519/indicators")

    assert response.status_code == 200
    growth = response.json()["indicators"]["growth"]
    assert growth["revenue_yoy"]["value"] == 0.25
    assert growth["revenue_yoy_fy"]["value"] == -0.5
    assert growth["net_profit_yoy"]["value"] == 0.10
    assert growth["net_profit_yoy_fy"]["value"] == -0.40
    assert growth["deducted_profit_yoy"]["value"] == 0.05
    assert growth["deducted_profit_yoy_fy"]["value"] == -0.30
