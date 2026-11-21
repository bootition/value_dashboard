"""守卫：二级索引全量删除（schema v50）—— 迁移幂等 / 记账可重试 / 结果不变 / 不许再建。

背景（``docs/reports/151``，2026-11-21 副本逐索引实测；机制解释经独立审查 2026-11-21 纠正）。
正式库曾有 30 个二级索引 / 26 张表，其中 ``idx_business_breakdown_stock_report`` 与堆失步后
每轮自动更新都死在 ``DuckDBIndexInconsistencyError``（reports/136 §7）。

**删索引的正当性不是"优化器弃用它们"**（那是错的机制解释）：DuckDB 1.5.5 **会**选这批 ART 索引 ——
``EXPLAIN ANALYZE`` 下等值查询的计划节点就是 ``Type: Index Scan``（22.9M 行 ``source_audit``
实测 0.49ms；DROP 后同一查询 24.57ms；重建后 0.42ms）。普通 ``EXPLAIN`` 不打印执行期扫描类型，
所以"EXPLAIN 全是 SEQ_SCAN"看着像"没选"，其实什么都没证明。

真正的理由是**这些索引的列与真实查询形状不匹配**：代码里做的是 JOIN / ``IN (子查询)`` /
范围扫描，**没有任何 ``col = 字面量``**；42 条真实代码路径查询删索引后没有一条变慢 ≥2 倍
（最慢 1.43 倍、绝对值 <2ms）。

本文件钉四件事（①②③ 是原任务书，④ 是 2026-11-21 审查后换掉假绿断言的新判据）：
① 迁移幂等 + **记账可重试**：``drop_useless_secondary_indexes()`` 连跑两遍 == 跑一遍；
   且"没清干净就不许写 v50 记账"，否则 ``skip_if_current`` 快速启动会让它永不重试
   （审查缺陷 1）；
② 删索引前后代表性查询（筛选 / 详情 / 覆盖规则 / 分红 / ETF）结果集**逐行相同**；
③ 全仓 ``*.py`` 里除 SQLite 侧那几条之外**不再有任何可执行 ``CREATE INDEX``**
   （审查缺陷 2：``scripts/repair_share_capital_history_index.py`` 曾能把索引建回来）；
④ **可证伪的形状判据**（审查缺陷 3）：生产代码里不存在
   ``<被删索引首列> = <字面量>`` 的真实查询 —— 这个形状一旦出现，索引就值得留，
   本守卫变红要求重新裁定；判据另有一条"非空转自检"，证明它真的会红。
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import (
    DROPPED_SECONDARY_INDEXES,
    V50_DROP_DESCRIPTION,
    _record_v50_drop_migration,
    drop_useless_secondary_indexes,
    init_all_schema,
)
from app.core.storage.sqlite_store import SQLiteStore

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE = REPO_ROOT / "app" / "core" / "storage" / "schema.py"

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

#: 全仓扫描范围：Python 生产代码 + 测试（``build/`` ``_legacy/`` ``.agents/`` 是构建产物/别的
#: worktree，不是本仓事实来源，故不扫）。
_SCAN_ROOTS: tuple[str, ...] = ("app", "scripts", "tests")
_SCAN_SKIP_PARTS = frozenset({"__pycache__", ".ipynb_checkpoints"})

_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)",
    re.IGNORECASE,
)

#: 全仓 ``*.py`` 里**允许存在的可执行 ``CREATE INDEX``**（其余一律红）。
#: 只有两类，且都必须写清"为什么不是 v50 漏网"：
_ALLOWED_CREATE_INDEX_SITES: dict[str, frozenset[str]] = {
    # SQLite 侧（另一个引擎，planner 真的用 B-tree；审查明确要求不要动它们）。
    "app/core/storage/schema.py": frozenset({
        "idx_etf_trades_code",
        "idx_announcement_registry_stock_time",
        "idx_announcement_registry_time",
        # SQLite v1/v2/v3 的历史唯一索引（同一引擎、同一理由；v50 只管 DuckDB）。
        "uq_manual_overrides_published",
        "uq_retry_list_request",
        "uq_missing_list_stock_field_open",
        "uq_watchlist_stock_group",
    }),
    # 本文件自己：对照组必须能把 30 个索引**建回来**才能做「有索引 vs 无索引」对比。
    "tests/regression/test_schema_secondary_index_removal.py": frozenset(_ORIGINAL_DDL),
}


def _index_names(store: DuckDBStore) -> set[str]:
    return {str(row["index_name"]) for row in store.read_query("SELECT index_name FROM duckdb_indexes()")}


def _migration_versions(store: DuckDBStore) -> set[int]:
    return {int(row["version"]) for row in store.read_query("SELECT version FROM schema_migrations")}


def _seed(store: DuckDBStore) -> None:
    with store.transaction() as connection:
        for statement in _SEED_SQL:
            connection.execute(statement)


def _create(store: DuckDBStore, index_name: str) -> None:
    """按 schema 里原来的 DDL 把单个索引建回来（用于「有索引」对照组）。"""
    ddl = _ORIGINAL_DDL[index_name]
    with store.transaction() as connection:
        connection.execute(ddl)


def _rows(store: DuckDBStore, sql: str) -> list[tuple]:
    return [tuple(row.values()) for row in store.read_query(sql)]


# ---------------------------------------------------------------------------
# ② 结果集不变（删索引不改语义）
# ---------------------------------------------------------------------------


def test_ddl_table_matches_dropped_index_list() -> None:
    """对照组 DDL 表必须与判删清单一一对应（清单改了这里没改 = 静默少测一个）。"""
    assert set(_ORIGINAL_DDL) == set(DROPPED_SECONDARY_INDEXES)


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
    assert first["complete"] is True
    assert _index_names(duckdb_store) == set()

    second = drop_useless_secondary_indexes(duckdb_store)
    assert second == {
        "present_before": [], "dropped": [], "failed": {}, "remaining": [], "complete": True,
    }

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
    with_index = {label: _rows(duckdb_store, sql) for _i, label, sql in REPRESENTATIVE_QUERIES}

    drop_useless_secondary_indexes(duckdb_store)
    after = {label: _rows(duckdb_store, sql) for _i, label, sql in REPRESENTATIVE_QUERIES}

    assert with_index == without, "有索引 vs 无索引 结果集不一致"
    assert after == without, "删索引后结果集变了"
    for label in without:
        assert isinstance(without[label], list)


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


# ---------------------------------------------------------------------------
# ① 记账可重试：删不干净 → 不许写 v50（审查缺陷 1）
# ---------------------------------------------------------------------------


def _rewind_to_v49_with_all_indexes(store: DuckDBStore) -> None:
    """复现审查的现场：抹掉 v50 记账 + 把 30 个索引全建回来（= 从 v49 备份恢复）。"""
    with store.transaction() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 50")
    for name in DROPPED_SECONDARY_INDEXES:
        _create(store, name)
    assert _index_names(store) == set(DROPPED_SECONDARY_INDEXES)


def _fail_one_drop(monkeypatch: pytest.MonkeyPatch, index_name: str) -> None:
    """往 DROP 路径里注入一次失败（DROP 前的唯一标识符校验抛错 = 该条 DROP 不执行）。"""
    real = DuckDBStore._assert_simple_identifier

    def flaky(self: DuckDBStore, name: str) -> str:
        if name == index_name:
            raise RuntimeError(f"injected DROP failure for {name}")
        return real(self, name)

    monkeypatch.setattr(DuckDBStore, "_assert_simple_identifier", flaky)


def test_failed_drop_is_not_recorded_and_next_start_retries(
    database_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inject DROP 失败 → **不得**写 v50 记账 → 下一次启动必须重试并补齐。

    审查缺陷 1 的原话是"删失败会在下次启动重试"这句保证是假的：旧代码无条件写
    ``version=50``，而 ``skip_if_current`` 只看 ``MAX(version)``，于是失败之后
    快速启动永远跳过清理（把 30 个索引建回来再跑一次完整启动 → 30 个全在）。
    """
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite)
    assert 50 in _migration_versions(duck)

    _rewind_to_v49_with_all_indexes(duck)
    broken = "idx_source_audit_hash"
    real_assert_identifier = DuckDBStore._assert_simple_identifier
    _fail_one_drop(monkeypatch, broken)

    init_all_schema(duckdb_store=duck, sqlite_store=sqlite, skip_if_current=True)

    assert 50 not in _migration_versions(duck), (
        "DROP 失败却写了 v50 记账 → 快速启动从此永远跳过清理（审查缺陷 1 复发）"
    )
    assert _index_names(duck) == {broken}, "除注入失败的那条外，其余 29 条应该已经删掉"

    # 再启动一次仍然是坏的：记账还是不许写（"没清干净就会再试"）。
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite, skip_if_current=True)
    assert 50 not in _migration_versions(duck)
    assert _index_names(duck) == {broken}

    # 故障消失 → 下一次启动**真的**重试，删掉最后一条并记账。
    monkeypatch.setattr(DuckDBStore, "_assert_simple_identifier", real_assert_identifier)
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite, skip_if_current=True)
    assert _index_names(duck) == set()
    assert 50 in _migration_versions(duck)
    description = duck.read_query(
        "SELECT description FROM schema_migrations WHERE version = 50"
    )[0]["description"]
    assert description == V50_DROP_DESCRIPTION


