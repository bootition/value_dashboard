from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.data_quality import (
    PRICE_FRESHNESS_MAX_AGE_DAYS,
    _bind_named_params,
    _published_override_stale_snapshot_codes,
    minimum_data_readiness,
    screening_readiness,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed

#: 门禁用例的同一只"就绪股票"骨架：12 处用例逐字相同，收敛成一个函数。
#: `is_st` / `is_suspended` 必须是 false —— 门禁另有 ST/停牌分支，这里不能留 NULL。


def _ready_stock(duck: DuckDBStore) -> None:
    """`stock_meta` 一行：SZSE、2020-01-01 上市、非 ST、未停牌（种子工厂负责列口径）。"""
    Seed(duck).stock(
        "000001", name="ready", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )


def test_empty_database_is_not_screenable(duckdb_store: DuckDBStore) -> None:
    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["stock_count"] == 0


def test_readiness_requires_complete_history_fresh_prices_and_coherent_snapshot(
    duckdb_store: DuckDBStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)

    assert minimum_data_readiness(duckdb_store)["ready"] is True

    duckdb_store.write_query(
        "DELETE FROM price_daily_raw WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '3 years'"
    )
    assert minimum_data_readiness(
        duckdb_store, minimum_volume_observations=1,
    )["missing_counts"] == {
        # 只有 `raw_history` 命中 —— 这是 2026-11-21 位置参数错位修复后的
        # **原语义**，不是放宽：本调用显式把成交量下限设成 1
        # （`minimum_volume_observations=1`），782 根带量 bar 当然达标。
        # 错位期间该参数被忽略、实际按 `minimum_history_observations`(1300) 判，
        # 于是"3 年 raw 被删"顺带报出 `meaningful_volume` —— 那个多余项
        # 是参数错位的症状（绑到了 1300），不是新发现的口径，故删除。
        "raw_history": 1,
    }

    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_close = 11 WHERE stock_code = '000001'"
    )
    assert minimum_data_readiness(duckdb_store)["missing_counts"] == {"snapshot_price_coherence": 1}


def _shift_snapshot_price_date(duck: DuckDBStore, lag_days: int) -> None:
    """把快照价格日改到「该股自己最新 bar 日 - `lag_days`」，并保证那根 bar 存在、收盘一致。

    构造要点（2026-11-21 `reports/152` §B）：`has_coherent_snapshot` 拆成
    「证据」与「新鲜度」两件事。这里让证据恒成立（bar 存在 + 收盘逐字一致），
    `lag_days` 才是唯一变量 —— 否则一个周末日会让"bar 不存在"抢先命中，
    测不到新鲜度窗口本身。
    """
    last_bar = duck.read_query(
        "SELECT CAST(MAX(trade_date) AS DATE) AS d FROM price_daily_raw"
        " WHERE stock_code = '000001'"
    )[0]["d"]
    target = last_bar - timedelta(days=lag_days)
    duck.write_query(
        "INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)"
        " VALUES ('000001', ?, 10, 100) ON CONFLICT DO NOTHING",
        [target],
    )
    duck.write_query(
        "INSERT INTO price_daily_qfq (stock_code, trade_date, close, volume)"
        " VALUES ('000001', ?, 10, 100) ON CONFLICT DO NOTHING",
        [target],
    )
    duck.write_query(
        "UPDATE indicator_snapshot SET latest_price_date = ?, latest_close = 10"
        " WHERE stock_code = '000001'",
        [target],
    )


