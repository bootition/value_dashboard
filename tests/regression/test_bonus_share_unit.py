"""事故出处：reports/139 / 2026-11-18。

行为契约（本文件守的那一条）：`indicator_ext.bonus_share_ratio_3y` 的分子口径是
**每股**送转比例 —— `xdxr.songzhuangu` 存的就是每股口径（10 送 10 → 1.0、
10 送 3 → 0.3），所以三年累计值必须 ≈ `Σ songzhuangu`，**不得再 ÷10**。

事故：`reports/139` §5.3 的 **X-SZ10** —— `app/core/indicators/extended.py` 的
送转 LATERAL 曾写成 `SUM(COALESCE(x.songzhuangu, 0)) / 10.0`（注释还写着
「每 10 股送转 X 股 → 换算成比例」），把每股口径当成了每 10 股口径，
本列系统性少算 10 倍，且不会报错。

判据来源（为什么是「每股」而不是「每 10 股」）：
- `reports/139` §2.4 / §5.3：用 `share_capital_history` 的股本跳变对照送转事件，
  `跳变倍数 / (1 + songzhuangu)` 中位 = 1.000（按 `/10` 解释则 1.09–4.26）；
  按 `songzhuangu` 分 6 档，每档都 = 1.000。
- `docs/runbooks/ops-knowledge-base.md` **D53**（同一结论的知识库裁决）。
- 同源的既有实现：`scripts/backfill_share_capital_from_xdxr.py` 一直用
  `×(1 + songzhuangu)` 回填股本链 —— 两边口径现已对齐（R1 一个事实一个实现）。
- ⚠️ 与 `dividends.dividend_per_share`（D46）**不是一回事**：那一列是「已经 ÷10
  后的每股现金分红」，本处改的是 `xdxr.songzhuangu`，两列不可互相套换算。

反向用例纪律（AGENTS R2）：`test_bonus_share_ratio_rejects_per_ten_share_scaling`
既断言 SQL 结构（分子不得被 10 除），又断言单事件数值（`songzhuangu=1.0` → 1.0 而
不是 0.1）。若有人把 `/10.0` 写回去，本文件的三个用例会同时变红。
"""

from __future__ import annotations

import re

import pytest

from app.core.indicators.extended import ExtendedIndicatorBuilder, build_select_sql
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from tests.support.seed import Seed

STOCK = "600080"
REPORT_DATE = "2024-12-31"


def _seed_report_row(store: DuckDBStore, stock_code: str) -> None:
    """最小骨架：builder 以 income_statement × balance_sheet 内连接为骨架。"""
    with store.transaction() as conn:
        seed = Seed(store).join(conn)
        seed.income(stock_code, REPORT_DATE, revenue=1000.0, net_profit=100.0)
        seed.balance(stock_code, REPORT_DATE, total_assets=5000.0, total_equity=2000.0)


def _seed_bonus_events(
    store: DuckDBStore,
    stock_code: str,
    events: list[tuple[str, float]],
    share_jumps: list[tuple[str, float]],
) -> None:
    """写 xdxr 送转事件 + 与之自洽的 share_capital_history 股本跳变。

    `events` 里的 `songzhuangu` 一律**按每股口径**给（10 送 10 → 1.0）。
    `share_jumps` 是按事件累乘后的股本轨迹（`股本 × (1 + songzhuangu)`），
    它存在的意义是让「每股口径」这件事在 fixture 层就可核对 —— 真实库的判据
    正是拿这条链去反推 `songzhuangu` 的量纲（D53）。
    """
    with store.transaction() as conn:
        seed = Seed(store).join(conn)
        for event_date, songzhuangu in events:
            seed.xdxr(stock_code, event_date, songzhuangu=songzhuangu)
        # 股本链的 `raw_hash`/`batch_id` 显式传：这两列是本 fixture 的一部分
        # （"上一批"标识），不让工厂用确定性默认值替换。
        seed.shares(stock_code, share_jumps, raw_hash="h", batch_id="b")


def _bonus(store: DuckDBStore, stock_code: str = STOCK) -> float | None:
    rows = store.read_query(
        "SELECT bonus_share_ratio_3y FROM indicator_ext "
        "WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
        [stock_code, REPORT_DATE],
    )
    assert rows, "indicator_ext 未生成该股票该报告期的行"
    return rows[0]["bonus_share_ratio_3y"]