def test_v50_record_is_revoked_when_cleanup_is_incomplete(duckdb_store: DuckDBStore) -> None:
    """已有 v50 记账 + 清理不完整 → 记账必须被**抹掉**（否则快速启动把自己锁死）。

    场景：库是从"记账已是 50"的状态来的（备份恢复 / 早先跑过旧代码），此时若索引又长出
    一条且这次 DROP 失败，旧代码会把 50 留在表里、永不重试。
    """
    with duckdb_store.transaction() as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (50, 'stale') "
            "ON CONFLICT (version) DO NOTHING"
        )
    assert 50 in _migration_versions(duckdb_store)

    written = _record_v50_drop_migration(
        duckdb_store, {"complete": False, "failed": {"idx_x": "boom"}, "remaining": ["idx_x"]},
    )

    assert written is False
    assert 50 not in _migration_versions(duckdb_store), "清理没完成却留着 v50 记账 → 永不重试"

    # 反过来：真清干净了就必须写（否则每次启动都白跑一遍完整 DDL）。
    written = _record_v50_drop_migration(
        duckdb_store, {"complete": True, "failed": {}, "remaining": []},
    )
    assert written is True
    assert 50 in _migration_versions(duckdb_store)


# ---------------------------------------------------------------------------
# ③ 全仓不许再建（审查缺陷 2：只 grep schema.py 会漏掉 scripts/）
# ---------------------------------------------------------------------------


