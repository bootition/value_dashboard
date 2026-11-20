"""指数看板（多指数 ERP）只读域与 API 回归（2026-09-05）

覆盖：
- ERP 公式：1/PE*100 − 10Y 国债收益率（百分点口径）
- 分位带：p20/p50/p80 与当前分位；样本不足/无数据语义
- 同日期多源去重主源优先（宽基 legulegu、行业 sws）
- API：/api/index/catalog、/overview、/erp-compare、/{code}/detail、
  /{code}/erp、/{code}/valuation 与 404 守卫
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.index_dashboard import (
    _as_date,
    compute_erp_series,
    erp_compare,
    erp_detail,
    index_catalog,
    index_detail,
    index_summaries_batch,
    index_summary,
    valuation_detail,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.index_dashboard import router as index_router
from tests.support.seed import Seed


def _seed_index_valuation(duck: DuckDBStore) -> None:
    """000300：乐咕月度 40 点；SW801010：申万日度 40 点（同窗口无国债的点用于测试跳过）。"""
    # 原来是逐点 `INSERT ... CURRENT_TIMESTAMP`（80 次写锁）→ 每源一次批量事务；
    # `trace_now=True` 保住"刚抓的"，`pb` 逐行不同照旧在调用点（`raw_hash` 交给
    # 工厂的确定性 64 位 sha256，原来是 `h00`/`s00` 这种短占位值）。
    seed = Seed(duck, trace_now=True)
    base = date(2026, 1, 31)
    seed.index_valuation(
        "000300",
        [
            (
                base - timedelta(days=30 * i),
                10.0 + (i % 10) * 0.5,  # 10.0 ~ 14.5
                {"pb": 1.0 + i * 0.01},
            )
            for i in range(40)
        ],
        source="legulegu",
        pe_metric="ttm",
        confidence="approximate",
    )
    seed.index_valuation(
        "SW801010",
        [
            (
                date(2026, 8, 31) - timedelta(days=i),
                20.0 + (i % 10) * 0.4,
                {"pb": 2.0 + i * 0.01},
            )
            for i in range(40)
        ],
        source="sws",
        pe_metric="sws_daily",
        div_yield=1.5,
        confidence="approximate",
    )


def _seed_treasury(duck: DuckDBStore) -> None:
    # 50 个曲线日（主键 `(curve_date, tenor_years)`）："刚抓的"用 `trace_now=True`
    # 打开，`raw_hash` 逐行不同照旧写在调用点。
    seed = Seed(duck, trace_now=True)
    base = date(2026, 9, 1)
    for i in range(50):
        d = base - timedelta(days=30 * i)
        seed.treasury_yield_curve(d, 10.0, 1.72, raw_hash=f"t{i:02d}", batch_id="b")


def test_erp_formula_matches_article_caliber() -> None:
    points = compute_erp_series(
        [{"trade_date": "2026-07-31", "pe_ttm": 14.6, "source": "legulegu"}],
        [{"curve_date": "2026-07-31", "yield_pct": 1.72}],
    )
    assert points[0]["erp"] == pytest.approx((1 / 14.6 * 100) - 1.72, abs=1e-9)
    assert points[0]["erp"] == pytest.approx(5.129, abs=1e-2)


def test_as_date_normalizes_pandas_timestamp() -> None:
    """DuckDB Python 驱动返回 pandas.Timestamp（datetime 子类），
    直接比较 datetime.date 会 TypeError（正式库实锤），必须归一为纯 date。"""
    import pandas as pd

    value = _as_date(pd.Timestamp("2026-09-03"))
    assert value == date(2026, 9, 3)
    assert _as_date("2026-09-03") == date(2026, 9, 3)
    assert _as_date(None) is None


def test_erp_skips_missing_treasury_and_nonpositive_pe() -> None:
    points = compute_erp_series(
        [
            {"trade_date": "2026-01-01", "pe_ttm": 14.0, "source": "legulegu"},  # 无国债 → 跳过
            {"trade_date": "2026-02-01", "pe_ttm": 0.0, "source": "legulegu"},  # 非正 PE → 跳过
            {"trade_date": "2026-03-01", "pe_ttm": 10.0, "source": "legulegu"},
        ],
        [{"curve_date": "2026-03-01", "yield_pct": 2.0}],
    )
    assert len(points) == 1
    assert points[0]["erp"] == pytest.approx(8.0)


def test_overview_and_erp_detail(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)

    overview = erp_compare(duckdb_store)
    assert len(overview["items"]) == 44  # 全A + 12 宽基 + 31 申万一级
    by_code = {item["code"]: item for item in overview["items"]}
    hs300 = by_code["000300"]
    assert hs300["status"] == "ok"
    assert hs300["category"] == "broad"
    assert hs300["backtest_validated"] is True
    assert hs300["pe"] is not None
    assert hs300["erp"] is not None
    assert hs300["erp_percentile"] is not None
    assert hs300["erp_bands"]["p20"] <= hs300["erp_bands"]["p50"] <= hs300["erp_bands"]["p80"]

    sw = by_code["SW801010"]
    assert sw["category"] == "industry"
    assert sw["backtest_validated"] is False
    assert sw["pe_metric"] == "sws_daily"

    all_a = by_code["ALL_A"]
    assert all_a["category"] == "broad"
    assert all_a["status"] in {"ok", "partial"}

    detail = erp_detail(duckdb_store, "000300")
    assert len(detail["series"]) > 0
    assert detail["erp_bands"]["p50"] is not None
    assert "宽基" in detail["disclaimer"]

    industry_detail = erp_detail(duckdb_store, "SW801010")
    assert "暂无回测验证" in industry_detail["disclaimer"]

    valuation = valuation_detail(duckdb_store, "000300")
    assert len(valuation["pe_series"]) == 40
    assert valuation["pe_bands"]["p20"] is not None


def test_batch_summary_and_combined_detail_match_single_calls(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)

    batch = index_summaries_batch(duckdb_store, ["000300", "SW801010"])
    assert batch["000300"] == index_summary(duckdb_store, "000300")
    assert batch["SW801010"] == index_summary(duckdb_store, "SW801010")

    combined = index_detail(duckdb_store, "000300")
    assert combined["erp"] == erp_detail(duckdb_store, "000300")
    assert combined["valuation"] == valuation_detail(duckdb_store, "000300")


def test_unavailable_index_is_honest(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    overview = erp_compare(duckdb_store)
    unavailable = [i for i in overview["items"] if i["status"] == "unavailable"]
    assert len(unavailable) == 43, "空库时除全A合成指数外必须如实 unavailable，不伪造"


def test_index_api_endpoints(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> None:
    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)

    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(index_router)
    client = TestClient(app)

    catalog = client.get("/api/index/catalog")
    assert catalog.status_code == 200
    assert len(catalog.json()["items"]) == 44

    overview = client.get("/api/index/overview")
    assert overview.status_code == 200
    assert len(overview.json()["items"]) == 44

    compare = client.get("/api/index/erp-compare")
    assert compare.status_code == 200

    erp = client.get("/api/index/000300/erp")
    assert erp.status_code == 200
    assert erp.json()["name"] == "沪深300"
    erp_series = erp.json()["series"]
    assert len(erp_series) == 40, "种子里的 40 个估值点必须逐点配上国债算出 ERP（不许静默丢点）"
    assert [p["trade_date"] for p in erp_series] == sorted(
        p["trade_date"] for p in erp_series
    ), "ERP 序列必须按日期升序"
    assert all(
        p["erp"] == pytest.approx(1 / p["pe"] * 100 - p["treasury_yield"])
        for p in erp_series
    ), "series 里的 erp 必须真是 1/PE×100 − 10Y 国债收益率"

    detail = client.get("/api/index/000300/detail")
    assert detail.status_code == 200
    assert detail.json()["erp"]["series"] == erp_series, "详情页必须与 /{code}/erp 同源同算"
    pe_series = detail.json()["valuation"]["pe_series"]
    assert len(pe_series) == 40, "PE 序列必须逐点返回（不许去重/截断）"
    assert [p["trade_date"] for p in pe_series] == sorted(
        p["trade_date"] for p in pe_series
    ), "PE 序列必须按日期升序"
    assert all(10.0 <= p["value"] <= 14.5 for p in pe_series), "PE 列必须是 pe_ttm，不能混入 PB"

    valuation = client.get("/api/index/SW801010/valuation")
    assert valuation.status_code == 200
    assert valuation.json()["pe_metric"] == "sws_daily"

    missing = client.get("/api/index/999999/erp")
    assert missing.status_code == 404


def test_catalog_contains_agreed_universe() -> None:
    codes = {item["code"] for item in index_catalog()}
    assert {"000300", "000905", "000852", "000016"} <= codes
    assert len({c for c in codes if c.startswith("SW")}) == 31
    assert "SW801150" in codes  # 医药生物


def _index_client(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(index_router)
    return TestClient(app)


def test_erp_compare_propagates_duckdb_read_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """外部写进程持锁必须向上抛，不能被吞成全 error 快照。

    2026-09-17 修复：此前 erp_compare 的 except Exception 吞掉
    DuckDBReadLockedError，导致 API 层 stale 回退成为死代码。
    """
    from app.core import index_dashboard as core
    from app.core.storage.duckdb_store import DuckDBReadLockedError

    def locked(*_args: object, **_kwargs: object) -> dict:
        raise DuckDBReadLockedError("file already open in PID 28840")

    monkeypatch.setattr(core, "grouped_valuation_rows", locked)
    with pytest.raises(DuckDBReadLockedError):
        core.erp_compare(object())


def test_overview_keeps_warm_snapshot_when_duckdb_locked(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新窗口内卡片墙必须回退预热快照；无快照才 503。"""
    from app.core.storage.duckdb_store import DuckDBReadLockedError
    from app.web.api import index_dashboard as api

    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)
    client = _index_client(duckdb_store, sqlite_store)

    warm = client.get("/api/index/overview")
    assert warm.status_code == 200
    assert any(item.get("pe") is not None for item in warm.json()["items"])

    key = ("overview", str(duckdb_store.db_path))
    api._compare_cache.invalidate(key)  # 标记过期但保留旧快照

    def locked(*_args: object, **_kwargs: object) -> dict:
        raise DuckDBReadLockedError("file already open in PID 28840")

    monkeypatch.setattr(api, "erp_compare", locked)

    fallback = client.get("/api/index/overview")
    assert fallback.status_code == 200, "有预热快照时必须回退旧值而不是 503"
    assert fallback.json()["items"] == warm.json()["items"]

    api._compare_cache.clear()
    no_snapshot = client.get("/api/index/overview")
    assert no_snapshot.status_code == 503


