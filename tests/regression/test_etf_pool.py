"""ETF 池配置与合并结果导入回归（2026-09-05 用户定稿）

覆盖：三层池（industry 26 / strategy 4 / market 4，无工具行业剔除）、
seed 幂等不覆盖预算、合并 CSV 模板/解析/幂等、schema v17 category 列。
"""

from __future__ import annotations

from pathlib import Path

from app.core.etf_pool import (
    DEFAULT_ETF_POOL,
    import_etf_merge_csv,
    seed_etf_pool,
    write_merge_template,
)
from app.core.etf_reset import bootstrap_portfolio, fresh_start, reset_portfolio
from app.core.etf_strategy import load_etf_meta, upsert_etf_meta
from app.core.storage.sqlite_store import SQLiteStore


def test_default_pool_layers_and_no_tool_industries_excluded() -> None:
    items = DEFAULT_ETF_POOL
    by_cat = {c: [i for i in items if i["category"] == c] for c in ("industry", "strategy", "market")}
    assert len(by_cat["industry"]) == 26
    assert len(by_cat["strategy"]) == 4
    assert len(by_cat["market"]) == 5
    market_codes = {i["etf_code"] for i in by_cat["market"]}
    assert {"510300", "510500", "512100", "513130", "ALL_A"} == market_codes, "全A指数纳入市场层"
    for missing in ("纺织服饰", "轻工制造", "商贸零售", "综合", "美容护理"):
        assert not any(i["industry_group"] == missing for i in by_cat["industry"])


def test_seed_pool_apply_inserts_and_preserves_existing_budget(sqlite_store: SQLiteStore) -> None:
    upsert_etf_meta(sqlite_store, etf_code="512880", name="证券ETF",
                    category="industry", budget=1234.0, step_pct=5.0)
    report = seed_etf_pool(sqlite_store, apply=True)
    assert report["status"] == "applied"
    assert report["total"] == len(DEFAULT_ETF_POOL)
    metas = {m["etf_code"]: m for m in load_etf_meta(sqlite_store)}
    assert metas["512880"]["budget"] == 1234.0, "seed 不得覆盖用户预算"
    assert metas["510300"]["category"] == "market"
    assert metas["513130"]["category"] == "market"


def test_merge_template_and_import_roundtrip(
    tmp_path: Path, sqlite_store: SQLiteStore,
) -> None:
    import csv

    template_path = tmp_path / "merge.csv"
    written = write_merge_template(template_path)
    assert "row_type" in written["columns"]

    with template_path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))

    rows = [
        {"row_type": "meta", "etf_code": "512880", "name": "证券ETF", "category": "industry",
         "track_index_code": "SW801790", "track_index_name": "非银金融",
         "primary_metric": "pb", "industry_group": "金融", "budget": "1000", "step_pct": "5"},
        {"row_type": "meta", "etf_code": "513130", "name": "恒生科技", "category": "market",
         "track_index_name": "恒生科技(同花顺5年分位)", "primary_metric": "pe",
         "industry_group": "市场指数"},
        {"row_type": "trade", "etf_code": "512880", "trade_date": "2026-09-04",
         "direction": "buy", "price": "1.10", "shares": "100", "amount": "110",
         "fee": "0.1", "note": "合并后首笔"},
        {"row_type": "trade", "etf_code": "512880", "trade_date": "2026-09-04",
         "direction": "sell", "price": "1.20", "shares": "50", "amount": "60",
         "fee": "0.1", "note": "卖出旧仓"},
        {"row_type": "cash", "flow_date": "2026-09-04", "cash_direction": "in",
         "cash_amount": "500"},
    ]
    path = tmp_path / "merge-result.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, restval="")
        writer.writeheader()
        writer.writerows(rows)

    preview = import_etf_merge_csv(sqlite_store, path, dry_run=True)
    assert preview["status"] == "preview"
    assert (preview["metas"], preview["trades"], preview["cash"]) == (2, 2, 1)

    first = import_etf_merge_csv(sqlite_store, path)
    assert first["status"] == "ok"
    assert first["trades_inserted"] == 2

    metas = {m["etf_code"]: m for m in load_etf_meta(sqlite_store)}
    assert metas["512880"]["category"] == "industry"
    assert metas["512880"]["budget"] == 1000.0
    assert metas["513130"]["category"] == "market"

    second = import_etf_merge_csv(sqlite_store, path)
    assert second["trades_inserted"] == 0, "重复流水必须跳过（幂等）"


def test_merge_import_rejects_unknown_row_type(
    tmp_path: Path, sqlite_store: SQLiteStore,
) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("row_type,etf_code\nweird,512880\n", encoding="utf-8")
    report = import_etf_merge_csv(sqlite_store, path)
    assert report["status"] == "invalid"
    assert "row_type 未知" in report["issues"][0]


def test_schema_v17_adds_category_column(database_paths) -> None:
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
    from app.core.storage.sqlite_store import SQLiteStore

    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)

    columns = {row["name"] for row in sqlite.query("PRAGMA table_info(etf_meta)")}
    assert "category" in columns
    versions = sqlite.query("SELECT MAX(version) AS v FROM schema_migrations")[0]["v"]
    assert versions >= 17


