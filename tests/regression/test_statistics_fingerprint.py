"""事故出处：reports/118 / 2026-11-13。

Regression: 逐股统计指纹不得包含全库聚合量。

2026-11-13 生产发现：`StatisticsBuilder._stock_fingerprints()` 把
`SELECT COUNT(*) FROM share_capital_history WHERE verified`（**全库**计数）
混进每只股票的指纹，而 `capital_history` 每轮核验 20 只股票就会让该计数
+20 → 5,580 只指纹全部失效 → 每轮全量重建统计域（实测 285–305 s/轮，
占无缺口轮次 65–76%），轮次时长因此永远达不到 3–5 min 基线。

修复后：逐股指纹只用逐股输入（含该股自己的 verified 计数）。一次核验
只应让被核验的那几只股票重算。
"""

from __future__ import annotations

from app.core.statistics import StatisticsBuilder
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed

_CAP_ROWS = [
    # (stock_code, effective_date, total_shares, verified)
    ("000001", "2020-01-01", 100, True),
    ("000001", "2021-01-01", 110, True),
    ("000002", "2020-01-01", 200, True),
    ("000002", "2021-01-01", 210, True),
    ("000003", "2020-01-01", 300, True),
    ("000003", "2021-01-01", 310, True),
]


def _seed(store: DuckDBStore) -> None:
    seed = Seed(store)
    for code in ("000001", "000002", "000003"):
        # `stock_meta` → `Seed.stock()`：`exchange='SZSE'` 与代码前缀推断一致（不额外指定）。
        seed.stock(
            code,
            name=f"Test{code}",
            listing_date="2020-01-01",
            is_st=False,
            is_suspended=False,
            total_shares=100,
            circ_shares=80,
        )
        # `price_daily_raw` → `Seed.prices()`：`volume` 是逐行业务列，仍逐字出现。
        seed.prices(code, [("2024-01-02", 10)], volume=100)
    for code, effective_date, shares, verified in _CAP_ROWS:
        # `share_capital_history` → `Seed.shares()`：`verified` 逐行不同，走 `**cols`。
        seed.shares(code, [(effective_date, shares)], verified=verified)


def test_verifying_one_stock_does_not_invalidate_everyone(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed(duckdb_store)
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    codes = ["000001", "000002", "000003"]

    before = builder._stock_fingerprints(codes)
    assert set(before) == set(codes)

    # 只把 000002 的一行标记为"未核验"（等价于资本链核验状态变化）。
    duckdb_store.write_query(
        "UPDATE share_capital_history SET verified = FALSE "
        "WHERE stock_code = '000002' AND effective_date = DATE '2021-01-01'"
    )

    after = builder._stock_fingerprints(codes)

    assert after["000002"] != before["000002"], "被改动股票的指纹必须变化"
    assert after["000001"] == before["000001"], (
        "其他股票的指纹不得因**别处**的核验状态变化而失效（旧实现会全部失效，"
        "导致每轮全量重建统计域）"
    )
    assert after["000003"] == before["000003"]


def test_global_verified_count_no_longer_feeds_per_stock_fingerprint(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """新增一只股票的核验记录，不应影响已有股票的指纹。"""
    _seed(duckdb_store)
    builder = StatisticsBuilder(duck=duckdb_store, sqlite=sqlite_store)
    before = builder._stock_fingerprints(["000001"])

    seed = Seed(duckdb_store)
    # `stock_meta` + `share_capital_history` → 工厂（此处**没有** lastrowid 回捞，
    # 新增行本身只是"别处多了一条已核验记录"）。
    seed.stock(
        "000004",
        name="Test4",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
        total_shares=100,
        circ_shares=80,
    )
    seed.shares("000004", [("2020-01-01", 400)], verified=True)

    after = builder._stock_fingerprints(["000001"])
    assert after["000001"] == before["000001"]