def test_overview_rejects_all_error_snapshot_without_destroying_warm_cache(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全 error 快照不得覆盖/删除预热旧快照（非锁错误的源级失败场景）。"""
    from app.web.api import index_dashboard as api

    _seed_index_valuation(duckdb_store)
    _seed_treasury(duckdb_store)
    client = _index_client(duckdb_store, sqlite_store)

    warm = client.get("/api/index/overview")
    assert warm.status_code == 200

    key = ("overview", str(duckdb_store.db_path))
    api._compare_cache.invalidate(key)

    calls = 0

    def all_error(_duck: object) -> dict:
        nonlocal calls
        calls += 1
        return {
            "items": [
                {**item, "status": "error", "error": "prefetch boom"}
                for item in index_catalog()
            ],
            "updated_at": None,
        }

    monkeypatch.setattr(api, "erp_compare", all_error)

    fallback = client.get("/api/index/overview")
    assert fallback.status_code == 200
    assert fallback.json()["items"] == warm.json()["items"], "应返回旧快照而不是全 error 快照"

    # 无旧值时保留原契约：200 + 逐项 error，但坏快照不得写入缓存。
    api._compare_cache.clear()
    degraded = client.get("/api/index/overview")
    assert degraded.status_code == 200
    assert all(item.get("status") == "error" for item in degraded.json()["items"])
    assert calls == 2
    client.get("/api/index/overview")
    assert calls == 3, "全 error 快照不得进入缓存"