def _executable_sql_strings(path: Path) -> list[str]:
    """文件里**可执行**的字符串常量（跳过 docstring —— 注释里的 DDL 不会被执行）。"""
    with warnings.catch_warnings():
        # 个别脚本的旧正则字面量会抛 SyntaxWarning（`"\s"`），解析它不该污染测试输出。
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


def _scanned_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if any(part in _SCAN_SKIP_PARTS for part in path.parts):
                continue
            files.append(path)
    return files


def _scanned_sql_sites(roots: tuple[str, ...] = _SCAN_ROOTS) -> list[tuple[str, str]]:
    sites: list[tuple[str, str]] = []
    for root in roots:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if any(part in _SCAN_SKIP_PARTS for part in path.parts):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            sites.extend((rel, sql) for sql in _executable_sql_strings(path))
    return sites


def _scanned_create_index_sites() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel, sql in _scanned_sql_sites():
        for match in _CREATE_INDEX_RE.finditer(sql):
            found.setdefault(rel, set()).add(match.group(1))
    return found


def test_repo_has_no_unauthorized_create_index_ddl() -> None:
    """③ 全仓扫描：``CREATE INDEX`` 只允许出现在白名单站点。

    审查缺陷 2 的教训：旧守卫只 grep ``schema.py``，于是
    ``scripts/repair_share_capital_history_index.py``（还能把 v50 刚删的索引建回来、
    而且用的正是 D2 的 DROP+CREATE 同事务写法）完全在视野之外。
    现在扫 ``app/`` ``scripts/`` ``tests/`` 的全部 ``*.py``：白名单之外多一条就红。
    """
    found = {path: names for path, names in _scanned_create_index_sites().items()}
    expected = {path: set(names) for path, names in _ALLOWED_CREATE_INDEX_SITES.items()}

    unexpected = {
        path: sorted(names - expected.get(path, set())) for path, names in found.items()
    }
    unexpected = {path: names for path, names in unexpected.items() if names}
    missing = {path: sorted(names - found.get(path, set())) for path, names in expected.items()}
    missing = {path: names for path, names in missing.items() if names}

    assert unexpected == {}, f"全仓出现白名单之外的 CREATE INDEX（会复活被判删的索引）: {unexpected}"
    assert missing == {}, f"白名单里的 CREATE INDEX 不见了（清单过期，需同步）: {missing}"

    # 再钉一次最要命的那条：判删的索引名不许在生产代码里被重建。
    production = {
        path: names for path, names in found.items() if not path.startswith("tests/")
    }
    resurrected = sorted(
        name for name in DROPPED_SECONDARY_INDEXES for names in production.values() if name in names
    )
    assert resurrected == [], f"生产代码仍在重建被判删的索引: {resurrected}"


