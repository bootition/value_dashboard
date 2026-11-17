"""事故出处：reports/118 / 2026-11-13。

2026-11-13 P1：小库/退化库上的股本门禁必须能识别"整库被隔离"。

缺陷形态：``threshold = max(50, int(listed * 1%))`` 在上市股 <101 只时恒为 50，
于是 30 只的小库即使 30 只全违规，门禁也只判"逐股隔离"（``ready=True``），
发布集合变成空集 —— 与"永不出新快照"的事故形态等价，却只报 ``partial``。

修复（判据仍单一实现，见 ``share_capital_hard_block``）：

    hard_block = violations > max(50, int(listed * 1%))   # 系统性规模
              OR violations >= listed / 3                 # 占比兜底（任意规模）

注意整数实现：用 ``violations * 3 >= listed`` 表达"≥1/3"。若按字面
``violations >= int(listed / 3)``，20 只库里 6 只（30%）会被误判成系统性
（``int(20/3) == 6``），与"≥1/3"以及验收用例 ② 都冲突。

以及发布侧配套：发布集合被隔离光时返回 ``rejected`` +
``publish_gate_quarantined_all``，不再用 ``partial`` 掩盖。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.data_quality import (
    GATE_HARD_BLOCK_MIN,
    gate_hard_block_threshold,
    gate_hard_blocks_by_share,
    minimum_data_readiness,
    share_capital_hard_block,
    snapshot_publish_gate,
)
from app.core.indicators.calculator import IndicatorCalculator
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def _seed_listed(store: DuckDBStore, total: int, broken: int) -> list[str]:
    """Insert ``total`` listed stocks, of which ``broken`` breach circ<=total."""
    codes = [f"{index:06d}" for index in range(total)]
    with store.write_connection() as connection:
        connection.executemany(
            """INSERT INTO stock_meta
                   (stock_code, name, exchange, is_listed, listing_date,
                    total_shares, circ_shares)
               VALUES (?, ?, 'SZSE', TRUE, DATE '2020-01-01', 100, 80)""",
            [(code, code) for code in codes],
        )
        if broken:
            connection.execute(
                "UPDATE stock_meta SET circ_shares = 200 "
                f"WHERE stock_code IN ({', '.join('?' for _ in range(broken))})",
                codes[:broken],
            )
    return codes


def _seed_publish_sources(store: DuckDBStore, codes: list[str]) -> None:
    with store.write_connection() as connection:
        for code in codes:
            connection.execute(
                """INSERT INTO balance_sheet
                       (stock_code, report_date, total_assets, total_liabilities, total_equity)
                   VALUES (?, '2025-03-31', 100.0, 20.0, 80.0)""",
                [code],
            )
            connection.execute(
                """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
                   VALUES (?, '2025-03-31', 100.0, 10.0)""",
                [code],
            )
            connection.execute(
                """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
                   VALUES (?, '2025-03-31', 1.0)""",
                [code],
            )
            connection.execute(
                """INSERT INTO price_daily_raw (stock_code, trade_date, close)
                   VALUES (?, '2025-03-31', 10.0)""",
                [code],
            )
            connection.execute(
                """INSERT INTO price_daily_qfq (stock_code, trade_date, close)
                   VALUES (?, '2025-03-31', 10.0)""",
                [code],
            )


# ─── 纯判据（唯一实现）──────────────────────────────────────────────


def test_share_ratio_rule_is_exactly_one_third_inclusive() -> None:
    """≥1/3 用整数比较实现：7/20 触发，6/20 不触发，1/3 恰好触发。"""
    assert gate_hard_blocks_by_share(7, 20) is True
    assert gate_hard_blocks_by_share(6, 20) is False
    assert gate_hard_blocks_by_share(1, 3) is True    # 恰好 1/3 → 触发
    assert gate_hard_blocks_by_share(2, 4) is True    # 50%
    assert gate_hard_blocks_by_share(164, 500) is False  # 32.8% < 1/3
    assert gate_hard_blocks_by_share(167, 500) is True   # 33.4% ≥ 1/3


def test_hard_block_helper_combines_absolute_threshold_and_share_rule() -> None:
    # 绝对阈值：5000 只 → 阈值 50；51 只触发，50 只不触发
    assert gate_hard_block_threshold(5_000) == GATE_HARD_BLOCK_MIN
    assert share_capital_hard_block(51, 5_000) is True
    assert share_capital_hard_block(50, 5_000) is False
    # 占比兜底：小库 20 只 → 绝对阈值 50 永不触发，靠 1/3 识别
    assert share_capital_hard_block(6, 20) is False
    assert share_capital_hard_block(7, 20) is True
    # 空库/零违规不触发
    assert share_capital_hard_block(0, 0) is False
    assert share_capital_hard_block(0, 30) is False


# ─── 门禁行为 ──────────────────────────────────────────────────────


def test_small_library_above_one_third_hard_blocks(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """①20 只库里 7 只违规（≥1/3）→ hard_block=True（哪怕绝对阈值 50 未达）。"""
    _seed_listed(duckdb_store, total=20, broken=7)

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["listed_count"] == 20
    assert gate["threshold"] == GATE_HARD_BLOCK_MIN
    assert gate["violation_count"] == 7
    assert gate["violation_count"] <= gate["threshold"]
    assert gate["hard_block"] is True
    assert gate["hard_block_rule"] == "share"
    assert gate["ready"] is False
    assert gate["reason"] == "share_capital_integrity"


def test_small_library_below_one_third_quarantines_per_stock(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """②20 只库里 6 只违规（30% < 1/3）→ 不 hard_block，逐股隔离。"""
    _seed_listed(duckdb_store, total=20, broken=6)

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["hard_block"] is False
    assert gate["hard_block_rule"] is None
    assert gate["ready"] is True
    assert gate["quarantined_codes"] == [f"{index:06d}" for index in range(6)]


def test_large_library_still_uses_the_absolute_threshold(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """③5,000 只里 60 只违规 → 绝对阈值 50 命中（原有语义不变）。"""
    _seed_listed(duckdb_store, total=5_000, broken=60)

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["listed_count"] == 5_000
    assert gate["threshold"] == 50
    assert gate["violation_count"] == 60
    # 占比判据未命中（60*3=180 < 5000）→ 命中的是绝对阈值规则
    assert gate_hard_blocks_by_share(60, 5_000) is False
    assert gate["hard_block"] is True
    assert gate["hard_block_rule"] == "threshold"


def test_exactly_one_third_of_the_library_hard_blocks(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """边界：3 只库、1 只违规 = 恰好 1/3 → 判为系统性（≥ 含等号）。"""
    _seed_listed(duckdb_store, total=3, broken=1)

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["hard_block"] is True
    assert gate["hard_block_rule"] == "share"


def test_empty_library_is_not_hard_blocked(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """空库既无违规也无分母 → 不得被占比判据误判成硬阻断。"""
    gate = snapshot_publish_gate(duckdb_store, sqlite_store)

    assert gate["listed_count"] == 0
    assert gate["violation_count"] == 0
    assert gate["hard_block"] is False
    assert gate["ready"] is True


# ─── 发布侧：整库被隔离必须是 rejected ─────────────────────────────


def test_publish_rejects_instead_of_partial_when_every_stock_is_quarantined(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """④发布集合被隔离光 → status=rejected + publish_gate_quarantined_all。

    用注入门禁构造（真门禁在这个规模会先命中 1/3 规则走 hard_block 分支），
    验证的是"隔离光"这条路径本身不再用 partial 掩盖。
    """
    codes = _seed_listed(duckdb_store, total=2, broken=0)
    _seed_publish_sources(duckdb_store, codes)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
           VALUES ('OLD001', '2024-12-31', 10.0)"""
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all(
        publish_gate=lambda duck, sqlite: {
            "ready": True,
            "hard_block": False,
            "quarantined_codes": list(codes),
        }
    )

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_quarantined_all"
    assert report["quarantined_count"] == len(codes)
    assert report["quarantined_codes"] == codes
    assert report["success"] == 0
    # 一条不写：上一代快照完整保留
    assert duckdb_store.read_query(
        "SELECT stock_code FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001"}]


