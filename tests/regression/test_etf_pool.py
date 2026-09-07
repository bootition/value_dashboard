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
from app.core.etf_strategy import load_etf_meta, upsert_etf_meta
from app.core.storage.sqlite_store import SQLiteStore


def test_default_pool_layers_and_no_tool_industries_excluded() -> None:
    items = DEFAULT_ETF_POOL
    by_cat = {c: [i for i in items if i["category"] == c] for c in ("industry", "strategy", "market")}
    assert len(by_cat["industry"]) == 26
    assert len(by_cat["strategy"]) == 4
    assert len(by_cat["market"]) == 4
    market_codes = {i["etf_code"] for i in by_cat["market"]}
    assert {"510300", "510500", "512100", "513130"} == market_codes, "恒生科技纳入市场层"
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
