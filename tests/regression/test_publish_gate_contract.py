"""事故出处：reports/117 / 2026-11-12。

2026-11-12 契约 §2/§3/§4 回归：门禁分级、单一真相、步骤状态归并。

背景（`.planning/2026-11-12-update-log-review/contracts.md`）：

* P0：``share_capital_violations()`` 是 fail-closed 一票否决，26 只股本缺失股里
  5 只新股越过 90 天豁免线后，全市场 5,580 只快照再没发布过；
* P1：readiness 的 ``has_share_capital`` 与门禁 SQL 各写一份，同一时刻
  "门禁报 5 只、readiness 报 26 只"；
* P0：``{**后一个子步骤, "full_recompute": 前一个}`` 让
  ``refresh_treasury_spreads`` 的 success 掩盖了指标快照的 rejected。

全部用例使用隔离临时库（``duckdb_store`` / ``sqlite_store`` fixture），
不接触 ``data/`` 下的正式库。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.data_quality import (
    GATE_HARD_BLOCK_MIN,
    actionable_missing_count,
    listed_share_capital_gap_codes,
    minimum_data_readiness,
    snapshot_publish_gate,
)
from app.core.indicators.calculator import IndicatorCalculator
from app.core.job_status import aggregate_job_status, merge_step_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.data_status import _actionable_missing_condition
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

# ─── 公共 fixture 工具 ──────────────────────────────────────────────


def _insert_stock(
    store: DuckDBStore,
    code: str,
    *,
    total_shares: int | None = 100,
    circ_shares: int | None = 80,
    listing_date: date | None = date(2020, 1, 1),
) -> None:
    """一只已上市股票；门禁只读股本与上市日，其余列保持 NULL。

    原实现是 ``INSERT ... ON CONFLICT DO UPDATE`` 的 upsert：本文件每个调用点
    对同一 code 只播一次（一个用例一个新库），upsert 分支从未被执行。改用工厂
    后重复播种会当场抛 ``DuplicateSeed``——比"静默覆盖股本"更安全。
    """
    Seed(store).stock(
        code,
        name=code,
        exchange="SZSE",
        is_listed=True,
        listing_date=listing_date,
        total_shares=total_shares,
        circ_shares=circ_shares,
    )


def _insert_bulk_valid_stocks(store: DuckDBStore, prefix: str, count: int) -> list[str]:
    """Insert ``count`` listed stocks that honour the share-capital floor."""
    codes = [f"{prefix}{index:05d}" for index in range(count)]
    # 刻意保留内联批量写：这是 5,000 行 × 一条 executemany 的规模构造，
    # 且要求在 write_connection() 事务内一次落盘；工厂是「一行一事务」的
    # 按域接口，逐只播种会让「少数违规不阻断全市场」这条门禁用例跑不动。
    with store.write_connection() as connection:
        # 留（第 4 类·多站点缺口）：这批是 5,000+ 只的**程序化生成**批量行，且已在调用方
        connection.executemany(
            """INSERT INTO stock_meta
                   (stock_code, name, exchange, is_listed, listing_date, total_shares, circ_shares)
               VALUES (?, ?, 'SZSE', TRUE, DATE '2020-01-01', 100, 80)""",
            [(code, code) for code in codes],
        )
    return codes


def _insert_publish_sources(store: DuckDBStore, code: str) -> None:
    """Insert the core inputs compute_snapshot_for_all requires for one stock."""
    seed = Seed(store)
    seed.balance(
        code, "2025-03-31", total_assets=100.0, total_liabilities=20.0, total_equity=80.0
    )
    seed.income(code, "2025-03-31", revenue=100.0, parent_net_profit=10.0)
    seed.cash_flow(code, "2025-03-31", cf_from_operating=1.0)
    # 原始/前复权是同一逻辑对两张表各跑一遍：只换表名，行内容逐字相同 ——
    # 就绪判据要求两张表**各自**存在，缺任一张都不许发布。
    for table in ("price_daily_raw", "price_daily_qfq"):
        seed.prices(code, [("2025-03-31", 10.0)], table=table)


# ─── 门禁分级（契约 §3）─────────────────────────────────────────────


def test_gate_quarantines_a_handful_of_gaps_instead_of_blocking_the_universe(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """5 只违规 + 5,000 只正常 → 门禁 must publish the healthy 5,000。

    这就是 2026-11-03~11-11 全市场停更的复现：少数股本缺口票不允许再
    以 ``ready=False`` 一票否决整个市场。
    """
    valid_codes = _insert_bulk_valid_stocks(duckdb_store, "00000", 5_000)
    broken_codes = ["001232", "301707", "301717", "603468", "688828"]
    for code in broken_codes:
        _insert_stock(duckdb_store, code, total_shares=None, circ_shares=None)

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["ready"] is True
    assert gate["hard_block"] is False
    assert gate["reason"] is None
    assert gate["quarantined_codes"] == broken_codes
    assert gate["violations"] == broken_codes
    assert gate["violation_count"] == 5
    assert gate["listed_count"] == len(valid_codes) + len(broken_codes)
    assert gate["threshold"] == max(GATE_HARD_BLOCK_MIN, int(gate["listed_count"] * 0.01))
    assert not set(gate["quarantined_codes"]) & set(valid_codes)


def test_gate_hard_blocks_when_violations_exceed_the_threshold(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """违规数超过阈值 → 怀疑系统性单位错乱，整批拒绝（hard_block）。"""
    for index in range(100):
        _insert_stock(
            duckdb_store, f"6000{index:02d}", total_shares=None, circ_shares=None,
        )

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["listed_count"] == 100
    assert gate["threshold"] == GATE_HARD_BLOCK_MIN
    assert gate["violation_count"] == 100
    assert gate["violation_count"] > gate["threshold"]
    assert gate["hard_block"] is True
    assert gate["ready"] is False
    assert gate["reason"] == "share_capital_integrity"
    assert gate["violations"] == gate["quarantined_codes"][:20]
    assert len(gate["violations"]) == 20


def test_gate_threshold_is_inclusive_not_strict(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """``violation_count == threshold`` 不算 hard_block（严格大于才阻断）。"""
    _insert_bulk_valid_stocks(duckdb_store, "00001", 4_950)
    for index in range(GATE_HARD_BLOCK_MIN):
        _insert_stock(
            duckdb_store, f"300{index:03d}", total_shares=None, circ_shares=None,
        )

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["listed_count"] == 5_000
    assert gate["threshold"] == GATE_HARD_BLOCK_MIN
    assert gate["violation_count"] == GATE_HARD_BLOCK_MIN
    assert gate["hard_block"] is False
    assert gate["ready"] is True
    assert len(gate["quarantined_codes"]) == GATE_HARD_BLOCK_MIN


def test_gate_fails_closed_when_the_check_itself_is_unavailable(
    sqlite_store: SQLiteStore,
) -> None:
    """检查抛异常 → ready=False + hard_block=True（绝不 fail-open）。"""

    class _ExplodingDuck:
        db_path = ":memory:"

        def read_query(self, sql, params=None):  # noqa: ANN001, ARG002
            raise RuntimeError("duck went away")

    gate = snapshot_publish_gate(_ExplodingDuck(), sqlite_store)

    assert gate["ready"] is False
    assert gate["hard_block"] is True
    assert gate["reason"] == "share_capital_check_unavailable"
    assert gate["quarantined_codes"] == []
    assert gate["violation_count"] == 0
    assert "duck went away" in gate["error"]


def test_listed_share_capital_gap_codes_never_raises_for_auxiliary_callers() -> None:
    """T2 的每日定向补抓复用它：查询失败返回 [] + warning，不抛裸异常。"""

    class _ExplodingDuck:
        db_path = ":memory:"

        def read_query(self, sql, params=None):  # noqa: ANN001, ARG002
            raise RuntimeError("transient read error")

    assert listed_share_capital_gap_codes(_ExplodingDuck()) == []


# ─── 单一真相：readiness 与门禁同源（契约 §3）────────────────────────


def test_readiness_share_capital_set_is_identical_to_the_gate_violation_set(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """同一 fixture 下，readiness 的 share_capital 缺口 == 门禁违规集合。

    此前两处各写一份 SQL：门禁报 5 只、readiness 报 26 只。
    """
    _insert_stock(duckdb_store, "000001")  # 合法
    _insert_stock(duckdb_store, "000002", total_shares=None, circ_shares=None)  # 缺失
    _insert_stock(duckdb_store, "000003", total_shares=100, circ_shares=200)  # circ > total
    _insert_stock(
        duckdb_store, "301999", total_shares=None, circ_shares=None,
        listing_date=date.today(),  # 90 天新股豁免
    )
    _insert_stock(
        duckdb_store, "301998", total_shares=None, circ_shares=None,
        listing_date=date.today() - timedelta(days=120),  # 已越过豁免线
    )

    expected = ["000002", "000003", "301998"]
    gate = snapshot_publish_gate(duckdb_store, sqlite_store)
    readiness = minimum_data_readiness(duckdb_store, sqlite_store)

    assert gate["quarantined_codes"] == expected
    assert listed_share_capital_gap_codes(duckdb_store) == expected
    assert readiness["missing"]["share_capital"] == expected
    assert readiness["missing_counts"]["share_capital"] == len(expected)


def test_share_capital_gap_exempts_only_recent_listings_missing_both_columns(
    duckdb_store: DuckDBStore,
) -> None:
    """豁免只覆盖「上市 ≤90 天且两者皆 NULL」；只缺一列仍然违规。"""
    _insert_stock(
        duckdb_store, "301901", total_shares=None, circ_shares=None,
        listing_date=date.today() - timedelta(days=30),
    )
    _insert_stock(
        duckdb_store, "301902", total_shares=100, circ_shares=None,
        listing_date=date.today() - timedelta(days=30),
    )
    _insert_stock(
        duckdb_store, "301903", total_shares=None, circ_shares=None,
        listing_date=date.today() - timedelta(days=91),
    )

    assert listed_share_capital_gap_codes(duckdb_store) == ["301902", "301903"]


# ─── 发布：逐股隔离（契约 §4）──────────────────────────────────────


def test_publish_quarantines_broken_stock_and_keeps_its_previous_row(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存在隔离股时：其余股票照常发布，隔离股的旧快照行必须原样保留。"""
    # 2026-11-13：门禁增加「≥1/3 上市股违规 = 系统性」的占比判据，因此逐股隔离
    # 路径必须用「占比低于 1/3」的库来测：4 只库里 1 只违规（25% < 1/3）。
    for code in ("000001", "000003", "000004"):
        _insert_stock(duckdb_store, code)
        _insert_publish_sources(duckdb_store, code)
    _insert_stock(duckdb_store, "000002", total_shares=100, circ_shares=200)
    _insert_publish_sources(duckdb_store, "000002")
    # 隔离股上一代已发布行（旧报告期 + 旧价格），发布后必须还在。
    Seed(duckdb_store).snapshot("000002", "2024-12-31", latest_close=10.0)
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all()

    assert report["status"] == "partial"
    assert report["reason"] == "publish_gate_quarantined"
    assert report["quarantined_codes"] == ["000002"]
    assert report["quarantined_count"] == 1
    assert report["success"] == 3
    assert report["failed"] == 0
    rows = duckdb_store.read_query(
        "SELECT stock_code, report_date, latest_close FROM indicator_snapshot ORDER BY stock_code"
    )
    assert rows == [
        {"stock_code": "000001", "report_date": date(2025, 3, 31), "latest_close": 20.0},
        {"stock_code": "000002", "report_date": date(2024, 12, 31), "latest_close": 10.0},
        {"stock_code": "000003", "report_date": date(2025, 3, 31), "latest_close": 20.0},
        {"stock_code": "000004", "report_date": date(2025, 3, 31), "latest_close": 20.0},
    ]


