"""2026-11-13 生产轮暴露的 P0/P1/P2 回归：`/api/ready` 三道闸、股本阈值政策、CN 时区。

事故形态（Lead 生产实测，应用正跑自动更新、持 DuckDB 写锁的同一秒）：

    GET /api/ready                   → 200 {"ready":true, checked_at:13 小时前}   ← 全绿
    GET /api/health                  → 503 Cannot open file ... already open in PID 109704
    GET /api/stock/600519/indicators → 503 {"reason_code":"duckdb_read_locked"}

2026-11-12 加 `/api/ready` 的初衷就是消灭"health 一直 200 而筛选全挂"，
结果换成"ready 一直 200 而筛选全挂"——同一类缺陷换了入口。现在三道闸：

1. **可用性**：真读一次 ``SELECT 1``；失败 → 503 ``database_unavailable``；
2. **实时遮蔽态**：``indicator_trust(warning_codes)["untrusted_all"]`` 为真 →
   503 ``snapshot_values_masked``（库能读但快照值全被 mask 的事故形态）；
3. **陈旧度只披露不翻状态**：``checked_at_age_seconds`` / ``stale`` 仅作观测。

外加两条配套修复：
* readiness 的 share_capital 改用与发布门禁同一阈值政策（
  ``gate_hard_block_threshold``），避免"1 只缺口 → /api/ready 503"的假红；
* ``data_status.summary`` 的 ``price_age_days`` 统一到 CN_TZ（北京 00:00~08:00
  用 UTC 会少算 1 天）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.data_quality as data_quality
import app.web.main as web_main
from app.core.config import Config
from app.core.data_quality import (
    READINESS_CACHE_KEY,
    gate_hard_block_threshold,
    minimum_data_readiness,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api import data_status as data_status_module
from tests.conftest import insert_minimum_screenable_data

_READY_PAYLOAD = {
    "ready": True,
    "stock_count": 5_580,
    "missing": {},
    "missing_counts": {},
}


def _build_app(
    database_paths: DatabasePathSet,
    duck: DuckDBStore | None = None,
    sqlite: SQLiteStore | None = None,
) -> tuple[TestClient, FastAPI]:
    """Build the app on schema-initialized stores.

    `DuckDBStore` 只做懒连接；未初始化 schema 时文件可能还不存在，
    `read_query("SELECT 1")` 会以 "database file not found" 抛错——那会让
    "可用性闸"的用例测到错误的失败原因。这里允许注入 conftest 已初始化的
    store，未注入时就地初始化一次。
    """
    from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema

    if duck is None:
        duck = DuckDBStore(paths=database_paths)
        init_duckdb_schema(duck)
    if sqlite is None:
        sqlite = SQLiteStore(paths=database_paths)
        init_sqlite_schema(sqlite)
    app = web_main.create_app(
        paths=database_paths,
        config=Config({}, paths=database_paths),
        duck=duck,
        sqlite=sqlite,
    )
    return TestClient(app), app


def _store_readiness(sqlite: SQLiteStore, payload: dict | None = None) -> None:
    from app.core.data_quality import store_cached_data_readiness

    store_cached_data_readiness(sqlite, dict(payload or _READY_PAYLOAD))


# ─── 闸 1：可用性 ────────────────────────────────────────────────────


def test_ready_returns_503_when_the_database_cannot_be_read(
    database_paths: DatabasePathSet, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库读不出来（更新持写锁 / 文件被独占）→ 503 database_unavailable。

    这正是事故现场：缓存里 readiness 全绿，但此刻库根本读不了。
    """
    client, app = _build_app(database_paths)
    _store_readiness(app.state.sqlite)
    monkeypatch.setattr(
        data_quality, "read_warning_codes", lambda duck, sqlite: [],
    )

    def exploding_read_query(self, sql, params=None):  # noqa: ANN001, ARG001
        raise RuntimeError("Cannot open file 'valuedashboard.duckdb': already open in PID 109704")

    monkeypatch.setattr(DuckDBStore, "read_query", exploding_read_query)

    response = client.get("/api/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == "database_unavailable"
    assert body["db_available"] is False
    assert body["ready"] is False
    assert "already open in PID 109704" in body["error"]


# ─── 闸 2：实时遮蔽态 ────────────────────────────────────────────────


@pytest.mark.parametrize("warning_code", ["SNAPSHOT_STALE", "LINEAGE_INVALID"])
def test_ready_returns_503_when_snapshot_values_are_masked_even_if_cache_is_green(
    database_paths: DatabasePathSet, monkeypatch: pytest.MonkeyPatch, warning_code: str,
) -> None:
    """缓存 ready=True + 库能读，但快照值被 mask → 必须 503 snapshot_values_masked。"""
    client, app = _build_app(database_paths)
    _store_readiness(app.state.sqlite)
    monkeypatch.setattr(
        data_quality, "read_warning_codes", lambda duck, sqlite: [warning_code],
    )

    response = client.get("/api/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == "snapshot_values_masked"
    assert body["untrusted_all"] is True
    assert body["db_available"] is True
    assert body["ready"] is False
    assert warning_code in body["warning_codes"]


# ─── 闸 3：陈旧度只披露不翻状态 ──────────────────────────────────────


def test_ready_returns_200_and_discloses_freshness_fields(
    database_paths: DatabasePathSet, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库可读 + 无遮蔽 + 缓存 ready → 200，并披露 checked_at_age_seconds/stale。"""
    client, app = _build_app(database_paths)
    _store_readiness(app.state.sqlite)
    monkeypatch.setattr(data_quality, "read_warning_codes", lambda duck, sqlite: [])

    response = client.get("/api/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["reason"] is None
    assert body["db_available"] is True
    assert body["untrusted_all"] is False
    assert isinstance(body["checked_at_age_seconds"], int)
    assert body["checked_at_age_seconds"] < web_main.READINESS_STALE_AFTER_SECONDS
    assert body["stale"] is False
    # 契约 §6 要求 body 含这四个键
    assert {"ready", "checked_at", "warning_codes", "missing_counts"} <= set(body)


def test_stale_readiness_cache_is_disclosed_but_still_answers_200(
    database_paths: DatabasePathSet, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 小时前的 readiness 缓存 → stale=True，但**不得**因此翻成 503（假红）。"""
    client, app = _build_app(database_paths)
    sqlite = app.state.sqlite
    stale_at = (datetime.now().astimezone() - timedelta(hours=3)).isoformat()
    sqlite.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        [READINESS_CACHE_KEY, json.dumps(_READY_PAYLOAD), stale_at],
    )
    monkeypatch.setattr(data_quality, "read_warning_codes", lambda duck, sqlite: [])

    response = client.get("/api/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["stale"] is True
    assert body["checked_at_age_seconds"] > web_main.READINESS_STALE_AFTER_SECONDS


def test_ready_returns_503_when_no_readiness_evidence_exists(
    database_paths: DatabasePathSet, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既无缓存也无启动快照 → fail closed（ready 结论不可凭空为真）。"""
    client, _app = _build_app(database_paths)
    monkeypatch.setattr(data_quality, "read_warning_codes", lambda duck, sqlite: [])

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["reason"] == "readiness_unavailable"


# ─── P1：readiness 与发布门禁同一股本阈值政策 ────────────────────────


def test_readiness_does_not_block_on_share_capital_gaps_below_the_threshold(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """缺口占比 < 1/3（=发布侧逐股隔离）→ ready 仍为 True，但计数如实披露。

    2026-11-13 起门禁/readiness 共用「绝对阈值 OR ≥1/3 占比」判据，所以这里
    必须造一个占比低于 1/3 的库（4 只库里 1 只违规 = 25%）。
    """
    for code in ("000001", "000002", "000003", "000004"):
        duckdb_store.write_query(
            """INSERT INTO stock_meta
                   (stock_code, name, exchange, listing_date, is_st, is_suspended)
               VALUES (?, ?, 'SZSE', '2020-01-01', FALSE, FALSE)""",
            [code, code],
        )
        insert_minimum_screenable_data(duckdb_store, code)
    # 制造 1 只股本缺口（circ > total）。小库阈值 = max(50, ...) = 50。
    duckdb_store.write_query(
        "UPDATE stock_meta SET circ_shares = 200 WHERE stock_code = '000001'"
    )

    readiness = minimum_data_readiness(duckdb_store, sqlite_store)

    assert readiness["share_capital_policy"] == {
        "gap_count": 1,
        "threshold": gate_hard_block_threshold(readiness["stock_count"]),
        "blocking": False,
        "quarantined": True,
    }
    assert readiness["share_capital_policy"]["threshold"] == 50
    # 计数必须保留（如实披露），只是不再据此阻断。
    assert readiness["missing_counts"]["share_capital"] == 1
    assert readiness["missing"]["share_capital"] == ["000001"]
    assert readiness["ready"] is True


def test_readiness_blocks_when_share_capital_gaps_exceed_the_threshold(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """> 阈值（发布侧会整批拒绝）→ readiness 也必须 ready=False。"""
    # 硬阻断下限做成可注入常量（契约 §3 设计意图），小库用 1 复现同一政策。
    monkeypatch.setattr(data_quality, "GATE_HARD_BLOCK_MIN", 1)
    for code in ("000001", "000002"):
        duckdb_store.write_query(
            """INSERT INTO stock_meta
                   (stock_code, name, exchange, listing_date, is_st, is_suspended)
               VALUES (?, ?, 'SZSE', '2020-01-01', FALSE, FALSE)""",
            [code, code],
        )
        insert_minimum_screenable_data(duckdb_store, code)
    duckdb_store.write_query(
        "UPDATE stock_meta SET circ_shares = 200 WHERE stock_code IN ('000001','000002')"
    )

    readiness = minimum_data_readiness(duckdb_store, sqlite_store)

    assert readiness["share_capital_policy"]["threshold"] == 1
    assert readiness["share_capital_policy"]["gap_count"] == 2
    assert readiness["share_capital_policy"]["blocking"] is True
    assert readiness["share_capital_policy"]["quarantined"] is False
    assert readiness["ready"] is False


@pytest.mark.parametrize(("total", "broken"), [(10, 2), (5, 5)])
def test_readiness_keeps_share_capital_gate_aligned_with_publish_gate(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    total: int, broken: int,
) -> None:
    """同一状态两个结论必须消失：readiness 阻断 ⇔ 发布门禁 hard_block。

    两边都只经由 `share_capital_hard_block`（绝对阈值 OR ≥1/3 占比）判定，
    因此同一 fixture 下两个布尔值必须一致：10 只库 2 只违规（20%）→ 都不阻断；
    5 只库 5 只违规（100%）→ 都阻断（占比判据）。
    """
    from app.core.data_quality import snapshot_publish_gate

    codes = [f"6000{index}" for index in range(total)]
    with duckdb_store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO stock_meta (stock_code, name, exchange, listing_date)
               VALUES (?, ?, 'SSE', '2020-01-01')""",
            [(code, code) for code in codes],
        )
        # 先给**所有**股票合法股本（否则 NULL 股本在 90 天豁免外同样是违规，
        # 会让 gap_count 从 broken 变成 total，测例失真）。
        connection.execute("UPDATE stock_meta SET total_shares = 100, circ_shares = 80")
        connection.execute(
            "UPDATE stock_meta SET circ_shares = 200 "
            f"WHERE stock_code IN ({', '.join('?' for _ in range(broken))})",
            codes[:broken],
        )

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)
    readiness = minimum_data_readiness(duckdb_store, sqlite_store)

    assert gate["threshold"] == readiness["share_capital_policy"]["threshold"]
    assert gate["hard_block"] is readiness["share_capital_policy"]["blocking"]
    assert gate["violation_count"] == readiness["share_capital_policy"]["gap_count"] == broken
    assert gate["hard_block"] is (broken * 3 >= total)


# ─── P2：price_age_days 必须是北京时区 ───────────────────────────────


def test_price_age_days_uses_china_timezone_at_beijing_midnight() -> None:
    """北京 00:30（UTC 仍是前一天 16:30）→ price_age_days 必须 +1，不能少算。"""
    from app.core.financial_period import CN_TZ

    class _FakeDuck:
        def read_query(self, sql, params=None):  # noqa: ANN001, ARG002
            return [{
                "price_latest_date": date(2026, 11, 12),
                "snapshot_latest_price_date": date(2026, 11, 5),
            }]

    frozen = datetime(2026, 11, 13, 0, 30, tzinfo=CN_TZ)

    ages = data_status_module._freshness_ages(_FakeDuck(), now=frozen)

    assert ages["price_age_days"] == 1, "北京 00:30 应已跨天；UTC 口径会少算成 0"
    assert ages["data_age_days"] == 7
    assert ages["price_latest_date"] == "2026-11-12"


def test_price_age_days_is_zero_on_the_same_beijing_day() -> None:
    from app.core.financial_period import CN_TZ

    class _FakeDuck:
        def read_query(self, sql, params=None):  # noqa: ANN001, ARG002
            return [{
                "price_latest_date": date(2026, 11, 12),
                "snapshot_latest_price_date": None,
            }]

    ages = data_status_module._freshness_ages(
        _FakeDuck(), now=datetime(2026, 11, 12, 20, 0, tzinfo=CN_TZ),
    )

    assert ages["price_age_days"] == 0
    assert ages["data_age_days"] is None
    assert ages["snapshot_latest_price_date"] is None


def test_freshness_ages_handles_an_empty_database() -> None:
    class _EmptyDuck:
        def read_query(self, sql, params=None):  # noqa: ANN001, ARG002
            return [{"price_latest_date": None, "snapshot_latest_price_date": None}]

    ages = data_status_module._freshness_ages(_EmptyDuck())

    assert ages == {
        "price_latest_date": None,
        "snapshot_latest_price_date": None,
        "price_age_days": None,
        "data_age_days": None,
    }