# ---------------------------------------------------------------------------
# ④ 可证伪的形状判据（审查缺陷 3：换掉"tiny fixture 上断言没有 INDEX_SCAN"的假绿）
# ---------------------------------------------------------------------------

#: `<列> = <字面量>` 里的"字面量"：字符串 / 数字 / DATE / TIMESTAMP / 布尔。
#: 刻意**不**匹配 `= ?`（参数）、`= (子查询)`、`= 另一张表的列` —— 那才是本仓真实查询的形状。
_INDEX_USABLE_LITERAL = (
    r"(?:'[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?|DATE\s*'[^']*'|TIMESTAMP\s*'[^']*'|TRUE|FALSE)"
)


def _dropped_index_columns() -> dict[str, tuple[str, tuple[str, ...]]]:
    """索引名 → (表, 索引列…)；从对照组 DDL 解析，保证与本文件建出来的索引同源。"""
    parsed: dict[str, tuple[str, tuple[str, ...]]] = {}
    for name, ddl in _ORIGINAL_DDL.items():
        match = re.search(r"ON\s+(\w+)\s*\(([^)]*)\)", ddl, re.IGNORECASE)
        assert match is not None, f"对照组 DDL 解析失败: {ddl}"
        columns = tuple(part.strip().strip('"') for part in match.group(2).split(","))
        parsed[name] = (match.group(1), columns)
    return parsed


def _equality_literal_offenders(
    sql_sites: list[tuple[str, str]],
    index_columns: dict[str, tuple[str, tuple[str, ...]]] | None = None,
) -> list[str]:
    """在 SQL 文本里找 `被删索引首列 = 字面量` —— 这是唯一让 ART 索引真正划算的形状。"""
    parsed = index_columns if index_columns is not None else _dropped_index_columns()
    offenders: list[str] = []
    for rel, sql in sql_sites:
        for index_name, (table, columns) in parsed.items():
            if not re.search(rf"\b{table}\b", sql, re.IGNORECASE):
                continue
            leading = columns[0]
            # 列名可能被双引号/方括号包起来（`"stock_code" = 1`），两种写法都要抓。
            column = rf'(?:\b{leading}\b|"{leading}"|\[{leading}\])'
            if re.search(rf"{column}\s*=\s*{_INDEX_USABLE_LITERAL}", sql, re.IGNORECASE):
                excerpt = " ".join(sql.split())[:120]
                offenders.append(f"{rel} → {index_name}: {excerpt}")
    return offenders


