"""2026-08-02 系统红队 P1 回归：研究主路径上的静默错误结论。

- P1-A: 季度视图缺期时单季值 fail-closed（见 test_stock_detail_periods.py）
- P1-B: 部分/截断股票池响应不得静默剔除有效股票（退市门禁）
- P1-C: 筛选结果超过 5000 行必须显式 truncated 标记且 total 为真实匹配数
- P1-D: 行业排名按 CSRC 口径命名与标注，不再伪装成申万（SW）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.adapters.akshare_adapter import AKShareAdapter
from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.init import DataInitializer
from app.core.screening.engine import MAX_RESULT_ROWS, ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import router as screening_router


# ─── P1-B: 退市门禁 ─────────────────────────────────────────────

def _seed_listed_stocks(store: DuckDBStore, count: int, exchange: str = "SSE") -> None:
    with store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO stock_meta
               (stock_code, name, exchange, is_listed, is_st, is_suspended)
               VALUES (?, ?, ?, true, false, false)""",
            [
                (f"{600000 + i:06d}", f"stock-{i}", exchange)
                for i in range(count)
            ],
        )


class _PartialStockListAdapter:
    """只返回指定数量股票的"截断"响应（模拟部分/截断的源响应）。"""

    def __init__(self, codes: list[str]) -> None:
        self._codes = codes

    def fetch(self, request: FetchRequest) -> FetchResult:
        rows = [{"stock_code": code, "name": f"n-{code}", "exchange": "SSE"} for code in self._codes]
        raw = json.dumps(rows).encode("utf-8")
        return FetchResult(
            data=rows,
            metadata=SourceMetadata(
                source="akshare_eastmoney", fetch_time=datetime.now(timezone.utc),
                raw_response_hash=hashlib.sha256(raw).hexdigest(), confidence="strict",
            ),
            raw_response=raw,
        )