def test_real_gate_hard_blocks_a_fully_broken_small_library(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真门禁路径：小库 100% 违规 → 1/3 规则命中 → 整批 rejected（保留旧行）。"""
    _seed_listed(duckdb_store, total=1, broken=1)
    _seed_publish_sources(duckdb_store, ["000000"])
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot (stock_code, report_date, latest_close)
           VALUES ('OLD001', '2024-12-31', 10.0)"""
    )
    calculator = IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)
    monkeypatch.setattr(
        calculator,
        "compute_all_for_stock",
        lambda code: {"stock_code": code, "report_date": "2025-03-31", "latest_close": 20.0},
    )

    report = calculator.compute_snapshot_for_all()

    assert report["status"] == "rejected"
    assert report["reason"] == "publish_gate_failed"
    assert report["gate"]["hard_block"] is True
    assert report["gate"]["hard_block_rule"] == "share"
    assert duckdb_store.read_query(
        "SELECT stock_code, report_date FROM indicator_snapshot ORDER BY stock_code"
    ) == [{"stock_code": "OLD001", "report_date": date(2024, 12, 31)}]


# ─── readiness 与门禁同源（新判据下仍对齐）──────────────────────────


@pytest.mark.parametrize(("total", "broken", "expected_blocking"), [
    (10, 2, False),   # 20% < 1/3 且未达绝对阈值 → 逐股隔离
    (10, 4, True),    # 40% ≥ 1/3 → 系统性
])
def test_readiness_share_capital_policy_matches_the_publish_gate(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    total: int, broken: int, expected_blocking: bool,
) -> None:
    """⑤同一 fixture 下 readiness.blocking 必须等于 gate.hard_block。"""
    _seed_listed(duckdb_store, total=total, broken=broken)

    gate = snapshot_publish_gate(duckdb_store, sqlite_store)
    readiness = minimum_data_readiness(duckdb_store, sqlite_store)
    policy = readiness["share_capital_policy"]

    assert gate["hard_block"] is policy["blocking"] is expected_blocking
    assert gate["threshold"] == policy["threshold"] == GATE_HARD_BLOCK_MIN
    assert gate["violation_count"] == policy["gap_count"] == broken
    assert policy["quarantined"] is (not expected_blocking and broken > 0)
    # 缺口计数在任何情况下都如实披露
    assert readiness["missing_counts"]["share_capital"] == broken
