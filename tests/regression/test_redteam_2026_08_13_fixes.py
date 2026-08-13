"""reports/76 红队修复回归：更新窗口降级、保存期字段校验、staging 清理、API 404。

覆盖：
- P1-1 read_connection 对瞬时 connect 失败的重试（不再 5×0.5s 固定后 500）
- P1-2 read_warning_codes 写锁活跃时跳过全量重建（stale 缓存/空集）
- P1-3 treasury-comparison 批量语义与旧逐日 SQL 等价（窗口/公告日过滤）
- P1-4 写锁活跃时筛选门禁返回 409 auto_update_in_progress
- P3-2 research_statistics_staging_* 残留清理
- P3-3 未知 /api/* 返回 404 JSON（不落入 SPA 兜底）
- P3-4 规则保存期字段名校验
- P3-6 watchlist remove 无匹配行返回 404
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore
from app.web import main as web_main


# ─── P1-1: read_connection 重试 ─────────────────────────────────────────

def test_read_connection_retries_transient_connect_failures(
    database_paths: DatabasePathSet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import duckdb

    duck = DuckDBStore(paths=database_paths)
    real_connect = duckdb.connect
    attempts = {"n": 0}

    class FakeConn:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    def flaky_connect(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise duckdb.IOException("file already open (transient)")
        return FakeConn()

    monkeypatch.setattr(duckdb, "connect", flaky_connect)
    with duck.read_connection() as conn:
        assert isinstance(conn, FakeConn)
    assert attempts["n"] == 4
    monkeypatch.setattr(duckdb, "connect", real_connect)


# ─── P1-2: read_warning_codes 写锁感知 ──────────────────────────────────

def test_read_warning_codes_skips_rebuild_while_update_lock_held(
    database_paths: DatabasePathSet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import data_quality
    from app.core.storage.update_lock import exclusive_update

    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)

    built = {"count": 0}

    def exploding_build(*args, **kwargs):
        built["count"] += 1
        raise AssertionError("full build must not run while the update lock is held")

    monkeypatch.setattr(data_quality, "build_data_quality_status", exploding_build)
    with exclusive_update(duck.db_path):
        codes = data_quality.read_warning_codes(duck, sqlite)
    assert codes == []
    assert built["count"] == 0


def test_read_warning_codes_serves_cached_result_while_update_lock_held(
    database_paths: DatabasePathSet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import data_quality
    from app.core.storage.update_lock import exclusive_update

    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)

    data_quality._warning_codes_cache.clear()
    monkeypatch.setattr(
        data_quality, "build_data_quality_status",
        lambda duck, sqlite: {"warning_codes": ["DIVIDEND_DATES_UNVERIFIED"]},
    )
    # 空闲时首次构建并缓存
    assert data_quality.read_warning_codes(duck, sqlite) == ["DIVIDEND_DATES_UNVERIFIED"]

    built = {"count": 0}

    def exploding_build(*args, **kwargs):
        built["count"] += 1
        raise AssertionError("full build must not run while the update lock is held")

    monkeypatch.setattr(data_quality, "build_data_quality_status", exploding_build)
    with exclusive_update(duck.db_path):
        codes = data_quality.read_warning_codes(duck, sqlite)
    assert codes == ["DIVIDEND_DATES_UNVERIFIED"]
    assert built["count"] == 0
    data_quality._warning_codes_cache.clear()


def test_read_warning_codes_expired_cache_returns_stale_and_rebuilds_in_background(
    database_paths: DatabasePathSet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reports/76 P1-2 增强：TTL 过期不阻塞请求——返回旧值并后台单飞重建。"""
    from app.core import data_quality

    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)

    data_quality._warning_codes_cache.clear()
    data_quality._warning_codes_refreshing.clear()

    calls = {"n": 0}

    def slow_build(*args, **kwargs):
        calls["n"] += 1
        return {"warning_codes": [f"CODE_{calls['n']}"]}

    monkeypatch.setattr(data_quality, "build_data_quality_status", slow_build)
    data_quality._warning_codes_cache[
        f"{duck.db_path}|{sqlite.db_path}"
    ] = (time.monotonic() - 1000, ["OLD_CODE"])

    codes = data_quality.read_warning_codes(duck, sqlite)
    # 立即返回旧缓存（不等待后台重建）
    assert codes == ["OLD_CODE"]
    deadline = time.monotonic() + 5.0
    while calls["n"] == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls["n"] == 1
    data_quality._warning_codes_cache.clear()
    data_quality._warning_codes_refreshing.clear()