def test_bonus_share_ratio_three_year_total_is_per_share_sum(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """三年累计 = Σ 每股送转比例：10送10 + 10送3 + 10转增10 → 1.0+0.3+1.0 = 2.3。

    若口径被改回「每 10 股」（`/10.0`），本断言得到的是 0.23（少 10 倍）。
    """
    _seed_report_row(duckdb_store, STOCK)
    _seed_bonus_events(
        duckdb_store,
        STOCK,
        events=[
            ("2023-05-01", 1.0),   # 10 送 10 → 每股 1.0
            ("2023-10-01", 0.3),   # 10 送 3  → 每股 0.3
            ("2024-06-01", 1.0),   # 10 转增 10 → 每股 1.0
        ],
        share_jumps=[
            ("2020-01-01", 1000.0),
            ("2023-05-01", 2000.0),  # 1000 × (1 + 1.0)
            ("2023-10-01", 2600.0),  # 2000 × (1 + 0.3)
            ("2024-06-01", 5200.0),  # 2600 × (1 + 1.0)
        ],
    )

    report = ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()
    assert report["status"] == "success"

    value = _bonus(duckdb_store)
    assert value is not None
    assert abs(value - 2.3) < 1e-9, f"三年累计送转比例应为 2.3（每股口径），实际 {value}"
    # 反向断言：不能是 0.23（把 songzhuangu 当「每 10 股」再 ÷10 的结果）
    assert abs(value - 0.23) > 1e-9


def test_bonus_share_ratio_rejects_per_ten_share_scaling(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """反向用例：口径一旦改回 `SUM(songzhuangu) / 10`，本用例必须失败。

    两道互不依赖的断言：
    1. **结构**：生成的 SQL 里送转分母不得出现 `/ 10`（注释行先剥掉再匹配，
       避免把说明文字里的写法当成代码）；
    2. **数值**：单事件 `songzhuangu = 1.0`（10 送 10）必须产出 1.0；
       每 10 股口径下会得到 0.1。
    """
    sql = build_select_sql()
    block_match = re.search(r"--[^\n]*送转比例合计.*?\) xr ON true", sql, re.S)
    assert block_match is not None, "生成的 SQL 里找不到送转 LATERAL 子查询"
    code_only = "\n".join(line.split("--", 1)[0] for line in block_match.group(0).splitlines())
    assert re.search(r"SUM\(COALESCE\(x\.songzhuangu,\s*0\)\)\s*/\s*10", code_only) is None, (
        "送转分子不得 ÷10：xdxr.songzhuangu 本身就是每股口径（reports/139 §5.3 X-SZ10 / D53）"
    )
    assert re.search(r"SUM\(COALESCE\(x\.songzhuangu,\s*0\)\)\s+AS\s+bonus", code_only) is not None

    _seed_report_row(duckdb_store, STOCK)
    _seed_bonus_events(
        duckdb_store,
        STOCK,
        events=[("2024-06-01", 1.0)],  # 10 送 10 一次
        share_jumps=[("2020-01-01", 1000.0), ("2024-06-01", 2000.0)],
    )
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()

    value = _bonus(duckdb_store)
    assert value is not None
    assert abs(value - 1.0) < 1e-9, f"10 送 10 应记 1.0（每股口径），实际 {value}"


def test_bonus_share_ratio_outlier_beyond_guard_is_null(
    duckdb_store: DuckDBStore, database_paths: DatabasePathSet
) -> None:
    """护栏仍按每股口径生效：合计 > BONUS_MAX(20) 的病理值写 NULL，不展示 99.4。

    背景：正式库 `xdxr` 实测存在 `songzhuangu = 99.0` 的行（量纲异常），
    修好口径后这类行会变成 99.4 级合计，必须继续被 `BONUS_MAX` 拦掉。
    """
    _seed_report_row(duckdb_store, STOCK)
    _seed_bonus_events(
        duckdb_store,
        STOCK,
        events=[("2024-06-01", 99.0)],
        share_jumps=[("2020-01-01", 1000.0), ("2024-06-01", 100000.0)],
    )
    ExtendedIndicatorBuilder(duck=duckdb_store, paths=database_paths).build()

    assert _bonus(duckdb_store) is None


if __name__ == "__main__":  # pragma: no cover - 手工调试入口
    raise SystemExit(pytest.main([__file__, "-q"]))