@pytest.mark.parametrize("lag_days", [1, 3, PRICE_FRESHNESS_MAX_AGE_DAYS])
def test_snapshot_price_date_inside_the_freshness_window_is_ready(
    duckdb_store: DuckDBStore, lag_days: int,
) -> None:
    """落后 1 / 3 / 7 天（= 恰好边界）且 bar 存在、收盘一致 → 放行。

    2026-11-21 事故回归：这条用例在修复前会**通过**（因为真实的错位让窗口
    变成 1300 天），所以它必须与下面那条"超过边界要拦"的用例**成对存在**
    —— 单看放行侧永远发现不了窗口被放大。
    """
    assert lag_days <= PRICE_FRESHNESS_MAX_AGE_DAYS
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    _shift_snapshot_price_date(duckdb_store, lag_days)

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is True
    assert "snapshot_price_coherence" not in readiness["missing_counts"]


@pytest.mark.parametrize(
    "lag_days",
    [PRICE_FRESHNESS_MAX_AGE_DAYS + 1, 30, 100, 1299, 1400],
)
def test_snapshot_price_date_beyond_the_freshness_window_blocks(
    duckdb_store: DuckDBStore, lag_days: int,
) -> None:
    """落后 8 / 30 / 100 / 1299 / 1400 天 → 必须拦（边界 = `> 7` 天）。

    修复前实测：8 / 30 / 100 / 1299 天**全部放行**，1400 天才拦 —— 因为
    `snapshot.latest_price_date >= raw.last_date - ?` 的第 6 个 `?` 绑到了
    `minimum_volume_observations`(1300)。同一个错位还让
    `minimum_volume_observations=1` 反而**收紧**了新鲜度窗口（见
    `test_freshness_window_does_not_follow_the_volume_threshold`）。
    """
    assert lag_days > PRICE_FRESHNESS_MAX_AGE_DAYS
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    _shift_snapshot_price_date(duckdb_store, lag_days)

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"] == {"snapshot_price_coherence": 1}


def test_freshness_window_does_not_follow_the_volume_threshold(
    duckdb_store: DuckDBStore,
) -> None:
    """交叉铁证：「落后 3 天」的快照，结果不得随 `minimum_volume_observations` 变化。

    修复前实测：默认参数放行，`minimum_volume_observations=1` 反被拦 ——
    两个语义无关的参数互相串了位，这类错误只有"改一个、另一个跟着动"才看得见。
    """
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    _shift_snapshot_price_date(duckdb_store, 3)

    assert minimum_data_readiness(duckdb_store)["ready"] is True
    assert minimum_data_readiness(
        duckdb_store, minimum_volume_observations=1,
    )["ready"] is True
    assert minimum_data_readiness(
        duckdb_store, minimum_history_observations=1,
    )["ready"] is True


def test_snapshot_price_date_must_be_a_bar_that_exists(
    duckdb_store: DuckDBStore,
) -> None:
    """快照声称的价格日在库里**没有** bar（周末/节假日/从未入库）→ 拦。"""
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    last_bar = duckdb_store.read_query(
        "SELECT CAST(MAX(trade_date) AS DATE) AS d FROM price_daily_raw"
        " WHERE stock_code = '000001'"
    )[0]["d"]
    missing_bar = next(
        d
        for d in (last_bar - timedelta(days=offset) for offset in range(1, 8))
        if not duckdb_store.read_query(
            "SELECT COUNT(*) AS n FROM price_daily_raw WHERE stock_code = '000001'"
            " AND trade_date = ?",
            [d],
        )[0]["n"]
    )
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_price_date = ? WHERE stock_code = '000001'",
        [missing_bar],
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_price_coherence"] == 1


def test_snapshot_price_date_whose_bar_was_deleted_blocks(
    duckdb_store: DuckDBStore,
) -> None:
    """bar 曾经存在、后来被删掉 → 同样拦（不能因为"那天的快照看起来新"就放行）。"""
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    last_bar = duckdb_store.read_query(
        "SELECT CAST(MAX(trade_date) AS DATE) AS d FROM price_daily_raw"
        " WHERE stock_code = '000001'"
    )[0]["d"]
    duckdb_store.write_query(
        "DELETE FROM price_daily_raw WHERE stock_code = '000001' AND trade_date = ?",
        [last_bar],
    )
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_price_date = ? WHERE stock_code = '000001'",
        [last_bar],
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_price_coherence"] == 1


