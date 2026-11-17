"""快照发布的"派生列不得被清空"不变量（`reports/133` R3，事故来源 K5/D28）。

`indicator_snapshot` 的两条发布路径都是 DELETE + INSERT：

* 整表发布 `_publish_snapshot()`：`INSERT ... BY NAME SELECT * FROM staging`；
* 增量发布 `_publish_snapshot_delta()`：只写记录里出现过的列。

凡是**不由指标计算器生产**的列（当前是 ROCE 六列，以后还可能有别的外部派生列）
在这两种写法下都会被静默写成 NULL。历史事故：

* `reports/131` K5：ROCE 落地当晚，每次快照重发把全市场 5,575 行 `roce_*` 清空；
* D28：同一类"派生域没有重建触发"。
* 而 `reports/131 §9` 的补丁只补了整表路径——**增量路径仍在清空**，
  本文件第 1 条用例正是钉死这一点。

固化成两条不变量（不再依赖"记得加补丁"）：
1. 发布前后，派生列的非空行数不得从 > 0 掉到 0；
2. 无法沿 `(stock_code, report_date)` 保全时必须**抛错回滚**，而不是静默写 NULL。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.indicators.calculator import IndicatorCalculator, derived_snapshot_domains
from app.core.indicators.roce import ELIGIBLE_MIN_YEARS
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

PROBE_COLUMN = "derived_probe"  # 模拟"不由计算器生产"的外部派生列


@pytest.fixture
def calculator(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> IndicatorCalculator:
    return IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)


def _add_probe_column(duck: DuckDBStore) -> None:
    duck.write_query(
        f"ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS {PROBE_COLUMN} DOUBLE"
    )


def _seed_snapshot(
    duck: DuckDBStore, code: str, *, report_date: date, probe: float | None = None,
    roce_median: float | None = None,
) -> None:
    duck.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES (?, ?, 'SZSE', true)",
        [code, code],
    )
    duck.write_query(
        f"""INSERT INTO indicator_snapshot
                (stock_code, report_date, calculated_at, data_version, {PROBE_COLUMN},
                 roce_5y_median, roce_5y_years)
            VALUES (?, CAST(? AS DATE), now(), 'fixture', ?, ?, ?)""",
        [code, report_date.isoformat(), probe, roce_median, 5 if roce_median else None],
    )


def _record(code: str, report_date: date, **extra: object) -> dict[str, object]:
    """模拟计算器产出的一行：只写它自己负责的列。"""
    row: dict[str, object] = {
        "stock_code": code,
        "report_date": report_date,
        "calculated_at": datetime.now(UTC),
        "data_version": "fixture",
        "pe_ttm": 12.5,
    }
    row.update(extra)
    return row


def _probe(duck: DuckDBStore, code: str) -> float | None:
    rows = duck.read_query(
        f"SELECT {PROBE_COLUMN} FROM indicator_snapshot WHERE stock_code = ?", [code]
    )
    return rows[0][PROBE_COLUMN]


# ─── 1. 增量发布路径也必须保全（reports/131 §9 补丁漏掉的路径）───────────


def test_delta_publish_preserves_externally_written_columns(
    duckdb_store: DuckDBStore, calculator: IndicatorCalculator,
) -> None:
    _add_probe_column(duckdb_store)
    report_date = date(2025, 12, 31)
    _seed_snapshot(duckdb_store, "000001", report_date=report_date, probe=7.5)

    calculator._publish_snapshot_delta(["000001"], [_record("000001", report_date)])

    assert _probe(duckdb_store, "000001") == pytest.approx(7.5)


# ─── 2. 整表发布路径同样保全 ─────────────────────────────────────────────


def test_full_publish_preserves_externally_written_columns(
    duckdb_store: DuckDBStore, calculator: IndicatorCalculator,
) -> None:
    _add_probe_column(duckdb_store)
    report_date = date(2025, 12, 31)
    _seed_snapshot(duckdb_store, "000002", report_date=report_date, probe=3.25)

    staging = "indicator_snapshot_staging_test"
    duckdb_store.write_query(
        f"""CREATE TABLE "{staging}" AS
            SELECT stock_code, report_date, calculated_at, data_version, pe_ttm
            FROM indicator_snapshot WHERE FALSE"""
    )
    duckdb_store.write_query(
        f"""INSERT INTO "{staging}"
                (stock_code, report_date, calculated_at, data_version, pe_ttm)
            VALUES ('000002', CAST('2025-12-31' AS DATE), now(), 'fixture', 9.9)"""
    )
    try:
        calculator._publish_snapshot(staging, expected_count=1, records=[
            _record("000002", report_date),
        ])
    finally:
        duckdb_store.write_query(f'DROP TABLE IF EXISTS "{staging}"')

    assert _probe(duckdb_store, "000002") == pytest.approx(3.25)


# ─── 3. 保不住的时候必须抛错回滚，不能静默写 NULL ────────────────────────


def test_publish_refuses_to_wipe_unpreservable_derived_column(
    duckdb_store: DuckDBStore, calculator: IndicatorCalculator,
) -> None:
    _add_probe_column(duckdb_store)
    old_date = date(2025, 12, 31)
    new_date = date(2026, 9, 30)
    _seed_snapshot(duckdb_store, "000003", report_date=old_date, probe=11.0)

    # 报告期前移 → (stock_code, report_date) 键对不上，没法沿旧键保全
    with pytest.raises(RuntimeError, match="wipe derived columns"):
        calculator._publish_snapshot_delta(["000003"], [_record("000003", new_date)])

    # 事务已回滚：上一代行原样保留，不是"发布了一半"
    rows = duckdb_store.read_query(
        "SELECT report_date, pe_ttm FROM indicator_snapshot WHERE stock_code = '000003'"
    )
    assert len(rows) == 1
    assert str(rows[0]["report_date"])[:10] == old_date.isoformat()
    assert rows[0]["pe_ttm"] is None


def test_publish_allows_legendary_empty_derived_column(
    duckdb_store: DuckDBStore, calculator: IndicatorCalculator,
) -> None:
    """派生列本来就全空（发布前无值）→ 不触发"被清空"告警。"""
    _add_probe_column(duckdb_store)
    report_date = date(2025, 12, 31)
    _seed_snapshot(duckdb_store, "000004", report_date=report_date, probe=None)

    calculator._publish_snapshot_delta(["000004"], [_record("000004", report_date)])

    assert _probe(duckdb_store, "000004") is None


# ─── 4. ROCE 钩子：增量发布后派生值仍然新鲜且被保全 ──────────────────────


def _seed_roce_history(duck: DuckDBStore, code: str, values: list[float]) -> None:
    for index, value in enumerate(values):
        year = 2016 + index
        duck.write_query(
            """INSERT INTO roce_history
                   (stock_code, report_date, ce, ce_prev, ebit, roce, status,
                    calculated_at, source, data_version)
               VALUES (?, CAST(? AS DATE), 1000, 1000, 200, ?, 'OK', now(),
                       'fixture', 1)""",
            [code, f"{year}-12-31", value],
        )


def test_delta_publish_refreshes_registered_roce_domain(
    duckdb_store: DuckDBStore, calculator: IndicatorCalculator,
) -> None:
    _add_probe_column(duckdb_store)
    code = "000005"
    report_date = date(2025, 12, 31)
    _seed_snapshot(duckdb_store, code, report_date=report_date, probe=1.0, roce_median=0.9)
    _seed_roce_history(duckdb_store, code, [0.10, 0.12, 0.14, 0.16, 0.18])

    calculator._publish_snapshot_delta([code], [_record(code, report_date)])

    row = duckdb_store.read_query(
        "SELECT roce_5y_median, roce_5y_years FROM indicator_snapshot WHERE stock_code = ?",
        [code],
    )[0]
    # 钩子重算覆盖了旧的 0.9，且年数按真实历史给
    assert row["roce_5y_median"] == pytest.approx(0.14)
    assert row["roce_5y_years"] == 5
    assert ELIGIBLE_MIN_YEARS > 5  # 5 年历史不足以进入 roce_eligible 池
    assert _probe(duckdb_store, code) == pytest.approx(1.0)


def test_registered_domains_declare_existing_columns(duckdb_store: DuckDBStore) -> None:
    columns = {
        str(row["column_name"])
        for row in duckdb_store.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'indicator_snapshot'"
        )
    }
    for domain in derived_snapshot_domains():
        assert set(domain.columns) <= columns, domain.name


# ─── 2b. 生产形态的 staging（全列）也必须保全（reports/135 回归）─────────
#
# 上面第 2 条用例用 5 列 staging，而**生产路径的 staging 是全 70 列**
# （`CREATE TABLE staging AS SELECT * FROM indicator_snapshot WHERE FALSE`）。
# 旧实现把 "staging 的列集合" 当成 `owned_columns` → `untouched = 全列 - 全列 = 空`
# → 派生列保全与"被清空就回滚"两段**都空转**。生产库里 ROCE 只靠
# `roce_history` 覆盖 100% 侥幸活着。本用例用生产形态 staging 钉死这条路径。


def test_full_publish_with_production_shaped_staging_preserves_derived_columns(
    duckdb_store: DuckDBStore, calculator: IndicatorCalculator,
) -> None:
    """staging 含**全部**列时，派生列仍必须被保全。

    这是 `reports/135` 复现的形态：审计用全列 staging 跑真实 `_publish_snapshot`，
    发现 probe 列被清成 NULL 且**不报错**（而 5 列 staging 的用例是绿的）。
    """
    _add_probe_column(duckdb_store)
    code = "000006"
    report_date = date(2025, 12, 31)
    _seed_snapshot(duckdb_store, code, report_date=report_date, probe=3.25)

    staging = "indicator_snapshot_staging_fullshape_test"
    # 生产形态：staging 复制 indicator_snapshot 的**全列**
    duckdb_store.write_query(
        f"""CREATE TABLE "{staging}" AS
            SELECT * FROM indicator_snapshot WHERE FALSE"""
    )
    # 只按计算器真正负责的列填值（与生产 staging 被 `_insert_records_in_connection`
    # 按同样方式填充一致）
    duckdb_store.write_query(
        f"""INSERT INTO "{staging}" BY NAME
            SELECT stock_code, report_date, calculated_at, data_version, pe_ttm
            FROM (SELECT ? AS stock_code, CAST(? AS DATE) AS report_date,
                         now() AS calculated_at, 'fixture' AS data_version,
                         9.9 AS pe_ttm)""",
        [code, report_date.isoformat()],
    )
    try:
        calculator._publish_snapshot(staging, expected_count=1, records=[
            _record(code, report_date),
        ])
    finally:
        duckdb_store.write_query(f'DROP TABLE IF EXISTS "{staging}"')

    assert _probe(duckdb_store, code) == pytest.approx(3.25), (
        "生产形态 staging（全列）下派生列被清空了 —— 说明 owned_columns 又退回到"
        "'staging 的列集合'，守卫重新空转"
    )