def test_publish_rejects_the_whole_batch_only_on_hard_block(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hard_block 时一条不写，上一代快照完整保留。"""
    _insert_stock(duckdb_store, "000001")
    _insert_publish_sources(duckdb_store, "000001")
    Seed(duckdb_store).snapshot("OLD001", "2024-12-31", latest_close=10.0)
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all(
        publish_gate=lambda duck, sqlite: {
            "ready": False, "hard_block": True, "reason": "share_capital_integrity",
            "quarantined_codes": [], "violations": [], "violation_count": 999,
        }
    )

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["success"] == 0
    assert duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001"}]


def test_incremental_publish_reports_quarantine_without_deleting_old_row(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """增量路径同样逐股隔离：隔离股不重算、旧行不被删除。"""
    # 000001 是本次要重算的已发布行（旧价 10）；000002 的 2024-12-31 旧行
    # 在增量路径里必须原样躺着，不许被隔离逻辑删掉或改写。
    seed = Seed(duckdb_store)
    seed.snapshot("000001", "2025-03-31", latest_close=10)
    seed.snapshot("000002", "2024-12-31", latest_close=20)
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 15},
    )

    report = calculator.compute_snapshot_for_codes(
        ["000001", "000002"],
        publish_gate=lambda duck, sqlite: {
            "ready": True, "hard_block": False, "quarantined_codes": ["000002"],
        },
    )

    assert report["status"] == "partial"
    assert report["reason"] == "publish_gate_quarantined"
    assert report["quarantined_codes"] == ["000002"]
    assert report["quarantined_count"] == 1
    assert report["success"] == 1
    assert duckdb_store.read_query(
        "SELECT stock_code, latest_close FROM indicator_snapshot ORDER BY stock_code"
    ) == [
        {"stock_code": "000001", "latest_close": 15.0},
        {"stock_code": "000002", "latest_close": 20.0},
    ]


# ─── 步骤状态归并（契约 §2）────────────────────────────────────────


def test_merge_step_status_never_lets_success_mask_a_rejection() -> None:
    """update.py 的旧写法让 refresh_treasury_spreads 的成功掩盖了 rejected。"""
    merged = merge_step_status(
        {"status": "success", "reason": None, "full_recompute": {"status": "partial"}},
        {"status": "rejected", "reason": "publish_gate_failed"},
        {"status": "success", "reason": None},
        reason="indicators",
    )

    assert merged["status"] == "rejected"
    assert merged["reason"] == "publish_gate_failed"
    assert len(merged["sub_steps"]) == 3


def test_merge_step_status_rejection_cannot_be_overwritten_by_a_later_step() -> None:
    merged = merge_step_status(
        {"status": "rejected", "reason": "publish_gate_failed"},
        {"status": "success", "reason": "treasury_spreads_refreshed"},
    )

    assert merged["status"] == "rejected"
    assert merged["reason"] == "publish_gate_failed"


def test_merge_step_status_returns_skipped_when_every_step_was_skipped() -> None:
    merged = merge_step_status(
        {"status": "skipped", "reason": "no_changed_stocks"},
        {"status": "skipped"},
        None,
        reason="indicators",
    )

    assert merged["status"] == "skipped"
    assert merged["reason"] == "indicators"
    assert merged["sub_steps"] == []


@pytest.mark.parametrize(
    ("step_statuses", "expected"),
    [
        (["success", "ok"], "success"),
        (["success", "partial"], "partial"),
        (["partial", "failed"], "failed"),
        (["failed", "error"], "failed"),
        (["error", "rejected"], "rejected"),
        (["success", "skipped"], "success"),
        (["skipped"], "skipped"),
    ],
)
def test_merge_step_status_severity_ladder(
    step_statuses: list[str], expected: str,
) -> None:
    merged = merge_step_status(*[{"status": status} for status in step_statuses])

    assert merged["status"] == expected


def test_merge_step_status_passes_extra_fields_through() -> None:
    merged = merge_step_status(
        {"status": "partial", "reason": "changed_stock_not_ready"},
        full_recompute={"status": "success"},
        quarantined_count=2,
    )

    assert merged["status"] == "partial"
    assert merged["reason"] == "changed_stock_not_ready"
    assert merged["full_recompute"] == {"status": "success"}
    assert merged["quarantined_count"] == 2


def test_aggregate_job_status_keeps_its_original_semantics() -> None:
    """老接口不动：其它模块仍在用它。"""
    assert aggregate_job_status({"a": {"status": "success"}, "b": {"status": "ok"}}) == "success"
    assert aggregate_job_status({"a": {"status": "failed"}, "b": {"status": "error"}}) == "failed"
    assert aggregate_job_status({"a": {"status": "success"}, "b": {"status": "failed"}}) == "partial"
    assert aggregate_job_status({"a": {"status": "skipped"}}) == "success"


# ─── missing 口径单一真相（契约 §3）──────────────────────────────────


def test_actionable_missing_count_matches_the_shared_condition(
    sqlite_store: SQLiteStore,
) -> None:
    """CLI/状态页/update.py 必须报同一个数字，且不含已解决行。"""
    # 原来卡在缺口 B（`missing_list` 没有 SQLite 侧播种方法，跨库不能借 Seed）。
    # 现在：未解决行给 `resolved_at=None`，已解决行逐字给一个历史时刻 ——
    # "已解决的不许计入"就是这两类行的区别。
    seed = SeedSqlite(sqlite_store)
    for stock_code, field_name, reason_code, resolved_at in [
        ("000001", "price.close", "source_incomplete", None),
        ("000002", "balance.capital_adequacy_ratio", "source_unsupported", None),
        ("000003", "price.close", "source_incomplete", "2026-01-01 00:00:00"),
        ("830001", "placement_funding", "source_unsupported", None),
        ("600001", "placement_funding", "source_unsupported", None),
    ]:
        seed.missing_list(stock_code, field_name, reason_code, resolved_at=resolved_at)

    condition, disabled = _actionable_missing_condition()
    expected = sqlite_store.query(
        f"SELECT COUNT(*) AS cnt FROM missing_list "
        f"WHERE resolved_at IS NULL AND {condition}",
        disabled,
    )[0]["cnt"]

    assert expected == 2
    assert actionable_missing_count(sqlite_store) == expected


def test_actionable_missing_count_excludes_every_resolved_row(
    sqlite_store: SQLiteStore,
) -> None:
    # 同上（缺口 B）：两行都**已解决**，只是解决时刻不同。
    seed = SeedSqlite(sqlite_store)
    seed.missing_list("000001", "price.close", "source_incomplete", resolved_at="2026-01-01 00:00:00")
    seed.missing_list("000002", "price.close", "source_incomplete", resolved_at="2026-02-01 00:00:00")

    assert actionable_missing_count(sqlite_store) == 0
