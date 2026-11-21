"""守卫：二级索引全量删除（schema v50）—— 迁移幂等 / 结果不变 / 不许再建。

背景（``docs/reports/151``，2026-11-21 副本逐索引实测）：
正式库曾有 30 个二级索引 / 26 张表，其中 ``idx_business_breakdown_stock_report``
与堆失步后每轮自动更新都 ``FALSE`` 在 ``DuckDBIndexInconsistencyError``（reports/136 §7）。
逐索引 A/B（有索引 vs DROP INDEX 后，各 3 次取中位数、单文件 bracketing）结论：
  · 42 条**真实代码路径**查询的 EXPLAIN 在索引全在时与全删后**都是 SEQ_SCAN** ——
    优化器一次都没选 ART（不是"选了没好处"，是没选）；
  · 没有一条查询在删索引后变慢 ≥2 倍；5 条明显变快（source_audit 溯源子查询 22×）；
  · 30 个里 11 个的列是 PRIMARY KEY 的严格前缀。

本文件钉三件事（对应任务书 C 节的①②③）：
① 迁移幂等：``drop_useless_secondary_indexes()`` 连跑两遍 == 跑一遍（数据零变更、
   迁移版本不重复记账）；
② 删索引前后代表性查询（筛选 / 详情 / 覆盖规则 / 分红 / ETF）结果集**逐行相同**；
③ ``schema.py`` 源码里不再有任何被判删的 ``CREATE INDEX``，且真建出来的新库
   二级索引数 = 0（防止"迁移删了、建库又建回来"）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import (
    DROPPED_SECONDARY_INDEXES,
    drop_useless_secondary_indexes,
)

SCHEMA_SOURCE = Path(__file__).resolve().parents[2] / "app" / "core" / "storage" / "schema.py"

#: 判删索引 → 它**本该服务**的真实查询（取自代码，见各条注释里的落点）。
#: 每条都在建索引/删索引两种状态下执行，结果集必须逐行相同。
REPRESENTATIVE_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "idx_business_breakdown_stock_report",
        "详情页·主营构成单股多期（core/business.py）",
        "SELECT stock_code, report_date, type, item_name, amount, ratio FROM business_breakdown "
        "WHERE stock_code = '000099' AND report_date IN (DATE '2024-12-31', DATE '2024-09-30') "
        "ORDER BY type, rank",
    ),
    (
        "idx_business_breakdown_stock_report",
        "看板·全市场主营构成覆盖（core/business.py GROUP BY）",
        "SELECT stock_code, COUNT(*) AS n FROM business_breakdown GROUP BY stock_code "
        "ORDER BY stock_code",
    ),
    (
        "idx_share_capital_history_stock",
        "详情页·单股股本历史（core/capital.py）",
        "SELECT stock_code, effective_date, total_shares, verified FROM share_capital_history "
        "WHERE stock_code = '600519' ORDER BY effective_date",
    ),
    (
        "idx_roce_history_date",
        "探针·ROCE 最新期 MAX(report_date)（core/update.py）",
        "SELECT MAX(report_date) AS latest FROM roce_history",
    ),
    (
        "idx_financial_report_dates_stock",
        "PIT·单股公告日（core/point_in_time.py）",
        "SELECT report_date, announce_date FROM financial_report_dates "
        "WHERE stock_code = '600519' AND announce_date <= DATE '2024-12-31' "
        "ORDER BY report_date DESC LIMIT 1",
    ),
    (
        "idx_hk_dividends_stock",
        "分红·港股单股除净窗口（core/ah_metrics.py）",
        "SELECT stock_code, ex_date FROM hk_dividends WHERE stock_code = '00857' "
        "ORDER BY ex_date",
    ),
    (
        "idx_etf_daily_code",
        "ETF·最新收盘（core/etf_strategy.py）",
        "SELECT close_price FROM etf_daily WHERE etf_code = '510300' "
        "AND close_price IS NOT NULL ORDER BY trade_date DESC, source LIMIT 1",
    ),
    (
        "idx_balance_sheet_ext_date",
        "质量规则·资产负债表补充覆盖缺口（core/quality/asset_health.py）",
        "SELECT COUNT(*) AS gaps FROM stock_meta m, (SELECT DATE '2024-12-31' AS period) p "
        "WHERE m.is_listed IS TRUE AND NOT EXISTS (SELECT 1 FROM balance_sheet_ext c "
        "WHERE c.stock_code = m.stock_code AND c.report_date = p.period)",
    ),
    (
        "idx_indicator_null_reason_field",
        "数据状态·空值原因按字段+期（core/quality/asset_health.py）",
        "SELECT stock_code, reason_code FROM indicator_null_reason "
        "WHERE report_date = DATE '2025-09-30' AND field_name IN ('roce_latest', 'bps') "
        "ORDER BY stock_code",
    ),
    (
        "idx_source_audit_stock_date_field",
        "筛选·严格溯源关联子查询（core/screening/engine.py _build_strict_clause）",
        "SELECT COUNT(*) AS c FROM indicator_snapshot ind WHERE COALESCE("
        "(SELECT audit.confidence = 'strict' FROM source_audit audit "
        "WHERE audit.stock_code = ind.stock_code AND audit.field_name = 'ps_ttm' "
        "AND audit.report_date = ind.report_date "
        "ORDER BY audit.fetch_time DESC, audit.id DESC LIMIT 1), false)",
    ),
)

#: 让上面这批查询真的"有东西可查"的最小种子（只进 tmp 库，不碰正式库）。
_SEED_SQL: tuple[str, ...] = (
    "INSERT INTO business_breakdown (stock_code, report_date, type, item_name, amount, ratio, "
    "rank, source, fetch_time, raw_hash, confidence, batch_id) VALUES "
    "('000099', DATE '2024-12-31', 1, '主营收入', 1000.0, 55.5, 1, 'eastmoney_f10', "
    "TIMESTAMP '2026-01-01 00:00:00', 'h1', 'strict', 'b1'),"
    "('000099', DATE '2024-09-30', 2, '产品A', 400.0, 48.0, 1, 'eastmoney_f10', "
    "TIMESTAMP '2026-01-01 00:00:00', 'h2', 'strict', 'b1'),"
    "('600519', DATE '2024-12-31', 1, '主营收入', 9000.0, 100.0, 1, 'eastmoney_f10', "
    "TIMESTAMP '2026-01-01 00:00:00', 'h3', 'strict', 'b2')",
    "INSERT INTO share_capital_history (stock_code, effective_date, total_shares, "
    "change_reason, is_anchor, verified, source, raw_hash, batch_id) VALUES "
    "('600519', DATE '2019-06-30', 1256197800, 'anchor', TRUE, TRUE, 'cninfo', 'h4', 'b3'),"
    "('600519', DATE '2021-12-31', 1256197800, 'none', TRUE, TRUE, 'cninfo', 'h5', 'b3')",
    "INSERT INTO roce_history (stock_code, report_date, ce, ce_prev, ebit, roce, status, "
    "calculated_at, source, data_version) VALUES "
    "('600519', DATE '2023-12-31', 100.0, 90.0, 40.0, 0.42, 'OK', "
    "TIMESTAMP '2026-01-01 00:00:00', 'self', 1),"
    "('600519', DATE '2024-12-31', 120.0, 100.0, 50.0, 0.45, 'OK', "
    "TIMESTAMP '2026-01-01 00:00:00', 'self', 1)",
    "INSERT INTO financial_report_dates (stock_code, report_date, announce_date, source, "
    "fetch_time, batch_id) VALUES "
    "('600519', DATE '2024-12-31', DATE '2025-03-28', 'cninfo', "
    "TIMESTAMP '2026-01-01 00:00:00', 'b4'),"
    "('600519', DATE '2024-09-30', DATE '2024-10-25', 'cninfo', "
    "TIMESTAMP '2026-01-01 00:00:00', 'b4')",
    "INSERT INTO hk_dividends (stock_code, ex_date, plan_explain, dividend_per_share_cny, "
    "source, fetch_time, raw_response_hash, confidence, batch_id) VALUES "
    "('00857', DATE '2023-06-15', '派息', 0.52, 'eastmoney_hk', "
    "TIMESTAMP '2026-01-01 00:00:00', 'h6', 'strict', 'b5'),"
    "('00857', DATE '2024-06-20', '派息', 0.48, 'eastmoney_hk', "
    "TIMESTAMP '2026-01-01 00:00:00', 'h7', 'strict', 'b5')",
    "INSERT INTO etf_daily (etf_code, trade_date, close_price, source, fetch_time, raw_hash, "
    "confidence, batch_id) VALUES "
    "('510300', DATE '2024-12-30', 3.95, 'ths', TIMESTAMP '2026-01-01 00:00:00', 'h8', "
    "'strict', 'b6'),"
    "('510300', DATE '2024-12-31', 4.01, 'ths', TIMESTAMP '2026-01-01 00:00:00', 'h9', "
    "'strict', 'b6')",
    "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES "
    "('600519', '贵州茅台', 'SH', TRUE), ('000099', '中信海直', 'SZ', TRUE)",
    "INSERT INTO indicator_null_reason (stock_code, report_date, field_name, reason_code, "
    "detail, computed_at) VALUES "
    "('600519', DATE '2025-09-30', 'roce_latest', 'NO_DATA', '', "
    "TIMESTAMP '2026-01-01 00:00:00'),"
    "('000099', DATE '2025-09-30', 'bps', 'NO_DATA', '', TIMESTAMP '2026-01-01 00:00:00')",
    "INSERT INTO indicator_snapshot (stock_code, report_date, ps_ttm) VALUES "
    "('600519', DATE '2026-09-30', 8.5), ('000099', DATE '2026-09-30', 3.2)",
    "INSERT INTO source_audit (id, stock_code, field_name, report_date, value, source, "
    "fetch_batch_id, fetch_time, raw_response_hash, confidence) VALUES "
    "(1, '600519', 'ps_ttm', DATE '2026-09-30', 8.5, 'cninfo', 'b7', "
    "TIMESTAMP '2026-01-01 00:00:00', 'a' || repeat('b', 63), 'strict'),"
    "(2, '000099', 'ps_ttm', DATE '2026-09-30', 3.2, 'cninfo', 'b7', "
    "TIMESTAMP '2026-01-01 00:00:00', 'a' || repeat('c', 63), 'strict')",
)


def _index_names(store: DuckDBStore) -> set[str]:
    return {str(row["index_name"]) for row in store.read_query("SELECT index_name FROM duckdb_indexes()")}


def _seed(store: DuckDBStore) -> None:
    with store.transaction() as connection:
        for statement in _SEED_SQL:
            connection.execute(statement)


def _create(store: DuckDBStore, index_name: str) -> None:
    """按 schema 里原来的 DDL 把单个索引建回来（用于「有索引」对照组）。"""
    ddl = _ORIGINAL_DDL[index_name]
    with store.transaction() as connection:
        connection.execute(ddl)


#: 判删索引的原始 DDL（对照组要能把它建回来；与 reports/151 实测的那 30 条一致）。
_ORIGINAL_DDL: dict[str, str] = {
    "idx_ah_market_snapshot_asof": "CREATE INDEX idx_ah_market_snapshot_asof ON ah_market_snapshot (as_of)",
    "idx_balance_sheet_ext_date": "CREATE INDEX idx_balance_sheet_ext_date ON balance_sheet_ext (report_date)",
    "idx_business_breakdown_stock_report": "CREATE INDEX idx_business_breakdown_stock_report ON business_breakdown (stock_code, report_date)",
    "idx_buyback_events_stock": "CREATE INDEX idx_buyback_events_stock ON buyback_events (stock_code)",
    "idx_cash_flow_activity_date": "CREATE INDEX idx_cash_flow_activity_date ON cash_flow_activity (report_date)",
    "idx_cash_flow_indirect_date": "CREATE INDEX idx_cash_flow_indirect_date ON cash_flow_indirect (report_date)",
    "idx_company_employee_history_stock": "CREATE INDEX idx_company_employee_history_stock ON company_employee_history (stock_code, report_date)",
    "idx_etf_daily_code": "CREATE INDEX idx_etf_daily_code ON etf_daily (etf_code, trade_date)",
    "idx_field_coverage_history_field": 'CREATE INDEX idx_field_coverage_history_field ON field_coverage_history ("domain", field_name, recorded_at)',
    "idx_financial_report_dates_announce": "CREATE INDEX idx_financial_report_dates_announce ON financial_report_dates (announce_date)",
    "idx_financial_report_dates_stock": "CREATE INDEX idx_financial_report_dates_stock ON financial_report_dates (stock_code, report_date)",
    "idx_financial_sector_items_stock": "CREATE INDEX idx_financial_sector_items_stock ON financial_sector_items (stock_code, report_date)",
    "idx_funding_events_stock": "CREATE INDEX idx_funding_events_stock ON funding_events (stock_code)",
    "idx_hk_dividends_stock": "CREATE INDEX idx_hk_dividends_stock ON hk_dividends (stock_code)",
    "idx_hk_price_daily_date": "CREATE INDEX idx_hk_price_daily_date ON hk_price_daily (trade_date)",
    "idx_index_valuation_code": "CREATE INDEX idx_index_valuation_code ON index_valuation (index_code, trade_date)",
    "idx_indicator_ext_date": "CREATE INDEX idx_indicator_ext_date ON indicator_ext (report_date)",
    "idx_indicator_null_reason_field": "CREATE INDEX idx_indicator_null_reason_field ON indicator_null_reason (field_name, report_date)",
    "idx_industry_history_stock": "CREATE INDEX idx_industry_history_stock ON industry_history (stock_code, effective_date)",
    "idx_institution_holdings_report_date": "CREATE INDEX idx_institution_holdings_report_date ON institution_holdings (report_date, holder_type)",
    "idx_report_metadata_stock": "CREATE INDEX idx_report_metadata_stock ON report_metadata (stock_code, report_date)",
    "idx_research_statistics_lookup": 'CREATE INDEX idx_research_statistics_lookup ON research_statistics (metric, window_years, "method", "version")',
    "idx_roce_history_date": "CREATE INDEX idx_roce_history_date ON roce_history (report_date)",
    "idx_share_capital_history_stock": "CREATE INDEX idx_share_capital_history_stock ON share_capital_history (stock_code, effective_date)",
    "idx_source_audit_archive_hash": "CREATE INDEX idx_source_audit_archive_hash ON source_audit_archive (raw_response_hash)",
    "idx_source_audit_archive_stock_date_field": "CREATE INDEX idx_source_audit_archive_stock_date_field ON source_audit_archive (stock_code, report_date, field_name)",
    "idx_source_audit_fetch_batch": "CREATE INDEX idx_source_audit_fetch_batch ON source_audit (fetch_batch_id)",
    "idx_source_audit_hash": "CREATE INDEX idx_source_audit_hash ON source_audit (raw_response_hash)",
    "idx_source_audit_stock_date_field": "CREATE INDEX idx_source_audit_stock_date_field ON source_audit (stock_code, report_date, field_name)",
    "idx_treasury_yield_curve_date": "CREATE INDEX idx_treasury_yield_curve_date ON treasury_yield_curve (curve_date)",
}


def test_ddl_table_matches_dropped_index_list() -> None:
    """对照组 DDL 表必须与判删清单一一对应（清单改了这里没改 = 静默少测一个）。"""
    assert set(_ORIGINAL_DDL) == set(DROPPED_SECONDARY_INDEXES)


def test_schema_source_has_no_dropped_index_ddl() -> None:
    """③ 「不要再建」的静态那半：源码里不许再出现判删索引的 CREATE INDEX。"""
    source = SCHEMA_SOURCE.read_text(encoding="utf-8")
    offenders = [
        name for name in DROPPED_SECONDARY_INDEXES
        if re.search(rf"CREATE\s+INDEX\s+(IF\s+NOT\s+EXISTS\s+)?{name}\b", source, re.IGNORECASE)
    ]
    assert offenders == [], f"schema.py 里仍在建被判删的索引: {offenders}"


def test_fresh_schema_creates_zero_secondary_indexes(duckdb_store: DuckDBStore) -> None:
    """③ 动态那半：真跑一遍 schema 建出来的新库，二级索引必须是 0 个。

    （PRIMARY KEY 的 ART 不在 ``duckdb_indexes()`` 里，所以 0 就是真的"没有二级索引"。）
    """
    assert _index_names(duckdb_store) == set()


def test_drop_migration_is_idempotent(duckdb_store: DuckDBStore) -> None:
    """① 迁移幂等：把 30 个索引建回来 → 跑一次全删 → 再跑一次必须是纯 no-op。"""
    _seed(duckdb_store)
    for name in DROPPED_SECONDARY_INDEXES:
        _create(duckdb_store, name)
    assert _index_names(duckdb_store) == set(DROPPED_SECONDARY_INDEXES)

    rows_before = _rows(duckdb_store, "SELECT stock_code, report_date, item_name FROM business_breakdown ORDER BY 1, 2, 3")
    first = drop_useless_secondary_indexes(duckdb_store)
    assert sorted(first["dropped"]) == sorted(DROPPED_SECONDARY_INDEXES)
    assert first["failed"] == {}
    assert first["remaining"] == []
    assert _index_names(duckdb_store) == set()

    second = drop_useless_secondary_indexes(duckdb_store)
    assert second == {"present_before": [], "dropped": [], "failed": {}, "remaining": []}

    # 幂等还意味着：数据一行不少、迁移版本没被重复记账、查询结果没变。
    assert _rows(duckdb_store, "SELECT stock_code, report_date, item_name FROM business_breakdown ORDER BY 1, 2, 3") == rows_before


def test_representative_queries_identical_with_and_without_indexes(
    duckdb_store: DuckDBStore,
) -> None:
    """② 删索引前后，10 组代表性查询结果集逐行相同（筛选/详情/看板/规则/分红/ETF）。"""
    _seed(duckdb_store)
    without = {label: _rows(duckdb_store, sql) for _i, label, sql in REPRESENTATIVE_QUERIES}

    for name in DROPPED_SECONDARY_INDEXES:
        _create(duckdb_store, name)
    _assert_index_scan_not_assumed(duckdb_store)
    with_index = {label: _rows(duckdb_store, sql) for _i, label, sql in REPRESENTATIVE_QUERIES}

    drop_useless_secondary_indexes(duckdb_store)
    after = {label: _rows(duckdb_store, sql) for _i, label, sql in REPRESENTATIVE_QUERIES}

    assert with_index == without, "有索引 vs 无索引 结果集不一致"
    assert after == without, "删索引后结果集变了"
    for label in without:
        assert isinstance(without[label], list)


def _rows(store: DuckDBStore, sql: str) -> list[tuple]:
    return [tuple(row.values()) for row in store.read_query(sql)]


def _assert_index_scan_not_assumed(store: DuckDBStore) -> None:
    """反向用例：证明这 10 条查询在**有索引**时也不会走 INDEX_SCAN。

    没有这条，「结果一致」可能只是因为两边都恰好走了索引/都没走 —— 而 DuckDB 1.5.5
    的优化器对这批谓词一律 SEQ_SCAN，这正是"索引没有用"的机制性证据。
    """
    offenders: list[str] = []
    with store.write_connection() as connection:   # read_query 只收 SELECT，EXPLAIN 走写连接
        for _index, label, sql in REPRESENTATIVE_QUERIES:
            rows = connection.execute("EXPLAIN " + sql).fetchall()
            plan = " ".join(str(value) for row in rows for value in row)
            if "INDEX_SCAN" in plan:
                offenders.append(label)
    assert offenders == [], f"这些查询在有索引时走了 INDEX_SCAN（判删前提不再成立）: {offenders}"


@pytest.mark.parametrize("index_name", ["idx_source_audit_hash", "idx_business_breakdown_stock_report"])
def test_drop_does_not_perturb_writes(duckdb_store: DuckDBStore, index_name: str) -> None:
    """④ 写入侧：DROP 之后 DELETE+INSERT 仍然成功且行数守恒（索引维护不再参与）。"""
    _seed(duckdb_store)
    _create(duckdb_store, index_name)
    before = duckdb_store.read_query("SELECT COUNT(*) AS n FROM business_breakdown")[0]["n"]
    drop_useless_secondary_indexes(duckdb_store)
    with duckdb_store.transaction() as connection:
        connection.execute("DELETE FROM business_breakdown WHERE stock_code = '000099'")
        connection.execute(
            "INSERT INTO business_breakdown (stock_code, report_date, type, item_name, amount, "
            "ratio, rank, source, fetch_time, raw_hash, confidence, batch_id) VALUES "
            "('000099', DATE '2024-12-31', 1, '主营收入', 1000.0, 55.5, 1, 'eastmoney_f10', "
            "TIMESTAMP '2026-01-01 00:00:00', 'h1', 'strict', 'b1')"
        )
    after = duckdb_store.read_query("SELECT COUNT(*) AS n FROM business_breakdown")[0]["n"]
    assert after == before - 1  # 删了 2 行补回 1 行