def test_production_queries_have_no_equality_literal_on_dropped_index_columns() -> None:
    """④ 判据本体：生产代码里**不存在** `被删索引首列 = 字面量` 的真实查询。

    为什么这条能红（而旧的"断言 EXPLAIN 里没有 INDEX_SCAN"永远为真）：旧断言测的是
    **计划文本里的字符串**，而 DuckDB 1.5.5 的普通 ``EXPLAIN`` 对真走索引的查询也不打印
    ``INDEX_SCAN``，所以它在 tiny fixture 上恒为真。这条测的是**仓库源码里的查询形状**：
    只要有人写出一条 ``WHERE stock_code = '002185'``（命中 ``business_breakdown`` 的判删
    索引首列）这类真实查询，这里立刻红 —— 因为那个形状**会**吃到 ART 索引，
    这时"删掉它"就不再是免费的了，必须重新裁定（拿 ``EXPLAIN ANALYZE`` + A/B 复测）。
    """
    offenders = _equality_literal_offenders(_scanned_sql_sites(("app", "scripts")))
    assert offenders == [], (
        "生产代码里出现了 `被删索引首列 = 字面量` 的真实查询（索引可能真的有用，"
        f"需要重新裁定 v50）: {offenders}"
    )


def test_equality_literal_detector_is_not_vacuous() -> None:
    """④ 判据自检：喂进"坏形状"必须红，喂进"本仓真实形状"必须不红。

    没有这条，判据本身可能是空转（正是缺陷 3 的病根：一个永远为真的断言）。
    """
    parsed = {
        "idx_business_breakdown_stock_report": ("business_breakdown", ("stock_code", "report_date")),
        "idx_roce_history_date": ("roce_history", ("report_date",)),
    }

    bad_shapes = [
        "SELECT * FROM business_breakdown WHERE stock_code = '002185'",
        "SELECT * FROM roce_history WHERE report_date = DATE '2024-12-31'",
        'SELECT * FROM business_breakdown WHERE "stock_code" = 1',
    ]
    for sql in bad_shapes:
        offenders = _equality_literal_offenders([("synthetic.py", sql)], parsed)
        assert offenders, f"检测器漏掉了坏形状（判据空转）: {sql}"

    real_shapes = [
        "SELECT * FROM business_breakdown WHERE stock_code = ?",
        "SELECT * FROM business_breakdown WHERE stock_code = (SELECT max(stock_code) FROM x)",
        "SELECT * FROM business_breakdown b JOIN stock_meta m ON b.stock_code = m.stock_code",
        "SELECT * FROM business_breakdown WHERE stock_code IN (SELECT stock_code FROM y)",
        # 表名不同（balance_sheet 不是 balance_sheet_ext）→ 与本索引无关，不许误报。
        "SELECT * FROM balance_sheet WHERE stock_code = '600519'",
    ]
    for sql in real_shapes:
        offenders = _equality_literal_offenders([("synthetic.py", sql)], parsed)
        assert offenders == [], f"检测器误报了本仓真实形状: {sql} → {offenders}"


# ---------------------------------------------------------------------------
# 只读守卫：全仓扫到的 SQL 字符串集合是稳定的（防止扫描器静默扫空）
# ---------------------------------------------------------------------------


def test_repo_scan_actually_reads_production_sql() -> None:
    """扫描器自检：扫到的文件/字符串数量必须像"真读到了代码"，不是空转。"""
    files = _scanned_python_files()
    assert len(files) > 100, f"只扫到 {len(files)} 个 .py 文件，扫描范围被改坏了"
    schema_strings = _executable_sql_strings(SCHEMA_SOURCE)
    assert any("CREATE TABLE IF NOT EXISTS stock_meta" in sql for sql in schema_strings), (
        "扫描器没读到 schema.py 里的 DDL 字符串（扫描逻辑坏了）"
    )
    assert not any("schema 定义与迁移机制" in sql for sql in schema_strings), (
        "扫描器把模块 docstring 当成可执行 SQL 了（会把注释里的 DDL 误判成建索引）"
    )