def test_reset_portfolio_clears_operations_but_keeps_pool(sqlite_store: SQLiteStore) -> None:
    from app.core.etf_strategy import add_cash_flow, add_etf_trade, set_setting

    upsert_etf_meta(sqlite_store, etf_code="512880", name="证券ETF",
                    category="industry", budget=1000.0, step_pct=5.0)
    upsert_etf_meta(sqlite_store, etf_code="512690", name="酒ETF",
                    category="industry", budget=200.0, step_pct=5.0)  # 池外旧持仓
    add_etf_trade(sqlite_store, etf_code="512880", trade_date="2026-09-04",
                  direction="buy", price=1.0, shares=100)
    add_cash_flow(sqlite_store, flow_date="2026-09-04", direction="in", amount=500.0)
    set_setting(sqlite_store, "total_assets", "4100.99")

    preview = reset_portfolio(sqlite_store)
    assert preview["status"] == "preview"
    assert preview["current"]["trades"] == 1
    assert any(item["etf_code"] == "512690" for item in preview["non_pool_to_disable"])

    report = reset_portfolio(sqlite_store, dry_run=False)
    assert report["status"] == "reset_done"
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_trades")[0]["c"] == 0
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_cash_flows")[0]["c"] == 0
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_sell_plans")[0]["c"] == 0
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_settings")[0]["c"] == 0

    metas = {m["etf_code"]: m for m in load_etf_meta(sqlite_store)}
    assert metas["512880"]["category"] == "industry", "重置不得破坏池配置"
    assert metas["512880"]["enabled"] == 1
    assert metas["512880"]["budget"] == 0.0, "重新开始后预算归零，由用户重新填写"
    assert metas["512690"]["enabled"] == 0, "池外旧持仓停止观察（记录保留）"
    assert metas["513130"]["category"] == "market", "池同步必须校正市场层"


def test_fresh_start_deletes_all_meta_and_reseeds_pool(sqlite_store: SQLiteStore) -> None:
    from app.core.etf_strategy import add_cash_flow, add_etf_trade, set_setting

    upsert_etf_meta(sqlite_store, etf_code="512690", name="酒ETF",
                    category="industry", budget=200.0, step_pct=5.0)
    add_etf_trade(sqlite_store, etf_code="512690", trade_date="2026-09-04",
                  direction="buy", price=1.0, shares=100)
    add_cash_flow(sqlite_store, flow_date="2026-09-04", direction="in", amount=500.0)
    set_setting(sqlite_store, "total_assets", "4100.99")

    preview = fresh_start(sqlite_store)
    assert preview["status"] == "preview"
    assert preview["meta_to_delete"] >= 1

    report = fresh_start(sqlite_store, dry_run=False)
    assert report["status"] == "fresh_start_done"
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_trades")[0]["c"] == 0
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_settings")[0]["c"] == 0
    metas = load_etf_meta(sqlite_store)
    assert len(metas) == len(DEFAULT_ETF_POOL), "旧元数据必须全部删除后重建默认池"
    assert all(meta["enabled"] == 1 for meta in metas)
    assert all(meta["budget"] == 0.0 for meta in metas)
    assert "512690" not in {meta["etf_code"] for meta in metas}


def test_bootstrap_portfolio_writes_capital_meta_and_initial_trades(
    sqlite_store: SQLiteStore,
) -> None:
    fresh_start(sqlite_store, dry_run=False)
    report = bootstrap_portfolio(
        sqlite_store,
        total_assets=10000.0,
        positions=[
            {
                "etf_code": "512880", "name": "证券ETF", "category": "industry",
                "track_index_code": "SW801790", "track_index_name": "非银金融",
                "primary_metric": "pb", "industry_group": "金融",
                "budget": 2000.0, "step_pct": 5.0,
                "shares": 1000, "price": 1.1, "trade_date": "2026-09-04",
                "fee": 0.2,
            },
            {
                "etf_code": "510300", "name": "沪深300ETF", "category": "market",
                "track_index_code": "000300", "track_index_name": "沪深300",
                "primary_metric": "pe", "industry_group": "市场指数",
                "budget": 3000.0, "step_pct": 5.0,
            },
        ],
    )
    assert report["status"] == "bootstrapped"
    assert report["positions_written"] == 2
    assert report["trades_written"] == 1

    metas = {meta["etf_code"]: meta for meta in load_etf_meta(sqlite_store)}
    assert metas["512880"]["budget"] == 2000.0
    assert metas["510300"]["budget"] == 3000.0
    assert sqlite_store.query("SELECT value FROM etf_settings WHERE key='total_assets'")[0]["value"] == "10000.0"
    trades = sqlite_store.query("SELECT * FROM etf_trades WHERE etf_code='512880'")
    assert len(trades) == 1
    assert trades[0]["direction"] == "buy"
    assert trades[0]["note"] == "初始化持仓"