def test_snapshot_close_within_one_percent_still_blocks(
    duckdb_store: DuckDBStore,
) -> None:
    """收盘价差 1% → 拦（容差是 1e-6，不是 1%。"差不多"不是证据）。"""
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_close = 10.1 WHERE stock_code = '000001'"
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"] == {"snapshot_price_coherence": 1}


def test_snapshot_without_a_price_date_blocks(duckdb_store: DuckDBStore) -> None:
    """快照价格日 NULL（= 快照从未记过价格日）→ 拦。"""
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_price_date = NULL WHERE stock_code = '000001'"
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_price_coherence"] == 1


def test_snapshot_for_a_stock_that_never_had_a_bar_blocks(
    duckdb_store: DuckDBStore,
) -> None:
    """该股从未有过任何 bar（快照对着空气）→ 拦，且 `raw_history` 同时命中。"""
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query("DELETE FROM price_daily_raw WHERE stock_code = '000001'")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_price_coherence"] == 1
    assert readiness["missing_counts"]["raw_history"] == 1


def test_raw_history_floor_stays_at_the_default_threshold(
    duckdb_store: DuckDBStore,
) -> None:
    """只留 7 根 raw bar → `raw_history` 必须报缺口（下限是 1300，不是 7）。

    2026-11-21 事故回归：位置参数错位让 `has_raw_history` 的 `LEAST(?, …)`
    绑到了 `PRICE_FRESHNESS_MAX_AGE_DAYS`(7)，于是只剩 3 年 782 根 bar 也
    不报缺口 —— 这条用例把它钉回 1300。
    """
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "DELETE FROM price_daily_raw WHERE stock_code = '000001' AND trade_date NOT IN ("
        "  SELECT trade_date FROM price_daily_raw WHERE stock_code = '000001'"
        "  ORDER BY trade_date LIMIT 7)"
    )
    # 快照价格日跟到剩下的最后一根 bar：让口径唯一落在"观测数不够"上。
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET latest_price_date = ("
        "  SELECT MAX(trade_date) FROM price_daily_raw WHERE stock_code = '000001')"
        " WHERE stock_code = '000001'"
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["raw_history"] == 1


def test_named_param_binding_refuses_positional_placeholders() -> None:
    """具名参数守卫①：SQL 里混入 `?` 直接抛错（位置参数错位的事故入口）。"""
    with pytest.raises(ValueError, match="位置参数"):
        _bind_named_params("SELECT ?::DATE AS today", {"today": "2026-01-01"}, context="t")


def test_named_param_binding_requires_an_exact_key_set() -> None:
    """具名参数守卫②：SQL 引用的 `$name` 与传入键必须**双向**相等。"""
    with pytest.raises(ValueError, match="不一致"):
        _bind_named_params("SELECT $a, $b", {"a": 1}, context="t")
    with pytest.raises(ValueError, match="不一致"):
        _bind_named_params("SELECT $a", {"a": 1, "b": 2}, context="t")
    assert _bind_named_params("SELECT $a", {"a": 1}, context="t") == {"a": 1}


@pytest.mark.parametrize(
    ("changes", "missing_key"),
    [
        (
            [
                "DELETE FROM price_daily_qfq WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '3 years'",
            ],
            "qfq_history",
        ),
        (
            [
                "DELETE FROM price_daily_raw WHERE stock_code = '000001' AND trade_date >= CURRENT_DATE - INTERVAL '8 days'",
                "DELETE FROM price_daily_qfq WHERE stock_code = '000001' AND trade_date >= CURRENT_DATE - INTERVAL '8 days'",
                # 留（第 6 类·库侧相对时间）：raw/qfq 两行都是「8 天前」的库侧相对日期
                "INSERT INTO price_daily_raw (stock_code, trade_date, close) VALUES ('000001', CURRENT_DATE - INTERVAL '8 days', 10)",
                # 留（第 6 类·库侧相对时间）：qfq 同一刻度；工厂不给库侧 CURRENT_DATE 口径
                "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES ('000001', CURRENT_DATE - INTERVAL '8 days', 10)",
                "UPDATE indicator_snapshot SET latest_price_date = CURRENT_DATE - INTERVAL '8 days' WHERE stock_code = '000001'",
                "UPDATE source_audit SET report_date = CURRENT_DATE - INTERVAL '8 days' "
                "WHERE stock_code = '000001' AND field_name = 'latest_close' "
                "AND fetch_batch_id IN (SELECT batch_id FROM fetch_batch "
                "WHERE data_type IN ('price_daily_raw', 'price_daily_qfq'))",
            ],
            "price_freshness",
        ),
        (
            ["DELETE FROM cash_flow WHERE stock_code = '000001' AND report_date = '2025-12-31'"],
            "financial_period",
        ),
    ],
)
def test_readiness_identifies_each_remaining_hard_requirement(
    duckdb_store: DuckDBStore,
    changes: list[str],
    missing_key: str,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    for statement in changes:
        duckdb_store.write_query(statement)

    readiness = minimum_data_readiness(duckdb_store)

    if missing_key == "price_freshness":
        # 价格陈旧属披露项（PRD §6.4 D7: 允许陈旧但显示日期）: 有数据但
        # 超过 7 天无新 bar 视为停牌豁免，不阻断 ready 也不计入缺口。
        assert readiness["ready"] is True
        assert "price_freshness" not in readiness["missing_counts"]
    else:
        assert readiness["ready"] is False
        assert readiness["missing_counts"][missing_key] == 1


def test_readiness_requires_sufficient_nonzero_volume(duckdb_store: DuckDBStore) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query("UPDATE price_daily_raw SET volume = 0 WHERE stock_code = '000001'")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"] == {"meaningful_volume": 1}


@pytest.mark.parametrize(
    ("statement", "missing_key"),
    [
        ("DELETE FROM xdxr WHERE stock_code = '000001'", "corporate_action_dividend_lineage"),
        ("UPDATE stock_meta SET total_shares = NULL WHERE stock_code = '000001'", "share_capital"),
    ],
)
def test_readiness_requires_company_actions_and_share_capital(
    duckdb_store: DuckDBStore, statement: str, missing_key: str,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(statement)

    assert minimum_data_readiness(duckdb_store)["missing_counts"][missing_key] == 1


def test_readiness_rejects_snapshot_older_than_financial_source_input(duckdb_store: DuckDBStore) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    # `fetch_time` 必须是**未来**（快照的 `calculated_at` 之后才算"新输入"）：
    # 工厂不替你编时刻，调用点逐字给；`repeat('a', 64)` 这个形状也同样写在调用点。
    Seed(duckdb_store).source_audit(
        "000001", "revenue",
        report_date="2025-12-31", value=100.0, source="test",
        fetch_batch_id="new-financial-source",
        fetch_time=datetime.now() + timedelta(seconds=1), raw_response_hash="a" * 64,
        confidence="strict",
    )

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_input_freshness"] == 1


def test_readiness_rejects_materialized_data_without_source_graph(duckdb_store: DuckDBStore) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query("DELETE FROM source_audit WHERE stock_code = '000001'")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["lineage_coverage"] == 1


def test_screening_requires_a_persisted_trading_calendar(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


def test_screening_rejects_a_trading_calendar_gap_for_a_qfq_series(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    assert screening_readiness(duckdb_store, sqlite_store)["ready"] is True

    gap = duckdb_store.read_query(
        """SELECT CAST(trade_date AS VARCHAR) AS trade_date FROM price_daily_qfq
           WHERE stock_code = '000001' AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           ORDER BY trade_date DESC LIMIT 1"""
    )[0]["trade_date"]
    sqlite_store.execute("DELETE FROM trading_dates WHERE trade_date = ?", [gap])

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


def test_screening_rejects_an_internal_qfq_gap_on_a_calendar_date(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # Remove a gap larger than the 2% suspension tolerance.
    # 60 natural days ≈ 43 trading days of ~1570 (≈2.7%).
    duckdb_store.write_query(
        """DELETE FROM price_daily_qfq WHERE stock_code = '000001'
           AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           AND trade_date >= CURRENT_DATE - INTERVAL '1 year' - INTERVAL '60 days'"""
    )

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is False
    assert decision["warning_codes"] == ["TRADING_CALENDAR_UNAVAILABLE"]


def test_screening_tolerates_a_small_calendar_gap_as_suspension(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    # A single missing bar is within the 2% suspension tolerance.
    duckdb_store.write_query(
        """DELETE FROM price_daily_qfq WHERE stock_code = '000001'
           AND trade_date < CURRENT_DATE - INTERVAL '1 year'
           AND trade_date >= CURRENT_DATE - INTERVAL '1 year' - INTERVAL '2 days'"""
    )

    decision = screening_readiness(duckdb_store, sqlite_store)

    assert decision["ready"] is True


def test_published_override_after_snapshot_blocks_readiness(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    duckdb_store.write_query(
        "UPDATE indicator_snapshot SET calculated_at = TIMESTAMP '2000-01-01' WHERE stock_code = '000001'"
    )
    # 留（第 6 类·库侧相对时间）：`created_at = datetime('now','+1 hour')`——覆盖值必须
    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status, created_at)
           VALUES ('000001', 'total_assets', '2025-12-31', 101, 'correction', 'published',
                   datetime('now', '+1 hour'))"""
    )

    assert sqlite_store.query(
        "SELECT stock_code, created_at FROM manual_overrides WHERE status = 'published'"
    )[0]["stock_code"] == "000001"
    assert _published_override_stale_snapshot_codes(duckdb_store, sqlite_store) == ["000001"]
    readiness = minimum_data_readiness(duckdb_store, sqlite_store)

    assert readiness["ready"] is False
    assert readiness["missing_counts"]["snapshot_input_freshness"] == 1


def test_readiness_blocks_durable_actions_when_current_data_changes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _ready_stock(duckdb_store)
    insert_minimum_screenable_data(duckdb_store)
    assert minimum_data_readiness(duckdb_store)["ready"] is True

    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = {"ready": True}
    app.include_router(router)
    duckdb_store.write_query("UPDATE indicator_snapshot SET latest_close = 11 WHERE stock_code = '000001'")

    response = TestClient(app).post("/api/screening/save", json={"title": "blocked", "run_id": "missing"})

    assert response.status_code == 409
    assert response.json()["detail"]["readiness"]["missing_counts"] == {"snapshot_price_coherence": 1}


def test_readiness_blocks_an_incompatible_live_schema(duckdb_store: DuckDBStore) -> None:
    duckdb_store.execute_script("DROP TABLE raw_response_archive")

    readiness = minimum_data_readiness(duckdb_store)

    assert readiness["ready"] is False
    assert "raw_response_archive.raw_response_hash" in readiness["schema_compatibility"]["missing"]


def test_screening_rejects_a_known_incomplete_startup_state(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = {"ready": False, "missing_counts": {"raw_history": 1}}
    app.include_router(router)

    response = TestClient(app).post(
        "/api/screening/run", json={"rule_id": 1, "rule_version": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "minimum_data_not_ready"


def test_screening_durable_actions_revalidate_current_readiness(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = {"ready": True}
    app.include_router(router)
    client = TestClient(app)

    for path, payload in (
        ("/api/screening/save", {"title": "blocked", "run_id": "missing"}),
        ("/api/screening/export_csv", {"result_id": 1}),
        ("/api/screening/add_to_watchlist", {"stock_codes": ["000001"], "result_id": 1}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["reason_code"] == "minimum_data_not_ready"