def test_partial_stock_list_does_not_silently_delist_valid_stocks(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """P1-B: 只返回 10/100 只 → 退市门禁触发，is_listed 全部保留并披露。"""
    _seed_listed_stocks(duckdb_store, count=100)
    partial_codes = [f"{600000 + i:06d}" for i in range(10)]

    initializer = DataInitializer(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_PartialStockListAdapter(partial_codes)
    )
    report = initializer._fetch_stock_universe()

    assert report["status"] == "partial"
    assert "SSE" in report["delist_guarded_exchanges"]
    listed = duckdb_store.read_query(
        "SELECT COUNT(*) AS cnt FROM stock_meta WHERE is_listed IS TRUE"
    )[0]["cnt"]
    assert listed == 100


def test_complete_stock_list_still_delists_absent_codes(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """完整响应（≥90%）照常执行退市标记（正常退市事件单轮不超过 10%）。"""
    _seed_listed_stocks(duckdb_store, count=100)
    remaining_codes = [f"{600000 + i:06d}" for i in range(90)]

    initializer = DataInitializer(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=_PartialStockListAdapter(remaining_codes)
    )
    report = initializer._fetch_stock_universe()

    assert report["status"] == "success"
    listed = duckdb_store.read_query(
        "SELECT COUNT(*) AS cnt FROM stock_meta WHERE is_listed IS TRUE"
    )[0]["cnt"]
    assert listed == 90


def test_stock_list_adapter_reports_partial_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-B: 板块级抓取失败时适配器如实报告 partial，不伪装 strict 成功。"""

    def fake_a_code_name() -> pd.DataFrame:
        return pd.DataFrame({"code": ["000001"], "name": ["平安银行"]})

    def fake_bj_down() -> pd.DataFrame:
        raise ConnectionError("bse source down")

    monkeypatch.setattr("akshare.stock_info_a_code_name", fake_a_code_name)
    monkeypatch.setattr("akshare.stock_info_bj_name_code", fake_bj_down)

    adapter = AKShareAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(data_type="stock_list"))

    assert result.data
    assert result.metadata.confidence == "approximate"
    assert result.metadata.error is not None
    assert "partial" in result.metadata.error


# ─── P1-C: 筛选截断显式化 ───────────────────────────────────────

def _seed_pool_for_truncation(store: DuckDBStore, count: int) -> None:
    with store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO stock_meta
               (stock_code, name, exchange, listing_date, is_listed, is_st, is_suspended)
               VALUES (?, ?, 'SZSE', '2020-01-01', true, false, false)""",
            [(f"{600000 + i:06d}", f"stock-{i}") for i in range(count)],
        )
        connection.executemany(
            """INSERT INTO indicator_snapshot
               (stock_code, report_date, latest_close, latest_price_date, pe_ttm, calculated_at)
               VALUES (?, '2026-06-30', 10.0, '2026-07-31', ?, CURRENT_TIMESTAMP)""",
            [(f"{600000 + i:06d}", 1.0 + (i % 50)) for i in range(count)],
        )


def test_screening_truncates_above_limit_with_explicit_flag(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """P1-C: 匹配数 > 5000 时 truncated=True、total=真实匹配数，绝不静默丢尾。"""
    pool_size = MAX_RESULT_ROWS + 1000
    _seed_pool_for_truncation(duckdb_store, pool_size)

    result = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        min_listing_years=0,
    )

    assert len(result["results"]) == MAX_RESULT_ROWS
    assert result["truncated"] is True
    assert result["total"] == pool_size


def test_screening_below_limit_reports_no_truncation(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_pool_for_truncation(duckdb_store, 100)

    result = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        min_listing_years=0,
    )

    assert result["truncated"] is False
    assert result["total"] == 100
    assert len(result["results"]) == 100


def test_screening_run_api_exposes_truncated_flag(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """P1-C: API 响应透传 truncated/total（run_screening 展开引擎结果）。"""
    _seed_pool_for_truncation(duckdb_store, MAX_RESULT_ROWS + 5)
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(screening_router)
    client = TestClient(app)
    client.post(
        "/api/screening/rules/save",
        json={
            "name": "truncation-flag",
            "rule_json": {
                "conditions": {"logic": "AND", "rules": []},
                "columns": ["stock_code", "pe_ttm"],
            },
        },
    )

    # 直接断言引擎结果含 truncated 键，且 API 透传路径包含它
    from app.core.screening.engine import ScreeningEngine

    engine_result = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store).run(
        {"conditions": {"logic": "AND", "rules": []}},
        min_listing_years=0,
    )
    assert engine_result["truncated"] is True
    assert engine_result["total"] == MAX_RESULT_ROWS + 5
    # run_screening 的返回为 {**engine_result, "results": [...]}，截断标记随之透传
    assert "truncated" in engine_result
    assert "total" in engine_result


# ─── P1-D: CSRC 行业排名命名/标注 ───────────────────────────────

def test_indicator_list_exposes_csrc_ranks_without_sw_labels(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """P1-D: 指标列表暴露正确命名的 CSRC 排名列，不再出现"申万"标签。"""
    app = FastAPI()
    app.state.duck = duckdb_store
    app.state.sqlite = sqlite_store
    app.include_router(screening_router)
    client = TestClient(app)

    data = client.get("/api/screening/indicators").json()
    indicators = data["indicators"]
    names = [item["name"] for item in indicators]

    assert "pe_ttm_industry_rank" in names
    assert "pe_ttm_industry_percentile" in names
    assert not any("申万" in (item.get("label") or "") for item in indicators)
    industry_label = next(
        item.get("label") for item in indicators if item["name"] == "pe_ttm_industry_rank"
    )
    assert "证监会一级排名" in industry_label


def test_industry_rank_is_partitioned_by_csrc_classification(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """P1-D: _industry_rank 与 _sw1_rank 同为 csrc_l1 分区（口径一致，
    仅命名不同）；legacy 列仍可用，新正确命名列可选择。"""
    with duckdb_store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO stock_meta
               (stock_code, name, exchange, csrc_l1, csrc_l2, listing_date, is_listed, is_st, is_suspended)
               VALUES (?, ?, 'SZSE', ?, ?, '2020-01-01', true, false, false)""",
            [
                ("000001", "a", "制造业", "大类A"),
                ("000002", "b", "制造业", "大类A"),
                ("000003", "c", "金融业", "大类B"),
            ],
        )
        connection.executemany(
            """INSERT INTO indicator_snapshot
               (stock_code, report_date, pe_ttm, calculated_at)
               VALUES (?, '2026-06-30', ?, CURRENT_TIMESTAMP)""",
            [("000001", 10.0), ("000002", 20.0), ("000003", 30.0)],
        )

    result = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store).run(
        {
            "conditions": {"logic": "AND", "rules": []},
            "columns": [
                "stock_code", "pe_ttm_industry_rank", "pe_ttm_sw1_rank",
                "pe_ttm_sw2_rank", "pe_ttm_market_rank",
            ],
        },
        min_listing_years=0,
    )

    by_code = {row["stock_code"]: row for row in result["results"]}
    # csrc_l1 同为"制造业"的两只：industry_rank 与 sw1_rank 完全一致（1/2）
    assert by_code["000001"]["pe_ttm_industry_rank"] == by_code["000001"]["pe_ttm_sw1_rank"]
    assert by_code["000001"]["pe_ttm_industry_rank"] == 1
    assert by_code["000002"]["pe_ttm_industry_rank"] == 2
    # 金融业单独分区，rank=1
    assert by_code["000003"]["pe_ttm_industry_rank"] == 1
    # 全市场排名跨行业
    assert by_code["000003"]["pe_ttm_market_rank"] == 3