# ─── P1-4: 写锁活跃时筛选门禁 409 ───────────────────────────────────────

def _app_with_schema(database_paths: DatabasePathSet):
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=duck,
        sqlite=sqlite,
    )
    return app, duck


def test_screening_gate_returns_409_during_update_lock(
    database_paths: DatabasePathSet,
) -> None:
    from app.core.storage.update_lock import exclusive_update

    app, _duck = _app_with_schema(database_paths)
    client = TestClient(app)
    token = client.get("/api/session").json()["write_token"]
    headers = {"X-VD-Write-Token": token}

    with exclusive_update(app.state.duck.db_path):
        response = client.post(
            "/api/screening/run",
            headers=headers,
            json={"rule_id": 1, "rule_version": 1, "include_st": False,
                  "include_suspended": False, "min_listing_years": 1,
                  "strict_only": False},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "auto_update_in_progress"


# ─── P1-3: treasury-comparison 批量语义等价性 ───────────────────────────

def test_treasury_comparison_batch_window_matches_legacy_semantics(
    database_paths: DatabasePathSet,
) -> None:
    from tests.conftest import insert_minimum_screenable_data

    app, duck = _app_with_schema(database_paths)
    code = "600519"
    duck.write_query(
        """INSERT INTO stock_meta
           (stock_code, name, exchange, listing_date, is_st, is_suspended, is_listed)
           VALUES (?, ?, 'SSE', '2020-01-01', false, false, true)""",
        [code, code],
    )
    insert_minimum_screenable_data(duck, code)
    for trade_date, close in [(date(2026, 8, 1), 10.0), (date(2026, 8, 5), 10.0)]:
        duck.write_query(
            "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            [code, trade_date, close],
        )
    # 分红 1：2025-08-01 除权（对 2026-08-01 在 1 年窗口内；对 2026-08-05 在窗口外）
    duck.write_query(
        "INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share) "
        "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
        [code, date(2025, 8, 1), date(2025, 7, 30), 1.0],
    )
    # 分红 2：2026-07-01 除权（两个价格日都在窗口内）
    duck.write_query(
        "INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share) "
        "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
        [code, date(2026, 7, 1), date(2026, 6, 30), 2.0],
    )
    duck.write_query(
        """INSERT INTO treasury_yield_curve
           (curve_date, tenor_years, yield_pct, source, fetch_time, raw_hash, confidence, batch_id)
           VALUES (?, 10.0, 1.5, 'czb_mof', ?, '0'*64, 'strict', 'b1')""",
        [date(2026, 7, 31), datetime.now(timezone.utc)],
    )

    client = TestClient(app)
    response = client.get(f"/api/stock/{code}/treasury-comparison", params={"tenor": 10.0})
    assert response.status_code == 200
    series = {item["price_date"]: item for item in response.json()["series"]}
    # 08-01：1.0 + 2.0 = 3.0 → 30%；08-05：仅 2.0 → 20%
    assert series["2026-08-01"]["ttm_div_yield"] == pytest.approx(30.0)
    assert series["2026-08-01"]["spread"] == pytest.approx(28.5)
    assert series["2026-08-05"]["ttm_div_yield"] == pytest.approx(20.0)
    assert series["2026-08-05"]["spread"] == pytest.approx(18.5)


# ─── P3-2: statistics staging 清理 ──────────────────────────────────────

def test_statistics_staging_tables_are_swept(
    database_paths: DatabasePathSet,
) -> None:
    from app.core.statistics import StatisticsBuilder

    app, duck = _app_with_schema(database_paths)
    duck.write_query(
        "CREATE TABLE research_statistics_staging_deadbeef AS "
        "SELECT * FROM research_statistics WHERE FALSE"
    )
    builder = StatisticsBuilder(duck=duck, sqlite=app.state.sqlite)
    builder._cleanup_staging_tables()
    rows = duck.read_query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name LIKE 'research_statistics_staging_%'"
    )
    assert rows == []


# ─── P3-3: 未知 /api/* 返回 404 JSON ────────────────────────────────────

def test_unknown_api_path_returns_json_404_not_spa_fallback(
    database_paths: DatabasePathSet,
) -> None:
    app, _duck = _app_with_schema(database_paths)
    response = TestClient(app).get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "API endpoint not found"
    assert "text/html" not in response.headers.get("content-type", "")


# ─── P3-4: 规则保存期字段校验 ───────────────────────────────────────────

def test_validate_rule_fields_rejects_unknown_fields() -> None:
    from app.core.screening.engine import validate_rule_fields

    valid = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": "<", "value": 15},
        ]},
        "sort": [{"field": "pe_ttm", "direction": "asc"}],
        "columns": ["pe_ttm", "pb_mrq", "stock_code"],
    }
    validate_rule_fields(valid, set())

    bad_condition = {
        "conditions": {"logic": "AND", "rules": [
            {"field": "nonsense_field", "op": "<", "value": 1},
        ]},
    }
    with pytest.raises(ValueError, match="未知筛选字段"):
        validate_rule_fields(bad_condition, set())

    bad_sort = {"sort": [{"field": "nope", "direction": "asc"}]}
    with pytest.raises(ValueError, match="未知排序字段"):
        validate_rule_fields(bad_sort, set())

    bad_column = {"columns": ["nope"]}
    with pytest.raises(ValueError, match="未知结果字段"):
        validate_rule_fields(bad_column, set())

    # 已发布 DSL 自定义字段应被接受
    dsl_rule = {"conditions": {"logic": "AND", "rules": [
        {"field": "my_factor", "op": ">", "value": 0},
    ]}}
    validate_rule_fields(dsl_rule, {"my_factor"})


def test_rule_save_rejects_unknown_field_at_save_time(
    database_paths: DatabasePathSet,
) -> None:
    app, _duck = _app_with_schema(database_paths)
    client = TestClient(app)
    token = client.get("/api/session").json()["write_token"]
    headers = {"X-VD-Write-Token": token}
    response = client.post(
        "/api/screening/rules/save",
        headers=headers,
        json={
            "name": "bad-field-rule",
            "rule_json": {
                "conditions": {"logic": "AND", "rules": [
                    {"field": "nonsense_field", "op": "<", "value": 1},
                ]},
                "sort": [], "columns": [],
            },
        },
    )
    assert response.status_code == 400
    assert "未知筛选字段" in response.json()["detail"]


# ─── P3-6: watchlist remove 无匹配 404 ──────────────────────────────────

def test_watchlist_remove_missing_entry_returns_404(
    database_paths: DatabasePathSet,
) -> None:
    app, _duck = _app_with_schema(database_paths)
    client = TestClient(app)
    token = client.get("/api/session").json()["write_token"]
    response = client.request(
        "DELETE", "/api/watchlist/remove",
        headers={"X-VD-Write-Token": token},
        json={"stock_code": "600519", "group_name": "default"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "watchlist entry not found"


def test_watchlist_remove_reports_deleted_rows(
    database_paths: DatabasePathSet,
) -> None:
    app, _duck = _app_with_schema(database_paths)
    client = TestClient(app)
    token = client.get("/api/session").json()["write_token"]
    headers = {"X-VD-Write-Token": token}
    app.state.sqlite.execute(
        "INSERT INTO watchlist (stock_code, group_name) VALUES (?, ?)",
        ["600519", "default"],
    )
    response = client.request(
        "DELETE", "/api/watchlist/remove",
        headers=headers,
        json={"stock_code": "600519", "group_name": "default"},
    )
    assert response.status_code == 200
    assert response.json()["rows"] == 1
